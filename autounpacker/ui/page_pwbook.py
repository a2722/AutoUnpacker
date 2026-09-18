# -*- coding: utf-8 -*-
"""密码本页（M4）：口令表 + 命中统计 + 搜索 / 筛选 + 复制 / 新增 / 编辑 / 删除。

职责：
- PasswordBookPage：页头（「密码本」+ 口令总数 + 命中过 + 细统计条）、搜索（实时过滤）、
  四筛选分段（全部 / 命中过 / 未命中 / 有来源）、口令表（口令 / 来源 / 命中次数 /
  最近命中 / 备注 + 行内 复制 / 编辑 / 删除）、空态、Ctrl+F 聚焦搜索、
  Delete 删除选中行（二次确认）、刷新按钮
- 行数据 = 「长期密码本 ∪ 本次临时密码 ∪ 解压字典」按口令合并去重（长期在前；
  字典只补缺），来源列区分：手动（长期）/ 剪贴板（临时）/ —（仅字典收录，无主动来源）
- _PwBookStore：页面私有的数据访问层，只用现有公开 API（state.passwords /
  set_passwords / add_long_password / temp_passwords、db.load_password_dict），
  绝不直接读写 SQLite 文件（测试可对这些入口打桩）
关键入口：PasswordBookPage / EMPTY_BOOK / EMPTY_PWFILTER
依赖：PyQt5、db、style（PALETTE）、widgets（Glyph / SegControl / show_toast）
注意：口令属隐私数据——本模块不把明文写进任何日志、提示或掩码态 tooltip；复制是
      用户显式动作（只写系统剪贴板）；删除只动密码本条目，绝不触碰任何文件。
注意：共享数据 API 缺口（交付报告已记录）：password_book.py 只有弹窗与文本解析器，
      没有行级 list/add/update/delete/stats；passwords 表也没有 note 列。本页因此
      在现有 API 上合成行：编辑 / 删除经 set_passwords 整体重写，备注列暂以「—」占位。
注意：表格复用 #taskTable 的既有 QSS（style.py 属共享文件、本次禁改，不能新增
      对象名样式）；自绘取色一律走 PALETTE / widgets 的 token 机制。
"""
# allow: SIZE_OK — 交付约束只允许新建本模块与测试两个文件；本模块 = 页面 + 私有模型 /
# 表格 / 编辑对话框 / 数据门面（常规应拆 3~4 个文件，此处按单文件约束合并在一个页面单元）。
import time

from PyQt5.QtCore import QAbstractTableModel, QEvent, Qt, pyqtSignal
from PyQt5.QtGui import QFont, QKeySequence
from PyQt5.QtWidgets import (QAbstractItemView, QApplication, QCheckBox, QDialog,
                             QHBoxLayout, QHeaderView, QLabel, QLineEdit,
                             QMessageBox, QPushButton, QShortcut,
                             QStyledItemDelegate, QStyleOptionViewItem, QTableView,
                             QVBoxLayout, QWidget)

from .. import db
from .style import PALETTE
from .widgets import Glyph, SegControl, show_toast

# 空态文案（与 pages.py 的空态语气一致）
EMPTY_BOOK = "密码本还是空的 · 点「新增口令」，或让剪贴板 / 二维码自动收录提取码"
EMPTY_PWFILTER = "没有符合当前搜索 / 筛选条件的口令"

# 屏幕掩码文本：固定位数，不泄露口令长度
_MASK_TEXT = "••••••••"

# 来源文案：只描述「这行从哪来」，不猜 db 内部 source 取值
_SOURCE_BOOK = "手动"
_SOURCE_TEMP = "剪贴板"

# 筛选分段（key, 文案）——计数在运行期刷新
_FILTERS = (("all", "全部"), ("hit", "命中过"), ("miss", "未命中"), ("src", "有来源"))


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


