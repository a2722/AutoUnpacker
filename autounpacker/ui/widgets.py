# -*- coding: utf-8 -*-
"""通用控件：HotkeyEdit（快捷键捕获）、托盘图标、监听卡片 WatchCard、全局热键过滤器、彩虹引导按钮。

职责：- HotkeyEdit 捕获「修饰键 + 普通键」组合并发出 comboChanged 信号
- make_tray_icon() 程序化绘制托盘图标；_HotkeyFilter 捕获 WM_HOTKEY 全局热键
- WatchCard 单个监听路径的编辑卡片（路径/输出目录/删除源文件/监听模式）
- RainbowBorderButton 无监听路径时的高亮彩虹边框引导按钮
- M2 新控件家族（主窗口重写用）：DirChip / DirChipStrip / NavTabs / SegControl /
  FilterChipStrip / TaskTable + TaskModel / StatusTipTicker / NeedsAttentionCard /
  ModeCard / ModeSelector；Glyph 为 QPainter 画的线性图标（项目无图片资源）
- ToastBubble / show_toast()：点击反馈小气泡（已复制/复制失败），不抢焦点、自动消失
关键入口：HotkeyEdit / WatchCard / RainbowBorderButton / make_tray_icon() /
          _HotkeyFilter / repolish() / show_toast() / 上述 M2 控件类
依赖：PyQt5、win32gui（可选）、config（快捷键常量）、style（token 取色）
注意：RainbowBorderButton 仅在 set_rainbow(True) 时启动约 30fps 定时重绘，平时零后台开销
注意：带动态属性（selected/active/checked/urgency）的控件在 setProperty 后必须
      repolish(...)，Qt 不会自动重算属性选择器
"""
import ctypes

from PyQt5.QtWidgets import (QVBoxLayout, QHBoxLayout, QLabel, QLineEdit, QPushButton, QCheckBox, QFileDialog, QFrame, QComboBox,
                             QWidget, QProgressBar, QButtonGroup, QSizePolicy, QTableView, QHeaderView,
                             QAbstractItemView, QStyledItemDelegate, QStyleOptionViewItem, QStyle,
                             QApplication, QGraphicsOpacityEffect)
from PyQt5.QtCore import (Qt, QTimer, QRectF, QEvent, QSize, pyqtSignal,
                          QAbstractNativeEventFilter,
                          QAbstractTableModel, QModelIndex, QPoint, QPointF,
                          QPropertyAnimation, QEasingCurve)
from PyQt5.QtGui import (QIcon, QPixmap, QPainter, QColor, QBrush, QPen, QConicalGradient, QPainterPath, QPalette, QFont)

from ..config import _HK_NAME_BY_VK, HOTKEY_ID, HOTKEY_ID_SHARE, HOTKEY_ID_SHARE_CODE, WM_HOTKEY
from .style import PALETTE
from . import style as ui_style

# 系统主题变化广播：WM_SETTINGCHANGE 的 lParam 为 "ImmersiveColorSet" 时表示深浅色变了
WM_SETTINGCHANGE = 0x001A

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


TRAIL_STATUS_TEXT = {
    "recorded": "已记录（处理中）",
    "kept": "未删除",
    "deleted": "已删除（回收站）",
    "restored": "已还原",
    "failed": "解压失败",
}

TRAIL_STATUS_COLORS = PALETTE["trail"]   # 与 style.PALETTE 同一对象，随主题就地更新

TRAIL_STATUS_ORDER = ["deleted", "restored", "kept", "failed", "recorded"]


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


# ===========================================================================
# M2：主窗口重写用的新控件家族
# ---------------------------------------------------------------------------
# 取色规则：只用 style.py 的 QSS token（自绘控件经 style.tokens() 取）与 PALETTE；
# 动态属性（selected/active/checked/urgency）在 setProperty 后必须 repolish()。
# ===========================================================================

# 目录状态文案（FINAL-SPEC §7）；running/idle 是原型数据的别名
DIR_STATE_TEXT = {
    "listening": "监听中", "extracting": "解压中", "waiting": "等待中",
    "paused": "已暂停", "error": "错误",
}
_DIR_STATE_ALIASES = {
    "running": "extracting", "idle": "listening", "wait": "waiting",
    "err": "error", "failed": "error", "failure": "error",
}

