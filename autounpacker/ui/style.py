# -*- coding: utf-8 -*-
"""主题系统：两套配色（fluent 浅色 / devtool 深色）+ 系统深浅色检测 + 应用/切换。

职责：
- 定义两套主题 token（颜色 + 圆角），由**同一个 QSS 模板**渲染出对应样式表；
- `PALETTE`：散落在各 UI 文件里的「内联颜色」集中在此，随主题就地更新；
- `detect_system_theme()`：读注册表判断系统深浅色（单次约 0.03ms，无 COM/无子进程）；
- `resolve_theme(pref)`：把配置偏好 auto/fluent/devtool 解析成具体主题；
- `apply_theme(app, theme)`：套用样式表；深色额外套 Fusion + 深色 QPalette
  （否则下拉箭头/勾选/微调按钮等「原生绘制件」会残留浅色）。

关键入口：build_style() / detect_system_theme() / resolve_theme() / apply_theme() /
          PALETTE / STYLE（兼容旧引用，等价 fluent）

依赖：仅标准库 string/winreg；PyQt5 仅在 apply_theme 内惰性导入。

注意（启动性能）：**检测很快，慢的是「应用」**。启动时不要检测——用上次记住的主题
先出首屏，等窗口显示后再 `detect_system_theme()` 纠正（见 app.py）。系统切换深浅色时
Windows 会广播 WM_SETTINGCHANGE(lParam="ImmersiveColorSet")，主窗口据此重套主题，
零轮询。

注意（QSS 限制）：QSS 无 box-shadow / 变量 / 亚克力；QFileDialog 为系统原生对话框，
永远浅色，无法随主题变化。
"""
from string import Template

THEMES = ("fluent", "devtool")
DEFAULT_THEME = "fluent"

# ---------------------------------------------------------------------------
# 主题 token：颜色 + 圆角（两套主题只差这些值，QSS 模板只有一份）
# ---------------------------------------------------------------------------
_FLUENT = {
    "window_bg": "#f3f3f3", "window_fg": "#1b1b1b",
    "card_bg": "#ffffff", "card_border": "#e5e5e5", "radius_card": "8px",
    "title_fg": "#0067c0", "section_fg": "#5b5b5b",
    "ctl_bg": "#ffffff", "ctl_border": "#d1d1d1", "ctl_focus": "#0067c0",
    "ctl_fg": "#1b1b1b", "radius_ctl": "4px",
    "sel_bg": "#0067c0", "sel_fg": "#ffffff",
    "item_sel_bg": "#e5f1fb", "item_sel_fg": "#005a9e",
    "btn_bg": "#fbfbfb", "btn_border": "#d1d1d1", "btn_fg": "#1b1b1b",
    "btn_hover": "#f2f2f2", "btn_pressed": "#ebebeb",
    "btn_dis_bg": "#f5f5f5", "btn_dis_fg": "#9a9a9a", "btn_dis_border": "#e5e5e5",
    "primary_bg": "#0067c0", "primary_hover": "#1a76c9", "primary_pressed": "#005ba3",
    "primary_fg": "#ffffff",
    "danger_bg": "#fdf3f4", "danger_fg": "#c42b1c", "danger_border": "#f1c0c4",
    "danger_hover": "#fbe6e8",
    "pause_bg": "#fff4ce", "pause_border": "#e8d07a", "pause_fg": "#6b5200",
    "pause_hover": "#ffe9a8",
    "paused_bg": "#0067c0", "paused_hover": "#1a76c9", "paused_fg": "#ffffff",
    "prog_bg": "#e5e5e5", "prog_chunk": "#0067c0",
    "table_bg": "#ffffff", "table_border": "#e5e5e5", "table_grid": "#f0f0f0",
    "table_alt": "#fafafa", "table_sel_bg": "#cce4f7", "table_sel_fg": "#0f3d5c",
    "head_bg": "#f7f7f7", "head_fg": "#616161", "head_border": "#e5e5e5",
    "log_bg": "#191919", "log_fg": "#d4d4d4", "log_border": "#e5e5e5",
    "cat_bg": "#ffffff", "cat_border": "#e5e5e5", "cat_fg": "#1b1b1b",
    "cat_hover": "#f5f5f5",
    "group_border": "#e5e5e5", "group_title": "#5b5b5b",
    "menu_bg": "#ffffff", "menu_border": "#e5e5e5", "menu_sel_bg": "#e5f1fb",
    "menu_sel_fg": "#1b1b1b", "menu_sep": "#e5e5e5",
    "tip_bg": "#ffffff", "tip_fg": "#1b1b1b", "tip_border": "#d1d1d1",
    "sb_handle": "#c8c8c8", "sb_hover": "#b0b0b0",
    "ind_bg": "#ffffff", "ind_border": "#8a8a8a",
}