def _make_row(password, source, kind, hits):
    """合成一行展示数据：来源 / 命中次数 / 最近命中来自现有两个公开数据源。"""
    meta = hits.get(password) if isinstance(hits, dict) else None
    try:
        count = max(0, int((meta or {}).get("used_count") or 0))
    except Exception:
        count = 0
    try:
        last = float((meta or {}).get("last_used_at") or 0)
    except Exception:
        last = 0.0
    return {"password": str(password), "source": str(source), "hit_count": count,
            "last_hit": last, "note": "", "kind": str(kind)}


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
# 数据访问层（私有）：把现有公开 API 合成页面需要的行
# ---------------------------------------------------------------------------

class _PwBookStore:
    """密码本页的数据门面：state 优先，缺省直连 db 模块的公开函数。

    只用公开 API；任何异常都退化为「空/失败」，绝不让页面因数据层抖动而崩。
    编辑 / 删除没有行级 API，统一读全量 -> 改一份 -> set_passwords 覆盖写回。
    """

    def __init__(self, state=None):
        self._state = state

    def book(self):
        """长期密码本口令（保持库内顺序 = 解压尝试顺序）。"""
        try:
            if self._state is not None:
                items = self._state.passwords()
            else:
                items = db.get_passwords()
        except Exception:
            return []
        return [str(p) for p in (items or []) if str(p)]

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
        for p in self.book():
            if p not in seen:
                seen.add(p)
                rows.append(_make_row(p, _SOURCE_BOOK, "book", hits))
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

    def add(self, password):
        """新增一条长期口令（已存在时由调用方先行拦截）；失败返回 False。"""
        try:
            if self._state is not None:
                self._state.add_long_password(password)
            else:
                db.add_password(password, source="manual")
            return True
        except Exception:
            return False

    def replace(self, passwords):
        """整体覆盖长期密码本（编辑 / 删除的落点）；失败返回 False。"""
        try:
            if self._state is not None:
                self._state.set_passwords(list(passwords))
            else:
                db.set_passwords(list(passwords))
            return True
        except Exception:
            return False

    def rename(self, old, new):
        """把一条口令改名（保持位置 = 保持尝试顺序）；未找到返回 False。"""
        items = self.book()
        try:
            idx = items.index(str(old))
        except ValueError:
            return False
        items[idx] = str(new)
        return self.replace(items)

    def remove(self, password):
        """删除一条长期口令；未找到返回 False。"""
        items = self.book()
        if str(password) not in items:
            return False
        return self.replace([p for p in items if p != str(password)])


# ---------------------------------------------------------------------------
# 口令表：模型 / 委托 / 视图
# ---------------------------------------------------------------------------

class _PwModel(QAbstractTableModel):
    """口令表数据模型：列 口令 / 来源 / 命中次数 / 最近命中 / 备注 / 行操作。

    口令默认掩码（固定位数）；UserRole = 口令（行身份），ToolTip 在掩码态绝不泄露明文。
    """

    HEADERS = ("口令", "来源", "命中次数", "最近命中", "备注", "")
    (COL_PWD, COL_SRC, COL_HITS, COL_LAST, COL_NOTE, COL_ACT) = range(6)

    def __init__(self, parent=None):
        super().__init__(parent)
        self._rows = []
        self._masked = True

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

    def masked(self):
        return self._masked

    def set_masked(self, masked):
        """显隐全表口令（只重绘口令列，不动行数据）。"""
        flag = bool(masked)
        if flag == self._masked:
            return
        self._masked = flag
        if self._rows:
            self.dataChanged.emit(
                self.index(0, self.COL_PWD),
                self.index(len(self._rows) - 1, self.COL_PWD),
                [Qt.DisplayRole, Qt.ToolTipRole])

    def rowCount(self, parent=None):
        if parent is not None and parent.isValid():
            return 0
        return len(self._rows)

    def columnCount(self, parent=None):
        if parent is not None and parent.isValid():
            return 0
        return len(self.HEADERS)

    def headerData(self, section, orientation, role=Qt.DisplayRole):
        if orientation != Qt.Horizontal or role != Qt.DisplayRole:
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
            return _MASK_TEXT if self._masked else str(row.get("password") or "")
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
                if self._masked:
                    return "口令已隐藏 · 用「显示口令」查看，或直接复制"
                return None
            if col == self.COL_SRC:
                return "来源：%s" % (row.get("source") or "仅解压字典收录（无主动来源）")
            if col == self.COL_HITS:
                return "解压时命中次数：%d" % _hit_count(row)
            if col == self.COL_LAST:
                return "%s（最近一次命中）" % _fmt_hit_time(row.get("last_hit"))
            if col == self.COL_NOTE:
                return "备注暂不可编辑（数据层暂无备注列）"
            return None
        if role == Qt.TextAlignmentRole and col == self.COL_HITS:
            return int(Qt.AlignRight | Qt.AlignVCenter)
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
        super().paint(painter, opt, index)