# 底栏播报默认 5 句（与 mockups/assets/parts.js::TIPS_LIST 逐字一致）；
# 末句的快捷键在运行期由 set_hotkey() 重新拼装，这里只作回退。
DEFAULT_TIPS = [
    "拖入压缩包即可直接解压；拖入二维码图片可直接识别",
    "分卷不全会等到齐再解，不会误判失败",
    "密码本可从二维码截图自动收录提取码",
    "点底栏的「失败」可直接筛出失败条目",
    "关闭窗口后仍在托盘继续工作，快捷键 Ctrl+Alt+W",
]


def dir_state_key(state):
    """把外部状态值规整成 DirState：listening|extracting|waiting|paused|error。"""
    s = str(state or "").strip().lower()
    return _DIR_STATE_ALIASES.get(s, s or "listening")


def _tk(name, fallback=""):
    """取当前主题的 QSS token 值（自绘控件取色用；异常回退）。"""
    try:
        return ui_style.tokens().get(name) or fallback
    except Exception:
        return fallback


def repolish(w):
    """重算动态属性选择器：setProperty(...) 之后必须调用（Qt 不会自动重算 QSS）。"""
    try:
        w.style().unpolish(w)
        w.style().polish(w)
        w.update()
    except Exception:
        pass


def repolish_tree(w):
    """连同 QLabel 子控件一起 repolish（属性选择器的后代规则需要子控件重算）。"""
    repolish(w)
    try:
        for child in w.findChildren(QLabel):
            repolish(child)
    except Exception:
        pass


def _clear_layout(lay):
    """清空布局并销毁其中的控件（takeAt 保证布局项不残留）。"""
    try:
        while lay.count():
            item = lay.takeAt(0)
            w = item.widget() if item is not None else None
            if w is not None:
                try:
                    w.setParent(None)
                    w.deleteLater()
                except Exception:
                    pass
    except Exception:
        pass


# ---- 线性图标（24x24 设计坐标；stroke 风格对齐 mockups/assets/parts.js 的 SVG）----
_GLYPHS = {
    "plus": {"lines": [[(12, 5), (12, 19)], [(5, 12), (19, 12)]]},
    "close": {"lines": [[(5, 5), (19, 19)], [(19, 5), (5, 19)]]},
    "check": {"lines": [[(4, 12.5), (9, 17.5), (20, 6.5)]]},
    "gear": {"circles": [((12, 12), 3)],
             "lines": [[(12, 3), (12, 5.5)], [(12, 18.5), (12, 21)],
                       [(4.2, 7.5), (6.4, 8.8)], [(17.6, 15.2), (19.8, 16.5)],
                       [(4.2, 16.5), (6.4, 15.2)], [(17.6, 8.8), (19.8, 7.5)]]},
    "alert": {"lines": [[(12, 3), (21, 19), (3, 19), (12, 3)],
                        [(12, 9), (12, 14)], [(12, 17), (12, 17.01)]]},
    "refresh": {"arcs": [((12, 12), 8, 45, 315)],
                "lines": [[(20, 4), (20, 9)], [(20, 4), (15, 4)]]},
    "search": {"circles": [((11, 11), 6)], "lines": [[(16, 16), (20, 20)]]},
    "external": {"lines": [[(14, 4), (20, 4), (20, 10)], [(20, 4), (12, 12)],
                           [(18, 14), (18, 19), (5, 19), (5, 7), (10, 7)]]},
    "trash": {"lines": [[(4, 7), (20, 7)], [(9, 7), (9, 5), (15, 5), (15, 7)],
                        [(6, 7), (7, 20), (17, 20), (18, 7)],
                        [(10, 11), (10, 17)], [(14, 11), (14, 17)]]},
    "folder": {"lines": [[(3, 7), (3, 19), (21, 19), (21, 9), (11, 9), (9, 7), (3, 7)]]},
    "archive": {"rects": [((3, 4, 18, 4), 1)],
                "lines": [[(5, 8), (5, 18)], [(19, 8), (19, 18)], [(5, 18), (19, 18)],
                          [(10, 12), (14, 12)]]},
    "terminal": {"rects": [((3, 4, 18, 16), 2)],
                 "lines": [[(7, 9), (10, 12), (7, 15)], [(13, 15), (17, 15)]]},
    "queue": {"lines": [[(4, 6), (20, 6)], [(4, 12), (20, 12)], [(4, 18), (13, 18)]],
              "circles": [((18.5, 18.5), 2.5)]},
    "key": {"circles": [((8, 12), 4)],
            "lines": [[(12, 12), (21, 12)], [(18, 12), (18, 15)], [(15, 12), (15, 14)]]},
    "history": {"arcs": [((12, 12), 9, 40, 330)],
                "lines": [[(3, 4), (3, 9), (8, 9)], [(12, 8), (12, 12), (15, 14)]]},
    "shield": {"lines": [[(12, 3), (19, 6), (19, 12), (12, 21), (5, 12), (5, 6), (12, 3)]]},
    "bolt": {"lines": [[(13, 3), (5, 14), (11, 14), (10, 21), (18, 10), (12, 10), (13, 3)]]},
    "dashboard": {"rects": [((3, 3, 7, 9), 1), ((14, 3, 7, 5), 1),
                            ((14, 11, 7, 10), 1), ((3, 15, 7, 6), 1)]},
    "pause": {"lines": [[(9, 5), (9, 19)], [(15, 5), (15, 19)]]},
    "download": {"lines": [[(12, 4), (12, 14)], [(8, 10), (12, 14), (16, 10)],
                           [(4, 19), (20, 19)]]},
    "more": {"circles": [((5, 12), 1.6), ((12, 12), 1.6), ((19, 12), 1.6)]},
}