_DEVTOOL = {
    "window_bg": "#1e1e1e", "window_fg": "#cccccc",
    "card_bg": "#2d2d30", "card_border": "#3c3c3c", "radius_card": "4px",
    "title_fg": "#4daafc", "section_fg": "#cccccc",
    "ctl_bg": "#3c3c3c", "ctl_border": "#565656", "ctl_focus": "#007fd4",
    "ctl_fg": "#cccccc", "radius_ctl": "2px",
    "sel_bg": "#094771", "sel_fg": "#ffffff",
    "item_sel_bg": "#094771", "item_sel_fg": "#ffffff",
    "btn_bg": "#3a3d41", "btn_border": "#3a3d41", "btn_fg": "#cccccc",
    "btn_hover": "#45494e", "btn_pressed": "#2f3236",
    "btn_dis_bg": "#2d2d30", "btn_dis_fg": "#6b6b6b", "btn_dis_border": "#3c3c3c",
    "primary_bg": "#0e639c", "primary_hover": "#1177bb", "primary_pressed": "#0a4d7a",
    "primary_fg": "#ffffff",
    "danger_bg": "#5a1d1d", "danger_fg": "#f48771", "danger_border": "#8a2b2b",
    "danger_hover": "#6b2323",
    "pause_bg": "#4a3b12", "pause_border": "#6b5518", "pause_fg": "#e8b454",
    "pause_hover": "#5a4818",
    "paused_bg": "#0e639c", "paused_hover": "#1177bb", "paused_fg": "#ffffff",
    "prog_bg": "#3c3c3c", "prog_chunk": "#007acc",
    "table_bg": "#252526", "table_border": "#3c3c3c", "table_grid": "#333333",
    "table_alt": "#2d2d30", "table_sel_bg": "#094771", "table_sel_fg": "#ffffff",
    "head_bg": "#2d2d30", "head_fg": "#cccccc", "head_border": "#3c3c3c",
    "log_bg": "#1b1b1b", "log_fg": "#d4d4d4", "log_border": "#3c3c3c",
    "cat_bg": "#252526", "cat_border": "#3c3c3c", "cat_fg": "#cccccc",
    "cat_hover": "#2a2d2e",
    "group_border": "#3c3c3c", "group_title": "#cccccc",
    "menu_bg": "#252526", "menu_border": "#454545", "menu_sel_bg": "#094771",
    "menu_sel_fg": "#ffffff", "menu_sep": "#454545",
    "tip_bg": "#252526", "tip_fg": "#cccccc", "tip_border": "#454545",
    "sb_handle": "#424242", "sb_hover": "#4f4f4f",
    "ind_bg": "#2d2d30", "ind_border": "#9a9a9a",
}

_TOKENS = {"fluent": _FLUENT, "devtool": _DEVTOOL}

# ---------------------------------------------------------------------------
# 内联颜色调色板：各 UI 文件 `from .style import PALETTE`，随主题**就地更新**
# ---------------------------------------------------------------------------
_PALETTES = {
    "fluent": {
        "accent": "#0067c0", "accent_text": "#0067c0",
        "danger": "#c0392b", "muted": "#6b7688", "muted2": "#3d4756",
        "info": "#2e86c1", "success": "#2e7d32",
        "log_error": "#ff8080", "log_success": "#8be28b",
        "log_info": "#7fb6ff", "log_wait": "#f2c97d", "log_default": "#d8e0ea",
        "warn_text": "#c0392b", "warn_bg": "#ffe4e4", "warn_border": "#f2c2c2",
        "tray_icon": "#1f6feb",
        "trail": {"recorded": "#8a94a6", "kept": "#2e7d32", "deleted": "#c0392b",
                  "restored": "#1f6feb", "failed": "#ad1457"},
    },
    "devtool": {
        "accent": "#0e639c", "accent_text": "#4daafc",
        "danger": "#ff6b6b", "muted": "#9aa4b2", "muted2": "#b8c0cc",
        "info": "#5aa9ff", "success": "#6fcf7f",
        "log_error": "#f44747", "log_success": "#4ec9b0",
        "log_info": "#569cd6", "log_wait": "#dcdcaa", "log_default": "#cccccc",
        "warn_text": "#ff9a9a", "warn_bg": "#3a1f22", "warn_border": "#7a3b40",
        "tray_icon": "#4daafc",
        "trail": {"recorded": "#9aa4b2", "kept": "#6fcf7f", "deleted": "#ff6b6b",
                  "restored": "#5aa9ff", "failed": "#ff6fa5"},
    },
}

