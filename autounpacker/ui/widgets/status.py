# -*- coding: utf-8 -*-
"""状态 / 引导类控件：底栏播报 StatusTipTicker、需要处理卡片、
模式卡 ModeCard 与 ModeSelector。"""

from PyQt5.QtWidgets import (QWidget, QLabel, QFrame, QVBoxLayout,
                             QHBoxLayout, QPushButton, QSizePolicy,
                             QButtonGroup)
from PyQt5.QtCore import (Qt, QTimer, QPoint, QPropertyAnimation,
                          QEasingCurve, pyqtSignal)

from .common import _clear_layout, repolish_tree
from .inputs import Glyph, LayoutButton
from .nav import DEFAULT_TIPS
class StatusTipTicker(QWidget):
    """底栏纵向播报：一次一句、4.5s 上移一行（350ms 缓动）、末行复制首句无缝循环。

    只动 track 的 pos（QPropertyAnimation），绝不动画 layout。
    """

    ROW_H = 16
    INTERVAL = 4500
    DURATION = 350
    WIDTH = 336

    def __init__(self, tips=None, parent=None):
        # 容错：允许把 parent 当成第一个位置参数传入（QPushButton 风格的调用习惯）
        if isinstance(tips, QWidget) and parent is None:
            parent, tips = tips, None
        super().__init__(parent)
        self.setObjectName("tipTicker")
        self.setFixedSize(self.WIDTH, self.ROW_H)
        self._hotkey = ""
        self._paused = False
        self._rows_text = []
        self._row_labels = []
        self._idx = 0
        self._anim = None
        self._track = QWidget(self)
        self._track.setAttribute(Qt.WA_TransparentForMouseEvents, True)
        self.set_tips(tips if tips is not None else list(DEFAULT_TIPS))
        self._timer = QTimer(self)
        self._timer.setInterval(self.INTERVAL)
        self._timer.timeout.connect(self._advance)
        self._timer.start()

    def set_tips(self, tips):
        items = [str(t) for t in (tips or []) if str(t).strip()]
        if not items:
            items = list(DEFAULT_TIPS)
        self._rows_text = items + [items[0]]
        for lbl in self._row_labels:
            try:
                lbl.setParent(None)
                lbl.deleteLater()
            except Exception:
                pass
        self._row_labels = []
        self._track.setGeometry(0, 0, self.WIDTH, len(self._rows_text) * self.ROW_H)
        for i, text in enumerate(self._rows_text):
            lbl = QLabel(self._track)
            lbl.setObjectName("tipRow")
            lbl.setGeometry(0, i * self.ROW_H, self.WIDTH, self.ROW_H)
            lbl.setText(self._compose(text))
            lbl.setAttribute(Qt.WA_TransparentForMouseEvents, True)
            lbl.show()
            self._row_labels.append(lbl)
        self._idx = 0
        try:
            self._track.move(0, 0)
        except Exception:
            pass

    def set_hotkey(self, hotkey):
        """运行期拼装末句的快捷键（不再硬编码 Ctrl+Alt+W）。"""
        self._hotkey = str(hotkey or "").strip()
        for i, lbl in enumerate(self._row_labels):
            if i < len(self._rows_text):
                lbl.setText(self._compose(self._rows_text[i]))

    def _compose(self, tip):
        hk = str(self._hotkey or "").strip()
        if hk and "快捷键 " in tip:
            return tip.split("快捷键 ")[0] + "快捷键 " + hk
        return tip

    def pause(self):
        """显式暂停（宿主意图）；隐藏时也会临时停表，重新显示后自动恢复。"""
        self._paused = True
        try:
            self._timer.stop()
        except Exception:
            pass

    def resume(self):
        self._paused = False
        try:
            if not self._timer.isActive():
                self._timer.start()
        except Exception:
            pass

    def hideEvent(self, event):
        """隐藏（含最小化到托盘）时停表，避免后台空转；显示时自动恢复。"""
        try:
            self._timer.stop()
        except Exception:
            pass
        super().hideEvent(event)

    def showEvent(self, event):
        super().showEvent(event)
        if not self._paused:
            self.resume()

    def current_index(self):
        return self._idx

    def track(self):
        return self._track

    def _advance(self):
        """上移一行；到末行（首句副本）后瞬时回卷再动画，视觉无缝。"""
        if not self._rows_text:
            return
        self._idx += 1
        if self._idx >= len(self._rows_text):
            self._idx = 1
            try:
                self._track.move(0, 0)
            except Exception:
                pass
        start_y = -(self._idx - 1) * self.ROW_H
        end_y = -self._idx * self.ROW_H
        try:
            dur = int(self.DURATION)
        except Exception:
            dur = 0
        if dur <= 0:
            try:
                self._track.move(0, end_y)
            except Exception:
                pass
            return
        anim = QPropertyAnimation(self._track, b"pos", self)
        anim.setDuration(dur)
        anim.setEasingCurve(QEasingCurve.OutCubic)
        anim.setStartValue(QPoint(0, start_y))
        anim.setEndValue(QPoint(0, end_y))
        self._anim = anim
        anim.start()


