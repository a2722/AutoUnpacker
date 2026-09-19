# -*- coding: utf-8 -*-
"""输入 / 基础控件：HotkeyEdit（快捷键捕获）、托盘图标、WatchCard 监听卡片、
全局热键过滤器、彩虹引导按钮、Glyph 线性图标、LayoutButton、SegControl、筛选 chip。"""

import ctypes

from PyQt5.QtWidgets import (QVBoxLayout, QHBoxLayout, QLabel, QLineEdit,
                             QPushButton, QCheckBox, QFileDialog, QFrame,
                             QComboBox, QWidget, QSizePolicy)
from PyQt5.QtCore import (Qt, QTimer, QRectF, QEvent, QSize, pyqtSignal,
                          QAbstractNativeEventFilter)
from PyQt5.QtGui import (QIcon, QPixmap, QPainter, QColor, QBrush, QPen,
                         QConicalGradient, QPainterPath, QPalette)

from ...config import (HOTKEY_ID, HOTKEY_ID_SHARE, HOTKEY_ID_SHARE_CODE,
                       WM_HOTKEY)
from ..style import PALETTE
from .common import (_key_display_name, WM_SETTINGCHANGE, _tk, _draw_glyph,
                     _clear_layout, repolish, repolish_tree, _StatusLamp)
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
            self.setStyleSheet(f"color: {PALETTE['accent_text']};")
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
    p.setBrush(QColor(PALETTE["tray_icon"]))
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
        self.mode_cb = QComboBox()
        self.mode_cb.addItem("表层（安全）", "surface")
        self.mode_cb.addItem("百度清单（含子目录）", "baidu")
        self.mode_cb.setCurrentIndex(
            1 if str(entry.get("mode") or "") == "baidu" else 0)
        self.mode_cb.setToolTip(
            "表层（安全）：只处理监听目录表面一层的文件（原有行为）。\n"
            "百度清单（含子目录）：额外按百度网盘任务清单，处理下载到子目录里的\n"
            "压缩包/分卷；清单不可用（未开实验性 / 历史被清）时自动退回表层。")
        self.mode_cb.currentIndexChanged.connect(self._on_mode)
        row3.addStretch(1)              # 勾选框靠左、「模式」靠右，避免挤在一起
        row3.addSpacing(16)
        row3.addWidget(QLabel("模式"))
        row3.addWidget(self.mode_cb)
        lay.addLayout(row3)

    def _on_mode(self, _i):
        self.state.update_path(self.idx, "mode", self.mode_cb.currentData())

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


class _HotkeyFilter(QAbstractNativeEventFilter):
    """Win32 消息过滤器：捕获 WM_HOTKEY（全局快捷键）与 WM_SETTINGCHANGE（主题变化）。"""

    def __init__(self, on_hotkey, on_settings_change=None, on_hotkey_share=None,
                 on_hotkey_share_code=None):
        super().__init__()
        self._on_hotkey = on_hotkey
        self._on_settings_change = on_settings_change
        self._on_hotkey_share = on_hotkey_share
        self._on_hotkey_share_code = on_hotkey_share_code

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
            if (msg.message == WM_HOTKEY and int(msg.wParam) == HOTKEY_ID_SHARE
                    and self._on_hotkey_share is not None):
                try:
                    self._on_hotkey_share()
                except Exception:
                    pass
                return True, 0
            if (msg.message == WM_HOTKEY and int(msg.wParam) == HOTKEY_ID_SHARE_CODE
                    and self._on_hotkey_share_code is not None):
                try:
                    self._on_hotkey_share_code()
                except Exception:
                    pass
                return True, 0
            if (msg.message == WM_SETTINGCHANGE
                    and self._on_settings_change is not None):
                # lParam 指向宽字符串：含 "ImmersiveColorSet" 即系统深浅色切换
                try:
                    lp = ctypes.cast(msg.lParam, ctypes.c_wchar_p)
                    if lp and lp.value and "ImmersiveColorSet" in lp.value:
                        self._on_settings_change()
                except Exception:
                    pass
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


