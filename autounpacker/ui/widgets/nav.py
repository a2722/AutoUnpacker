# -*- coding: utf-8 -*-
"""导航类控件：DirChip / DirChipStrip 目录胶囊条、NavTabs 标签页、
以及回溯状态 / 目录状态 / 底栏播报文案表。"""

from PyQt5.QtWidgets import (QLabel, QWidget, QHBoxLayout, QProgressBar,
                             QSizePolicy)
from PyQt5.QtCore import Qt, QSize, pyqtSignal

from ..style import PALETTE
from .common import dir_state_key, repolish_tree, _clear_layout, _StatusLamp
from .inputs import (LayoutButton, RainbowLayoutButton, Glyph, _ElideLabel)
TRAIL_STATUS_TEXT = {
    "recorded": "已记录（处理中）",
    "kept": "未删除",
    "deleted": "已删除（回收站）",
    "restored": "已还原",
    "failed": "解压失败",
}


TRAIL_STATUS_COLORS = PALETTE["trail"]   # 与 style.PALETTE 同一对象，随主题就地更新


TRAIL_STATUS_ORDER = ["deleted", "restored", "kept", "failed", "recorded"]


# 目录状态文案（FINAL-SPEC §7）；running/idle 是原型数据的别名
DIR_STATE_TEXT = {
    "listening": "监听中", "extracting": "解压中", "waiting": "等待中",
    "paused": "已暂停", "error": "错误",
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
