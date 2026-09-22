# -*- coding: utf-8 -*-
"""删除回溯记录存储（deletion 包最底层）：记录持久化与 TTL 回溯清理。

职责：- TRAIL_FILE / _lock：记录文件路径与包内唯一的记录互斥锁
- load_records()/save_records()：容错读 + 原子写（绝不截断原文件；失败返回 False）
- new_record()/add_record()/already_handled()：建档、落盘、去重判定
- update_record()/get_record()/mark_kept()/mark_failed()/mark_deleted()：状态推进
- mark_deleting()：删除动作前预写「正在删除…」意图（崩溃窗口不丢还原明细）
- prune_records()：启动时按 deleted_at TTL（默认 30 天）清理旧记录，防累积
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

# 回溯记录 TTL（天）：prune_records 按 deleted_at 老化清理的默认期限（本任务不加配置键）。
# kept 记录不受此期限限制：只要源文件仍在原位就无条件保留（already_handled 靠它跳过
# 已处理文件），详见 prune_records。
TRAIL_TTL_DAYS = 30

# 保存失败提示只打一次（进程级）：stdout 会被 GUI 捕获进日志，重复刷屏无意义。
_save_failed_warned = False


def _warn_save_failed():
    """写盘失败提示：每进程只打印一次（供 GUI 日志可见，绝不静默丢明细）。"""
    global _save_failed_warned
    if _save_failed_warned:
        return
    _save_failed_warned = True
    print("[回溯] 写入 deletion_trail.json 失败，本次删除明细可能未保存")

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

    返回 True=已原子落盘；False=写失败（原文件保持原样）。绝不抛出：调用方
    （删除主流程）不能因为记录写入失败而中断；失败时打一条进程级一次性提示，
    让 GUI 日志如实可见「明细可能未保存」。
    """
    try:
        data = json.dumps(records, ensure_ascii=False, indent=2)
        tmp = TRAIL_FILE.with_name(TRAIL_FILE.name + ".tmp")
        tmp.write_text(data, encoding="utf-8")
        os.replace(tmp, TRAIL_FILE)
        return True
    except Exception:
        _warn_save_failed()
        return False


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
        "status": "recorded",      # recorded/deleting/kept/deleted/restored/failed
        "file_size": fsize,        # 处理时的文件身份，用于识别同名新文件
        "file_mtime": fmtime,
        "deleted_paths": [],       # 已移入回收站、可还原的路径
        "failed_paths": [],        # 永久删除、无法还原的路径
        "kept_paths": [],          # 回收站不可用且策略保留时留在原位、未删除的路径
        "quarantine_map": [],      # 回收站不可用时移入隔离区、可还原的文件地图
                                   # [{"from": 原路径, "to": 隔离区路径}, ...]
        "delete_targets": [],      # 删除前预写的本次删除目标（mark_deleting 写入）
        "deleted_at": "",
        "note": "",
    }


def _boot_time():
    """系统本次开机时间（Unix 秒）。取不到时返回 0。

    注意：prune_records 已改为按 deleted_at TTL 清理，不再使用本函数；函数本身
    保留是因为 trail 兼容 shim 仍从本模块再导出 _boot_time（旧名面仍需存在），
    直接删除会破坏该再导出。
    """
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


def _record_age_ts(rec):
    """记录的老化基准时间：优先 deleted_at（删除/还原完成的时刻），缺失或解析失败
    时回退 created_ts/created_at；都取不到返回 0（调用方按「不过期」保守处理）。"""
    raw = rec.get("deleted_at")
    if isinstance(raw, str) and raw.strip():
        try:
            return time.mktime(time.strptime(raw, "%Y-%m-%d %H:%M:%S"))
        except Exception:
            pass
    return _record_ts(rec)


def _should_keep_record(rec, cutoff):
    """TTL 清理判定：True=保留，False=可清理。任何异常一律按保留处理（绝不误删）。"""
    try:
        status = rec.get("status")
        if status in ("recorded", "deleting"):
            return True     # 崩溃窗口中间态：无论多旧都保留，删除去向/还原明细不能丢
        if status == "kept":
            # kept：源文件仍在原位就必须保留——already_handled 靠它跳过已处理文件，
            # 清掉会导致同一文件被重新解压；仅当源文件已不存在才允许按 TTL 老化。
            p = str(rec.get("original_path") or "")
            if not p or Path(p).exists():
                return True
        ts = _record_age_ts(rec)
        if ts <= 0:
            return True     # 无法判龄（字段缺失/损坏）：保守保留
        return ts >= cutoff
    except Exception:
        return True