def _draw_glyph(painter, name, rect, color, width=1.8):
    """把一个线性图标画进 rect（24x24 设计坐标等比缩放）。"""
    g = _GLYPHS.get(name)
    if not g:
        return
    painter.save()
    painter.setRenderHint(QPainter.Antialiasing, True)
    pen = QPen(QColor(color))
    pen.setWidthF(float(width))
    pen.setCapStyle(Qt.RoundCap)
    pen.setJoinStyle(Qt.RoundJoin)
    painter.setPen(pen)
    painter.setBrush(Qt.NoBrush)
    sx = rect.width() / 24.0
    sy = rect.height() / 24.0

    def _x(v):
        return rect.x() + v * sx

    def _y(v):
        return rect.y() + v * sy

    for poly in g.get("lines", []):
        path = QPainterPath()
        path.moveTo(_x(poly[0][0]), _y(poly[0][1]))
        for pt in poly[1:]:
            path.lineTo(_x(pt[0]), _y(pt[1]))
        painter.drawPath(path)
    for (cx, cy), r in g.get("circles", []):
        painter.drawEllipse(QPointF(_x(cx), _y(cy)), r * sx, r * sy)
    for (rx, ry, rw, rh), rad in g.get("rects", []):
        painter.drawRoundedRect(
            QRectF(_x(rx), _y(ry), rw * sx, rh * sy), rad * sx, rad * sy)
    for (cx, cy), r, a1, a2 in g.get("arcs", []):
        painter.drawArc(QRectF(_x(cx - r), _y(cy - r), 2 * r * sx, 2 * r * sy),
                        int(a1 * 16), int((a2 - a1) * 16))
    painter.restore()


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


def _qcolor(spec, fallback="#000000"):
    """把 token 颜色字符串转 QColor：QColor 不认 CSS 的 rgba(...)，这里手动解析。"""
    s = str(spec or "").strip()
    try:
        if s.startswith("rgba(") and s.endswith(")"):
            parts = [p.strip() for p in s[5:-1].split(",")]
            if len(parts) == 4:
                r, g, b = (int(round(float(p))) for p in parts[:3])
                a = float(parts[3])
                if a <= 1.0:
                    a = int(round(a * 255.0))
                return QColor(max(0, min(255, r)), max(0, min(255, g)),
                              max(0, min(255, b)), max(0, min(255, int(a))))
        return QColor(s)
    except Exception:
        return QColor(fallback)


def _lamp_color(state):
    """目录状态 -> 灯色（只由状态决定；跟着当前主题 token 走）。"""
    key = dir_state_key(state)
    if key == "extracting":
        return PALETTE["success"]
    if key == "waiting":
        return _tk("nbar_warn", PALETTE["muted"])
    if key == "error":
        return PALETTE["danger"]
    if key == "paused":
        return PALETTE["muted"]
    return PALETTE["accent"]


class _StatusLamp(QWidget):
    """状态灯（自绘圆点）：颜色只由 set_state 决定，选中/筛选态绝不影响它。"""

    SIZE = 8

    def __init__(self, parent=None):
        super().__init__(parent)
        self._state = "listening"
        self.setFixedSize(self.SIZE, self.SIZE)
        self.setAttribute(Qt.WA_TransparentForMouseEvents, True)

    def set_state(self, state):
        self._state = dir_state_key(state)
        self.update()

    def state(self):
        return self._state

    def color_name(self):
        """当前灯色的 #rrggbb（断言用）。"""
        return QColor(_lamp_color(self._state)).name()

    def paintEvent(self, event):
        p = QPainter(self)
        p.setRenderHint(QPainter.Antialiasing, True)
        p.setPen(Qt.NoPen)
        p.setBrush(QBrush(QColor(_lamp_color(self._state))))
        p.drawEllipse(QRectF(0.5, 0.5, self.width() - 1.0, self.height() - 1.0))
        p.end()