# 行内操作词表（mockup 13 的「需要处理」）
_NEED_ACTION_TEXT = {"retry": "重试", "open_dir": "打开目录",
                     "input_password": "输入密码", "ignore": "忽略"}

# 行内操作 tooltip（文案用户定稿）：说明「输入密码」去哪补码，以及待密码行
# 为什么「重试」仍可能失败。失败行的「重试」不挂此 tip，避免对非密码失败误导。
_NEED_ACTION_TIP = {
    "input_password": "到「密码本」页补充密码（固定密码本 / 临时密码 / 分享提取码）",
    "retry": "已在所有来源（固定密码本 · 临时密码 · 分享提取码）中查找，均无匹配密码。"
             "若刚复制过密码，请先到「密码本」页保存后再点「重试」。",
}


class _NeedRow(QFrame):
    """「需要处理」一行：3px 紧急度竖条 + 文件名/时间 + 说明 + 行内 ghost 按钮。"""

    clicked = pyqtSignal(int)
    actionTriggered = pyqtSignal(int, str)

    def __init__(self, item, parent=None):
        super().__init__(parent)
        self.task_id = int(item.get("task_id") or 0)
        self.setCursor(Qt.PointingHandCursor)
        lay = QHBoxLayout(self)
        lay.setContentsMargins(0, 9, 0, 9)
        lay.setSpacing(10)
        self.bar = QFrame(self)
        self.bar.setObjectName("needBar")
        self.bar.setProperty(
            "urgency", "warn" if str(item.get("urgency") or "") == "warn" else "err")
        self.bar.setFixedWidth(3)
        self.bar.setSizePolicy(QSizePolicy.Fixed, QSizePolicy.Expanding)
        self.bar.setAttribute(Qt.WA_TransparentForMouseEvents, True)
        lay.addWidget(self.bar)

        col = QVBoxLayout()
        col.setContentsMargins(0, 0, 0, 0)
        col.setSpacing(2)
        top = QHBoxLayout()
        top.setContentsMargins(0, 0, 0, 0)
        top.setSpacing(8)
        self.name_label = QLabel(self)
        self.name_label.setObjectName("needName")
        self.name_label.setAttribute(Qt.WA_TransparentForMouseEvents, True)
        full_name = str(item.get("name") or "")
        try:
            self.name_label.setText(self.name_label.fontMetrics().elidedText(
                full_name, Qt.ElideMiddle, 140))
        except Exception:
            self.name_label.setText(full_name)
        self.name_label.setToolTip(full_name)
        top.addWidget(self.name_label)
        top.addStretch(1)
        self.ts_label = QLabel(str(item.get("ts") or ""), self)
        self.ts_label.setObjectName("needTs")
        self.ts_label.setAttribute(Qt.WA_TransparentForMouseEvents, True)
        top.addWidget(self.ts_label)
        col.addLayout(top)

        self.note_label = QLabel(str(item.get("note") or ""), self)
        self.note_label.setObjectName("needNote")
        self.note_label.setWordWrap(True)
        self.note_label.setAttribute(Qt.WA_TransparentForMouseEvents, True)
        col.addWidget(self.note_label)

        actions = QHBoxLayout()
        actions.setContentsMargins(0, 0, 0, 0)
        actions.setSpacing(6)
        self.action_buttons = {}
        action_keys = [str(k) for k in (item.get("actions") or [])]
        pwd_row = "input_password" in action_keys
        for key in action_keys:
            text = _NEED_ACTION_TEXT.get(key)
            if not text:
                continue
            btn = QPushButton(text, self)
            btn.setObjectName("ghostSm")
            btn.setCursor(Qt.PointingHandCursor)
            tip = _NEED_ACTION_TIP.get(key)
            if tip and (key != "retry" or pwd_row):
                btn.setToolTip(tip)
            btn.clicked.connect(
                lambda _=False, k=key: self.actionTriggered.emit(self.task_id, k))
            actions.addWidget(btn)
            self.action_buttons[key] = btn
        actions.addStretch(1)
        col.addLayout(actions)
        lay.addLayout(col, 1)

    def mouseReleaseEvent(self, event):
        if event.button() == Qt.LeftButton:
            self.clicked.emit(self.task_id)
        super().mouseReleaseEvent(event)


