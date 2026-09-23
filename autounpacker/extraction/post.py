# -*- coding: utf-8 -*-
"""后处理与回收站委托（阶段6c 自 extract.py 纯搬移，函数体未做任何拆分）。

职责：输出时间戳校准（_set_created_time / stamp_output_now / _stamp_output_times）、
--move-to 与提升内容（promote_extracted_content）、删除源（delete_source）、
中间产物 staging（RAR 分卷规范化 / 假分卷改名 / 内嵌 ZIP 剥离）、空目录清理、
_recycle_paths 薄委托 deletion.engine（失败路径 permanent_fallback=False 绝不永久删除）。
依赖：..config（删除策略回退）、..deletion.engine（回收站实现）、formats（分卷判定）、
      engines（CREATE_NO_WINDOW）。
"""
import ctypes
import os
import re
import shutil
import struct
import subprocess
import time
import uuid
import zipfile
from pathlib import Path

from ..config import delete_policy_permanent_fallback
from ..deletion import engine as deletion_engine
from .engines import CREATE_NO_WINDOW
from .formats import PART_RE, _part_info, is_volume_file, is_volume_name


def strip_embedded_zip(path, dest_dir):
    """把多段伪装文件内嵌的 ZIP 部分剥离成独立 zip 文件（7-Zip 对超大/越界偏移的
    内嵌 ZIP64 打不开，剥离后可正常处理，含 AES 加密）。失败返回 None。"""
    try:
        path = Path(path)
        with zipfile.ZipFile(path) as zf:
            entries = [i for i in zf.infolist() if not i.is_dir()]
            if not entries:
                return None
            start = min(i.header_offset for i in entries)
        with open(path, "rb") as f:
            f.seek(0, 2)
            size = f.tell()
            if size - start <= 0:
                return None
            f.seek(max(0, size - 65557))
            tail = f.read()
        eocd = tail.rfind(b"PK\x05\x06")
        if eocd < 0:
            return None
        clen = struct.unpack_from("<H", tail, eocd + 20)[0]
        end = (size - (len(tail) - eocd)) + 22 + clen
        if end <= start:
            return None
        dest_dir = Path(dest_dir)
        dest_dir.mkdir(parents=True, exist_ok=True)
        dest = dest_dir / f"poly_{path.stem[:20]}_{uuid.uuid4().hex[:6]}.zip"
        with open(path, "rb") as src, open(dest, "wb") as out:
            src.seek(start)
            remaining = end - start
            while remaining > 0:
                chunk = src.read(min(8 * 1024 * 1024, remaining))
                if not chunk:
                    break
                out.write(chunk)
                remaining -= len(chunk)
        if remaining > 0:
            # 重新组装中断的临时 zip 是失败中间产物：移入回收站，绝不永久删除
            _recycle_paths([dest], permanent_fallback=False)
            return None
        return dest
    except Exception:
        return None


def _stage_rar_volumes(source):
    """规范化 RAR 分卷命名：分卷组里只要有一个卷后缀非标准（如 .part1.除rar
    或 part3.删除rar 而其余是 .rar），7-Zip 就会按首卷命名去找兄弟卷而报
    Missing volume（例如找 MBM717.part3.rar 但实际是 .part3.删除rar）。

    把同基础名的所有分卷硬链接到源目录下的临时子目录，统一命名为
    .partN.rar 供 7-Zip 识别。返回 (规范后的首卷路径, 临时目录) 或 None。
    """
    source = Path(source)
    pinfo = _part_info(source.name)
    if not pinfo:
        return None
    base, num = pinfo
    if num != 1:
        return None  # 只从首卷进入
    vols = []
    try:
        for entry in source.parent.iterdir():
            if entry.is_file():
                pi = _part_info(entry.name)
                if pi and pi[0] == base:
                    vols.append((pi[1], entry))
    except OSError:
        return None
    if not vols:
        return None
    # 全套卷都是标准 .partN.rar 命名时无需处理（7-Zip 可直接识别为同套分卷）；
    # 任一卷非标准（如 .part1.除rar、part3.删除rar、feal.part01(2).rar 这类
    # 带括号/多余标记的命名）就整体规范化——否则 7-Zip 会按字面名去找
    # "feal.part01(3).rar" 这类不存在的兄弟卷而报 Missing volume。
    def _standard_name(n):
        return bool(PART_RE.match(n))
    if all(_standard_name(v.name) for _, v in vols):
        return None
    # 临时文件名用「去掉批次标记」的基础名（feal(1) → feal），交给 7-Zip
    # 的仍是干净的 .partN.rar 命名；集合标识 base 含标记仅用于分组。
    plain_base = re.sub(r"\.part\d+(?:\([^)]*\))?\.[^.]+$", "", source.name, flags=re.I)
    stage = source.parent / f".stage_{uuid.uuid4().hex[:6]}"
    try:
        stage.mkdir(exist_ok=True)
        for pn, entry in vols:
            dest = stage / f"{plain_base}.part{pn}.rar"
            try:
                os.link(str(entry), str(dest))
            except OSError:
                shutil.copy2(str(entry), str(dest))
        master = stage / f"{plain_base}.part1.rar"
        if not master.exists():
            raise OSError("staging master missing")
        return master, stage
    except Exception:
        # staging 构造失败：半成品临时目录移入回收站（绝不永久删除）
        _recycle_paths([stage], permanent_fallback=False)
        return None


