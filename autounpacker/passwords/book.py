# -*- coding: utf-8 -*-
"""密码本数据层（无 Qt）：长期密码的行级 helper + 行文本解析。

职责：- 行级 helper（list_password_rows / add_password_row / update_password_row /
  delete_password_row）：薄封装 db.py 的行级 API，供密码本页按行读写（含备注）
- 密码字典 helper（delete_dict_password / clear_password_dict）：薄封装 db.py
- 行文本助手（parse_password_text）：编辑框文本 <-> 行数据的解析
关键入口：list_password_rows() / parse_password_text()
依赖：db
注意：本模块属核心层，禁止顶层导入 PyQt5 / autounpacker.ui（守卫见 test_layer_guard.py）；
      密码本对话框已移至 autounpacker.ui.password_book
"""
from .. import db

# 密码本读取失败只提示一次，避免单纯翻页/刷新就刷屏；用户据此能区分
# 「密码本没读到」与「密码本真的是空的」。
_ROWS_READ_WARNED = False


# ---------- 长期密码本行级 helper（薄封装 db 行级 API，供密码本页使用） ----------
def list_password_rows():
    """行级读取长期密码本：[{"id","password","source","created_at","note"}, ...]。

    与 db.list_passwords() 同源同序（id 升序 = 解压尝试顺序）；任何异常返回 []。
    """
    global _ROWS_READ_WARNED
    try:
        return db.list_passwords()
    except Exception as e:
        if not _ROWS_READ_WARNED:
            _ROWS_READ_WARNED = True
            print(f"[密码本] 读取长期密码失败，本次运行不再重复提示：{e}")
        return []


def add_password_row(password, source="manual", note=""):
    """行级新增一条长期口令（重复口令被忽略），返回行 id；失败返回 0。"""
    try:
        return db.add_password(password, source=source, note=note)
    except Exception:
        return 0


def update_password_row(pid, password=None, note=None, source=None):
    """行级更新长期口令（只写显式提供的字段），返回是否命中该行；失败返回 False。"""
    try:
        return db.update_password(pid, password=password, note=note, source=source)
    except Exception:
        return False


def delete_password_row(pid):
    """行级按 id 精确删除一条长期口令，返回是否命中；失败返回 False。"""
    try:
        return db.delete_password(pid)
    except Exception:
        return False


# ---------- 密码字典 helper（薄封装 db，供密码本页删除 / 清空字典条目） ----------
def delete_dict_password(password):
    """从密码字典按口令精确删除一条记录（不影响密码本），返回是否命中；失败返回 False。"""
    try:
        return db.delete_dict_password(password)
    except Exception:
        return False


def clear_password_dict():
    """清空密码字典（命中统计随之归零），返回删除条数；失败返回 -1。"""
    try:
        return db.clear_password_dict()
    except Exception:
        return -1


def parse_password_text(text):
    """按行解析密码文本（换行分隔），去掉空行并去重，保持顺序"""
    result = []
    seen = set()
    for line in (text or "").splitlines():
        p = line.strip()
        if p and p not in seen:
            seen.add(p)
            result.append(p)
    return result
