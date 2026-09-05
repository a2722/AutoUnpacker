# -*- coding: utf-8 -*-
"""通用控件：HotkeyEdit（快捷键捕获）、托盘图标、监听卡片 WatchCard、
全局热键过滤器、彩虹引导按钮。"""
import ctypes

from PyQt5.QtWidgets import (QVBoxLayout, QHBoxLayout, QLabel, QLineEdit, QPushButton, QCheckBox, QFileDialog, QFrame)
from PyQt5.QtCore import (Qt, QTimer, QRectF, pyqtSignal, QAbstractNativeEventFilter)
from PyQt5.QtGui import (QIcon, QPixmap, QPainter, QColor, QBrush, QPen, QConicalGradient, QPainterPath)

from ..config import _HK_NAME_BY_VK, HOTKEY_ID, WM_HOTKEY

try:
    import win32gui
except ImportError:
    win32gui = None

def _key_display_name(qt_key):
    """Qt.Key -> 显示名（用于 HotkeyEdit 捕获后回显）。"""
    if Qt.Key_A <= qt_key <= Qt.Key_Z:
        return chr(qt_key)
    if Qt.Key_0 <= qt_key <= Qt.Key_9:
        return chr(qt_key)
    if Qt.Key_F1 <= qt_key <= Qt.Key_F24:
        return "F%d" % (qt_key - Qt.Key_F1 + 1)
    return _HK_NAME_BY_VK.get(qt_key)


class HotkeyEdit(QLineEdit):
    """点击后捕获按键组合：要求 修饰键(Ctrl/Alt/Win 至少其一) + 普通键。"""

    comboChanged = pyqtSignal(str)

    def __init__(self, parent=None):
        super().__init__(parent)
        self.setReadOnly(True)
        self.setPlaceholderText("点击后按下组合键...")
        self.setToolTip("点击输入框，再按下要用的组合键（如 Ctrl+Alt+W）")
        self._capturing = False

    def mousePressEvent(self, event):
        if event.button() == Qt.LeftButton:
            self._capturing = True
            self.setText("按下组合键...")
            self.setStyleSheet("color: #1f6feb;")
        super().mousePressEvent(event)

    def keyPressEvent(self, event):
        if not self._capturing:
            event.ignore()
            return
        mods = []
        if event.modifiers() & Qt.ControlModifier:
            mods.append("Ctrl")
        if event.modifiers() & Qt.AltModifier:
            mods.append("Alt")
        if event.modifiers() & Qt.ShiftModifier:
            mods.append("Shift")
        if event.modifiers() & Qt.MetaModifier:
            mods.append("Win")
        key = event.key()
        event.accept()
        if key in (Qt.Key_Control, Qt.Key_Alt, Qt.Key_Shift, Qt.Key_Meta,
                   Qt.Key_CapsLock, Qt.Key_NumLock):
            return  # 纯修饰键，继续捕获下一个键
        name = _key_display_name(key)
        if name is None:
            return
        if not any(m in mods for m in ("Ctrl", "Alt", "Win")):
            return  # 必须带 Ctrl/Alt/Win 之一，避免与普通按键冲突
        combo = "+".join(mods + [name])
        self._capturing = False
        self.setText(combo)
        self.setStyleSheet("")
        self.comboChanged.emit(combo)


def make_tray_icon():
    pm = QPixmap(64, 64)
    pm.fill(Qt.transparent)
    p = QPainter(pm)
    p.setRenderHint(QPainter.Antialiasing)
    p.setPen(Qt.NoPen)
    p.setBrush(QColor(31, 111, 235))
    p.drawRoundedRect(4, 10, 56, 48, 7, 7)
    p.setBrush(QColor(255, 255, 255))
    p.drawRect(26, 3, 12, 9)
    p.setBrush(QColor(255, 255, 255))
    p.drawRect(10, 16, 44, 36)
    p.setBrush(QColor(30, 40, 60))
    cell = 44.0 / 8.0
    ox, oy = 10.0, 16.0

    def cellrect(x, y):
        return (int(ox + x * cell), int(oy + y * cell),
                int(cell + 0.5), int(cell + 0.5))

    for (bx, by) in [(0, 0), (0, 5), (5, 0)]:
        for dx in range(3):
            for dy in range(3):
                p.drawRect(*cellrect(bx + dx, by + dy))
    for (mx, my) in [(1, 4), (2, 3), (2, 6), (3, 2), (3, 7), (4, 4),
                     (5, 3), (5, 6), (6, 1), (6, 4), (7, 3), (7, 6)]:
        p.drawRect(*cellrect(mx, my))
    p.end()
    return QIcon(pm)


