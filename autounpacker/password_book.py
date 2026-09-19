# -*- coding: utf-8 -*-
"""向后兼容 shim：密码本 helper 与 PasswordBookDialog 已迁至各自新家。

- 11 个纯 helper（无 Qt）→ autounpacker.passwords.book
- PasswordBookDialog（Qt）→ autounpacker.ui.password_book
本模块只保留旧导入路径的名称：`from autounpacker.password_book import ...` 依旧可用。
注意：模块顶层不再导入 PyQt5；PasswordBookDialog 由 PEP 562 的 __getattr__ 按需
懒加载，因此 `import autounpacker.password_book` 在导入期不加载 Qt / UI 层。
依赖：passwords.book（顶层）；ui.password_book（仅访问 PasswordBookDialog 属性时）
"""
from .passwords.book import (  # noqa: F401  （兼容旧路径：仅转出名称，新家 passwords/book.py）
    list_password_rows,
    add_password_row,
    update_password_row,
    delete_password_row,
    list_share_code_rows,
    add_share_code_row,
    update_share_code_row,
    delete_share_code_row,
    set_share_code_rows,
    parse_password_text,
    parse_share_code_text,
    format_share_code_text,
)


def __getattr__(name):
    # PEP 562 模块属性兜底：PasswordBookDialog 懒加载自 ui 层，保证
    # `import autounpacker.password_book` 在导入期不碰 Qt（导入期解耦），
    # 而 `from autounpacker.password_book import PasswordBookDialog` 仍照常工作。
    if name == "PasswordBookDialog":
        from .ui.password_book import PasswordBookDialog
        return PasswordBookDialog
    raise AttributeError(name)