class DirChip(LayoutButton):
    """目录胶囊：状态灯 + 等宽路径 + 状态词 +（解压中）细进度条 + 齿轮。

    set_selected() 只翻转 selected 动态属性并 repolish，绝不改状态灯颜色。
    """

    clickedDir = pyqtSignal(int)
    CHIP_MAX_W = 280

    def __init__(self, idx, entry, parent=None):
        super().__init__(parent)
        self.idx = int(idx)
        self._entry = dict(entry or {})
        self._path = str(self._entry.get("path") or "")
        self._state = dir_state_key(self._entry.get("state"))
        self._progress = self._entry.get("progress")
        self._name = str(self._entry.get("name") or "")

        self.setObjectName("dirChip")
        self.setProperty("selected", False)
        self.setCursor(Qt.PointingHandCursor)
        self.setFixedHeight(30)
        self.setMaximumWidth(self.CHIP_MAX_W)
        self.setSizePolicy(QSizePolicy.Maximum, QSizePolicy.Fixed)

        lay = QHBoxLayout(self)
        lay.setContentsMargins(10, 0, 10, 0)
        lay.setSpacing(7)
        self.lamp = _StatusLamp(self)
        # DPI 小屏修复：路径标签用 _ElideLabel（最小宽度 0，随实际宽度重省略）。
        # 普通 QLabel 的 minimumSizeHint = 全文/省略文本宽度，胶囊被压时文字会被
        # 硬裁（机械对比 chip.width() < minimumSizeHint()）。
        self.path_label = _ElideLabel(self)
        self.path_label.setObjectName("chipPath")
        self.path_label.setAttribute(Qt.WA_TransparentForMouseEvents, True)
        self.state_label = QLabel(self)
        self.state_label.setObjectName("chipState")
        self.state_label.setAttribute(Qt.WA_TransparentForMouseEvents, True)
        self.progress = QProgressBar(self)
        self.progress.setObjectName("chipProg")
        self.progress.setTextVisible(False)
        self.progress.setRange(0, 100)
        self.progress.setFixedSize(46, 4)
        self.progress.setAttribute(Qt.WA_TransparentForMouseEvents, True)
        self.gear = Glyph("gear", self, 13, role="muted", opacity=0.55)
        lay.addWidget(self.lamp)
        lay.addWidget(self.path_label)
        lay.addWidget(self.state_label)
        lay.addWidget(self.progress)
        lay.addWidget(self.gear)
        self.clicked.connect(self._emit_clicked)
        self.set_state(self._state, self._progress, self._name or None)

    def _emit_clicked(self):
        self.clickedDir.emit(self.idx)

    def minimumSizeHint(self):
        """胶囊的真实最小尺寸：路径可继续省略，别的元素是固定的。

        LayoutButton.minimumSizeHint() 返回 sizeHint()（全文宽度）——对胶囊会
        虚报 700+，窄屏布局要么被顶宽、要么把胶囊误判成被裁。这里按「除路径
        外的固定件」算，路径交给 _ElideLabel 随实际宽度省略（DPI 小屏要求）。
        """
        try:
            fixed = 20 + 4 * 7 + self.lamp.width() + self.gear.width()
            fixed += self.state_label.sizeHint().width()
            if self._show_progress():
                fixed += self.progress.width()
            return QSize(fixed, 30)
        except Exception:
            return QSize(120, 30)

    def _show_progress(self):
        return self._progress is not None and self._state == "extracting"

    def _apply_text(self):
        # 全文交给 _ElideLabel：按胶囊当前宽度中间省略（resizeEvent 会自动重算），
        # 不再用 CHIP_MAX_W 预算硬裁 —— 窄屏下路径会继续收窄而不是被切掉。
        self.path_label.set_full_text(self._path)

    def set_state(self, state, progress=None, name=None):
        """更新状态灯 / 状态词 / 进度（进度只在解压中显示）。"""
        self._state = dir_state_key(state)
        self._progress = progress
        if name is not None:
            self._name = str(name)
        self.lamp.set_state(self._state)
        self.state_label.setText(DIR_STATE_TEXT.get(self._state, self._state))
        show = self._show_progress()
        self.progress.setVisible(show)
        pct = 0
        if progress is not None:
            try:
                pct = max(0, min(100, int(round(float(progress)))))
            except Exception:
                pct = 0
        self.progress.setValue(pct)
        tip = self._path
        if self._name:
            tip += "\n当前正在处理：%s" % self._name
        if show:
            tip += " · %d%%" % pct
        self.setToolTip(tip)
        self._apply_text()

    def set_selected(self, flag):
        """只翻转 selected 动态属性并 repolish；状态灯颜色保持不变。"""
        self.setProperty("selected", bool(flag))
        repolish_tree(self)

    def is_selected(self):
        return bool(self.property("selected"))

    def showEvent(self, event):
        # 首次显示时 QSS 字体才生效：按最终字体重量一次路径省略（构造期字体偏窄会多省略）
        super().showEvent(event)
        self._apply_text()


