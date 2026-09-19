# -*- coding: utf-8 -*-
"""密码本页（M4）：口令表 + 命中统计 + 搜索 / 筛选 + 复制 / 新增 / 编辑 / 删除；
另含固定提取码视图（特殊网盘作者的固定提取码行级编辑 + 批量文本编辑）。

职责：
- PasswordBookPage：页头（「密码本」+ 口令总数 + 命中过 + 模式分段）、口令视图
  （细统计条、搜索实时过滤、四筛选分段（全部 / 命中过 / 未命中 / 有来源）、口令表
  （口令 / 来源 / 命中次数 / 最近命中 / 备注 + 行内 复制 / 编辑 / 删除）、空态、
  Ctrl+F 聚焦搜索、Delete 删除选中行（二次确认）、刷新、点列头排序（仅显示层）、
  多选（Shift/Ctrl）时「复制选中 / 删除选中」、查重、实时刷新（QTimer + showEvent）、
  固定提取码视图（分享者UK / 提取码 / 需挑选 / 备注 + 行内 编辑 / 删除 +
  新增提取码 + 批量编辑（文本））
- 行数据 = 「长期密码本 ∪ 本次临时密码 ∪ 解压字典」按口令合并去重（长期在前；
  字典只补缺），来源列区分：手动（长期）/ 剪贴板（临时）/ —（仅字典收录，无主动来源）
- 固定提取码行 = share_code_map 表（share_uk 主键）的完整字段（含 note / pick），
  空态、计数与模式分段计数随 reload 刷新；批量文本沿用「分享者UK 提取码 [pick] [#备注]」
  行格式（parse/format 复用 password_book，格式与语义未改）
- _PwData：页面私有的行级数据层。长期口令的读 / 增 / 改 / 删按行 id 直连
  db.list_passwords / add_password / update_password / delete_password（经 state 的
  行级代理 password_rows/add_password_row/update_password_row/delete_password_row）；
  只提供旧整表接口的离线桩退回 passwords/set_passwords 合成行（保证旧桩可用）。
  临时密码与命中统计仍只读 state.temp_passwords / db.load_password_dict。
- _ShareData：固定提取码的行级数据层。读 / 增 / 整表覆盖经 state 的 share 代理
  （share_code_map/add_share_code/set_share_code_map），改 / 删走 password_book 的
  薄封装（update_share_code_row / delete_share_code_row → db.update/delete_share_code）。
  本模块绝不直接读写 SQLite 文件（测试可对这些入口打桩）。
关键入口：PasswordBookPage / EMPTY_BOOK / EMPTY_PWFILTER / EMPTY_SHARE
依赖：PyQt5、db、password_book（行级 helper / 行格式解析器）、style（PALETTE）、widgets（Glyph / SegControl / show_toast）
注意：口令与固定提取码属隐私数据——本模块不把明文写进任何日志、提示或 tooltip；
      复制是用户显式动作（只写系统剪贴板）；删除只动密码本 / 提取码条目，绝不触碰任何文件。
注意：备注列来自 passwords.note（行级 API），为空显示「—」；编辑对话框同时改口令与备注，
      备注可只改不改口令；重名口令按 id 定向删除，不再整体重写。
注意：排序只重排视图（点列头升 / 降序，默认命中次数降序），绝不改写密码本顺序——
      解压尝试顺序只由库内 id 顺序决定，任何表头点击都不会改变它；「查重」仍会清理
      重复行（整表覆盖时按口令保留备注与来源）。
注意：表格复用 #taskTable 的既有 QSS；口令表列头只在本地修正（_PwHeader 自绘排序
      指示器 + 本地右侧 11px padding，不改 style.py）；自绘取色一律走 PALETTE /
      widgets 的 token 机制。
"""
# allow: SIZE_OK — 交付约束只允许新建本模块与测试两个文件；本模块 = 页面 + 私有模型 /
# 表格 / 编辑对话框 / 数据门面（常规应拆 3~4 个文件，此处按单文件约束合并在一个页面单元）。
import time

from PyQt5.QtCore import (QAbstractTableModel, QEvent, QItemSelectionModel,
                          QTimer, Qt, pyqtSignal)
from PyQt5.QtGui import (QColor, QFont, QFontMetrics, QKeySequence, QPainter,
                         QPainterPath)
from PyQt5.QtWidgets import (QAbstractItemView, QApplication, QCheckBox, QDialog,
                             QHBoxLayout, QHeaderView, QLabel, QLineEdit,
                             QMessageBox, QPlainTextEdit, QPushButton, QShortcut,
                             QStackedWidget, QStyledItemDelegate,
                             QStyleOptionViewItem, QTableView, QVBoxLayout,
                             QWidget)

from .. import db
from ..password_book import (add_password_row, add_share_code_row,
                             delete_password_row, delete_share_code_row,
                             format_share_code_text, list_password_rows,
                             list_share_code_rows, parse_share_code_text,
                             set_share_code_rows, update_password_row,
                             update_share_code_row)
from .style import PALETTE, tokens
from .widgets import Glyph, SegControl, show_toast

# 空态文案（与 pages.py 的空态语气一致）
EMPTY_BOOK = "密码本还是空的 · 点「新增口令」，或让剪贴板 / 二维码自动收录提取码"
EMPTY_PWFILTER = "没有符合当前搜索 / 筛选条件的口令"
EMPTY_SHARE = "还没有固定提取码 · 点「新增提取码」，或「批量编辑（文本）」粘贴多行"

# 来源文案：显示库中真实存储的 source（manual→手动 / clipboard→剪贴板，未知原样显示）；
# 临时密码不在库里，固定标为「剪贴板」。
_SOURCE_BOOK = "手动"
_SOURCE_TEMP = "剪贴板"
_SOURCE_LABELS = {"manual": "手动", "clipboard": "剪贴板"}

# 筛选分段（key, 文案）——计数在运行期刷新
_FILTERS = (("all", "全部"), ("hit", "命中过"), ("miss", "未命中"), ("src", "有来源"))

# 视图模式分段（口令本 / 固定提取码）——计数在运行期刷新
_MODES = (("pwd", "口令"), ("share", "固定提取码"))


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
# 小控件助手（私有；pages.py 的 _EmptyOverlay / _icon_button 不跨模块引用私有名）
# ---------------------------------------------------------------------------

class _EmptyOverlay(QLabel):
    """视图空态：覆盖在目标视图之上居中一行说明（随尺寸跟随，不拦截鼠标）。"""

    def __init__(self, target, text, parent=None):
        host = parent
        if host is None:
            try:
                host = target.viewport()
            except Exception:
                host = target
        super().__init__(host)
        self._host = host
        self.setObjectName("stripHint")
        self.setAlignment(Qt.AlignCenter)
        self.setAttribute(Qt.WA_TransparentForMouseEvents, True)
        self.setText(str(text))
        self.hide()
        try:
            host.installEventFilter(self)
        except Exception:
            pass
        self._sync()

    def _sync(self):
        try:
            self.setGeometry(self._host.rect())
        except Exception:
            pass

    def eventFilter(self, obj, event):
        try:
            if obj is self._host and event.type() in (QEvent.Resize, QEvent.Show):
                self._sync()
        except Exception:
            pass
        return False                       # 绝不消费视图自己的事件

    def set_empty(self, empty, text=None):
        if text is not None:
            self.setText(str(text))
        self._sync()
        self.setVisible(bool(empty))
        if empty:
            try:
                self.raise_()
            except Exception:
                pass


def _icon_button(glyph, tip, parent=None, size=28):
    """方形图标按钮（刷新等）；取色与悬停底由 QSS #iconBtn 负责。"""
    btn = QPushButton(parent)
    btn.setObjectName("iconBtn")
    btn.setFixedSize(size, size)
    btn.setToolTip(str(tip))
    btn.setCursor(Qt.PointingHandCursor)
    lay = QHBoxLayout(btn)
    lay.setContentsMargins(0, 0, 0, 0)
    lay.addWidget(Glyph(glyph, btn, 15, role="muted"), 0, Qt.AlignCenter)
    return btn


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


# ---------------------------------------------------------------------------
# 口令表：模型 / 委托 / 视图
# ---------------------------------------------------------------------------

