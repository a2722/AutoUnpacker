# -*- coding: utf-8 -*-
"""向后兼容别名：`autounpacker.baidu_watch` 已迁至 `autounpacker.baidu.watch`。

本文件只是旧路径的 shim：`sys.modules[__name__]` 直接指向新模块对象，
因此旧路径与新路径是同一个模块；通过旧路径的属性赋值（monkeypatch）会
直接作用在新模块上。
"""
import sys

from .baidu import watch as _impl

sys.modules[__name__] = _impl
