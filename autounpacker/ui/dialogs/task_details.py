# -*- coding: utf-8 -*-
"""TaskDetailsDialog：任务详情弹窗（队列行「详细信息」按钮 / 双击队列行打开）。

只读展示单条任务的字段与最近日志；按「状态 + 源文件是否存在」计算动作集合——
任何状态都至少有一条出路（任务绝无「不朽」态）。动作经 actionRequested 交给
宿主落库并刷新队列/徽标；本弹窗只重读任务（绝不基于过期副本）、重建自身，
记录已消失则关闭并发 notice 提示。

遮罩与「点击外部关闭」：见 dialogs/scrim.py。Scrim 是覆盖主窗的暗化顶层窗口，
它自己持有 ApplicationModal 模态（被模态阻塞的窗口收不到任何鼠标事件，所以不能
用事件过滤器抓「外点」）；本弹窗是它的子窗——Windows 上被拥有的窗口永远在属主
之上，且模态窗口自身的 transient 子窗不受其模态阻塞，因此弹窗内按钮照常可用。
点击遮罩 -> Scrim.mousePressEvent -> 调用本弹窗的 reject() 关闭。宿主负责成对
创建/拆除 Scrim 与弹窗（见 main_window._open_task_details）。
"""
import os
import time

from PyQt5.QtCore import Qt, pyqtSignal
from PyQt5.QtWidgets import (QApplication, QDialog, QFrame, QGridLayout,
                             QHBoxLayout, QLabel, QLineEdit, QPlainTextEdit,
                             QPushButton, QVBoxLayout, QWidget)

from ... import db
from ..pages import task_log_line
from ..widgets import (Glyph, _STATE_TEXT, _clear_layout, _fmt_cost, _fmt_pwd,
                       _fmt_size, _row_file, _row_out, _task_state_key)

# 显示字段（顺序即弹窗行顺序）；带编辑框的三个为长文本（可选可滚动）
_ROWS = (("状态", "state"), ("任务号", "id"), ("文件名", "file"),
         ("大小", "size"), ("源目录", "source_dir"), ("输出去向", "out"),
         ("模式", "mode"), ("密码来源", "pwd"), ("耗时", "cost"),
         ("时间", "times"))
_EDIT_KEYS = ("file", "source_dir", "out")
_MODE_TEXT = {"surface": "表层", "baidu": "百度清单（含子目录）"}


def _fmt_ts(value):
    """时间戳 -> 本地时间文本；空/非法/非正数一律「—」。"""
    try:
        ts = float(value or 0)
    except Exception:
        return "—"
    if ts <= 0:
        return "—"
    try:
        return time.strftime("%Y-%m-%d %H:%M:%S", time.localtime(ts))
    except Exception:
        return "—"


def source_exists(task):
    """源文件是否仍在监听目录（重试类动作的成立条件）。"""
    try:
        src = str((task or {}).get("source_dir") or "")
        name = str((task or {}).get("file_name") or "")
        if not src or not name:
            return False
        return os.path.isfile(os.path.join(src, name))
    except Exception:
        return False