class NeedsAttentionCard(QFrame):
    """「需要处理」面板：列表行（3px 竖条 + 纯文本），数量是普通加粗数字。"""

    taskActivated = pyqtSignal(int)
    actionTriggered = pyqtSignal(int, str)

    def __init__(self, parent=None):
        super().__init__(parent)
        self.setObjectName("card")
        self._rows = []
        lay = QVBoxLayout(self)
        lay.setContentsMargins(14, 12, 14, 12)
        lay.setSpacing(6)

        head = QHBoxLayout()
        head.setContentsMargins(0, 0, 0, 0)
        head.setSpacing(8)
        head.addWidget(Glyph("alert", self, 13, role="muted"))
        title = QLabel("需要处理", self)
        title.setObjectName("needTitle")
        head.addWidget(title)
        head.addStretch(1)
        self.count_label = QLabel("0", self)
        self.count_label.setObjectName("needCount")
        head.addWidget(self.count_label)
        lay.addLayout(head)

        self._rows_box = QWidget(self)
        self._rows_lay = QVBoxLayout(self._rows_box)
        self._rows_lay.setContentsMargins(0, 0, 0, 0)
        self._rows_lay.setSpacing(0)
        lay.addWidget(self._rows_box)

        # 空态文案渲染在卡片内部（M3-QA：曾作为兄弟控件浮在卡片边框之外）
        self.empty_label = QLabel("", self)
        self.empty_label.setObjectName("stripHint")
        self.empty_label.setAlignment(Qt.AlignCenter)
        self.empty_label.setWordWrap(True)
        self.empty_label.setVisible(False)
        lay.addWidget(self.empty_label)
        lay.addStretch(1)

        self._sep = QFrame(self)
        self._sep.setObjectName("needSepLast")
        self._sep.setFixedHeight(1)
        lay.addWidget(self._sep)
        hint = QLabel("点击整行可跳到「任务」页并选中该任务，只看它自己的日志。", self)
        hint.setObjectName("needNote")
        hint.setWordWrap(True)
        lay.addWidget(hint)

    def set_items(self, items):
        items = [it for it in (items or []) if isinstance(it, dict)]
        _clear_layout(self._rows_lay)
        self._rows = []
        for i, item in enumerate(items):
            row = _NeedRow(item, self._rows_box)
            row.clicked.connect(self.taskActivated)
            row.actionTriggered.connect(self.actionTriggered)
            self._rows_lay.addWidget(row)
            self._rows.append(row)
            if i != len(items) - 1:
                sep = QFrame(self._rows_box)
                sep.setObjectName("needSep")
                sep.setFixedHeight(1)
                self._rows_lay.addWidget(sep)
        self.count_label.setText(str(len(items)))
        self.empty_label.setVisible(not items)

    def set_empty_text(self, text):
        """空态文案（空列表时显示在卡片内部的 rows 区）。"""
        self.empty_label.setText(str(text))

    def empty_text(self):
        return self.empty_label.text()

    def rows(self):
        return list(self._rows)

    def count(self):
        return len(self._rows)