class _PwModel(QAbstractTableModel):
    """口令表数据模型：列 口令 / 来源 / 命中次数 / 最近命中 / 备注 / 行操作。

    口令列直接显示原文（不遮蔽）；UserRole = 口令（显示 / 复制用），行身份取行字典的 id。
    """

    HEADERS = ("口令", "来源", "命中次数", "最近命中", "备注", "")
    (COL_PWD, COL_SRC, COL_HITS, COL_LAST, COL_NOTE, COL_ACT) = range(6)

    # 每列内容对齐：单元格与表头文字共用同一口径（表头文字才能与单元格文字对齐）
    _ALIGN = {
        COL_PWD: Qt.AlignLeft | Qt.AlignVCenter,
        COL_SRC: Qt.AlignLeft | Qt.AlignVCenter,
        COL_HITS: Qt.AlignRight | Qt.AlignVCenter,
        COL_LAST: Qt.AlignLeft | Qt.AlignVCenter,
        COL_NOTE: Qt.AlignLeft | Qt.AlignVCenter,
        COL_ACT: Qt.AlignLeft | Qt.AlignVCenter,
    }

    def __init__(self, parent=None):
        super().__init__(parent)
        self._rows = []

    def set_rows(self, rows):
        self.beginResetModel()
        self._rows = [dict(r) for r in (rows or []) if isinstance(r, dict)]
        self.endResetModel()

    def rows(self):
        return [dict(r) for r in self._rows]

    def row_at(self, row):
        try:
            i = int(row)
        except Exception:
            return None
        if 0 <= i < len(self._rows):
            return dict(self._rows[i])
        return None

    def rowCount(self, parent=None):
        if parent is not None and parent.isValid():
            return 0
        return len(self._rows)

    def columnCount(self, parent=None):
        if parent is not None and parent.isValid():
            return 0
        return len(self.HEADERS)

    def headerData(self, section, orientation, role=Qt.DisplayRole):
        if orientation != Qt.Horizontal:
            return None
        if role == Qt.TextAlignmentRole:
            return int(self._ALIGN.get(section, Qt.AlignLeft | Qt.AlignVCenter))
        if role != Qt.DisplayRole:
            return None
        if 0 <= section < len(self.HEADERS):
            return self.HEADERS[section]
        return None

    def flags(self, index):
        if not index.isValid():
            return Qt.NoItemFlags
        return Qt.ItemIsEnabled | Qt.ItemIsSelectable

    def _display(self, row, col):
        if col == self.COL_PWD:
            return str(row.get("password") or "")
        if col == self.COL_SRC:
            return str(row.get("source") or "—")
        if col == self.COL_HITS:
            return str(_hit_count(row))
        if col == self.COL_LAST:
            return _fmt_hit_time(row.get("last_hit"))
        if col == self.COL_NOTE:
            return str(row.get("note") or "—")
        return ""

    def data(self, index, role=Qt.DisplayRole):
        if not index.isValid() or not (0 <= index.row() < len(self._rows)):
            return None
        row = self._rows[index.row()]
        col = index.column()
        if role == Qt.UserRole:
            return row.get("password")
        if role == Qt.DisplayRole:
            return self._display(row, col)
        if role == Qt.ToolTipRole:
            if col == self.COL_PWD:
                return "口令：明文显示（过长时中间省略）· 双击行可复制"
            if col == self.COL_SRC:
                return "来源：%s" % (row.get("source") or "仅解压字典收录（无主动来源）")
            if col == self.COL_HITS:
                return "解压时命中次数：%d" % _hit_count(row)
            if col == self.COL_LAST:
                return "%s（最近一次命中）" % _fmt_hit_time(row.get("last_hit"))
            if col == self.COL_NOTE:
                note = str(row.get("note") or "")
                if note:
                    return "备注：%s" % note
                return "备注为空 · 点「编辑」可添加备注"
            return None
        if role == Qt.TextAlignmentRole:
            return int(self._ALIGN.get(col, Qt.AlignLeft | Qt.AlignVCenter))
        return None


class _PwDelegate(QStyledItemDelegate):
    """口令列 / 最近命中列用等宽字体（对齐 TaskTable 的路径列观感）。"""

    MONO_COLS = (_PwModel.COL_PWD, _PwModel.COL_LAST)

    def paint(self, painter, option, index):
        if index.column() not in self.MONO_COLS:
            super().paint(painter, option, index)
            return
        opt = QStyleOptionViewItem(option)
        font = QFont(opt.font)
        font.setFamily("Consolas")
        font.setStyleHint(QFont.Monospace)
        opt.font = font
        # 同步等宽字体的度量：否则 Qt 用界面字体的宽度决定省略，可能把
        # 更宽的等宽时间戳截短（或反过来超出列宽被硬裁）
        opt.fontMetrics = QFontMetrics(font)
        super().paint(painter, opt, index)


class _PwHeader(QHeaderView):
    """口令表专用列头：自绘排序指示器（▼/▲），保证不压住表头文字。

    复用 #taskTable 的共享 QSS 时，QStyleSheetStyle 不会为指示器预留表头文字
    宽度（Qt 已知行为）：原生指示器会压到右对齐的「命中次数」最后一个字形上，
    且在深色主题下几乎不可见。本类配合 _PwTable 的局部 QSS（把原生指示器收成
    0 尺寸）在表头右侧 padding 留白里自绘：

      · 底边宽 7px、高 4px、右缘距 section 右缘 2px；
      · 与表头文字保持 2px 间隙（右侧 padding 11px = 2 + 7 + 2，见 _PwTable）；
      · 取色用当前主题的 QSS token head_fg（浅色 #616161 / 深色 #cccccc），
        主题切换由 tokens() 自动跟随。

    仅 _PwTable 使用：另外三张复用 #taskTable 的表（任务表 / 提取码表 / 回溯表）
    不换成此类，表头外观保持不变。
    """

    _W = 7        # 指示器底边宽（逻辑 px）
    _H = 4        # 指示器高（逻辑 px）
    _RIGHT = 2    # 指示器右缘距 section 右缘（逻辑 px）

    def paintEvent(self, event):
        """先按常规画列头（底 + 文字），再在右侧留白里自绘 ▼/▲。

        不用 paintSection 覆写：PyQt5 不会把 C++ 对 paintSection 的调用派发到
        Python 覆写（已实测），paintEvent 是可靠的虚函数入口。
        """
        super().paintEvent(event)
        if not self.isSortIndicatorShown():
            return
        logical = int(self.sortIndicatorSection())
        if not (0 <= logical < self.count()):
            return
        width = int(self.sectionSize(logical))
        if width <= 0:
            return
        try:
            color = QColor(str(tokens().get("head_fg") or "#616161"))
        except Exception:
            color = QColor("#616161")
        right = float(self.sectionViewportPosition(logical) + width - self._RIGHT)
        cx = right - self._W / 2.0
        top = float(self.viewport().height()) / 2.0 - self._H / 2.0
        path = QPainterPath()
        if self.sortIndicatorOrder() == Qt.DescendingOrder:
            path.moveTo(cx - self._W / 2.0, top)
            path.lineTo(cx + self._W / 2.0, top)
            path.lineTo(cx, top + self._H)
        else:
            path.moveTo(cx - self._W / 2.0, top + self._H)
            path.lineTo(cx + self._W / 2.0, top + self._H)
            path.lineTo(cx, top)
        path.closeSubpath()
        painter = QPainter(self.viewport())
        try:
            painter.setRenderHint(QPainter.Antialiasing, True)
            painter.setPen(Qt.NoPen)
            painter.setBrush(color)
            painter.drawPath(path)
        finally:
            painter.end()