class DirChipStrip(QWidget):
    """目录胶囊条：常显于标签页下方；点胶囊 -> dirActivated(idx)（宿主开设置弹窗）。"""

    dirActivated = pyqtSignal(int)
    addRequested = pyqtSignal()
    netdiskRequested = pyqtSignal()

    def __init__(self, parent=None):
        super().__init__(parent)
        self.setObjectName("dirStrip")
        self.setAttribute(Qt.WA_StyledBackground, True)
        self.setSizePolicy(QSizePolicy.Preferred, QSizePolicy.Fixed)
        self._chips = []
        self._highlight = -1
        lay = QHBoxLayout(self)
        lay.setContentsMargins(12, 8, 12, 8)
        lay.setSpacing(7)

        self.add_btn = RainbowLayoutButton(self)     # §9：无路径时彩虹引导（_update_rainbow）
        self.add_btn.setObjectName("dirChip")
        self.add_btn.setProperty("add", True)
        self.add_btn.setCursor(Qt.PointingHandCursor)
        self.add_btn.setFixedHeight(30)
        self.add_btn.setSizePolicy(QSizePolicy.Maximum, QSizePolicy.Fixed)
        add_lay = QHBoxLayout(self.add_btn)
        add_lay.setContentsMargins(10, 0, 10, 0)
        add_lay.setSpacing(7)
        add_lay.addWidget(Glyph("plus", self.add_btn, 13, role="muted"))
        add_lay.addWidget(QLabel("添加目录", self.add_btn))
        self.add_btn.clicked.connect(self.addRequested.emit)
        lay.addWidget(self.add_btn)

        self._chips_box = QWidget(self)
        self._chip_lay = QHBoxLayout(self._chips_box)
        self._chip_lay.setContentsMargins(0, 0, 0, 0)
        self._chip_lay.setSpacing(7)
        lay.addWidget(self._chips_box)

        lay.addStretch(1)
        hint = QLabel("点目录可临时改设置 · 也可把压缩包直接拖进窗口", self)
        hint.setObjectName("stripHint")
        lay.addWidget(hint)
        self.netdisk_btn = LayoutButton(self)
        self.netdisk_btn.setObjectName("ghostSm")
        self.netdisk_btn.setCursor(Qt.PointingHandCursor)
        net_lay = QHBoxLayout(self.netdisk_btn)
        net_lay.setContentsMargins(0, 0, 0, 0)
        net_lay.setSpacing(6)
        net_lay.addWidget(Glyph("download", self.netdisk_btn, 13, role="muted"))
        net_lay.addWidget(QLabel("网盘下载目录", self.netdisk_btn))
        self.netdisk_btn.clicked.connect(self.netdiskRequested.emit)
        lay.addWidget(self.netdisk_btn)

    def set_dirs(self, entries):
        """按 entries（path/enabled/state/progress/name/…）整体重建胶囊。"""
        _clear_layout(self._chip_lay)
        self._chips = []
        self._highlight = -1
        for i, entry in enumerate(entries or []):
            if not isinstance(entry, dict):
                continue
            chip = DirChip(i, entry, self._chips_box)
            chip.clickedDir.connect(self.dirActivated)
            self._chip_lay.addWidget(chip)
            self._chips.append(chip)

    def update_dir_state(self, idx, state, progress=None, name=None):
        try:
            if 0 <= int(idx) < len(self._chips):
                self._chips[int(idx)].set_state(state, progress, name)
        except Exception:
            pass

    def set_highlight(self, idx):
        """「在这里」高亮（正在编辑设置的那一个）；-1 清除。"""
        self._highlight = int(idx)
        for i, chip in enumerate(self._chips):
            chip.set_selected(i == self._highlight)

    def highlight(self):
        return self._highlight

    def chips(self):
        return list(self._chips)