class _PwTable(QTableView):
    """口令表：掩码列 + 行内 复制 / 编辑 / 删除；Delete 键发 deleteKeyPressed。"""

    copyRequested = pyqtSignal(int)
    editRequested = pyqtSignal(int)
    deleteRowRequested = pyqtSignal(int)
    deleteKeyPressed = pyqtSignal()

    def __init__(self, parent=None):
        super().__init__(parent)
        self.setObjectName("taskTable")    # 复用既有表格 QSS（style.py 禁改）
        self._model = _PwModel(self)
        self.setModel(self._model)
        self.setItemDelegate(_PwDelegate(self))
        self.setSelectionBehavior(QAbstractItemView.SelectRows)
        self.setSelectionMode(QAbstractItemView.SingleSelection)
        self.setEditTriggers(QAbstractItemView.NoEditTriggers)
        self.setAlternatingRowColors(True)
        self.setShowGrid(False)
        self.setWordWrap(False)
        self.setTextElideMode(Qt.ElideMiddle)
        self.setMinimumHeight(160)
        self.verticalHeader().setVisible(False)
        self.verticalHeader().setDefaultSectionSize(44)
        hh = self.horizontalHeader()
        hh.setHighlightSections(False)
        hh.setStretchLastSection(False)
        hh.setSectionResizeMode(_PwModel.COL_PWD, QHeaderView.Stretch)
        hh.setSectionResizeMode(_PwModel.COL_NOTE, QHeaderView.Stretch)
        for col, width in ((_PwModel.COL_SRC, 88), (_PwModel.COL_HITS, 84),
                           (_PwModel.COL_LAST, 124), (_PwModel.COL_ACT, 158)):
            hh.setSectionResizeMode(col, QHeaderView.Fixed)
            self.setColumnWidth(col, width)
        self._action_widgets = []
        self._danger_buttons = []
        self.doubleClicked.connect(self._on_double_clicked)

    # ---- 装载 ----
    def set_rows(self, rows):
        self._clear_actions()
        self._model.set_rows(rows)
        self._build_actions()
        self.scroll_to_top()

    def pw_model(self):
        return self._model

    def row_at(self, row):
        return self._model.row_at(row)

    def select_password(self, password):
        """按口令选中行（重载 / 过滤后保持选中用），返回是否命中。"""
        for row in range(self._model.rowCount()):
            data = self._model.row_at(row) or {}
            if str(data.get("password")) == str(password):
                self.selectRow(row)
                return True
        return False

    def selected_row(self):
        """当前选中行数据（无选中返回 None）。"""
        try:
            rows = self.selectionModel().selectedRows()
        except Exception:
            rows = []
        if not rows:
            return None
        return self._model.row_at(rows[0].row())

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
        can_edit = str(data.get("kind")) == "book"
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
        del_btn = self._small_button(
            "删除", "从密码本删除这条口令" if can_edit else "临时 / 字典口令不支持删除",
            danger=True)
        del_btn.setEnabled(can_edit)
        if can_edit:
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
# 新增 / 编辑口令小对话框
# ---------------------------------------------------------------------------

