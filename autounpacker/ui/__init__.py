"""autounpacker.ui——界面层（PyQt5）包入口。

本包存放主窗口、页面、样式等 GUI 模块。此文件仅作包标记与模块说明：
不导入任何子模块或第三方库，保证 `import autounpacker.ui` 轻量且无副作用。
分层约束：核心层模块禁止顶层依赖本包（守卫见离线测试 test_layer_guard.py）。
"""
