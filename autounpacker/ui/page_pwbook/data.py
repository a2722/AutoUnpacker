# -*- coding: utf-8 -*-
"""密码本页数据层（阶段6e 自 ui/page_pwbook.py 纯搬移）：口令行级门面 _PwData、
固定提取码行级门面 _ShareData，以及两侧共用的纯函数助手与来源常量。

无 Qt 依赖；行级读写仍只经 state 代理 / password_book 薄封装 / db，
绝不直接读写 SQLite 文件（测试可对这些入口打桩）。
"""
import time

from ... import db
from ...passwords.book import (add_password_row, add_share_code_row,
                               clear_password_dict, delete_dict_password,
                               delete_password_row, delete_share_code_row,
                               list_password_rows, list_share_code_rows,
                               set_share_code_rows, update_password_row,
                               update_share_code_row)


# 来源文案：显示库中真实存储的 source（manual→手动 / clipboard→剪贴板，未知原样显示）；
# 临时密码不在库里，固定标为「剪贴板」。
_SOURCE_BOOK = "手动"
_SOURCE_TEMP = "剪贴板"
_SOURCE_LABELS = {"manual": "手动", "clipboard": "剪贴板"}


# ---------------------------------------------------------------------------
# 纯函数助手（无 Qt 依赖；行过滤口径与日志页一致，离线可测）
# ---------------------------------------------------------------------------

def _hit_count(row):
    """命中的解压次数（非法一律 0）。"""
    try:
        return max(0, int((row or {}).get("hit_count") or 0))
    except Exception:
        return 0


def _fmt_hit_time(ts):
    """最近命中时间戳 -> 「YYYY-MM-DD HH:MM」；0 / 非法一律「—」。"""
    try:
        v = float(ts or 0)
    except Exception:
        return "—"
    if v <= 0:
        return "—"
    try:
        return time.strftime("%Y-%m-%d %H:%M", time.localtime(v))
    except Exception:
        return "—"


def _source_label(source):
    """把库中存储的 source 原文转成展示文案；空串保持空（页面显示「—」）。"""
    s = str(source or "").strip()
    if not s:
        return ""
    return _SOURCE_LABELS.get(s.lower(), s)


def _row_matches(row, keyword):
    """搜索：口令 / 来源 / 备注 的大小写不敏感子串（与日志页 keyword 同口径）。"""
    kw = str(keyword or "").strip().lower()
    if not kw:
        return True
    if not isinstance(row, dict):
        return False
    for key in ("password", "source", "note"):
        if kw in str(row.get(key) or "").lower():
            return True
    return False


def _passes_filter(row, key):
    """筛选：all / hit（命中过）/ miss（未命中）/ src（有来源）；未知一律 all。"""
    k = str(key or "all")
    if k == "hit":
        return _hit_count(row) > 0
    if k == "miss":
        return _hit_count(row) <= 0
    if k == "src":
        return bool(str((row or {}).get("source") or ""))
    return True


def _make_row(password, source, kind, hits, pid=None, note=""):
    """合成一行展示数据：id（行身份）/ 口令 / 来源 / 命中次数 / 最近命中 / 备注。

    命中统计来自解压字典（hits），备注来自 passwords.note；临时 / 字典行没有 id，pid=None。
    """
    meta = hits.get(password) if isinstance(hits, dict) else None
    try:
        count = max(0, int((meta or {}).get("used_count") or 0))
    except Exception:
        count = 0
    try:
        last = float((meta or {}).get("last_used_at") or 0)
    except Exception:
        last = 0.0
    return {"id": pid, "password": str(password), "source": str(source or ""),
            "hit_count": count, "last_hit": last, "note": str(note or ""),
            "kind": str(kind)}


def _pick_on(value):
    """「需挑选」归一化为 True/False（兼容 bool / 0/1 / "pick"/"挑选" 等写法；绝不抛错）。"""
    try:
        if isinstance(value, str):
            return value.strip().lower() in ("1", "true", "yes", "on", "pick", "挑选")
        return int(value) != 0
    except Exception:
        return False


