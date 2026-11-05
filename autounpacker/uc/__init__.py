# -*- coding: utf-8 -*-
"""UC 网盘（fast.uc.cn）网页端下载通路 —— 实验性，与百度链路完全独立。

设计约束（务必遵守）：
- **本包 `__init__` 零导入**：绝不在这里 import PyQt5 / QtWebEngine / 本包其它模块。
  调用方按需 `from .uc import detect` 之类显式导入，保证「没装可选组件也不影响主程序」。
- 与 `autounpacker/baidu/` 零耦合：不共用 cookie、不共用开关、不共用任务队列。
- 浏览器这一层是**软依赖**：`capability.web_engine_available()` 为 False 时，
  调用方必须优雅降级（设置页置灰 + 提示安装），绝不抛异常。

模块划分：
- `detect`      —— 纯标准库：识别/解析 UC 分享链接（无 Qt，可在任意线程用）
- `capability`  —— 纯标准库：探测可选组件 PyQtWebEngine 是否可用（只 find_spec，不导入）
- `login`       —— 纯标准库：浏览器里执行的 JS 片段（登录态探测 + 手机号登录步骤）
- `handoff`     —— 纯标准库：把下载直链交给 IDM / 默认浏览器 / 剪贴板

内嵌浏览器窗口在 `autounpacker/ui/uc_window.py`（UI 层），因为分层守卫要求
非 ui 模块不得顶层导入 PyQt5；本包保持零 Qt 依赖。
"""
