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


# ---- 卷回收站可用性 / 计数探测（只读；绝不弹窗 / 绝不删除 / 绝不抛异常） ----
DRIVE_REMOVABLE = 2   # 可移动盘（U 盘 / 移动硬盘）
DRIVE_FIXED = 3       # 固定盘
DRIVE_REMOTE = 4      # 网络盘
DRIVE_RAMDISK = 6     # 内存盘

# _probe_volume 的盘型判定结果
_DRIVE_NO_BIN = 0      # 明确没有本机回收站：UNC / 无盘符 / 可移动 / 网络 / 内存盘
_DRIVE_QUERYABLE = 1   # 固定盘：可向 Shell 实测该卷回收站
_DRIVE_UNKNOWN = 2     # 非 Windows / 其它盘型 / 探测异常：不确定

# 卷回收站计数缓存 TTL（秒）：页面一次渲染里同一卷最多查一次 Shell，绝不逐行查。
RECYCLE_STATS_TTL = 45.0
# 卷根(normcase) -> (到期 monotonic 时刻, _query_recycle_bin 结果)
_recycle_stats_cache = {}


class SHQUERYRBINFO(ctypes.Structure):
    """SHQueryRecycleBinW 的输出结构（本模块唯一声明 / 使用此结构的地方）。"""

    _fields_ = [
        ("cbSize", wintypes.DWORD),
        ("i64Size", ctypes.c_longlong),
        ("i64NumItems", ctypes.c_longlong),
    ]


def reset_recycle_bin_stats_cache():
    """清空卷回收站计数缓存（测试钩子）：下次查询重新走 Shell。"""
    _recycle_stats_cache.clear()


def _probe_volume(path):
    """path 所在卷根与盘型：返回 (root, kind)。

    kind ∈ (_DRIVE_NO_BIN, _DRIVE_QUERYABLE, _DRIVE_UNKNOWN)：
    - UNC / 无盘符 / 可移动 / 网络 / 内存盘 → _DRIVE_NO_BIN（明确没有回收站）；
    - 固定盘 → _DRIVE_QUERYABLE（可实测）；
    - 非 Windows / 其它盘型 / 任何异常 → _DRIVE_UNKNOWN（不确定）。
    只读探测：绝不弹窗、绝不删除、绝不抛异常。
    """
    try:
        if os.name != "nt":
            return None, _DRIVE_UNKNOWN
        p = str(path or "")
        if p.startswith("\\\\") or p.startswith("//"):
            return None, _DRIVE_NO_BIN
        drive = os.path.splitdrive(os.path.abspath(p))[0]
        if not drive:
            return None, _DRIVE_NO_BIN
        root = drive + "\\"
        dtype = int(ctypes.windll.kernel32.GetDriveTypeW(root))
        if dtype in (DRIVE_REMOVABLE, DRIVE_REMOTE, DRIVE_RAMDISK):
            return root, _DRIVE_NO_BIN
        if dtype != DRIVE_FIXED:
            return root, _DRIVE_UNKNOWN
        return root, _DRIVE_QUERYABLE
    except Exception:
        return None, _DRIVE_UNKNOWN


def _query_recycle_bin(root):
    """向 Shell 实测卷根回收站：返回 (ok, num_items, size_bytes)。

    ok=True 调用成功（计数有效）/ False 明确失败 / None 过程出错（不确定）。
    本模块唯一的 SHQueryRecycleBinW 调用点；只读、绝不弹窗、绝不抛异常。
    """
    try:
        info = SHQUERYRBINFO()
        info.cbSize = ctypes.sizeof(SHQUERYRBINFO)
        ret = ctypes.windll.shell32.SHQueryRecycleBinW(root, ctypes.byref(info))
    except Exception:
        return None, 0, 0
    if ret == 0:
        return True, int(info.i64NumItems), int(info.i64Size)
    return False, 0, 0