# 标签键 -> 图标（键不认识时只显示文字，不影响布局）
_TAB_ICONS = {
    "tasks": "queue", "task": "queue", "queue": "queue",
    "log": "terminal", "logs": "terminal", "logpage": "terminal",
    "password": "key", "passwords": "key", "book": "key",
    "trail": "history", "history": "history",
    "settings": "gear", "setting": "gear", "options": "gear",
}


class _NavTab(LayoutButton):
    """单个标签页按钮：可选图标 + 文本 + 计数徽标（count<=0 隐藏）。"""

    def __init__(self, key, label, parent=None):
        super().__init__(parent)
        self.key = str(key)
        self.setObjectName("navTab")
        self.setProperty("active", False)
        self.setCursor(Qt.PointingHandCursor)
        lay = QHBoxLayout(self)
        lay.setContentsMargins(15, 8, 15, 8)
        lay.setSpacing(6)
        icon = _TAB_ICONS.get(self.key)
        self.icon = None
        if icon:
            self.icon = Glyph(icon, self, 13, role="muted", opacity=0.9)
            lay.addWidget(self.icon)
        self.text_label = QLabel(str(label), self)
        self.text_label.setAttribute(Qt.WA_TransparentForMouseEvents, True)
        lay.addWidget(self.text_label)
        self.badge = QLabel(self)
        self.badge.setObjectName("navBadge")
        self.badge.setAttribute(Qt.WA_TransparentForMouseEvents, True)
        self.badge.hide()
        lay.addWidget(self.badge)

    def set_active(self, flag):
        flag = bool(flag)
        self.setProperty("active", flag)
        if self.icon is not None:
            self.icon.set_role("accent" if flag else "muted")
        repolish_tree(self)

    def set_badge(self, count):
        try:
            n = int(count)
        except Exception:
            n = 0
        self.badge.setText(str(n))
        self.badge.setVisible(n > 0)

    def badge_count(self):
        try:
            return int(self.badge.text() or 0)
        except Exception:
            return 0


class NavTabs(QWidget):
    """顶部标签页：互斥激活；(key, label) 列表 + 计数徽标。"""

    currentChanged = pyqtSignal(str)

    def __init__(self, parent=None):
        super().__init__(parent)
        self.setObjectName("navTabs")
        self.setAttribute(Qt.WA_StyledBackground, True)
        self.setSizePolicy(QSizePolicy.Preferred, QSizePolicy.Fixed)
        self._tabs = []
        self._current = None
        self._lay = QHBoxLayout(self)
        self._lay.setContentsMargins(4, 0, 12, 0)
        self._lay.setSpacing(2)
        self._lay.addStretch(1)

    def set_tabs(self, items):
        for tab in self._tabs:
            try:
                self._lay.removeWidget(tab)
                tab.setParent(None)
                tab.deleteLater()
            except Exception:
                pass
        self._tabs = []
        for i, pair in enumerate(items or []):
            key, label = pair[0], pair[1]
            tab = _NavTab(key, label, self)
            tab.clicked.connect(lambda _=False, k=str(key): self._on_clicked(k))
            self._lay.insertWidget(i, tab)
            self._tabs.append(tab)
        keys = [t.key for t in self._tabs]
        if self._current not in keys:
            self._current = keys[0] if keys else None
        for tab in self._tabs:
            tab.set_active(tab.key == self._current)

    def _on_clicked(self, key):
        self._current = key
        for tab in self._tabs:
            tab.set_active(tab.key == key)
        self.currentChanged.emit(key)

    def set_current(self, key):
        key = str(key)
        if key not in [t.key for t in self._tabs]:
            return
        self._current = key
        for tab in self._tabs:
            tab.set_active(tab.key == key)

    def current(self):
        return self._current

    def set_badge(self, key, count):
        """count<=0 隐藏徽标。"""
        for tab in self._tabs:
            if tab.key == str(key):
                tab.set_badge(count)
                return

    def tabs(self):
        return list(self._tabs)


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


