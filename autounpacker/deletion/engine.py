# -*- coding: utf-8 -*-
"""删除引擎：把存在的路径移入回收站，回收站不可用时按策略回退。

职责：- _recycle_paths()：删除的唯一入口（delete_source / promote_extracted_content 等调用）
依赖：标准库（pathlib）+ recycle（回收站）+ quarantine（隔离区）
注意：本模块绝不导入 extract / trail；回退语义（永久删除 / 保留原样 / 移入隔离区）
      由 permanent_fallback 与 quarantine_root 参数决定
"""
from pathlib import Path

from . import quarantine
from . import recycle


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
    targets = [str(p) for p in paths if Path(p).exists()]
    if not targets:
        return [], []
    recycled, failed = [], []
    try:
        ok, failed = recycle.send_to_recycle_bin(targets)
        recycled = [t for t in targets if t not in failed]
        if not ok:
            failed = [t for t in targets if t not in recycled]
    except Exception:
        recycled, failed = [], list(targets)
    if failed and quarantine_root is not None:
        # 隔离模式：能移则移，移不动的原样留在原位；无论成败都不永久删除。
        try:
            moved, still = quarantine.move_to_quarantine(failed, quarantine_root)
            if quarantine_out is not None:
                quarantine_out.extend(moved)
            failed = list(still)
        except Exception:
            pass
        return recycled, failed
    if permanent_fallback:
        for p in failed:
            try:
                Path(p).unlink(missing_ok=True)
            except OSError:
                pass
    return recycled, failed
