# -*- coding: utf-8 -*-
"""工作台页面组件（M3）：TaskPage / LogPage / StatusBar。

职责：- TaskPage（原型 11 主区）：队列/历史任务表 + 该任务日志（当前任务/全部）+ 行内操作
- LogPage（原型 13）：级别/路径/搜索三滤镜日志页 + 今日统计卡 + 需要处理面板
- StatusBar（原型 11 底栏）：左状态区（监听/轮询/进行中/失败聚合/快捷键）+ 右纵向播报
关键入口：TaskPage / LogPage / StatusBar /
          task_log_line() / log_matches() / today_stats() / count_today_logs()
依赖：PyQt5、widgets（M2 控件家族）、style（PALETTE）、db、trail、hub.guess_level
注意：本模块不联网、不起线程；数据只经 db / trail 模块读，测试可对这些模块打桩替换。
注意：页面不直接写 db；数据装载由宿主（MainWindow）调用 set_tasks/set_stats/reload 等入口。
注意：动态属性控件（选中/激活）在主题切换后由宿主 repolish，见 MainWindow._repolish_dynamic。
注意：密码本 / 删除回溯 / 设置三个正式页自 M4 起分别位于 page_pwbook / page_trail /
      page_settings 模块（本模块只保留任务 / 日志 / 底栏与共享小控件）。
"""
import time

from PyQt5.QtCore import QEvent, Qt, QTimer, pyqtSignal
from PyQt5.QtGui import QColor, QPainter, QPalette
from PyQt5.QtWidgets import (QCheckBox, QFileDialog, QFrame, QHBoxLayout, QLabel,
                             QLineEdit, QMenu, QMessageBox, QPlainTextEdit,
                             QProgressBar, QPushButton, QSizePolicy, QSplitter,
                             QSplitterHandle, QVBoxLayout, QWidget)

from .. import db
from .. import trail as deletion_trail
from ..hub import guess_level
from .style import PALETTE, tokens
from .widgets import (DEFAULT_TIPS, FilterChipStrip, Glyph,
                      NeedsAttentionCard, SegControl, StatusTipTicker, TaskTable)

# 级别分段 -> log_index.level 集合（success/link 归入「信息」，与原型计数口径一致）
LOG_LEVEL_BUCKETS = {
    "all": None,
    "info": frozenset(("info", "success", "link")),
    "wait": frozenset(("wait",)),
    "error": frozenset(("error",)),
}

# 空态文案（原型只给了 .empty 样式、没有具体文案；这里按同一语气补齐）
EMPTY_TASKS = "暂无任务 · 把压缩包拖进窗口或放进监听目录即可开始"
EMPTY_TASK_LOG = "在上方选择一个任务，查看它自己的日志"
EMPTY_TASK_LOG_EMPTY = "该任务暂无日志记录"
EMPTY_LOG = "暂无日志 · 监听、解压与分享的记录都会出现在这里"
EMPTY_NEEDS = "暂无需要处理的任务"


# ---------------------------------------------------------------------------
# 纯函数助手（无 Qt 依赖，离线测试可直接调用）
# ---------------------------------------------------------------------------

def task_log_line(row):
    """db 日志行 -> 展示文本：补 "[HH:MM:SS] " 前缀（与 Hub.log 的行格式一致）。"""
    if not isinstance(row, dict):
        return ""
    text = str(row.get("text") or "")
    try:
        prefix = time.strftime("[%H:%M:%S] ", time.localtime(float(row.get("ts"))))
    except Exception:
        prefix = ""
    return prefix + text


def level_bucket(level):
    """把一条日志的级别归入 信息/警告/错误 三个分段之一（未知一律按信息）。"""
    lv = str(level or "").strip().lower()
    if lv in ("error", "failed", "failure"):
        return "error"
    if lv in ("wait", "warning", "warn"):
        return "wait"
    return "info"


def log_matches(record, levels=None, sources=None, keyword=None):
    """FINAL-SPEC §3.2 过滤算法：级别 / 路径 / 关键词三滤镜叠加。

    - sources 为空 = 不按路径过滤；source_dir is None 的全局日志**永不被路径挡住**；
    - keyword 大小写不敏感子串；缺 level 时用 hub.guess_level(text) 现算。
    """
    rec = record if isinstance(record, dict) else {}
    level = rec.get("level")
    if not level:
        level = guess_level(str(rec.get("text") or ""))
    if levels and level not in levels:
        return False
    kw = str(keyword or "").strip()
    if kw and kw.lower() not in str(rec.get("text") or "").lower():
        return False
    src = rec.get("source_dir")
    if sources and src is not None and src not in sources:
        return False
    return True


def _today_start():
    """本地时区今天 0 点的时间戳。"""
    lt = time.localtime()
    return time.mktime((lt.tm_year, lt.tm_mon, lt.tm_mday, 0, 0, 0, 0, 0, -1))