class _PwTable(QTableView):
    """口令表：掩码列 + 行内 复制 / 编辑 / 删除；Delete 键发 deleteKeyPressed。

    支持 ExtendedSelection（Shift / Ctrl 多选）；点列头发 headerClicked（页面做
    仅显示层的排序，动作列除外）；列头指示器只做视觉提示，Qt 自身排序保持关闭。
    """

    copyRequested = pyqtSignal(int)
    editRequested = pyqtSignal(int)
    deleteRowRequested = pyqtSignal(int)
    deleteKeyPressed = pyqtSignal()
    headerClicked = pyqtSignal(int)

    def __init__(self, parent=None):
        super().__init__(parent)
        self.setObjectName("taskTable")    # 复用既有表格 QSS（style.py 禁改）
        self._model = _PwModel(self)
        self.setModel(self._model)
        self.setItemDelegate(_PwDelegate(self))
        self.setSelectionBehavior(QAbstractItemView.SelectRows)
        self.setSelectionMode(QAbstractItemView.ExtendedSelection)
        self.setEditTriggers(QAbstractItemView.NoEditTriggers)
        self.setSortingEnabled(False)      # 排序由页面在数据层做（仅视图重排）
        self.setAlternatingRowColors(True)
        self.setShowGrid(False)
        self.setWordWrap(False)
        self.setTextElideMode(Qt.ElideMiddle)
        self.setMinimumHeight(160)
        self.verticalHeader().setVisible(False)
        self.verticalHeader().setDefaultSectionSize(44)
        # 列头换成 _PwHeader（自绘排序指示器）；局部 QSS 只作用于本表列头
        # （style.py 是 4 张表共享的，禁改）：右侧 padding 8→11px 给自绘 ▼/▲
        # 留出「2px 间隙 + 7px 底边 + 2px 右缘」；Qt 原生指示器收成 0 尺寸，
        # 否则它会压在本表右对齐的「命中次数」文字上（且深色下几乎不可见）。
        hh = _PwHeader(Qt.Horizontal, self)
        self.setHorizontalHeader(hh)
        hh.setStyleSheet(
            "QHeaderView::section { padding: 7px 11px 7px 8px; }"
            "QHeaderView::down-arrow { width: 0px; height: 0px; }"
            "QHeaderView::up-arrow { width: 0px; height: 0px; }")
        hh.setHighlightSections(False)
        hh.setStretchLastSection(False)
        hh.setSectionsClickable(True)
        hh.setSortIndicatorShown(True)
        hh.sectionClicked.connect(self._on_section_clicked)
        hh.setSectionResizeMode(_PwModel.COL_PWD, QHeaderView.Stretch)
        hh.setSectionResizeMode(_PwModel.COL_NOTE, QHeaderView.Stretch)
        # 固定列宽按真实字体实测：最近命中列 16 字符时间戳（Consolas 13px）
        # 在 132px 仍会省略中间字符，140px 时完整显示（含 padding / 行内边距）
        for col, width in ((_PwModel.COL_SRC, 88), (_PwModel.COL_HITS, 84),
                           (_PwModel.COL_LAST, 140), (_PwModel.COL_ACT, 158)):
            hh.setSectionResizeMode(col, QHeaderView.Fixed)
            self.setColumnWidth(col, width)
        self._action_widgets = []
        self._danger_buttons = []
        self.doubleClicked.connect(self._on_double_clicked)

    # ---- 装载 ----
    def set_rows(self, rows, keep_scroll=False):
        """重建行；keep_scroll=True 时保持当前竖向滚动位置（实时刷新用）。"""
        scroll = 0
        if keep_scroll:
            try:
                scroll = int(self.verticalScrollBar().value())
            except Exception:
                scroll = 0
        self._clear_actions()
        self._model.set_rows(rows)
        self._build_actions()
        if keep_scroll:
            try:
                self.verticalScrollBar().setValue(scroll)
            except Exception:
                pass
        else:
            self.scroll_to_top()

    def pw_model(self):
        return self._model

    def row_at(self, row):
        return self._model.row_at(row)

    def select_password(self, password):
        """按口令选中行（重载 / 过滤后保持选中用），返回是否命中。"""
        return self.select_passwords([password])

    def select_passwords(self, passwords):
        """按口令集合选中行（多选保持用），返回是否命中至少一行。

        只改选中集与当前行，不抢焦点；口令不在当前视图里的条目自动忽略。"""
        wanted = {str(p) for p in (passwords or []) if str(p)}
        if not wanted:
            return False
        sm = self.selectionModel()
        if sm is None:
            return False
        sm.clearSelection()
        hit = False
        first = None
        for row in range(self._model.rowCount()):
            data = self._model.row_at(row) or {}
            if str(data.get("password")) in wanted:
                idx = self._model.index(row, 0)
                sm.select(idx, QItemSelectionModel.Select | QItemSelectionModel.Rows)
                hit = True
                if first is None:
                    first = idx
        if first is not None:
            sm.setCurrentIndex(first, QItemSelectionModel.NoUpdate)
        return hit

    def selected_passwords(self):
        """当前选中的口令列表（按视图从上到下的顺序；无选中返回 []）。"""
        return [str(r.get("password") or "") for r in self.selected_rows_data()
                if str(r.get("password") or "")]

    def selected_rows_data(self):
        """当前选中的行数据列表（按视图从上到下的顺序；无选中返回 []）。"""
        try:
            idxs = sorted(self.selectionModel().selectedRows(),
                          key=lambda i: i.row())
        except Exception:
            return []
        out = []
        for idx in idxs:
            data = self._model.row_at(idx.row())
            if data:
                out.append(data)
        return out

    def set_sort_indicator(self, col, order):
        """在列头显示排序指示器（▲/▼）；不触发 Qt 自身排序。"""
        try:
            hh = self.horizontalHeader()
            hh.setSortIndicatorShown(True)
            hh.setSortIndicator(int(col), order)
        except Exception:
            pass

    def selected_row(self):
        """当前选中行数据（无选中返回 None；多选时取视图最靠上的一行）。"""
        rows = self.selected_rows_data()
        return rows[0] if rows else None

    def scroll_to_top(self):
        try:
            self.verticalScrollBar().setValue(0)
        except Exception:
            pass

    # ---- 行内操作 ----
    def _build_actions(self):
        for row in range(self._model.rowCount()):
            data = self._model.row_at(row) or {}
            widget = self._make_actions(row, data)
            self.setIndexWidget(self._model.index(row, _PwModel.COL_ACT), widget)
            self._action_widgets.append(widget)

    def _make_actions(self, row_index, data):
        kind = str(data.get("kind"))
        can_edit = kind == "book"
        can_delete = kind in ("book", "temp")
        if can_edit:
            del_tip = "从密码本删除这条口令"
        elif kind == "temp":
            del_tip = "移除这条临时口令（剪贴板捕获）"
        else:
            del_tip = "字典口令来自解压命中记录，不支持删除"
        w = QWidget(self)
        lay = QHBoxLayout(w)
        lay.setContentsMargins(0, 0, 6, 0)
        lay.setSpacing(4)
        lay.addStretch(1)
        copy_btn = self._small_button("复制", "复制口令到剪贴板")
        copy_btn.clicked.connect(
            lambda _=False, r=row_index: self.copyRequested.emit(int(r)))
        edit_btn = self._small_button(
            "编辑", "编辑这条口令" if can_edit else "临时 / 字典口令不支持编辑")
        edit_btn.setEnabled(can_edit)
        if can_edit:
            edit_btn.clicked.connect(
                lambda _=False, r=row_index: self.editRequested.emit(int(r)))
        del_btn = self._small_button("删除", del_tip, danger=True)
        del_btn.setEnabled(can_delete)
        if can_delete:
            del_btn.clicked.connect(
                lambda _=False, r=row_index: self.deleteRowRequested.emit(int(r)))
        for btn in (copy_btn, edit_btn, del_btn):
            lay.addWidget(btn)
        return w

    def _small_button(self, text, tip, danger=False):
        btn = QPushButton(str(text), self)
        btn.setObjectName("ghostSm")
        btn.setCursor(Qt.PointingHandCursor)
        btn.setToolTip(str(tip))
        if danger:
            btn.setStyleSheet("color: %s;" % PALETTE["danger"])
            self._danger_buttons.append(btn)
        return btn

    def _clear_actions(self):
        for w in self._action_widgets:
            try:
                w.setParent(None)
                w.deleteLater()
            except Exception:
                pass
        self._action_widgets = []
        self._danger_buttons = []

    def refresh_theme(self):
        """主题切换后重贴删除按钮的 danger 色（内联样式不随 QSS 自动变）。"""
        for btn in self._danger_buttons:
            try:
                btn.setStyleSheet("color: %s;" % PALETTE["danger"])
            except Exception:
                pass

    # ---- 交互 ----
    def _on_section_clicked(self, section):
        """列头点击：动作列（最后一列，表头为空）不可排序，其余转交页面处理。"""
        try:
            col = int(section)
        except Exception:
            return
        if col == _PwModel.COL_ACT:
            return
        self.headerClicked.emit(col)

    def _on_double_clicked(self, index):
        if index.isValid():
            self.copyRequested.emit(int(index.row()))

    def keyPressEvent(self, event):
        if event.key() == Qt.Key_Delete:
            self.deleteKeyPressed.emit()
            event.accept()
            return
        super().keyPressEvent(event)


# ---------------------------------------------------------------------------
# 固定提取码表：模型 / 视图
# ---------------------------------------------------------------------------

class _ShareModel(QAbstractTableModel):
    """固定提取码表数据模型：列 分享者UK / 提取码 / 需挑选 / 备注 / 行操作。

    行身份取行字典的 share_uk（库主键）；提取码按用户预期明文展示（旧弹窗也明文），
    但本模块绝不把它写进任何日志 / 回执 / tooltip 之外的输出。
    """

    HEADERS = ("分享者UK", "提取码", "需挑选", "备注", "")
    (COL_UK, COL_CODE, COL_PICK, COL_NOTE, COL_ACT) = range(5)

    # 每列内容对齐：单元格与表头文字共用同一口径（表头文字才能与单元格文字对齐）
    _ALIGN = {
        COL_UK: Qt.AlignLeft | Qt.AlignVCenter,
        COL_CODE: Qt.AlignLeft | Qt.AlignVCenter,
        COL_PICK: Qt.AlignCenter,
        COL_NOTE: Qt.AlignLeft | Qt.AlignVCenter,
        COL_ACT: Qt.AlignLeft | Qt.AlignVCenter,
    }

    def __init__(self, parent=None):
        super().__init__(parent)
        self._rows = []

    def set_rows(self, rows):
        self.beginResetModel()
        self._rows = [dict(r) for r in (rows or []) if isinstance(r, dict)]
        self.endResetModel()

    def rows(self):
        return [dict(r) for r in self._rows]

    def row_at(self, row):
        try:
            i = int(row)
        except Exception:
            return None
        if 0 <= i < len(self._rows):
            return dict(self._rows[i])
        return None

    def rowCount(self, parent=None):
        if parent is not None and parent.isValid():
            return 0
        return len(self._rows)

    def columnCount(self, parent=None):
        if parent is not None and parent.isValid():
            return 0
        return len(self.HEADERS)

    def headerData(self, section, orientation, role=Qt.DisplayRole):
        if orientation != Qt.Horizontal:
            return None
        if role == Qt.TextAlignmentRole:
            return int(self._ALIGN.get(section, Qt.AlignLeft | Qt.AlignVCenter))
        if role != Qt.DisplayRole:
            return None
        if 0 <= section < len(self.HEADERS):
            return self.HEADERS[section]
        return None

    def flags(self, index):
        if not index.isValid():
            return Qt.NoItemFlags
        return Qt.ItemIsEnabled | Qt.ItemIsSelectable

    def _display(self, row, col):
        if col == self.COL_UK:
            return str(row.get("share_uk") or "")
        if col == self.COL_CODE:
            return str(row.get("code") or "")
        if col == self.COL_PICK:
            return "是" if _pick_on(row.get("pick")) else "—"
        if col == self.COL_NOTE:
            return str(row.get("note") or "—")
        return ""

    def data(self, index, role=Qt.DisplayRole):
        if not index.isValid() or not (0 <= index.row() < len(self._rows)):
            return None
        row = self._rows[index.row()]
        col = index.column()
        if role == Qt.DisplayRole:
            return self._display(row, col)
        if role == Qt.ToolTipRole:
            if col == self.COL_UK:
                return "分享者UK：分享页上传者的数字 uid"
            if col == self.COL_CODE:
                return "固定提取码：命中该分享者时自动填入"
            if col == self.COL_PICK:
                return "需挑选：只下载选中的文件；不勾选则整包下载"
            if col == self.COL_NOTE:
                note = str(row.get("note") or "")
                if note:
                    return "备注：%s" % note
                return "备注为空 · 点「编辑」可添加备注"
            return None
        if role == Qt.TextAlignmentRole:
            return int(self._ALIGN.get(col, Qt.AlignLeft | Qt.AlignVCenter))
        return None


