# -*- coding: utf-8 -*-
"""删除回溯：记录解压源文件、删除到回收站（可撤销）并从回收站还原。

职责：- 为最初始源文件建档（多层解压产生的中间文件不标记）
- send_to_recycle_bin() 用 SHFileOperation(FOF_ALLOWUNDO) 移入回收站而非永久删除
- restore_record() 经 Shell.Application 从回收站一键还原
- 回溯窗口期 = 本次开机，启动时 prune_records() 丢弃旧记录防累积
关键入口：new_record() / already_handled() / send_to_recycle_bin() / restore_record()
依赖：ctypes（SHFileOperation）、win32com（还原）、DATA_DIR/deletion_trail.json
注意：回收站被清空或永久删除的文件无法还原
"""
import os
import time
import json
import uuid
import shutil
import threading
from pathlib import Path
from ctypes import wintypes
import ctypes

from .paths import DATA_DIR as APP_DIR
from .utils import same_volume
TRAIL_FILE = APP_DIR / "deletion_trail.json"
_lock = threading.Lock()

# ---- 删除到回收站（SHFileOperation, FOF_ALLOWUNDO） ----
FO_DELETE = 0x0003
FOF_ALLOWUNDO = 0x0040
FOF_NOCONFIRMATION = 0x0010
FOF_SILENT = 0x0004

# 隔离区目录名（回收站不可用时源文件的可还原落点）：<root>/_已删除/<YYYY-MM-DD>。
# 监听扫描必须跳过该目录（见 monitors._in_quarantine），否则隔离文件会被当成新压缩包重解。
QUARANTINE_DIRNAME = "_已删除"


class SHFILEOPSTRUCTW(ctypes.Structure):
    _fields_ = [
        ("hwnd", wintypes.HWND),
        ("wFunc", wintypes.UINT),
        ("pFrom", wintypes.LPCWSTR),
        ("pTo", wintypes.LPCWSTR),
        ("fFlags", ctypes.c_ushort),
        ("fAnyOperationsAborted", wintypes.BOOL),
        ("hNameMappings", ctypes.c_void_p),
        ("lpszProgressTitle", wintypes.LPCWSTR),
    ]


def send_to_recycle_bin(paths):
    """把一组存在的文件移入回收站。

    返回 (全部成功: bool, 仍残留的路径: list[str])
    """
    paths = [str(p) for p in (paths or []) if p]
    paths = [p for p in paths if Path(p).exists()]
    if not paths:
        return True, []
    sh = SHFILEOPSTRUCTW()
    sh.hwnd = None
    sh.wFunc = FO_DELETE
    sh.pFrom = "\x00".join(paths) + "\x00\x00"
    sh.pTo = None
    sh.fFlags = FOF_ALLOWUNDO | FOF_NOCONFIRMATION | FOF_SILENT
    sh.fAnyOperationsAborted = False
    sh.hNameMappings = None
    sh.lpszProgressTitle = None
    try:
        ret = ctypes.windll.shell32.SHFileOperationW(ctypes.byref(sh))
        ok = (ret == 0)
    except Exception:
        ok = False
    if not ok:
        return False, [p for p in paths if Path(p).exists()]
    failed = [p for p in paths if Path(p).exists()]
    return (not failed), failed


# ---- 卷回收站可用性探测（只读；绝不弹窗 / 绝不删除 / 绝不抛异常） ----
DRIVE_REMOVABLE = 2   # 可移动盘（U 盘 / 移动硬盘）
DRIVE_FIXED = 3       # 固定盘
DRIVE_REMOTE = 4      # 网络盘
DRIVE_RAMDISK = 6     # 内存盘


def volume_has_recycle_bin(path):
    """探测 path 所在卷是否有可用的回收站，返回 True / False / None（不确定）。

    判定规则：
    - UNC 路径（\\\\server\\share…）没有「本机回收站」概念 → False；
    - 可移动盘 / 网络盘 / 内存盘 → False（这类卷默认不带回收站）；
    - 固定盘 → 用 SHQueryRecycleBinW 实测该卷根的回收站是否可用：
      调用成功(0) → True，明确失败 → False；
    - 其它盘型 / 非 Windows / 探测过程任何异常 → None（不确定，调用方保守处理）。

    只做只读探测：绝不弹窗、绝不删除、绝不抛异常。"""
    if os.name != "nt":
        return None
    try:
        p = str(path or "")
        if p.startswith("\\\\") or p.startswith("//"):
            return False
        drive = os.path.splitdrive(os.path.abspath(p))[0]
        if not drive:
            return False
        root = drive + "\\"
        dtype = int(ctypes.windll.kernel32.GetDriveTypeW(root))
        if dtype in (DRIVE_REMOVABLE, DRIVE_REMOTE, DRIVE_RAMDISK):
            return False
        if dtype != DRIVE_FIXED:
            return None

        class SHQUERYRBINFO(ctypes.Structure):
            _fields_ = [
                ("cbSize", wintypes.DWORD),
                ("i64Size", ctypes.c_longlong),
                ("i64NumItems", ctypes.c_longlong),
            ]

        info = SHQUERYRBINFO()
        info.cbSize = ctypes.sizeof(SHQUERYRBINFO)
        ret = ctypes.windll.shell32.SHQueryRecycleBinW(root, ctypes.byref(info))
        return ret == 0
    except Exception:
        return None


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