def _valid_share_uk(text):
    """分享者 UK 是否合法（非空纯数字）——与文本行格式「分享者UK 提取码 …」的解析口径一致。"""
    uk = str(text or "").strip()
    return bool(uk) and uk.isascii() and uk.isdigit()


def _valid_share_code(text):
    """提取码是否合法（1~16 位 ASCII 字母或数字）——与文本行格式的解析口径一致。

    口径一致保证行编辑写入的数据一定能被「批量编辑（文本）」完整往返，不会静默丢行。
    """
    code = str(text or "").strip()
    return 1 <= len(code) <= 16 and code.isascii() and code.isalnum()


# ---------------------------------------------------------------------------
# 数据访问层（私有）：行级 API（长期口令按行 id 增删改）
# ---------------------------------------------------------------------------

class _PwData:
    """密码本页的行级数据层：长期口令按行 id 读 / 增 / 改 / 删（含备注）。

    行级优先顺序：
      1) state 提供行级接口（password_rows/add_password_row/update_password_row/
         delete_password_row，生产 AppState 已提供）时经 state 代理落库；
      2) 无 state 时直连 password_book 的行级 helper（薄封装 db）；
      3) state 只提供旧整表接口（离线测试桩）时退回 passwords/set_passwords 合成行，
         此时行 id 为 None、不支持备注——旧桩行为与改造前保持一致。
    临时密码与命中统计仍只读 state.temp_passwords / db.load_password_dict；
    任何异常都退化为「空 / 失败」，绝不让页面因数据层抖动而崩。
    """

    def __init__(self, state=None):
        self._state = state

    def _row_api(self):
        """是否走行级路径（无 state = 直连 db 行级；有 state 则看它是否提供行级接口）。"""
        return self._state is None or hasattr(self._state, "password_rows")

    def book(self):
        """长期密码本口令字符串列表（保持库内顺序 = 解压尝试顺序）。"""
        try:
            if self._state is not None and hasattr(self._state, "passwords"):
                items = self._state.passwords()
            else:
                items = db.get_passwords()
        except Exception:
            return []
        return [str(p) for p in (items or []) if str(p)]

    def _book_rows(self):
        """长期密码本行：[{id,password,source,created_at,note}, ...]（行级优先）。"""
        try:
            if self._state is not None and hasattr(self._state, "password_rows"):
                return list(self._state.password_rows() or [])
            if self._state is None:
                return list(list_password_rows() or [])
            # 旧整表桩：无 id / 无备注，来源按手动
            return [{"id": None, "password": str(p), "source": "manual",
                     "created_at": 0, "note": ""} for p in self.book()]
        except Exception:
            return []

    def temp(self):
        """本次开机内的临时密码（剪贴板捕获，只读展示）。"""
        try:
            if self._state is not None:
                items = self._state.temp_passwords()
                return [str(p) for p in (items or []) if str(p)]
        except Exception:
            pass
        return []                          # 无 state 时不猜临时密码来源，宁缺毋滥

    def hits(self):
        """解压字典元数据 {口令: {used_count, last_used_at}}（命中统计的唯一来源）。"""
        try:
            data = db.load_password_dict()
        except Exception:
            return {}
        return data if isinstance(data, dict) else {}

    def dict_count(self):
        """密码字典当前收录条数（「清空密码字典」的条数口径）。"""
        return len(self.hits())

    def remove_dict(self, row):
        """从密码字典按口令删除一行（只影响命中统计，不影响密码本），返回是否成功。"""
        try:
            p = str((row or {}).get("password") or "")
            if not p:
                return False
            return bool(delete_dict_password(p))
        except Exception:
            return False

    def clear_dict(self):
        """清空密码字典（命中统计随之归零），返回删除条数；失败返回 -1。"""
        try:
            return int(clear_password_dict())
        except Exception:
            return -1

    def rows(self):
        """合并成页面行：长期 -> 临时 -> 字典补缺（同口令只留最靠前的一条）。"""
        hits = self.hits()
        rows = []
        seen = set()
        for rec in self._book_rows():
            p = str((rec or {}).get("password") or "")
            if p and p not in seen:
                seen.add(p)
                rows.append(_make_row(p, _source_label((rec or {}).get("source")),
                                      "book", hits, pid=(rec or {}).get("id"),
                                      note=(rec or {}).get("note") or ""))
        for p in self.temp():
            if p not in seen:
                seen.add(p)
                rows.append(_make_row(p, _SOURCE_TEMP, "temp", hits))

        def _rank(item):
            meta = item[1] if isinstance(item[1], dict) else {}
            try:
                cnt = -int(meta.get("used_count") or 0)
            except Exception:
                cnt = 0
            try:
                last = -float(meta.get("last_used_at") or 0)
            except Exception:
                last = 0.0
            return (cnt, last, str(item[0]))

        for p, meta in sorted(hits.items(), key=_rank):
            if p not in seen:
                seen.add(p)
                rows.append(_make_row(p, "", "dict", {p: meta}))
        return rows

    def add(self, password, note=""):
        """行级新增一条长期口令，返回是否成功（重复口令由调用方先行拦截）。"""
        try:
            if self._state is not None and hasattr(self._state, "add_password_row"):
                return int(self._state.add_password_row(password, note=note) or 0) > 0
            if self._state is not None:
                self._state.add_long_password(password)      # 旧整表桩
                return True
            return int(add_password_row(password, note=note) or 0) > 0
        except Exception:
            return False

    def save(self, pid, old, new, note=None):
        """保存单行的口令 / 备注，返回是否成功。

        note=None 表示数据源不支持备注（旧整表桩），此时只处理口令改名；
        行级模式只写入真正发生变化的字段（口令与备注都没变则视为成功）。
        """
        try:
            if self._row_api():
                fields = {}
                if new is not None and str(new) != str(old):
                    fields["password"] = str(new)
                if note is not None:
                    fields["note"] = str(note)
                if not fields:
                    return True
                if pid is None:
                    return False
                if self._state is not None:
                    return bool(self._state.update_password_row(pid, **fields))
                return bool(update_password_row(pid, **fields))
            # 旧整表桩：只能保位改名，不支持备注
            if new is None or str(new) == str(old):
                return note is None or str(note or "") == ""
            items = self.book()
            try:
                idx = items.index(str(old))
            except ValueError:
                return False
            items[idx] = str(new)
            return self._legacy_replace(items)
        except Exception:
            return False

    def remove(self, row):
        """删除一行长期口令（行级按 id 精确删除），返回是否成功。"""
        try:
            row = row or {}
            if self._row_api():
                pid = row.get("id")
                if pid is None:
                    return False
                if self._state is not None:
                    return bool(self._state.delete_password_row(pid))
                return bool(delete_password_row(pid))
            items = self.book()
            p = str(row.get("password") or "")
            if p not in items:
                return False
            return self._legacy_replace([x for x in items if x != p])
        except Exception:
            return False

    def dedup_book(self):
        """移除重复口令（保留首次出现），返回移除条数；无重复返回 0，失败返回 -1。

        重复只可能来自旧版非 UNIQUE 表的遗留行；走整表覆盖时同口令的备注按
        db.set_passwords 的口令回填规则保留。
        """
        try:
            items = self.book()
            seen = set()
            out = []
            for p in items:
                if p not in seen:
                    seen.add(p)
                    out.append(p)
            if len(out) == len(items):
                return 0
            if not self._legacy_replace(out):
                return -1
            return len(items) - len(out)
        except Exception:
            return -1

    def _legacy_replace(self, passwords):
        """旧整表桩的覆盖写回（与改造前的 set_passwords 路径一致）。"""
        try:
            if self._state is not None:
                self._state.set_passwords(list(passwords))
            else:
                db.set_passwords(list(passwords))
            return True
        except Exception:
            return False


