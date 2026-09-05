# -*- coding: utf-8 -*-
"""
删除回溯模块
- 统一标记最初始源文件（多层解压产生的次级中间文件不标记）
- 删除源文件时移入回收站（可撤销），而不是永久删除
- 提供从回收站还原已删除源文件的功能（回收站被清空/永久删除的无法还原）
"""
import os
import time
import json
import uuid
import threading
from pathlib import Path
from ctypes import wintypes
import ctypes

from .paths import DATA_DIR as APP_DIR
TRAIL_FILE = APP_DIR / "deletion_trail.json"
_lock = threading.Lock()

# ---- 删除到回收站（SHFileOperation, FOF_ALLOWUNDO） ----
FO_DELETE = 0x0003
FOF_ALLOWUNDO = 0x0040
FOF_NOCONFIRMATION = 0x0010
FOF_SILENT = 0x0004


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


# ---- 记录持久化 ----
def load_records():
    try:
        if TRAIL_FILE.exists():
            data = json.loads(TRAIL_FILE.read_text(encoding="utf-8"))
            if isinstance(data, list):
                return data
    except Exception:
        pass
    return []


def save_records(records):
    try:
        TRAIL_FILE.write_text(
            json.dumps(records, ensure_ascii=False, indent=2), encoding="utf-8")
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


def mark_deleted(rec_id, recycled, failed):
    """解压后删除源文件：recycled=已移入回收站(可还原)，failed=永久删除(不可还原)"""
    fields = {
        "status": "deleted",
        "deleted_paths": [str(p) for p in (recycled or [])],
        "failed_paths": [str(p) for p in (failed or [])],
        "deleted_at": time.strftime("%Y-%m-%d %H:%M:%S"),
    }
    if failed:
        fields["note"] = "部分文件未能移入回收站，已永久删除，无法还原"
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