# ---- 从回收站还原 ----
def _invoke_restore(item):
    """触发回收站项的还原。优先 InvokeVerb('undelete')，失败再扫描本地化动词"""
    try:
        item.InvokeVerb("undelete")
        return True
    except Exception:
        pass
    try:
        verbs = item.Verbs()
        count = int(getattr(verbs, "Count", 0))
        for i in range(count):
            v = verbs.Item(i)
            name = ""
            try:
                name = str(v.Name or "")
            except Exception:
                name = ""
            low = name.lower()
            if "undelete" in low or "restore" in low or "还原" in name:
                v.DoIt()
                return True
    except Exception:
        pass
    return False


def _restore_one(original_path):
    """从回收站还原单个文件到原位置。返回是否成功"""
    original_path = str(Path(original_path))
    opath = Path(original_path)
    name = opath.name
    parent = str(opath.parent)
    try:
        import pythoncom
        import win32com.client
        pythoncom.CoInitialize()
        shell = win32com.client.Dispatch("Shell.Application")
        rb = shell.Namespace(10)  # 回收站
        if rb is None:
            return False
        found = False
        for item in rb.Items():
            try:
                it_name = str(item.Name or "")
                it_parent = str(item.ExtendedProperty("System.Recycle.DeletedFrom") or "")
            except Exception:
                continue
            # 回收站的 DeletedFrom 只给原始目录，因此用「文件名 + 原始目录」匹配
            # Windows 路径大小写不敏感，统一 normcase 后比较
            if (os.path.normcase(it_name) == os.path.normcase(name)
                    and os.path.normcase(it_parent) == os.path.normcase(parent)):
                found = True
                _invoke_restore(item)
                for _ in range(60):  # 还原是异步的，最长等 30 秒
                    time.sleep(0.5)
                    if Path(original_path).exists():
                        return True
        return found and Path(original_path).exists()
    except Exception:
        return False


def restore_record(rec_id):
    """还原记录中已删除（在回收站）的初始源文件。

    返回 (成功: bool, 消息: str)
    """
    rec = get_record(rec_id)
    if rec is None:
        return False, "记录不存在"
    if rec.get("status") != "deleted":
        return False, "仅「已删除」状态的记录可还原"
    targets = rec.get("deleted_paths") or []
    if not targets:
        return False, "没有可还原的文件"
    restored, failed = [], []
    for t in targets:
        if _restore_one(t):
            restored.append(t)
        else:
            failed.append(t)
    if restored:
        status = "restored" if not failed else "deleted"
        note = f"已还原 {len(restored)}/{len(targets)} 个文件"
        if failed:
            note += "；失败: " + ", ".join(os.path.basename(f) for f in failed)
        update_record(rec_id, status=status, note=note)
        if not failed:
            return True, f"已还原 {len(restored)} 个文件"
        return True, f"还原 {len(restored)}/{len(targets)}，部分失败：{os.path.basename(failed[0])}"
    return False, "还原失败：回收站中找不到对应文件，或已被永久删除"


# ---- 隔离区（回收站不可用时源文件的可还原落点） ----
def quarantine_target_dir(root, when=None):
    """隔离区目标目录：<root>/_已删除/<YYYY-MM-DD>。"""
    base = Path(str(root)) if str(root or "").strip() else Path(".")
    day = time.strftime("%Y-%m-%d",
                        when if when is not None else time.localtime())
    return base / QUARANTINE_DIRNAME / day


def _unique_quarantine_dest(dest):
    """不覆盖已有文件的落点：同名冲突时在扩展名前追加 " (2)"、" (3)"…"""
    dest = Path(dest)
    if not dest.exists():
        return dest
    parent, stem, suffix = dest.parent, dest.stem, dest.suffix
    i = 2
    while True:
        cand = parent / f"{stem} ({i}){suffix}"
        if not cand.exists():
            return cand
        i += 1


def _same_volume(src, dest_root):
    """src 与 dest_root 是否同一卷：优先 st_dev，取不到退回盘符比较。

    薄委托：同卷判定口径统一由 utils.same_volume 提供（保持既有名字，内部
    调用点不改）。注意 volume_pair._same_volume 是另一套「仅盘符」语义，
    为硬链接安全刻意保留，两者不可合并。
    """
    return same_volume(src, dest_root)


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