def task_action_set(state, src_exists):
    """按状态 + 源文件是否存在计算动作集合 -> [(kind, label, role)]。

    role: "primary" / "danger" / ""（默认样式）。不变量：任何输入都至少返回
    一个动作——未知状态兜底为「从队列移除 + 删除记录」，任务永远不会无处可去。
    """
    s = _task_state_key({"state": state})
    acts = []
    if s == "need_password":
        acts.append(("input_password", "跳转到密码本", "primary"))
        if src_exists:
            acts.append(("retry", "重试", ""))
        acts.append(("open_dir", "打开输出目录", ""))
        acts.append(("ignore", "从队列移除", "danger"))
        acts.append(("delete", "删除记录", "danger"))
    elif s == "failed":
        if src_exists:
            acts.append(("retry", "重试", ""))
        acts.append(("copy_error", "复制错误", ""))
        acts.append(("open_dir", "打开输出目录", ""))
        acts.append(("ignore", "从队列移除", "danger"))
        acts.append(("delete", "删除记录", "danger"))
    elif s in ("queued", "extracting"):
        acts.append(("open_dir", "打开输出目录", ""))
        acts.append(("ignore", "从队列移除", "danger"))
        acts.append(("delete", "删除记录", "danger"))
    elif s == "done":
        acts.append(("open_dir", "打开输出目录", ""))
        acts.append(("copy_output", "复制输出去向", ""))
        acts.append(("delete", "删除记录", "danger"))
    elif s == "canceled":
        if src_exists:
            acts.append(("retry", "重试", ""))
        acts.append(("delete", "删除记录", "danger"))
    if not acts:
        acts = [("ignore", "从队列移除", "danger"),
                ("delete", "删除记录", "danger")]
    return acts


