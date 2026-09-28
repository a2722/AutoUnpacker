# -*- coding: utf-8 -*-
"""CODE 页：分享缺提取码时在本窗换页填码（不再弹 `ShareCodeAskDialog` 浮窗）。

字段 / 校验 / 文案 / 快捷键与 `ui/dialogs/share_ask.py` 的 `ShareCodeAskDialog`
逐条一致（顶部 `‹ 返回`、标题、`剩余 Ns`、分享者、链接中部省略 + hover 全链、
`提取码（4 位）`、仅字母数字 maxLength=4、满 4 位才可提交、提示语满 4 位变绿 /
非法变红、主按钮 `本次使用（Alt+2）`、次按钮 `忽略`）。

红线（规格 §0 D4）：**不得出现**「同时存入永久口令本」或任何保存入口；
提取码 ≠ 压缩包口令，两者不得关联。

时序（与现浮窗一致）：
- 提交 `once` / `ignore`：调用宿主同一个决策回调
  `host._on_share_code_decision(kind, code, url, surl, share_uk)`；
- 120s 归零（`SHARE_ASK_TIMEOUT_SEC`）：判为忽略，**不回调**，只发 finished；
- `finished` 由 `CompactWindow` 接住并「回 HOME 且历史重置为 [HOME]」。
"""
import re

from PyQt5.QtCore import Qt, QRegularExpression, QTimer, pyqtSignal
from PyQt5.QtGui import QKeySequence, QRegularExpressionValidator
from PyQt5.QtWidgets import (QHBoxLayout, QLabel, QPushButton, QShortcut,
                             QVBoxLayout, QWidget)

from ..dialogs.common import (SHARE_ASK_TIMEOUT_SEC, _CodeLineEdit,
                              _call_decision)