# 就地可变的全局调色板（import 到别处也能看到主题切换后的新值；
# 嵌套的 "trail" 子字典也保持同一对象，切换时就地更新）
PALETTE = {k: (dict(v) if isinstance(v, dict) else v)
           for k, v in _PALETTES[DEFAULT_THEME].items()}

# ---------------------------------------------------------------------------
# QSS 模板（$token 占位，用 string.Template 渲染；避免 f-string 的花括号转义）
# ---------------------------------------------------------------------------
_QSS = Template("""
/* 只给顶层窗口上底色；普通 QWidget 不画底色（否则卡片/页面内部会出现
   「一块异色底」——浅色下不明显，深色下一眼可见） */
QMainWindow, QDialog { background: $window_bg; }
QWidget { color: $window_fg; font-size: 13px; }
/* 注意：QCheckBox / QRadioButton **不要**放进来设 background: transparent。
   一旦给它们设了背景，QStyleSheetStyle 会把绘制指示器（勾选框/单选圈）时所用
   的调色板 Base 一并变成透明/黑，深色下 Fusion 画出来的方框/圆圈就与背景同色
   → 完全看不见（浅色用原生 windowsvista 画指示器，不受影响）。不设背景时它们
   本就不绘制底色（非 WA_StyledBackground），无需透明。 */
QLabel, QGroupBox, QStackedWidget, QScrollArea, QFrame {
    background: transparent;
}

/* 卡片 + 统计条（#statcard 不带类型：修掉「选择器类型对不上、样式从未生效」的老 bug） */
QFrame#card, #statcard {
    background: $card_bg; border: 1px solid $card_border; border-radius: $radius_card;
}
QLabel#appTitle { font-size: 18px; font-weight: bold; color: $title_fg; }
QLabel#sectionTitle { font-size: 13px; font-weight: 600; color: $section_fg; }

QLineEdit, QSpinBox, QComboBox {
    background: $ctl_bg; border: 1px solid $ctl_border; border-radius: $radius_ctl;
    padding: 5px 8px; min-height: 22px; color: $ctl_fg;
    selection-background-color: $sel_bg; selection-color: $sel_fg;
}
QLineEdit:focus, QSpinBox:focus, QComboBox:focus, QComboBox:on {
    border: 1px solid $ctl_focus;
}
QComboBox::drop-down { border: none; width: 22px; }
QComboBox QAbstractItemView {
    background: $menu_bg; border: 1px solid $menu_border; color: $ctl_fg;
    selection-background-color: $item_sel_bg; selection-color: $item_sel_fg;
    outline: none;
}
/* 弹出列表行高：给每项加内边距，避免行高塌成「字高」。注意原生样式
   （windowsvista）的委托不认这条 QSS，需配合 _ensure_combo_popup_fix()
   换成 QStyledItemDelegate 才生效；Fusion 下同样受用，保证两套主题的
   弹出高度一致。 */
QComboBox QAbstractItemView::item { padding: 4px 8px; }

QPushButton {
    background: $btn_bg; border: 1px solid $btn_border; border-radius: $radius_ctl;
    padding: 5px 12px; color: $btn_fg;
}
QPushButton:hover { background: $btn_hover; }
QPushButton:pressed { background: $btn_pressed; }
QPushButton:disabled {
    background: $btn_dis_bg; color: $btn_dis_fg; border-color: $btn_dis_border;
}
QPushButton#primary { background: $primary_bg; color: $primary_fg; border: none; }
QPushButton#primary:hover { background: $primary_hover; }
QPushButton#primary:pressed { background: $primary_pressed; }
QPushButton#danger {
    background: $danger_bg; color: $danger_fg; border: 1px solid $danger_border;
}
QPushButton#danger:hover { background: $danger_hover; }
QPushButton#pause {
    background: $pause_bg; border: 1px solid $pause_border; border-radius: $radius_ctl;
    padding: 3px 8px; color: $pause_fg; font-weight: bold;
}
QPushButton#pause:hover { background: $pause_hover; }
QPushButton#pause[paused="true"] { background: $paused_bg; border: none; color: $paused_fg; }
QPushButton#pause[paused="true"]:hover { background: $paused_hover; }

QProgressBar { background: $prog_bg; border: none; border-radius: 4px; max-height: 8px; }
QProgressBar::chunk { background: $prog_chunk; border-radius: 4px; }

QCheckBox, QRadioButton { spacing: 6px; color: $window_fg; }
/* 「未选中」框：只用 QSS 补一条可见描边。不给 :checked 设规则，
   以免盖掉样式原生绘制的对勾 / 圆点（选中态仍是原生外观，清晰可见）。
   不这样做的话，深色下原生描边由调色板推导、颜色≈背景，框会「隐形」。 */
QCheckBox::indicator:unchecked {
    width: 15px; height: 15px; border: 1px solid $ind_border;
    background: $ind_bg; border-radius: 3px;
}
QRadioButton::indicator:unchecked {
    width: 15px; height: 15px; border: 1px solid $ind_border;
    background: $ind_bg; border-radius: 8px;
}

QTableWidget {
    background: $table_bg; border: 1px solid $table_border;
    border-radius: $radius_card; gridline-color: $table_grid;
    alternate-background-color: $table_alt; color: $window_fg;
    selection-background-color: $table_sel_bg; selection-color: $table_sel_fg;
}
QTableWidget::item { padding: 3px 6px; }
QHeaderView::section {
    background: $head_bg; color: $head_fg; font-weight: bold;
    border: none; border-bottom: 1px solid $head_border; padding: 6px 8px;
}
QTableCornerButton::section { background: $head_bg; border: none; }

QPlainTextEdit {
    background: $log_bg; color: $log_fg; border: 1px solid $log_border;
    border-radius: $radius_card; padding: 6px;
    font-family: Consolas, monospace; font-size: 12px;
}

QScrollArea { border: none; background: transparent; }
QListWidget#settingsCat {
    background: $cat_bg; border: 1px solid $cat_border;
    border-radius: $radius_card; padding: 6px; outline: none; color: $cat_fg;
}
QListWidget#settingsCat::item {
    padding: 9px 12px; border-radius: $radius_ctl; margin: 1px 0; color: $cat_fg;
}
QListWidget#settingsCat::item:hover { background: $cat_hover; }
QListWidget#settingsCat::item:selected {
    background: $item_sel_bg; color: $item_sel_fg; font-weight: bold;
}

QGroupBox {
    border: 1px solid $group_border; border-radius: $radius_card;
    margin-top: 12px; padding: 10px 10px 8px 10px;
}
QGroupBox::title {
    subcontrol-origin: margin; left: 10px; padding: 0 4px; color: $group_title;
}
QStackedWidget { background: transparent; }

QMenu { background: $menu_bg; border: 1px solid $menu_border; padding: 4px; }
QMenu::item { padding: 6px 18px; border-radius: $radius_ctl; }
QMenu::item:selected { background: $menu_sel_bg; color: $menu_sel_fg; }
QMenu::separator { height: 1px; background: $menu_sep; margin: 4px 6px; }

QToolTip {
    background: $tip_bg; color: $tip_fg; border: 1px solid $tip_border; padding: 4px;
}

QScrollBar:vertical { background: transparent; width: 11px; }
QScrollBar::handle:vertical { background: $sb_handle; border-radius: 5px; min-height: 24px; }
QScrollBar::handle:vertical:hover { background: $sb_hover; }
QScrollBar::add-line:vertical, QScrollBar::sub-line:vertical { height: 0; }
QScrollBar:horizontal { background: transparent; height: 11px; }
QScrollBar::handle:horizontal { background: $sb_handle; border-radius: 5px; min-width: 24px; }
QScrollBar::handle:horizontal:hover { background: $sb_hover; }
QScrollBar::add-line:horizontal, QScrollBar::sub-line:horizontal { width: 0; }
""")


