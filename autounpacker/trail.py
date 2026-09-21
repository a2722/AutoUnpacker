# -*- coding: utf-8 -*-
"""删除回溯（兼容 shim）：实现已拆分到 deletion 包，这里保持旧导入路径与旧名字可用。

本模块不再持有任何实现或状态：
- 记录存储 → deletion.records；回收站 → deletion.recycle；隔离区 → deletion.quarantine。
- 对旧模块属性的赋值（如测试重定向 TRAIL_FILE、打桩 send_to_recycle_bin）会同步转发到
  新归属模块，保证新旧两条路径看到同一份状态（同一个 _lock、同一个 TRAIL_FILE）。
新代码请直接 `from .deletion import ...`；本 shim 仅为向后兼容保留。
"""
import sys as _sys
from types import ModuleType as _ModuleType

from .deletion import quarantine as _quarantine_home
from .deletion import records as _records_home
from .deletion import recycle as _recycle_home
from .deletion.quarantine import (_quarantine_entries, _same_volume,
                                  _unique_quarantine_dest, in_quarantine,
                                  move_to_quarantine, quarantine_purge,
                                  quarantine_restore, quarantine_stats,
                                  quarantine_target_dir)
from .deletion.records import (_boot_time, _lock, _quarantine_note_dir,
                               _record_ts, QUARANTINE_DIRNAME, TRAIL_FILE,
                               add_record, already_handled, get_record,
                               is_restored_exempt, load_records,
                               mark_deleted, mark_failed, mark_kept, mark_restored_exempt,
                               new_record, prune_records,
                               save_records, update_record)
from .deletion.recycle import (DRIVE_FIXED, DRIVE_RAMDISK, DRIVE_REMOTE,
                               DRIVE_REMOVABLE, FOF_ALLOWUNDO,
                               FOF_NOCONFIRMATION, FOF_SILENT, FO_DELETE,
                               SHFILEOPSTRUCTW, _invoke_restore, _restore_one,
                               restore_record, send_to_recycle_bin,
                               volume_has_recycle_bin)
from .paths import DATA_DIR as APP_DIR
from .utils import same_volume

# 旧模块名字全集（再导出即使用；显式列出让静态检查与 * 导入都看清兼容面）。
__all__ = [
    "QUARANTINE_DIRNAME", "TRAIL_FILE", "_lock",
    "load_records", "save_records", "new_record", "_boot_time", "_record_ts",
    "prune_records", "add_record", "already_handled", "update_record",
    "mark_restored_exempt", "is_restored_exempt",
    "get_record", "mark_kept", "mark_failed", "mark_deleted",
    "_quarantine_note_dir",
    "FO_DELETE", "FOF_ALLOWUNDO", "FOF_NOCONFIRMATION", "FOF_SILENT",
    "SHFILEOPSTRUCTW", "send_to_recycle_bin",
    "DRIVE_REMOVABLE", "DRIVE_FIXED", "DRIVE_REMOTE", "DRIVE_RAMDISK",
    "volume_has_recycle_bin", "_invoke_restore", "_restore_one",
    "restore_record",
    "quarantine_target_dir", "_unique_quarantine_dest", "_same_volume",
    "move_to_quarantine", "_quarantine_entries", "quarantine_restore",
    "quarantine_purge", "quarantine_stats", "in_quarantine",
    "APP_DIR", "same_volume",
]

# 名字 → 新归属模块：旧模块属性被赋值时同步写入归属模块（保持打桩/重定向语义）。
_OWNER_BY_NAME = {}
for _mod in (_records_home, _recycle_home, _quarantine_home):
    for _name in dir(_mod):
        if not _name.startswith("__"):
            _OWNER_BY_NAME.setdefault(_name, _mod)
del _mod, _name


class _TrailShimModule(_ModuleType):
    """旧 trail 模块：属性赋值转发到新归属模块，读写永远指向同一份状态。"""

    def __setattr__(self, name, value):
        owner = _OWNER_BY_NAME.get(name)
        if owner is not None and owner is not self:
            setattr(owner, name, value)
        super().__setattr__(name, value)


_sys.modules[__name__].__class__ = _TrailShimModule
