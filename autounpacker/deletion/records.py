# -*- coding: utf-8 -*-
"""删除回溯记录存储（deletion 包最底层）：记录持久化与「本次开机」回溯窗口。

职责：- TRAIL_FILE / _lock：记录文件路径与包内唯一的记录互斥锁
- load_records()/save_records()：容错读 + 原子写（绝不截断原文件）
- new_record()/add_record()/already_handled()：建档、落盘、去重判定
- update_record()/get_record()/mark_kept()/mark_failed()/mark_deleted()：状态推进
- prune_records()：启动时丢弃上次开机前的旧记录，防累积
- QUARANTINE_DIRNAME / _quarantine_note_dir()：隔离区目录名与回溯备注（纯函数）
依赖：标准库（os/time/json/uuid/threading/ctypes）+ paths（DATA_DIR/deletion_trail.json）
注意：本模块是 deletion 包最底层，绝不导入 deletion.* 任何其它模块；
      所有记录变更统一走 _lock，全包共用这一个锁
"""
import os
import time
import json
import uuid
import threading
from pathlib import Path
import ctypes

from ..paths import DATA_DIR as APP_DIR
TRAIL_FILE = APP_DIR / "deletion_trail.json"
_lock = threading.Lock()

# 隔离区目录名（回收站不可用时源文件的可还原落点）：<root>/_已删除/<YYYY-MM-DD>。
# 监听扫描必须跳过该目录（见 monitors._in_quarantine），否则隔离文件会被当成新压缩包重解。
QUARANTINE_DIRNAME = "_已删除"


# ---- 记录持久化 ----
def load_records():
    """读取回溯记录：文件不存在 / 为空 / 半截 / 非列表一律返回 []，绝不抛进 UI。

    旧实现遇空文件或半截 JSON 时 json.loads 抛异常，由 except 吞掉后返回 []，
    已经能做到「不崩」；这里显式跳过空白内容，语义更清晰也只是加固。
    """
    try:
        if TRAIL_FILE.exists():
            raw = TRAIL_FILE.read_text(encoding="utf-8").strip()
            if raw:
                data = json.loads(raw)
                if isinstance(data, list):
                    return data
    except Exception:
        pass
    return []


def save_records(records):
    """原子写回溯记录：先写同目录临时文件再 os.replace，绝不截断原文件。

    旧实现直接 Path.write_text 覆盖 deletion_trail.json：该调用会先截断文件，
    若进程在截断后、写完整前崩溃/断电/被杀，文件会留下空内容或半截 JSON，
    于是**整个还原回溯丢失**，already_handled 对所有文件都返回 False（重复
    处理、已删除的文件也失去可还原记录）。改为与 state._save_temp_passwords /
    config.save_config 相同的模式：先把完整 JSON 写进同目录 .tmp，再原子替换；
    序列化或写入任一步失败都会保留原文件原样。
    """
    try:
        data = json.dumps(records, ensure_ascii=False, indent=2)
        tmp = TRAIL_FILE.with_name(TRAIL_FILE.name + ".tmp")
        tmp.write_text(data, encoding="utf-8")
        os.replace(tmp, TRAIL_FILE)
    except Exception:
        pass


def new_record(original_path, watch_dir):
    """初始源文件的回溯记录（仅最初始源文件，不含中间文件）"""
    try:
        st = Path(original_path).stat()
        fsize, fmtime = st.st_size, st.st_mtime
    except OSError:
        fsize, fmtime = None, None
    return {
        "id": uuid.uuid4().hex[:12],
        "created_at": time.strftime("%Y-%m-%d %H:%M:%S"),
        "created_ts": time.time(),
        "original_path": str(Path(original_path).resolve()),
        "name": Path(original_path).name,
        "watch_dir": str(watch_dir or Path(original_path).parent),
        "status": "recorded",      # recorded/kept/deleted/restored/failed
        "file_size": fsize,        # 处理时的文件身份，用于识别同名新文件
        "file_mtime": fmtime,
        "deleted_paths": [],       # 已移入回收站、可还原的路径
        "failed_paths": [],        # 永久删除、无法还原的路径
        "quarantine_map": [],      # 回收站不可用时移入隔离区、可还原的文件地图
                                   # [{"from": 原路径, "to": 隔离区路径}, ...]
        "deleted_at": "",
        "note": "",
    }


def _boot_time():
    """系统本次开机时间（Unix 秒）。取不到时返回 0（不清旧记录）"""
    try:
        ticks = ctypes.windll.kernel32.GetTickCount64()
        return time.time() - ticks / 1000.0
    except Exception:
        return 0.0


def _record_ts(rec):
    ts = rec.get("created_ts")
    if isinstance(ts, (int, float)):
        return float(ts)
    try:
        return time.mktime(time.strptime(rec.get("created_at", ""), "%Y-%m-%d %H:%M:%S"))
    except Exception:
        return 0.0