def _stage_fake_volume(source, fmt):
    """假分卷名的完整压缩包：把文件硬链接/复制到临时子目录并改名为标准后缀。

    打包方把完整 zip 改名为 .z11/.111/.partN.rar 等分卷样式的后缀迷惑，
    7-Zip 会因后缀误判为 split 分卷而报 Missing volume。在临时目录把
    文件改名为对应格式的标准后缀（.zip/.rar/.7z）再交给 7-Zip 即可正常解压。
    返回 (规范后的路径, 临时目录) 或 None。"""
    source = Path(source)
    if not source.is_file():
        return None
    ext_map = {"zip": ".zip", "rar": ".rar", "7z": ".7z",
               "gz": ".gz", "bz2": ".bz2", "xz": ".xz", "tar": ".tar"}
    ext = ext_map.get((fmt or "").lower())
    if not ext:
        return None
    stage = source.parent / f".stage_{uuid.uuid4().hex[:6]}"
    try:
        stage.mkdir(exist_ok=True)
        dest = stage / (source.stem + ext)
        try:
            os.link(str(source), str(dest))  # 同卷硬链接，瞬时完成不占空间
        except OSError:
            shutil.copy2(str(source), str(dest))
        if not dest.exists():
            raise OSError("staging fake volume missing")
        return dest, stage
    except Exception:
        # staging 构造失败：半成品临时目录移入回收站（绝不永久删除）
        _recycle_paths([stage], permanent_fallback=False)
        return None


def unique_dest_path(dest):
    """返回不覆盖已有文件的落点路径：已存在时按 `name (1).ext` 递增找空位。

    与提升内容（promote_extracted_content）用的 name(N) 约定一致（文件带空格
    的形式，避免与目录提升的 name(N) 混淆）。已存在同名文件时绝不 shutil.move
    覆盖——用户放在输出目录里的同名文件必须原样保留。"""
    dest = Path(dest)
    if not dest.exists():
        return dest
    parent, stem, suffix = dest.parent, dest.stem, dest.suffix
    i = 1
    while True:
        cand = parent / f"{stem} ({i}){suffix}"
        if not cand.exists():
            return cand
        i += 1


def is_clean_success(result):
    """结果是否为「干净的整体成功」：success 且无失败层/不完整/分卷缺卷标记，
    且（仅当结果显式携带产出清单时）产出非空。

    后处理（提升内容、删除源文件）与「完成」报告的唯一闸门：任何一层失败
    （引擎失败、打不开、错误跳过、CRC/大小不符）或分卷未到齐都不算成功。
    另加零产出防线：结果显式带 `extracted_files` 键且为空 = 本次什么都没产出，
    同样不算成功——否则共享输出目录里预存的旧文件会掩盖零产出，让「成功」
    闸门放行并误删源文件。无 `extracted_files` 键的裸结果（如手写
    {"success": True}）不据此判定，仍按原语义返回 True。"""
    if not result or not result.get("success"):
        return False
    if (result.get("incomplete")
            or result.get("failed_layers")
            or result.get("split_incomplete")):
        return False
    if "extracted_files" in result and not result.get("extracted_files"):
        return False
    return True