class _PasswordEditDialog(QDialog):
    """新增 / 编辑单条口令：输入框默认掩码，可勾「显示口令」切明文；空口令不可保存。"""

    def __init__(self, parent=None, title="新增口令", password=""):
        super().__init__(parent)
        self.setWindowTitle(str(title))
        self.setMinimumWidth(380)
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
        self.edit.setEchoMode(QLineEdit.Password)
        self.edit.setPlaceholderText("输入口令")
        self.edit.setText(str(password or ""))
        lay.addWidget(self.edit)
        self.show_cb = QCheckBox("显示口令", self)
        self.show_cb.toggled.connect(self._on_show_toggled)
        lay.addWidget(self.show_cb)
        btns = QHBoxLayout()
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
        self.edit.returnPressed.connect(self._on_return)
        self.edit.setFocus()

    def _on_show_toggled(self, checked):
        self.edit.setEchoMode(QLineEdit.Normal if checked else QLineEdit.Password)

    def _sync_save(self, _text):
        self.save_btn.setEnabled(bool(self.edit.text().strip()))

    def _on_return(self):
        if self.save_btn.isEnabled():
            self.accept()

    def password(self):
        return str(self.edit.text()).strip()


# ---------------------------------------------------------------------------
# 密码本页
# ---------------------------------------------------------------------------