class _ShareTable(QTableView):
    """固定提取码表：行内 编辑 / 删除（表格 QSS 复用 #taskTable）。"""

    editRequested = pyqtSignal(int)
    deleteRowRequested = pyqtSignal(int)

    def __init__(self, parent=None):
        super().__init__(parent)
        self.setObjectName("taskTable")    # 复用既有表格 QSS（style.py 禁改）
        self._model = _ShareModel(self)
        self.setModel(self._model)
        self.setSelectionBehavior(QAbstractItemView.SelectRows)
        self.setSelectionMode(QAbstractItemView.SingleSelection)
        self.setEditTriggers(QAbstractItemView.NoEditTriggers)
        self.setAlternatingRowColors(True)
        self.setShowGrid(False)
        self.setWordWrap(False)
        self.setTextElideMode(Qt.ElideMiddle)
        self.setMinimumHeight(150)
        self.verticalHeader().setVisible(False)
        self.verticalHeader().setDefaultSectionSize(40)
        hh = self.horizontalHeader()
        hh.setHighlightSections(False)
        hh.setStretchLastSection(False)
        hh.setSectionResizeMode(_ShareModel.COL_UK, QHeaderView.Fixed)
        self.setColumnWidth(_ShareModel.COL_UK, 150)
        hh.setSectionResizeMode(_ShareModel.COL_CODE, QHeaderView.Fixed)
        self.setColumnWidth(_ShareModel.COL_CODE, 110)
        hh.setSectionResizeMode(_ShareModel.COL_PICK, QHeaderView.Fixed)
        self.setColumnWidth(_ShareModel.COL_PICK, 72)
        hh.setSectionResizeMode(_ShareModel.COL_NOTE, QHeaderView.Stretch)
        hh.setSectionResizeMode(_ShareModel.COL_ACT, QHeaderView.Fixed)
        self.setColumnWidth(_ShareModel.COL_ACT, 128)
        self._action_widgets = []
        self._danger_buttons = []

    # ---- 装载 ----
    def set_rows(self, rows):
        self._clear_actions()
        self._model.set_rows(rows)
        self._build_actions()
        self.scroll_to_top()

    def share_model(self):
        return self._model

    def row_at(self, row):
        return self._model.row_at(row)

    def scroll_to_top(self):
        try:
            self.verticalScrollBar().setValue(0)
        except Exception:
            pass

    # ---- 行内操作 ----
    def _build_actions(self):
        for row in range(self._model.rowCount()):
            data = self._model.row_at(row) or {}
            widget = self._make_actions(row, data)
            self.setIndexWidget(self._model.index(row, _ShareModel.COL_ACT), widget)
            self._action_widgets.append(widget)

    def _make_actions(self, row_index, data):
        w = QWidget(self)
        lay = QHBoxLayout(w)
        lay.setContentsMargins(0, 0, 6, 0)
        lay.setSpacing(4)
        lay.addStretch(1)
        edit_btn = self._small_button("编辑", "编辑这条固定提取码")
        edit_btn.clicked.connect(
            lambda _=False, r=row_index: self.editRequested.emit(int(r)))
        del_btn = self._small_button("删除", "删除这条固定提取码", danger=True)
        del_btn.clicked.connect(
            lambda _=False, r=row_index: self.deleteRowRequested.emit(int(r)))
        for btn in (edit_btn, del_btn):
            lay.addWidget(btn)
        return w

    def _small_button(self, text, tip, danger=False):
        btn = QPushButton(str(text), self)
        btn.setObjectName("ghostSm")
        btn.setCursor(Qt.PointingHandCursor)
        btn.setToolTip(str(tip))
        if danger:
            btn.setStyleSheet("color: %s;" % PALETTE["danger"])
            self._danger_buttons.append(btn)
        return btn

    def _clear_actions(self):
        for w in self._action_widgets:
            try:
                w.setParent(None)
                w.deleteLater()
            except Exception:
                pass
        self._action_widgets = []
        self._danger_buttons = []

    def refresh_theme(self):
        """主题切换后重贴删除按钮的 danger 色（内联样式不随 QSS 自动变）。"""
        for btn in self._danger_buttons:
            try:
                btn.setStyleSheet("color: %s;" % PALETTE["danger"])
            except Exception:
                pass


# ---------------------------------------------------------------------------
# 新增 / 编辑口令小对话框
# ---------------------------------------------------------------------------

class _PasswordEditDialog(QDialog):
    """新增 / 编辑单条口令与备注：口令直接明文显示；空口令不可保存。

    新增（初始口令为空）时提供「批量导入」：同一对话框切换为多行输入（一行一个
    口令），备注对每个口令生效；「返回单条」切回单条表单。编辑既有口令不提供批量
    入口（避免把「改一条」误变成「加一批」）。对话框只收集输入，不写库。
    """

    def __init__(self, parent=None, title="新增口令", password="", note=""):
        super().__init__(parent)
        self.setWindowTitle(str(title))
        self.setMinimumWidth(380)
        self._batch = False
        self._batch_ok = not bool(str(password or "").strip())   # 仅新增支持批量
        lay = QVBoxLayout(self)
        lay.setContentsMargins(16, 14, 16, 14)
        lay.setSpacing(10)
        head = QLabel(str(title), self)
        head.setObjectName("appTitle")
        lay.addWidget(head)
        hint = QLabel("口令存入本机 toolbox.db 的长期密码本；解压时按从上到下的顺序尝试。",
                      self)
        hint.setObjectName("stripHint")
        hint.setWordWrap(True)
        lay.addWidget(hint)
        self.edit = QLineEdit(self)
        self.edit.setPlaceholderText("输入口令")
        self.edit.setText(str(password or ""))
        lay.addWidget(self.edit)
        self.batch_edit = QPlainTextEdit(self)
        self.batch_edit.setPlaceholderText(
            "每行一个口令（空行忽略；重复行自动去重）")
        self.batch_edit.setMinimumHeight(180)
        self.batch_edit.hide()
        lay.addWidget(self.batch_edit)
        note_label = QLabel("备注（可选，仅本机可见）：", self)
        note_label.setObjectName("stripHint")
        lay.addWidget(note_label)
        self.note_edit = QLineEdit(self)
        self.note_edit.setPlaceholderText("例如：老王分享 / 某网盘提取码")
        self.note_edit.setText(str(note or ""))
        lay.addWidget(self.note_edit)
        btns = QHBoxLayout()
        self.batch_btn = None
        if self._batch_ok:
            self.batch_btn = QPushButton("批量导入", self)
            self.batch_btn.setCursor(Qt.PointingHandCursor)
            self.batch_btn.setToolTip("切换到多行输入：每行一个口令，备注对全部口令生效")
            self.batch_btn.clicked.connect(self._toggle_batch)
            btns.addWidget(self.batch_btn)
        btns.addStretch(1)
        self.save_btn = QPushButton("保存", self)
        self.save_btn.setObjectName("primary")
        self.save_btn.setCursor(Qt.PointingHandCursor)
        self.save_btn.setEnabled(bool(str(password or "").strip()))
        self.save_btn.clicked.connect(self.accept)
        cancel = QPushButton("取消", self)
        cancel.clicked.connect(self.reject)
        btns.addWidget(self.save_btn)
        btns.addWidget(cancel)
        lay.addLayout(btns)
        self.edit.textChanged.connect(self._sync_save)
        self.batch_edit.textChanged.connect(self._sync_save)
        self.edit.returnPressed.connect(self._on_return)
        self.edit.setFocus()

    def _toggle_batch(self):
        """单条 / 批量输入切换（同一表单；备注字段两种模式共用）。"""
        self._batch = not self._batch
        self.edit.setVisible(not self._batch)
        self.batch_edit.setVisible(self._batch)
        if self.batch_btn is not None:
            self.batch_btn.setText("返回单条" if self._batch else "批量导入")
            self.batch_btn.setToolTip(
                "返回单条口令输入" if self._batch
                else "切换到多行输入：每行一个口令，备注对全部口令生效")
        self.save_btn.setText("导入" if self._batch else "保存")
        self._sync_save()
        if self._batch:
            self.batch_edit.setFocus()
        else:
            self.edit.setFocus()

    def is_batch(self):
        """当前是否处于批量导入模式。"""
        return bool(self._batch)

    def batch_values(self):
        """批量模式输入 -> (去重后的非空口令列表, 空行数, 批内重复行数)。不写库。"""
        values = []
        seen = set()
        empty = 0
        dup = 0
        for line in str(self.batch_edit.toPlainText()).splitlines():
            item = line.strip()
            if not item:
                empty += 1
                continue
            if item in seen:
                dup += 1
                continue
            seen.add(item)
            values.append(item)
        return values, empty, dup

    def _sync_save(self, _text=None):
        if self._batch:
            values, _empty, _dup = self.batch_values()
            self.save_btn.setEnabled(bool(values))
        else:
            self.save_btn.setEnabled(bool(self.edit.text().strip()))

    def _on_return(self):
        if self.save_btn.isEnabled():
            self.accept()

    def password(self):
        if self._batch:
            values, _empty, _dup = self.batch_values()
            return values[0] if values else ""
        return str(self.edit.text()).strip()

    def note(self):
        return str(self.note_edit.text()).strip()


# ---------------------------------------------------------------------------
# 新增 / 编辑固定提取码小对话框 + 批量文本编辑对话框
# ---------------------------------------------------------------------------