def build_post_actions(args):
    actions = []
    if args.move_to:
        actions.append({"action_type": "move_to_dir", "target_dir": args.move_to})
    promote_to = getattr(args, "promote_to", None)
    # 删除前意图钩子（pre_hook(targets)，在真正回收/删除之前回调）：随动作下传，
    # 用于先落盘「正在删除…」，避免删除与结果记录之间的崩溃窗口丢失还原明细。
    pre_hook = getattr(args, "pre_hook", None)
    # 源文件删除策略：随动作下传，最终驱动回收站不可用时的回退（keep=保留源文件）。
    delete_policy = getattr(args, "delete_policy", None)
    # quarantine 策略的隔离区根（空串=按源文件所在目录补 _已删除）；其它策略为 None。
    quarantine_root = getattr(args, "quarantine_root", None)
    if promote_to:
        actions.append({"action_type": "promote_content",
                        "promote_to": promote_to,
                        "merge": bool(getattr(args, "promote_merge", False)),
                        "delete_source": bool(getattr(args, "delete_source", False)),
                        "delete_policy": delete_policy,
                        "quarantine_root": quarantine_root,
                        "delete_hook": getattr(args, "delete_hook", None),
                        "pre_hook": pre_hook})
    elif args.delete_source:
        actions.append({"action_type": "delete_source",
                        "delete_policy": delete_policy,
                        "quarantine_root": quarantine_root,
                        "delete_hook": getattr(args, "delete_hook", None),
                        "pre_hook": pre_hook})
    if args.run_script:
        actions.append({"action_type": "run_script",
                        "script_path": args.run_script,
                        "script_args": args.script_args or []})
    return actions


def apply_post_actions(result, source, output_dir, actions):
    if not is_clean_success(result):
        return
    for action in actions:
        try:
            if action["action_type"] == "move_to_dir":
                moved = move_result_dir(output_dir, action["target_dir"])
                result["extracted_files"] = moved
                result["logs"].append(f"已移动 {len(moved)} 个文件到 {action['target_dir']}")
            elif action["action_type"] == "delete_source":
                delete_source(source, action.get("delete_hook"),
                              permanent_fallback=delete_policy_permanent_fallback(
                                  action.get("delete_policy")),
                              quarantine_root=action.get("quarantine_root"),
                              pre_hook=action.get("pre_hook"))
            elif action["action_type"] == "promote_content":
                res = promote_extracted_content(
                    output_dir, action["promote_to"], source,
                    action.get("delete_hook"), merge=action.get("merge", False),
                    delete_src=action.get("delete_source", False),
                    permanent_fallback=delete_policy_permanent_fallback(
                        action.get("delete_policy")),
                    quarantine_root=action.get("quarantine_root"),
                    pre_hook=action.get("pre_hook"))
                result["logs"].append(f"[后处理] {res['note']}")
                if res["promoted"]:
                    result["promoted_dir"] = res["promoted"]
            elif action["action_type"] == "run_script":
                run_script(action["script_path"], action["script_args"],
                           source, output_dir, result)
        except Exception as e:
            result["logs"].append(f"[后处理] 失败: {e}")


def move_result_dir(output_dir, target_dir):
    target = Path(target_dir)
    target.mkdir(parents=True, exist_ok=True)
    moved = []
    for src in output_dir.rglob("*"):
        rel = src.relative_to(output_dir)
        dest = target / rel
        if src.is_dir():
            dest.mkdir(parents=True, exist_ok=True)
        elif src.is_file():
            dest.parent.mkdir(parents=True, exist_ok=True)
            final = unique_dest_path(dest)
            shutil.move(str(src), str(final))
            moved.append(final)
    remove_empty_dirs(output_dir)
    return moved


def remove_empty_dirs(directory):
    directory = Path(directory)
    if not directory.exists():
        return
    for child in directory.iterdir():
        if child.is_dir():
            remove_empty_dirs(child)
    try:
        directory.rmdir()
    except OSError:
        pass


