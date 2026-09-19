# -*- coding: utf-8 -*-
"""密码本数据层（无 Qt）：长期密码 / 固定提取码的行级 helper + 行文本解析与格式化。

职责：- 行级 helper（list_password_rows / add_password_row / update_password_row /
  delete_password_row）：薄封装 db.py 的行级 API，供密码本页按行读写（含备注）
- 固定提取码行级 helper（list_share_code_rows / add_share_code_row /
  update_share_code_row / delete_share_code_row / set_share_code_rows）：
  同样薄封装 db.py，供密码本页的固定提取码视图按 share_uk 读写（含备注 / pick）
- 行文本助手（parse_password_text / parse_share_code_text / format_share_code_text）：
  编辑框文本 <-> 行数据的解析 / 格式化
关键入口：list_password_rows() / parse_share_code_text() / format_share_code_text()
依赖：db
注意：本模块属核心层，禁止顶层导入 PyQt5 / autounpacker.ui（守卫见 test_layer_guard.py）；
      密码本对话框已移至 autounpacker.ui.password_book
"""
from .. import db


# ---------- 长期密码本行级 helper（薄封装 db 行级 API，供密码本页使用） ----------
def list_password_rows():
    """行级读取长期密码本：[{"id","password","source","created_at","note"}, ...]。

    与 db.list_passwords() 同源同序（id 升序 = 解压尝试顺序）；任何异常返回 []。
    """
    try:
        return db.list_passwords()
    except Exception:
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


# ---------- 固定提取码行级 helper（薄封装 db 行级 API，供密码本页使用） ----------
def list_share_code_rows():
    """行级读取固定提取码：[{"share_uk","code","note","pick","updated_at"}, ...]；异常返回 []。"""
    try:
        return db.get_share_code_map()
    except Exception:
        return []


def add_share_code_row(share_uk, code, note="", pick=0):
    """行级新增一条固定提取码（同 UK UPSERT 覆盖），返回是否成功；失败返回 False。"""
    try:
        return db.add_share_code(share_uk, code, note, pick)
    except Exception:
        return False


def update_share_code_row(share_uk, new_share_uk=None, code=None, note=None, pick=None):
    """行级更新固定提取码（只写显式提供的字段，可为分享者 UK 改名），返回是否命中；失败返回 False。"""
    try:
        return db.update_share_code(share_uk, new_share_uk=new_share_uk,
                                    code=code, note=note, pick=pick)
    except Exception:
        return False


def delete_share_code_row(share_uk):
    """行级按 share_uk 精确删除一条固定提取码，返回是否命中；失败返回 False。"""
    try:
        return db.delete_share_code(share_uk)
    except Exception:
        return False


def set_share_code_rows(items):
    """整表覆盖固定提取码（批量文本编辑保存），返回是否成功；失败返回 False。"""
    try:
        return db.set_share_code_map(items)
    except Exception:
        return False


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


def parse_share_code_text(text):
    """把「分享者UK 提取码 [pick] [#备注]」多行文本解析为
    [{"share_uk","code","note","pick"}, ...]，pick 为 0/1。

    空行/注释行/格式不合法的行一律跳过；同一 UK 重复出现时以最后一行为准。
    接受格式：以空白（空格/制表符）分隔，「#」之后为备注（可省略）；UK 必须是
    纯数字，「提取码」必须是 1~16 位 ASCII 字母或数字。第三列若为 pick/挑选/1
    （pick 不分大小写）则标记「需要挑选」（pick=1），否则该列并入备注文本。
    任何输入都不抛异常。
    """
    result = []
    index = {}
    for line in str(text or "").splitlines():
        p = line.strip()
        if not p or p.startswith("#"):
            continue
        body, _, note = p.partition("#")
        parts = body.split()
        if len(parts) < 2:
            continue
        uk, code = parts[0], parts[1]
        if not (uk.isascii() and uk.isdigit()):
            continue
        if not (1 <= len(code) <= 16) or not (code.isascii() and code.isalnum()):
            continue
        rest = parts[2:]
        pick = 0
        if rest and rest[0].lower() in ("pick", "挑选", "1"):
            pick = 1
            rest = rest[1:]
        note = note.strip() or " ".join(rest)
        item = {"share_uk": uk, "code": code, "note": note, "pick": pick}
        if uk in index:
            # 同一 UK 重复：最后一行的提取码/备注/pick 生效，位置沿用首次出现（与 db 一致）
            index[uk].update(item)
        else:
            index[uk] = item
            result.append(item)
    return result


def format_share_code_text(items):
    """把固定提取码列表格式化为编辑框文本：每行「分享者UK 提取码 [pick] [#备注]」。

    与 parse_share_code_text 互为往返：pick=1 写成 pick 标记，再解析回来仍为 1；
    pick=0 不写标记。非法条目跳过，任何输入都不抛异常。
    """
    lines = []
    for it in items or []:
        if not isinstance(it, dict):
            continue
        uk = str(it.get("share_uk") or "").strip()
        code = str(it.get("code") or "").strip()
        if not uk or not code:
            continue
        try:
            pick = 1 if int(it.get("pick") or 0) else 0
        except Exception:
            pick = 1 if it.get("pick") else 0
        note = str(it.get("note") or "").strip()
        line = f"{uk} {code} pick" if pick else f"{uk} {code}"
        if note:
            line += f" #{note}"
        lines.append(line)
    return "\n".join(lines)