def _row_ts(row):
    try:
        return float(row.get("finished_at") or row.get("created_at") or 0)
    except Exception:
        return 0.0


def count_trail_pending():
    """回收站待还原条数（trail 记录里 status == deleted）。"""
    try:
        recs = deletion_trail.load_records() or []
    except Exception:
        return 0
    return sum(1 for r in recs if isinstance(r, dict) and r.get("status") == "deleted")


def count_today_logs(limit=5000):
    """今日日志条数（取最近 limit 行后按日期过滤；limit 即封顶，绝不虚报）。"""
    try:
        rows = db.query_logs(limit=limit) or []
    except Exception:
        return 0
    t0 = _today_start()
    n = 0
    for r in rows:
        try:
            if float(r.get("ts") or 0) >= t0:
                n += 1
        except Exception:
            pass
    return n


def today_stats(limit=500):
    """今日统计卡（原型 13 右栏）：今日解压 / 密码命中 / 失败 / 回收站待还原。

    前 3 项来自 tasks 表（最近 limit 条终态任务按 finished_at 过滤当天），
    回收站项来自 trail。任何异常都退化为 0，绝不让页面因统计失败而崩。
    """
    out = {"done": 0, "pwd": 0, "failed": 0, "trail": count_trail_pending()}
    try:
        rows = db.list_tasks(scope="history", limit=limit) or []
    except Exception:
        rows = []
    t0 = _today_start()
    for r in rows:
        if not isinstance(r, dict) or _row_ts(r) < t0:
            continue
        state = str(r.get("state") or "")
        if state == "done":
            out["done"] += 1
            if r.get("password_src"):
                out["pwd"] += 1
        elif state == "failed":
            out["failed"] += 1
    return out


def needs_item(task):
    """把一个任务行转成 NeedsAttentionCard 的一行（urgency / 说明 / 可用操作）。"""
    state = str(task.get("state") or "")
    err = str(task.get("error") or "").strip()
    try:
        when = time.strftime("%H:%M", time.localtime(_row_ts(task) or time.time()))
    except Exception:
        when = ""
    if state == "need_password":
        return {"task_id": int(task.get("id") or 0),
                "name": str(task.get("file_name") or ""), "ts": when,
                "urgency": "warn",
                "note": err[:80] or "密码未命中 · 可在密码本补充后重试",
                "actions": ["input_password", "retry", "ignore"]}
    return {"task_id": int(task.get("id") or 0),
            "name": str(task.get("file_name") or ""), "ts": when,
            "urgency": "err",
            "note": err[:80] or "解压失败 · 建议重试",
            "actions": ["retry", "open_dir"]}


# ---------------------------------------------------------------------------
# 小控件助手
# ---------------------------------------------------------------------------

class _EmptyOverlay(QLabel):
    """视图空态：覆盖在目标视图之上居中显示一行说明（随尺寸跟随，不拦截鼠标）。"""

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


def _ghost_button(text, parent=None):
    """行内 ghost 小按钮（重试 / 打开输出目录 / 复制 / 导出 / 清空）。"""
    btn = QPushButton(str(text), parent)
    btn.setObjectName("ghostSm")
    btn.setCursor(Qt.PointingHandCursor)
    return btn


# ---------------------------------------------------------------------------
# 任务页（原型 11 主区）
# ---------------------------------------------------------------------------