class TaskDetailsDialog(QDialog):
    """单条任务详情 + 状态相关动作（宿主为 Scrim 遮罩窗口；自身非模态）。"""

    actionRequested = pyqtSignal(int, str)   # task_id, kind（宿主执行并刷新）
    notice = pyqtSignal(str)                 # 一行提示（宿主写日志）

    def __init__(self, task_id, scrim=None):
        super().__init__(scrim)
        try:
            self.task_id = int(task_id or 0)
        except Exception:
            self.task_id = 0
        self._fields = {}
        self._actions = []
        self.setWindowTitle("任务详情")
        self.setModal(False)     # 阻塞由父级 Scrim 负责，绝不能反过来
        self.resize(640, 560)
        self.setMinimumWidth(600)

        root = QVBoxLayout(self)
        root.setContentsMargins(0, 0, 0, 0)
        root.setSpacing(0)
        root.addWidget(self._build_head())
        body = QWidget(self)
        body_lay = QVBoxLayout(body)
        body_lay.setContentsMargins(16, 14, 16, 14)
        body_lay.setSpacing(11)
        self._build_body(body_lay)
        root.addWidget(body, 1)
        root.addWidget(self._build_foot())
        self._reload()

    # ---- 头部 / 表单 / 底部 ----
    def _build_head(self):
        head = QFrame(self)
        head.setObjectName("dlgHead")
        lay = QHBoxLayout(head)
        lay.setContentsMargins(16, 14, 16, 12)
        lay.setSpacing(10)
        lay.addWidget(Glyph("info", head, 20, role="accent"))
        title = QLabel("任务详情", head)
        title.setObjectName("dTitle")
        lay.addWidget(title)
        self.head_badge = QLabel(head)
        self.head_badge.setObjectName("dlgState")
        self.head_badge.setAttribute(Qt.WA_TransparentForMouseEvents, True)
        lay.addWidget(self.head_badge)
        lay.addStretch(1)
        close_btn = QPushButton(head)
        close_btn.setObjectName("iconBtn")
        close_btn.setFixedSize(30, 30)
        close_btn.setToolTip("关闭")
        close_btn.setCursor(Qt.PointingHandCursor)
        close_lay = QHBoxLayout(close_btn)
        close_lay.setContentsMargins(0, 0, 0, 0)
        close_lay.addWidget(Glyph("close", close_btn, 16), 0, Qt.AlignCenter)
        close_btn.clicked.connect(self.reject)
        lay.addWidget(close_btn)
        return head

    def _build_body(self, lay):
        grid = QGridLayout()
        grid.setHorizontalSpacing(10)
        grid.setVerticalSpacing(7)
        grid.setColumnStretch(1, 1)
        for i, (text, key) in enumerate(_ROWS):
            lbl = QLabel(text, self)
            lbl.setObjectName("fLabel")
            lbl.setFixedWidth(62)
            grid.addWidget(lbl, i, 0, Qt.AlignTop)
            if key in _EDIT_KEYS:
                val = QLineEdit(self)
                val.setReadOnly(True)
            else:
                val = QLabel("—", self)
                val.setTextInteractionFlags(Qt.TextSelectableByMouse)
                val.setWordWrap(True)
            grid.addWidget(val, i, 1)
            self._fields[key] = val
        row = len(_ROWS)
        err_cap = QLabel("错误", self)
        err_cap.setObjectName("fLabel")
        err_cap.setFixedWidth(62)
        grid.addWidget(err_cap, row, 0, Qt.AlignTop)
        self.err_box = QPlainTextEdit(self)
        self.err_box.setReadOnly(True)
        self.err_box.setFixedHeight(88)
        grid.addWidget(self.err_box, row, 1)
        log_cap = QLabel("最近日志", self)
        log_cap.setObjectName("fLabel")
        log_cap.setFixedWidth(62)
        grid.addWidget(log_cap, row + 1, 0, Qt.AlignTop)
        self.log_box = QPlainTextEdit(self)
        self.log_box.setReadOnly(True)
        self.log_box.setFixedHeight(132)
        grid.addWidget(self.log_box, row + 1, 1)
        lay.addLayout(grid)

    def _build_foot(self):
        foot = QFrame(self)
        foot.setObjectName("dlgFoot")
        lay = QVBoxLayout(foot)
        lay.setContentsMargins(16, 10, 16, 12)
        lay.setSpacing(8)
        self.act_row = QHBoxLayout()
        self.act_row.setSpacing(8)
        self.act_row.addStretch(1)
        lay.addLayout(self.act_row)
        row = QHBoxLayout()
        row.setSpacing(8)
        self.feedback = QLabel("", foot)
        self.feedback.setObjectName("dlgHint")
        row.addWidget(self.feedback, 1)
        for text, kind in (("查看该任务日志", "view_log"),
                           ("复制文件路径", "copy_path")):
            btn = QPushButton(text, foot)
            btn.setCursor(Qt.PointingHandCursor)
            btn.clicked.connect(lambda _=False, k=kind: self._act(k))
            row.addWidget(btn)
        close_btn = QPushButton("关闭", foot)
        close_btn.setCursor(Qt.PointingHandCursor)
        close_btn.setDefault(True)
        close_btn.clicked.connect(self.reject)
        row.addWidget(close_btn)
        lay.addLayout(row)
        return foot

    # ---- 数据装载 ----
    def _fill(self, task):
        """把任务字段与日志填进控件（task 可为 None -> 全「—」）。"""
        task = task if isinstance(task, dict) else {}
        key = _task_state_key(task) if task else ""
        values = {
            "state": str(task.get("state_text")
                         or _STATE_TEXT.get(key, key or "—")),
            "id": ("#%d" % self.task_id) if self.task_id else "—",
            "file": _row_file(task) or "—",
            "size": _fmt_size(task.get("file_size")),
            "source_dir": str(task.get("source_dir") or "—"),
            "out": _row_out(task) or "—",
            "mode": _MODE_TEXT.get(str(task.get("mode") or "").strip(),
                                   str(task.get("mode") or "—")),
            "pwd": _fmt_pwd(task),
            "cost": _fmt_cost(task),
            "times": "创建 %s · 开始 %s · 完成 %s" % (
                _fmt_ts(task.get("created_at")), _fmt_ts(task.get("started_at")),
                _fmt_ts(task.get("finished_at"))),
        }
        for field, widget in self._fields.items():
            text = values.get(field, "—")
            if isinstance(widget, QLineEdit):
                widget.setText(text)
                widget.setCursorPosition(0)
                widget.setToolTip("" if text == "—" else text)
            else:
                widget.setText(text)
        self.err_box.setPlainText(str(task.get("error") or "").strip() or "—")
        try:
            rows = db.task_logs(self.task_id, limit=2000) or []
        except Exception:
            rows = []
        self.log_box.setPlainText(
            "\n".join(task_log_line(r) for r in rows[-200:])
            if rows else "该任务暂无日志记录")
        try:
            bar = self.log_box.verticalScrollBar()
            bar.setValue(bar.maximum())
        except Exception:
            pass

    def _reload(self):
        """重读任务并整体重建：字段 + 状态徽标 + 状态动作按钮。"""
        try:
            task = db.get_task(self.task_id)
        except Exception:
            task = None
        self._fill(task)
        if task:
            key = _task_state_key(task)
            self._set_badge(str(task.get("state_text")
                                or _STATE_TEXT.get(key, key or "")))
            actions = task_action_set(key, source_exists(task))
        else:
            self._set_badge("记录不存在")
            actions = []
        self._rebuild(state=task, actions=actions)
        self.feedback.clear()

    def _set_badge(self, text):
        self.head_badge.setText(str(text or ""))
        self.head_badge.setVisible(bool(str(text or "").strip()))

    def _rebuild(self, state, actions):
        """重建状态动作按钮（清空后按 actions 顺序重排）。"""
        _clear_layout(self.act_row)
        self._actions = list(actions or [])
        for kind, label, role in self._actions:
            btn = QPushButton(label, self)
            if role in ("primary", "danger"):
                btn.setObjectName(role)
            btn.setCursor(Qt.PointingHandCursor)
            btn.clicked.connect(lambda _=False, k=kind: self._act(k))
            self.act_row.addWidget(btn)
        self.act_row.addStretch(1)

    def action_kinds(self):
        """当前状态动作的 kind 列表（断言用；不含查看日志/复制/关闭）。"""
        return [k for k, _label, _role in self._actions]

    # ---- 动作 ----
    def _act(self, kind):
        """动作入口：复制就地处理；其余先重读任务（绝不基于过期副本）再交宿主。"""
        kind = str(kind)
        if kind in ("copy_error", "copy_output", "copy_path"):
            self._copy(kind)
            return
        if not db.get_task(self.task_id):
            self._notify("该任务记录已不存在，详情窗口已关闭。")
            self.reject()
            return
        self.actionRequested.emit(int(self.task_id), kind)
        if kind in ("view_log", "input_password"):
            self.reject()
            return
        self._reload()
        if not db.get_task(self.task_id):
            self.reject()

    def _copy(self, kind):
        """复制错误 / 输出去向 / 文件路径到剪贴板（空则只提示）。"""
        task = None
        try:
            task = db.get_task(self.task_id)
        except Exception:
            task = None
        task = task or {}
        if kind == "copy_error":
            text, what = str(task.get("error") or "").strip(), "错误信息"
        elif kind == "copy_output":
            text, what = _row_out(task).strip(), "输出去向"
        else:
            text, what = self._full_path(task), "文件路径"
        if not text or text == "—":
            self._notify("没有可复制的%s。" % what)
            return
        try:
            QApplication.clipboard().setText(text)
        except Exception as e:
            self._notify("复制%s失败：%s" % (what, e))
            return
        self._notify("已复制%s到剪贴板" % what)

    def _full_path(self, task):
        src = str((task or {}).get("source_dir") or "").strip()
        name = _row_file(task or {}).strip()
        try:
            if src and name:
                return os.path.join(src, name)
            return src or name
        except Exception:
            return ""

    def _notify(self, text):
        self.feedback.setText(str(text or ""))
        try:
            self.notice.emit(str(text or ""))
        except Exception:
            pass

    # ---- 几何：居中于父级（父级是 Scrim，其几何 == 主窗 frameGeometry） ----
    def showEvent(self, event):
        super().showEvent(event)
        self._center_on_parent()

    def _center_on_parent(self):
        try:
            parent = self.parentWidget()
            if parent is None:
                return
            self.adjustSize()
            pg = parent.frameGeometry()
            self.move(pg.center().x() - self.width() // 2,
                      pg.center().y() - self.height() // 2)
        except Exception:
            pass
