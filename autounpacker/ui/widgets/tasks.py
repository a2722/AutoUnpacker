# -*- coding: utf-8 -*-
"""任务表：TaskModel（FINAL-SPEC §2.3 列定义）+ TaskTable + 状态胶囊委托。"""

from PyQt5.QtWidgets import (QTableView, QHeaderView, QAbstractItemView,
                             QStyledItemDelegate, QStyleOptionViewItem,
                             QStyle, QWidget, QHBoxLayout, QPushButton)
from PyQt5.QtCore import (Qt, QRectF, pyqtSignal, QAbstractTableModel,
                          QModelIndex)
from PyQt5.QtGui import QPainter, QBrush, QPen, QPalette, QFont

from ..style import PALETTE
from .common import (_task_state_key, _task_rows_signature, _row_file,
                     _row_out, _fmt_size, _fmt_cost, _fmt_pwd, _qcolor,
                     _tk, _pill_colors)
from .inputs import Glyph
# ---- 任务表 ----
_STATE_TEXT = {
    "queued": "排队", "extracting": "解压中", "need_password": "待密码",
    "done": "已完成", "failed": "失败", "canceled": "已取消",
}


class TaskModel(QAbstractTableModel):
    """任务表数据模型：列见 FINAL-SPEC §2.3；UserRole=task id，ToolTip=完整文件名/输出路径。"""

    HEADERS = ("状态", "文件", "大小", "密码", "耗时", "输出去向", "")
    (COL_STATE, COL_FILE, COL_SIZE, COL_PWD, COL_COST, COL_OUT, COL_ACT) = range(7)
    STATE_ROLE = Qt.UserRole + 1

    # 每列内容对齐：单元格与表头文字共用同一口径（表头文字才能与单元格文字对齐）
    _ALIGN = {
        COL_STATE: Qt.AlignLeft | Qt.AlignVCenter,
        COL_FILE: Qt.AlignLeft | Qt.AlignVCenter,
        COL_SIZE: Qt.AlignRight | Qt.AlignVCenter,
        COL_PWD: Qt.AlignLeft | Qt.AlignVCenter,
        COL_COST: Qt.AlignRight | Qt.AlignVCenter,
        COL_OUT: Qt.AlignLeft | Qt.AlignVCenter,
        COL_ACT: Qt.AlignLeft | Qt.AlignVCenter,
    }

    def __init__(self, parent=None):
        super().__init__(parent)
        self._rows = []

    def set_tasks(self, rows):
        self.beginResetModel()
        self._rows = [dict(r) for r in (rows or []) if isinstance(r, dict)]
        self.endResetModel()

    def tasks(self):
        return list(self._rows)

    def task_at(self, row):
        try:
            i = int(row)
        except Exception:
            return None
        if 0 <= i < len(self._rows):
            return dict(self._rows[i])
        return None

    def rowCount(self, parent=QModelIndex()):
        if parent.isValid():
            return 0
        return len(self._rows)

    def columnCount(self, parent=QModelIndex()):
        if parent.isValid():
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
        if col == self.COL_STATE:
            key = _task_state_key(row)
            return str(row.get("state_text") or _STATE_TEXT.get(key, key or ""))
        if col == self.COL_FILE:
            return _row_file(row)
        if col == self.COL_SIZE:
            return str(row.get("size") or _fmt_size(row.get("file_size")))
        if col == self.COL_PWD:
            return str(row.get("pwd") or _fmt_pwd(row))
        if col == self.COL_COST:
            return str(row.get("cost") or _fmt_cost(row))
        if col == self.COL_OUT:
            return _row_out(row) or "—"
        return ""

    def data(self, index, role=Qt.DisplayRole):
        if not index.isValid() or not (0 <= index.row() < len(self._rows)):
            return None
        row = self._rows[index.row()]
        col = index.column()
        if role == Qt.UserRole:
            return row.get("id")
        if role == self.STATE_ROLE:
            return _task_state_key(row)
        if role == Qt.DisplayRole:
            return self._display(row, col)
        if role == Qt.ToolTipRole:
            if col == self.COL_FILE:
                return _row_file(row)
            if col == self.COL_OUT:
                return _row_out(row) or "—"
            if col == self.COL_STATE:
                return self._display(row, col)
            return None
        if role == Qt.TextAlignmentRole:
            return int(self._ALIGN.get(col, Qt.AlignLeft | Qt.AlignVCenter))
        return None


