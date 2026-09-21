# -*- coding: utf-8 -*-
"""回收站：把文件移入回收站（可撤销），并支持从回收站一键还原。

职责：- send_to_recycle_bin()：SHFileOperation(FOF_ALLOWUNDO) 批量移入回收站
- volume_has_recycle_bin()：只读探测卷回收站可用性（绝不弹窗 / 删除 / 抛异常）
- restore_record()/_restore_one()/_invoke_restore()：经 Shell.Application 从回收站还原
依赖：标准库（os/time/ctypes）+ win32com（还原，惰性导入）+ records（记录读写）
注意：回收站被清空或永久删除的文件无法还原
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
        # 还原成功 ⇒ 给「源文件」登记豁免（路径+身份）：用户还原的意图就是完整保留
        # 这份源文件，不该在监听目录里被再次解压、再按 delete_policy 删掉。
        # 只对真的回到原位的 original_path 生效（不在则内部自动 no-op）。
        try:
            mark_restored_exempt(rec.get("original_path"))
        except Exception:
            pass
        if not failed:
            return True, f"已还原 {len(restored)} 个文件"
        return True, f"还原 {len(restored)}/{len(targets)}，部分失败：{os.path.basename(failed[0])}"
    return False, "还原失败：回收站中找不到对应文件，或已被永久删除"
