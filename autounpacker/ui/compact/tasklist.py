# -*- coding: utf-8 -*-
"""精简任务行视图（只读）：紧凑行 = 状态字形 + 文件名(中部省略) + 细进度条 + 右侧状态。

数据与完整界面**同源**：直接读 `db.count_tasks()` / `db.list_tasks()`
（绝不新建数据库、绝不写入）。排序：进行中 → 排队(含待密码) → 失败 → 最近完成；
最多显示 6 行，超出在列表内滚动。双击行发 `taskActivated(task_id)`，
具体的「打开输出目录」动作由 `CompactWindow` 交给宿主既有入口执行。

进度说明：任务表本身没有逐任务进度列；进行中的行进度取自宿主已有的
`_dir_states`（目录级进度，键为规范化目录），取不到时进度条走「忙碌」态。
"""
from PyQt5.QtCore import Qt, pyqtSignal
from PyQt5.QtWidgets import (QFrame, QHBoxLayout, QLabel, QProgressBar,
                             QScrollArea, QSizePolicy, QVBoxLayout, QWidget)

from ... import db
from ..widgets.common import _task_state_key, _task_rows_signature
from ..widgets.inputs import Glyph, _ElideLabel

ROW_H = 32          # 单行高（含 1px 分隔线）
MAX_VISIBLE = 6     # 列表最多显示 6 行，超出滚动（规格 §5.1）
MAX_ROWS = 50       # 单次最多渲染行数（防极端数据量拖慢小窗）

# 排序分组：进行中 → 排队(含待密码) → 失败 → 最近完成（规格 §5.1）
_RANK = {"extracting": 0, "queued": 1, "need_password": 1,
         "failed": 2, "done": 3, "canceled": 3}
# 右侧状态文字（就用完整界面同款词表的口径）
_TEXT = {"extracting": "解压中", "queued": "排队中", "need_password": "待密码",
         "done": "完成", "failed": "失败", "canceled": "已取消"}
# 状态字形（复用 widgets 的线性图标；role 决定取色）
_GLYPH = {"extracting": ("bolt", "accent"), "queued": ("queue", "muted"),
          "need_password": ("key", "muted"), "done": ("check", "success"),
          "failed": ("alert", "danger"), "canceled": ("close", "muted")}


def _sort_key(row):
    """排序键：先分组，再按「最近」倒序（完成态用完成时间，其余用创建时间）。"""
    key = _task_state_key(row)
    try:
        created = float(row.get("created_at") or 0)
    except Exception:
        created = 0.0
    try:
        finished = float(row.get("finished_at") or 0)
    except Exception:
        finished = 0.0
    try:
        tid = int(row.get("id") or 0)
    except Exception:
        tid = 0
    return (_RANK.get(key, 9), -max(created, finished), -tid)


def _dir_progress(host, source_dir):
    """目录级进度查表（0..100）；查不到返回 None（进度未知）。

    宿主 `_dir_states` 只是可选数据源：宿主桩 / 其它形态一律安全退化。
    """
    src = str(source_dir or "").strip()
    if not src:
        return None
    states = getattr(host, "_dir_states", None)
    if not isinstance(states, dict):
        return None
    keys = [src]
    try:
        from ...utils import _norm_path_for_cfg
        keys.append(_norm_path_for_cfg(src))
    except Exception:
        pass
    for key in keys:
        ent = states.get(key)
        if not ent:
            continue
        try:
            state = str(ent[0] or "")
            prog = ent[1]
            if state == "extracting" and prog is not None:
                return max(0, min(100, int(round(float(prog)))))
        except Exception:
            continue
    return None


class _TaskRow(QWidget):
    """单行紧凑任务：只读展示；双击发 activated(task_id)。"""

    activated = pyqtSignal(int)

    def __init__(self, row, host, parent=None):
        super().__init__(parent)
        self.setObjectName("compactTaskRow")
        self.setAttribute(Qt.WA_StyledBackground, True)
        self.setFixedHeight(ROW_H)
        self._host = host
        self._state = _task_state_key(row)
        self._source = str(row.get("source_dir") or "")
        try:
            self.task_id = int(row.get("id") or 0)
        except Exception:
            self.task_id = 0

        glyph, role = _GLYPH.get(self._state, ("archive", "muted"))
        lay = QHBoxLayout(self)
        lay.setContentsMargins(10, 0, 10, 0)
        lay.setSpacing(8)
        self.glyph = Glyph(glyph, self, 14, role=role)
        lay.addWidget(self.glyph, 0, Qt.AlignVCenter)

        self.name = _ElideLabel(str(row.get("file_name") or row.get("file") or ""),
                                self)
        self.name.setObjectName("compactRowName")
        lay.addWidget(self.name, 1)

        self.bar = QProgressBar(self)
        self.bar.setObjectName("thinProg")
        self.bar.setTextVisible(False)
        self.bar.setFixedWidth(46)
        self.bar.setRange(0, 100)
        self.bar.setValue(0)
        lay.addWidget(self.bar, 0, Qt.AlignVCenter)

        self.state_label = QLabel(_TEXT.get(self._state, self._state), self)
        self.state_label.setObjectName("compactRowState")
        self.state_label.setAlignment(Qt.AlignRight | Qt.AlignVCenter)
        self.state_label.setFixedWidth(48)
        lay.addWidget(self.state_label, 0, Qt.AlignVCenter)

        self.update_progress(host)

    def update_progress(self, host=None):
        """刷新进度条与右侧状态文字（进行中显示百分比，其余显示状态词）。"""
        try:
            if self._state == "extracting":
                pct = _dir_progress(host if host is not None else self._host,
                                    self._source)
                if pct is None:
                    self.bar.setRange(0, 0)          # 忙碌态：进度未知
                    self.state_label.setText("解压中")
                else:
                    self.bar.setRange(0, 100)
                    self.bar.setValue(pct)
                    self.state_label.setText("%d%%" % pct)
            elif self._state == "done":
                self.bar.setRange(0, 100)
                self.bar.setValue(100)
            else:
                self.bar.setRange(0, 100)
                self.bar.setValue(0)
        except Exception:
            pass

    def mouseDoubleClickEvent(self, event):
        """双击行 = 打开输出目录（动作交给列表 / 窗口转发，本视图不做别的）。"""
        try:
            if self.task_id > 0:
                self.activated.emit(self.task_id)
        except Exception:
            pass
        try:
            event.accept()
        except Exception:
            pass