class PasswordBookPage(QWidget):
    """密码本页：搜索 + 四筛选 + 口令表 + 行内操作；数据经 _PwBookStore 读取。

    信号：notice(text)（复制/新增/编辑/删除等用户可见回执，绝不携带明文口令）、
          changed()（口令本发生写操作后发出，宿主可据此刷新标签徽标）。
    宿主接缝：reload()（切回本页时重查）/ refresh_theme()（主题切换后重贴内联色）。
    """

    notice = pyqtSignal(str)
    changed = pyqtSignal()

    def __init__(self, state=None, parent=None):
        super().__init__(parent)
        self.state = state
        self._store = _PwBookStore(state)
        self._rows = []
        self._keyword = ""
        self._filter = "all"

        root = QVBoxLayout(self)
        root.setContentsMargins(12, 12, 12, 10)
        root.setSpacing(10)

        root.addLayout(self._build_header())

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
        root.addLayout(strip)

        root.addLayout(self._build_filters())

        self.table = _PwTable(self)
        self.table.copyRequested.connect(self._copy_row)
        self.table.editRequested.connect(self._edit_row)
        self.table.deleteRowRequested.connect(self._delete_row)
        self.table.deleteKeyPressed.connect(self._delete_selected)
        root.addWidget(self.table, 1)
        self.table_empty = _EmptyOverlay(self.table, EMPTY_BOOK)

        # Ctrl+F：页内任意子控件有焦点时聚焦搜索框（页不可见时不抢焦点）
        self._sc_search = QShortcut(QKeySequence("Ctrl+F"), self)
        self._sc_search.setContext(Qt.WidgetWithChildrenShortcut)
        self._sc_search.activated.connect(self._focus_search)

        self.reload()

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
        self.reveal_btn = QPushButton("显示口令", self)
        self.reveal_btn.setObjectName("ghostSm")
        self.reveal_btn.setCursor(Qt.PointingHandCursor)
        self.reveal_btn.setCheckable(True)
        self.reveal_btn.setToolTip("在掩码与明文之间切换口令列的显示")
        self.reveal_btn.toggled.connect(self._on_reveal_toggled)
        head.addWidget(self.reveal_btn)
        self.refresh_btn = _icon_button("refresh", "刷新", self)
        self.refresh_btn.clicked.connect(self._on_refresh)
        head.addWidget(self.refresh_btn)
        self.add_btn = QPushButton("新增口令", self)
        self.add_btn.setObjectName("primary")
        self.add_btn.setCursor(Qt.PointingHandCursor)
        self.add_btn.clicked.connect(self._on_add)
        head.addWidget(self.add_btn)
        return head

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
    def reload(self):
        """从数据层重读全部口令并重建行（保留仍可见的选中行）。"""
        try:
            rows = self._store.rows()
        except Exception:
            rows = []
        self._rows = [dict(r) for r in rows if isinstance(r, dict)]
        self._apply_filters()

    def _apply_filters(self):
        base = [r for r in self._rows if _row_matches(r, self._keyword)]
        picked = [r for r in base if _passes_filter(r, self._filter)]
        self._update_stats()
        self._update_seg_labels(base)
        keep = self._selected_password()
        self.table.set_rows(picked)
        if keep:
            self.table.select_password(keep)
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
    def _on_search_changed(self, text):
        self._keyword = str(text).strip()
        self._apply_filters()

    def _on_filter_changed(self, data):
        key = str(data or "all")
        self._filter = key if key in dict(_FILTERS) else "all"
        self._apply_filters()

    def _on_reveal_toggled(self, checked):
        self.table.pw_model().set_masked(not checked)
        self.reveal_btn.setText("隐藏口令" if checked else "显示口令")

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

    def _toast_row(self, row_index, text):
        """在对应行附近弹提示气泡（失败静默——提示绝不打断复制本身）。"""
        try:
            rect = self.table.visualRect(
                self.table.pw_model().index(int(row_index), _PwModel.COL_PWD))
            pos = self.table.viewport().mapToGlobal(rect.center())
            show_toast(self, pos, str(text))
        except Exception:
            pass

    def _ask_password(self, title, current):
        """弹新增 / 编辑对话框；取消返回 None（口令绝不进日志）。"""
        dlg = _PasswordEditDialog(self, title, current)
        if dlg.exec_() != QDialog.Accepted:
            return None
        return str(dlg.password() or "").strip()

    def _on_add(self):
        password = self._ask_password("新增口令", "")
        if not password:
            return
        if password in self._store.book():
            QMessageBox.information(self, "新增口令", "该口令已在密码本中。")
            return
        if not self._store.add(password):
            self.notice.emit("新增失败：无法写入密码本")
            return
        self.reload()
        self.changed.emit()
        self.notice.emit("已新增口令")

    def _edit_row(self, row_index):
        row = self.table.row_at(row_index) or {}
        if str(row.get("kind")) != "book":
            self.notice.emit("临时 / 字典口令不支持编辑")
            return
        old = str(row.get("password") or "")
        new = self._ask_password("编辑口令", old)
        if not new or new == old:
            return
        if new in self._store.book():
            QMessageBox.information(self, "编辑口令", "该口令已在密码本中。")
            return
        if not self._store.rename(old, new):
            self.notice.emit("编辑失败：无法写入密码本")
            return
        self.reload()
        self.changed.emit()
        self.notice.emit("口令已更新")

    def _delete_row(self, row_index):
        row = self.table.row_at(row_index) or {}
        if str(row.get("kind")) != "book":
            self.notice.emit("临时 / 字典口令不支持删除")
            return
        answer = QMessageBox.question(
            self, "删除口令",
            "确定从密码本删除选中的口令吗？\n"
            "删除后解压将不再尝试该口令（不会删除任何已解压的文件）。",
            QMessageBox.Yes | QMessageBox.No, QMessageBox.No)
        if answer != QMessageBox.Yes:
            return
        if self._store.remove(str(row.get("password") or "")):
            self.reload()
            self.changed.emit()
            self.notice.emit("已删除口令")
        else:
            self.notice.emit("删除失败：无法写入密码本")

    def _delete_selected(self):
        row = self.table.selected_row()
        if row is None:
            self.notice.emit("请先选择要删除的口令")
            return
        self._delete_row(self.table.selectionModel().selectedRows()[0].row())

    def refresh_theme(self):
        """主题切换后重贴内联色（行内删除按钮的 danger 色来自 PALETTE）。"""
        try:
            self.table.refresh_theme()
        except Exception:
            pass