class CodePage(QWidget):
    """提取码页：单例复用（由 `CompactWindow.enter_code` 判定），结束发 finished。"""

    finished = pyqtSignal()

    def __init__(self, host, parent=None):
        super().__init__(parent)
        self._host = host
        self.setObjectName("compactCodePage")

        self.surl = ""
        self.url = ""
        self.share_uk = ""
        self.force_pick = False          # Alt+3 挑选手势：提交后仍要继续「选择文件」
        self._loaded = False             # 是否已装载过目标（用于单例判定）
        self._done = False               # 已提交 / 已忽略
        self._timed_out = False
        self._emitted = False            # finished 只发一次
        try:
            self._timeout_sec = max(1, int(SHARE_ASK_TIMEOUT_SEC))
        except Exception:
            self._timeout_sec = 120
        self._remain_sec = self._timeout_sec

        root = QVBoxLayout(self)
        # 版面压缩（本轮）：上下边距 10/12→6/6、行距 8→3——CODE 页原本是整窗最小高
        # （269px）的来源，瘦身后小窗才降得下来；控件自身高度（含文字的）一个没动。
        root.setContentsMargins(8, 6, 8, 6)
        root.setSpacing(3)

        # 顶部：‹ 返回（= 忽略并回 HOME）+ 标题 + 右侧倒计时
        head = QHBoxLayout()
        head.setSpacing(6)
        self.back_btn = QPushButton("‹ 返回", self)
        self.back_btn.setObjectName("compactBack")
        self.back_btn.setCursor(Qt.PointingHandCursor)
        self.back_btn.setFocusPolicy(Qt.NoFocus)
        self.back_btn.setToolTip("忽略并返回主界面（Esc）")
        self.back_btn.clicked.connect(lambda *_: self.request_ignore())
        head.addWidget(self.back_btn)
        title = QLabel("分享缺提取码", self)
        title.setObjectName("compactPageTitle")
        head.addWidget(title)
        head.addStretch(1)
        self.timeout_label = QLabel("剩余 %ds" % self._remain_sec, self)
        self.timeout_label.setObjectName("compactTimeout")
        head.addWidget(self.timeout_label)
        root.addLayout(head)

        self.meta_label = QLabel("分享者：未知", self)
        self.meta_label.setObjectName("compactMeta")
        self.meta_label.setTextInteractionFlags(Qt.TextSelectableByMouse)
        root.addWidget(self.meta_label)

        # 链接太长时中间截断，完整链接放 tooltip（可选中复制）
        self.url_label = QLabel("链接：", self)
        self.url_label.setObjectName("compactUrl")
        self.url_label.setTextInteractionFlags(Qt.TextSelectableByMouse)
        self.url_label.setMinimumWidth(0)     # 窄窗时按宽度重新省略，不顶宽整窗
        root.addWidget(self.url_label)

        code_cap = QLabel("提取码（4 位）", self)
        code_cap.setObjectName("compactField")
        root.addWidget(code_cap)

        self.code_edit = _CodeLineEdit(self)
        self.code_edit.setPlaceholderText("请输入 4 位提取码")
        self.code_edit.setMaxLength(4)
        self.code_edit.setValidator(QRegularExpressionValidator(
            QRegularExpression("[A-Za-z0-9]{0,4}"), self))
        root.addWidget(self.code_edit)

        self.hint_label = QLabel("请输入 4 位提取码（字母或数字）", self)
        self.hint_label.setObjectName("compactHint")
        self.hint_label.setWordWrap(True)
        root.addWidget(self.hint_label)

        self.once_btn = QPushButton("本次使用（Alt+2）", self)
        self.once_btn.setObjectName("primary")
        self.once_btn.setCursor(Qt.PointingHandCursor)
        self.once_btn.clicked.connect(lambda *_: self._on_once_clicked())
        root.addWidget(self.once_btn)

        foot = QHBoxLayout()
        foot.addStretch(1)
        self.ignore_btn = QPushButton("忽略", self)
        self.ignore_btn.setCursor(Qt.PointingHandCursor)
        self.ignore_btn.clicked.connect(lambda *_: self._on_ignore_clicked())
        foot.addWidget(self.ignore_btn)
        root.addLayout(foot)
        root.addStretch(1)

        # 码无效时下载按钮置灰（有效即恢复），行内提示随状态变色
        self.code_edit.textChanged.connect(self._refresh_state)
        self._refresh_state()

        # Alt+2：鼠标路径的键盘等价（仅本页激活时生效，不注册全局热键）
        self._sc_once = QShortcut(QKeySequence("Alt+2"), self)
        self._sc_once.setContext(Qt.WidgetWithChildrenShortcut)
        self._sc_once.activated.connect(self._on_once_clicked)

        # 逐秒倒计时：单个 1s 重复 QTimer；到点走 _on_timeout（只回 HOME、不回调）
        self._timer = QTimer(self)
        self._timer.setInterval(1000)
        self._timer.setSingleShot(False)
        self._timer.timeout.connect(self._tick)

    # ---- 生命周期（由 CompactWindow.enter_code / leave_code 驱动）----
    def load(self, surl, url, share_uk, force_pick=False, reset_input=True):
        """装载目标分享：更新字段；`reset_input=True` 时清空输入框。

        单例复用时（`reset_input=False`）**不覆盖**用户已填内容（与现浮窗
        「同分享复用不覆盖」一致）。
        """
        self.surl = str(surl or "").strip()
        self.url = str(url or "").strip()
        self.share_uk = str(share_uk or "").strip()
        self.force_pick = bool(force_pick)
        self._loaded = True
        self._done = False
        self._timed_out = False
        self._emitted = False
        self.meta_label.setText("分享者：%s" % (self.share_uk or "未知"))
        self._refresh_url_label()
        if reset_input:
            self.code_edit.setText("")
        self._refresh_state()

    def start(self):
        """重置并启动 120s 倒计时（复用场景 = 重新计时）。"""
        self._loaded = True
        self._done = False
        self._timed_out = False
        self._emitted = False
        self._remain_sec = self._timeout_sec
        self._update_countdown()
        self._timer.start()
        try:
            self.code_edit.setFocus()
        except Exception:
            pass

    def stop(self):
        """只停倒计时（不改变状态；供窗口隐藏 / 关闭时调用）。"""
        try:
            self._timer.stop()
        except Exception:
            pass

    def abandon(self):
        """外部结束本页（share_used / share_dead / 宿主主动收回）：静默作废。

        不发 finished（调用方已知道结果），也绝不再回调。
        """
        self._done = True
        self._emitted = True
        self.stop()

    def is_active(self):
        """是否存在未结束的取码请求（用于单例复用判定）。"""
        return bool(self._loaded and not self._done and not self._timed_out)

    def request_ignore(self):
        """Esc / `‹ 返回` / `忽略` 的统一入口：忽略并回 HOME。"""
        self._on_ignore_clicked()

    # ---- 只读访问器（命名与现浮窗一致，便于宿主接线）----
    def current_code(self):
        """返回框内通过校验的 4 位码（字母/数字），否则返回空串。"""
        txt = self.code_edit.text().strip()
        if getattr(self.code_edit, "is_overlong", lambda: False)():
            return ""
        if re.fullmatch(r"[A-Za-z0-9]{4}", txt):
            return txt
        return ""

    def target_surl(self):
        return self.surl

    def target_uk(self):
        return self.share_uk

    # ---- 状态刷新 / 按钮入口 ----
    def _refresh_state(self):
        """按框内内容刷新下载按钮可用态与行内提示（码无效即置灰）。"""
        raw = self.code_edit.text().strip()
        valid = bool(self.current_code())
        self.once_btn.setEnabled(valid)
        try:
            if not raw:
                self._set_hint("请输入 4 位提取码（字母或数字）", "")
            elif valid:
                self._set_hint("提取码格式有效", "ok")
            else:
                self._set_hint("提取码需为 4 位字母或数字", "bad")
        except Exception:
            pass

    def _set_hint(self, text, state):
        """行内提示：state ∈ {"", "ok", "bad"}；颜色由窗口局部 QSS 的属性规则给。"""
        self.hint_label.setText(text)
        self.hint_label.setProperty("state", state)
        try:
            from ..widgets.common import repolish
            repolish(self.hint_label)
        except Exception:
            pass

    def _submit(self, kind):
        """按钮统一入口：码有效才提交（无效仅刷新提示，不回调）。"""
        if self._done or self._timed_out:
            return
        code = self.current_code()
        if not code:
            self._refresh_state()
            return
        self._finish(kind, code)

    def _on_once_clicked(self):
        self._submit("once")

    def _on_ignore_clicked(self):
        if self._done or self._timed_out:
            return
        self._finish("ignore", "")

    # ---- 倒计时 / 超时 ----
    def _update_countdown(self):
        try:
            self.timeout_label.setText("剩余 %ds" % self._remain_sec)
        except Exception:
            pass

    def _tick(self):
        """逐秒递减；归零即走超时（回 HOME + 丢弃，绝不回调）。"""
        if self._done or self._timed_out:
            return
        self._remain_sec = max(0, self._remain_sec - 1)
        self._update_countdown()
        if self._remain_sec <= 0:
            self._on_timeout()

    def _on_timeout(self):
        """到点：停表、记一行日志、作废——绝不回调决策（与现浮窗一致）。"""
        if self._done or self._timed_out:
            return
        self._timed_out = True
        self._done = True
        self._remain_sec = 0
        self._update_countdown()
        self.stop()
        try:
            hub = getattr(self._host, "hub", None)
            if hub is not None:
                hub.log("分享询问超时(%ds)，已关闭丢弃: %s"
                        % (self._timeout_sec, self.surl))
        except Exception:
            pass
        self._emit_finished()

    def _finish(self, kind, code):
        """统一出口：同一决策回调只回调一次，并停掉倒计时。"""
        if self._done:
            return
        self._done = True
        self.stop()
        # force_pick 语义保留：Alt+3 触发时，提交后宿主仍要继续「选择要下载的文件」。
        # 宿主回调内部读的就是这个既有标记（main_window._on_share_code_decision）。
        if kind == "once" and self.force_pick:
            try:
                setattr(self._host, "_share_ask_force_pick", True)
            except Exception:
                pass
        cb = getattr(self._host, "_on_share_code_decision", None)
        if callable(cb):
            try:
                _call_decision(cb, kind, code, self.url, self.surl, self.share_uk)
            except Exception:
                pass
        self._emit_finished()

    def _emit_finished(self):
        if self._emitted:
            return
        self._emitted = True
        try:
            self.finished.emit()
        except Exception:
            pass

    # ---- 链接中部省略（窗口宽度可变，故按实际宽度重算）----
    def _refresh_url_label(self):
        try:
            avail = max(120, int(self.width()) - 24)
            elided = self.fontMetrics().elidedText(self.url, Qt.ElideMiddle, avail)
            self.url_label.setText("链接：" + elided)
            self.url_label.setToolTip(self.url)
        except Exception:
            try:
                self.url_label.setText("链接：" + self.url)
                self.url_label.setToolTip(self.url)
            except Exception:
                pass

    def resizeEvent(self, event):
        super().resizeEvent(event)
        self._refresh_url_label()