def _set_created_time(path, ts):
    """把 Windows「创建时间」设为 ts；非 Windows 无此概念，直接视为成功。

    优先 pywin32（项目热键/托盘层已依赖，打包内已带）；不可用时退回 ctypes。
    打开目录句柄必须带 FILE_FLAG_BACKUP_SEMANTICS，否则 CreateFile 对目录失败。"""
    if os.name != "nt":
        return True
    ft = int((ts + 11644473600) * 10000000)   # 1601-01-01 起的 100ns 计数
    try:
        import pywintypes
        import win32con
        import win32file
    except ImportError:
        pywintypes = None
    if pywintypes is not None:
        handle = win32file.CreateFileW(
            str(path), win32con.GENERIC_WRITE,
            win32con.FILE_SHARE_READ | win32con.FILE_SHARE_WRITE
            | win32con.FILE_SHARE_DELETE,
            None, win32con.OPEN_EXISTING, win32con.FILE_FLAG_BACKUP_SEMANTICS, None)
        try:
            win32file.SetFileTime(handle, pywintypes.Time(ts), None, None)
        finally:
            win32file.CloseHandle(handle)
        return True
    from ctypes import wintypes
    kernel32 = ctypes.windll.kernel32
    kernel32.CreateFileW.restype = ctypes.c_void_p
    kernel32.CreateFileW.argtypes = [wintypes.LPCWSTR, wintypes.DWORD,
                                     wintypes.DWORD, ctypes.c_void_p,
                                     wintypes.DWORD, wintypes.DWORD,
                                     ctypes.c_void_p]
    kernel32.SetFileTime.argtypes = [ctypes.c_void_p,
                                     ctypes.POINTER(wintypes.FILETIME),
                                     ctypes.POINTER(wintypes.FILETIME),
                                     ctypes.POINTER(wintypes.FILETIME)]
    handle = kernel32.CreateFileW(str(path), 0x40000000, 0x1 | 0x2 | 0x4,
                                  None, 3, 0x02000000, None)
    if not handle or handle == ctypes.c_void_p(-1).value:
        return False
    try:
        created = wintypes.FILETIME(ft & 0xFFFFFFFF, (ft >> 32) & 0xFFFFFFFF)
        return bool(kernel32.SetFileTime(handle, ctypes.byref(created), None, None))
    finally:
        kernel32.CloseHandle(ctypes.c_void_p(handle))


def stamp_output_now(paths):
    """把产物顶层条目的 修改/访问/创建时间 校准为现在。

    7-Zip 解压目录时会还原归档里保存的目录时间戳（含创建时间），穿透/提升后的
    成品文件夹会带着压缩包里的旧日期，在大目录里按日期排序就沉到底部找不到。
    这里只校准传入的条目本身，不递归子文件；单个条目失败只记录问题、绝不抛出，
    不影响解压结果。返回 (已校准的路径列表, 失败描述列表)。"""
    now = time.time()
    stamped, problems = [], []
    for p in paths:
        p = Path(p)
        try:
            os.utime(p, (now, now))
        except OSError as e:
            problems.append(f"{p.name}: {e}")
            continue
        try:
            if not _set_created_time(p, now):
                problems.append(f"{p.name}: 创建时间未设置")
        except Exception as e:
            problems.append(f"{p.name}: {e}")
        stamped.append(p)
    return stamped, problems


def _final_output_targets(result, out_dir, pre_entries, args):
    """本次解压最终产物的顶层条目：提升成功=提升出的文件夹；--move-to 搬走内容时
    =目标目录里本次搬出的顶层条目；否则=输出目录里本次新建的顶层条目（用户
    预先放进输出目录的内容不动）。"""
    promoted = result.get("promoted_dir")
    if promoted:
        p = Path(promoted)
        return [p] if p.exists() else []
    moved_to = getattr(args, "move_to", None)
    if moved_to:
        target = Path(moved_to)
        seen, tops = set(), []
        for f in result.get("extracted_files") or []:
            try:
                rel = Path(f).relative_to(target)
            except (OSError, ValueError):
                continue
            if rel.parts and rel.parts[0] not in seen:
                seen.add(rel.parts[0])
                p = target / rel.parts[0]
                if p.exists():
                    tops.append(p)
        return tops
    try:
        return [p for p in Path(out_dir).iterdir() if p not in pre_entries]
    except OSError:
        return []


def _stamp_output_times(result, out_dir, pre_entries, args):
    """干净成功后按 output_time_now 开关校准最终产物顶层时间（失败只提示不抛错）。"""
    try:
        from .. import config as app_config
        if not bool(app_config.load_config().get("output_time_now", True)):
            return
    except Exception:
        pass
    targets = _final_output_targets(result, out_dir, pre_entries, args)
    if not targets:
        return
    stamped, problems = stamp_output_now(targets)
    if stamped:
        names = [p.name for p in stamped]
        shown = "、".join(names[:3]) + (f" 等 {len(names)} 项" if len(names) > 3 else "")
        msg = f"已将输出时间戳校准到现在（避免在大目录里被旧日期淹没）: {shown}"
        print(msg)
        result["logs"].append(msg)
    if problems:
        warn = f"输出时间戳部分校准失败（不影响解压）: {'; '.join(problems[:2])}"
        print(warn)
        result["logs"].append(warn)