def _cached_recycle_query(root):
    """按卷根缓存的 _query_recycle_bin（TTL=RECYCLE_STATS_TTL）。

    一次页面渲染里同一卷最多触发一次 Shell 查询；缓存同时覆盖「明确失败 / 出错」，
    失败也不会在 TTL 内反复重查。测试可用 reset_recycle_bin_stats_cache() 清空。
    """
    key = os.path.normcase(str(root))
    now = time.monotonic()
    hit = _recycle_stats_cache.get(key)
    if hit is not None and hit[0] > now:
        return hit[1]
    result = _query_recycle_bin(root)
    _recycle_stats_cache[key] = (now + RECYCLE_STATS_TTL, result)
    return result


def recycle_bin_stats(path):
    """path 所在固定卷的回收站当前计数 (num_items, size_bytes)；不可判定时 None。

    None = 非 Windows / UNC / 非固定盘 / 查询失败或异常 —— 调用方据此**绝不**对
    当前位置做任何断言。结果按卷根缓存 RECYCLE_STATS_TTL 秒：一次页面渲染里同一卷
    最多一次 Shell 查询（绝不逐行查询）。只读、绝不弹窗、绝不抛异常。
    """
    root, kind = _probe_volume(path)
    if root is None or kind != _DRIVE_QUERYABLE:
        return None
    ok, num_items, size_bytes = _cached_recycle_query(root)
    if not ok:
        return None
    return (num_items, size_bytes)


def volume_has_recycle_bin(path):
    """探测 path 所在卷是否有可用的回收站，返回 True / False / None（不确定）。

    判定规则：
    - UNC 路径（\\\\server\\share…）没有「本机回收站」概念 → False；
    - 可移动盘 / 网络盘 / 内存盘 → False（这类卷默认不带回收站）；
    - 固定盘 → 用 SHQueryRecycleBinW 实测该卷根的回收站是否可用：
      调用成功(0) → True，明确失败 → False，过程出错 → None（不确定）；
    - 其它盘型 / 非 Windows / 探测过程任何异常 → None（不确定，调用方保守处理）。

    只做只读探测：绝不弹窗、绝不删除、绝不抛异常。查询复用 recycle_bin_stats
    的同一缓存：同一卷在 TTL 内只实测一次。
    """
    root, kind = _probe_volume(path)
    if kind == _DRIVE_NO_BIN:
        return False
    if root is None or kind != _DRIVE_QUERYABLE:
        return None
    if recycle_bin_stats(path) is not None:
        return True
    ok, _num_items, _size_bytes = _cached_recycle_query(root)
    return False if ok is False else None


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
    """目录内条目清单：{normcase(路径): 身份}，用于识别还原后真正落地的文件。

    目录条目以 None 占位（目录没有 (size, mtime) 身份）：这样「原位置本轮由空变有」
    的判定能认出「这里原本就有一个同名目录」而不误报成功；下方只认文件的落点扫描
    则因身份为 None 自然跳过目录。"""
    snap = {}
    try:
        for p in Path(folder).iterdir():
            try:
                snap[os.path.normcase(str(p))] = _identity_of(p)   # 目录 -> None
            except Exception:
                continue
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
        if key not in before and Path(original_path).exists():
            # 原位置还原前不存在、还原后存在：文件或目录都算本次还原的落点。
            # 记录里 deleted_paths 可能含「提升后已空的输出目录」这类中间目录，
            # 目录没有 (size, mtime) 身份，若仍要求 is_file，目录还原成功也归属不上，
            # 会白轮询满 30 秒再谎报「还原未生效」（真机：还原很慢 + 读条不停）。
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


def _recycle_items():
    """打开回收站并返回条目快照 list；打不开返回 None。

    整条记录的所有还原目标共用同一份快照：回收站枚举本身要走 Shell、很慢，逐目标
    重新枚举会让「还原 N 个文件」的耗时线性膨胀（真机单条记录 2 个目标就要枚举两遍）。
    """
    try:
        import pythoncom
        import win32com.client
        pythoncom.CoInitialize()
        shell = win32com.client.Dispatch("Shell.Application")
        rb = shell.Namespace(10)          # 回收站
        if rb is None:
            return None
        return list(rb.Items())
    except Exception:
        return None


