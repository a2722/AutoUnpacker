# -*- coding: utf-8 -*-
"""设置页（正式页面）：A 方案「左栏领域」——9 个领域 + 顶部常驻搜索。

职责：
- SettingsPage：9 个领域（解压与整理 / 删除与安全 / 通知与提醒 / 剪贴板与二维码 /
  链接与网盘 / 外观与快捷键 / 系统与维护 ／ 实验性、监听目录）覆盖
  config.DEFAULT_CONFIG 的全部键；
- 列表默认极简：每行只有「名称 + 控件」；描述 / 默认值 / 配置键 / 风险说明
  一律**悬停满 700ms 或点击名称**后由浮层气泡给出（点击可固定，点空白/Esc 收起）；
- 「改即存」：每个控件变更即走 AppState.set()（config.save_config 原子写），
  文本 / 多行编辑 400ms 防抖、失焦立即落盘；写后回读磁盘校验，失败如实回报；
- 「恢复默认」：**底部唯一入口**，先选范围（整个程序 / 本页）→ 警告 → 再次确认；
  正在搜索时该按钮不出现；
- 风险项（4 项）常驻红色「风险」徽章；往更高风险方向改动时徽章呼吸 10s（不弹窗）；
- 搜索：跨全部项按 名称 / 描述 / 同义词 / 配置键 过滤，按领域分组并标命中数；
- 目录（领域导航）：左侧 QListWidget#settingsCat，宽 236px；点选即整页换成该领域
  （不是长滚动 + 定位）；切领域视口回到顶部；
- 监听目录不再在本页增删改（页头胶囊条 → WatchDirDialog 已覆盖），本页把
  watch_paths 逐条原样写回，绝不丢字段；每张目录卡固定 6 个字段；
- #stripHint 描述行：11px CJK 墨迹几乎顶满 em 框，QLabel 折行高度按
  fontMetrics().height() 算、绘制按 lineSpacing() 排，默认上下各裁 1px；
  由 QSS padding + polish 后的 _fit_hint() 兜底（见 style.py 注释）；
- 主题：走既有 ui_style.resolve_theme + apply_theme + ui_theme_cached + 回调
  路径，绝不手写 QSS、绝不新增主题 token（风险徽章 / 呼吸灯 / 偏离小圆点 /
  气泡都在 refresh_theme() 里重贴）。

关键入口：SettingsPage
依赖：PyQt5、config（DEFAULT_CONFIG/_sanitize_cfg/load_config）、style（PALETTE）、
      widgets（Glyph/HotkeyEdit）
注意：本页是「改即存」模型：没有底部「保存」按钮，任何控件变更都立即写入配置；
      文本类编辑 400ms 防抖（失焦立即落盘），写后回读磁盘校验、失败如实提示。
注意：集成（把本页装进主窗口标签壳）由集成步骤完成；本模块只提供页面与信号：
      settingsSaved / settingsReset / watchPathsChanged / hotkeyChanged(str) /
      themeChanged(str) / notice(str)。
注意：本模块不联网、不起线程、不重启应用；所有异常都转成页内 notice 提示。
注意（allow: SIZE_OK）：按任务要求「单文件承载全部设置表单、不得新建兄弟模块」，
      9 个领域 + 全部顶层键覆盖 + 全部私有助手必然内聚于此；不拆分是为了让
      「键 -> 控件 -> 即时保存」的覆盖契约在一个文件里可直接审计。
"""
import json

from PyQt5.QtCore import (QEvent, QPoint, QPropertyAnimation, QRectF, QSize,
                          QTimer, Qt, pyqtSignal)
from PyQt5.QtGui import QBrush, QColor, QPainter, QPen
from PyQt5.QtWidgets import (QAbstractSpinBox, QApplication, QButtonGroup,
                             QCheckBox, QComboBox, QDoubleSpinBox, QFileDialog,
                             QFrame, QGraphicsOpacityEffect, QHBoxLayout, QLabel,
                             QLineEdit, QListWidget, QMessageBox,
                             QPlainTextEdit, QPushButton, QRadioButton,
                             QScrollArea, QSizePolicy, QSpinBox, QStyle,
                             QVBoxLayout, QWidget)

from ..config import DEFAULT_CONFIG, _sanitize_cfg
from ..config import load_config
from . import style as ui_style
from .style import PALETTE
from .widgets import Glyph, HotkeyEdit

# 主题偏好与显示名（设置页主题下拉的唯一真源）
_THEME_ITEMS = (("跟随系统", "auto"), ("浅色", "fluent"), ("深色", "devtool"))
_THEME_NAMES = {"auto": "跟随系统", "fluent": "浅色（Fluent）",
                "devtool": "深色（DevTool）"}

# 二维码打开网页后的剪贴板联动（group id 必须与 state 的取值顺序一致）
_CLIP_ACTIONS = ((0, "none"), (1, "code"), (2, "url"))

# 落盘回读校验时跳过的复合键（列表/字典无法逐值比对，另有专门断言）
_VERIFY_SKIP = ("watch_paths", "url_trust", "url_redirect_rules")

# 文本 / 多行编辑的防抖间隔（停止输入多久后落盘；失焦立即落盘）
_TEXT_DEBOUNCE_MS = 400

# 补充信息气泡：悬停延迟（对齐现有设置页的 Qt 原生 tooltip 唤醒延迟 = 700ms）
_BUBBLE_DELAY_MS = 700
# 风险徽章呼吸时长（往更高风险方向改动时；10 秒后停回常态色）
_BREATH_TOTAL_MS = 10000
_BREATH_STEP_MS = 500

# 领域顺序（7 个领域 → 分隔 → 实验性、监听目录）。分隔线用 None 表示。
_DOMAIN_ORDER = ("unzip", "safety", "notify", "clipboard", "links", "ui",
                 "system", None, "lab", "dirs")

# 领域元信息：id -> (显示名, 图标, 一句话说明)
_DOMAIN_META = {
    "unzip": ("解压与整理", "archive", "压缩包怎么解、产物怎么摆"),
    "safety": ("删除与安全", "shield", "会不会丢文件、会不会被压缩包撑爆"),
    "notify": ("通知与提醒", "alert", "什么时候弹提示"),
    "clipboard": ("剪贴板与二维码", "search", "复制粘贴与扫码识别"),
    "links": ("链接与网盘", "bolt", "打开 / 下载外部链接、网址信任、网盘"),
    "ui": ("外观与快捷键", "dashboard", "主题、日志着色、全局快捷键、关闭行为"),
    "system": ("系统与维护", "gear", "扫描频率、7-Zip 检测、历史保留"),
    "lab": ("实验性", "queue", "默认关闭、可能不稳定"),
    "dirs": ("监听目录", "folder", "每个目录自己的解压位置与删除策略"),
}


def _deepcopy(value):
    """深拷贝配置值（配置里只有 JSON 可序列化类型，json 往返最省事）。"""
    return json.loads(json.dumps(value))


_DEFAULTS_CACHE = None


def _defaults():
    """默认值真源：净化后的 DEFAULT_CONFIG（含净化期补齐的 drop_* 等键）。惰性缓存。"""
    global _DEFAULTS_CACHE
    if _DEFAULTS_CACHE is None:
        try:
            _DEFAULTS_CACHE = _sanitize_cfg(_deepcopy(DEFAULT_CONFIG))
        except Exception:
            _DEFAULTS_CACHE = dict(DEFAULT_CONFIG)
    return _DEFAULTS_CACHE


def _glyph_icon(name, size=16):
    """把内置线性图标渲染成 QIcon（左栏领域导航用；随主题取 muted 色）。

    复用 widgets.common._draw_glyph，绝不自己画路径、也不硬编码颜色。"""
    try:
        from PyQt5.QtGui import QIcon, QPainter, QPixmap
        from .widgets.common import _draw_glyph
        pm = QPixmap(size, size)
        pm.fill(Qt.transparent)
        p = QPainter(pm)
        _draw_glyph(p, name, pm.rect(), PALETTE.get("muted", "#666666"), 1.6)
        p.end()
        return QIcon(pm)
    except Exception:
        return QIcon()


def _common(entries, key):
    """取所有监听目录条目某字段的共同值，返回 (value, all_same)。"""
    values = [e.get(key) for e in entries]
    first = values[0] if values else None
    return first, all(v == first for v in values)


def _parse_domain_lines(text):
    """域名编辑框文本 -> 去重小写列表（与 config._sanitize_cfg 同口径）。"""
    out = []
    for line in str(text or "").splitlines():
        line = line.strip().lower()
        if line and line not in out:
            out.append(line)
    return out


def _parse_suffix_lines(text):
    """未完成下载后缀编辑框文本 -> 去重小写列表（自动补前导点；与 config 净化同口径）。"""
    out = []
    for line in str(text or "").splitlines():
        line = line.strip().lower()
        if not line:
            continue
        if not line.startswith("."):
            line = "." + line
        if line not in out:
            out.append(line)
    return out


def _format_suffix_lines(suffixes):
    """未完成后缀列表 -> 编辑框文本（每行一个，落盘与回填的互逆表示）。"""
    return "\n".join(str(s) for s in (suffixes or []))


def _parse_redirect_rules(text):
    """重定向编辑框文本 -> [{'from','to'}]：每行一条「源域名 -> 目标域名」。"""
    rules = []
    for line in str(text or "").splitlines():
        line = line.strip()
        if not line:
            continue
        a = b = ""
        for sep in ("->", "=>", "="):
            if sep in line:
                a, b = line.split(sep, 1)
                break
        else:
            parts = line.split()
            if len(parts) == 2:
                a, b = parts
        a, b = a.strip().lower(), b.strip()
        if a and b:
            rules.append({"from": a, "to": b})
    return rules


def _format_redirect_rules(rules):
    """[{'from','to'}] -> 编辑框文本（落盘与回填的互逆表示）。"""
    lines = []
    for r in rules or []:
        if isinstance(r, dict) and r.get("from") and r.get("to"):
            lines.append("%s -> %s" % (r["from"], r["to"]))
    return "\n".join(lines)


def _default_display(value):
    """把 DEFAULT_CONFIG 的真值渲染成气泡里「默认：<值>」的短字符串。

    刻意保持简短与类型安全：列表 / 字典给结构化摘要，布尔给「开 / 关」，
    其余直接 str()。这里**只读运行时真值**，绝不抄设计稿的显示串。"""
    if isinstance(value, bool):
        return "开" if value else "关"
    if value is None:
        return "—"
    if isinstance(value, (list, tuple)):
        return "（空）" if not value else "%d 项" % len(value)
    if isinstance(value, dict):
        return "（空）" if not value else "%d 项" % len(value)
    if value == "":
        return "（空）"
    return str(value)


class _SettingRow:
    """一行设置的元数据（名称 / 描述 / 同义词 / 配置键 / 控件 / 风险）。

    仅作为轻量容器：真正的控件（QCheckBox 等）由 _make_row 系列构造并登记到
    SettingsPage._controls；本对象负责搜索索引与气泡内容。"""

    __slots__ = ("label", "desc", "syn", "key", "widget", "domain", "group",
                 "risk", "default_key", "default_value", "unit")

    def __init__(self, label, desc, key, widget, domain, group,
                 syn=(), risk=False, default_key=None, default_value=None,
                 unit=None):
        self.label = label
        self.desc = desc
        self.key = key
        self.widget = widget
        self.domain = domain
        self.group = group
        self.syn = tuple(syn or ())
        self.risk = bool(risk)
        self.default_key = default_key
        self.default_value = default_value
        self.unit = unit

    def search_blob(self):
        """搜索用的小写文本（名称 + 描述 + 同义词 + 键名）。"""
        parts = [self.label, self.desc, self.key]
        parts.extend(self.syn)
        return " ".join(str(p).lower() for p in parts if p)


class _Switch(QCheckBox):
    """胶囊开关（pill switch）：自绘「药丸轨道 + 圆形滑块」的布尔控件。

    为什么继承 QCheckBox：全页布尔项既有 `.setChecked / .toggled / .isChecked`
    语义与键盘 Space 切换必须逐字保留（离线验收也断言 `isinstance(_, QCheckBox)`），
    本类只接管**绘制**，不改任何状态逻辑。

    - `paintEvent` 完全自绘、不调 `super().paintEvent`；颜色每次绘制都从
      `ui_style.tokens()` 现取（QSS token 的真源——`prog_bg` / `primary_bg` 等
      只存在于 tokens，不在内联 PALETTE 里，见 `style.tokens()` 文档：自绘控件
      走只读 token），所以主题切换只需 `update()` 就会自动换色——不新增 color
      token、不硬编码颜色 / 圆角、不加 QSS 规则。
    - 尺寸固定 38x20；指针为手型；OFF / ON / ON+hover / disabled 四态见 paint。"""

    _WIDTH = 38
    _HEIGHT = 20

    def __init__(self, parent=None):
        super().__init__(parent)
        self.setCursor(Qt.PointingHandCursor)
        self.setSizePolicy(QSizePolicy.Fixed, QSizePolicy.Fixed)

    def sizeHint(self):
        return QSize(self._WIDTH, self._HEIGHT)

    def minimumSizeHint(self):
        return QSize(self._WIDTH, self._HEIGHT)

    def enterEvent(self, event):
        super().enterEvent(event)
        self.update()      # 悬停态需要重绘（ON+hover 用 primary_hover）

    def leaveEvent(self, event):
        super().leaveEvent(event)
        self.update()

    def paintEvent(self, event):   # noqa: N802 (Qt 命名)
        painter = QPainter(self)
        try:
            tk = ui_style.tokens()
            painter.setRenderHint(QPainter.Antialiasing, True)
            enabled = self.isEnabled()
            checked = self.isChecked()
            hovered = self.underMouse()
            w = float(self.width())
            h = float(self.height())
            if w <= 0 or h <= 0:
                return
            radius = h / 2.0
            if not enabled:
                track = QColor(tk["btn_dis_bg"])
                edge = QColor(tk["btn_dis_border"])
                knob = QColor(tk["btn_dis_border"])
            elif checked:
                track = QColor(tk["primary_hover"] if hovered
                               else tk["primary_bg"])
                edge = track
                knob = QColor(tk["primary_fg"])
            else:
                track = QColor(tk["prog_bg"])
                edge = QColor(tk["ctl_border"])
                knob = QColor(tk["card_bg"])
            painter.setPen(QPen(edge, 1))
            painter.setBrush(QBrush(track))
            painter.drawRoundedRect(
                QRectF(0.5, 0.5, w - 1.0, h - 1.0), radius, radius)
            inset = 2.0
            d = h - inset * 2.0
            left = (w - inset - d) if checked else inset
            painter.setPen(Qt.NoPen)
            painter.setBrush(QBrush(knob))
            painter.drawEllipse(QRectF(left, inset, d, d))
        except Exception:
            pass
        finally:
            painter.end()


