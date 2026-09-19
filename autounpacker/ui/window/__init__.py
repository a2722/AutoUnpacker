# -*- coding: utf-8 -*-
"""ui.window 包（Stage 6f）：ui/main_window.py 模块级常量与助手的拆分目的地。

子模块：
- consts     共享常量（MainWindow 与各助手共读；中性模块避免导入环）
- logview    日志视图渲染/降级恢复/落盘助手
- share_flow 分享流程助手（提取码取值、缺码小窗、手势去重、线程回执）
- chrome     窗口尺寸与滚动容器助手

注意：本 __init__ 不导入任何子模块（零副作用、不引入导入边）。
"""
