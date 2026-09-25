# -*- coding: utf-8 -*-
"""向后兼容 shim：密码本 helper 已迁移至 autounpacker.passwords.book。

本模块只保留旧导入路径的名称：`from autounpacker.password_book import ...` 依旧可用。
注意：模块顶层不导入 PyQt5（旧路径的密码本对话框已随功能一并删除）。
依赖：passwords.book（顶层）
"""
from .passwords.book import (  # noqa: F401  （兼容旧路径：仅转出名称，新家 passwords/book.py）
    list_password_rows,
    add_password_row,
    update_password_row,
    delete_password_row,
    parse_password_text,
)