def _recycle_paths(paths, permanent_fallback=True, quarantine_root=None, quarantine_out=None):
    """把存在的路径移入回收站。

    permanent_fallback=True（默认，成功路径沿用）：回收站不可用时回退永久删除。
    permanent_fallback=False（解压失败路径专用）：回收站不可用时**保留原样**，
    绝不永久删除——满足「涉及解压失败的都不能走永久删除」。

    quarantine_root 非 None 时进入隔离模式：回收站不可用时把失败路径移入隔离区
    （trail.move_to_quarantine），移入项追加到 quarantine_out（调用方传入的 list），
    仍未能移走的路径作为 failed 返回且原样留在原位；**绝不永久删除**。空字符串表示
    按每个文件自身所在目录补 _已删除（无 output_dir 的兜底）。

    返回 (recycled, failed)：recycled=已移入回收站的路径；failed=回收站不可用时
    已永久删除的路径（True）或未能回收、保留在原地的路径（False）。不存在的忽略。"""
    return deletion_engine._recycle_paths(
        paths, permanent_fallback=permanent_fallback,
        quarantine_root=quarantine_root, quarantine_out=quarantine_out)


def delete_source(source, hook=None, permanent_fallback=True, quarantine_root=None,
                  pre_hook=None):
    """删除源文件（含分卷）。

    优先移入回收站（可撤销）；回收站不可用时按 permanent_fallback 决策：
    True（默认，auto / permanent 策略）退化为永久删除；False（keep 策略）**保留
    原文件**，绝不永久删除；quarantine_root 非 None（quarantine 策略）时移入隔离区，
    同样绝不永久删除。
    pre_hook(targets) 在真正删除前回调（targets=本次将处理的路径）：删除动作先于
    结果记录落盘，崩溃窗口里「还原明细」会丢；先落盘「正在删除…」意图记录可兜底。
    hook(recycled, failed, quarantine_map) 在删除后回调：recycled=已移入回收站，
    failed=回收站不可用时的残留（是否已永久删除取决于 permanent_fallback），
    quarantine_map=已移入隔离区、可还原的文件地图（无则 None）。
    """
    source = Path(source)
    targets = []
    if source.exists():
        targets.append(source)
    parent = source.parent
    if parent.exists():
        for entry in parent.iterdir():
            if entry.is_file() and is_volume_file(source.name, entry.name, source.stem):
                targets.append(entry)
    if not targets:
        if hook:
            hook([], [], None)
        print("没有需要删除的源文件")
        return

    qmap = []
    if pre_hook:
        pre_hook(targets)
    recycled, failed = _recycle_paths(
        targets, permanent_fallback=permanent_fallback,
        quarantine_root=quarantine_root, quarantine_out=qmap)
    if hook:
        hook(recycled, failed, qmap or None)
    # 按真实去向分句打印，避免在「永久删除 / 保留 / 隔离区」场景下误报「移入回收站」。
    # 隔离区条目已从 failed 中移除（见 engine._recycle_paths），故 total 不重复计数。
    parts = []
    if recycled:
        parts.append(f"移入回收站 {len(recycled)} 个")
    if qmap:
        parts.append(f"移入隔离区 {len(qmap)} 个")
    if failed:
        still = 0
        for p in failed:
            try:
                if Path(p).exists():
                    still += 1
            except OSError:
                pass
        if len(failed) - still:
            parts.append(f"永久删除 {len(failed) - still} 个")
        if still:
            parts.append(f"保留 {still} 个")
    total = len(recycled) + len(failed) + len(qmap)
    print(f"已处理源文件及分卷，共 {total} 个（{'、'.join(parts)}）")


