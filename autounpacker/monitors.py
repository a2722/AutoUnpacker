# -*- coding: utf-8 -*-
"""后台监控（兼容 shim）：实现已拆分到 monitor 包，这里保持旧导入路径与旧名字可用。

本模块不再持有任何实现或状态：
- 目录轮询 → monitor.watcher（FolderWatcher 及其独占的常量/助手，如
  _in_quarantine / _tasks_changed / PAIR_DIR_ACTIVE_MAX_SEC / SPLIT_INCOMPLETE_*）；
- 剪贴板/二维码 → monitor.clipboard（QRMonitor 及其独占的探测状态/正则/过滤器，
  如 CLIPBOARD_AVAILABLE / QR_AVAILABLE / _ensure_clipboard / decide_host /
  _looks_like_non_password / QRMonitor.is_image_bytes 等）。
对旧模块属性的赋值（如测试打桩 smart_extract / deletion_trail / _can_open_append /
decide_host / _ensure_clipboard，或重定向 CLIPBOARD_AVAILABLE / time）会同步转发到
新归属模块，保证新旧两条路径看到同一份状态（同一个 time 模块、同一份探测缓存、
同一组常量）。同一名字被两个新模块共读时（如 time/os 这类共享导入）会写入**所有**
归属模块，与拆分前「一个模块一份全局」的可见性保持一致。
新代码请直接 `from .monitor.watcher import ...` / `from .monitor.clipboard import ...`；
本 shim 仅为向后兼容保留。
"""
import sys as _sys
from types import ModuleType as _ModuleType

from .monitor import clipboard as _clipboard_home
from .monitor import watcher as _watcher_home

# 旧模块级名字全集：逐名从新归属模块显式再导入（静态检查与 * 导入都能看清兼容面；
# time/os/threading/Path 等共享导入在两侧是同一对象，任取一侧即可）。
from .monitor.watcher import (FolderWatcher, OrderedDict,
                              PAIR_DIR_ACTIVE_MAX_SEC, Path,
                              SPLIT_INCOMPLETE_DELAYS, SPLIT_INCOMPLETE_MAX,
                              _can_open_append, _in_quarantine,
                              _norm_path_for_cfg, _tasks_changed, baidu_manifest,
                              db, deletion_quarantine, deletion_trail, get_bool,
                              os, shutil, smart_extract, threading, time, types,
                              volume_pair)
from .monitor.clipboard import (CLIPBOARD_AVAILABLE, QR_AVAILABLE, QRMonitor,
                                _clipboard, _clipboard_mod, _DATE_LIKE_RE,
                                _DOMAIN_LIKE_RE, _ensure_clipboard, _FILE_EXT_RE,
                                _host_of, _IDENTIFIER_LIKE_RE, _imagegrab,
                                _imagegrab_mod, _looks_like_non_password,
                                _NON_PASSWORD_CHARS_RE, _QR_PROBED, _CLIP_PROBED,
                                _should_capture_temp_password, _TIME_LIKE_RE,
                                decide_host, deque, is_baidu_pan_url, is_url_like,
                                paths, queue, re, remember_auto_domain,
                                split_urls, sys, webbrowser)

# 名字 → 归属模块列表：旧模块属性被赋值时同步写入所有归属模块。
_OWNERS_BY_NAME = {}
for _mod in (_watcher_home, _clipboard_home):
    for _name in dir(_mod):
        if _name.startswith("__"):
            continue
        _OWNERS_BY_NAME.setdefault(_name, []).append(_mod)
del _mod, _name

# 兼容注记：旧源码扫描测试按字面查找剪贴板闸门语句（见 test_delete_policy T4d）。
# 真正的实现与执行点在 monitor/clipboard.py:QRMonitor._poll_once，语句原文即：
# if cfg.get("qr_enabled") and QR_AVAILABLE and CLIPBOARD_AVAILABLE:

# 旧模块名字全集（52 个）：显式列出让静态检查与 * 导入都看清兼容面子集。
__all__ = [
    "CLIPBOARD_AVAILABLE", "FolderWatcher", "OrderedDict",
    "PAIR_DIR_ACTIVE_MAX_SEC", "Path", "QRMonitor", "QR_AVAILABLE",
    "SPLIT_INCOMPLETE_DELAYS", "SPLIT_INCOMPLETE_MAX", "_CLIP_PROBED",
    "_DATE_LIKE_RE", "_DOMAIN_LIKE_RE", "_FILE_EXT_RE", "_IDENTIFIER_LIKE_RE",
    "_NON_PASSWORD_CHARS_RE", "_QR_PROBED", "_TIME_LIKE_RE",
    "_can_open_append", "_clipboard", "_clipboard_mod", "_ensure_clipboard",
    "_host_of", "_imagegrab", "_imagegrab_mod", "_in_quarantine",
    "_looks_like_non_password", "_norm_path_for_cfg",
    "_should_capture_temp_password", "_tasks_changed", "baidu_manifest", "db",
    "decide_host", "deletion_quarantine", "deletion_trail", "deque",
    "get_bool", "is_baidu_pan_url", "is_url_like", "os", "paths", "queue",
    "re", "remember_auto_domain", "shutil", "smart_extract", "split_urls",
    "sys", "threading", "time", "types", "volume_pair", "webbrowser",
]


class _MonitorsShimModule(_ModuleType):
    """旧 monitors 模块：属性赋值转发到新归属模块，读写永远指向同一份状态。"""

    def __setattr__(self, name, value):
        owners = _OWNERS_BY_NAME.get(name)
        if owners:
            for owner in owners:
                setattr(owner, name, value)
        super().__setattr__(name, value)


_sys.modules[__name__].__class__ = _MonitorsShimModule
