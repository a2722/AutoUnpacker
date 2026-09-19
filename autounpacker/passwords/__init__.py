# -*- coding: utf-8 -*-
"""autounpacker.passwords——密码本纯逻辑包入口。

本包存放不依赖 Qt 的密码本数据层：行级 helper 与行文本解析 / 格式化。
此文件仅作包标记与模块说明：不导入任何子模块或第三方库，
保证 `import autounpacker.passwords` 轻量且无副作用。
分层约束：本包属核心层，禁止顶层依赖 UI（PyQt5 / autounpacker.ui，
守卫见离线测试 test_layer_guard.py）；密码本对话框在 autounpacker.ui.password_book。
"""