# ---- 任务表 ----
_STATE_TEXT = {
    "queued": "排队", "extracting": "解压中", "need_password": "待密码",
    "done": "已完成", "failed": "失败", "canceled": "已取消",
}
_STATE_ALIASES = {
    "pending": "queued", "waiting": "need_password", "wait": "need_password",
    "error": "failed", "success": "done", "cancelled": "canceled",
    "running": "extracting", "ok": "done",
}


def _task_state_key(row):
    s = str(row.get("state") or "").strip().lower()
    return _STATE_ALIASES.get(s, s)


# 显示行指纹用的字段：任一字段变化都会改变表格内容（用于跳过无变化的整表重建）
_TASK_SIG_KEYS = ("id", "state", "state_text", "file_name", "file", "output_dir",
                  "out", "size", "file_size", "pwd", "password_src", "cost",
                  "started_at", "finished_at", "created_at", "error", "progress")


def _task_rows_signature(rows):
    """显示行集合的廉价指纹：只比较会影响展示的字段，不做任何格式化。"""
    return tuple(tuple(r.get(k) for k in _TASK_SIG_KEYS)
                 for r in (rows or []) if isinstance(r, dict))


def _row_file(row):
    return str(row.get("file_name") or row.get("file") or "")


def _row_out(row):
    return str(row.get("output_dir") or row.get("out") or "")


def _fmt_size(n):
    try:
        v = float(n)
    except Exception:
        return "—"
    if v <= 0:
        return "—"
    for div, name in ((1024 ** 3, "GB"), (1024 ** 2, "MB"), (1024, "KB")):
        if v >= div:
            x = v / div
            txt = ("%.1f" % x) if x < 100 else ("%.0f" % x)
            if txt.endswith(".0"):
                txt = txt[:-2]
            return "%s %s" % (txt, name)
    return "%d B" % int(v)