def prune_records():
    """删除回溯窗口期 = 本次开机内。

    程序启动时调用：丢弃本次开机之前产生的记录，
    避免 deletion_trail.json 无限累积变大。
    """
    boot = _boot_time()
    if boot <= 0:
        return
    with _lock:
        recs = load_records()
        kept = [r for r in recs if _record_ts(r) >= boot - 1]
        if len(kept) != len(recs):
            save_records(kept)


def add_record(rec):
    with _lock:
        recs = load_records()
        recs.insert(0, rec)
        save_records(recs)


def already_handled(original_path):
    """该源文件是否已被成功处理过（kept/deleted/restored）。

    程序重启或监听路径重初始化后重新扫描目录时，用于跳过这些文件，
    避免重复解压/重复建档。failed/recorded 视为未完成，会重试。
    同名新文件（大小/时间与记录不一致，如重新下载或替换）不算已处理。
    """
    target = str(Path(original_path).resolve())
    with _lock:
        for r in load_records():
            if r.get("original_path") != target:
                continue
            if r.get("status") not in ("kept", "deleted", "restored"):
                continue
            fsize, fmtime = r.get("file_size"), r.get("file_mtime")
            if fsize is not None and fmtime is not None:
                try:
                    st = Path(target).stat()
                except OSError:
                    return False
                if (st.st_size, st.st_mtime) != (fsize, fmtime):
                    return False  # 同名新文件，身份已变，需要重新处理
                return True
            # 旧记录（无身份信息）：已删除的文件现在又存在，说明是重新
            # 下载/替换的同名新文件，需要重新处理
            if r.get("status") == "deleted":
                try:
                    if Path(target).exists():
                        return False
                except OSError:
                    pass
            return True
    return False


def update_record(rec_id, **fields):
    with _lock:
        recs = load_records()
        for r in recs:
            if r.get("id") == rec_id:
                r.update(fields)
                break
        save_records(recs)


def get_record(rec_id):
    with _lock:
        for r in load_records():
            if r.get("id") == rec_id:
                return json.loads(json.dumps(r))
    return None


def mark_kept(rec_id):
    """解压成功但未删除源文件"""
    update_record(rec_id, status="kept", note="未删除源文件")


def mark_failed(rec_id, err):
    """解压失败，源文件未处理删除"""
    update_record(rec_id, status="failed", note=f"解压失败: {err}")


def mark_deleted(rec_id, recycled, failed, quarantine_map=None):
    """解压后删除源文件：recycled=已移入回收站(可还原)，failed=回收站不可用时的残留。

    quarantine_map 非空（quarantine 策略且回收站不可用）时：文件已移入隔离区、可还原，
    记入 quarantine_map，failed_paths 保持空（没有永久删除，绝不让 UI 谎称「无法还原」），
    备注写隔离目录。

    否则 failed 的最终归属取决于调用方的回退策略：可能已被永久删除（auto / permanent），
    也可能按「保留源文件」策略原样留在原位（keep）。这里按文件是否仍存在如实区分：
    仍存在=保留未删，已不存在=永久删除——绝不把「保留」写成「已永久删除」。"""
    recycled = [str(p) for p in (recycled or [])]
    failed = [str(p) for p in (failed or [])]
    qmap = []
    for e in (quarantine_map or []):
        if not isinstance(e, dict):
            continue
        frm, to = str(e.get("from") or ""), str(e.get("to") or "")
        if frm and to:
            qmap.append({"from": frm, "to": to})
    if qmap:
        fields = {
            "status": "deleted",
            "deleted_paths": recycled,
            "quarantine_map": qmap,
            "failed_paths": [],     # 隔离区无永久删除
            "deleted_at": time.strftime("%Y-%m-%d %H:%M:%S"),
            "note": "回收站不可用，已移入隔离区（可还原）：" + _quarantine_note_dir(qmap),
        }
        update_record(rec_id, **fields)
        return
    still_here, gone = [], []
    for p in failed:
        try:
            (still_here if Path(p).exists() else gone).append(p)
        except Exception:
            gone.append(p)
    fields = {
        "status": "deleted",
        "deleted_paths": recycled,
        "quarantine_map": [],
        "failed_paths": gone,       # 仅真正永久删除、不可还原的路径
        "deleted_at": time.strftime("%Y-%m-%d %H:%M:%S"),
    }
    if gone:
        fields["note"] = "部分文件未能移入回收站，已永久删除，无法还原"
    elif still_here:
        # 回收站不可用且策略为「保留源文件」：原文件留在原位，并未删除
        if not recycled:
            fields["status"] = "kept"
        fields["note"] = "回收站不可用，已按「保留源文件」策略保留，未删除源文件"
    update_record(rec_id, **fields)


def _quarantine_note_dir(qmap):
    """取隔离区地图里公共的隔离目录，供回溯备注展示。"""
    dirs = []
    for e in qmap or []:
        to = str(e.get("to") or "")
        if to:
            dirs.append(str(Path(to).parent))
    if not dirs:
        return ""
    if len(dirs) == 1:
        return dirs[0]
    try:
        return os.path.commonpath(dirs)
    except Exception:
        return dirs[0]