def move_to_quarantine(paths, root):
    """把一组文件移入隔离区，返回 (moved, failed)。

    moved 每项为 {"from": 原绝对路径, "to": 隔离区绝对路径}；failed 为仍未能移走、
    原样留在原位的路径。同卷走 os.replace 秒改名，跨卷走 shutil.move 复制+删除；
    目标已存在同名文件时在扩展名前追加 " (2)"、" (3)"… 绝不覆盖。root 为空时按每个
    文件自身所在目录补 _已删除（无 output_dir 时的兜底）。**绝不抛异常**。
    """
    moved, failed = [], []
    for raw in (paths or []):
        src = Path(str(raw))
        orig = os.path.abspath(str(raw))
        try:
            if not src.exists():
                continue
            base = str(root).strip()
            root_dir = Path(base) if base else src.parent
            dest_dir = quarantine_target_dir(root_dir)
            dest_dir.mkdir(parents=True, exist_ok=True)
            dest = _unique_quarantine_dest(dest_dir / src.name)
            if _same_volume(src, dest_dir):
                # 同卷：瞬间改名。dest 已是唯一落点，os.replace 不会覆盖他人文件。
                os.replace(str(src), str(dest))
            else:
                # 跨卷：shutil.move 复制后删除源；失败时源仍在，绝不删用户数据。
                shutil.move(str(src), str(dest))
            if src.exists():
                # 源仍在（如跨卷复制成功但删除失败）：如实算失败，源与隔离副本都保留。
                failed.append(orig)
                continue
            moved.append({"from": orig, "to": os.path.abspath(str(dest))})
        except Exception:
            failed.append(orig)
    return moved, failed


def _quarantine_entries(rec):
    out = []
    for e in (rec.get("quarantine_map") or []):
        if isinstance(e, dict) and e.get("from") and e.get("to"):
            out.append({"from": str(e["from"]), "to": str(e["to"])})
    return out


def quarantine_restore(rec_id):
    """把隔离区里的文件移回原位置，返回 (ok, skipped, failed)。

    - 原位置已有文件 / 隔离文件缺失 → 跳过（绝不覆盖，也不报失败）；
    - 成功移回 → 从 quarantine_map 移除；全部清空时 status="restored"；
    - 跳过（原位置已占用）的条目仍留在隔离区与地图里，等待用户处理；
    - 备注如实写明还原 / 跳过 / 失败数量。
    """
    rec = get_record(rec_id)
    if rec is None:
        return False, [], []
    entries = _quarantine_entries(rec)
    if not entries:
        return False, [], []
    restored, skipped, failed, left = [], [], [], []
    for e in entries:
        src = Path(e["to"])
        dst = Path(e["from"])
        if dst.exists():
            skipped.append(e["from"])
            left.append(e)          # 原位置已有文件：隔离副本保留，不覆盖
            continue
        if not src.exists():
            skipped.append(e["to"])  # 隔离文件已不在：视为无需还原，丢弃该条目
            continue
        try:
            dst.parent.mkdir(parents=True, exist_ok=True)
            if _same_volume(src, dst.parent):
                os.replace(str(src), str(dst))
            else:
                shutil.move(str(src), str(dst))
            if dst.exists() and not src.exists():
                restored.append(e["from"])
            else:
                failed.append(e["from"])
                left.append(e)
        except Exception:
            failed.append(e["from"])
            left.append(e)
    parts = []
    if restored:
        parts.append(f"已还原 {len(restored)} 个")
    if skipped:
        parts.append(f"跳过 {len(skipped)} 个（原位置已存在或隔离文件缺失）")
    if failed:
        parts.append(f"失败 {len(failed)} 个")
    if not parts:
        parts.append("无可还原文件")
    fields = {"quarantine_map": left,
              "note": "隔离区还原：" + "；".join(parts)}
    if not left:
        fields["status"] = "restored"
    update_record(rec_id, **fields)
    return (bool(restored) and not failed), skipped, failed


def quarantine_purge(rec_id):
    """永久删除该记录隔离区里的文件，返回 (ok, failed)。

    只删 quarantine_map 里仍存在的隔离副本；失败的原样保留在地图里。绝不抛异常。
    """
    rec = get_record(rec_id)
    if rec is None:
        return False, []
    entries = _quarantine_entries(rec)
    if not entries:
        return False, []
    failed, left, purged = [], [], 0
    for e in entries:
        src = Path(e["to"])
        try:
            if src.is_dir():
                shutil.rmtree(str(src))
                purged += 1
            elif src.exists():
                src.unlink()
                purged += 1
            # 不存在 = 已被清空，直接丢弃条目
        except Exception:
            failed.append(e["to"])
            left.append(e)
    note = f"隔离区已永久删除 {purged} 个文件"
    if failed:
        note += f"；失败 {len(failed)} 个"
    update_record(rec_id, quarantine_map=left, note=note)
    return (not failed), failed


def quarantine_stats():
    """统计所有记录隔离区里「此刻仍存在」的文件数与总字节数，返回 (files, bytes)。"""
    files, total = 0, 0
    for r in load_records():
        for e in _quarantine_entries(r):
            try:
                p = Path(e["to"])
                if p.is_file():
                    total += p.stat().st_size
                    files += 1
            except Exception:
                continue
    return files, total