class _ShareEditDialog(QDialog):
    """新增 / 编辑单条固定提取码：分享者UK + 提取码 + 需挑选 + 备注。

    UK 只接受纯数字、提取码只接受 1~16 位字母或数字——与文本行格式的解析口径
    一致，保证行编辑写入的数据能被「批量编辑（文本）」完整往返。
    """

    def __init__(self, parent=None, title="新增提取码", share_uk="", code="",
                 note="", pick=0):
        super().__init__(parent)
        self.setWindowTitle(str(title))
        self.setMinimumWidth(420)
        lay = QVBoxLayout(self)
        lay.setContentsMargins(16, 14, 16, 14)
        lay.setSpacing(10)
        head = QLabel(str(title), self)
        head.setObjectName("appTitle")
        lay.addWidget(head)
        hint = QLabel("分享者UK = 分享页上传者的数字 uid；提取码为 1~16 位字母或数字。\n"
                      "「需挑选」表示只下载选中的文件；不勾选则整包下载。", self)
        hint.setObjectName("stripHint")
        hint.setWordWrap(True)
        lay.addWidget(hint)

        uk_label = QLabel("分享者UK（纯数字）：", self)
        uk_label.setObjectName("stripHint")
        lay.addWidget(uk_label)
        self.uk_edit = QLineEdit(self)
        self.uk_edit.setPlaceholderText("例如：3567282991")
        self.uk_edit.setText(str(share_uk or ""))
        lay.addWidget(self.uk_edit)

        code_label = QLabel("提取码：", self)
        code_label.setObjectName("stripHint")
        lay.addWidget(code_label)
        self.code_edit = QLineEdit(self)
        self.code_edit.setPlaceholderText("例如：ab12")
        self.code_edit.setText(str(code or ""))
        lay.addWidget(self.code_edit)

        self.pick_cb = QCheckBox("需要挑选下载文件（不勾选则整包下载）", self)
        self.pick_cb.setChecked(bool(pick))
        lay.addWidget(self.pick_cb)

        note_label = QLabel("备注（可选，仅本机可见）：", self)
        note_label.setObjectName("stripHint")
        lay.addWidget(note_label)
        self.note_edit = QLineEdit(self)
        self.note_edit.setPlaceholderText("例如：老王分享 / 需要挑选的网盘作者")
        self.note_edit.setText(str(note or ""))
        lay.addWidget(self.note_edit)

        btns = QHBoxLayout()
        btns.addStretch(1)
        self.save_btn = QPushButton("保存", self)
        self.save_btn.setObjectName("primary")
        self.save_btn.setCursor(Qt.PointingHandCursor)
        self.save_btn.clicked.connect(self.accept)
        cancel = QPushButton("取消", self)
        cancel.clicked.connect(self.reject)
        btns.addWidget(self.save_btn)
        btns.addWidget(cancel)
        lay.addLayout(btns)

        self._sync_save()
        self.uk_edit.textChanged.connect(self._sync_save)
        self.code_edit.textChanged.connect(self._sync_save)
        self.uk_edit.returnPressed.connect(self._on_return)
        self.code_edit.returnPressed.connect(self._on_return)
        self.uk_edit.setFocus()

    def _valid(self):
        return (_valid_share_uk(self.uk_edit.text())
                and _valid_share_code(self.code_edit.text()))

    def _sync_save(self, _text=None):
        self.save_btn.setEnabled(self._valid())

    def _on_return(self):
        if self.save_btn.isEnabled():
            self.accept()

    def values(self):
        """(分享者UK, 提取码, 备注, 需挑选 0/1)。"""
        return (str(self.uk_edit.text()).strip(),
                str(self.code_edit.text()).strip(),
                str(self.note_edit.text()).strip(),
                1 if self.pick_cb.isChecked() else 0)


class _ShareTextDialog(QDialog):
    """批量编辑固定提取码：每行「分享者UK 提取码 [pick] [#备注]」（与旧弹窗同格式）。

    只做文本 <-> 行列表的往返，解析 / 格式化完全复用 password_book 的既有助手，
    行格式与语义保持不变。保存由调用方经 set_share_code_map 整表覆盖落库。
    """

    def __init__(self, parent=None, rows=None):
        super().__init__(parent)
        self.setWindowTitle("批量编辑固定提取码")
        self.resize(560, 460)
        lay = QVBoxLayout(self)
        lay.setContentsMargins(16, 14, 16, 14)
        lay.setSpacing(10)
        head = QLabel("批量编辑固定提取码", self)
        head.setObjectName("appTitle")
        lay.addWidget(head)
        hint = QLabel(
            "每行一条：分享者UK 提取码 [pick] [#备注]，例如：\n"
            "3567282991 ab12 #老王\n"
            "3567282991 ab12 pick #老王\n"
            "第三列写 pick / 挑选 / 1 表示该分享者需要挑选下载文件（不写则整包下载）；\n"
            "空行、以 # 开头的注释行与格式不合法的行会被忽略。", self)
        hint.setObjectName("stripHint")
        hint.setWordWrap(True)
        lay.addWidget(hint)

        self.edit = QPlainTextEdit(self)
        self.edit.setPlaceholderText("每行一条：分享者UK 提取码 [pick] [#备注]")
        self.edit.setPlainText(format_share_code_text(rows or []))
        lay.addWidget(self.edit, 1)

        count_row = QHBoxLayout()
        count_row.addStretch(1)
        self.count_lbl = QLabel("", self)
        self.count_lbl.setObjectName("stripHint")
        count_row.addWidget(self.count_lbl)
        lay.addLayout(count_row)

        btns = QHBoxLayout()
        btns.addStretch(1)
        self.save_btn = QPushButton("保存", self)
        self.save_btn.setObjectName("primary")
        self.save_btn.setCursor(Qt.PointingHandCursor)
        self.save_btn.clicked.connect(self.accept)
        cancel = QPushButton("取消", self)
        cancel.clicked.connect(self.reject)
        btns.addWidget(self.save_btn)
        btns.addWidget(cancel)
        lay.addLayout(btns)

        self.edit.textChanged.connect(self._update_count)
        self._update_count()
        self.edit.setFocus()

    def _update_count(self):
        n = len(parse_share_code_text(self.edit.toPlainText()))
        self.count_lbl.setText("共 %d 条" % n)

    def items(self):
        """当前文本解析出的固定提取码列表（与 parse_share_code_text 同一语义）。"""
        return parse_share_code_text(self.edit.toPlainText())


# ---------------------------------------------------------------------------
# 密码本页
# ---------------------------------------------------------------------------