class _QueueLogSplitHandle(QSplitterHandle):
    """队列/日志分隔条手柄：居中一条 1px 细线（卡片描边色，悬停转主题强调色）。

    分隔条没有 QSS 规则（style.py 不含 QSplitter 段），默认绘制在深/浅两套
    主题下都接近「空白间隙」；这里按当前主题 token 自绘，切主题自动跟随。
    """

    def paintEvent(self, event):
        key = "ctl_focus" if self.underMouse() else "card_border"
        try:
            color = QColor(tokens().get(key))
        except Exception:
            color = self.palette().color(QPalette.Mid)
        painter = QPainter(self)
        try:
            painter.fillRect(0, max(0, (self.height() - 1) // 2),
                             self.width(), 1, color)
        finally:
            painter.end()

    def enterEvent(self, event):
        super().enterEvent(event)
        self.update()

    def leaveEvent(self, event):
        super().leaveEvent(event)
        self.update()


class _QueueLogSplitter(QSplitter):
    """队列 ⇄ 该任务日志的垂直分隔条（拖动改变两区高度）。"""

    def createHandle(self):
        return _QueueLogSplitHandle(self.orientation(), self)


class TaskPage(QWidget):
    """任务页：队列/历史表 + 失败过滤 + 「该任务日志」（当前任务 / 全部）。

    只持有状态与控件；任务/日志数据的装载由宿主 MainWindow 完成（便于打桩测试）。
    信号：taskActivated(task_id) / taskDoubleClicked(task_id) / taskDeselected() /
          actionTriggered(task_id, kind) / copyRequested() / scopeChanged(scope) /
          resultFilterChanged(failed) / logScopeChanged(scope) / refreshRequested()
    """

    taskActivated = pyqtSignal(int)
    taskDoubleClicked = pyqtSignal(int)
    taskDeselected = pyqtSignal()
    actionTriggered = pyqtSignal(int, str)
    copyRequested = pyqtSignal()
    scopeChanged = pyqtSignal(str)
    resultFilterChanged = pyqtSignal(bool)
    logScopeChanged = pyqtSignal(str)
    refreshRequested = pyqtSignal()

    def __init__(self, log_view, parent=None):
        super().__init__(parent)
        self._scope = "queue"
        self._result_failed = False
        self._log_scope = "task"
        self._current_task = None
        self._counts = {}
        self.log_view = log_view

        root = QVBoxLayout(self)
        root.setContentsMargins(12, 12, 12, 10)
        root.setSpacing(10)

        # 第一行：队列/历史 · 统计 micro · 全部/失败 · 刷新
        row = QHBoxLayout()
        row.setSpacing(10)
        self.seg_scope = SegControl(self)
        self.seg_scope.set_items([("队列 0", "queue"), ("历史 0", "history")])
        self.seg_scope.currentChanged.connect(self._on_scope)
        row.addWidget(self.seg_scope)
        self.stats_label = QLabel("", self)
        self.stats_label.setObjectName("stripHint")
        row.addWidget(self.stats_label)
        row.addStretch(1)
        self.seg_result = SegControl(self)
        self.seg_result.set_items([("全部", "all"), ("失败 0", "failed")])
        self.seg_result.currentChanged.connect(self._on_result)
        row.addWidget(self.seg_result)
        self.refresh_btn = _icon_button("refresh", "刷新", self)
        self.refresh_btn.clicked.connect(self.refreshRequested.emit)
        row.addWidget(self.refresh_btn)
        # 「更多」：菜单项全部复用本页既有信号/宿主已有处理，绝不放假动作
        self.more_btn = _icon_button("more", "更多操作", self)
        self.more_menu = QMenu(self.more_btn)
        self.act_refresh = self.more_menu.addAction("刷新")
        self.act_refresh.triggered.connect(self.refreshRequested.emit)
        self.act_copy_log = self.more_menu.addAction("复制该任务日志")
        self.act_copy_log.triggered.connect(self.copyRequested.emit)
        self.act_open_dir = self.more_menu.addAction("打开输出目录")
        self.act_open_dir.triggered.connect(lambda: self._emit_action("open_dir"))
        self.act_retry = self.more_menu.addAction("重试")
        self.act_retry.triggered.connect(lambda: self._emit_action("retry"))
        self.more_btn.setMenu(self.more_menu)
        row.addWidget(self.more_btn)
        root.addLayout(row)

        # 任务表
        self.table = TaskTable(self)
        self.table.taskActivated.connect(self.taskActivated)
        self.table.taskDoubleClicked.connect(self.taskDoubleClicked)
        self.table.actionTriggered.connect(self.actionTriggered)
        self.table_empty = _EmptyOverlay(self.table, EMPTY_TASKS)

        # 该任务日志头部
        head = QHBoxLayout()
        head.setSpacing(8)
        head.addWidget(Glyph("layers", self, 14, role="muted"))
        title = QLabel("该任务日志", self)
        title.setObjectName("sectionTitle")
        head.addWidget(title)
        self.badge = QLabel("", self)
        self.badge.setObjectName("modeBadge")       # QSS 的 token 药丸样式（随主题）
        self.badge.setAttribute(Qt.WA_TransparentForMouseEvents, True)
        self.badge.hide()
        head.addWidget(self.badge)
        head.addStretch(1)
        self.seg_logscope = SegControl(self)
        self.seg_logscope.set_items([("当前任务", "task"), ("全部", "all")])
        self.seg_logscope.currentChanged.connect(self._on_log_scope)
        head.addWidget(self.seg_logscope)
        self.retry_btn = _ghost_button("重试", self)
        self.retry_btn.clicked.connect(lambda: self._emit_action("retry"))
        head.addWidget(self.retry_btn)
        self.open_btn = _ghost_button("打开输出目录", self)
        self.open_btn.clicked.connect(lambda: self._emit_action("open_dir"))
        head.addWidget(self.open_btn)
        self.copy_btn = _ghost_button("复制", self)
        self.copy_btn.clicked.connect(self.copyRequested.emit)
        head.addWidget(self.copy_btn)

        # 日志区 = 头部行 + 日志视图，整体作为分隔条下半区
        self.log_pane = QWidget(self)
        log_lay = QVBoxLayout(self.log_pane)
        log_lay.setContentsMargins(0, 0, 0, 0)
        log_lay.setSpacing(10)              # 与原 root 间距一致（头部 → 日志）
        log_lay.addLayout(head)

        if self.log_view is None:
            self.log_view = QPlainTextEdit(self)
            self.log_view.setReadOnly(True)
        log_lay.addWidget(self.log_view, 1)
        self.log_empty = _EmptyOverlay(self.log_view, EMPTY_TASK_LOG)
        self.log_pane.setMinimumHeight(130)  # 头部 + 最小可读日志高度

        # 队列 ⇄ 日志：垂直分隔条，拖动改变两区高度（初始 1:2，同原布局）
        self.splitter = _QueueLogSplitter(Qt.Vertical, self)
        self.splitter.setObjectName("queueSplit")
        self.splitter.setChildrenCollapsible(False)
        self.splitter.setHandleWidth(10)     # 与原布局 10px 行距同宽，细线居中
        self.table.setMinimumHeight(120)
        self.splitter.addWidget(self.table)
        self.splitter.addWidget(self.log_pane)
        self.splitter.setStretchFactor(0, 1)
        self.splitter.setStretchFactor(1, 2)
        self.splitter.setSizes([1, 2])
        root.addWidget(self.splitter, 1)
        self._split_sized = False

        self._set_task_buttons(False)

    def showEvent(self, event):
        """首次显示后按原布局比例设定初始高度（表 1 : 日志 2）。"""
        super().showEvent(event)
        if not self._split_sized:
            QTimer.singleShot(0, self._apply_initial_split)

    def _apply_initial_split(self):
        """QSplitter.setSizes 只认像素：显示后按实际高度换算一次。

        日志头部行 + 其间距是固定开销，不参与 1:2 分配（与原 QVBoxLayout 的
        stretch 1/2 算法一致）；之后高度一律由用户拖动决定，不再自动改写。
        """
        if self._split_sized:
            return
        h = int(self.splitter.height())
        handle = int(self.splitter.handleWidth())
        chrome = max(0, self.log_pane.sizeHint().height()
                     - self.log_view.sizeHint().height())
        content = h - handle - chrome
        if content < 60:                      # 布局尚未定型：下次显示再试
            return
        self._split_sized = True
        table_h = max(1, content // 3)
        self.splitter.setSizes([table_h, h - handle - table_h])

    # ---- 状态读取 ----
    def scope(self):
        return self._scope

    def result_filter(self):
        return self._result_failed

    def log_scope(self):
        return self._log_scope

    def current_task(self):
        return dict(self._current_task) if isinstance(self._current_task, dict) else None

    def current_task_id(self):
        try:
            return int(self._current_task.get("id"))
        except Exception:
            return None

    def counts(self):
        return dict(self._counts)

    def log_empty_copy(self):
        if self._log_scope == "all":
            return EMPTY_LOG
        if self.current_task_id() is None:
            return EMPTY_TASK_LOG
        return EMPTY_TASK_LOG_EMPTY

    # ---- 宿主装载入口 ----
    def set_tasks(self, rows, counts=None, preserve_view=False):
        """装载任务行并尽量保持选中；选中项已不在新集合时清空选中并发 taskDeselected。

        preserve_view=True：生命周期刷新专用——除选中（本就按 id 保留）外，重装后
        再还原表格滚动位置，避免用户在盯着队列时被反复弹回顶部；用户显式刷新 /
        换范围仍走默认（重建后回顶）。
        """
        if counts is not None:
            self.set_counts(counts)
        rows = [r for r in (rows or []) if isinstance(r, dict)]
        keep = self.current_task_id()
        scroll = self.table.scroll_value() if preserve_view else None
        self.table.set_tasks(rows, scroll_to_top=not preserve_view)
        try:
            self.table_empty.set_empty(not rows)
        except Exception:
            pass
        restored = bool(keep is not None and self.table.select_task(keep))
        if preserve_view and scroll is not None:
            self.table.set_scroll_value(scroll)
        if restored:
            return                          # 保留选中：table 会发 taskActivated
        if keep is not None:
            self.set_current_task(None)
            self.taskDeselected.emit()

    def set_counts(self, counts):
        """刷新分段文案与统计 micro（队列 N / 历史 N / 失败 N / 统计行）。"""
        c = counts if isinstance(counts, dict) else {}
        self._counts = dict(c)
        self.seg_scope.set_label("queue", "队列 %d" % int(c.get("queue", 0) or 0))
        self.seg_scope.set_label("history", "历史 %d" % int(c.get("history", 0) or 0))
        self.seg_result.set_label("failed", "失败 %d" % int(c.get("failed", 0) or 0))
        parts = []
        for key, word in (("extracting", "进行中"), ("queued", "排队"),
                          ("need_password", "待密码"), ("failed", "失败")):
            n = int(c.get(key, 0) or 0)
            if n:
                parts.append("%d %s" % (n, word))
        self.stats_label.setText(" · ".join(parts))

    def set_result_filter(self, failed):
        """程序化同步失败过滤（不发信号，避免与宿主状态互相回环）。"""
        self._result_failed = bool(failed)
        self.seg_result.set_current("failed" if failed else "all")

    def set_log_scope(self, scope):
        self._log_scope = "all" if str(scope) == "all" else "task"
        self.seg_logscope.set_current(self._log_scope)

    def set_current_task(self, task, name=None):
        """设置当前任务（dict 或 None）：更新徽标与三个操作按钮的可用性。"""
        if not isinstance(task, dict):
            self._current_task = None
            self.badge.clear()
            self.badge.hide()
            self._set_task_buttons(False)
            return
        self._current_task = dict(task)
        full = str(name or task.get("file_name") or "")
        self.badge.setToolTip(full)
        try:
            self.badge.setText(self.badge.fontMetrics().elidedText(
                full, Qt.ElideMiddle, 260))
        except Exception:
            self.badge.setText(full)
        self.badge.setVisible(bool(full))
        self._set_task_buttons(self.current_task_id() is not None)

    def _set_task_buttons(self, enabled):
        for btn in (self.retry_btn, self.open_btn, self.copy_btn):
            btn.setEnabled(bool(enabled))
        for act in (self.act_open_dir, self.act_retry):
            act.setEnabled(bool(enabled))

    def set_log_empty(self, empty, text=None):
        self.log_empty.set_empty(empty, text)

    # ---- 记录分流 ----
    def accepts_record(self, record):
        """当前日志视图是否接收该记录：全部=一律；当前任务=仅同 task_id。"""
        if self._log_scope == "all":
            return True
        tid = self.current_task_id()
        if tid is None or not isinstance(record, dict):
            return False
        try:
            return int(record.get("task_id")) == tid
        except Exception:
            return False

    # ---- 内部 ----
    def _on_scope(self, data):
        self._scope = "history" if str(data) == "history" else "queue"
        self.scopeChanged.emit(self._scope)

    def _on_result(self, data):
        self._result_failed = (str(data) == "failed")
        self.resultFilterChanged.emit(self._result_failed)

    def _on_log_scope(self, data):
        self._log_scope = "all" if str(data) == "all" else "task"
        self.logScopeChanged.emit(self._log_scope)

    def _emit_action(self, kind):
        tid = self.current_task_id()
        if tid is not None:
            self.actionTriggered.emit(tid, kind)


# ---------------------------------------------------------------------------
# 运行日志页（原型 13）
# ---------------------------------------------------------------------------

class LogPage(QWidget):
    """运行日志页：路径多选 + 级别 + 搜索三滤镜（可叠加），右栏统计与「需要处理」。

    信号：filtersChanged()（宿主据此重查 db）/ taskActivated(task_id) /
          actionTriggered(task_id, kind) / notice(text)（导出等用户可见回执）。
    """

    filtersChanged = pyqtSignal()
    taskActivated = pyqtSignal(int)
    actionTriggered = pyqtSignal(int, str)
    notice = pyqtSignal(str)

    def __init__(self, render_to=None, parent=None):
        super().__init__(parent)
        self._render_to = render_to          # callable(view, msg)：复用宿主的着色/链接管线
        self._records = []
        self._value_labels = {}
        self._danger_labels = []

        root = QVBoxLayout(self)
        root.setContentsMargins(12, 12, 12, 10)
        root.setSpacing(10)

        # 路径筛选条（多选；状态灯与选中态解耦由控件自身保证）
        self.filters = FilterChipStrip(self)
        self.filters.selectionChanged.connect(lambda _s: self.filtersChanged.emit())
        root.addWidget(self.filters)

        # 级别 + 搜索 + 自动滚动 + 导出/清空
        row = QHBoxLayout()
        row.setSpacing(8)
        self.seg_level = SegControl(self)
        self.seg_level.set_items([("全部 0", "all"), ("信息 0", "info"),
                                  ("警告 0", "wait"), ("错误 0", "error")])
        self.seg_level.currentChanged.connect(lambda _d: self.filtersChanged.emit())
        row.addWidget(self.seg_level)
        self.search = QLineEdit(self)
        self.search.setPlaceholderText("搜索文件名 / 路径 / 密码")
        self.search.setFixedWidth(230)
        self._search_timer = QTimer(self)
        self._search_timer.setSingleShot(True)
        self._search_timer.setInterval(250)
        self._search_timer.timeout.connect(self.filtersChanged.emit)
        self.search.textChanged.connect(lambda _t: self._search_timer.start())
        row.addWidget(self.search)
        row.addStretch(1)
        self.auto_scroll = QCheckBox("自动滚动", self)
        self.auto_scroll.setChecked(True)
        row.addWidget(self.auto_scroll)
        self.export_btn = _ghost_button("导出", self)
        self.export_btn.clicked.connect(self._on_export)
        row.addWidget(self.export_btn)
        self.clear_btn = _ghost_button("清空", self)
        self.clear_btn.clicked.connect(self._on_clear)
        row.addWidget(self.clear_btn)
        root.addLayout(row)

        # 日志视图 + 右栏（248px）
        main = QHBoxLayout()
        main.setSpacing(12)
        self.log_view = QPlainTextEdit(self)
        self.log_view.setReadOnly(True)
        self.log_view.setMaximumBlockCount(3000)
        main.addWidget(self.log_view, 1)
        self.log_empty = _EmptyOverlay(self.log_view, EMPTY_LOG)

        self.right_host = QWidget(self)
        self.right_host.setFixedWidth(248)
        right = QVBoxLayout(self.right_host)
        right.setContentsMargins(0, 0, 0, 0)
        right.setSpacing(10)
        right.addWidget(self._build_today_card())
        self.needs = NeedsAttentionCard(self.right_host)
        self.needs.taskActivated.connect(self.taskActivated)
        self.needs.actionTriggered.connect(self.actionTriggered)
        self.needs.set_empty_text(EMPTY_NEEDS)   # 空态渲染在卡片内部（M3-QA 修复）
        # 兼容旧属性：空态标签现在属于卡片（不再浮在卡片边框之外）
        self.needs_empty = self.needs.empty_label
        right.addWidget(self.needs, 1)
        main.addWidget(self.right_host)
        root.addLayout(main, 1)
        self.refresh_theme()

    def _build_today_card(self):
        card = QFrame(self)
        card.setObjectName("card")
        lay = QVBoxLayout(card)
        lay.setContentsMargins(14, 12, 14, 12)
        lay.setSpacing(5)
        head = QHBoxLayout()
        head.setSpacing(8)
        head.addWidget(Glyph("dashboard", card, 13, role="muted"))
        title = QLabel("今日", card)
        title.setObjectName("sectionTitle")
        head.addWidget(title)
        head.addStretch(1)
        lay.addLayout(head)
        rows = (("done", "今日解压", False), ("pwd", "密码命中", False),
                ("failed", "失败", True), ("trail", "回收站待还原", False))
        for i, (key, text, danger) in enumerate(rows):
            if i:
                sep = QFrame(card)
                sep.setObjectName("needSep")
                sep.setFixedHeight(1)
                lay.addWidget(sep)
            line = QHBoxLayout()
            line.setSpacing(8)
            label = QLabel(text, card)
            label.setObjectName("needNote")
            line.addWidget(label)
            line.addStretch(1)
            value = QLabel("0", card)
            value.setObjectName("needCount")
            if danger:
                self._danger_labels.append(value)
            self._value_labels[key] = value
            line.addWidget(value)
            lay.addLayout(line)
        return card

    # ---- 过滤器读取（宿主查询 db 用）----
    def levels(self):
        return LOG_LEVEL_BUCKETS.get(str(self.seg_level.current()))

    def sources(self):
        try:
            return set(self.filters.selected_keys())
        except Exception:
            return set()

    def keyword(self):
        return str(self.search.text()).strip()

    def filter_records(self, rows, levels=None, sources=None, keyword=None):
        """按当前（或显式传入的）三滤镜过滤行；全局行（source_dir is None）永不被路径挡住。"""
        lv = self.levels() if levels is None else levels
        src = self.sources() if sources is None else sources
        kw = self.keyword() if keyword is None else keyword
        return [r for r in (rows or [])
                if isinstance(r, dict) and log_matches(r, lv, src, kw)]

    # ---- 装载 / 追加 ----
    def set_paths(self, entries):
        """重建路径筛选 chip（保留仍然存在的选中路径），状态灯用 entry 的 state。"""
        try:
            old = set(self.filters.selected_keys())
        except Exception:
            old = set()
        chips = []
        for e in entries or []:
            if not isinstance(e, dict):
                continue
            path = str(e.get("path") or "").strip()
            if path:
                chips.append((path, path))
        self.filters.set_chips(chips)
        for e in entries or []:
            if isinstance(e, dict) and str(e.get("path") or "").strip():
                self.update_dir_state(str(e["path"]),
                                      e.get("state") or "listening")
        for chip in self.filters.chips():
            if chip.key in old:
                chip.set_active(True)

    def update_dir_state(self, path_key, state):
        try:
            self.filters.set_state(str(path_key), state)
        except Exception:
            pass

    def reload(self, rows):
        """按「路径+搜索」取基础集算级别计数，再按「级别」过滤后渲染（时间正序）。"""
        rows = [r for r in (rows or []) if isinstance(r, dict)]
        base = [r for r in rows
                if log_matches(r, None, self.sources(), self.keyword())]
        self.set_level_counts(base)
        picked = [r for r in base
                  if log_matches(r, self.levels(), None, None)]
        picked.reverse()                      # db 返回倒序 -> 展示用正序（最新在底）
        self._records = picked
        self.log_view.clear()
        for r in picked:
            self._render_line(task_log_line(r))
        self.log_empty.set_empty(not picked)
        if picked and self.auto_scroll.isChecked():
            self.scroll_bottom()
        return picked

    def set_level_counts(self, rows):
        """按基础集刷新级别分段计数（全部/信息/警告/错误）。"""
        counts = {"all": 0, "info": 0, "wait": 0, "error": 0}
        for r in rows or []:
            if not isinstance(r, dict):
                continue
            counts["all"] += 1
            counts[level_bucket(r.get("level"))] += 1
        self.seg_level.set_label("all", "全部 %d" % counts["all"])
        self.seg_level.set_label("info", "信息 %d" % counts["info"])
        self.seg_level.set_label("wait", "警告 %d" % counts["wait"])
        self.seg_level.set_label("error", "错误 %d" % counts["error"])

    def append_line(self, msg):
        """活日志：按当前级别/路径/搜索判定后追加（宿主在 _append_log 里调用）。"""
        self._render_line(msg)
        if self.auto_scroll.isChecked():
            self.scroll_bottom()

    def accepts_record(self, record, msg=""):
        """该记录是否应显示（级别+路径+搜索三滤镜，全局行永不被路径挡住）。"""
        if not isinstance(record, dict):
            record = {"text": str(msg), "level": guess_level(str(msg))}
        return log_matches(record, self.levels(), self.sources(), self.keyword())

    def set_stats(self, stats):
        s = stats if isinstance(stats, dict) else {}
        for key, label in self._value_labels.items():
            try:
                label.setText(str(int(s.get(key, 0) or 0)))
            except Exception:
                label.setText("0")

    def set_needs(self, items):
        items = [it for it in (items or []) if isinstance(it, dict)]
        self.needs.set_items(items)      # 卡片内部同时切换空态显隐

    def scroll_bottom(self):
        try:
            sb = self.log_view.verticalScrollBar()
            sb.setValue(sb.maximum())
        except Exception:
            pass

    def refresh_theme(self):
        """主题切换后重贴内联色（失败数字用 danger 色，来自 PALETTE）。"""
        for label in self._danger_labels:
            try:
                label.setStyleSheet("color: %s;" % PALETTE["danger"])
            except Exception:
                pass

    # ---- 内部 ----
    def _render_line(self, msg):
        if self._render_to is not None:
            try:
                self._render_to(self.log_view, msg)
                return
            except Exception:
                pass
        self.log_view.appendPlainText(str(msg))

    def _on_clear(self):
        self._records = []
        self.log_view.clear()
        self.log_empty.set_empty(True)

    def _on_export(self):
        try:
            default = time.strftime("autounpacker-log-%Y%m%d-%H%M%S.txt")
            path, _sel = QFileDialog.getSaveFileName(
                self, "导出运行日志", default, "文本文件 (*.txt)")
            if not path:
                return
            with open(path, "w", encoding="utf-8") as f:
                f.write(self.log_view.toPlainText())
            self.notice.emit("运行日志已导出: %s" % path)
        except Exception as e:
            try:
                QMessageBox.warning(self, "导出运行日志", "导出失败：%s" % e)
            except Exception:
                pass


# ---------------------------------------------------------------------------
# 底栏（原型 11 底栏）：左状态区 + 右纵向播报
# ---------------------------------------------------------------------------

class StatusBar(QWidget):
    """底栏：左「监听状态 · 轮询 · 进行中(细进度) · 失败聚合 · 快捷键」+ 右纵向播报。

    失败块在 failed == 0 时整块隐藏；点击发 failRequested（与任务页「失败」分段
    共用同一状态，由宿主 MainWindow 统一翻转）。
    """

    failRequested = pyqtSignal()

    def __init__(self, tips=None, parent=None):
        # 容错：允许把 parent 当第一个位置参数传入（QWidget 风格的调用习惯）
        if isinstance(tips, QWidget) and parent is None:
            parent, tips = tips, None
        super().__init__(parent)
        self.setObjectName("statusBar")
        self.setAttribute(Qt.WA_StyledBackground, True)
        self.setSizePolicy(QSizePolicy.Preferred, QSizePolicy.Fixed)
        self._paused = False

        lay = QHBoxLayout(self)
        lay.setContentsMargins(12, 5, 12, 5)
        lay.setSpacing(6)
        self.dot = QLabel("●", self)
        lay.addWidget(self.dot)
        self.run_label = QLabel("监听中", self)
        lay.addWidget(self.run_label)
        self.sep1 = QLabel("·", self)
        lay.addWidget(self.sep1)
        self.poll_label = QLabel("轮询 2s", self)
        lay.addWidget(self.poll_label)
        self.sep2 = QLabel("·", self)
        lay.addWidget(self.sep2)

        # 进行中：细进度条 + 百分比（无任务整段隐藏；ratio=None 为忙碌态）
        self.prog_zone = QWidget(self)
        pz = QHBoxLayout(self.prog_zone)
        pz.setContentsMargins(0, 0, 0, 0)
        pz.setSpacing(6)
        self.prog_label = QLabel("进行中", self.prog_zone)
        pz.addWidget(self.prog_label)
        self.progress = QProgressBar(self.prog_zone)
        self.progress.setObjectName("thinProg")
        self.progress.setTextVisible(False)
        self.progress.setRange(0, 100)
        self.progress.setFixedWidth(64)
        pz.addWidget(self.progress)
        self.pct_label = QLabel("", self.prog_zone)
        pz.addWidget(self.pct_label)
        lay.addWidget(self.prog_zone)
        self.sep3 = QLabel("·", self)
        lay.addWidget(self.sep3)

        self.fail_btn = QPushButton("", self)
        self.fail_btn.setObjectName("danger")
        self.fail_btn.setFixedHeight(20)   # 药丸定高；style.py 的 #statusBar 紧凑内边距保证 20px 内装得下文字
        self.fail_btn.setCursor(Qt.PointingHandCursor)
        self.fail_btn.clicked.connect(self.failRequested.emit)
        lay.addWidget(self.fail_btn)
        self.sep4 = QLabel("·", self)
        lay.addWidget(self.sep4)
        self.hotkey_label = QLabel("", self)
        lay.addWidget(self.hotkey_label)
        lay.addStretch(1)

        self.tip_icon = Glyph("bolt", self, 13, role="accent")
        lay.addWidget(self.tip_icon)
        self.tip_label = QLabel("使用提示", self)
        lay.addWidget(self.tip_label)
        self.ticker = StatusTipTicker(list(tips) if tips else list(DEFAULT_TIPS), self)
        lay.addWidget(self.ticker)

        for lbl in (self.sep1, self.sep2, self.sep3, self.sep4, self.poll_label,
                    self.pct_label, self.prog_label, self.hotkey_label, self.tip_label):
            lbl.setObjectName("stripHint")
        self.set_paused(False)
        self.clear_progress()
        self.set_failed_count(0)
        self.set_hotkey("")
        self.refresh_theme()

    # ---- 左状态区 ----
    def set_paused(self, paused):
        self._paused = bool(paused)
        self.run_label.setText("已暂停" if self._paused else "监听中")
        self._apply_dot()

    def _apply_dot(self):
        color = PALETTE["muted"] if self._paused else PALETTE["success"]
        try:
            self.dot.setStyleSheet("color: %s; font-size: 11px;" % color)
        except Exception:
            pass

    def set_poll_interval(self, secs):
        try:
            secs = max(1, int(secs))
        except Exception:
            secs = 2
        self.poll_label.setText("轮询 %ds" % secs)

    def set_progress(self, ratio=None):
        """显示进行中段：ratio 为 0..1 的浮点；None 表示忙碌（不确定进度）。"""
        self.prog_zone.setVisible(True)
        if ratio is None:
            self.progress.setRange(0, 0)
            self.pct_label.setText("")
        else:
            try:
                pct = max(0, min(100, int(round(float(ratio) * 100))))
            except Exception:
                pct = 0
            self.progress.setRange(0, 100)
            self.progress.setValue(pct)
            self.pct_label.setText("%d%%" % pct)
        self._sync_seps()

    def clear_progress(self):
        self.prog_zone.setVisible(False)
        self.progress.setRange(0, 100)
        self.progress.setValue(0)
        self.pct_label.setText("")
        self._sync_seps()

    def set_failed_count(self, count):
        try:
            n = max(0, int(count))
        except Exception:
            n = 0
        self.fail_btn.setText("⚠ %d 个失败 ›" % n)
        self.fail_btn.setVisible(n > 0)              # 0 时整块隐藏
        self._sync_seps()

    def set_hotkey(self, hotkey):
        """运行期拼装「快捷键 <config>」并把热键传入播报末句。"""
        hk = str(hotkey or "").strip()
        self.hotkey_label.setText(("快捷键 " + hk) if hk else "")
        self.hotkey_label.setVisible(bool(hk))
        try:
            self.ticker.set_hotkey(hk)
        except Exception:
            pass
        self._sync_seps()

    def _sync_seps(self):
        # isVisibleTo(self)：窗口尚未显示（隐藏在托盘）时也能算出正确的中点显隐
        has_prog = self.prog_zone.isVisibleTo(self)
        has_fail = self.fail_btn.isVisibleTo(self)
        has_hk = bool(self.hotkey_label.text())
        self.sep1.setVisible(True)
        self.sep2.setVisible(has_prog)
        self.sep3.setVisible(has_prog or has_fail)
        self.sep4.setVisible(has_fail or has_hk)

    def refresh_theme(self):
        """主题切换后重贴：灯色（PALETTE）与顶部描边（token）。"""
        self._apply_dot()
        try:
            border = tokens().get("card_border", "")
            self.setStyleSheet(
                "QWidget#statusBar { border-top: 1px solid %s; }" % border)
        except Exception:
            pass
