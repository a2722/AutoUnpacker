# -*- coding: utf-8 -*-
"""口令表的私有视图组件：空态覆盖层、图标按钮、等宽委托、
自绘排序指示器列头与表本体（阶段6e 自 ui/page_pwbook.py 纯搬移）。"""
from PyQt5.QtCore import QEvent, QItemSelectionModel, Qt, pyqtSignal
from PyQt5.QtGui import QColor, QFont, QFontMetrics, QPainter, QPainterPath
from PyQt5.QtWidgets import (QAbstractItemView, QHBoxLayout, QHeaderView,
                             QLabel, QPushButton, QStyledItemDelegate,
                             QStyleOptionViewItem, QTableView, QWidget)

from ..style import PALETTE, tokens
from ..widgets import Glyph

from .models import _PwModel


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


# 动作列宽 = 行内控件 sizeHint + 本余量。14px = 单元格左右内缩 12px（网格线 + 项边距）
# + 2px 安全余量；实测（离屏、真实样式表）4 字按钮「设为永久」在列宽 186px 时完整显示、
# 182px 时仍被中间截断成「为永」，故用 sizeHint + 14 作下限。
_ACTION_COL_INSET = 14


def _fit_action_column(table, col):
    """把动作列宽调到「最宽那一行」的实际需求（只增不减）。

    临时行的「设为永久」比两字按钮宽：固定 158px 时该按钮会被挤到只露出中段字
    （显示成「为永」）。这里按各行行内控件的 sizeHint 取最大值再留出单元格内缩
    ——sizeHint 已包含 QSS padding 与当前字体，所以换主题 / 换字号会自动跟随；
    异常一律保留原宽度。
    """
    try:
        cur = int(table.columnWidth(int(col)))
        need = cur
        for w in table._action_widgets:
            try:
                need = max(need, int(w.sizeHint().width()) + _ACTION_COL_INSET)
            except Exception:
                continue
        if need > cur:
            table.setColumnWidth(int(col), need)
    except Exception:
        pass


class _PwTable(QTableView):
    """口令表：掩码列 + 行内 复制 / 编辑 / 删除；Delete 键发 deleteKeyPressed。

    长期行行内为 复制 / 编辑 / 删除；临时（剪贴板）行编辑本就不可用，改为
    「设为永久」（promoteRequested）——不再放出点不动的禁用按钮；字典（派生）
    行只留 复制 / 删除（编辑同样不放出来）。
    双击备注列发 editNoteRequested（页面打开编辑框并把焦点锁进备注栏），
    双击其余列仍是复制（copyRequested）。
    支持 ExtendedSelection（Shift / Ctrl 多选）；点列头发 headerClicked（页面做
    仅显示层的排序，动作列除外）；列头指示器只做视觉提示，Qt 自身排序保持关闭。
    """

    copyRequested = pyqtSignal(int)
    editRequested = pyqtSignal(int)
    editNoteRequested = pyqtSignal(int)
    promoteRequested = pyqtSignal(int)
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
        _fit_action_column(self, _PwModel.COL_ACT)

    def _make_actions(self, row_index, data):
        kind = str(data.get("kind"))
        is_temp = kind == "temp"
        can_edit = kind == "book"
        can_delete = kind in ("book", "temp", "dict")
        if can_edit:
            del_tip = "从密码本删除这条口令"
        elif is_temp:
            del_tip = "移除这条临时口令（剪贴板捕获）"
        else:
            del_tip = "从密码字典删除这条记录（不影响密码本）"
        w = QWidget(self)
        lay = QHBoxLayout(w)
        lay.setContentsMargins(0, 0, 6, 0)
        lay.setSpacing(4)
        lay.addStretch(1)
        copy_btn = self._small_button("复制", "复制口令到剪贴板")
        copy_btn.clicked.connect(
            lambda _=False, r=row_index: self.copyRequested.emit(int(r)))
        lay.addWidget(copy_btn)
        if is_temp:
            # 临时（剪贴板）口令不支持编辑，按钮改为「设为永久」：把该口令加入
            # 长期密码本并移出临时记录（复用剪贴板自动收录的同一后端入口）。
            promote_btn = self._small_button(
                "设为永久", "把这条临时口令加入长期密码本（并移出临时记录）")
            promote_btn.clicked.connect(
                lambda _=False, r=row_index: self.promoteRequested.emit(int(r)))
            lay.addWidget(promote_btn)
        elif can_edit:
            # 只有长期（密码本）口令可编辑。字典行是解压命中记录派生出来的、
            # 本就改不了，不再放一个点不动的灰「编辑」按钮（只留 复制 / 删除）。
            edit_btn = self._small_button("编辑", "编辑这条口令")
            edit_btn.clicked.connect(
                lambda _=False, r=row_index: self.editRequested.emit(int(r)))
            lay.addWidget(edit_btn)
        del_btn = self._small_button("删除", del_tip, danger=True)
        del_btn.setEnabled(can_delete)
        if can_delete:
            del_btn.clicked.connect(
                lambda _=False, r=row_index: self.deleteRowRequested.emit(int(r)))
        lay.addWidget(del_btn)
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
        """双击备注列 -> 编辑该行（焦点锁进备注栏）；双击其余列仍是复制。"""
        if not index.isValid():
            return
        if index.column() == _PwModel.COL_NOTE:
            self.editNoteRequested.emit(int(index.row()))
            return
        self.copyRequested.emit(int(index.row()))

    def keyPressEvent(self, event):
        if event.key() == Qt.Key_Delete:
            self.deleteKeyPressed.emit()
            event.accept()
            return
        super().keyPressEvent(event)