class _StatePillDelegate(QStyledItemDelegate):
    """任务表：状态列圆角胶囊自绘；文件/输出去向列等宽字体（输出去向未选中时 muted 灰）。"""

    def paint(self, painter, option, index):
        col = index.column()
        if col in (TaskModel.COL_FILE, TaskModel.COL_OUT):
            opt = QStyleOptionViewItem(option)
            f = QFont(opt.font)
            f.setFamily("Consolas")
            f.setStyleHint(QFont.Monospace)
            opt.font = f
            if col == TaskModel.COL_OUT and not (option.state & QStyle.State_Selected):
                c = _qcolor(_tk("chip_off_fg", PALETTE["muted"]))
                opt.palette.setColor(QPalette.Text, c)
                opt.palette.setColor(QPalette.WindowText, c)
            super().paint(painter, opt, index)
            return
        if col != TaskModel.COL_STATE:
            super().paint(painter, option, index)
            return
        text = str(index.data(Qt.DisplayRole) or "")
        bg, border, fg = _pill_colors(str(index.data(TaskModel.STATE_ROLE) or ""))
        painter.save()
        painter.setRenderHint(QPainter.Antialiasing, True)
        if option.state & QStyle.State_Selected:
            try:
                painter.fillRect(option.rect, _qcolor(_tk("table_sel_bg", "#cce4f7")))
            except Exception:
                pass
        f = option.font
        f.setBold(True)
        painter.setFont(f)
        fm = painter.fontMetrics()
        h = 18.0
        w = min(float(option.rect.width() - 6), float(fm.horizontalAdvance(text)) + 16.0)
        if w < 20.0:
            w = min(float(option.rect.width()), 20.0)
        rect = QRectF(float(option.rect.x()) + 3.0,
                      float(option.rect.y()) + (option.rect.height() - h) / 2.0, w, h)
        try:
            painter.setPen(QPen(_qcolor(border), 1.0))
            painter.setBrush(QBrush(_qcolor(bg)))
            painter.drawRoundedRect(rect, h / 2.0, h / 2.0)
            painter.setPen(_qcolor(fg))
            painter.drawText(rect.adjusted(8.0, 0.0, -8.0, 0.0),
                             int(Qt.AlignLeft | Qt.AlignVCenter), text)
        except Exception:
            pass
        painter.restore()

    def sizeHint(self, option, index):
        sh = super().sizeHint(option, index)
        try:
            if index.column() == TaskModel.COL_STATE:
                sh.setWidth(max(sh.width(), 92))
        except Exception:
            pass
        return sh