def build_style(theme=DEFAULT_THEME):
    """按主题名渲染 QSS；未知主题回退 fluent。"""
    tk = _TOKENS.get(str(theme or "").lower(), _FLUENT)
    try:
        return _QSS.substitute(**tk)
    except Exception:
        return _QSS.substitute(**_FLUENT)


# 兼容旧引用（app.py 等）：等价于浅色主题
STYLE = build_style(DEFAULT_THEME)


# ---------------------------------------------------------------------------
# 系统深浅色检测 / 偏好解析 / 应用
# ---------------------------------------------------------------------------
def detect_system_theme():
    """读注册表判断系统「应用」深浅色，返回 'fluent'(浅) / 'devtool'(深)。

    只读 HKCU，单次约 0.03ms，无 COM、无子进程、无依赖；任何异常回退浅色。
    AppsUseLightTheme: 1=浅色，0=深色。
    """
    try:
        import winreg
        with winreg.OpenKey(
                winreg.HKEY_CURRENT_USER,
                r"Software\Microsoft\Windows\CurrentVersion\Themes\Personalize") as k:
            v, _ = winreg.QueryValueEx(k, "AppsUseLightTheme")
        return "fluent" if int(v) else "devtool"
    except Exception:
        return DEFAULT_THEME


def resolve_theme(pref):
    """把配置偏好解析成具体主题：auto -> 跟随系统；fluent/devtool -> 强制。"""
    p = str(pref or "auto").strip().lower()
    if p in THEMES:
        return p
    return detect_system_theme()