class WatchCard(QFrame):
    """单个监听路径的编辑卡片"""

    def __init__(self, state, idx, entry, on_remove, on_path_change=None):
        super().__init__()
        self.state = state
        self.idx = idx
        self.entry = entry
        self.on_path_change = on_path_change
        self.setObjectName("card")
        lay = QVBoxLayout(self)
        lay.setContentsMargins(14, 12, 14, 12)
        lay.setSpacing(8)

        row1 = QHBoxLayout()
        self.enable_cb = QCheckBox()
        self.enable_cb.setChecked(bool(entry.get("enabled", True)))
        self.enable_cb.stateChanged.connect(self._on_enable)
        self.path_edit = QLineEdit(entry.get("path", ""))
        self.path_edit.setPlaceholderText("监听目录路径")
        self.path_edit.textChanged.connect(self._on_path_text)
        self.browse_path_btn = RainbowBorderButton("浏览")
        self.browse_path_btn.clicked.connect(self._browse_path)
        remove = QPushButton("移除")
        remove.setObjectName("danger")
        remove.clicked.connect(lambda: on_remove(self.idx))
        row1.addWidget(self.enable_cb)
        row1.addWidget(QLabel("路径"))
        row1.addWidget(self.path_edit, 1)
        row1.addWidget(self.browse_path_btn)
        row1.addWidget(remove)
        lay.addLayout(row1)

        row2 = QHBoxLayout()
        self.out_edit = QLineEdit(entry.get("output_dir", ""))
        self.out_edit.setPlaceholderText("留空 = 在监听目录下创建同名文件夹")
        self.out_edit.textChanged.connect(lambda t: self.state.update_path(self.idx, "output_dir", t))
        obrowse = QPushButton("浏览")
        obrowse.clicked.connect(self._browse_out)
        row2.addWidget(QLabel("解压到"))
        row2.addWidget(self.out_edit, 1)
        row2.addWidget(obrowse)
        lay.addLayout(row2)

        row3 = QHBoxLayout()
        self.del_cb = QCheckBox("解压成功后删除源文件")
        self.del_cb.setChecked(bool(entry.get("delete_source", False)))
        self.del_cb.stateChanged.connect(self._on_delete)
        row3.addWidget(self.del_cb)
        row3.addStretch(1)
        lay.addLayout(row3)

    def _on_enable(self, s):
        self.state.update_path(self.idx, "enabled", bool(s))

    def _on_path_text(self, t):
        self.state.update_path(self.idx, "path", t)
        if self.on_path_change is not None:
            self.on_path_change()

    def _on_delete(self, s):
        self.state.update_path(self.idx, "delete_source", bool(s))

    def _browse_path(self):
        d = QFileDialog.getExistingDirectory(self, "选择监听目录")
        if d:
            self.path_edit.setText(d)

    def _browse_out(self):
        d = QFileDialog.getExistingDirectory(self, "选择解压输出目录")
        if d:
            self.out_edit.setText(d)


TRAIL_STATUS_TEXT = {
    "recorded": "已记录（处理中）",
    "kept": "未删除",
    "deleted": "已删除（回收站）",
    "restored": "已还原",
    "failed": "解压失败",
}

TRAIL_STATUS_COLORS = {
    "recorded": "#8a94a6",
    "kept": "#2e7d32",
    "deleted": "#c0392b",
    "restored": "#1f6feb",
    "failed": "#ad1457",
}

TRAIL_STATUS_ORDER = ["deleted", "restored", "kept", "failed", "recorded"]


class _HotkeyFilter(QAbstractNativeEventFilter):
    """Win32 消息过滤器：捕获 WM_HOTKEY（全局快捷键）并回调。"""

    def __init__(self, on_hotkey):
        super().__init__()
        self._on_hotkey = on_hotkey

    def nativeEventFilter(self, eventType, message):
        if eventType == b"windows_generic_MSG":
            try:
                msg = ctypes.wintypes.MSG.from_address(int(message))
            except Exception:
                return False, 0
            if msg.message == WM_HOTKEY and int(msg.wParam) == HOTKEY_ID:
                try:
                    self._on_hotkey()
                except Exception:
                    pass
                return True, 0
        return False, 0


class RainbowBorderButton(QPushButton):
    """无监听路径时的高亮按钮：流动彩虹渐变边框，引导用户先添加监听路径。

    只在 set_rainbow(True) 时启动定时重绘（约 30fps），False 时停止，
    平时零后台开销。基类样式（QSS）由 super().paintEvent 正常绘制。"""

    RAINBOW_STOPS = [
        (0.00, "#ff5252"), (0.16, "#ffb74d"), (0.33, "#fff176"),
        (0.50, "#66bb6a"), (0.66, "#4fc3f7"), (0.83, "#9575cd"),
        (1.00, "#ff5252"),
    ]

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self._rainbow = False
        self._angle = 0.0
        self._timer = QTimer(self)
        self._timer.setInterval(33)
        self._timer.timeout.connect(self._tick)

    def set_rainbow(self, active):
        self._rainbow = bool(active)
        if self._rainbow:
            self._timer.start()
        else:
            self._timer.stop()
        self.update()

    def _tick(self):
        self._angle = (self._angle + 5.0) % 360.0
        self.update()

    def paintEvent(self, event):
        super().paintEvent(event)
        if not self._rainbow:
            return
        p = QPainter(self)
        p.setRenderHint(QPainter.Antialiasing)
        # 与 QSS 的 QPushButton 边框完全对齐：
        #   QSS 为 border:1px solid + border-radius:6px，故取 inset=1、radius=6，
        #   3px 画笔以边框线为中心向外/向内各盖 1.5px，正好覆盖 QSS 的 1px 边框，
        #   不再残留外层细框线。
        rect = QRectF(self.rect()).adjusted(1.0, 1.0, -1.0, -1.0)
        grad = QConicalGradient(rect.center(), self._angle)
        for pos, color in self.RAINBOW_STOPS:
            grad.setColorAt(pos, QColor(color))
        pen = QPen(QBrush(grad), 3.0)
        pen.setCapStyle(Qt.RoundCap)
        path = QPainterPath()
        path.addRoundedRect(rect, 6, 6)
        p.setPen(pen)
        p.drawPath(path)
        p.end()