class Glyph(QWidget):
    """QPainter 画的线性图标（项目无图片资源）。role 决定取色，随主题走。"""

    def __init__(self, name, parent=None, size=16, role="text", opacity=1.0):
        super().__init__(parent)
        self._name = str(name)
        self._role = str(role)
        self._opacity = float(opacity)
        self.setFixedSize(int(size), int(size))
        self.setAttribute(Qt.WA_TransparentForMouseEvents, True)

    def set_glyph(self, name):
        self._name = str(name)
        self.update()

    def set_role(self, role):
        self._role = str(role)
        self.update()

    def role(self):
        return self._role

    def _ink(self):
        try:
            if self._role == "muted":
                return QColor(PALETTE["muted"])
            if self._role == "accent":
                return QColor(PALETTE["accent_text"])
            if self._role == "danger":
                return QColor(PALETTE["danger"])
            if self._role == "success":
                return QColor(PALETTE["success"])
            if self._role == "invert":
                return self.palette().color(QPalette.HighlightedText)
            if self._role.startswith("token:"):
                return QColor(_tk(self._role.split(":", 1)[1], PALETTE["muted"]))
            return self.palette().color(QPalette.WindowText)
        except Exception:
            return QColor(PALETTE["muted"])

    def paintEvent(self, event):
        p = QPainter(self)
        p.setOpacity(self._opacity)
        pad = self.width() * 0.04
        _draw_glyph(p, self._name, QRectF(self.rect()).adjusted(pad, pad, -pad, -pad),
                    self._ink(), max(1.2, self.width() * 0.115))
        p.end()


class LayoutButton(QPushButton):
    """带子布局的按钮：QPushButton 默认 sizeHint 不认子布局（会塌成 30x15），
    这里按布局 sizeHint 计算，供胶囊/标签页/模式卡等「按钮里塞控件」的场景使用。"""

    def sizeHint(self):
        base = super().sizeHint()
        lay = self.layout()
        if lay is None:
            return base
        try:
            return lay.sizeHint().expandedTo(base)
        except Exception:
            return base

    def minimumSizeHint(self):
        try:
            return self.sizeHint()
        except Exception:
            return super().minimumSizeHint()


class RainbowLayoutButton(LayoutButton, RainbowBorderButton):
    """带子布局的彩虹引导按钮：尺寸按 LayoutButton 的布局计算，动画/边框用
    RainbowBorderButton（FINAL-SPEC §9：无监听路径时「添加目录」的发现性引导）。"""


class SegControl(QWidget):
    """分段控件（队列 N | 历史 N）：互斥，点击发 currentChanged(data)。"""

    currentChanged = pyqtSignal(str)

    def __init__(self, parent=None):
        super().__init__(parent)
        self._buttons = []
        self._current = None
        root = QHBoxLayout(self)
        root.setContentsMargins(0, 0, 0, 0)
        root.setSpacing(0)
        self.box = QFrame(self)
        self.box.setObjectName("segBox")
        self._lay = QHBoxLayout(self.box)
        self._lay.setContentsMargins(0, 0, 0, 0)
        self._lay.setSpacing(0)
        root.addWidget(self.box)

    def set_items(self, items):
        _clear_layout(self._lay)
        self._buttons = []
        for pair in items or []:
            label, data = pair[0], str(pair[1])
            btn = QPushButton(str(label), self.box)
            btn.setObjectName("segItem")
            btn.setProperty("active", False)
            btn.setCursor(Qt.PointingHandCursor)
            btn.setSizePolicy(QSizePolicy.Maximum, QSizePolicy.Fixed)
            btn.clicked.connect(lambda _=False, d=data: self._select(d, True))
            self._lay.addWidget(btn)
            self._buttons.append((data, btn))
        self._current = self._buttons[0][0] if self._buttons else None
        self._refresh()

    def _select(self, data, emit):
        self._current = data
        self._refresh()
        if emit:
            self.currentChanged.emit(data)

    def _refresh(self):
        for data, btn in self._buttons:
            btn.setProperty("active", data == self._current)
            repolish(btn)

    def set_label(self, data, label):
        """运行期刷新计数文案（如「失败 2」）。"""
        for d, btn in self._buttons:
            if d == str(data):
                btn.setText(str(label))
                return

    def set_current(self, data):
        self._select(str(data), False)

    def current(self):
        return self._current


