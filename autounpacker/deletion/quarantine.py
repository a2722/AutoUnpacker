# -*- coding: utf-8 -*-
"""隔离区：回收站不可用时源文件的可还原落点（<root>/_已删除/<YYYY-MM-DD>）。

职责：- in_quarantine()：路径是否位于隔离区（所有监听/输出根枚举必须先过这一关）
- quarantine_target_dir()/_unique_quarantine_dest()/move_to_quarantine()：不覆盖地移入隔离区
- quarantine_restore()/quarantine_purge()/quarantine_stats()：还原、彻底删除、统计
依赖：标准库（os/time/shutil）+ utils.same_volume + records（记录读写与隔离区常量）
注意：绝不覆盖已有文件、绝不抛异常；隔离文件是可还原副本，绝不能被当成新压缩包重解
"""
import os
import time
import shutil
from pathlib import Path

from ..utils import same_volume
from .records import (QUARANTINE_DIRNAME, get_record, load_records,
                      update_record)


# ---- 隔离区（回收站不可用时源文件的可还原落点） ----
def in_quarantine(path):
    """路径是否位于隔离区（_已删除）之内：任一路径段等于 QUARANTINE_DIRNAME 即命中。

    隔离区里的文件是「已删除源文件的可还原副本」，绝不能再被当成新压缩包扫描/重解，
    否则会被反复处理甚至再次删除。所有枚举监听/输出根的地方都必须先过这一关。
    """
    try:
        return QUARANTINE_DIRNAME in Path(path).parts
    except Exception:
        return False


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
