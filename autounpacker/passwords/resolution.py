# -*- coding: utf-8 -*-
"""密码候选与密码字典数据层（无 Qt）：按层生成候选口令 + 薄封装 db 字典读写。

职责：- get_password_for_layer()：按尝试顺序生成某层候选密码列表（上一层成功密码 /
  按层序号映射 / 文件名提取密码 / 默认密码 / 字典密码，全部去重）
- load_password_dict() / save_password_dict() / get_dict_passwords() / add_dict_password()：
  薄封装 db.py 的密码字典 API（任何异常都不抛，读失败返回空值）
关键入口：get_password_for_layer() / get_dict_passwords()
依赖：db
注意：本模块属核心层，禁止顶层导入 PyQt5 / autounpacker.ui（守卫见 test_layer_guard.py）；
      这 5 个旧名在 autounpacker.extract 顶层 re-export 保留，供既有调用点与
      ex.get_dict_passwords 打桩语义使用（不再用函数内裸导入，见下）。
"""
from .. import db


def get_password_for_layer(layer, user_passwords, extracted=None, default=None, dict_passwords=(), prev_used=None):
    """生成某层的候选密码列表（按尝试顺序）。

    - 嵌套层优先用上一层成功密码 prev_used（内外层常共用同一密码）；
    - 再按层序号映射 user_passwords[idx]（旧行为，兼容每层不同密码）；
    - 最后补文件名提取密码 / 默认密码 / 字典密码，全部去重。
    """
    passwords = []
    seen = set()
    if prev_used and layer > 1:
        passwords.append(prev_used)
        seen.add(prev_used)
    idx = max(0, layer - 1)
    if idx < len(user_passwords):
        p = user_passwords[idx]
        if p not in seen:
            passwords.append(p)
            seen.add(p)
    for i, p in enumerate(user_passwords):
        if i != idx and p not in seen:
            passwords.append(p)
            seen.add(p)
    if extracted and extracted not in seen:
        passwords.append(extracted)
        seen.add(extracted)
    if default and default not in seen:
        passwords.append(default)
        seen.add(default)
    for p in dict_passwords:
        if p not in seen:
            passwords.append(p)
            seen.add(p)
    if not passwords:
        passwords.append("")
    return passwords


def load_password_dict():
    try:
        return db.load_password_dict()
    except Exception:
        return {}


def save_password_dict(data):
    try:
        db.save_password_dict(data)
    except Exception:
        pass


def get_dict_passwords():
    try:
        return db.get_dict_passwords()
    except Exception:
        return []


def add_dict_password(password):
    try:
        db.add_dict_password(password)
    except Exception:
        pass