def _fmt_cost(row):
    try:
        st = float(row.get("started_at") or 0)
        ft = float(row.get("finished_at") or 0)
    except Exception:
        return "—"
    if st <= 0 or ft <= 0 or ft < st:
        return "—"
    secs = int(round(ft - st))
    return "%02d:%02d" % (secs // 60, secs % 60)


def _fmt_pwd(row):
    src = str(row.get("password_src") or "").strip()
    if not src:
        state = _task_state_key(row)
        if state == "need_password":
            return "未命中"
        if state == "done":
            return "无需"
        return "—"
    if "#" in src:
        kind, _, num = src.partition("#")
        return "命中 #%s" % num if kind in ("book", "dict") else src
    if src == "filename":
        return "命中 文件名"
    return src


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


def _pill_colors(state):
    """状态胶囊的 (底, 边, 字) 三色；全部来自 token/PALETTE。"""
    if state == "extracting":
        return _tk("accent_soft"), _tk("ctl_focus"), PALETTE["accent_text"]
    if state == "need_password":
        return _tk("pause_bg"), _tk("pause_border"), _tk("pause_fg")
    if state == "done":
        return _tk("accent_soft"), _tk("card_border"), PALETTE["success"]
    if state == "failed":
        return PALETTE["warn_bg"], _tk("danger_border"), _tk("danger_fg")
    return _tk("cat_hover"), _tk("card_border"), PALETTE["muted2"]


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
    """任务表：TaskModel + 状态胶囊 + 行内操作（打开输出目录 / 重试）。"""

    taskActivated = pyqtSignal(int)
    actionTriggered = pyqtSignal(int, str)   # task_id, 'open_dir'|'retry'

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
                           (TaskModel.COL_ACT, 72)):
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
        for action, glyph, tip in (("open_dir", "external", "打开输出目录"),
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
        if not index.isValid():
            return
        task_id = self._model.data(index, Qt.UserRole)
        try:
            self.taskActivated.emit(int(task_id))
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
        for key in (item.get("actions") or []):
            text = _NEED_ACTION_TEXT.get(str(key))
            if not text:
                continue
            btn = QPushButton(text, self)
            btn.setObjectName("ghostSm")
            btn.setCursor(Qt.PointingHandCursor)
            btn.clicked.connect(
                lambda _=False, k=str(key): self.actionTriggered.emit(self.task_id, k))
            actions.addWidget(btn)
            self.action_buttons[str(key)] = btn
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


# ---------------------------------------------------------------------------
# 点击反馈小气泡（日志链接「已复制 / 复制失败」）：无边框、绝不抢焦点、自动消失
# ---------------------------------------------------------------------------
# 活动气泡强引用表：顶层 ToolTip 窗没有父控件接管生命周期，不持有引用会被 GC
# 提前回收；close_toast() 关闭时自行注销，不留悬挂引用。
_ACTIVE_TOASTS = []


class ToastBubble(QLabel):
    """自动消失的提示气泡：定位在给定全局坐标附近，msec 毫秒后淡出自关。

    - 顶层 ToolTip 窗（Qt.ToolTip | Qt.FramelessWindowHint）：不进任务栏；
    - WA_TransparentForMouseEvents + WA_ShowWithoutActivating：鼠标事件穿透、
      绝不抢焦点（点击日志链接时不打断用户正在进行的操作）；
    - 越出屏幕可用区时自动收进，底部放不下则翻到坐标点上方；
    - QTimer 以自身为 parent，随控件销毁；QGraphicsOpacityEffect 淡出失败
      也必须关闭（异常安全，绝不让气泡卡在屏幕上）。
    """

    def __init__(self, global_pos, text, msec=900, parent=None):
        super().__init__(parent)
        self.setObjectName("toastBubble")
        self.setWindowFlags(Qt.ToolTip | Qt.FramelessWindowHint)
        self.setAttribute(Qt.WA_TransparentForMouseEvents, True)
        self.setAttribute(Qt.WA_ShowWithoutActivating, True)
        self.setText(str(text))
        self._closed = False
        self._fade_anim = None
        self.adjustSize()
        self._move_near(global_pos)
        self._timer = QTimer(self)
        self._timer.setSingleShot(True)
        try:
            self._timer.setInterval(max(1, int(msec)))
        except Exception:
            self._timer.setInterval(900)
        self._timer.timeout.connect(self._fade_out)
        self._timer.start()

    # ---- 定位 ----
    def _available_geometry(self, global_pos):
        """给定全局坐标所在屏幕的可用区；取不到返回 None（此时不做钳制）。"""
        try:
            scr = QApplication.screenAt(global_pos)
            if scr is None:
                scr = QApplication.primaryScreen()
            if scr is not None:
                return scr.availableGeometry()
        except Exception:
            pass
        return None

    def _move_near(self, global_pos):
        """默认显示在坐标点右下方；越界则收进屏幕，底部放不下翻到点上方。"""
        try:
            x = int(global_pos.x()) + 12
            y = int(global_pos.y()) + 16
            geo = self._available_geometry(global_pos)
            if geo is not None:
                w, h = self.width(), self.height()
                right = geo.x() + geo.width() - w
                bottom = geo.y() + geo.height() - h
                if x > right:
                    x = right
                if y > bottom:
                    y = int(global_pos.y()) - h - 8
                x = max(geo.x(), x)
                y = max(geo.y(), y)
            self.move(x, y)
        except Exception:
            pass

    # ---- 关闭 ----
    def _fade_out(self):
        """到点淡出（约 150ms）；任何特效失败都立即关闭，绝不留下悬挂气泡。"""
        if self._closed:
            return
        try:
            eff = QGraphicsOpacityEffect(self)
            self.setGraphicsEffect(eff)
            anim = QPropertyAnimation(eff, b"opacity", self)
            anim.setDuration(150)
            anim.setStartValue(1.0)
            anim.setEndValue(0.0)
            anim.setEasingCurve(QEasingCurve.OutCubic)
            anim.finished.connect(self.close_toast)
            self._fade_anim = anim
            anim.start()
        except Exception:
            self.close_toast()

    def close_toast(self):
        """立即关闭并注销（幂等；计时器随控件一起销毁）。"""
        if self._closed:
            return
        self._closed = True
        try:
            self._timer.stop()
        except Exception:
            pass
        try:
            if self in _ACTIVE_TOASTS:
                _ACTIVE_TOASTS.remove(self)
        except Exception:
            pass
        try:
            self.hide()
        except Exception:
            pass
        try:
            self.deleteLater()
        except Exception:
            pass


def show_toast(anchor_widget, global_pos, text, msec=900):
    """在 global_pos（全局坐标）附近弹一个自动消失的气泡，返回控件或 None。

    anchor_widget：宿主控件，仅用于建立 transient 关系；不是 QWidget（如既有
    测试桩）时忽略。任何创建失败都静默返回 None——提示气泡绝不打断主流程。
    """
    try:
        parent = anchor_widget if isinstance(anchor_widget, QWidget) else None
        toast = ToastBubble(global_pos, text, msec, parent)
        _ACTIVE_TOASTS.append(toast)
        toast.show()
        return toast
    except Exception:
        return None


