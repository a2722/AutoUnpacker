# -*- coding: utf-8 -*-
"""精简模式（方案 E）界面包：一个无边框小窗承载 HOME / CODE / PW 三页。

对外只导出 `CompactWindow`（宿主 `MainWindow._ensure_compact_window` 按名导入：
`from .compact import CompactWindow`）。包内模块：
  - window.py     CompactWindow（顶层无边框窗 + 自绘标题栏 + 历史导航 + 拖放 + 记忆）
  - nav.py        历史栈控制器（浏览器语义）
  - tasklist.py   紧凑任务行视图（只读读现有任务数据）
  - pages_home.py HOME 页（落区 + 任务列表 + 底部三按钮）
  - pages_code.py CODE 页（提取码；字段与 ShareCodeAskDialog 逐条一致）
  - pages_pw.py   PW 页（新增永久口令）

约定：本包**不弹任何子窗**（提取码 / 新增口令都在本窗换页完成），
不新建任何存储 / 入队 / 解压管线，一律复用宿主既有入口。
"""
from .window import CompactWindow

__all__ = ["CompactWindow"]