class TaskTable(QTableView):
    """任务表：TaskModel + 状态胶囊 + 行内操作（详细信息 / 打开输出目录 / 重试）。"""

    taskActivated = pyqtSignal(int)
    taskDoubleClicked = pyqtSignal(int)      # 双击行（详情入口）；与点击选中解耦
    actionTriggered = pyqtSignal(int, str)   # task_id, 'details'|'open_dir'|'retry'

    def __init__(self, parent=None):
        super().__init__(parent)
        self.setObjectName("taskTable")
        self._model = TaskModel(self)
        self.setModel(self._model)
        self.setItemDelegate(_StatePillDelegate(self))
        self.setSelectionBehavior(QAbstractItemView.SelectRows)
        self.setSelectionMode(QAbstractItemView.SingleSelection)
        self.setEditTriggers(QAbstractItemView.NoEditTriggers)
        self.setAlternatingRowColors(True)
        self.setShowGrid(False)
        self.setWordWrap(False)
        self.setTextElideMode(Qt.ElideMiddle)
        self.verticalHeader().setVisible(False)
        self.verticalHeader().setDefaultSectionSize(44)
        hh = self.horizontalHeader()
        hh.setHighlightSections(False)
        hh.setStretchLastSection(False)
        hh.setSectionResizeMode(TaskModel.COL_FILE, QHeaderView.Stretch)
        hh.setSectionResizeMode(TaskModel.COL_OUT, QHeaderView.Stretch)
        for col, width in ((TaskModel.COL_STATE, 92), (TaskModel.COL_SIZE, 78),
                           (TaskModel.COL_PWD, 96), (TaskModel.COL_COST, 72),
                           (TaskModel.COL_ACT, 106)):
            hh.setSectionResizeMode(col, QHeaderView.Fixed)
            self.setColumnWidth(col, width)
        self._action_widgets = []
        self._last_tasks_sig = None      # 上次装载的显示行指纹（保视图刷新短路用）
        sm = self.selectionModel()
        if sm is not None:
            sm.currentRowChanged.connect(self._on_current_row)
        self.doubleClicked.connect(self._on_double_clicked)

    def set_tasks(self, rows, scroll_to_top=True):
        """重建任务表。

        scroll_to_top=True（默认，保持既有行为）：重建后回到顶部，供用户显式
        刷新 / 换范围使用。scroll_to_top=False：留给生命周期刷新——由调用方
        （TaskPage.set_tasks(preserve_view=True)）在选中恢复后再还原滚动位置。

        生命周期刷新（scroll_to_top=False）下若显示行指纹与上次完全一致，
        直接返回：不重置模型、不重建行内操作控件（最多 ~500 行 → 1000+ 控件）。
        调用方（TaskPage）的计数 / 空态 / 选中恢复逻辑照常执行。
        """
        rows = [r for r in (rows or []) if isinstance(r, dict)]
        sig = _task_rows_signature(rows)
        if not scroll_to_top and sig == self._last_tasks_sig:
            return
        self._last_tasks_sig = sig
        self._clear_actions()
        self._model.set_tasks(rows)
        self._build_actions()
        if scroll_to_top:
            self.scroll_to_top()

    def scroll_value(self):
        """当前垂直滚动位置（异常时返回 0，绝不打断刷新）。"""
        try:
            return int(self.verticalScrollBar().value())
        except Exception:
            return 0

    def set_scroll_value(self, value):
        """还原垂直滚动位置（尽力而为；越界由控件自行钳制）。"""
        try:
            self.verticalScrollBar().setValue(int(value))
        except Exception:
            pass

    def task_model(self):
        return self._model

    def task_at(self, row):
        return self._model.task_at(row)

    def _build_actions(self):
        for row in range(self._model.rowCount()):
            task_id = self._model.data(
                self._model.index(row, TaskModel.COL_STATE), Qt.UserRole)
            try:
                tid = int(task_id)
            except Exception:
                continue
            widget = self._make_action_widget(tid)
            self.setIndexWidget(self._model.index(row, TaskModel.COL_ACT), widget)
            self._action_widgets.append(widget)

    def _make_action_widget(self, task_id):
        w = QWidget(self)
        lay = QHBoxLayout(w)
        lay.setContentsMargins(0, 0, 6, 0)
        lay.setSpacing(4)
        lay.addStretch(1)
        for action, glyph, tip in (("details", "info", "详细信息"),
                                   ("open_dir", "external", "打开输出目录"),
                                   ("retry", "refresh", "重试")):
            btn = QPushButton(w)
            btn.setObjectName("rowAct")
            btn.setFixedSize(30, 30)
            btn.setToolTip(tip)
            btn.setCursor(Qt.PointingHandCursor)
            inner = QHBoxLayout(btn)
            inner.setContentsMargins(0, 0, 0, 0)
            inner.addWidget(Glyph(glyph, btn, 16, role="muted"), 0, Qt.AlignCenter)
            btn.clicked.connect(
                lambda _=False, a=action, t=task_id: self.actionTriggered.emit(t, a))
            lay.addWidget(btn)
        return w

    def _clear_actions(self):
        for w in self._action_widgets:
            try:
                w.setParent(None)
                w.deleteLater()
            except Exception:
                pass
        self._action_widgets = []

    def _on_current_row(self, current, previous=None):
        if not current.isValid():
            return
        task_id = self._model.data(current, Qt.UserRole)
        try:
            self.taskActivated.emit(int(task_id))
        except Exception:
            pass

    def _on_double_clicked(self, index):
        """双击行：发 taskDoubleClicked（详情入口）。

        taskActivated 保持旧行为照发（双击先经选中路径，选中未变时这里兜底），
        详情弹窗只认 taskDoubleClicked，绝不复用 taskActivated（点击选中也会发）。
        """
        if not index.isValid():
            return
        task_id = self._model.data(index, Qt.UserRole)
        try:
            tid = int(task_id)
        except Exception:
            return
        try:
            self.taskActivated.emit(tid)
        except Exception:
            pass
        try:
            self.taskDoubleClicked.emit(tid)
        except Exception:
            pass

    def select_task(self, task_id):
        for row in range(self._model.rowCount()):
            cur = self._model.data(self._model.index(row, TaskModel.COL_STATE),
                                   Qt.UserRole)
            if cur == task_id or str(cur) == str(task_id):
                self.selectRow(row)
                return True
        return False

    def scroll_to_top(self):
        try:
            self.verticalScrollBar().setValue(0)
        except Exception:
            pass