class ModeCard(LayoutButton):
    """目录设置弹窗的模式卡（checkable）：勾选标记 + 标题 +（可选）徽标 + 说明。"""

    def __init__(self, value, title, desc, badge=None, parent=None):
        super().__init__(parent)
        self.value = str(value)
        self.setObjectName("modeCard")
        self.setCheckable(True)
        self.setCursor(Qt.PointingHandCursor)
        lay = QVBoxLayout(self)
        lay.setContentsMargins(12, 11, 12, 11)
        lay.setSpacing(5)
        top = QHBoxLayout()
        top.setContentsMargins(0, 0, 0, 0)
        top.setSpacing(8)
        self.check_box = QFrame(self)
        self.check_box.setObjectName("modeCheck")
        self.check_box.setFixedSize(15, 15)
        self.check_box.setAttribute(Qt.WA_TransparentForMouseEvents, True)
        cb_lay = QHBoxLayout(self.check_box)
        cb_lay.setContentsMargins(0, 0, 0, 0)
        self.tick = Glyph("check", self.check_box, 10, role="invert")
        self.tick.setVisible(False)
        cb_lay.addWidget(self.tick, 0, Qt.AlignCenter)
        top.addWidget(self.check_box, 0, Qt.AlignVCenter)
        self.title_label = QLabel(str(title), self)
        self.title_label.setObjectName("modeTitle")
        self.title_label.setAttribute(Qt.WA_TransparentForMouseEvents, True)
        top.addWidget(self.title_label, 0, Qt.AlignVCenter)
        self.badge_label = None
        if badge:
            self.badge_label = QLabel(str(badge), self)
            self.badge_label.setObjectName("modeBadge")
            self.badge_label.setAttribute(Qt.WA_TransparentForMouseEvents, True)
            top.addWidget(self.badge_label, 0, Qt.AlignVCenter)
        top.addStretch(1)
        lay.addLayout(top)
        self.desc_label = QLabel(str(desc), self)
        self.desc_label.setObjectName("modeDesc")
        self.desc_label.setWordWrap(True)
        self.desc_label.setAlignment(Qt.AlignLeft | Qt.AlignTop)
        self.desc_label.setAttribute(Qt.WA_TransparentForMouseEvents, True)
        lay.addWidget(self.desc_label)
        self.toggled.connect(self._on_toggled)

    def _on_toggled(self, checked):
        try:
            self.tick.setVisible(bool(checked))
        except Exception:
            pass
        repolish_tree(self)

    def description(self):
        return self.desc_label.text()


class ModeSelector(QWidget):
    """监听模式：两张平铺卡互斥（绝不用 QComboBox）。"""

    modeChanged = pyqtSignal(str)   # 'surface' | 'baidu'

    def __init__(self, parent=None):
        super().__init__(parent)
        self._cards = []
        self._loading = False
        self._group = QButtonGroup(self)
        self._group.setExclusive(True)
        self._lay = QHBoxLayout(self)
        self._lay.setContentsMargins(0, 0, 0, 0)
        self._lay.setSpacing(10)

    def set_modes(self, items):
        """items: [(value, title, desc, badge_or_None), …]，默认选中第一张。"""
        self._loading = True
        try:
            for card in self._cards:
                try:
                    self._group.removeButton(card)
                except Exception:
                    pass
            self._cards = []
            _clear_layout(self._lay)
            for item in (items or []):
                value, title, desc = item[0], item[1], item[2]
                badge = item[3] if len(item) > 3 else None
                card = ModeCard(value, title, desc, badge, self)
                self._group.addButton(card)
                card.toggled.connect(
                    lambda checked, c=card: self._on_card_toggled(c, checked))
                self._lay.addWidget(card, 1)
                self._cards.append(card)
            if self._cards:
                self._cards[0].setChecked(True)
        finally:
            self._loading = False

    def _on_card_toggled(self, card, checked):
        if self._loading or not checked:
            return
        self.modeChanged.emit(card.value)

    def set_mode(self, value):
        value = str(value)
        self._loading = True
        try:
            for card in self._cards:
                card.setChecked(card.value == value)
        finally:
            self._loading = False

    def mode(self):
        for card in self._cards:
            if card.isChecked():
                return card.value
        return ""

    def cards(self):
        return list(self._cards)