# ---------------------------------------------------------------------------
# 固定提取码数据层（私有）：share_code_map 按 share_uk 行级读 / 增 / 改 / 删
# ---------------------------------------------------------------------------

class _ShareData:
    """固定提取码的数据层：share_uk 为主键，行级读写（含备注 / 需挑选）。

    行级优先顺序与 _PwData 一致：
      1) state 提供对应接口（share_code_map / add_share_code / find_share_entry /
         set_share_code_map；AppState 已提供）时经 state 代理落库；
      2) 行级更新 / 删除没有 state 代理，一律走 password_book 的薄封装
         （update_share_code_row / delete_share_code_row → db.update/delete_share_code）；
      3) 无 state 时读取也直连 password_book / db 的只读入口。
    任何异常都退化为「空 / 失败」，绝不让页面因数据层抖动而崩。
    """

    def __init__(self, state=None):
        self._state = state

    def rows(self):
        """全部行：[{"share_uk","code","note","pick","updated_at"}, ...]（pick 为 0/1）。"""
        try:
            if self._state is not None and hasattr(self._state, "share_code_map"):
                items = self._state.share_code_map()
            else:
                items = list_share_code_rows()
        except Exception:
            return []
        out = []
        for it in items or []:
            if not isinstance(it, dict):
                continue
            uk = str(it.get("share_uk") or "").strip()
            code = str(it.get("code") or "").strip()
            if not uk or not code:
                continue
            out.append({"share_uk": uk, "code": code,
                        "note": str(it.get("note") or ""),
                        "pick": 1 if _pick_on(it.get("pick")) else 0,
                        "updated_at": it.get("updated_at") or 0})
        return out

    def find(self, share_uk):
        """按 share_uk 取单行（无记录 / 异常返回 None）。"""
        try:
            if self._state is not None and hasattr(self._state, "find_share_entry"):
                return self._state.find_share_entry(share_uk)
            return db.find_share_entry(share_uk)
        except Exception:
            return None

    def add(self, share_uk, code, note="", pick=0):
        """行级新增（同 UK UPSERT 覆盖），返回是否成功。"""
        try:
            if self._state is not None and hasattr(self._state, "add_share_code"):
                return bool(self._state.add_share_code(share_uk, code, note, pick))
            return bool(add_share_code_row(share_uk, code, note, pick))
        except Exception:
            return False

    def update(self, share_uk, new_share_uk=None, code=None, note=None, pick=None):
        """行级更新（只写显式提供的字段，可为分享者 UK 改名），返回是否命中。"""
        try:
            if self._state is not None and hasattr(self._state, "update_share_code"):
                return bool(self._state.update_share_code(
                    share_uk, new_share_uk=new_share_uk, code=code,
                    note=note, pick=pick))
            return bool(update_share_code_row(share_uk, new_share_uk=new_share_uk,
                                              code=code, note=note, pick=pick))
        except Exception:
            return False

    def remove(self, share_uk):
        """行级按 share_uk 精确删除，返回是否命中。"""
        try:
            if self._state is not None and hasattr(self._state, "delete_share_code"):
                return bool(self._state.delete_share_code(share_uk))
            return bool(delete_share_code_row(share_uk))
        except Exception:
            return False

    def replace_all(self, items):
        """整表覆盖（批量文本编辑保存，沿用旧弹窗的 set_share_code_map 语义）。"""
        try:
            if self._state is not None and hasattr(self._state, "set_share_code_map"):
                return bool(self._state.set_share_code_map(items))
            if self._state is None:
                return bool(set_share_code_rows(items))
            return False                   # 桩既无行级也无整表接口：无法保存
        except Exception:
            return False
