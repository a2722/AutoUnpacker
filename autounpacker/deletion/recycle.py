# -*- coding: utf-8 -*-
"""回收站：把文件移入回收站（可撤销），并支持从回收站一键还原。

职责：- send_to_recycle_bin()：SHFileOperation(FOF_ALLOWUNDO) 批量移入回收站
- volume_has_recycle_bin()：只读探测卷回收站可用性（绝不弹窗 / 删除 / 抛异常）
- restore_record()/_restore_one()/_invoke_restore()：经 Shell.Application 从回收站还原
依赖：标准库（os/time/ctypes）+ win32com（还原，惰性导入）+ records（记录读写）
注意：回收站被清空或永久删除的文件无法还原；还原成功只认「实际落地的文件」，
      原位置被占用时回收站可能落到新名字，如实上报实际落点，绝不谎报成功。
      取消为协作式且只在「记录之间」生效（调用方在两次 restore_record 之间检查）；
      本模块不中断阻塞中的 win32com InvokeVerb，也不打断单文件最长 30 秒的落地轮询。
"""
import os
import time
from pathlib import Path
from ctypes import wintypes
import ctypes

from .records import get_record, mark_restored_exempt, update_record

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


# 还原是异步的：调用后轮询等待落地的节奏（60 × 0.5s ≈ 30 秒上限）。
# 取消是协作式的，只在调用方「两条记录之间」生效；这里不会被打断：
# win32com 的 InvokeVerb 与下面的单文件落地轮询都不感知取消。
RESTORE_POLL_TIMES = 60
RESTORE_POLL_INTERVAL = 0.5


def _identity_of(path):
    """文件身份 (size, mtime)；取不到 / 不是文件时返回 None。"""
    try:
        p = Path(path)
        if not p.is_file():
            return None
        st = p.stat()
        return (int(st.st_size), float(st.st_mtime))
    except Exception:
        return None


def _snapshot_dir(folder):
    """目录内文件清单：{normcase(路径): 身份}，用于识别还原后真正落地的文件。"""
    snap = {}
    try:
        for p in Path(folder).iterdir():
            ident = _identity_of(p)
            if ident is not None:
                snap[os.path.normcase(str(p))] = ident
    except Exception:
        pass
    return snap


def _record_identity(rec):
    """记录里存的源文件身份 (file_size, file_mtime)；缺失 / 非法时返回 None。"""
    try:
        size, mtime = rec.get("file_size"), rec.get("file_mtime")
        if size is None or mtime is None:
            return None
        return (int(size), float(mtime))
    except Exception:
        return None


def _same_identity(ident, other, mtime_tol=2.0):
    """身份比对：大小必须相等，mtime 允许毫秒级极小误差（还原会保留原时间戳）。"""
    if ident is None or other is None:
        return False
    return ident[0] == other[0] and abs(ident[1] - other[1]) <= mtime_tol


def _find_landed(original_path, ident, before):
    """还原后在原始目录里找本次真正落地的文件；找不到返回 ""。

    1. 还原前原位置为空、现在被占用 → 原位置就是落点（A：只看调用后的实际占用，
       绝不把「调用过还原」当成「还原成功」）；
    2. 原位置被占用时回收站可能落到同名变体（如 "name (1).ext"）→ 在目录里找
       「新出现 / 身份变化」且与记录身份一致的文件，认领为实际落点（C：绝不覆盖
       已有文件，也绝不把用户原有的其它文件误报成还原结果）。
    """
    opath = Path(original_path)
    key = os.path.normcase(str(opath))
    try:
        if key not in before and _identity_of(str(opath)) is not None:
            return str(opath)
    except Exception:
        pass
    try:
        entries = list(opath.parent.iterdir())
    except Exception:
        return ""
    for p in entries:
        try:
            pkey = os.path.normcase(str(p))
            now = _identity_of(p)
            if now is None or pkey == key:
                continue
            if pkey in before and before[pkey] == now:
                continue            # 还原前后都在、身份未变：不是本次还原的产物
            if _same_identity(ident, now):
                return str(p)
        except Exception:
            continue
    return ""