def refresh_palette(theme):
    """把 PALETTE 就地更新为目标主题（保持对象标识与其 "trail" 子字典标识，
    这样其它模块 import 到的引用在切换主题后依然有效）。"""
    src = _PALETTES.get(str(theme or "").lower(), _PALETTES[DEFAULT_THEME])
    for k, v in src.items():
        if k == "trail":
            d = PALETTE.get("trail")
            if isinstance(d, dict):
                d.clear()
                d.update(v)
            else:
                PALETTE["trail"] = dict(v)
        else:
            PALETTE[k] = v
    return PALETTE


# 首套样式应用前捕获的「原生风格 / 调色板」，用于切回浅色时还原
_BASE = {"style": None, "palette": None}
# 当前已应用的主题（供上层判断是否需要切换）
_CURRENT = {"theme": DEFAULT_THEME}


def current_theme():
    """返回当前已应用的主题名。"""
    return _CURRENT["theme"]


def _capture_base(app):
    if _BASE["style"] is None:
        try:
            _BASE["style"] = app.style().objectName()
        except Exception:
            _BASE["style"] = "windowsvista"
        try:
            _BASE["palette"] = app.palette()
        except Exception:
            _BASE["palette"] = None


# 运行时生成的主题图标对应的 QSS 片段缓存（见 _theme_extra_qss）
_EXTRA_QSS = {}