def _dirs_conflict(src_dir, dest):
    """合并前检查两目录是否有冲突：相同相对路径的文件对（哪怕一个），
    或文件/目录同名混排。目录对目录不算冲突（可递归合并）。"""
    def _entries(d):
        out = {}
        for p in Path(d).rglob("*"):
            out[p.relative_to(d).as_posix().lower()] = p.is_dir()
        return out
    s, d_ = _entries(src_dir), _entries(dest)
    for rel, is_dir in s.items():
        if rel in d_:
            if is_dir and d_[rel]:
                continue  # 目录对目录，可递归合并
            return True    # 文件冲突 / 类型冲突
    return False


def _merge_dir(src, dst):
    """把 src 目录的内容并入 dst（目录递归合并，文件移动），src 会被清空。"""
    for entry in src.iterdir():
        target = dst / entry.name
        if entry.is_dir():
            target.mkdir(parents=True, exist_ok=True)
            _merge_dir(entry, target)
        else:
            shutil.move(str(entry), str(target))


def promote_extracted_content(output_dir, promote_to, source, hook=None, merge=False,
                              delete_src=False, permanent_fallback=True,
                              quarantine_root=None, pre_hook=None):
    """解压后处理：输出目录顶层只有 1 个文件夹时，把该文件夹提升到指定地区。

    delete_src=True 时，随后把源文件（含分卷）与输出目录移入回收站并回调 hook 标记
    删除回溯；回收站不可用时按 permanent_fallback 决策（True=永久删除，
    False=保留源文件，见 delete_source）；quarantine_root 非 None（quarantine 策略）
    时移入隔离区，绝不永久删除。delete_src=False（默认，数据安全优先）时
    只做提升，绝不回收源文件/分卷，仅原地清理提升后空掉的输出目录。
    pre_hook(targets) 与 delete_source 同义：真正删除前回调，先落盘删除意图。
    条件不满足（0 个或多于 1 个顶层文件夹）时：delete_src=True 退化为仅删除源文件，
    delete_src=False 则原样保留源文件。
    merge=True 且目标同名文件夹无文件冲突时，直接并入（不建 (N) 文件夹）；
    有同名文件冲突时仍按原逻辑重命名为 name(N)。
    """
    output_dir = Path(output_dir)
    promote_to = Path(promote_to)
    try:
        entries = list(output_dir.iterdir())
    except OSError as e:
        return {"promoted": None, "recycled": [], "hook_called": False,
                "note": f"提升失败: {e}"}
    top_dirs = [p for p in entries if p.is_dir()]
    top_files = [p for p in entries if p.is_file()]

    # 只有顶层「恰好 1 个文件夹、且没有顶层文件」时才适合提升该文件夹；
    # 否则（顶层文件 + 空文件夹并存，如压缩包里有个空目录）提升会搬错内容，
    # 把真实文件随输出目录一起回收。
    if len(top_dirs) != 1 or top_files:
        if not delete_src:
            return {"promoted": None, "recycled": [], "hook_called": False,
                    "note": "顶层文件夹数≠1 或存在顶层文件，未提升，保留源文件"}
        delete_source(source, hook, permanent_fallback=permanent_fallback,
                      quarantine_root=quarantine_root, pre_hook=pre_hook)
        return {"promoted": None, "recycled": [], "hook_called": True,
                "note": "顶层文件夹数≠1 或存在顶层文件，未提升，仅删除源文件"}

    src_dir = top_dirs[0]
    try:
        # 提升目标若与输出目录是同一位置（如 xx.mp4 内层文件夹也叫 xx，
        # output_dir 与 dest 指向同一目录），说明内容已到位，
        # 视为 same_place：不移动、不回收 output_dir（否则连内容一起回收）。
        same_place = (promote_to.resolve() == output_dir.resolve()
                      or (promote_to / src_dir.name).resolve() == output_dir.resolve())
    except OSError:
        same_place = False

    promoted = None
    pre_recycled, pre_failed = [], []
    qmap = []            # 本函数累计的隔离区地图（pre + 主回收两段）
    occupies_source = False
    if not same_place:
        promote_to.mkdir(parents=True, exist_ok=True)
        dest = promote_to / src_dir.name
        # 目标名被「本次即将回收的源文件/其分卷」占用：典型为无扩展名压缩包与其
        # 内层同名文件夹撞名（源文件 D:\下载\2022 与内层文件夹 2022）。此时既不能
        # 合并进去、也不能直接 move 覆盖；先回收占位文件让出位置，提升后才能用回
        # 原名（否则会退化成 2022(1)）。
        if dest.is_file() and source.exists():
            try:
                occupies_source = os.path.samefile(dest, source)
            except OSError:
                occupies_source = False
            if not occupies_source and is_volume_name(source.name):
                occupies_source = is_volume_file(source.name, dest.name, source.stem)
        if occupies_source and delete_src:
            pre_recycled, pre_failed = _recycle_paths(
                [dest], permanent_fallback=permanent_fallback,
                quarantine_root=quarantine_root, quarantine_out=qmap)

        if dest.is_dir() and merge and not _dirs_conflict(src_dir, dest):
            # 目标同名文件夹存在但无文件冲突：直接并入，不建 (N)
            _merge_dir(src_dir, dest)
            try:
                src_dir.rmdir()
            except OSError:
                pass
            promoted = str(dest)
        else:
            if dest.exists():
                # 有同名文件冲突（哪怕一个）或未开启合并：重命名为 name(N)
                i = 1
                while (promote_to / f"{src_dir.name}({i})").exists():
                    i += 1
                dest = promote_to / f"{src_dir.name}({i})"
            try:
                shutil.move(str(src_dir), str(dest))
                promoted = str(dest)
            except OSError as e:
                if hook:
                    hook(pre_recycled, pre_failed, qmap or None)
                return {"promoted": None, "recycled": pre_recycled,
                        "hook_called": bool(pre_recycled or pre_failed),
                        "note": f"提升失败: {e}"}
    else:
        promoted = str(src_dir)

    # delete_source=False：只完成内容提升，绝不回收源文件与分卷；提升后已空掉的
    # 输出目录用原地清理（空目录）而非回收，确保源数据一定留在原位。
    if not delete_src:
        if not same_place:
            remove_empty_dirs(output_dir)
        return {"promoted": promoted, "recycled": list(pre_recycled),
                "hook_called": False,
                "note": f"已提升 {Path(promoted).name}，保留源文件与分卷"}

    # 源文件路径已被提升后的文件夹复用（occupies_source）时，该路径此刻是成品
    # 目录，不能再当源文件回收（否则会把刚提升出来的内容一起删掉）。
    reused = {str(source)} if occupies_source else set()
    targets = []
    if source.exists() and str(source) not in reused:
        targets.append(str(source))
    # 分卷源文件（如 xxx.7z.001）连同其他分卷一起回收，否则只删主卷和
    # 输出目录，分卷兄弟（xxx.7z.002...）会残留（promote 成功时才走到这里）。
    src_parent = source.parent
    if src_parent.exists():
        for entry in src_parent.iterdir():
            if (entry.is_file()
                    and is_volume_file(source.name, entry.name, source.stem)
                    and str(entry) not in targets
                    and str(entry) not in reused):
                targets.append(str(entry))
    if same_place:
        targets.extend(str(f) for f in top_files)
    elif output_dir.exists():
        targets.append(str(output_dir))

    if pre_hook:
        pre_hook(targets)
    recycled2, failed2 = _recycle_paths(
        targets, permanent_fallback=permanent_fallback,
        quarantine_root=quarantine_root, quarantine_out=qmap)
    recycled = pre_recycled + recycled2
    failed = pre_failed + failed2

    if hook:
        hook(recycled, failed, qmap or None)

    note = f"已提升 {Path(promoted).name}，回收源文件与中间文件共 {len(recycled) + len(failed)} 个"
    return {"promoted": promoted, "recycled": recycled, "hook_called": True, "note": note}


def run_script(script_path, args, source, output_dir, result):
    script = Path(script_path)
    if not script.exists():
        raise FileNotFoundError(f"脚本不存在: {script_path}")
    env = dict(os.environ)
    env["EXTRACT_SOURCE_PATH"] = str(source)
    env["EXTRACT_OUTPUT_DIR"] = str(output_dir)
    env["EXTRACT_SUCCESS"] = "true" if result["success"] else "false"
    env["EXTRACT_DEPTH"] = str(result["depth_reached"])
    cmd = [str(script)] + (args or [])
    r = subprocess.run(cmd, capture_output=True, text=True, env=env,
                       creationflags=CREATE_NO_WINDOW,
                       encoding="utf-8", errors="replace")
    if r.returncode != 0:
        raise RuntimeError(f"脚本返回错误: {(r.stderr or '').strip()}")
    result["logs"].append("脚本执行成功")