class PasswordBookPage(QWidget):
    """密码本页：口令视图（搜索 + 四筛选 + 口令表 + 行内操作，含排序 / 查重）+
    固定提取码视图（特殊网盘作者的固定提取码：行级增删改 + 批量文本编辑）。

    两个视图共用一页、用模式分段（口令 / 固定提取码）互斥切换；口令数据经 _PwData、
    固定提取码经 _ShareData 行级读写。
    信号：notice(text)（复制/新增/编辑/删除等用户可见回执，绝不携带明文口令或提取码）、
          changed()（口令本发生写操作后发出，宿主可据此刷新标签徽标）。
    宿主接缝：reload()（切回本页时重查两个数据集）/ refresh_theme()（主题切换后重贴内联色）。
    """

    notice = pyqtSignal(str)
    changed = pyqtSignal()

    def __init__(self, state=None, parent=None):
        super().__init__(parent)
        self.state = state
        self._data = _PwData(state)
        self._share_data = _ShareData(state)
        self._rows = []
        self._share_rows = []
        self._keyword = ""
        self._filter = "all"
        self._mode = "pwd"
        # 视图排序状态：默认按命中次数降序（命中多的排最前）；仅重排显示，不写库
        self._sort_col = _PwModel.COL_HITS
        self._sort_order = Qt.DescendingOrder
        self._live_signature = None

        root = QVBoxLayout(self)
        root.setContentsMargins(12, 12, 12, 10)
        root.setSpacing(10)

        root.addLayout(self._build_header())

        # 口令 / 固定提取码两个视图互斥切换（模式分段在页头）
        self.stack = QStackedWidget(self)
        self.stack.addWidget(self._build_pwd_view())
        self.stack.addWidget(self._build_share_view())
        root.addWidget(self.stack, 1)

        # Ctrl+F：页内任意子控件有焦点时聚焦搜索框（页不可见时不抢焦点）
        self._sc_search = QShortcut(QKeySequence("Ctrl+F"), self)
        self._sc_search.setContext(Qt.WidgetWithChildrenShortcut)
        self._sc_search.activated.connect(self._focus_search)

        self.reload()

        # 实时刷新：页面可见时每 2s 校验一次数据签名（变了才重载；弹窗打开时跳过）。
        # 定时器只在 showEvent 里启动——从未显示过的页面绝不后台轮询（hideEvent 停表）。
        self._live_timer = QTimer(self)
        self._live_timer.setInterval(2000)
        self._live_timer.timeout.connect(self._live_tick)
        self._live_signature = self._data_signature()

    # ---- 构建 ----
    def _build_header(self):
        head = QHBoxLayout()
        head.setSpacing(8)
        head.addWidget(Glyph("key", self, 16, role="muted"))
        title = QLabel("密码本", self)
        title.setObjectName("appTitle")
        head.addWidget(title)
        self.total_label = QLabel("", self)
        self.total_label.setObjectName("modeBadge")
        head.addWidget(self.total_label)
        self.hit_label = QLabel("", self)
        self.hit_label.setObjectName("modeBadge")
        head.addWidget(self.hit_label)
        head.addStretch(1)
        self.seg_mode = SegControl(self)
        self.seg_mode.set_items([(label, key) for key, label in _MODES])
        self.seg_mode.currentChanged.connect(self._on_mode)
        head.addWidget(self.seg_mode)
        self.refresh_btn = _icon_button("refresh", "刷新", self)
        self.refresh_btn.clicked.connect(self._on_refresh)
        head.addWidget(self.refresh_btn)
        return head

    def _build_pwd_view(self):
        """口令视图：细统计条 + 搜索/筛选/操作行 + 口令表 + 空态。"""
        view = QWidget(self)
        lay = QVBoxLayout(view)
        lay.setContentsMargins(0, 0, 0, 0)
        lay.setSpacing(10)

        # 细统计条：来源 / 命中口径一眼可见（与日志页 stripHint 同语气）
        strip = QHBoxLayout()
        strip.setSpacing(8)
        self.stats_label = QLabel("", self)
        self.stats_label.setObjectName("stripHint")
        strip.addWidget(self.stats_label)
        strip.addStretch(1)
        self.hint_label = QLabel("命中次数来自解压使用记录 · 双击行可复制口令", self)
        self.hint_label.setObjectName("stripHint")
        strip.addWidget(self.hint_label)
        lay.addLayout(strip)

        lay.addLayout(self._build_filters())

        self.table = _PwTable(self)
        self.table.copyRequested.connect(self._copy_row)
        self.table.editRequested.connect(self._edit_row)
        self.table.deleteRowRequested.connect(self._delete_row)
        self.table.deleteKeyPressed.connect(self._delete_selected)
        self.table.headerClicked.connect(self._on_header_clicked)
        self.table.set_sort_indicator(self._sort_col, self._sort_order)
        sm = self.table.selectionModel()
        if sm is not None:
            sm.selectionChanged.connect(self._on_selection_changed)
        lay.addWidget(self.table, 1)
        self.table_empty = _EmptyOverlay(self.table, EMPTY_BOOK)
        return view

    def _build_share_view(self):
        """固定提取码视图：说明 + 计数/操作行 + 提取码表 + 空态。"""
        view = QWidget(self)
        lay = QVBoxLayout(view)
        lay.setContentsMargins(0, 0, 0, 0)
        lay.setSpacing(10)

        guide = QLabel(
            "特殊网盘作者的固定提取码：遇到这些分享者时优先填入提取码；"
            "勾选「需挑选」表示只挑需要的文件下载。", self)
        guide.setObjectName("stripHint")
        guide.setWordWrap(True)
        lay.addWidget(guide)

        bar = QHBoxLayout()
        bar.setSpacing(8)
        self.share_count_lbl = QLabel("", self)
        self.share_count_lbl.setObjectName("stripHint")
        bar.addWidget(self.share_count_lbl)
        bar.addStretch(1)
        self.share_batch_btn = QPushButton("批量编辑（文本）", self)
        self.share_batch_btn.setObjectName("ghostSm")
        self.share_batch_btn.setCursor(Qt.PointingHandCursor)
        self.share_batch_btn.setToolTip("按「分享者UK 提取码 [pick] [#备注]」逐行批量编辑")
        self.share_batch_btn.clicked.connect(self._batch_edit_share)
        bar.addWidget(self.share_batch_btn)
        self.share_add_btn = QPushButton("新增提取码", self)
        self.share_add_btn.setObjectName("primary")
        self.share_add_btn.setCursor(Qt.PointingHandCursor)
        self.share_add_btn.setToolTip("登记一个特殊网盘作者的固定提取码")
        self.share_add_btn.clicked.connect(self._add_share)
        bar.addWidget(self.share_add_btn)
        lay.addLayout(bar)

        self.share_table = _ShareTable(self)
        self.share_table.editRequested.connect(self._edit_share)
        self.share_table.deleteRowRequested.connect(self._delete_share)
        lay.addWidget(self.share_table, 1)
        self.share_empty = _EmptyOverlay(self.share_table, EMPTY_SHARE)
        return view

    def _build_filters(self):
        row = QHBoxLayout()
        row.setSpacing(8)
        self.search = QLineEdit(self)
        self.search.setPlaceholderText("搜索口令 / 来源 / 备注")
        self.search.setFixedWidth(230)
        self.search.textChanged.connect(self._on_search_changed)   # 内存过滤，实时生效
        row.addWidget(self.search)
        self.seg_filter = SegControl(self)
        self.seg_filter.set_items([(label, key) for key, label in _FILTERS])
        self.seg_filter.currentChanged.connect(self._on_filter_changed)
        row.addWidget(self.seg_filter)
        row.addStretch(1)
        # 多选（>=2 行）时出现；0/1 行或空页时隐藏（不是禁用）
        self.sel_copy_btn = QPushButton("复制选中", self)
        self.sel_copy_btn.setObjectName("ghostSm")
        self.sel_copy_btn.setCursor(Qt.PointingHandCursor)
        self.sel_copy_btn.setToolTip("按表格从上到下的顺序复制选中的口令（每行一条）")
        self.sel_copy_btn.clicked.connect(self._copy_selected)
        self.sel_copy_btn.setVisible(False)
        row.addWidget(self.sel_copy_btn)
        self.sel_del_btn = QPushButton("删除选中", self)
        self.sel_del_btn.setObjectName("ghostSm")
        self.sel_del_btn.setCursor(Qt.PointingHandCursor)
        self.sel_del_btn.setToolTip("删除选中的口令（先确认；仅字典收录的行会跳过）")
        self.sel_del_btn.clicked.connect(self._delete_selected)
        self.sel_del_btn.setVisible(False)
        row.addWidget(self.sel_del_btn)
        self.dedup_btn = QPushButton("查重", self)
        self.dedup_btn.setObjectName("ghostSm")
        self.dedup_btn.setCursor(Qt.PointingHandCursor)
        self.dedup_btn.setToolTip("移除重复的长期口令（保留首次出现；备注保留）")
        self.dedup_btn.clicked.connect(self._on_dedup)
        row.addWidget(self.dedup_btn)
        self.add_btn = QPushButton("新增口令", self)
        self.add_btn.setObjectName("primary")
        self.add_btn.setCursor(Qt.PointingHandCursor)
        self.add_btn.clicked.connect(self._on_add)
        row.addWidget(self.add_btn)
        return row

    # ---- 状态读取（宿主 / 测试） ----
    def rows(self):
        """全部行（不受搜索 / 筛选影响）。"""
        return [dict(r) for r in self._rows]

    def visible_rows(self):
        """当前表内可见行。"""
        return self.table.pw_model().rows()

    def keyword(self):
        return self._keyword

    def current_filter(self):
        return self._filter

    def current_mode(self):
        """当前视图模式：pwd（口令）| share（固定提取码）。"""
        return self._mode

    def share_rows(self):
        """固定提取码全部行（不受口令搜索 / 筛选影响）。"""
        return [dict(r) for r in self._share_rows]

    def stats(self):
        """页头口径统计：total / hit / miss / source / book / temp / dict。"""
        out = {"total": len(self._rows), "hit": 0, "miss": 0, "source": 0,
               "book": 0, "temp": 0, "dict": 0}
        for row in self._rows:
            if _hit_count(row) > 0:
                out["hit"] += 1
            else:
                out["miss"] += 1
            if str(row.get("source") or ""):
                out["source"] += 1
            kind = str(row.get("kind") or "")
            if kind in out:
                out[kind] += 1
        return out

    # ---- 装载 ----
    def reload(self, keep_view=False):
        """从数据层重读全部口令与固定提取码并重建行（保留仍可见的选中行）。

        keep_view=True 时额外保持竖向滚动位置（实时刷新用，绝不跳回顶部）。"""
        try:
            rows = self._data.rows()
        except Exception:
            rows = []
        self._rows = [dict(r) for r in rows if isinstance(r, dict)]
        self._apply_filters(keep_scroll=keep_view)
        self._reload_share()
        self._live_signature = self._data_signature()

    def _data_signature(self):
        """当前展示数据的廉价指纹：库内任何会改变表格内容的变化都会改变它。"""
        try:
            rows = self._data.rows()
        except Exception:
            rows = []
        out = []
        for rec in rows:
            if not isinstance(rec, dict):
                continue
            try:
                last = float(rec.get("last_hit") or 0)
            except Exception:
                last = 0.0
            out.append((rec.get("id"), str(rec.get("password") or ""),
                        str(rec.get("source") or ""), _hit_count(rec), last,
                        str(rec.get("note") or ""), str(rec.get("kind") or "")))
        return tuple(out)

    @staticmethod
    def _modal_open():
        """是否有本进程的模态对话框打开（有则实时刷新让路，避免动到弹窗下的数据）。"""
        try:
            return QApplication.activeModalWidget() is not None
        except Exception:
            return False

    def _live_tick(self):
        """实时刷新 tick：数据签名变了才重载（保留选中 / 滚动位置）。"""
        if self._modal_open():
            return
        try:
            sig = self._data_signature()
        except Exception:
            return
        if sig == self._live_signature:
            return
        self._reload_keep_view()

    def _reload_keep_view(self):
        """重载并恢复选中行（按口令）与竖向滚动位置（实时刷新专用）。"""
        passwords = []
        scroll = 0
        try:
            passwords = self.table.selected_passwords()
            scroll = int(self.table.verticalScrollBar().value())
        except Exception:
            pass
        self.reload(keep_view=True)
        try:
            self.table.select_passwords(passwords)
            self.table.verticalScrollBar().setValue(scroll)
        except Exception:
            pass

    def showEvent(self, event):
        """切回本页时立即校验一次数据（停留期间 QTimer 每 2s 校验）。"""
        super().showEvent(event)
        try:
            self._live_timer.start()
            self._live_tick()
        except Exception:
            pass

    def hideEvent(self, event):
        """离开本页时停表（页面不可见就不轮询；回到本页由 showEvent 立即补一次）。"""
        try:
            self._live_timer.stop()
        except Exception:
            pass
        super().hideEvent(event)

    def _reload_share(self):
        """重读固定提取码并重建表（保留空态与模式分段计数）。"""
        try:
            rows = self._share_data.rows()
        except Exception:
            rows = []
        self._share_rows = [dict(r) for r in rows if isinstance(r, dict)]
        self.share_table.set_rows(self._share_rows)
        self.share_count_lbl.setText("共 %d 条" % len(self._share_rows))
        self.share_empty.set_empty(not self._share_rows)
        self._update_mode_labels()

    def _update_mode_labels(self):
        """模式分段计数：口令 N / 固定提取码 N（与列表页分段计数同语气）。"""
        self.seg_mode.set_label("pwd", "口令 %d" % len(self._rows))
        self.seg_mode.set_label("share", "固定提取码 %d" % len(self._share_rows))

    def _sorted_rows(self, rows):
        """按当前列头排序状态对可见行做「仅显示层」重排（绝不改写库内顺序）。

        列头点击只在页面数据层重排本列表：解压尝试顺序只由库内 id 顺序决定。
        排序键：口令（大小写不敏感）/ 来源 / 命中次数（整数）/ 最近命中（原始
        时间戳）/ 备注；「从未命中」在升 / 降序都排最后；并列按口令作稳定兜底。"""
        col = self._sort_col
        order = self._sort_order
        if col not in (_PwModel.COL_PWD, _PwModel.COL_SRC, _PwModel.COL_HITS,
                       _PwModel.COL_LAST, _PwModel.COL_NOTE):
            return list(rows)
        out = sorted(rows, key=lambda r: str(r.get("password") or "").casefold())
        if col == _PwModel.COL_LAST:
            def _ts(rec):
                try:
                    return float(rec.get("last_hit") or 0)
                except Exception:
                    return 0.0

            fresh = [r for r in out if _ts(r) > 0]
            never = [r for r in out if _ts(r) <= 0]
            fresh.sort(key=_ts, reverse=(order == Qt.DescendingOrder))
            return fresh + never
        keys = {
            _PwModel.COL_PWD: lambda r: str(r.get("password") or "").casefold(),
            _PwModel.COL_SRC: lambda r: str(r.get("source") or "—").casefold(),
            _PwModel.COL_HITS: lambda r: _hit_count(r),
            _PwModel.COL_NOTE: lambda r: str(r.get("note") or "—").casefold(),
        }
        key = keys.get(col)
        if key is None:
            return out
        return sorted(out, key=key, reverse=(order == Qt.DescendingOrder))

    def _apply_filters(self, keep_scroll=False):
        base = [r for r in self._rows if _row_matches(r, self._keyword)]
        picked = [r for r in base if _passes_filter(r, self._filter)]
        picked = self._sorted_rows(picked)
        self._update_stats()
        self._update_seg_labels(base)
        keep = self.table.selected_passwords()          # 单选 / 多选都保留（按口令）
        self.table.set_rows(picked, keep_scroll=keep_scroll)
        if keep:
            self.table.select_passwords(keep)
        self.table_empty.set_empty(
            not picked, EMPTY_PWFILTER if self._rows else EMPTY_BOOK)

    def _update_stats(self):
        st = self.stats()
        self.total_label.setText("口令总数 %d" % st["total"])
        self.hit_label.setText("命中过 %d" % st["hit"])
        self.stats_label.setText(
            "长期 %d · 临时 %d · 字典 %d · 有来源 %d"
            % (st["book"], st["temp"], st["dict"], st["source"]))

    def _update_seg_labels(self, base):
        """分段计数按「搜索后的基础集」算（与日志页级别计数同口径）。"""
        counts = {key: 0 for key, _label in _FILTERS}
        counts["all"] = len(base)
        for row in base:
            if _hit_count(row) > 0:
                counts["hit"] += 1
            else:
                counts["miss"] += 1
            if str(row.get("source") or ""):
                counts["src"] += 1
        for key, label in _FILTERS:
            self.seg_filter.set_label(key, "%s %d" % (label, counts[key]))

    def _selected_password(self):
        row = self.table.selected_row()
        return str((row or {}).get("password") or "")

    # ---- 交互 ----
    def _on_mode(self, data):
        """模式分段：切换口令 / 固定提取码视图（不重查数据，重查由 reload 负责）。"""
        key = str(data or "pwd")
        self._mode = key if key in dict(_MODES) else "pwd"
        self.stack.setCurrentIndex(0 if self._mode == "pwd" else 1)

    def _on_search_changed(self, text):
        self._keyword = str(text).strip()
        self._apply_filters()

    def _on_filter_changed(self, data):
        key = str(data or "all")
        self._filter = key if key in dict(_FILTERS) else "all"
        self._apply_filters()

    def _on_header_clicked(self, section):
        """点击列头：按该列排序；再点同一列头切换升 / 降序（纯显示层，不写库）。"""
        col = int(section)
        if col == _PwModel.COL_ACT:
            return
        if col == self._sort_col:
            self._sort_order = (Qt.AscendingOrder
                                if self._sort_order == Qt.DescendingOrder
                                else Qt.DescendingOrder)
        else:
            self._sort_col = col
            # 数字列（命中次数）先看「多」的，文本列先看字母序
            self._sort_order = (Qt.DescendingOrder if col == _PwModel.COL_HITS
                                else Qt.AscendingOrder)
        self.table.set_sort_indicator(self._sort_col, self._sort_order)
        self._apply_filters()

    def _focus_search(self):
        """Ctrl+F：聚焦搜索框并全选（重复按 Ctrl+F 便于直接改关键词）。"""
        try:
            self.search.setFocus(Qt.ShortcutFocusReason)
            self.search.selectAll()
        except Exception:
            pass

    def _on_refresh(self):
        self.reload()
        self.notice.emit("密码本已刷新")

    def _copy_row(self, row_index):
        row = self.table.row_at(row_index) or {}
        password = str(row.get("password") or "")
        if not password:
            return
        try:
            QApplication.clipboard().setText(password)
        except Exception:
            self.notice.emit("复制失败：无法写入剪贴板")
            return
        self._toast_row(row_index, "已复制")
        self.notice.emit("已复制口令到剪贴板")

    def _copy_selected(self):
        """复制全部选中行（视图从上到下的顺序，每行一条）到系统剪贴板。"""
        passwords = self.table.selected_passwords()
        if len(passwords) < 2:
            return
        try:
            QApplication.clipboard().setText("\n".join(passwords))
        except Exception:
            self.notice.emit("复制失败：无法写入剪贴板")
            return
        self.notice.emit("已复制选中的 %d 条口令到剪贴板" % len(passwords))

    def _on_selection_changed(self, *_args):
        """选中数 >=2 时显示「复制选中 / 删除选中」；0/1 行或空页时隐藏。"""
        try:
            n = len(self.table.selected_rows_data())
        except Exception:
            n = 0
        show = n >= 2
        for btn in (getattr(self, "sel_copy_btn", None),
                    getattr(self, "sel_del_btn", None)):
            if btn is not None:
                try:
                    btn.setVisible(show)
                except Exception:
                    pass

    def _toast_row(self, row_index, text):
        """在对应行附近弹提示气泡（失败静默——提示绝不打断复制本身）。"""
        try:
            rect = self.table.visualRect(
                self.table.pw_model().index(int(row_index), _PwModel.COL_PWD))
            pos = self.table.viewport().mapToGlobal(rect.center())
            show_toast(self, pos, str(text))
        except Exception:
            pass

    def _ask_edit(self, title, current, note):
        """弹新增 / 编辑对话框（口令 + 备注）；取消返回 None（口令绝不进日志）。

        真实对话框接受备注；旧测试桩只接受 (parent, title, password) 三参，构造
        失败时退回三参并令 note=None（表示该数据源 / 对话框不支持备注）。
        批量导入（仅新增对话框提供）经返回值的 batch 字段传递，页面统一走
        与单条新增相同的 _PwData.add 入口写入。
        """
        try:
            dlg = _PasswordEditDialog(self, title, current, note)
            has_note = True
        except TypeError:
            dlg = _PasswordEditDialog(self, title, current)
            has_note = False
        if dlg.exec_() != QDialog.Accepted:
            return None
        out = {"password": str(dlg.password() or "").strip()}
        out["note"] = str(dlg.note()).strip() if has_note else None
        out["batch"] = None
        try:
            if has_note and dlg.is_batch():
                values, empty, dup = dlg.batch_values()
                out["batch"] = {"passwords": values, "empty": empty, "dup": dup}
        except Exception:
            out["batch"] = None
        return out

    def _on_add(self):
        result = self._ask_edit("新增口令", "", "")
        if result is None:
            return
        if result.get("batch"):
            self._batch_add(result["batch"], result.get("note") or "")
            return
        password = result["password"]
        if not password:
            return
        if password in self._data.book():
            QMessageBox.information(self, "新增口令", "该口令已在密码本中。")
            return
        if not self._data.add(password, result["note"] or ""):
            self.notice.emit("新增失败：无法写入密码本")
            return
        self.reload()
        self.changed.emit()
        self.notice.emit("已新增口令")

    def _batch_add(self, batch, note):
        """批量导入：逐条走单条新增的同一入口（_PwData.add），最后给出诚实回执。

        依赖 db.add_password 的 INSERT OR IGNORE + UNIQUE(password) 语义：即便
        预检查 / 计数有偏差，重复口令也只会被忽略、绝不覆盖已有行、绝不抛错。"""
        values = [str(p).strip() for p in (batch.get("passwords") or [])
                  if str(p).strip()]
        empty = int(batch.get("empty") or 0)
        dup_batch = int(batch.get("dup") or 0)
        existing = set(self._data.book())
        imported = 0
        dup = dup_batch
        failed = 0
        touched = False
        for p in values:
            if p in existing:
                dup += 1
                continue
            if self._data.add(p, note):
                existing.add(p)
                imported += 1
                touched = True
            else:
                failed += 1
        skipped = dup + empty + failed
        if touched:
            self.reload()
            self.changed.emit()
        self.notice.emit(
            "批量导入完成：新增 %d 条，跳过 %d 条（重复 %d / 空行 %d / 失败 %d）"
            % (imported, skipped, dup, empty, failed))

    def _edit_row(self, row_index):
        row = self.table.row_at(row_index) or {}
        if str(row.get("kind")) != "book":
            self.notice.emit("临时 / 字典口令不支持编辑")
            return
        old = str(row.get("password") or "")
        old_note = str(row.get("note") or "")
        result = self._ask_edit("编辑口令", old, old_note)
        if result is None:
            return
        new = result["password"]
        note = result["note"]                      # None = 该数据源不支持备注
        if not new:
            return
        if new != old and new in self._data.book():
            QMessageBox.information(self, "编辑口令", "该口令已在密码本中。")
            return
        if new == old and (note is None or note == old_note):
            return                                 # 口令与备注都没改
        if not self._data.save(row.get("id"), old, new, note):
            self.notice.emit("编辑失败：无法写入密码本")
            return
        self.reload()
        self.changed.emit()
        self.notice.emit("口令已更新" if new != old else "备注已更新")

    def _remove_temp(self, password):
        """移除一条临时（剪贴板）口令；state 不支持该接口（旧整表桩）时返回 False。"""
        try:
            remover = getattr(self.state, "remove_temp_password", None)
            if remover is None:
                return False
            return bool(remover(password))
        except Exception:
            return False

    def _delete_rows(self, rows):
        """按行 kind 逐行删除，返回 (deleted, skipped)。

        book -> 数据库按 id 精确删除；temp -> state.remove_temp_password（同步落盘
        剪贴板清单）；仅字典收录（dict）的行是派生数据，一律跳过。"""
        deleted = 0
        skipped = 0
        for row in rows or []:
            kind = str((row or {}).get("kind") or "")
            if kind == "book":
                if self._data.remove(row):
                    deleted += 1
                else:
                    skipped += 1
            elif kind == "temp":
                if self._remove_temp(str((row or {}).get("password") or "")):
                    deleted += 1
                else:
                    skipped += 1
            else:
                skipped += 1
        return deleted, skipped

    def _confirm_delete(self, rows):
        """批量删除确认（只说条数与后果，绝不携带明文口令）；确认返回 True。"""
        n_book = sum(1 for r in rows if str(r.get("kind")) == "book")
        n_temp = sum(1 for r in rows if str(r.get("kind")) == "temp")
        detail = []
        if n_book:
            detail.append("长期口令 %d 条（解压将不再尝试）" % n_book)
        if n_temp:
            detail.append("临时口令 %d 条（剪贴板捕获记录）" % n_temp)
        text = ("确定删除选中的 %d 条口令吗？\n%s\n"
                "删除只影响密码本 / 剪贴板记录，不会删除任何已解压的文件。"
                % (len(rows), " · ".join(detail)))
        answer = QMessageBox.question(
            self, "删除口令", text,
            QMessageBox.Yes | QMessageBox.No, QMessageBox.No)
        return answer == QMessageBox.Yes

    def _delete_row(self, row_index):
        """行内「删除」按钮：book 先二次确认；temp 是剪贴板临时记录，直接移除；
        dict 行（仅解压命中收录）拒绝删除。"""
        row = self.table.row_at(row_index) or {}
        kind = str(row.get("kind") or "")
        if kind == "book":
            answer = QMessageBox.question(
                self, "删除口令",
                "确定从密码本删除选中的口令吗？\n"
                "删除后解压将不再尝试该口令（不会删除任何已解压的文件）。",
                QMessageBox.Yes | QMessageBox.No, QMessageBox.No)
            if answer != QMessageBox.Yes:
                return
            deleted, _skipped = self._delete_rows([row])
            if deleted:
                self.reload()
                self.changed.emit()
                self.notice.emit("已删除口令")
            else:
                self.notice.emit("删除失败：无法写入密码本")
            return
        if kind == "temp":
            deleted, _skipped = self._delete_rows([row])
            if deleted:
                self.reload()
                self.changed.emit()
                self.notice.emit("已移除临时口令")
            else:
                self.notice.emit("删除失败：无法移除临时口令")
            return
        self.notice.emit("字典口令来自解压命中记录，不支持删除")

    def _delete_selected(self):
        """删除全部选中行（Delete 键与「删除选中」共用）：先确认，再逐行删除。

        长期行按 id 删、临时行从剪贴板记录移除；仅字典收录的行跳过并如实回执。"""
        rows = self.table.selected_rows_data()
        if not rows:
            self.notice.emit("请先选择要删除的口令")
            return
        deletable = [r for r in rows
                     if str(r.get("kind") or "") in ("book", "temp")]
        if not deletable:
            self.notice.emit("字典口令来自解压命中记录，不支持删除")
            return
        if not self._confirm_delete(deletable):
            return
        dict_skipped = len(rows) - len(deletable)
        deleted, skipped = self._delete_rows(deletable)
        skipped += dict_skipped
        if deleted:
            self.reload()
            self.changed.emit()
        if deleted and skipped:
            self.notice.emit("已删除 %d 条口令，跳过 %d 条（字典 / 无法写入）"
                             % (deleted, skipped))
        elif deleted:
            self.notice.emit("已删除 %d 条口令" % deleted)
        else:
            self.notice.emit("删除失败：无法写入密码本")

    # ---- 口令整理：查重（排序已改为点列头的纯视图重排，见 _on_header_clicked） ----
    def _on_dedup(self):
        """移除重复的长期口令（保留首次出现）。"""
        removed = self._data.dedup_book()
        if removed < 0:
            self.notice.emit("查重失败：无法写入密码本")
            return
        if removed == 0:
            self.notice.emit("未发现重复口令")
            return
        self.reload()
        self.changed.emit()
        self.notice.emit("已移除重复口令 %d 条" % removed)

    # ---- 固定提取码：行级增 / 改 / 删 + 批量文本编辑 ----
    def _ask_share_edit(self, title, share_uk, code, note, pick):
        """弹新增 / 编辑固定提取码对话框（UK + 提取码 + 需挑选 + 备注）；取消返回 None。

        提取码 / UK 绝不进入日志或回执——只在这两个对话框控件里出现。
        """
        try:
            dlg = _ShareEditDialog(self, title, share_uk, code, note, pick)
        except Exception:
            self.notice.emit("打开编辑框失败")
            return None
        if dlg.exec_() != QDialog.Accepted:
            return None
        return dlg.values()

    def _add_share(self):
        result = self._ask_share_edit("新增提取码", "", "", "", 0)
        if result is None:
            return
        uk, code, note, pick = result
        if not uk or not code:
            return
        if self._share_data.find(uk) is not None:
            QMessageBox.information(
                self, "新增提取码", "该分享者 UK 已有固定提取码，请改用「编辑」。")
            return
        if not self._share_data.add(uk, code, note, pick):
            self.notice.emit("新增失败：无法写入固定提取码")
            return
        self._reload_share()
        self.notice.emit("已新增固定提取码")

    def _edit_share(self, row_index):
        row = self.share_table.row_at(row_index) or {}
        old_uk = str(row.get("share_uk") or "")
        old_code = str(row.get("code") or "")
        old_note = str(row.get("note") or "")
        old_pick = 1 if _pick_on(row.get("pick")) else 0
        if not old_uk:
            return
        result = self._ask_share_edit("编辑提取码", old_uk, old_code, old_note, old_pick)
        if result is None:
            return
        uk, code, note, pick = result
        if not uk or not code:
            return
        if (uk == old_uk and code == old_code and note == old_note
                and pick == old_pick):
            return                                 # 什么都没改
        if uk != old_uk and self._share_data.find(uk) is not None:
            QMessageBox.information(self, "编辑提取码", "该分享者 UK 已有固定提取码。")
            return
        if not self._share_data.update(old_uk, new_share_uk=uk, code=code,
                                       note=note, pick=pick):
            self.notice.emit("保存失败：无法写入固定提取码")
            return
        self._reload_share()
        self.notice.emit("固定提取码已更新")

    def _delete_share(self, row_index):
        row = self.share_table.row_at(row_index) or {}
        uk = str(row.get("share_uk") or "")
        if not uk:
            return
        answer = QMessageBox.question(
            self, "删除固定提取码",
            "确定删除这条固定提取码吗？\n"
            "删除后遇到该分享者将不再自动填入提取码（不会删除任何文件）。",
            QMessageBox.Yes | QMessageBox.No, QMessageBox.No)
        if answer != QMessageBox.Yes:
            return
        if self._share_data.remove(uk):
            self._reload_share()
            self.notice.emit("已删除固定提取码")
        else:
            self.notice.emit("删除失败：无法写入固定提取码")

    def _batch_edit_share(self):
        """批量编辑（文本）：复用既有行格式的解析 / 格式化，保存走整表覆盖。"""
        try:
            dlg = _ShareTextDialog(self, self._share_rows)
        except Exception:
            self.notice.emit("打开批量编辑失败")
            return
        if dlg.exec_() != QDialog.Accepted:
            return
        if not self._share_data.replace_all(dlg.items()):
            self.notice.emit("保存失败：无法写入固定提取码")
            return
        self._reload_share()
        self.notice.emit("固定提取码已保存")

    def refresh_theme(self):
        """主题切换后重贴内联色（两张表的行内删除按钮 danger 色来自 PALETTE）。"""
        for table in (self.table, self.share_table):
            try:
                table.refresh_theme()
            except Exception:
                pass
