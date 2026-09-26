# -*- coding: utf-8 -*-
"""TaskDetailsDialog：任务详情弹窗（队列行「详细信息」按钮 / 双击队列行打开）。

只读展示单条任务的字段与最近日志；按「状态 + 源文件是否存在」计算动作集合——
任何状态都至少有一条出路（任务绝无「不朽」态）。动作经 actionRequested 交给
宿主落库并刷新队列/徽标；本弹窗只重读任务（绝不基于过期副本）、重建自身，
记录已消失则关闭并发 notice 提示。

遮罩：与 WatchDirDialog 同一套实现（项目没有 dim-mask 原语）——父窗口上的一个
rgba(0,0,0,.34) 子控件，showEvent 建、hideEvent/closeEvent 拆。主窗使用原生
Windows 标题栏（非客户区），子控件遮罩天然只覆盖客户区，系统标题栏不受影响。

点击外部关闭：仍保持模态（exec_），但显示期间装 QApplication 级事件过滤器——
弹窗 frameGeometry 之外的按下（含遮罩上的点击）一律 reject；弹窗内部（按钮/输入
框/滚动区）的按下原样放行。hide/close 时移除过滤器，避免拦截宿主窗口后续事件。
"""
import os
import time

from PyQt5.QtCore import QEvent, Qt, pyqtSignal
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
    """单条任务详情 + 状态相关动作（模态；遮罩同 WatchDirDialog）。"""

    actionRequested = pyqtSignal(int, str)   # task_id, kind（宿主执行并刷新）
    notice = pyqtSignal(str)                 # 一行提示（宿主写日志）

    def __init__(self, task_id, parent=None):
        super().__init__(parent)
        try:
            self.task_id = int(task_id or 0)
        except Exception:
            self.task_id = 0
        self._scrim = None
        self._filter_installed = False
        self._fields = {}
        self._actions = []
        self.setWindowTitle("任务详情")
        self.setModal(True)
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

    # ---- 遮罩（与 WatchDirDialog 同款实现） ----
    def _ensure_scrim(self):
        if self._scrim is not None:
            return
        parent = self.parentWidget()
        if parent is None:
            return
        try:
            sc = QFrame(parent)
            sc.setObjectName("dlgScrim")
            sc.setAttribute(Qt.WA_TransparentForMouseEvents, True)
            sc.setAttribute(Qt.WA_StyledBackground, True)
            sc.setStyleSheet("background: rgba(0,0,0,0.34);")
            sc.setGeometry(parent.rect())
            sc.show()
            sc.raise_()
            self._scrim = sc
        except Exception:
            self._scrim = None

    def _destroy_scrim(self):
        sc = self._scrim
        self._scrim = None
        if sc is None:
            return
        try:
            sc.hide()
            sc.setParent(None)
            sc.deleteLater()
        except Exception:
            pass

    # ---- 点击外部关闭（仍保持模态） ----
    def _install_app_filter(self):
        """装上 QApplication 级过滤器（仅窗口显示期间；重复 show 不重装）。"""
        if self._filter_installed:
            return
        app = QApplication.instance()
        if app is None:
            return
        try:
            app.installEventFilter(self)
        except Exception:
            return
        self._filter_installed = True

    def _uninstall_app_filter(self):
        """移除 QApplication 级过滤器（close/hide 都走这里）。"""
        if not self._filter_installed:
            return
        self._filter_installed = False
        app = QApplication.instance()
        if app is None:
            return
        try:
            app.removeEventFilter(self)
        except Exception:
            pass

    def _hit_scrim(self, gp):
        """全局坐标是否落在遮罩上（遮罩可能不存在 -> False）。"""
        sc = self._scrim
        if sc is None:
            return False
        try:
            return bool(sc.isVisible()) and sc.rect().contains(sc.mapFromGlobal(gp))
        except Exception:
            return False

    def eventFilter(self, obj, event):
        """弹窗显示期间：弹窗外（含遮罩）的鼠标按下 = 关闭，返回 True 吞掉该事件。

        只按全局坐标判断——frameGeometry 内的按下（按钮/输入框/滚动区）原样放行，
        绝不误关；reject 后 hideEvent 会自行移除过滤器。
        """
        try:
            if event.type() == QEvent.MouseButtonPress and self.isVisible():
                gp = event.globalPos()
                if not self.frameGeometry().contains(gp) or self._hit_scrim(gp):
                    self.reject()
                    return True
        except Exception:
            pass
        return super().eventFilter(obj, event)

    def showEvent(self, event):
        self._install_app_filter()
        self._ensure_scrim()
        super().showEvent(event)
        self._center_on_parent()

    def hideEvent(self, event):
        self._uninstall_app_filter()
        self._destroy_scrim()
        super().hideEvent(event)

    def closeEvent(self, event):
        self._uninstall_app_filter()
        self._destroy_scrim()
        super().closeEvent(event)

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
