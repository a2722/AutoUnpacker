# -*- coding: utf-8 -*-
"""ShareCodeAskDialog：分享缺提取码时贴主窗右缘的非阻塞取码小窗（120s 到点关闭作废）。"""
import re

from PyQt5.QtWidgets import (QWidget, QVBoxLayout, QHBoxLayout, QLabel,
                             QPushButton, QDialog, QFrame, QApplication,
                             QShortcut)
from PyQt5.QtCore import (Qt, QTimer, QRect, QRegularExpression, QEvent,
                          QPoint, QPropertyAnimation, QEasingCurve)
from PyQt5.QtGui import QKeySequence, QRegularExpressionValidator

from ..style import PALETTE
from .common import (SHARE_ASK_TIMEOUT_SEC, SHARE_ASK_EDGE_MARGIN,
                     SHARE_ASK_WINDOW_WIDTH, _call_decision, _CodeLineEdit)


class ShareCodeAskDialog(QDialog):
    """分享缺提取码时贴在主窗右缘的非阻塞取码小窗（120s 到点自动关闭丢弃）。

    识别到分享链接、但剪贴板附近没有有效提取码时，由调用方用 show() 展示：
    挂在**主窗上的子工具窗**（Qt.Tool | FramelessWindowHint，不做全局置顶），
    贴着主窗右缘滑出、跟随主窗移动/缩放；主窗隐藏/最小化时一并隐藏——那时
    调用方根本不会创建本窗（改为日志 + 托盘气泡，见 main_window）。不进任务栏、
    不抢焦点（不 raise_() / 不 activateWindow()，且设 WA_ShowWithoutActivating）。
    默认 120 秒倒计时，逐秒可见（「剩余 Ns」）；到点只关闭并丢弃框内内容，绝不
    自动用框里的码发起任何操作。

    三个按钮即回调词表（语义见下），一律经 _finish 恰好回调一次，回调不向外抛异常：
      「本次使用」   -> on_decision("once", code)
      「绑定并下载」 -> on_decision("mapped", code)
      「忽略」/关闭  -> on_decision("ignore", "")

    只读访问器（供接线侧读取本窗当前状态）：
      current_code() -> 通过校验的 4 位码，否则 ""
      target_surl() / target_uk() -> 本窗对应的 surl / share_uk
    本类只发回调、绝不写库；持久化由调用方负责。
    """

    def __init__(self, parent, surl, url, share_uk, mapped_code="",
                 timeout_sec=SHARE_ASK_TIMEOUT_SEC, on_decision=None,
                 state=None, hub=None):
        # UX-5：挂到传进来的主窗上（子工具窗，随主窗移动/隐藏）。非 QWidget
        # （既有测试桩）按无父处理；无父时几何退回旧「贴屏幕右缘」行为。
        p = parent if isinstance(parent, QWidget) else None
        super().__init__(p)
        self.surl = str(surl or "").strip()
        self.url = str(url or "").strip()
        self.share_uk = str(share_uk or "").strip()
        self.on_decision = on_decision   # 由调用方注入：def (kind, code[, url, surl, uk])
        self._state = state
        self._hub = hub
        self._done = False
        self._timed_out = False
        try:
            self._timeout_sec = max(1, int(timeout_sec))
        except Exception:
            self._timeout_sec = SHARE_ASK_TIMEOUT_SEC
        self._remain_sec = self._timeout_sec

        self.setWindowTitle("分享缺提取码")
        # 子工具窗 + 无边框：父窗存在时始终位于主窗之上（但不越过整个桌面）；
        # UX-5 明确去掉 WindowStaysOnTopHint。不抢焦点：不 raise_/activateWindow，
        # 且 WA_ShowWithoutActivating 保证 show() 不激活本窗。
        self.setWindowFlags(Qt.Tool | Qt.FramelessWindowHint)
        self.setAttribute(Qt.WA_ShowWithoutActivating, True)
        self.setModal(False)   # 沿用本项目「非阻塞提示」做法，绝不 exec_()

        root = QVBoxLayout(self)
        root.setContentsMargins(0, 0, 0, 0)
        root.setSpacing(0)
        card = QFrame()
        card.setObjectName("card")
        root.addWidget(card)

        lay = QVBoxLayout(card)
        lay.setContentsMargins(12, 10, 12, 12)
        lay.setSpacing(8)

        # 标题行：标题 + 逐秒倒计时 + 关闭（等价「忽略」）
        head = QHBoxLayout()
        head.setSpacing(6)
        title = QLabel("分享缺提取码")
        title.setStyleSheet(
            f"color: {PALETTE['accent_text']}; font-weight: bold; font-size: 14px;")
        head.addWidget(title)
        head.addStretch(1)
        self.timeout_label = QLabel(f"剩余 {self._remain_sec}s")
        self.timeout_label.setStyleSheet(
            f"color: {PALETTE['muted']}; font-size: 12px;")
        head.addWidget(self.timeout_label)
        close_btn = QPushButton("✕")
        close_btn.setFixedSize(22, 22)
        close_btn.setToolTip("忽略并关闭（Esc）")
        close_btn.setStyleSheet(
            "QPushButton { border: none; background: transparent; padding: 0;"
            f" color: {PALETTE['muted']}; font-size: 13px; }}"
            f"QPushButton:hover {{ color: {PALETTE['danger']}; }}")
        close_btn.clicked.connect(self._on_ignore_clicked)
        head.addWidget(close_btn)
        lay.addLayout(head)

        meta = QLabel(f"分享者：{self.share_uk or '未知'}")
        meta.setStyleSheet(f"color: {PALETTE['muted2']}; font-size: 12px;")
        meta.setTextInteractionFlags(Qt.TextSelectableByMouse)
        lay.addWidget(meta)

        # 链接太长时中间截断，完整链接放 tooltip（可选中复制）
        url_label = QLabel()
        url_label.setTextInteractionFlags(Qt.TextSelectableByMouse)
        url_label.setStyleSheet(f"color: {PALETTE['muted']}; font-size: 12px;")
        url_label.setText("链接：" + self.fontMetrics().elidedText(
            self.url, Qt.ElideMiddle, SHARE_ASK_WINDOW_WIDTH - 60))
        url_label.setToolTip(self.url)
        lay.addWidget(url_label)

        code_cap = QLabel("提取码（4 位）")
        code_cap.setStyleSheet(
            f"color: {PALETTE['muted2']}; font-size: 12px;")
        lay.addWidget(code_cap)

        self.code_edit = _CodeLineEdit()
        self.code_edit.setPlaceholderText("请输入 4 位提取码")
        self.code_edit.setMaxLength(4)
        self.code_edit.setValidator(QRegularExpressionValidator(
            QRegularExpression("[A-Za-z0-9]{0,4}"), self))
        prefill = str(mapped_code or "").strip()
        if prefill:
            self.code_edit.setText(prefill)
        lay.addWidget(self.code_edit)

        self.hint_label = QLabel("请输入 4 位提取码（字母或数字）")
        self.hint_label.setWordWrap(True)
        self.hint_label.setStyleSheet(
            f"color: {PALETTE['muted']}; font-size: 12px;")
        lay.addWidget(self.hint_label)

        self.once_btn = QPushButton("本次使用（Alt+2）")
        self.once_btn.setObjectName("primary")
        self.once_btn.clicked.connect(self._on_once_clicked)
        lay.addWidget(self.once_btn)

        self.mapped_btn = QPushButton("绑定并下载（Alt+3）")
        self.mapped_btn.clicked.connect(self._on_mapped_clicked)
        lay.addWidget(self.mapped_btn)

        foot = QHBoxLayout()
        foot.addStretch(1)
        self.ignore_btn = QPushButton("忽略")
        self.ignore_btn.clicked.connect(self._on_ignore_clicked)
        foot.addWidget(self.ignore_btn)
        lay.addLayout(foot)

        # 码无效时两个下载按钮置灰（有效即恢复），行内提示随状态变色
        self.code_edit.textChanged.connect(self._refresh_state)
        self._refresh_state()

        # Alt+2 / Alt+3：鼠标路径的键盘等价（仅本窗激活时生效，不注册全局热键）
        QShortcut(QKeySequence("Alt+2"), self).activated.connect(
            self._on_once_clicked)
        QShortcut(QKeySequence("Alt+3"), self).activated.connect(
            self._on_mapped_clicked)

        # 逐秒倒计时：单个 1s 重复 QTimer；到点走 _on_timeout（只关闭、不回调）
        self._timer = QTimer(self)
        self._timer.setInterval(1000)
        self._timer.setSingleShot(False)
        self._timer.timeout.connect(self._tick)
        self._timer.start()

        self.setFixedWidth(SHARE_ASK_WINDOW_WIDTH)
        # UX-5：跟随父窗 Move/Resize 的锚定状态（showEvent 安装、hideEvent 卸载，
        # 绝不留悬挂过滤器；滑出动画引用保存在 _slide_anim，随控件一起销毁）。
        self._anchor_parent = None
        self._slide_anim = None
        self._place_beside_parent()

    # ---- 只读访问器（供接线侧读取本窗当前状态；命名不得更改）----
    def current_code(self):
        """返回框内通过校验的 4 位码（字母/数字），否则返回空串。"""
        txt = self.code_edit.text().strip()
        if getattr(self.code_edit, "is_overlong", lambda: False)():
            return ""
        if re.fullmatch(r"[A-Za-z0-9]{4}", txt):
            return txt
        return ""

    def target_surl(self):
        """本窗对应的 surl（可能为空串）。"""
        return self.surl

    def target_uk(self):
        """本窗对应的 share_uk（可能为空串）。"""
        return self.share_uk

    # ---- 状态刷新 / 按钮入口 ----
    def _refresh_state(self):
        """按框内内容刷新两个下载按钮可用态与行内提示（码无效即置灰）。"""
        raw = self.code_edit.text().strip()
        valid = bool(self.current_code())
        self.once_btn.setEnabled(valid)
        self.mapped_btn.setEnabled(valid)
        if not raw:
            self.hint_label.setText("请输入 4 位提取码（字母或数字）")
            self.hint_label.setStyleSheet(
                f"color: {PALETTE['muted']}; font-size: 12px;")
        elif valid:
            self.hint_label.setText("提取码格式有效")
            self.hint_label.setStyleSheet(
                f"color: {PALETTE['success']}; font-size: 12px;")
        else:
            self.hint_label.setText("提取码需为 4 位字母或数字")
            self.hint_label.setStyleSheet(
                f"color: {PALETTE['danger']}; font-size: 12px;")

    def _submit(self, kind):
        """按钮统一入口：码有效才提交（无效仅刷新提示，不回调）。"""
        if self._done or self._timed_out:
            return
        code = self.current_code()
        if not code:
            self._refresh_state()
            return
        self._finish(kind, code)
        self.close()

    def _on_once_clicked(self):
        self._submit("once")

    def _on_mapped_clicked(self):
        self._submit("mapped")

    def _on_ignore_clicked(self):
        if self._done or self._timed_out:
            return
        self._finish("ignore", "")
        self.close()

    # ---- 倒计时 / 超时 ----
    def _update_countdown(self):
        try:
            self.timeout_label.setText(f"剩余 {self._remain_sec}s")
        except Exception:
            pass

    def _tick(self):
        """逐秒递减；归零即走超时关闭（关闭 + 丢弃，不回调）。"""
        if self._done or self._timed_out:
            return
        self._remain_sec = max(0, self._remain_sec - 1)
        self._update_countdown()
        if self._remain_sec <= 0:
            self._on_timeout()

    def _on_timeout(self):
        """到点：停表、记一行日志、关闭并丢弃框内内容——绝不回调 on_decision。"""
        if self._done or self._timed_out:
            return
        self._timed_out = True
        self._remain_sec = 0
        self._update_countdown()
        try:
            self._timer.stop()
        except Exception:
            pass
        try:
            if self._hub is not None:
                self._hub.log(
                    f"分享询问超时({self._timeout_sec}s)，已关闭丢弃: {self.surl}")
        except Exception:
            pass
        self.close()

    def _finish(self, kind, code):
        """统一出口：on_decision 只回调一次，并停掉倒计时计时器。"""
        if self._done:
            return
        self._done = True
        try:
            self._timer.stop()
        except Exception:
            pass
        cb = self.on_decision
        self.on_decision = None
        if cb is not None:
            try:
                _call_decision(cb, kind, code, self.url, self.surl, self.share_uk)
            except Exception:
                pass

    def reject(self):
        # Esc 关闭：等价「忽略」（超时/已作答时不再重复回调）
        if not self._timed_out:
            self._finish("ignore", "")
        super().reject()

    def closeEvent(self, event):
        # 关闭按钮 / 代码关闭：等价「忽略」；超时关闭已置 _timed_out，不再回调
        if not self._timed_out:
            self._finish("ignore", "")
        super().closeEvent(event)

    # ---- 几何：优先贴主窗右缘（跟随主窗）；无父时退回旧「贴屏幕右缘」 ----
    def _available_geometry(self):
        try:
            scr = QApplication.primaryScreen()
            if scr is not None:
                return scr.availableGeometry()
        except Exception:
            pass
        return QRect(0, 0, 1280, 800)

    def _screen_geometry_for(self, widget):
        """widget 所在屏幕的可用区；取不到时退回主屏（_available_geometry）。"""
        try:
            scr = widget.screen()
            if scr is not None:
                return scr.availableGeometry()
        except Exception:
            pass
        return self._available_geometry()

    def _place_right_edge(self):
        """无父回退：贴屏幕可用区右缘、纵向居中（避开底部托盘区）。"""
        try:
            geo = self._available_geometry()
            self.layout().activate()
            self.adjustSize()
            w = self.width()
            h = self.height()
            x = geo.x() + geo.width() - w - SHARE_ASK_EDGE_MARGIN
            y = geo.y() + max(SHARE_ASK_EDGE_MARGIN, (geo.height() - h) // 2)
            self.move(x, y)
        except Exception:
            pass

    def _parent_target_pos(self):
        """小窗目标全局位置：贴主窗右缘、与主窗顶部对齐，再收进屏幕可用区。

        返回 QPoint；无父（或父窗几何不可读）返回 None，由调用方走 _place_right_edge。"""
        p = self.parentWidget()
        if p is None:
            return None
        try:
            fg = p.frameGeometry()
            if fg.width() <= 0 or fg.height() <= 0:
                fg = p.geometry()
        except Exception:
            return None
        try:
            self.layout().activate()
            self.adjustSize()
        except Exception:
            pass
        w, h = self.width(), self.height()
        x = fg.x() + fg.width() + SHARE_ASK_EDGE_MARGIN
        y = fg.y() + SHARE_ASK_EDGE_MARGIN
        geo = self._screen_geometry_for(p)
        if geo is not None:
            x = min(x, geo.x() + geo.width() - w)
            y = min(y, geo.y() + geo.height() - h)
            x = max(geo.x(), x)
            y = max(geo.y(), y)
        return QPoint(x, y)

    def _place_beside_parent(self, animate=False):
        """把窗摆到主窗右缘；animate=True 时用约 160ms 的「从主窗右缘滑出」动画。

        起始点刻意放在目标点**左侧**（叠进主窗右缘约 12px），再向右滑到贴边位置：
        视觉上是「从程序右侧平移出来」，而不是从屏幕外侧滑进来。"""
        target = self._parent_target_pos()
        if target is None:
            self._place_right_edge()
            return
        if animate:
            try:
                start = QPoint(target.x() - 28, target.y())
                self.move(start)
                anim = QPropertyAnimation(self, b"pos", self)
                anim.setDuration(160)
                anim.setEasingCurve(QEasingCurve.OutCubic)
                anim.setStartValue(start)
                anim.setEndValue(target)
                self._slide_anim = anim
                anim.start()
                return
            except Exception:
                pass
        try:
            self.move(target)
        except Exception:
            pass

    # ---- 跟随主窗：Move/Resize 重锚定；主窗隐藏/最小化则一并隐藏 ----
    def _install_parent_filter(self):
        """在父窗上安装事件过滤器（重复安装前先移除，绝不叠加/悬挂）。"""
        p = self.parentWidget()
        if p is None or self._anchor_parent is p:
            return
        self._remove_parent_filter()
        try:
            p.installEventFilter(self)
            self._anchor_parent = p
        except Exception:
            self._anchor_parent = None

    def _remove_parent_filter(self):
        p = self._anchor_parent
        self._anchor_parent = None
        if p is None:
            return
        try:
            p.removeEventFilter(self)
        except Exception:
            pass

    def showEvent(self, event):
        super().showEvent(event)
        try:
            self._install_parent_filter()
        except Exception:
            pass
        try:
            self._place_beside_parent(animate=True)
        except Exception:
            pass

    def hideEvent(self, event):
        try:
            self._remove_parent_filter()
        except Exception:
            pass
        super().hideEvent(event)

    def eventFilter(self, obj, event):
        """父窗事件：移动/缩放即重锚定；父窗隐藏/最小化则把自己也藏起来。"""
        try:
            p = self._anchor_parent
            if p is not None and obj is p:
                et = event.type()
                if et in (QEvent.Move, QEvent.Resize):
                    if self.isVisible():
                        self._place_beside_parent(animate=False)
                elif et == QEvent.WindowStateChange:
                    if p.isMinimized() or not p.isVisible():
                        self.hide()
                elif et == QEvent.Hide:
                    self.hide()
        except Exception:
            pass
        return super().eventFilter(obj, event)