class _InfoBubble(QFrame):
    """补充信息气泡：无边框浮层，定位到名称下方（不就地展开、不阻塞）。

    内容：名称（+ 风险徽章）/ 描述 / 默认：<值> / 配置键：<key> /
    风险说明（仅 risk=high）/ 底部操作提示。同一时刻只允许一个（由 SettingsPage
    统一持有并复用）。"""

    def __init__(self, parent=None):
        # 非 Qt.Popup：Popup 会抓鼠标，第二次点击（取消固定）会被弹窗吞掉，
        # 导致点击固定只能开不能关。改用 Qt.Tool + 不激活显示：
        # 不抢键盘焦点（WA_ShowWithoutActivating + NoFocus），点击照常落到名称标签，
        # 页面空白处点击（SettingsPage.mousePressEvent）与 Esc 仍可收起。
        super().__init__(parent, Qt.Tool | Qt.FramelessWindowHint)
        self.setAttribute(Qt.WA_ShowWithoutActivating, True)
        self.setFocusPolicy(Qt.NoFocus)
        self.setObjectName("settingsBubble")
        self.setAttribute(Qt.WA_StyledBackground, True)
        self._lay = QVBoxLayout(self)
        self._lay.setContentsMargins(12, 10, 12, 10)
        self._lay.setSpacing(6)
        self._title = QLabel(self)
        self._title.setObjectName("sectionTitle")
        self._title.setWordWrap(True)
        self._lay.addWidget(self._title)
        self._desc = QLabel(self)
        self._desc.setObjectName("stripHint")
        self._desc.setWordWrap(True)
        self._lay.addWidget(self._desc)
        self._meta = QLabel(self)
        self._meta.setObjectName("stripHint")
        self._meta.setWordWrap(True)
        self._lay.addWidget(self._meta)
        self._risk_box = QFrame(self)
        self._risk_box.setObjectName("bubbleRisk")
        rb = QVBoxLayout(self._risk_box)
        rb.setContentsMargins(10, 8, 10, 8)
        rb.setSpacing(3)
        self._risk_lbl = QLabel(self._risk_box)
        self._risk_lbl.setWordWrap(True)
        rb.addWidget(self._risk_lbl)
        self._lay.addWidget(self._risk_box)
        self._foot = QLabel("悬停片刻或点击名称可见 · 点击可固定 · 点空白处收起", self)
        self._foot.setObjectName("stripHint")
        self._foot.setWordWrap(True)
        self._lay.addWidget(self._foot)

    def show_for(self, row, anchor_global_pos):
        """按行元数据填充内容并显示到 anchor 下方。"""
        self._title.setText(row.label)
        self._desc.setText(row.desc or "")
        # 默认值：优先用登记时捕获的默认（延迟取 DEFAULT_CONFIG），否则现读
        dv = row.default_value
        if row.default_key is not None and row.default_key in DEFAULT_CONFIG:
            dv = DEFAULT_CONFIG.get(row.default_key)
        meta = "默认：%s    ·    配置键：%s" % (
            _default_display(dv if dv is not None else "—"), row.key)
        self._meta.setText(meta)
        if row.risk:
            self._risk_box.setVisible(True)
            self._risk_lbl.setText(
                "影响范围：会改变磁盘上的文件或降低连接安全性\n"
                "后果：重则丢失源文件 / 被中间人攻击，且不可撤销\n"
                "如何改回：把本项改回「默认」即可（可在底部「恢复默认」一键还原本页）")
            self._risk_box.setStyleSheet(
                "QFrame#bubbleRisk { background: %s; border: 1px solid %s;"
                " border-radius: 4px; }" % (PALETTE["warn_bg"], PALETTE["warn_border"]))
            self._risk_lbl.setStyleSheet("color: %s;" % PALETTE["danger"])
        else:
            self._risk_box.setVisible(False)
        self.adjustSize()
        x = int(anchor_global_pos.x())
        y = int(anchor_global_pos.y()) + 22
        screen = QApplication.desktop().availableGeometry(self)
        if x + self.width() > screen.right():
            x = max(screen.left(), screen.right() - self.width())
        if y + self.height() > screen.bottom():
            y = max(screen.top(), int(anchor_global_pos.y()) - self.height() - 6)
        self.move(QPoint(int(x), int(y)))
        self.show()
        self.raise_()


