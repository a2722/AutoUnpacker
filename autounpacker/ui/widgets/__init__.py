# -*- coding: utf-8 -*-
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

# ---------------------------------------------------------------------------
# 子模块布局（旧 ui/widgets.py 按职责拆分；本文件只做重导出，保持
# `autounpacker.ui.widgets.X` 与旧模块完全一致，含以 `_` 开头的内部名）：
#   common.py  共享 helper/常量（token 取色、repolish、线性图标、状态灯、行格式化…）
#   inputs.py  HotkeyEdit / WatchCard / 热键过滤器 / 托盘图标 / Glyph / SegControl / 筛选 chip
#   nav.py     DirChip / DirChipStrip / NavTabs 及导航文案表
#   tasks.py   TaskModel / TaskTable / 状态胶囊委托
#   status.py  底栏播报 / 需要处理卡片 / 模式卡选择器
#   toast.py   ToastBubble / show_toast / _ACTIVE_TOASTS（全包唯一共享列表）
# 注意：以下第三方 / 配置名的重导出只为保持旧模块的 dir() 表面（历史行为）。
# ---------------------------------------------------------------------------
import ctypes  # noqa: F401

from PyQt5.QtWidgets import (  # noqa: F401
    QVBoxLayout, QHBoxLayout, QLabel, QLineEdit, QPushButton, QCheckBox,
    QFileDialog, QFrame, QComboBox, QWidget, QProgressBar, QButtonGroup,
    QSizePolicy, QTableView, QHeaderView, QAbstractItemView,
    QStyledItemDelegate, QStyleOptionViewItem, QStyle, QApplication,
    QGraphicsOpacityEffect)
from PyQt5.QtCore import (  # noqa: F401
    Qt, QTimer, QRectF, QEvent, QSize, pyqtSignal,
    QAbstractNativeEventFilter, QAbstractTableModel, QModelIndex, QPoint,
    QPointF, QPropertyAnimation, QEasingCurve)
from PyQt5.QtGui import (  # noqa: F401
    QIcon, QPixmap, QPainter, QColor, QBrush, QPen, QConicalGradient,
    QPainterPath, QPalette, QFont)

from ...config import (  # noqa: F401
    _HK_NAME_BY_VK, HOTKEY_ID, HOTKEY_ID_SHARE, HOTKEY_ID_SHARE_PICK,
    WM_HOTKEY)
from ..style import PALETTE  # noqa: F401
from .. import style as ui_style  # noqa: F401

from .common import (  # noqa: F401
    WM_SETTINGCHANGE, win32gui,
    _key_display_name, dir_state_key, _tk, repolish, repolish_tree,
    _clear_layout, _GLYPHS, _draw_glyph, _qcolor, _lamp_color, _StatusLamp,
    _DIR_STATE_ALIASES, _STATE_ALIASES, _task_state_key, _TASK_SIG_KEYS,
    _task_rows_signature, _row_file, _row_out, _fmt_size, _fmt_cost,
    _fmt_pwd, _pill_colors)
from .inputs import (  # noqa: F401
    HotkeyEdit, make_tray_icon, WatchCard, _HotkeyFilter, RainbowBorderButton,
    Glyph, LayoutButton, RainbowLayoutButton, SegControl, _ElideLabel,
    _FilterChip, FilterChipStrip)
from .nav import (  # noqa: F401
    TRAIL_STATUS_TEXT, TRAIL_STATUS_COLORS, TRAIL_STATUS_ORDER,
    DIR_STATE_TEXT, DEFAULT_TIPS, DirChip, DirChipStrip, _TAB_ICONS,
    _NavTab, NavTabs)
from .tasks import (  # noqa: F401
    _STATE_TEXT, TaskModel, _StatePillDelegate, TaskTable)
from .status import (  # noqa: F401
    StatusTipTicker, _NEED_ACTION_TEXT, _NEED_ACTION_TIP, _NeedRow,
    NeedsAttentionCard, ModeCard, ModeSelector)
from .toast import _ACTIVE_TOASTS, ToastBubble, show_toast  # noqa: F401