def prune_records():
    """启动时按 TTL 清理回溯记录（不再依赖开机时间窗），防记录无限累积。

    清理规则：
    - 老化基准 = deleted_at（缺失/解析失败回退 created_ts）：仅已完成且超过
      TRAIL_TTL_DAYS 天的记录才会被清理；
    - status=recorded/deleting：崩溃窗口中间态，无论多旧一律保留；
    - status=kept：源文件仍在原位时一律保留（见 _should_keep_record），
      源文件已不存在时才按 TTL 清理；
    - failed/restored/deleted：按 TTL 正常老化。
    返回 True=无失败（含无可清理项）；False=清理结果写盘失败。绝不抛出。
    """
    cutoff = time.time() - TRAIL_TTL_DAYS * 86400.0
    with _lock:
        recs = load_records()
        kept = []
        pruned = False
        for r in recs:
            if _should_keep_record(r, cutoff):
                kept.append(r)
            else:
                pruned = True
        if pruned:
            return save_records(kept)
    return True


def add_record(rec):
    """落盘一条新记录；返回保存结果（True/False），不改变原有语义。"""
    with _lock:
        recs = load_records()
        recs.insert(0, rec)
        return save_records(recs)


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


def mark_restored_exempt(original_path):
    """登记「从删除回溯还原回来的源文件 ⇒ 豁免再次解压」（路径 + 身份）。

    用户从回溯页还原某个源文件，意图就是**完整保留这份源文件**；所以它再次出现在
    监听目录里时应当跳过解压——否则解压成功后又会被 delete_policy 删掉（这正是
    「还原后立刻又被解压、再被删」的根因）。身份 = (size, mtime)：文件被重新下载 /
    替换（身份变化）后豁免立即失效，绝不误挡真正的新文件。

    只对「已经回到原位」的 original_path 生效（不在则自动 no-op）。绝不抛异常。
    """
    try:
        target = str(Path(original_path).resolve())
        st = Path(target).stat()
        ident = [int(st.st_size), float(st.st_mtime)]
    except Exception:
        return False
    try:
        with _lock:
            recs = load_records()
            for r in recs:
                if r.get("original_path") != target:
                    continue
                entries = r.get("restored_exempt")
                entries = list(entries) if isinstance(entries, list) else []
                entries = [e for e in entries
                           if not (isinstance(e, dict) and e.get("path") == target)]
                entries.append({"path": target, "ident": ident})
                r["restored_exempt"] = entries
                return save_records(recs)
    except Exception:
        return False
    return False


def is_restored_exempt(original_path):
    """该源文件是否处于「已还原 ⇒ 豁免」状态**且身份未变**。

    身份（size, mtime）对不上 → 说明已被重新下载/替换，豁免立即失效并顺手清掉那条
    登记（绝不让豁免黏在别的文件上）。任何异常一律按「不豁免」处理。
    """
    try:
        target = str(Path(original_path).resolve())
        st = Path(target).stat()
        ident = [int(st.st_size), float(st.st_mtime)]
    except Exception:
        return False
    hit = False
    dirty = False
    try:
        with _lock:
            recs = load_records()
            for r in recs:
                entries = r.get("restored_exempt")
                if not isinstance(entries, list):
                    continue
                keep = []
                for e in entries:
                    if not (isinstance(e, dict) and e.get("path") == target):
                        keep.append(e)
                        continue
                    if list(e.get("ident") or []) == ident:
                        hit = True
                        keep.append(e)
                    else:
                        dirty = True        # 身份变了 → 豁免失效
                if len(keep) != len(entries):
                    r["restored_exempt"] = keep
                    dirty = True
            if dirty:
                save_records(recs)
    except Exception:
        return False
    return hit


def update_record(rec_id, **fields):
    """按 id 更新记录字段并落盘；返回保存结果（True/False）。绝不抛出。"""
    with _lock:
        recs = load_records()
        for r in recs:
            if r.get("id") == rec_id:
                r.update(fields)
                break
        return save_records(recs)


def mark_deleting(rec_id, targets):
    """删除动作开始前预写「正在删除…」意图记录。

    删除先于结果落盘的崩溃窗口里，记录若停在「已记录（处理中）」就永远看不到
    删除去向；先把本次将删除的路径写入 delete_targets 并置 status=deleting，
    崩溃后至少能知道当时正在删什么。targets=本次将回收/删除的路径（可空）。
    返回保存结果（True/False）。
    """
    return update_record(rec_id, status="deleting",
                         delete_targets=[str(p) for p in (targets or [])])


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
            "kept_paths": [],       # 隔离模式不存在「保留在原位」
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
        "kept_paths": still_here,   # 仍留在原位、未被删除的路径（混合场景也有据可查）
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