class SettingsPage(QWidget):
    """设置页：9 领域左栏导航 + 改即存 / 恢复默认；覆盖 DEFAULT_CONFIG 全部键。

    宿主接入（集成步骤）：`SettingsPage(state, hub, parent,
    on_hotkey_change=..., on_theme_change=...)`——两个回调分别是「重新注册全局
    快捷键」「主题已切换」，可原样传入
    MainWindow._register_hotkey / MainWindow.on_theme_changed。

    信号：settingsSaved() / settingsReset() / watchPathsChanged() /
          hotkeyChanged(str) / themeChanged(str) / notice(str)。
    """

    settingsSaved = pyqtSignal()
    settingsReset = pyqtSignal()
    watchPathsChanged = pyqtSignal()
    hotkeyChanged = pyqtSignal(str)
    themeChanged = pyqtSignal(str)
    notice = pyqtSignal(str)

    def __init__(self, state, hub=None, parent=None, on_hotkey_change=None,
                 on_theme_change=None):
        super().__init__(parent)
        self.state = state
        self.hub = hub
        self._hotkey_cb = on_hotkey_change
        self._theme_cb = on_theme_change
        self._controls = {}        # 配置键 -> [控件]（含 url_trust.* / watch_paths.* 点号路径）
        self._sections = []        # 领域标题（保持插入顺序，与左栏一一对应）
        self._section_cards = {}   # 领域 id -> 承载该领域行的 QWidget
        self._rows = []            # _SettingRow 列表（搜索索引 + 气泡内容）
        self._warn_labels = []     # 需要随主题重贴 warn 色的说明文字
        self._theme_pref = "auto"
        self._notice_failed = False
        self._loading = True       # 回填 / 构造期间抑制「改即存」监听
        self._text_timers = {}     # 文本控件 -> (单发 QTimer, flush 可调用)
        self._hotkeys_at_load = (None, None, None, None)  # 热键变更检测基线
        self._current_domain = "unzip"
        self._query = ""           # 当前搜索词（非空则显示结果列表、隐藏恢复默认）
        self._bubble = None        # 当前唯一气泡（惰性创建、复用）
        self._bubble_pinned = False
        self._bubble_row = None
        self._bubble_timer = None  # 悬停 700ms 单发计时器
        self._breath_timers = {}   # 徽章 -> (QTimer, 剩余步数)
        self._risk_badges = {}     # 配置键 -> 徽章 QLabel（呼吸 / 重贴用）
        self._dev_dots = {}        # 配置键 -> 偏离默认小圆点 QLabel
        self._name_labels = {}     # id(名称标签) -> (标签, _SettingRow)
        self._pending_name = None  # 悬停中的名称标签（700ms 计时器用）
        self._result_card = None   # 搜索结果容器（搜索态用）
        self._domain_box = None
        self._domain_id = "unzip"

        self._build_ui()
        self._load_from_cfg()

    # ------------------------------------------------------------------
    # 构建
    # ------------------------------------------------------------------
    def _build_ui(self):
        self._sections = []
        self._section_cards = {}
        root = QVBoxLayout(self)
        root.setContentsMargins(0, 0, 0, 0)
        root.setSpacing(0)

        # 左右两栏：[领域导航 | 右侧内容]；导航不随内容滚动。
        body = QHBoxLayout()
        body.setContentsMargins(12, 0, 0, 0)
        body.setSpacing(8)

        self.cat_list = QListWidget(self)
        self.cat_list.setObjectName("settingsCat")
        self.cat_list.setFixedWidth(236)
        self.cat_list.setHorizontalScrollBarPolicy(Qt.ScrollBarAlwaysOff)
        self.cat_list.setToolTip("选择一个领域查看该领域的设置。")
        self.cat_list.currentRowChanged.connect(self._on_cat_row_changed)
        body.addWidget(self.cat_list)

        # 右侧：顶部工具条（搜索 + 向导 + 导入/导出）+ 领域内容滚动区。
        right = QWidget(self)
        rlay = QVBoxLayout(right)
        rlay.setContentsMargins(0, 0, 0, 0)
        rlay.setSpacing(0)

        top = QFrame(right)
        top.setObjectName("settingsTop")
        t = QHBoxLayout(top)
        t.setContentsMargins(0, 10, 12, 8)
        t.setSpacing(8)
        self.search_edit = QLineEdit(top)
        self.search_edit.setPlaceholderText("搜索设置项")
        self.search_edit.setClearButtonEnabled(True)
        self.search_edit.setMinimumWidth(220)
        self.search_edit.setMaximumWidth(360)
        self.search_edit.textChanged.connect(self._on_search_changed)
        t.addWidget(self.search_edit)
        t.addStretch(1)
        self.wizard_btn = QPushButton("设置向导", top)
        self.wizard_btn.setObjectName("primary")
        self.wizard_btn.setCursor(Qt.PointingHandCursor)
        self.wizard_btn.setToolTip("可跳过，跳过后不再显示")
        self.wizard_btn.clicked.connect(self._on_wizard_clicked)
        t.addWidget(self.wizard_btn)
        # 登记向导键（覆盖契约：settings_wizard_done 必须有归属控件）
        self._reg("settings_wizard_done", self.wizard_btn)
        self.import_btn = QPushButton("导入 / 导出（规划中）", top)
        self.import_btn.setEnabled(False)
        self.import_btn.setToolTip("规划中")
        t.addWidget(self.import_btn)
        rlay.addWidget(top)

        scroll = QScrollArea(right)
        scroll.setObjectName("settingsScroll")
        scroll.setWidgetResizable(True)
        scroll.setFrameShape(QFrame.NoFrame)
        scroll.setHorizontalScrollBarPolicy(Qt.ScrollBarAsNeeded)
        self.scroll = scroll
        inner = QWidget(scroll)
        self._lay = QVBoxLayout(inner)
        self._lay.setContentsMargins(0, 4, 12, 10)
        self._lay.setSpacing(10)
        scroll.setWidget(inner)
        rlay.addWidget(scroll, 1)
        body.addWidget(right, 1)
        root.addLayout(body, 1)

        # 逐领域构建（每个领域 = 一个容器 QWidget，装标题 + 说明 + 分组 + 行）
        for did in _DOMAIN_ORDER:
            if did is None:
                self._sections.append(None)   # 左栏分隔线占位（无对应领域容器）
                continue
            self._build_domain(did)

        # 默认选中第一个领域
        self._refresh_catalog(select=0)
        self._lay.addStretch(1)   # 内容顶对齐（卡片不被拉伸填满视口）

        # 底部动作条（固定在滚动区之外）：唯一「恢复默认」+ 即时保存提示。
        foot = QFrame(self)
        foot.setObjectName("dlgFoot")
        f = QHBoxLayout(foot)
        f.setContentsMargins(12, 8, 12, 10)
        f.setSpacing(8)
        f.addStretch(1)
        self.notice_label = QLabel("", foot)
        self.notice_label.setObjectName("stripHint")
        self.notice_label.setSizePolicy(QSizePolicy.Ignored, QSizePolicy.Fixed)
        f.addWidget(self.notice_label)
        self.reset_btn = QPushButton("恢复默认", foot)
        self.reset_btn.setObjectName("danger")
        self.reset_btn.setCursor(Qt.PointingHandCursor)
        self.reset_btn.setToolTip("选择范围后把所有设置恢复为程序默认值（需再次确认）。")
        self.reset_btn.clicked.connect(self._on_reset)
        f.addWidget(self.reset_btn)
        root.addWidget(foot)

        # 惰性气泡 + 悬停计时器
        self._bubble_timer = QTimer(self)
        self._bubble_timer.setSingleShot(True)
        self._bubble_timer.setInterval(_BUBBLE_DELAY_MS)
        self._bubble_timer.timeout.connect(self._on_bubble_due)

    def _build_domain(self, did):
        """建一个领域容器：标题 + 一句话说明（同一行）+ 一张带边框的卡片装所有行。"""
        name, icon, desc = _DOMAIN_META[did]
        card = QWidget(self)
        card.setObjectName("settingsDomain")
        box = QVBoxLayout(card)
        box.setContentsMargins(0, 0, 0, 0)
        box.setSpacing(10)

        # 标题行：图标 + 领域名（粗）+ 一句话说明（灰，同行右侧）
        head = QHBoxLayout()
        head.setSpacing(8)
        head.addWidget(Glyph(icon, card, 15, role="muted"))
        title = QLabel(name, card)
        title.setObjectName("appTitle")
        head.addWidget(title)
        d = QLabel(desc, card)
        d.setObjectName("stripHint")
        head.addWidget(d)
        head.addStretch(1)
        box.addLayout(head)

        # 行容器：一张卡片，内部用细分隔线分 组
        rows_card = QFrame(card)
        rows_card.setObjectName("card")
        rbox = QVBoxLayout(rows_card)
        rbox.setContentsMargins(14, 4, 14, 4)
        rbox.setSpacing(0)

        self._lay.addWidget(card)
        self._section_cards[did] = card
        self._sections.append(did)
        self._domain_box = rbox            # 后续 _group/_row 追加到这里
        self._domain_id = did

        if did == "unzip":
            self._build_unzip(rbox)
        elif did == "safety":
            self._build_safety(rbox)
        elif did == "notify":
            self._build_notify(rbox)
        elif did == "clipboard":
            self._build_clipboard(rbox)
        elif did == "links":
            self._build_links(rbox)
        elif did == "ui":
            self._build_ui_domain(rbox)
        elif did == "system":
            self._build_system(rbox)
        elif did == "lab":
            self._build_lab(rbox)
        elif did == "dirs":
            self._build_dirs(rbox)
        box.addWidget(rows_card)

    # ---- 分组标题 / 行骨架 ----
    def _group(self, lay, title):
        """分组小标题（+ 上方细分隔线），返回同一布局（行直接追加进来）。

        参考稿：第一组的分隔线画在分组名上方即可；后续每组前也有一条细线。"""
        lbl = QLabel(str(title), lay.parentWidget())
        lbl.setObjectName("groupTitle")
        lay.addWidget(lbl)
        sep = QFrame(lay.parentWidget())
        sep.setObjectName("groupSep")
        sep.setFrameShape(QFrame.HLine)
        sep.setFixedHeight(1)
        lay.addWidget(sep)
        return lay

    def _reg(self, key, widget):
        """登记某配置键对应的控件（同一键可多个控件）。"""
        self._controls.setdefault(str(key), []).append(widget)
        return widget

    def _hint(self, text, parent=None, warn=False):
        lbl = QLabel(str(text), parent if parent is not None else self)
        lbl.setObjectName("stripHint")
        lbl.setWordWrap(True)
        if warn:
            lbl.setStyleSheet("color: %s;" % PALETTE["warn_text"])
            self._warn_labels.append(lbl)
        return self._watch_hint(lbl)

    # ---- #stripHint 折行高度兜底（修 CJK 描述行裁切） ----
    def _watch_hint(self, lbl):
        """让页面在标签 polish / 样式变化后重算其最小高度（返回同一标签）。"""
        try:
            lbl.installEventFilter(self)
        except Exception:
            pass
        return lbl

    def _fit_hint(self, lbl):
        """把说明标签的最小高度抬到 lineSpacing() + 2（CJK 兜底）。"""
        try:
            need = int(lbl.fontMetrics().lineSpacing()) + 2
            if lbl.minimumHeight() < need:
                lbl.setMinimumHeight(need)
        except Exception:
            pass

    def _fit_all_hints(self):
        for lbl in self.findChildren(QLabel):
            if lbl.objectName() in ("stripHint", "bubbleHint"):
                self._fit_hint(lbl)

    def eventFilter(self, obj, event):
        try:
            etype = event.type()
            if (obj.objectName() in ("stripHint", "bubbleHint")
                    and etype in (QEvent.Polish, QEvent.StyleChange)):
                self._fit_hint(obj)
            if etype == QEvent.FocusOut and obj in self._text_timers:
                self._flush_text(obj)   # 失焦立即落盘（不等防抖）
            # 设置名称：悬停 700ms 出气泡；左键点击名称立即固定 / 再点收起。
            # 注意：_name_labels 的键是 id(标签)（int），obj 是 QWidget；必须用
            # id(obj) 做成员判断（历史缺陷：直接 `obj in dict` 恒为 False → 悬停/点击全死）。
            if etype == QEvent.Enter and id(obj) in self._name_labels:
                self._arm_bubble(obj)
            elif etype == QEvent.Leave and id(obj) in self._name_labels:
                self._on_name_leave(obj)
            elif (etype == QEvent.MouseButtonRelease
                  and event.button() == Qt.LeftButton
                  and id(obj) in self._name_labels):
                self._toggle_pinned_bubble(obj)
        except Exception:
            pass
        return super().eventFilter(obj, event)

    def showEvent(self, event):
        super().showEvent(event)
        self._fit_all_hints()

    # ------------------------------------------------------------------
    # 设置行（默认极简：名称 + 控件；名称可悬停/点击出气泡）
    # ------------------------------------------------------------------
    def _make_name(self, parent, text, row_meta):
        """建「设置名称」标签：只改鼠标指针（PointingHandCursor），可悬停/点击。

        名称**不折行**（单行显示）：默认窗口宽度下横向空间充足，折行会让
        「产物时间戳改为当前时间」这类 10 字出头的名称被切到两行；布局按
        sizeHint 给足自然宽度即可（描述行 _hint 才是该折行的那种文本）。"""
        lbl = QLabel(str(text), parent)
        lbl.setObjectName("setName")
        lbl.setWordWrap(False)
        lbl.setCursor(Qt.PointingHandCursor)
        lbl.installEventFilter(self)
        self._name_labels[id(lbl)] = (lbl, row_meta)
        return lbl

    def _row(self, lay, row_meta, make_control, risk=False, key=None):
        """通用行：左侧只有名称（+风险徽章+偏离小圆点），右侧控件，整行较高。

        make_control(host) -> 控件；返回该控件。"""
        host = QWidget(lay.parentWidget())
        host.setObjectName("setRow")
        h = QHBoxLayout(host)
        h.setContentsMargins(0, 9, 0, 9)      # 参考稿：行内上下留白，行高约 46px
        h.setSpacing(8)
        left = QWidget(host)
        l = QHBoxLayout(left)
        l.setContentsMargins(0, 0, 0, 0)
        l.setSpacing(6)
        name = self._make_name(left, row_meta.label, row_meta)
        l.addWidget(name, 0, Qt.AlignVCenter)
        if risk:
            badge = QLabel("风险", left)
            badge.setObjectName("riskBadge")
            badge.setAlignment(Qt.AlignCenter)
            l.addWidget(badge)
            if key:
                self._risk_badges[key] = badge
        dot = QLabel("•", left)
        dot.setObjectName("devDot")
        dot.setVisible(False)
        l.addWidget(dot)
        if key:
            self._dev_dots[key] = dot
        l.addStretch(1)
        h.addWidget(left, 1)
        ctl = make_control(host)
        h.addWidget(ctl, 0, Qt.AlignVCenter)
        lay.addWidget(host)
        row_meta.widget = ctl
        self._rows.append(row_meta)
        return ctl

    def _mark_dirty(self, key):
        """按当前值 vs 默认值刷新「偏离默认」小圆点。

        默认值真源 = `_sanitize_cfg(DEFAULT_CONFIG)`（show_status_tips /
        settings_wizard_done 等在净化里补的键也算进来；否则这些键会被误判为「偏离」）。"""
        dot = self._dev_dots.get(key)
        if dot is None:
            return
        try:
            cfg = self._snapshot()
            cur = cfg.get(key, "<缺失>")
            dft = _defaults().get(key, "<缺失>")
            if isinstance(dft, bool):
                dirty = bool(cur) != bool(dft)
            elif isinstance(dft, (int, float)):
                try:
                    dirty = float(cur) != float(dft)
                except Exception:
                    dirty = str(cur) != str(dft)
            else:
                dirty = str(cur) != str(dft)
            dot.setVisible(dirty)
        except Exception:
            pass

    def _check(self, lay, label, key, desc, syn=(), risk=False,
               extra_keys=None, commit=None, default=True):
        """一行复选框（默认极简）。"""
        meta = _SettingRow(label, desc, key, None, self._domain_id,
                           "一般", syn=syn, risk=risk, default_key=key,
                           default_value=default)

        def mk(host):
            cb = _Switch(host)
            cb.toggled.connect(
                commit or (lambda checked, k=key: self._commit(k, bool(checked))))
            self._reg(key, cb)
            for k in (extra_keys or []):
                self._reg(k, cb)
            return cb

        cb = self._row(lay, meta, mk, risk=risk, key=key)
        meta.widget = cb
        return cb

    def _spin(self, minimum, maximum):
        spin = QSpinBox(self)
        spin.setRange(int(minimum), int(maximum))
        spin.setButtonSymbols(QAbstractSpinBox.NoButtons)
        return spin

    def _dspin(self, minimum, maximum):
        """浮点数值框（GB 类阈值需要小数，如 1.0 / 50.0）。"""
        spin = QDoubleSpinBox(self)
        spin.setRange(float(minimum), float(maximum))
        spin.setDecimals(1)
        spin.setButtonSymbols(QAbstractSpinBox.NoButtons)
        return spin

    def _spin_row(self, lay, label, key, desc, minimum, maximum, syn=(),
                  unit=None, risk=False, default=0, dbl=False):
        """一行「名称 + 数值框 [+ 单位]」。返回内部 spinbox（dbl=True 时是浮点框）。"""
        meta = _SettingRow(label, desc, key, None, self._domain_id, "一般",
                           syn=syn, risk=risk, default_key=key,
                           default_value=default, unit=unit)
        holder = {}

        def mk(host):
            box = QWidget(host)
            b = QHBoxLayout(box)
            b.setContentsMargins(0, 0, 0, 0)
            b.setSpacing(6)
            spin = self._dspin(minimum, maximum) if dbl else self._spin(minimum, maximum)
            if dbl:
                spin.valueChanged.connect(lambda v, k=key: self._commit(k, float(v)))
            else:
                spin.valueChanged.connect(lambda v, k=key: self._commit(k, int(v)))
            self._reg(key, spin)
            holder["spin"] = spin
            b.addWidget(spin)
            if unit:
                u = QLabel(unit, box)
                u.setObjectName("fLabel")
                b.addWidget(u)
            return box

        self._row(lay, meta, mk, risk=risk, key=key)
        spin = holder.get("spin")
        meta.widget = spin
        return spin

    def _text_row(self, lay, label, key, desc, parse, syn=(), rows=4,
                  placeholder="", risk=False, commit_key=None):
        """一行「名称 + 多行文本」；compose 由 parse 提供（400ms 防抖即存）。

        key 用于覆盖登记（可能是点号叶子键）；commit_key 指定真正写盘的键
        （信任名单等复合键：叶子只用于登记，写入走顶层 url_trust）。"""
        write_key = commit_key or key
        meta = _SettingRow(label, desc, key, None, self._domain_id, "一般",
                           syn=syn, risk=risk, default_key=key)

        def mk(host):
            edit = QPlainTextEdit(host)
            edit.setObjectName("settingsTextEdit")   # 走输入框样式（非日志控制台样式）
            edit.setFixedHeight(26 + 18 * max(2, rows))
            edit.setSizePolicy(QSizePolicy.Expanding, QSizePolicy.Fixed)
            if placeholder:
                edit.setPlaceholderText(placeholder)
            self._reg(key, edit)
            self._bind_text(edit, write_key, lambda: parse(edit.toPlainText()))
            return edit

        edit = self._row(lay, meta, mk, risk=risk, key=key)
        meta.widget = edit
        return edit

    def _radio_row(self, lay, label, key, desc, options, value_of, syn=(),
                   risk=False, default="", on_change=None):
        """一行「名称 + 竖排单选组」。value_of() 返回当前选中值。

        on_change 给定则任一单选被选中时触发（用于改即存）。"""
        meta = _SettingRow(label, desc, key, None, self._domain_id, "一般",
                           syn=syn, risk=risk, default_key=key,
                           default_value=default)

        def mk(host):
            box = QWidget(host)
            b = QVBoxLayout(box)
            b.setContentsMargins(0, 0, 0, 0)
            b.setSpacing(3)
            grp = QButtonGroup(box)
            for value, text in options:
                rb = QRadioButton(text, box)
                grp.addButton(rb)
                b.addWidget(rb)
                if on_change is not None:
                    rb.toggled.connect(
                        lambda checked, cb=on_change: cb() if checked else None)
            self._reg(key, box)
            return box

        self._row(lay, meta, mk, risk=risk, key=key)
        return meta.widget

    # ---- 复合键（信任 / 单选组）辅助：控件查找 ----
    def _find_check(self, key):
        for w in self._controls.get(key, []):
            if isinstance(w, QCheckBox):
                return w
        return None

    # ------------------------------------------------------------------
    # 各领域构建
    # ------------------------------------------------------------------
    def _build_unzip(self, box):
        g = self._group(box, "产物整理")
        self.output_time_cb = self._check(
            g, "产物时间戳改为当前时间", "output_time_now",
            "解压完成后，把产物最外层文件夹的时间改成现在，免得旧日期沉在目录列表底部。",
            syn=("时间戳", "日期", "排序", "沉底", "修改时间"))
        self.promote_merge_cb = self._check(
            g, "同名文件夹自动合并", "promote_merge",
            "解压提升时，同名文件夹里没有同名文件就合并成一个；真有冲突的仍会重命名成 (N)。",
            syn=("合并", "同名", "冲突", "重命名", "提升"))
        self.translate_cb = self._check(
            g, "翻译文件自动归位", "translation_move_enabled",
            "小于 10MB 的单个 json（多半是翻译文件）自动移进同名的那个大文件夹。",
            syn=("翻译", "json", "归位", "字幕", "小文件"))

        g = self._group(box, "分卷与完整性")
        self.pair_split_cb = self._check(
            g, "分卷压缩包自动配对", "pair_split_enabled",
            "把 xxx.7z.001 / 002… 这类分卷认出来并接上；改了名的尾卷在验证通过后也会配对。"
            "这是唯一总闸，关掉即完全停用。",
            syn=("分卷", "跨名", "尾卷", "配对", "7z.001", "001", "合并"))
        self.dl_suffix_edit = self._text_row(
            g, "没下完的文件后缀", "incomplete_download_suffixes",
            "带这些后缀的文件先不解压，等下载器改完名再处理。每行一个，不写前导点会自动补。",
            _parse_suffix_lines, syn=("未完成", "下载中", "后缀", "part",
                                      "crdownload", "aria2", "临时文件"),
            rows=3, placeholder="每行一个后缀，如 .part")

    def _build_safety(self, box):
        g = self._group(box, "删除源文件")
        self.delete_master_cb = _Switch(self)   # 名称由左侧标签承担，选框不带文字
        self.delete_master_cb.toggled.connect(self._on_delete_master)
        self._reg("watch_paths.delete_source", self.delete_master_cb)
        self._reg("watch_paths", self.delete_master_cb)
        host = QWidget(self)
        hl = QHBoxLayout(host)
        hl.setContentsMargins(0, 0, 0, 0)
        hl.setSpacing(6)
        nm = self._make_name(host, "解压成功后删除源文件",
                             _SettingRow("解压成功后删除源文件", "", "", None,
                                         "safety", "删除源文件", risk=True))
        hl.addWidget(nm)
        self.delete_risk_badge = QLabel("风险", host)
        self.delete_risk_badge.setObjectName("riskBadge")
        hl.addWidget(self.delete_risk_badge)
        self._risk_badges["watch_paths.delete_source"] = self.delete_risk_badge
        hl.addStretch(1)
        hl.addWidget(self.delete_master_cb)
        g.addWidget(host)
        self._rows.append(_SettingRow(
            "解压成功后删除源文件",
            "对所有监听目录统一生效；删除是移进回收站，可在「删除回溯」页还原。"
            "单个目录可在「监听目录」里单独覆盖。",
            "watch_paths.delete_source", self.delete_master_cb, "safety",
            "删除源文件", syn=("删除", "源文件", "删源", "回收站", "清理"),
            risk=True, default_key="watch_paths"))
        self.delete_state_label = self._hint("", self)
        g.addWidget(self.delete_state_label)

        g = self._group(box, "防炸弹与防爆盘")
        self.bomb_guard_cb = self._check(
            g, "解压前安全检查", "bomb_guard_enabled",
            "解压前先看一眼压缩包，疑似 zip 炸弹就拒绝解压并提示。",
            syn=("zip bomb", "炸弹", "安全检查", "预检", "膨胀"))
        self.bomb_entries_spin = self._spin_row(
            g, "文件数量预警", "bomb_soft_entries",
            "压缩包里文件数超过这个值只提醒、不拦截；0 = 关闭。",
            0, 1000000, syn=("条目数", "文件数", "数量", "告警", "预警"),
            default=50000)
        self.bomb_soft_ratio_spin = self._spin_row(
            g, "体积膨胀预警倍数", "bomb_soft_ratio",
            "解压后体积超过压缩包的这个倍数时，只提醒、不拦截。",
            1, 100000, syn=("软告警", "膨胀", "倍数", "比例", "预警"), default=100)
        self.bomb_hard_ratio_spin = self._spin_row(
            g, "体积膨胀拦截倍数", "bomb_hard_ratio",
            "解压后体积超过压缩包的这个倍数、且达到下面的最小体积时，直接拒绝解压。",
            1, 100000, syn=("硬拒绝", "膨胀", "倍数", "拦截", "拒绝"), default=200)
        self.bomb_min_gb_spin = self._spin_row(
            g, "膨胀拦截的最小体积", "bomb_hard_min_gb",
            "只有解压后体积达到这个大小，上面的倍数规则才生效——避免误伤小文件。",
            0, 100000, syn=("最小体积", "门槛", "比例规则", "误伤"),
            unit="GB", default=1.0, dbl=True)
        self.bomb_size_gb_spin = self._spin_row(
            g, "解压后体积上限", "bomb_hard_size_gb",
            "解压后声明的总体积超过这个值就直接拒绝。",
            0, 1000000, syn=("绝对上限", "总体积", "硬上限", "体积上限"),
            unit="GB", default=50.0, dbl=True)
        self.free_space_spin = self._spin_row(
            g, "磁盘剩余空间下限", "min_free_space_gb",
            "目标盘剩余空间低于这个值就暂停自动解压，空间够了自动继续；0 = 关闭。",
            0, 100000, syn=("磁盘", "空间", "剩余", "暂停", "爆盘", "守护"),
            unit="GB", default=5.0, dbl=True)

    def _build_notify(self, box):
        g = self._group(box, "总开关")
        self.notify_cb = self._check(
            g, "通知总开关", "notify_enabled",
            "关掉后不再弹任何提示（日志仍照记），下面各项一并变灰。",
            syn=("通知", "总开关", "静音", "关闭提示"))
        g = self._group(box, "解压事件")
        self.notify_archive_cb = self._check(
            g, "发现压缩包", "notify_archive", "扫描到新的压缩包时提示。",
            syn=("发现", "压缩包", "扫描到"))
        self.notify_success_cb = self._check(
            g, "解压完成", "notify_success", "解压成功时提示。",
            syn=("成功", "完成", "解压完"))
        self.notify_failure_cb = self._check(
            g, "解压失败", "notify_failure", "解压失败时提示。",
            syn=("失败", "错误", "没解出来"))
        self.notify_error_cb = self._check(
            g, "解压出错", "notify_error", "解压过程报错时提示。",
            syn=("出错", "异常", "报错"))
        g = self._group(box, "托盘与启动")
        self.notify_trayed_cb = self._check(
            g, "已最小化到托盘", "notify_trayed", "程序收进托盘时提示一次。",
            syn=("托盘", "最小化", "收起"))
        self.notify_running_cb = self._check(
            g, "程序已在运行时提示", "notify_already_running",
            "重复启动时提示，并打开已有窗口。", syn=("已运行", "重复启动", "单实例"))
        self.notify_trust_cb = self._check(
            g, "有新的网址等待确认", "notify_trust_pending",
            "遇到没见过的网站、需要你决定信任与否时提示。",
            syn=("网址", "信任", "待确认", "新域名"))
        g = self._group(box, "分享与网盘")
        self.notify_share_cb = self._check(
            g, "分享相关通知", "notify_share",
            "分享手势、链接解析、拉起客户端、下载结果的统一开关。",
            syn=("分享", "网盘分享", "解析", "手势"))
        self.notify_share_dead_cb = self._check(
            g, "分享链接已失效", "notify_share_dead",
            "链接被取消 / 过期 / 违规时当场提醒（需与上面开关同时打开）。",
            syn=("失效", "过期", "取消", "违规", "死链"))
        self.notify_baidu_done_cb = self._check(
            g, "网盘下载批次完成", "notify_baidu_done",
            "实验性功能开启时：一个下载批次全部完成时提示。",
            syn=("网盘", "批次", "下载完成", "百度"))
        self.notify_baidu_leftover_cb = self._check(
            g, "启动时有没下完的网盘任务", "notify_baidu_leftover",
            "实验性功能开启时：启动发现还有未完成任务时提示。",
            syn=("网盘", "未完成", "残留", "启动"))
        self.notify_baidu_dup_cb = self._check(
            g, "新任务与历史重复", "notify_baidu_dup",
            "实验性功能开启时：新任务和以前下载过的一样时提示（默认关，避免打扰）。",
            syn=("重复", "去重", "历史下载", "网盘"), default=False)

        self._notify_subs = (self.notify_archive_cb, self.notify_success_cb,
                             self.notify_failure_cb, self.notify_error_cb,
                             self.notify_trayed_cb, self.notify_running_cb,
                             self.notify_trust_cb, self.notify_share_cb,
                             self.notify_share_dead_cb, self.notify_baidu_done_cb,
                             self.notify_baidu_leftover_cb, self.notify_baidu_dup_cb)
        self._notify_labels = ()
        self.notify_cb.toggled.connect(lambda _s: self._sync_notify_enabled())

    def _build_clipboard(self, box):
        g = self._group(box, "二维码识别")
        self.qr_cb = self._check(
            g, "识别剪贴板图片里的二维码", "qr_enabled",
            "复制到剪贴板的截图里有二维码就自动识别；手动拖进来的图片不受这个开关限制。",
            syn=("二维码", "扫码", "剪贴板", "截图", "识别"))
        self.clip_host = self._radio_row(
            g, "打开二维码网页后，剪贴板怎么办", "qr_clipboard_action",
            "不处理 / 恢复刚复制的提取码 / 把二维码原文写回剪贴板。",
            (("none", "不处理"), ("code", "恢复刚复制的提取码"),
             ("url", "把二维码原文写回剪贴板")),
            self._clip_value, syn=("剪贴板联动", "提取码", "恢复", "写回"),
            on_change=self._commit_clip)
        g = self._group(box, "密码识别")
        self.auto_add_cb = self._check(
            g, "识别到新密码就自动存进密码本", "auto_add_clipboard_password",
            "剪贴板里识别出口令时自动收进长期密码本（与「密码本」页里的开关是同一项）。",
            syn=("密码本", "自动收录", "长期密码", "口令"), default=False)
        ph = QWidget(self)
        pl = QHBoxLayout(ph)
        pl.setContentsMargins(0, 0, 0, 0)
        pl.setSpacing(6)
        pl.addWidget(self._make_name(
            ph, "密码本条目", _SettingRow("密码本条目", "", "passwords", None,
                                          "clipboard", "密码识别")))
        self.passwords_label = QLabel("", ph)
        self.passwords_label.setObjectName("stripHint")
        pl.addWidget(self.passwords_label)
        pl.addStretch(1)
        self._reg("passwords", self.passwords_label)
        g.addWidget(ph)
        self._rows.append(_SettingRow(
            "密码本条目", "长期密码本在「密码本」页管理，这里只显示条数。",
            "passwords", self.passwords_label, "clipboard", "密码识别",
            syn=("密码本", "条目", "数量")))

        g = self._group(box, "临时密码")
        self.url_exclude_cb = self._check(
            g, "网址不当密码记录", "url_exclude_temp_password",
            "带 :// 的完整网址不记成临时密码；xxxx.com 这种没带协议的仍会记录。关掉就连网址也照收。",
            syn=("网址排除", "临时密码", "过滤", "协议"))
        self.temp_filter_cb = self._check(
            g, "更严格的密码过滤", "temp_password_filter",
            "在上一项基础上，再挡掉多行文本、文件路径、文件名、句子、超长串等明显不是口令的内容。",
            syn=("智能过滤", "严格", "误报", "口令"), default=False)
        self.url_exclude_cb.toggled.connect(
            lambda s: self.temp_filter_cb.setEnabled(bool(s)))
        self.ttl_spin = self._spin_row(
            g, "临时密码有效期", "temp_password_ttl_hours",
            "超过这个小时数自动清理。", 1, 24 * 365,
            syn=("有效期", "过期", "清理", "小时"), unit="小时", default=24)
        self.temp_max_spin = self._spin_row(
            g, "临时密码保留上限", "temp_password_max",
            "超过这个条数就丢掉最旧的。", 1, 100000,
            syn=("上限", "条数", "丢最旧"), unit="条", default=200)

    def _build_links(self, box):
        g = self._group(box, "打开二维码链接")
        self.qr_url_cb = self._check(
            g, "复制网址时自动识别二维码", "qr_url_enabled",
            "复制 http(s) 网址时自动访问一次；如果返回的是二维码图片，就解码后按设置打开。",
            syn=("复制网址", "识别二维码", "http", "自动访问"))
        self.qr_redirect_cb = self._check(
            g, "二维码链接域名重定向", "qr_url_redirect",
            "打开前按下面的重定向规则改写域名，例如 drive.uc.cn → fast.uc.cn。",
            syn=("重定向", "改写域名", "加速域名", "跳转"))
        self.rules_edit = self._text_row(
            g, "域名重定向规则", "url_redirect_rules",
            "每行一条「源域名 -> 目标域名」；只换主机名，路径和参数原样保留。",
            _parse_redirect_rules, syn=("重定向规则", "域名替换", "映射"),
            rows=3, placeholder="每行一条：源域名 -> 目标域名")

        g = self._group(box, "网址信任")
        self.trust_builtin_cb = self._check(
            g, "拦截内网与本机地址", "url_trust.builtin_blacklist",
            "私网 / 回环 / 链路本地 / 云元数据等地址一律拒绝，防 SSRF；两种用途共享。",
            syn=("SSRF", "内网", "回环", "本机", "元数据", "敏感地址", "安全"),
            extra_keys=["url_trust"], commit=lambda _c: self._commit_trust())
        self.tls_cb = self._check(
            g, "不验证 HTTPS 证书", "tls_skip_verify",
            "只在这类网站证书有问题时才需要。开启后可能被中间人攻击，默认关。",
            syn=("HTTPS", "证书", "TLS", "中间人", "MITM", "SSL"),
            risk=True, default=False)
        self._trust_radios = {}
        self.trust_editors = {}
        self._build_trust_purpose(
            g, "open", "打开二维码链接时，遇到新网站",
            "无操作 / 弹窗询问 / 自动信任 / 自动拒绝。")
        self._build_trust_purpose(
            g, "fetch", "下载识别时，遇到新网站", "同上；两种用途互不影响。")
        # 既有说明（两条）随信任分区保留：一条讲内置敏感地址 + 两套名单关系，
        # 一条讲分享链路例外（实验性开启后不受两套名单限制，必须与旧行为逐字一致）。
        self.trust_note = self._hint(
            "说明：私网 / 回环 / 链路本地 / 元数据等内置敏感地址默认拒绝，"
            "即使选择「自动信任」也不会放行，只有手动加入白名单才会信任。"
            "两套名单互不影响：同一域名可「自动打开」放行、同时「下载识别」拒绝。",
            self)
        self.trust_note.setStyleSheet("color: %s;" % PALETTE["danger"])
        g.addWidget(self.trust_note)
        g.addWidget(self._hint(
            "⚠ 分享链路例外：开启「实验性功能」后，分享链路抓取公开分享页时不受上述"
            "两套名单限制（必须先抓一次分享页才能拿到 shareid/share_uk）。"
            "把 pan.baidu.com 加进「下载识别」的黑名单，拦不住分享链路的这次抓取。",
            self, warn=True))

        g = self._group(box, "网盘与分享")
        self.share_wait_spin = self._spin_row(
            g, "分享等待时长", "share_gesture_wait_sec",
            "分享手势最多等「解析中链接」多少秒；超时取消、不回退旧链接（5~600）。",
            5, 600, syn=("分享", "手势", "等待", "超时", "解析中"),
            unit="秒", default=60)
        self.baidu_auto_invoke_cb = self._check(
            g, "检测到分享链接时自动拉起客户端下载", "baidu_auto_invoke",
            "复制到百度网盘分享链接时，自动交给网盘客户端下载（整包）。会自动触发下载，请确认来源可信。",
            syn=("网盘", "自动拉起", "客户端", "百度", "下载"), default=False)
        # 注意：方案 §5.5 的「分享下载前先让我挑选文件」(baidu_pick_before_download)
        # 在本机 config 里已被 _sanitize_cfg 显式 pop（该功能 v2.1.6 退场）。若在此
        # 放一个活控件，用户一勾就写一个「加载即被丢弃」的键 → 回读校验必失败、
        # 界面报「保存失败」。故按落地纪律「冲突项先搁置」不渲染该项（见交回清单）。
        bah = QWidget(self)
        bal = QHBoxLayout(bah)
        bal.setContentsMargins(0, 0, 0, 0)
        bal.setSpacing(6)
        bal.addWidget(self._make_name(
            bah, "网盘任务库路径",
            _SettingRow("网盘任务库路径", "", "baidu_task_db", None, "links",
                        "网盘与分享")))
        self.baidu_db_edit = QLineEdit(bah)
        self.baidu_db_edit.setPlaceholderText("留空 = 自动探测网盘客户端任务库")
        self.baidu_db_edit.setToolTip("BaiduYunGuanjia.db 路径；留空自动探测。")
        bal.addWidget(self.baidu_db_edit, 1)
        self.baidu_db_browse_btn = QPushButton("浏览", bah)
        self.baidu_db_browse_btn.setCursor(Qt.PointingHandCursor)
        self.baidu_db_browse_btn.clicked.connect(self._browse_baidu_db)
        bal.addWidget(self.baidu_db_browse_btn)
        self._reg("baidu_task_db", self.baidu_db_edit)
        self._bind_text(self.baidu_db_edit, "baidu_task_db",
                        lambda: str(self.baidu_db_edit.text()).strip())
        g.addWidget(bah)
        self._rows.append(_SettingRow(
            "网盘任务库路径", "BaiduYunGuanjia.db 的位置；留空自动探测。",
            "baidu_task_db", self.baidu_db_edit, "links", "网盘与分享",
            syn=("任务库", "数据库", "路径", "探测", "百度")))
        self.share_nologin_hint = self._hint(
            "⚠ 实验性提示：该链路不携带浏览器登录态，也不使用浏览器 cookie。"
            "若百度网盘客户端未在运行，唤起可能让客户端进入未登录状态；"
            "因此自动拉起前会先检查客户端进程，未运行时跳过并提示。", self, warn=True)
        g.addWidget(self.share_nologin_hint)
        self._share_nologin_hint = self.share_nologin_hint

        self._exp_subs = (self.baidu_auto_invoke_cb,
                          self.baidu_db_edit, self.baidu_db_browse_btn,
                          self.share_nologin_hint)

    def _build_trust_purpose(self, lay, purpose, title, tip):
        """某用途的信任分区：新域名默认行为单选 + 白/黑名单编辑框。"""
        self._sub_label(lay, title)
        self._build_trust_radios(lay, purpose, tip)
        hosts = {}
        for key, label, hint in (
                ("whitelist", "自动%s · 白名单" % ("打开" if purpose == "open" else "识别"),
                 "每行一个域名，含全部子域；命中即信任。"),
                ("blacklist", "自动%s · 黑名单" % ("打开" if purpose == "open" else "识别"),
                 "每行一个域名，优先级最高；命中即静默拒绝。")):
            edit = self._text_row(
                lay, label, "url_trust.%s.%s" % (purpose, key), hint,
                lambda t: self._collect_trust(), syn=("白名单" if key == "whitelist"
                                                      else "黑名单", "域名"),
                rows=3, placeholder="每行一个域名", commit_key="url_trust")
            hosts[key] = edit
        self.trust_editors[purpose] = hosts

    def _build_trust_radios(self, lay, purpose, tip):
        """信任用途的单选组（独立于通用 _radio_row，需按用途登记）。"""
        host = QWidget(lay.parentWidget())
        b = QVBoxLayout(host)
        b.setContentsMargins(0, 0, 0, 0)
        b.setSpacing(3)
        grp = QButtonGroup(host)
        radios = {}
        for value, text, t in (
                ("none", "无操作", "不打开、不询问、也不记录，静默跳过。"),
                ("ask", "弹窗询问（默认）", "每次遇到本用途下未信任的新域名都弹窗询问。"),
                ("auto_whitelist", "自动信任", "公网新域名自动放行并加入本用途白名单。"),
                ("auto_blacklist", "自动拒绝", "公网新域名自动拒绝并加入本用途黑名单。")):
            rb = QRadioButton(text, host)
            rb.setToolTip(t)
            grp.addButton(rb)
            radios[value] = rb
            b.addWidget(rb)
        self._reg("url_trust.%s.new_domain_action" % purpose, host)
        for rb in radios.values():
            rb.toggled.connect(lambda c: self._commit_trust() if c else None)
        lay.addWidget(host)
        self._trust_radios[purpose] = radios

    def _build_ui_domain(self, box):
        g = self._group(box, "外观")
        self.theme_combo = QComboBox(self)
        for label, value in _THEME_ITEMS:
            self.theme_combo.addItem(label, value)
        self.theme_combo.setMinimumWidth(150)
        self.theme_combo.currentIndexChanged.connect(self._on_theme_selected)
        self._reg("ui_theme", self.theme_combo)
        th = QWidget(self)
        tl = QHBoxLayout(th)
        tl.setContentsMargins(0, 0, 0, 0)
        tl.setSpacing(6)
        tl.addWidget(self._make_name(
            th, "主题", _SettingRow("主题", "", "ui_theme", None, "ui", "外观")))
        tl.addWidget(self.theme_combo)
        tl.addStretch(1)
        g.addWidget(th)
        self._rows.append(_SettingRow(
            "主题", "跟随系统 / 浅色 / 深色；切换后立即生效。", "ui_theme",
            self.theme_combo, "ui", "外观",
            syn=("主题", "深色", "浅色", "暗色", "配色", "跟随系统"),
            default_key="ui_theme"))

        self.logcolor_cb = self._check(
            g, "日志按类型着色", "log_colors_enabled",
            "运行日志按成功 / 失败 / 等待等类型着色。",
            syn=("日志", "着色", "颜色", "高亮"))
        self.show_tips_cb = self._check(
            g, "底栏滚动提示", "show_status_tips",
            "底栏每次滚动显示一句使用提示（如「拖入压缩包即可直接解压」）；关掉就不再轮播。",
            syn=("状态栏", "底栏", "提示", "滚动", "使用提示", "轮播"))
        # 只读行：上次实际应用的主题（自动维护；覆盖契约要求有归属控件）
        self.theme_cached_label = QLabel("", self)
        self.theme_cached_label.setObjectName("stripHint")
        self.theme_cached_label.setToolTip(
            "自动维护：启动时先用上次实际应用的主题出首屏，显示后再按「主题」偏好纠正。")
        self._reg("ui_theme_cached", self.theme_cached_label)
        ch = QWidget(self)
        chl = QHBoxLayout(ch)
        chl.setContentsMargins(0, 0, 0, 0)
        chl.setSpacing(6)
        chl.addWidget(self._make_name(
            ch, "上次实际应用的主题",
            _SettingRow("上次实际应用的主题", "", "ui_theme_cached", None, "ui",
                        "外观")))
        chl.addWidget(self.theme_cached_label)
        chl.addStretch(1)
        g.addWidget(ch)
        self._rows.append(_SettingRow(
            "上次实际应用的主题",
            "程序自动维护：启动时先用它出首屏，显示后再按「主题」偏好纠正。",
            "ui_theme_cached", self.theme_cached_label, "ui", "外观",
            syn=("缓存主题", "首屏", "启动")))

        g = self._group(box, "全局快捷键")
        self.hotkey_enable_cb = self._check(
            g, "启用全局快捷键", "hotkey_enabled",
            "主界面隐藏到托盘时也能用它唤起。", syn=("快捷键", "热键", "全局", "唤起"))
        self.hotkey_edit = self._hotkey_row(
            g, "唤起主界面", "hotkey", "快捷键组合；留空 = 不设置。",
            syn=("唤起", "主界面", "显示窗口", "热键"))
        self.hotkey_share_edit = self._hotkey_row(
            g, "分享下载", "hotkey_share", "用客户端下载最近一次分享；留空 = 不设置。",
            syn=("分享", "下载", "热键", "最近分享"))
        self.hotkey_share_pick_edit = self._hotkey_row(
            g, "挑选文件下载", "hotkey_share_pick",
            "用「先挑选文件」的方式下载最近一次分享；留空 = 不设置。",
            syn=("挑选", "选择文件", "分享", "热键"))

        g = self._group(box, "关闭行为")
        self._close_rbs = {}
        self._build_close_radios(g)

    def _hotkey_row(self, lay, label, key, desc, syn=()):
        meta = _SettingRow(label, desc, key, None, "ui", "全局快捷键", syn=syn,
                           default_key=key)

        def mk(host):
            box = QWidget(host)
            b = QHBoxLayout(box)
            b.setContentsMargins(0, 0, 0, 0)
            b.setSpacing(6)
            edit = HotkeyEdit(box)
            clear = QPushButton("清除", box)
            clear.setObjectName("ghostSm")
            clear.setCursor(Qt.PointingHandCursor)
            clear.clicked.connect(lambda: self._clear_hotkey(edit, key))
            edit.comboChanged.connect(
                lambda combo, k=key: self._commit(k, str(combo).strip()))
            self._reg(key, edit)
            b.addWidget(edit, 1)
            b.addWidget(clear)
            return box

        self._row(lay, meta, mk, key=key)
        # 抓回 HotkeyEdit（回填 / 读取用）
        edit = None
        for w in self._controls.get(key, []):
            if isinstance(w, HotkeyEdit):
                edit = w
        meta.widget = edit
        return edit

    def _build_close_radios(self, lay):
        host = QWidget(lay.parentWidget())
        b = QVBoxLayout(host)
        b.setContentsMargins(0, 0, 0, 0)
        b.setSpacing(3)
        grp = QButtonGroup(host)
        for value, text, tip in (("ask", "每次询问", "每次关闭都弹出选择。"),
                                 ("tray", "隐藏到托盘", "程序继续在后台运行。"),
                                 ("exit", "关闭程序", "停止所有监听与剪贴板监控。")):
            rb = QRadioButton(text, host)
            rb.setToolTip(tip)
            grp.addButton(rb)
            rb.toggled.connect(lambda c: self._commit_close() if c else None)
            b.addWidget(rb)
            self._close_rbs[value] = rb
        self._reg("close_action", host)
        lay.addWidget(host)

    def _build_system(self, box):
        g = self._group(box, "监听")
        self.interval_spin = self._spin_row(
            g, "目录扫描间隔", "poll_interval",
            "每隔多少秒扫一次监听目录；改完下一轮即生效。", 1, 30,
            syn=("轮询", "扫描", "间隔", "频率", "性能"), unit="秒", default=2)
        g = self._group(box, "解压引擎")
        # sevenzip_check_done 的界面语义与键值相反：勾选 = 下次启动重新检测 = 写 False
        self.sevenzip_cb = _Switch(self)   # 名称由左侧标签承担，选框不带文字
        self._reg("sevenzip_check_done", self.sevenzip_cb)
        sh = QWidget(self)
        sl = QHBoxLayout(sh)
        sl.setContentsMargins(0, 0, 0, 0)
        sl.setSpacing(6)
        sl.addWidget(self._make_name(
            sh, "下次启动重新检测 7-Zip",
            _SettingRow("下次启动重新检测 7-Zip", "", "sevenzip_check_done",
                        None, "system", "解压引擎")))
        sl.addStretch(1)
        sl.addWidget(self.sevenzip_cb)
        g.addWidget(sh)
        self._rows.append(_SettingRow(
            "下次启动重新检测 7-Zip",
            "勾上就表示「已检测过」；取消勾选 = 下次启动重新检测，缺失或版本过低时会弹安装引导。",
            "sevenzip_check_done", self.sevenzip_cb, "system", "解压引擎",
            syn=("7-Zip", "7z", "检测", "重新检测", "引擎", "安装"),
            default_key="sevenzip_check_done"))
        # 语义反转：勾选 -> False（重检）；不勾 -> True（已检测）
        self.sevenzip_cb.toggled.connect(
            lambda checked: self._commit("sevenzip_check_done", not bool(checked)))
        g = self._group(box, "历史")
        self.task_limit_spin = self._spin_row(
            g, "任务历史保留条数", "task_history_limit",
            "只清理已结束的任务，进行中的永不删除；磁盘上的旧数据在下次启动时清理。",
            1, 100000, syn=("历史", "记录", "条数", "上限", "清理"),
            unit="条", default=500)

    def _build_lab(self, box):
        g = self._group(box, "总开关")
        self.experimental_cb = self._check(
            g, "开启实验性功能", "experimental_enabled",
            "默认关闭。开启后解锁：网盘任务库只读探测、自动拉起客户端、分享前挑选文件"
            "（都在「链接与网盘」里）。",
            syn=("实验性", "总开关", "不稳定", "测试", "网盘"), default=False)
        self.experimental_cb.toggled.connect(lambda _s: self._sync_experimental())

    def _build_dirs(self, box):
        self.dirs_box = box
        self._dir_card_widgets = []
        self.rebuild_dirs()

    def rebuild_dirs(self):
        """按 watch_paths 重建目录卡（每卡固定 6 个字段）。

        旧卡必须**先注销**再销毁：卡内控件登记在 `_controls`（dir.* 键）与
        `_text_timers` 里，若只 setParent(None) 丢引用，QFrame 会被 C++ 析构、
        其子控件随之被删，但登记表仍持有野指针 → 之后任何遍历 `_controls` 的
        代码都会 RuntimeError。这里先把旧卡下的控件从登记表里摘掉。"""
        box = self.dirs_box
        # 清空旧的目录卡（先注销登记，再销毁）
        for w in getattr(self, "_dir_card_widgets", []):
            try:
                stale = w.findChildren(QWidget)
            except Exception:
                stale = []
            for child in stale:
                for store in (self._controls, self._text_timers):
                    for k, lst in list(store.items()):
                        if isinstance(lst, list):
                            store[k] = [x for x in lst if x is not child]
                        elif lst is child:
                            store.pop(k, None)
                self._name_labels.pop(id(child), None)
            try:
                w.setParent(None)
                w.deleteLater()
            except Exception:
                pass
        self._dir_card_widgets = []
        for key in [k for k in list(self._dev_dots) if k.startswith("dir.")]:
            self._dev_dots.pop(key, None)
        try:
            entries = [e for e in (self._snapshot().get("watch_paths") or [])
                       if isinstance(e, dict)]
        except Exception:
            entries = []
        for idx, entry in enumerate(entries):
            card = QFrame(box.parentWidget())
            card.setObjectName("card")
            cv = QVBoxLayout(card)
            cv.setContentsMargins(12, 10, 12, 10)
            cv.setSpacing(6)
            head = QHBoxLayout()
            path = str(entry.get("path") or "（未设置路径）")
            hlbl = QLabel(path, card)
            hlbl.setObjectName("sectionTitle")
            head.addWidget(hlbl)
            badge = QLabel("已启用" if entry.get("enabled", True) else "已停用", card)
            badge.setObjectName("chipState")
            head.addWidget(badge)
            head.addStretch(1)
            open_btn = QPushButton("打开目录设置", card)
            open_btn.setObjectName("ghostSm")
            open_btn.setCursor(Qt.PointingHandCursor)
            open_btn.clicked.connect(self._open_dir_dialog)
            head.addWidget(open_btn)
            cv.addLayout(head)

            self._dir_field_path(cv, card, idx, entry)
            self._dir_field_output(cv, card, idx, entry)
            self._dir_field_enabled(cv, card, idx, entry)
            self._dir_field_mode(cv, card, idx, entry)
            self._dir_field_delete(cv, card, idx, entry)
            self._dir_field_policy(cv, card, idx, entry)
            box.addWidget(card)
            self._dir_card_widgets.append(card)

    def _dir_set(self, idx, field, value):
        """就地改某目录某字段（整段 watch_paths 写回，绝不丢其它字段）。"""
        entries = [dict(e) for e in (self._snapshot().get("watch_paths") or [])
                   if isinstance(e, dict)]
        if not (0 <= idx < len(entries)):
            return
        entries[idx][field] = value
        self._commit("watch_paths", entries)

    def _dir_label(self, card, text, key=None):
        lbl = QLabel(text, card)
        lbl.setObjectName("setName")
        lbl.setCursor(Qt.PointingHandCursor)
        lbl.installEventFilter(self)
        meta = _SettingRow(text, "", key or "watch_paths", None, "dirs", "目录")
        self._name_labels[id(lbl)] = (lbl, meta)
        return lbl, meta

    def _dir_field_path(self, lay, card, idx, entry):
        h = QHBoxLayout()
        lbl, meta = self._dir_label(card, "监听路径", "dir.path")
        self._rows.append(meta)
        h.addWidget(lbl)
        edit = QLineEdit(card)
        edit.setText(str(entry.get("path") or ""))
        edit.setPlaceholderText("要盯着的文件夹")
        self._reg("dir.path", edit)
        self._bind_text(edit, "watch_paths",
                        lambda i=idx, e=edit: self._dir_collect(i, "path", e.text()))
        h.addWidget(edit, 1)
        browse = QPushButton("浏览", card)
        browse.setCursor(Qt.PointingHandCursor)
        browse.clicked.connect(lambda: self._browse_dir(idx, edit, "path"))
        h.addWidget(browse)
        lay.addLayout(h)

    def _dir_field_output(self, lay, card, idx, entry):
        h = QHBoxLayout()
        lbl, meta = self._dir_label(card, "解压到", "dir.output_dir")
        self._rows.append(meta)
        h.addWidget(lbl)
        edit = QLineEdit(card)
        edit.setText(str(entry.get("output_dir") or ""))
        edit.setPlaceholderText("留空 = 同目录下建同名文件夹")
        self._reg("dir.output_dir", edit)
        self._bind_text(edit, "watch_paths",
                        lambda i=idx, e=edit: self._dir_collect(i, "output_dir", e.text()))
        h.addWidget(edit, 1)
        browse = QPushButton("浏览", card)
        browse.setCursor(Qt.PointingHandCursor)
        browse.clicked.connect(lambda: self._browse_dir(idx, edit, "output_dir"))
        h.addWidget(browse)
        lay.addLayout(h)

    def _dir_field_enabled(self, lay, card, idx, entry):
        h = QHBoxLayout()
        lbl, meta = self._dir_label(card, "启用这个目录", "dir.enabled")
        self._rows.append(meta)
        h.addWidget(lbl)
        h.addStretch(1)
        cb = _Switch(card)
        cb.setChecked(bool(entry.get("enabled", True)))
        cb.toggled.connect(lambda c, i=idx: self._dir_set(i, "enabled", bool(c)))
        self._reg("dir.enabled", cb)
        h.addWidget(cb)
        lay.addLayout(h)

    def _dir_field_mode(self, lay, card, idx, entry):
        lbl, meta = self._dir_label(card, "监听模式", "dir.mode")
        self._rows.append(meta)
        lay.addWidget(lbl)
        cur = str(entry.get("mode") or "surface")
        grp = QButtonGroup(card)
        host = QWidget(card)
        b = QVBoxLayout(host)
        b.setContentsMargins(0, 0, 0, 0)
        b.setSpacing(3)
        for value, text in (("surface", "只扫表层"), ("manifest", "按网盘清单处理子目录")):
            rb = QRadioButton(text, host)
            grp.addButton(rb)
            rb.setChecked(value == cur)
            rb.toggled.connect(
                lambda c, i=idx, v=value: self._dir_set(i, "mode", v) if c else None)
            b.addWidget(rb)
        self._reg("dir.mode", host)
        lay.addWidget(host)

    def _dir_field_delete(self, lay, card, idx, entry):
        h = QHBoxLayout()
        lbl, meta = self._dir_label(card, "本目录解压后删除源文件", "dir.delete_source")
        meta.risk = True
        self._rows.append(meta)
        h.addWidget(lbl)
        badge = QLabel("风险", card)
        badge.setObjectName("riskBadge")
        h.addWidget(badge)
        self._risk_badges["dir.delete_source.%d" % idx] = badge
        h.addStretch(1)
        cb = _Switch(card)
        cb.setChecked(bool(entry.get("delete_source", False)))
        cb.toggled.connect(lambda c, i=idx: self._dir_set(i, "delete_source", bool(c)))
        self._reg("dir.delete_source", cb)
        h.addWidget(cb)
        lay.addLayout(h)

    def _dir_field_policy(self, lay, card, idx, entry):
        lbl, meta = self._dir_label(card, "回收站不可用时", "dir.delete_policy")
        meta.risk = True
        self._rows.append(meta)
        h = QHBoxLayout()
        h.addWidget(lbl)
        badge = QLabel("风险", card)
        badge.setObjectName("riskBadge")
        h.addWidget(badge)
        self._risk_badges["dir.delete_policy.%d" % idx] = badge
        h.addStretch(1)
        lay.addLayout(h)
        cur = str(entry.get("delete_policy") or "auto")
        host = QWidget(card)
        b = QVBoxLayout(host)
        b.setContentsMargins(0, 0, 0, 0)
        b.setSpacing(3)
        for value, text in (("auto", "自动判断"), ("purge", "永久删除"),
                            ("keep", "保留不删"), ("quarantine", "移入隔离区")):
            rb = QRadioButton(text, host)
            rb.setChecked(value == cur)
            rb.toggled.connect(
                lambda c, i=idx, v=value: self._dir_set(i, "delete_policy", v) if c else None)
            b.addWidget(rb)
        self._reg("dir.delete_policy", host)
        lay.addWidget(host)

    def _dir_collect(self, idx, field, value):
        return value

    def _browse_dir(self, idx, edit, field):
        try:
            if field == "output_dir":
                path = QFileDialog.getExistingDirectory(self, "选择解压目录")
            else:
                path = QFileDialog.getExistingDirectory(self, "选择监听目录")
        except Exception:
            path = ""
        if path:
            edit.setText(path)

    def _open_dir_dialog(self):
        """复用既有 WatchDirDialog（本页不自行增删目录）。"""
        try:
            from .dialogs import WatchDirDialog
        except Exception:
            self._notice("目录设置弹窗不可用", ok=False)
            return
        try:
            dlg = WatchDirDialog(self.state, self)
            if dlg.exec_():
                self.rebuild_dirs()
                self.watchPathsChanged.emit()
        except Exception as e:
            self._notice("打开目录设置失败：%s" % e, ok=False)

    def _browse_baidu_db(self):
        try:
            path, _flt = QFileDialog.getOpenFileName(
                self, "选择网盘任务库", "", "数据库 (*.db);;所有文件 (*)")
        except Exception:
            path = ""
        if path:
            self.baidu_db_edit.setText(path)

    # ------------------------------------------------------------------
    # 领域导航
    # ------------------------------------------------------------------
    def _refresh_catalog(self, select=None):
        """按 _sections 顺序重建左栏（领域名或分隔线），恢复选中项。"""
        from PyQt5.QtWidgets import QListWidgetItem
        self.cat_list.blockSignals(True)
        self.cat_list.clear()
        first_row = 0
        for did in self._sections:
            if did is None:
                item = QListWidgetItem("──────────")
                item.setFlags(Qt.NoItemFlags)
                self.cat_list.addItem(item)
                continue
            name, icon, _desc = _DOMAIN_META[did]
            item = QListWidgetItem(_glyph_icon(icon), name)
            item.setData(Qt.UserRole, did)
            self.cat_list.addItem(item)
        if self._sections:
            target = select if isinstance(select, int) else 0
            target = max(0, min(target, self.cat_list.count() - 1))
            self.cat_list.setCurrentRow(target)
        self.cat_list.blockSignals(False)
        self.cat_list.setVisible(bool(self._sections))
        if self._sections:
            self._show_domain(self._current_domain)

    def _on_cat_row_changed(self, row):
        """点左栏 → 右侧整页换成该领域（不是长滚动定位）；视口回顶部。"""
        if row < 0 or row >= self.cat_list.count():
            return
        item = self.cat_list.item(row)
        did = item.data(Qt.UserRole)
        if not did:
            return
        self._current_domain = did
        if self._query:
            return                       # 搜索态下切换领域无意义（结果列表优先）
        self._show_domain(did)

    def _show_domain(self, did):
        """只显示该领域容器，其余隐藏；滚动回顶部。"""
        for d, card in self._section_cards.items():
            card.setVisible(d == did)
        try:
            self.scroll.verticalScrollBar().setValue(0)
        except Exception:
            pass
        self._fit_all_hints()

    def _sub_label(self, lay, text):
        lbl = QLabel(str(text), self)
        lbl.setObjectName("sectionTitle")
        lay.addWidget(lbl)
        return lbl

    # ------------------------------------------------------------------
    # 搜索
    # ------------------------------------------------------------------
    def _on_search_changed(self, text):
        self._query = str(text or "").strip().lower()
        # 搜索时隐藏「恢复默认」（D3-1）
        self.reset_btn.setVisible(not self._query)
        if not self._query:
            self._hide_search_results()
            self._show_domain(self._current_domain)
            return
        self._show_search_results(self._query)

    def _show_search_results(self, q):
        """把右侧换成搜索结果（按领域分组、标命中数）。"""
        if getattr(self, "_result_card", None) is not None:
            self._result_card.setParent(None)
            self._result_card = None
        for d, card in self._section_cards.items():
            card.setVisible(False)
        hits = [r for r in self._rows if q in r.search_blob()]
        card = QWidget(self.scroll.widget())
        cl = QVBoxLayout(card)
        cl.setContentsMargins(0, 4, 0, 0)
        cl.setSpacing(6)
        head = QLabel("共 %d 项命中" % len(hits), card)
        head.setObjectName("appTitle")
        cl.addWidget(head)
        by_domain = {}
        for r in hits:
            by_domain.setdefault(r.domain, []).append(r)
        for d in _DOMAIN_ORDER:
            if d is None or d not in by_domain:
                continue
            dname = _DOMAIN_META[d][0]
            grp = QLabel("%s（%d 项）" % (dname, len(by_domain[d])), card)
            grp.setObjectName("sectionTitle")
            cl.addWidget(grp)
            for r in by_domain[d]:
                item = QLabel("•  %s" % r.label, card)
                item.setObjectName("setName")
                item.setCursor(Qt.PointingHandCursor)
                item.installEventFilter(self)
                self._name_labels[id(item)] = (item, r)
                cl.addWidget(item)
        cl.addStretch(1)
        # 插在末尾的顶对齐 stretch 之前（保持内容顶对齐）
        self._lay.insertWidget(self._lay.count() - 1, card)
        self._result_card = card
        try:
            self.scroll.verticalScrollBar().setValue(0)
        except Exception:
            pass

    def _hide_search_results(self):
        if getattr(self, "_result_card", None) is not None:
            self._result_card.setParent(None)
            self._result_card = None

    # ------------------------------------------------------------------
    # 补充信息气泡（悬停 700ms / 点击固定）
    # ------------------------------------------------------------------
    def _arm_bubble(self, obj):
        tup = self._name_labels.get(id(obj))
        if not tup:
            return
        self._pending_name = obj
        if self._bubble_pinned:
            return
        self._bubble_timer.start()

    def _on_name_leave(self, obj):
        if self._bubble_pinned:
            return
        if getattr(self, "_pending_name", None) is obj:
            self._bubble_timer.stop()
        if self._bubble is not None and self._bubble.isVisible() and not self._bubble_pinned:
            self._bubble.hide()

    def _on_bubble_due(self):
        obj = getattr(self, "_pending_name", None)
        if obj is None or not obj.underMouse():
            return
        self._show_bubble_for(obj, pinned=False)

    def _show_bubble_for(self, obj, pinned):
        tup = self._name_labels.get(id(obj))
        if not tup:
            return
        lbl, row = tup
        if self._bubble is None:
            self._bubble = _InfoBubble(self)
        self._bubble_pinned = bool(pinned)
        self._bubble_row = row
        self._bubble.show_for(row, lbl.mapToGlobal(QPoint(0, lbl.height())))
        self._fit_hint(self._bubble)

    def _toggle_pinned_bubble(self, obj):
        if self._bubble_pinned and self._bubble is not None and self._bubble.isVisible():
            self._bubble.hide()
            self._bubble_pinned = False
            return
        self._show_bubble_for(obj, pinned=True)

    def mousePressEvent(self, event):
        # 点空白处收起已固定气泡；按在「设置名称」上的点击不算空白——该次点击
        # 由 eventFilter 的 MouseButtonRelease 负责切换固定状态。
        if (self._bubble is not None and self._bubble.isVisible()
                and self._bubble_pinned):
            child = self.childAt(event.pos())
            on_name = child is not None and id(child) in self._name_labels
            if not on_name:
                self._bubble.hide()
                self._bubble_pinned = False
        super().mousePressEvent(event)

    def keyPressEvent(self, event):
        if event.key() == Qt.Key_Escape and self._bubble is not None:
            self._bubble.hide()
            self._bubble_pinned = False
        super().keyPressEvent(event)

    # ------------------------------------------------------------------
    # 回填（构造 / 恢复默认共用）
    # ------------------------------------------------------------------
    def _snapshot(self):
        try:
            return self.state.snapshot() or {}
        except Exception:
            return {}

    def _load_from_cfg(self):
        """把 state.snapshot() 的当前配置回填到所有控件（回填期间不触发即时保存）。"""
        self._loading = True
        try:
            self._cancel_text_timers()
            cfg = self._snapshot()

            def b(key, default=False):
                return bool(cfg.get(key, default))

            def s(key, default=""):
                v = cfg.get(key, default)
                return str(v if v is not None else default)

            def i(key, default=0):
                try:
                    return int(cfg.get(key, default))
                except Exception:
                    return default

            def fl(key, default=0.0):
                try:
                    return float(cfg.get(key, default))
                except Exception:
                    return default

            # 解压与整理
            self.output_time_cb.setChecked(b("output_time_now", True))
            self.promote_merge_cb.setChecked(b("promote_merge", True))
            self.translate_cb.setChecked(b("translation_move_enabled", True))
            self.pair_split_cb.setChecked(b("pair_split_enabled", True))
            suffixes = cfg.get("incomplete_download_suffixes")
            if not isinstance(suffixes, list):
                suffixes = DEFAULT_CONFIG.get("incomplete_download_suffixes")
            self.dl_suffix_edit.setPlainText(_format_suffix_lines(suffixes))

            # 删除与安全
            entries = [e for e in (cfg.get("watch_paths") or []) if isinstance(e, dict)]
            del_val, del_same = _common(entries, "delete_source")
            self.delete_master_cb.setChecked(bool(del_val) if del_same else False)
            self.bomb_guard_cb.setChecked(b("bomb_guard_enabled", True))
            self.bomb_entries_spin.setValue(max(0, min(1000000, i("bomb_soft_entries", 50000))))
            self.bomb_soft_ratio_spin.setValue(max(1, min(100000, i("bomb_soft_ratio", 100))))
            self.bomb_hard_ratio_spin.setValue(max(1, min(100000, i("bomb_hard_ratio", 200))))
            self.bomb_min_gb_spin.setValue(max(0, min(100000, i("bomb_hard_min_gb", 1))))
            self.bomb_size_gb_spin.setValue(max(0, min(1000000, i("bomb_hard_size_gb", 50))))
            self.free_space_spin.setValue(max(0, min(100000, i("min_free_space_gb", 5))))
            self._refresh_delete_state()

            # 通知与提醒
            self.notify_cb.setChecked(b("notify_enabled", True))
            self.notify_archive_cb.setChecked(b("notify_archive", True))
            self.notify_success_cb.setChecked(b("notify_success", True))
            self.notify_failure_cb.setChecked(b("notify_failure", True))
            self.notify_error_cb.setChecked(b("notify_error", True))
            self.notify_trayed_cb.setChecked(b("notify_trayed", True))
            self.notify_running_cb.setChecked(b("notify_already_running", True))
            self.notify_trust_cb.setChecked(b("notify_trust_pending", True))
            self.notify_share_cb.setChecked(b("notify_share", True))
            self.notify_share_dead_cb.setChecked(b("notify_share_dead", True))
            self.notify_baidu_done_cb.setChecked(b("notify_baidu_done", True))
            self.notify_baidu_leftover_cb.setChecked(b("notify_baidu_leftover", True))
            self.notify_baidu_dup_cb.setChecked(b("notify_baidu_dup", False))
            self._sync_notify_enabled()

            # 剪贴板与二维码
            self.qr_cb.setChecked(b("qr_enabled", True))
            cur_clip = s("qr_clipboard_action", "none") or "none"
            for rb in self.clip_host.findChildren(QRadioButton):
                pass
            self._set_radio_in_host(self.clip_host, _CLIP_LABELS, cur_clip)
            self.auto_add_cb.setChecked(b("auto_add_clipboard_password", False))
            self.url_exclude_cb.setChecked(b("url_exclude_temp_password", True))
            self.temp_filter_cb.setChecked(b("temp_password_filter", False))
            self.temp_filter_cb.setEnabled(self.url_exclude_cb.isChecked())
            self.ttl_spin.setValue(max(1, min(24 * 365, i("temp_password_ttl_hours", 24))))
            self.temp_max_spin.setValue(max(1, min(100000, i("temp_password_max", 200))))
            self._refresh_passwords_label()

            # 链接与网盘
            self.qr_url_cb.setChecked(b("qr_url_enabled", True))
            self.qr_redirect_cb.setChecked(b("qr_url_redirect", True))
            self.rules_edit.setPlainText(
                _format_redirect_rules(cfg.get("url_redirect_rules")))
            self._load_trust(cfg)
            self.tls_cb.setChecked(b("tls_skip_verify", False))
            self.share_wait_spin.setValue(max(5, min(600, i("share_gesture_wait_sec", 60))))
            self.baidu_auto_invoke_cb.setChecked(b("baidu_auto_invoke", False))
            self.baidu_db_edit.setText(s("baidu_task_db"))

            # 外观与快捷键
            pref = s("ui_theme", "auto").lower()
            index = 0
            for n, (_label, value) in enumerate(_THEME_ITEMS):
                if value == pref:
                    index = n
                    break
            self.theme_combo.setCurrentIndex(index)
            self._theme_pref = pref if pref in ("auto", "fluent", "devtool") else "auto"
            self.logcolor_cb.setChecked(b("log_colors_enabled", True))
            self.show_tips_cb.setChecked(b("show_status_tips", True))
            cached = s("ui_theme_cached").lower()
            self.theme_cached_label.setText(
                "上次实际应用：%s" % _THEME_NAMES.get(cached, "（未记录）"))
            self.hotkey_enable_cb.setChecked(b("hotkey_enabled", True))
            self.hotkey_edit.setText(s("hotkey"))
            self.hotkey_share_edit.setText(s("hotkey_share"))
            self.hotkey_share_pick_edit.setText(s("hotkey_share_pick"))
            self._hotkeys_at_load = (s("hotkey").strip(), s("hotkey_share").strip(),
                                     s("hotkey_share_pick").strip(),
                                     b("hotkey_enabled", True))
            cur_close = s("close_action", "ask") or "ask"
            if cur_close not in self._close_rbs:
                cur_close = "ask"
            self._close_rbs[cur_close].setChecked(True)

            # 系统与维护
            self.interval_spin.setValue(max(1, min(30, i("poll_interval", 2))))
            # 语义反转：已检测过(True) => 界面不勾选；否则勾选（下次重检）
            self.sevenzip_cb.setChecked(not b("sevenzip_check_done", False))
            self.task_limit_spin.setValue(max(1, min(100000, i("task_history_limit", 500))))

            # 实验性
            self.experimental_cb.setChecked(b("experimental_enabled", False))
            self._sync_experimental()

            # 监听目录
            self.rebuild_dirs()

            # 偏离默认小圆点 + 向导入口可见性 + 恢复默认可见性（搜索态）
            self._refresh_dirty_dots()
            self._refresh_wizard_visibility()
            self.reset_btn.setVisible(not self._query)
            self._clear_notice()
        finally:
            self._loading = False
        # 回填完成后按当前领域渲染一次（初始只显示一个领域）
        if self._query:
            self._show_search_results(self._query)
        else:
            self._show_domain(self._current_domain)

    def _set_radio_in_host(self, host, labels, value):
        for rb in host.findChildren(QRadioButton):
            if rb.text() in labels.get(value, ()):
                rb.setChecked(True)
                return

    def _load_trust(self, cfg):
        ut = cfg.get("url_trust") or {}
        if not isinstance(ut, dict):
            ut = {}
        self.trust_builtin_cb.setChecked(bool(ut.get("builtin_blacklist", True)))
        for purpose in ("open", "fetch"):
            sub = ut.get(purpose) if isinstance(ut.get(purpose), dict) else {}
            action = str((sub or {}).get("new_domain_action", "ask"))
            radios = self._trust_radios.get(purpose) or {}
            if action not in radios:
                action = "ask"
            radios[action].setChecked(True)
            for key in ("whitelist", "blacklist"):
                edit = (self.trust_editors.get(purpose) or {}).get(key)
                if edit is not None:
                    values = (sub or {}).get(key) or []
                    edit.setPlainText("\n".join(str(x) for x in values))

    def _refresh_delete_state(self):
        entries = [e for e in (self._snapshot().get("watch_paths") or [])
                   if isinstance(e, dict)]
        if not entries:
            self.delete_state_label.setText("")
            return
        n = sum(1 for e in entries if e.get("delete_source"))
        if 0 < n < len(entries):
            self.delete_state_label.setText(
                "当前：%d/%d 个目录已开启（勾选即统一覆盖全部）" % (n, len(entries)))
        elif n:
            self.delete_state_label.setText("当前：全部 %d 个目录均已开启" % n)
        else:
            self.delete_state_label.setText("当前：全部目录均保留源文件")

    def _refresh_passwords_label(self):
        try:
            n = len(self.state.passwords() or [])
            self.passwords_label.setText("%d 条（在「密码本」页管理）" % n)
        except Exception:
            self.passwords_label.setText("（读取失败 · 在「密码本」页管理）")

    def _refresh_dirty_dots(self):
        for key in list(self._dev_dots):
            self._mark_dirty(key)

    def _refresh_wizard_visibility(self):
        try:
            done = bool(self._snapshot().get("settings_wizard_done", False))
            self.wizard_btn.setVisible(not done)
        except Exception:
            pass

    # ------------------------------------------------------------------
    # 改即存（唯一写入口）
    # ------------------------------------------------------------------
    def _commit(self, key, value):
        """把单个配置键立即写入（改即存的唯一出口）：落盘 → 回读 → 副作用。"""
        if self._loading:
            return False
        if key == "ui_theme":
            err = self._apply_theme_path(value)
            if err:
                self._notice("已保存，但%s" % err, ok=False)
                self.settingsSaved.emit()
                return False
            value = self._theme_pref
        else:
            try:
                self.state.set(key, value)
            except Exception as e:
                self._notice("保存失败：%s" % e, ok=False)
                return False
        return self._after_commit(key, value)

    def _after_commit(self, key, value):
        """写入后的既有副作用：热键变更检测 → 回读校验 → 提示 / 信号。"""
        hotkey_error = ""
        if key in ("hotkey", "hotkey_share", "hotkey_share_pick", "hotkey_enabled"):
            hotkey_error = self._apply_hotkey_change()
        if key == "incomplete_download_suffixes":
            self._apply_incomplete_suffixes()
        if not self._verify_saved({key: value}):
            self._notice("保存失败：配置写入未生效（config.json 是否可写？）", ok=False)
            return False
        if key == "watch_paths":
            self.watchPathsChanged.emit()
        if key == "settings_wizard_done":
            self._refresh_wizard_visibility()
        if hotkey_error:
            self._notice("已保存，但%s" % hotkey_error, ok=False)
        else:
            self._notice("已保存", announce=False)
        self._mark_dirty(key)
        self.settingsSaved.emit()
        return True

    def _apply_incomplete_suffixes(self):
        try:
            from ..extraction.formats import set_incomplete_suffixes
            set_incomplete_suffixes(
                self._snapshot().get("incomplete_download_suffixes"))
        except Exception:
            pass

    def _apply_hotkey_change(self):
        cfg = self._snapshot()
        now = (str(cfg.get("hotkey") or "").strip(),
               str(cfg.get("hotkey_share") or "").strip(),
               str(cfg.get("hotkey_share_pick") or "").strip(),
               bool(cfg.get("hotkey_enabled", True)))
        if now == self._hotkeys_at_load:
            return ""
        self._hotkeys_at_load = now
        error = ""
        if self._hotkey_cb is not None:
            try:
                self._hotkey_cb()
            except Exception as e:
                error = "快捷键重新注册失败：%s" % e
        self.hotkeyChanged.emit(now[0])
        return error

    def _verify_saved(self, values):
        """回读校验：save_config 自己吞异常，必须读回磁盘才能确认真的写成功。"""
        try:
            disk = load_config()
        except Exception:
            return False
        for key, want in values.items():
            if key in _VERIFY_SKIP:
                continue
            got = disk.get(key, "<缺失>")
            if isinstance(want, bool):
                if bool(got) != want:
                    return False
            elif isinstance(want, int):
                try:
                    if int(got) != want:
                        return False
                except Exception:
                    return False
            elif isinstance(want, float):
                try:
                    if float(got) != want:
                        return False
                except Exception:
                    return False
            elif str(got) != str(want):
                return False
        return True

    # ---- 复合键的即时提交 ----
    def _commit_trust(self):
        self._commit("url_trust", self._collect_trust())

    def _commit_clip(self):
        self._commit("qr_clipboard_action", self._clip_value())

    def _commit_close(self):
        self._commit("close_action", self._close_value())

    def _on_theme_selected(self, _index):
        self._commit("ui_theme", self._theme_value())

    def _clear_hotkey(self, edit, key):
        edit.clear()
        self._commit(key, "")

    def _clip_value(self):
        for rb in getattr(self, "clip_host", None).findChildren(QRadioButton) \
                if getattr(self, "clip_host", None) is not None else []:
            if rb.isChecked():
                return _CLIP_LABELS_REV.get(rb.text(), "none")
        return "none"

    def _theme_value(self):
        try:
            return str(self.theme_combo.currentData() or "auto")
        except Exception:
            return "auto"

    def _close_value(self):
        for value, rb in self._close_rbs.items():
            if rb.isChecked():
                return value
        return "ask"

    def _collect_trust(self):
        ut = {"builtin_blacklist": bool(self.trust_builtin_cb.isChecked())}
        for purpose in ("open", "fetch"):
            action = "ask"
            for value, rb in (self._trust_radios.get(purpose) or {}).items():
                if rb.isChecked():
                    action = value
                    break
            hosts = self.trust_editors.get(purpose) or {}
            ut[purpose] = {
                "new_domain_action": action,
                "whitelist": _parse_domain_lines(
                    hosts["whitelist"].toPlainText() if hosts.get("whitelist") else ""),
                "blacklist": _parse_domain_lines(
                    hosts["blacklist"].toPlainText() if hosts.get("blacklist") else ""),
            }
        return ut

    @staticmethod
    def _empty_indices(entries):
        return [n for n, e in enumerate(entries or [])
                if not str(e.get("path") or "").strip()]

    # ---- 文本编辑防抖 ----
    def _bind_text(self, widget, key, compose):
        def _commit_now():
            if self._loading:
                return
            self._commit(key, compose())

        def _flush():
            timer.stop()
            _commit_now()

        timer = QTimer(self)
        timer.setSingleShot(True)
        timer.setInterval(_TEXT_DEBOUNCE_MS)
        timer.timeout.connect(_commit_now)
        widget.textChanged.connect(lambda *_a: self._arm_text_timer(widget))
        widget.installEventFilter(self)
        self._text_timers[widget] = (timer, _flush)

    def _arm_text_timer(self, widget):
        if self._loading:
            return
        item = self._text_timers.get(widget)
        if item is not None:
            item[0].start()

    def _flush_text(self, widget):
        item = self._text_timers.get(widget)
        if item is not None:
            item[0].stop()
            item[1]()

    def _cancel_text_timers(self):
        for timer, _flush in self._text_timers.values():
            timer.stop()

    # ------------------------------------------------------------------
    # 联动禁用
    # ------------------------------------------------------------------
    def _sync_notify_enabled(self):
        on = bool(self.notify_cb.isChecked())
        for w in self._notify_subs + tuple(self._notify_labels):
            w.setEnabled(on)

    def _sync_experimental(self):
        on = bool(self.experimental_cb.isChecked())
        for w in self._exp_subs:
            w.setEnabled(on)

    # ------------------------------------------------------------------
    # 删源总控 / 向导
    # ------------------------------------------------------------------
    def _on_delete_master(self, checked):
        """删源总控：点击即把 delete_source 统一写入全部监听目录。"""
        if self._loading:
            return
        want = bool(checked)
        cfg = self._snapshot()
        entries = [dict(e) for e in (cfg.get("watch_paths") or [])
                   if isinstance(e, dict)]
        empties = self._empty_indices(entries)
        if empties:
            ret = QMessageBox.question(
                self, "路径校验",
                "有 %d 条监听目录路径为空；继续会移除这些空条目（其余设置不受影响）。\n"
                "继续保存？" % len(empties),
                QMessageBox.Yes | QMessageBox.No, QMessageBox.No)
            if ret != QMessageBox.Yes:
                self._notice("保存已取消：请先处理空路径条目", ok=False)
                self._revert_delete_master(not want)
                return
            entries = [e for n, e in enumerate(entries) if n not in empties]
        for e in entries:
            e["delete_source"] = want
        if self._commit("watch_paths", entries):
            self._refresh_delete_state()
        else:
            self._revert_delete_master(not want)

    def _revert_delete_master(self, previous):
        self._loading = True
        try:
            self.delete_master_cb.setChecked(bool(previous))
        finally:
            self._loading = False
        self._refresh_delete_state()

    def _on_wizard_clicked(self):
        """设置向导入口：跳过后不再显示（持久化 settings_wizard_done）。"""
        ret = QMessageBox.question(
            self, "设置向导",
            "设置向导会带你过一遍最常用的几项。\n"
            "暂时不想看，可以点「跳过」——跳过后这个入口不再显示。",
            QMessageBox.Ok | QMessageBox.Cancel, QMessageBox.Ok)
        if ret != QMessageBox.Ok:
            if self._commit("settings_wizard_done", True):
                self._notice("已跳过设置向导（入口不再显示）")
            return
        self._notice("设置向导尚在规划中；已记录你的选择")
        self._commit("settings_wizard_done", True)

    # ------------------------------------------------------------------
    # 恢复默认（两步：选范围 → 再次确认）
    # ------------------------------------------------------------------
    def _page_keys(self, did):
        """某领域页渲染的顶层键集合（「本页」范围取数口径）。"""
        keys = set()
        for r in self._rows:
            k = r.key
            if not k:
                continue
            if did == "dirs":
                if k.startswith("dir."):
                    keys.add(k)
            elif did == "safety":
                if r.domain == "safety" and not k.startswith("dir."):
                    keys.add(k)
            elif r.domain == did:
                keys.add(k)
        return keys

    def _on_reset(self):
        """恢复默认：先选范围 → 警告 → 再次确认（底部唯一入口）。"""
        did = self._current_domain
        dname = _DOMAIN_META.get(did, ("本页",))[0]
        page_keys = self._page_keys(did)
        scope, ok = self._ask_reset_scope(dname, len(page_keys))
        if not ok:
            self._notice("已取消恢复默认")
            return
        if not self._confirm_reset(scope, dname, len(page_keys)):
            self._notice("已取消恢复默认")
            return
        self._do_reset(scope, page_keys)

    def _ask_reset_scope(self, dname, n_page):
        """第一步：选范围 + 警告。返回 (scope, ok)。scope ∈ {'all','page'}。"""
        box = QMessageBox(self)
        box.setWindowTitle("恢复默认")
        box.setIcon(QMessageBox.Warning)
        box.setText("恢复默认会覆盖你当前的设置，且不能撤销。请先选择要恢复的范围。")
        all_btn = box.addButton("整个程序", QMessageBox.AcceptRole)
        page_btn = box.addButton("本页（%s · %d 项）" % (dname, n_page),
                                 QMessageBox.AcceptRole)
        cancel = box.addButton("取消", QMessageBox.RejectRole)
        box.setDefaultButton(cancel)
        box.exec_()
        clicked = box.clickedButton()
        if clicked is cancel or clicked is None:
            return "all", False
        return ("page" if clicked is page_btn else "all"), True

    def _confirm_reset(self, scope, dname, n_page):
        """第二步：再次确认。"""
        if scope == "page":
            detail = "将要恢复的范围：本页「%s」（%d 项）" % (dname, n_page)
        else:
            detail = "将要恢复的范围：整个程序（全部 %d 项）" % len(DEFAULT_CONFIG)
        ret = QMessageBox.question(
            self, "再次确认",
            "即将恢复默认，你在这部分里的改动会全部丢失，且不能撤销。\n\n" + detail,
            QMessageBox.Yes | QMessageBox.No, QMessageBox.No)
        return ret == QMessageBox.Yes

    def _do_reset(self, scope, page_keys):
        old_pref = self._theme_pref
        old_hotkeys = self._hotkeys_at_load
        try:
            defaults = _sanitize_cfg(_deepcopy(DEFAULT_CONFIG))
            if scope == "page":
                # 只写本页涉及的顶层键（点号路径按键首段/整键处理）
                for k in page_keys:
                    if "." in k and not k.startswith("dir."):
                        top = k.split(".")[0]
                        self.state.set(top, _deepcopy(defaults.get(top)))
                    elif k.startswith("dir."):
                        top = "watch_paths"
                        self.state.set(top, _deepcopy(defaults.get(top)))
                    else:
                        self.state.set(k, _deepcopy(defaults.get(k)))
            else:
                for key, value in defaults.items():
                    self.state.set(key, _deepcopy(value))
        except Exception as e:
            self._notice("恢复默认失败：%s" % e, ok=False)
            return
        self._load_from_cfg()
        self._apply_incomplete_suffixes()
        if self._theme_pref != old_pref:
            self._apply_theme_path(self._theme_pref)
        if old_hotkeys != self._hotkeys_at_load:
            if self._hotkey_cb is not None:
                try:
                    self._hotkey_cb()
                except Exception as e:
                    self._notice("已恢复默认，但快捷键重新注册失败：%s" % e, ok=False)
                    return
            self.hotkeyChanged.emit(str(self._snapshot().get("hotkey") or "").strip())
        self._notice("已恢复默认设置")
        self.settingsReset.emit()

    # ------------------------------------------------------------------
    # 主题（既有路径）
    # ------------------------------------------------------------------
    def _apply_theme_path(self, pref):
        pref = str(pref or "auto").lower()
        if pref not in ("auto", "fluent", "devtool"):
            pref = "auto"
        try:
            self.state.set("ui_theme", pref)
            want = ui_style.resolve_theme(pref)
            ui_style.apply_theme(QApplication.instance(), want)
            self.state.set("ui_theme_cached", want)
            self._theme_pref = pref
            try:
                self.theme_cached_label.setText(
                    "上次实际应用：%s" % _THEME_NAMES.get(want, want))
            except Exception:
                pass
            if callable(self._theme_cb):
                self._theme_cb(want)
            self.themeChanged.emit(want)
            return ""
        except Exception as e:
            try:
                if self.hub is not None:
                    self.hub.log("切换主题失败: %s" % e)
            except Exception:
                pass
            return "主题切换失败：%s" % e

    # ------------------------------------------------------------------
    # 提示 / 主题刷新 / 供宿主调用的读取入口
    # ------------------------------------------------------------------
    def _clear_notice(self):
        self._notice_failed = False
        self.notice_label.setText("")
        self.notice_label.setToolTip("")
        try:
            self.notice_label.setStyleSheet("")
        except Exception:
            pass

    def _notice(self, text, ok=True, announce=True):
        text = str(text)
        self._notice_failed = not ok
        self.notice_label.setText(text)
        self.notice_label.setToolTip(text)
        try:
            self.notice_label.setStyleSheet(
                "" if ok else "color: %s;" % PALETTE["danger"])
        except Exception:
            pass
        if announce:
            self.notice.emit(text)

    def refresh_theme(self):
        """主题切换后重贴内联色（warn 说明 / 风险徽章 / 偏离点 / 气泡）。"""
        for lbl in self._warn_labels:
            try:
                lbl.setStyleSheet("color: %s;" % PALETTE["warn_text"])
            except Exception:
                pass
        for badge in self._risk_badges.values():
            try:
                badge.setStyleSheet(self._badge_qss())
            except Exception:
                pass
        for dot in self._dev_dots.values():
            try:
                dot.setStyleSheet("color: %s;" % PALETTE["warn_text"])
            except Exception:
                pass
        if self._notice_failed:
            try:
                self.notice_label.setStyleSheet("color: %s;" % PALETTE["danger"])
            except Exception:
                pass
        for glyph in self.findChildren(Glyph):
            try:
                glyph.update()
            except Exception:
                pass
        for sw in self.findChildren(_Switch):
            try:
                sw.update()     # 胶囊开关在 paint 时读 tokens()，主题变了要重绘
            except Exception:
                pass
        self._fit_all_hints()

    def _badge_qss(self):
        return ("QLabel#riskBadge { color: %s; background: %s; border: 1px solid %s;"
                " border-radius: 4px; padding: 0 6px; }"
                % (PALETTE["danger"], PALETTE["warn_bg"], PALETTE["warn_border"]))

    # ---- 风险徽章呼吸（往更高风险方向改动时；10s 后停回常态） ----
    def _breathe_badge(self, key):
        badge = self._risk_badges.get(key)
        if badge is None:
            return
        # 清理旧动画
        old = self._breath_timers.pop(key, None)
        if old is not None:
            old[0].stop()
        effect = badge.graphicsEffect()
        if not isinstance(effect, QGraphicsOpacityEffect):
            effect = QGraphicsOpacityEffect(badge)
            badge.setGraphicsEffect(effect)
        anim = QPropertyAnimation(effect, b"opacity", badge)
        anim.setDuration(2000)
        anim.setStartValue(1.0)
        anim.setKeyValueAt(0.5, 0.25)
        anim.setEndValue(1.0)
        anim.setLoopCount(5)          # 5 × 2s = 10s
        anim.start()
        self._breath_timers[key] = (anim, 0)

    def reload(self):
        """宿主外部改过配置（如「密码本」页）后，重新回填本页控件。"""
        self._load_from_cfg()

    def covered_keys(self):
        """已登记控件的配置键全集（含 url_trust.* / watch_paths.* 点号路径）。"""
        return set(self._controls.keys())

    def covered_top_keys(self):
        """顶层配置键（不含点号路径）。"""
        return {k for k in self._controls.keys() if "." not in k}

    def control_for(self, key):
        return list(self._controls.get(str(key)) or [])

    def section_titles(self):
        """领域显示名列表（左栏顺序，与领域一一对应；不含分隔线）。"""
        return [_DOMAIN_META[d][0] for d in self._sections if d is not None]


# 剪贴板联动的界面文案 -> 取值（_clip_value 反查用）
_CLIP_LABELS = {
    "none": ("不处理",),
    "code": ("恢复刚复制的提取码",),
    "url": ("把二维码原文写回剪贴板",),
}
_CLIP_LABELS_REV = {
    "不处理": "none",
    "恢复刚复制的提取码": "code",
    "把二维码原文写回剪贴板": "url",
}