class CompactTaskList(QWidget):
    """紧凑任务列表：空态一行灰字；最多 6 行后滚动；整表随数据指纹重建。"""

    taskActivated = pyqtSignal(int)

    def __init__(self, host, parent=None):
        super().__init__(parent)
        self._host = host
        self._sig = None
        self._row_widgets = {}
        self.setSizePolicy(QSizePolicy.Preferred, QSizePolicy.Expanding)
        self.setMinimumHeight(ROW_H)

        root = QVBoxLayout(self)
        root.setContentsMargins(0, 0, 0, 0)
        root.setSpacing(0)

        self.empty = QLabel("暂无任务 · 拖入压缩包即可开始", self)
        self.empty.setObjectName("compactEmpty")
        self.empty.setAlignment(Qt.AlignCenter)
        self.empty.setFixedHeight(ROW_H)
        root.addWidget(self.empty)

        self.scroll = QScrollArea(self)
        self.scroll.setObjectName("compactTaskScroll")
        self.scroll.setWidgetResizable(True)
        self.scroll.setFrameShape(QFrame.NoFrame)
        self.scroll.setHorizontalScrollBarPolicy(Qt.ScrollBarAlwaysOff)
        self.scroll.setVerticalScrollBarPolicy(Qt.ScrollBarAsNeeded)
        self.scroll.setMinimumHeight(ROW_H)
        self.scroll.hide()
        host_widget = QWidget()
        host_widget.setObjectName("compactTaskHost")
        self._vbox = QVBoxLayout(host_widget)
        self._vbox.setContentsMargins(0, 0, 0, 0)
        self._vbox.setSpacing(0)
        self._vbox.addStretch(1)
        self.scroll.setWidget(host_widget)
        self._host_widget = host_widget
        root.addWidget(self.scroll, 1)

    # ---- 数据 ----
    def refresh(self):
        """重查任务数据并刷新视图；返回 `db.count_tasks()` 的计数字典。"""
        try:
            counts = db.count_tasks() or {}
        except Exception:
            counts = {}
        try:
            rows = (db.list_tasks(scope="queue", limit=MAX_ROWS)
                    + db.list_tasks(scope="history", limit=MAX_ROWS))
        except Exception:
            rows = []
        rows = [r for r in rows if isinstance(r, dict)]
        rows.sort(key=_sort_key)
        rows = rows[:MAX_ROWS]

        sig = _task_rows_signature(rows)
        if sig != self._sig:
            self._sig = sig
            self._rebuild(rows)
        else:
            for widget in list(self._row_widgets.values()):
                widget.update_progress(self._host)
        self._sync_visibility(len(rows))
        return counts

    def _rebuild(self, rows):
        """按行集合重建行控件（行数/内容变化时才做）。"""
        while self._vbox.count():
            item = self._vbox.takeAt(0)
            widget = item.widget() if item is not None else None
            if widget is not None:
                try:
                    widget.setParent(None)
                    widget.deleteLater()
                except Exception:
                    pass
        self._row_widgets = {}
        for row in rows:
            widget = _TaskRow(row, self._host, self._host_widget)
            widget.activated.connect(self.taskActivated)
            self._vbox.addWidget(widget)
            if widget.task_id > 0:
                self._row_widgets[widget.task_id] = widget
        self._vbox.addStretch(1)

    def _sync_visibility(self, count):
        """空态切换 + 列表高度（≤6 行按行数收缩，超出固定 6 行高度滚动）。"""
        try:
            if count <= 0:
                self.scroll.hide()
                self.empty.show()
                return
            self.empty.hide()
            self.scroll.show()
            self.scroll.setMaximumHeight(min(count, MAX_VISIBLE) * ROW_H)
        except Exception:
            pass