def _theme_extra_qss(theme):
    """生成主题取色的图标并返回需要**运行时注入**的 QSS 片段（按主题缓存）。

    这里放两类「QSS 天然缺口」的补丁：

    1. 下拉箭头 —— 只要用 QSS 定制了 `QComboBox::drop-down`（本样式表为了扁平
       外观设了 `border: none; width: 22px`），Qt 就**不再自动绘制**
       `::down-arrow`（浅色 windowsvista / 深色 Fusion 都一样），组合框于是
       没有任何下拉箭头。而 QSS 的 `::down-arrow` 只认 `image:`（CSS 三角色块
       写法在子控件上不生效），故用 QPainter 画一个 V 形存成 PNG 再 `url()` 引回。

    2. 「已选中」的勾选框/单选圈 —— 用 QSS 补成**主题主色填充 + 白色对勾/圆点**，
       避免原生渲染在深色下调色板取色偏暗、选中态反而不如未选中醒目（未选中的
       描边由样式表补，选中态见下）。对勾/圆点同样运行时画成 PNG。
    """
    if theme in _EXTRA_QSS:
        return _EXTRA_QSS[theme]
    rule = ""
    try:
        from PyQt5.QtCore import QPoint, QPointF, Qt
        from PyQt5.QtGui import (QColor, QPainter, QPen, QPixmap,
                                 QPolygon, QPolygonF)
        from .. import paths
        tk = _TOKENS.get(theme, _FLUENT)
        base = paths.DATA_DIR

        def _url(pm, name):
            path = base / f".{name}_{theme}.png"
            if pm.save(str(path), "PNG"):
                return str(path).replace("\\", "/")
            return ""

        # --- 下拉箭头（V 形，取控件前景色）---
        arrow = QPixmap(12, 7)
        arrow.fill(Qt.transparent)
        p = QPainter(arrow)
        p.setRenderHint(QPainter.Antialiasing, True)
        pen = QPen(QColor(tk["ctl_fg"]))
        pen.setWidthF(1.7)
        pen.setCapStyle(Qt.RoundCap)
        pen.setJoinStyle(Qt.RoundJoin)
        p.setPen(pen)
        p.drawPolyline(QPolygon([QPoint(2, 2), QPoint(6, 6), QPoint(10, 2)]))
        p.end()
        arrow_url = _url(arrow, "combo_arrow")

        # --- 对勾（白色，压在主题主色底上）---
        check = QPixmap(11, 8)
        check.fill(Qt.transparent)
        p = QPainter(check)
        p.setRenderHint(QPainter.Antialiasing, True)
        pen = QPen(QColor("#ffffff"))
        pen.setWidthF(1.9)
        pen.setCapStyle(Qt.RoundCap)
        pen.setJoinStyle(Qt.RoundJoin)
        p.setPen(pen)
        p.drawPolyline(QPolygon([QPoint(1, 4), QPoint(4, 6), QPoint(10, 1)]))
        p.end()
        check_url = _url(check, "check")

        # --- 圆点（白色，单选选中）---
        dot = QPixmap(8, 8)
        dot.fill(Qt.transparent)
        p = QPainter(dot)
        p.setRenderHint(QPainter.Antialiasing, True)
        p.setPen(Qt.NoPen)
        p.setBrush(QColor("#ffffff"))
        p.drawEllipse(QPointF(4, 4), 3.0, 3.0)
        p.end()
        dot_url = _url(dot, "dot")

        sel = tk["sel_bg"]
        if arrow_url:
            rule += ("QComboBox::down-arrow { image: url(\"%s\");"
                     " width: 12px; height: 7px; }\n" % arrow_url)
        if check_url:
            rule += ("QCheckBox::indicator:checked { image: url(\"%s\");"
                     " width: 15px; height: 15px; border: 1px solid %s;"
                     " background: %s; border-radius: 3px; }\n"
                     % (check_url, sel, sel))
        if dot_url:
            rule += ("QRadioButton::indicator:checked { image: url(\"%s\");"
                     " width: 15px; height: 15px; border: 1px solid %s;"
                     " background: %s; border-radius: 8px; }\n"
                     % (dot_url, sel, sel))
    except Exception:
        rule = ""
    _EXTRA_QSS[theme] = rule
    return rule


def _combo_dropdown_style(base_style):
    """把基础样式包一层，强制 `SH_ComboBox_Popup = 0`。

    QComboBox 弹出的「方向 / 动画 / 默认委托」都由这一个 style hint 决定
    （Qt qcombobox.cpp 对 SH_ComboBox_Popup 有三处判断）：
    - 为 0：下拉式——在组合框**下方**展开，且触发 150ms 滚动动画
      （浅色 windowsvista 就是这样，「向下有动画地展开」）；
    - 为 1：菜单式——相对**当前项**定位（可能向上弹），**无**动画
      （Fusion 默认如此，「向上瞬间出现」）。
    因此浅色/深色观感不一致。统一返回 0：让 Fusion 也走下拉式，方向与
    动画都对齐到浅色那套。（windowsvista 本就把该 hint 返回 0，无需包装。）
    """
    from PyQt5.QtWidgets import QProxyStyle, QStyle

    class _ComboDropdownStyle(QProxyStyle):
        def styleHint(self, hint, option=None, widget=None, returnData=None):
            if hint == QStyle.SH_ComboBox_Popup:
                return 0
            return super().styleHint(hint, option, widget, returnData)

    return _ComboDropdownStyle(base_style)