def _restore_one(original_path, rec=None, items=None):
    """从回收站还原单个文件（或目录），返回 (是否成功, 实际落点, 失败原因)。

    - 还原前先记录原位置占用状态与目录清单（A）：绝不把「调用过还原」当成功；
    - 原位置被占用时回收站可能落到同名变体：按记录身份 (file_size/file_mtime)
      在原始目录里找真正落点，如实上报实际路径（C）；
    - items=回收站条目快照（restore_record 一次枚举、多目标共用）；None 时自行枚举，
      打不开回收站返回「无法打开回收站」；
    - **名称先筛**：只有名字命中的条目才去读 System.Recycle.DeletedFrom —— Shell 的
      ExtendedProperty 每次调用都很慢，对回收站每个条目都读一遍正是「还原很慢」主因；
    - 目录也能还原，落点判定见 _find_landed（原位置本轮由空变有即算成功）；
    - 绝不覆盖已有文件、绝不抛异常。
    """
    original_path = str(Path(original_path))
    opath = Path(original_path)
    name = opath.name
    parent = str(opath.parent)
    before = _snapshot_dir(parent)
    ident = _record_identity(rec)
    try:
        if items is None:
            items = _recycle_items()
            if items is None:
                return False, "", "无法打开回收站"
        want = os.path.normcase(name)
        found = False
        invoked = False
        for item in items:
            try:
                it_name = str(item.Name or "")
            except Exception:
                continue
            if os.path.normcase(it_name) != want:
                continue        # 名字不对：跳过昂贵的 DeletedFrom 读取
            try:
                it_parent = str(item.ExtendedProperty("System.Recycle.DeletedFrom") or "")
            except Exception:
                continue
            # 回收站的 DeletedFrom 只给原始目录，因此用「文件名 + 原始目录」匹配
            # Windows 路径大小写不敏感，统一 normcase 后比较
            if os.path.normcase(it_parent) == os.path.normcase(parent):
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


def _intermediate_dirs(rec, targets, items):
    """在 targets 里挑出「程序自建的中间目录」——**不还原**它们（返回 set）。

    背景（真机）：2.2.9 之前产生的历史记录会把「提升后已空的输出目录」也记进
    `deleted_paths`（形如 `<源目录>/<源文件去扩展名>`）。那个路径是**目录**（源文件
    都是文件），还原它只会让用户凭空多出一个空文件夹；而且它本就不该出现在记录里。

    判定用回收站条目的 `IsFolder`（精确、不看名字）：**除记录原路径之外的目录**一律
    视为解压中间产物、跳过。判不出（打不开回收站 / 替身没有该属性）时返回**空集**——
    退化为旧行为，绝不误伤。
    """
    out = set()
    try:
        orig = os.path.normcase(str((rec or {}).get("original_path") or ""))
        for t in (targets or []):
            if os.path.normcase(str(t)) == orig:
                continue                 # 记录的原源文件本身：永远要还原
            name = os.path.basename(str(t))
            parent = os.path.dirname(str(t))
            for item in (items or []):
                try:
                    if (os.path.normcase(str(item.Name or ""))
                            != os.path.normcase(name)):
                        continue
                    it_parent = str(
                        item.ExtendedProperty("System.Recycle.DeletedFrom") or "")
                    if os.path.normcase(it_parent) != os.path.normcase(parent):
                        continue
                    if bool(item.IsFolder):
                        out.add(t)
                    break
                except Exception:
                    continue
    except Exception:
        return set()
    return out


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
    # 回收站只枚举一次，本记录所有目标共用（枚举很慢，逐目标重来会线性变慢）。
    items = _recycle_items()
    # 老记录里可能混着「程序自建的中间目录」（提升后已空的输出目录）：还原它们只会
    # 让用户凭空多一个空文件夹。这里先把它们挑出来跳过，且**不计入失败**（不是失败）。
    _skip = _intermediate_dirs(rec, targets, items)
    for t in targets:
        if t in _skip:
            continue
        if items is None:
            ok, dest, reason = False, "", "无法打开回收站"
        else:
            ok, dest, reason = _restore_one(t, rec, items)
        if ok:
            restored.append((t, dest))
        else:
            failed.append((t, reason))
    if _skip and not restored and not failed:
        return False, "记录里只有解压中间目录（程序自建），没有可还原的源文件"
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