class _ElideLabel(QLabel):
    """按实际可用宽度中间省略的 QLabel：最小宽度为 0，随宽度/字体重算省略文本。

    M3-QA 修复用：普通 QLabel 的 minimumSizeHint 等于全文宽度，会经
    QStackedWidget 传播成整窗最小宽度（日志页曾因此把窗口顶到 1684px）。
    本控件把最小宽度压到 0（可被压到任意窄），sizeHint 仍按全文（首选宽度
    不被压扁），显示文本按当前宽度 ElideMiddle，全文始终保留在 tooltip。
    """

    def __init__(self, text="", parent=None):
        super().__init__(parent)
        self._full_text = ""
        self.setMinimumWidth(0)
        self.setSizePolicy(QSizePolicy.Preferred, QSizePolicy.Preferred)
        self.set_full_text(text)

    def full_text(self):
        return self._full_text

    def set_full_text(self, text):
        self._full_text = str(text)
        self.setToolTip(self._full_text)
        self.setText(self._full_text)
        self._apply_text()

    def minimumSizeHint(self):
        try:
            return QSize(0, super().minimumSizeHint().height())
        except Exception:
            return QSize(0, 18)

    def sizeHint(self):
        try:
            fm = self.fontMetrics()
            return QSize(fm.horizontalAdvance(self._full_text) + 2,
                         super().sizeHint().height())
        except Exception:
            return super().sizeHint()

    def _apply_text(self):
        try:
            avail = int(self.width())
            if avail < 24:                 # 空间太紧：整条提示让位（可被完全收起）
                self.setText("")
                return
            fm = self.fontMetrics()
            self.setText(fm.elidedText(self._full_text, Qt.ElideMiddle, avail))
        except Exception:
            self.setText(self._full_text)

    def resizeEvent(self, event):
        super().resizeEvent(event)
        self._apply_text()

    def changeEvent(self, event):
        super().changeEvent(event)
        if event.type() == QEvent.FontChange:   # 主题切换换字体：重新省略
            self._apply_text()


class _FilterChip(QFrame):
    """日志页的路径筛选 chip：点击切换选中；状态灯颜色与选中态解耦。

    路径标签固定上限并中间省略（全文在 tooltip），chip 自身宽度上限 280px、
    最小宽度很小——筛选条在任何路径长度下都不会把窗口撑宽（M3-QA 阻断项）。
    """

    toggled = pyqtSignal(str, bool)
    CHIP_MAX_W = 280
    # 路径预算由 chip 上限推出（280 - 内边距/灯/间距 ≈ 245），标签随实际宽度省略

    def __init__(self, key, label, parent=None):
        super().__init__(parent)
        self.key = str(key)
        self._path = str(label)
        self.setObjectName("logChip")
        self.setProperty("active", False)
        self.setCursor(Qt.PointingHandCursor)
        self.setFixedHeight(30)
        self.setMaximumWidth(self.CHIP_MAX_W)
        self.setSizePolicy(QSizePolicy.Preferred, QSizePolicy.Fixed)
        lay = QHBoxLayout(self)
        lay.setContentsMargins(10, 0, 10, 0)
        lay.setSpacing(7)
        self.lamp = _StatusLamp(self)
        self.path_label = _ElideLabel(self._path, self)
        self.path_label.setObjectName("chipPath")
        self.path_label.setAttribute(Qt.WA_TransparentForMouseEvents, True)
        lay.addWidget(self.lamp)
        lay.addWidget(self.path_label, 1)
        self.setToolTip(self._path)

    def _apply_text(self):
        """按 chip 最新宽度/字体重算省略（全文在 tooltip；构造/主题切换都会调）。"""
        self.path_label.set_full_text(self._path)

    def minimumSizeHint(self):
        # 允许布局把 chip 压得很窄：路径会随实际宽度重新省略，
        # 不再以「全文宽度」参与 QStackedWidget 的最小宽度传播。
        return QSize(96, 30)

    def showEvent(self, event):
        # 首次显示时 QSS 字体才生效：按最终字体重量一次省略
        super().showEvent(event)
        self._apply_text()

    def mousePressEvent(self, event):
        if event.button() == Qt.LeftButton:
            event.accept()
            return
        super().mousePressEvent(event)

    def mouseReleaseEvent(self, event):
        if event.button() == Qt.LeftButton:
            self.set_active(not self.is_active())
            self.toggled.emit(self.key, self.is_active())
        super().mouseReleaseEvent(event)

    def set_active(self, flag):
        self.setProperty("active", bool(flag))
        repolish_tree(self)
        self._apply_text()                 # 主题切换（宿主 repolish 路径）后按新字体省略

    def is_active(self):
        return bool(self.property("active"))

    def set_state(self, state):
        """只改状态灯颜色；与选中态无关。"""
        self.lamp.set_state(state)

    def color_name(self):
        return self.lamp.color_name()