def _apply_dark_palette(app):
    """深色必备：Fusion + QPalette，否则原生绘制件（箭头/勾选/微调）残留浅色。"""
    from PyQt5.QtGui import QColor, QPalette
    from PyQt5.QtWidgets import QStyleFactory
    app.setStyle(_combo_dropdown_style(QStyleFactory.create("Fusion")))
    p = QPalette()
    p.setColor(QPalette.Window, QColor("#1e1e1e"))
    p.setColor(QPalette.WindowText, QColor("#cccccc"))
    p.setColor(QPalette.Base, QColor("#252526"))
    p.setColor(QPalette.AlternateBase, QColor("#2d2d30"))
    p.setColor(QPalette.Text, QColor("#cccccc"))
    p.setColor(QPalette.Button, QColor("#3a3d41"))
    p.setColor(QPalette.ButtonText, QColor("#cccccc"))
    p.setColor(QPalette.Highlight, QColor("#094771"))
    p.setColor(QPalette.HighlightedText, QColor("#ffffff"))
    p.setColor(QPalette.ToolTipBase, QColor("#252526"))
    p.setColor(QPalette.ToolTipText, QColor("#cccccc"))
    p.setColor(QPalette.Disabled, QPalette.Text, QColor("#6b6b6b"))
    p.setColor(QPalette.Disabled, QPalette.ButtonText, QColor("#6b6b6b"))
    # 边框/阴影色：Fusion 用 Mid/Dark/Light 画勾选框、单选圈、微调按钮的边框；
    # 不给亮色的话深色下这些「框」的边框≈底色，会看不见。
    p.setColor(QPalette.Light, QColor("#5a5a5a"))
    p.setColor(QPalette.Midlight, QColor("#4a4a4a"))
    p.setColor(QPalette.Mid, QColor("#6b6b6b"))
    p.setColor(QPalette.Dark, QColor("#8a8a8a"))
    p.setColor(QPalette.Shadow, QColor("#111111"))
    app.setPalette(p)


# 组合框弹出列表修复器的安装状态（保持引用，避免被 GC；只装一次）
_COMBO_FIX = {"installed": False, "filter": None}


def _ensure_combo_popup_fix(app):
    """修复：QComboBox 弹出列表行高塌成「一行字高」（三项挤不下、文字被裁）。

    根因——两套主题都把 `SH_ComboBox_Popup` 归一到 0（见
    `_combo_dropdown_style`），QComboBox 便使用默认委托 `QComboBoxDelegate`
    （继承自 `QItemDelegate`），而它**不认 QSS 的
    `QComboBox QAbstractItemView::item` 行高规则**，于是行高退回字高
    （约 13px）。浅色（windowsvista）一直如此；深色改用下拉式后亦然。

    做法——把弹出视图的委托统一换成 QStyledItemDelegate，让 QSS 行高
    规则生效。用应用级事件过滤器在控件 polish / show 时惰性处理，可覆盖
    运行期才创建（如设置对话框）的组合框；重复安装自动跳过。
    """
    if _COMBO_FIX["installed"]:
        return
    try:
        from PyQt5.QtCore import QObject, QEvent
        from PyQt5.QtWidgets import QComboBox, QStyledItemDelegate
    except Exception:
        return

    class _ComboPopupDelegateFixer(QObject):
        def eventFilter(self, obj, ev):
            if ev.type() in (QEvent.Polish, QEvent.Show) and isinstance(obj, QComboBox):
                try:
                    view = obj.view()
                    if view is not None and not isinstance(
                            view.itemDelegate(), QStyledItemDelegate):
                        view.setItemDelegate(QStyledItemDelegate(view))
                except Exception:
                    pass
            return False

    fixer = _ComboPopupDelegateFixer(app)
    app.installEventFilter(fixer)
    _COMBO_FIX["installed"] = True
    _COMBO_FIX["filter"] = fixer


def apply_theme(app, theme):
    """套用主题：更新 PALETTE + 样式表；（深色额外）Fusion + QPalette。返回实际主题。"""
    theme = str(theme or "").lower()
    if theme not in THEMES:
        theme = DEFAULT_THEME
    _capture_base(app)
    _ensure_combo_popup_fix(app)
    try:
        if theme == "devtool":
            _apply_dark_palette(app)
        else:
            from PyQt5.QtWidgets import QStyleFactory
            st = QStyleFactory.create(_BASE["style"] or "")
            if st is not None:
                app.setStyle(st)
            if _BASE["palette"] is not None:
                app.setPalette(_BASE["palette"])
    except Exception:
        pass
    refresh_palette(theme)
    try:
        qss = build_style(theme)
        extra = _theme_extra_qss(theme)   # 运行时图标补丁（箭头 / 选中勾点）
        if extra:
            qss = qss + "\n" + extra
        app.setStyleSheet(qss)
    except Exception:
        pass
    _CURRENT["theme"] = theme
    return theme