def _restore_one(original_path, rec=None):
    """从回收站还原单个文件，返回 (是否成功, 实际落点, 失败原因)。

    - 还原前先记录原位置占用状态与目录清单（A）：绝不把「调用过还原」当成功；
    - 原位置被占用时回收站可能落到同名变体：按记录身份 (file_size/file_mtime)
      在原始目录里找真正落点，如实上报实际路径（C）；
    - 绝不覆盖已有文件、绝不抛异常。
    """
    original_path = str(Path(original_path))
    opath = Path(original_path)
    name = opath.name
    parent = str(opath.parent)
    before = _snapshot_dir(parent)
    ident = _record_identity(rec)
    try:
        import pythoncom
        import win32com.client
        pythoncom.CoInitialize()
        shell = win32com.client.Dispatch("Shell.Application")
        rb = shell.Namespace(10)  # 回收站
        if rb is None:
            return False, "", "无法打开回收站"
        found = False
        invoked = False
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
                if not _invoke_restore(item):
                    continue
                invoked = True
                for _ in range(RESTORE_POLL_TIMES):  # 还原是异步的，最长等 30 秒
                    time.sleep(RESTORE_POLL_INTERVAL)
                    dest = _find_landed(original_path, ident, before)
                    if dest:
                        return True, dest, ""
        if found and not invoked:
            return False, "", "回收站还原调用失败"
        if found and os.path.normcase(str(opath)) in before:
            return False, "", "原位置已被其他文件占用，未能还原"
        if found:
            return False, "", "还原未生效（回收站中的文件可能已被永久删除）"
        return False, "", "回收站中找不到对应文件，或已被永久删除"
    except Exception:
        return False, "", "还原过程出错"


def restore_record(rec_id):
    """还原记录中已删除（在回收站）的初始源文件。

    返回 (成功: bool, 消息: str)
    成功只认「确有文件可归因于本次还原」：原位置空出后确实被占用，或原始目录里
    出现了与记录身份一致的还原文件；原位置被占用而落到新名字时，消息如实写明
    实际落点。绝不覆盖已有文件、绝不把「调用过还原」当成「还原成功」。
    """
    rec = get_record(rec_id)
    if rec is None:
        return False, "记录不存在"
    if rec.get("status") != "deleted":
        return False, "仅「已删除」状态的记录可还原"
    targets = rec.get("deleted_paths") or []
    if not targets:
        return False, "没有可还原的文件"
    restored, failed = [], []   # restored: [(原路径, 实际落点)]；failed: [(原路径, 原因)]
    for t in targets:
        ok, dest, reason = _restore_one(t, rec)
        if ok:
            restored.append((t, dest))
        else:
            failed.append((t, reason))
    if restored:
        status = "restored" if not failed else "deleted"
        renamed = [(t, d) for t, d in restored
                   if os.path.normcase(str(d)) != os.path.normcase(str(t))]
        if failed:
            note = f"还原 {len(restored)}/{len(targets)} 个文件"
        else:
            note = f"已还原 {len(restored)} 个文件"
        if renamed:
            names = "、".join(os.path.basename(str(d)) for _, d in renamed)
            note += f"；原位置已被占用，{len(renamed)} 个已另存为：{names}"
        elif not failed:
            note += "到原位置"
        if failed:
            note += "；失败: " + ", ".join(os.path.basename(str(t)) for t, _ in failed)
        update_record(rec_id, status=status, note=note)
        # 还原成功 ⇒ 给「还原回来的文件」登记豁免（路径+身份）：用户还原的意图就是
        # 完整保留这份源文件，不该在监听目录里被再次解压、再按 delete_policy 删掉。
        # 登记实际落点（原位置被占用时是新名字），不在则内部自动 no-op。
        for _, dest in restored:
            try:
                mark_restored_exempt(dest)
            except Exception:
                pass
        return True, note
    reason = failed[0][1] if failed else "回收站中找不到对应文件，或已被永久删除"
    return False, f"还原失败：{reason}"