class FilterChipStrip(QWidget):
    """日志页路径筛选条：多选（可为空集）；选中态与状态灯解耦。"""

    selectionChanged = pyqtSignal(set)

    def __init__(self, parent=None):
        super().__init__(parent)
        self._chips = []
        lay = QHBoxLayout(self)
        lay.setContentsMargins(0, 0, 0, 0)
        lay.setSpacing(7)
        lead = QLabel("按路径筛选", self)
        lead.setObjectName("stripHint")
        lay.addWidget(lead)
        self._chips_box = QWidget(self)
        self._chip_lay = QHBoxLayout(self._chips_box)
        self._chip_lay.setContentsMargins(0, 0, 0, 0)
        self._chip_lay.setSpacing(7)
        lay.addWidget(self._chips_box)
        self.clear_btn = LayoutButton(self)
        self.clear_btn.setObjectName("ghostSm")
        self.clear_btn.setCursor(Qt.PointingHandCursor)
        clr_lay = QHBoxLayout(self.clear_btn)
        clr_lay.setContentsMargins(0, 0, 0, 0)
        clr_lay.setSpacing(5)
        clr_lay.addWidget(Glyph("close", self.clear_btn, 12, role="muted"))
        clr_lay.addWidget(QLabel("清除筛选", self.clear_btn))
        self.clear_btn.clicked.connect(self._on_clear)
        lay.addWidget(self.clear_btn)
        lay.addStretch(1)
        self.hint = _ElideLabel(
            "点击路径可多选 · 只显示所选路径的日志 · 全局日志始终显示", self)
        self.hint.setObjectName("stripHint")
        lay.addWidget(self.hint)

    def set_chips(self, items):
        _clear_layout(self._chip_lay)
        self._chips = []
        for pair in items or []:
            chip = _FilterChip(pair[0], pair[1], self._chips_box)
            chip.toggled.connect(self._on_toggled)
            self._chip_lay.addWidget(chip)
            self._chips.append(chip)

    def _on_toggled(self, key, active):
        self.selectionChanged.emit(self.selected_keys())

    def _on_clear(self):
        if self.selected_keys():
            self.clear_selection()

    def set_state(self, key, state):
        for chip in self._chips:
            if chip.key == str(key):
                chip.set_state(state)
                return

    def selected_keys(self):
        return set(c.key for c in self._chips if c.is_active())

    def clear_selection(self):
        changed = False
        for chip in self._chips:
            if chip.is_active():
                chip.set_active(False)
                changed = True
        if changed:
            self.selectionChanged.emit(self.selected_keys())

    def chips(self):
        return list(self._chips)
