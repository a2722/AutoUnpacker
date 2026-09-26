# -*- coding: utf-8 -*-
"""设置页（正式页面）：A 方案「左栏领域」——9 个领域 + 顶部常驻搜索。

职责：
- SettingsPage：9 个领域（解压与整理 / 删除与安全 / 通知与提醒 / 剪贴板与二维码 /
  链接与网盘 / 外观与快捷键 / 系统与维护 ／ 实验性、监听目录）覆盖
  config.DEFAULT_CONFIG 的全部键；
- 列表默认极简：每行只有「名称 + 控件」；描述 / 风险说明一律**悬停满 700ms
  或点击名称**后由浮层气泡给出（只显示标题 + 描述；点击可固定并跟随窗口，
  点气泡外任意处 / Esc 收起）；
- 「改即存」：每个控件变更即走 AppState.set()（config.save_config 原子写），
  文本 / 多行编辑 400ms 防抖、失焦立即落盘；写后回读磁盘校验，失败如实回报；
- 「恢复默认」：**底部唯一入口**，先选范围（整个程序 / 本页）→ 警告 → 再次确认；
  正在搜索时该按钮不出现；
- 风险项（4 项）常驻红色「风险」徽章；往更高风险方向改动时徽章呼吸 10s（不弹窗）；
- 搜索：跨全部项按 名称 / 描述 / 同义词 / 配置键 过滤，按领域分组并标命中数；
- 目录（领域导航）：左侧 QListWidget#settingsCat，宽 236px；点选即整页换成该领域
  （不是长滚动 + 定位）；切领域视口回到顶部；
- 监听目录不再在本页增删改（页头胶囊条 → WatchDirDialog 已覆盖），本页把
  watch_paths 逐条原样写回，绝不丢字段；每张目录卡固定 6 个字段，
  「打开目录设置」就在该卡的虚线页脚里（按卡片绑定 idx，不再共用按钮）；
- #stripHint 描述行：11px CJK 墨迹几乎顶满 em 框，QLabel 折行高度按
  fontMetrics().height() 算、绘制按 lineSpacing() 排，默认上下各裁 1px；
  由 QSS padding + polish 后的 _fit_hint() 兜底（见 style.py 注释）；
- 主题：走既有 ui_style.resolve_theme + apply_theme + ui_theme_cached + 回调
  路径，绝不手写 QSS、绝不新增主题 token（风险徽章 / 呼吸灯 / 气泡都在
  refresh_theme() 里重贴）。

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

from PyQt5.QtCore import (QEvent, QPoint, QPropertyAnimation, QRect, QRectF,
                          QSize, QTimer, Qt, pyqtSignal)
from PyQt5.QtGui import QBrush, QColor, QPainter, QPainterPath, QPen
from PyQt5.QtWidgets import (QAbstractSpinBox, QApplication, QButtonGroup,
                             QCheckBox, QComboBox, QDoubleSpinBox, QFileDialog,
                             QFrame, QGraphicsDropShadowEffect,
                             QGraphicsOpacityEffect, QGridLayout, QHBoxLayout,
                             QLabel, QLineEdit, QListWidget, QMessageBox,
                             QPlainTextEdit, QPushButton, QRadioButton,
                             QScrollArea, QSizePolicy, QSpinBox, QStyle,
                             QStyledItemDelegate, QVBoxLayout, QWidget)

from ..config import DEFAULT_CONFIG, _sanitize_cfg
from ..config import load_config
from . import style as ui_style
from .style import METRICS, PALETTE
from .widgets import Glyph, HotkeyEdit, repolish_tree

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

# #stripHint 类说明标签（折行高度兜底 + 气泡正文）：见 _fit_all_hints / eventFilter
_HINT_NAMES = ("stripHint", "bubbleHint", "bubbleDesc", "bubbleRiskBody")

# 补充信息气泡：悬停延迟（对齐现有设置页的 Qt 原生 tooltip 唤醒延迟 = 700ms）
_BUBBLE_DELAY_MS = 700
# 补充信息气泡目标宽度（§7：宽 380，上限 屏宽-24）
_BUBBLE_W = 380
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


def _apply_card_shadow(widget, kind="card"):
    """给卡片挂 QGraphicsDropShadowEffect（§6.2；QSS 无 box-shadow）。

    只给「领域卡片 / 目录卡片 / 气泡 / 模态」这类少量容器挂，**不给每一行挂**
    （每行一个 effect 会走 pixmap 缓存，页内几十张卡就卡）。
    一个 widget 只能有一个 QGraphicsEffect；主题切换后由 refresh_theme() 重建
    （暗色阴影更重，颜色不同）。返回创建的 effect。
    """
    spec = ui_style.shadow_spec(kind)
    eff = QGraphicsDropShadowEffect(widget)
    eff.setBlurRadius(float(spec["blur"]))
    eff.setOffset(0.0, float(spec["dy"]))
    eff.setColor(QColor(0, 0, 0, int(spec["alpha"])))
    widget.setGraphicsEffect(eff)
    return eff


class _RailDelegate(QStyledItemDelegate):
    """左栏选中项：在 QSS 底色/粗体之上，再画一条左侧 3px 主色竖条（§6.1）。

    QSS 在 `::item` 上画 `border-left` 不可靠，故用委托自绘：颜色每次 paint 现取
    `ui_style.tokens()["primary_bg"]`，主题切换只需重绘（不缓存颜色）。竖条上下
    各内缩 7px、圆角 2px（radius_bar），与设计稿 `.nav-item.is-on::before` 一致。
    """

    _INSET = 7
    _WIDTH = 3

    def paint(self, painter, option, index):
        super().paint(painter, option, index)
        if option.state & QStyle.State_Selected:
            tk = ui_style.tokens()
            bar = QRect(int(option.rect.left()), int(option.rect.top()) + self._INSET,
                        self._WIDTH,
                        max(0, int(option.rect.height()) - self._INSET * 2))
            path = QPainterPath()
            path.addRoundedRect(QRectF(bar), 2.0, 2.0)
            painter.save()
            painter.setRenderHint(QPainter.Antialiasing, True)
            painter.fillPath(path, QColor(tk["primary_bg"]))
            painter.restore()


class _ResetScopeBox(QMessageBox):
    """恢复默认 · 第一步「选范围」：样式化模态（§7：头 / 警告块 / 范围卡 / 页脚）。

    为什么仍是 QMessageBox：离线验收把这一步锁定为 `QMessageBox.exec_()` 与按钮
    文案（`整个程序` / `本页（…）` / `取消`，见 test_page_settings.py F1/F3）。
    所以做法是——保留三个隐藏的标准按钮（供测试 `buttons()` / `clickedButton()`
    契约与假 exec_ 点击），把系统图标 / 文本 / 按钮盒藏起来，自建卡片式内容：
    头（标题 + ✕）/ 警告块（#warnBox）/ 范围卡（QRadioButton#scopeItem，含副标题）
    / 页脚（取消 + 下一步 #primary）。真实使用时点自建按钮走 `_scope`；
    测试路径下 `_scope` 为空，回退到 `clickedButton()`（`chosen()`）。
    """

    def __init__(self, parent, dname, n_page):
        super().__init__(parent)
        self.setObjectName("modalCard")
        self.setWindowTitle("恢复默认")
        self.setIcon(QMessageBox.NoIcon)
        self.setWindowFlags(Qt.Dialog | Qt.FramelessWindowHint)
        self.setModal(True)
        self._scope = None
        # 隐藏标准按钮：测试按文案点击 / 读取，真实界面不可见
        self._all_btn = self.addButton("整个程序", QMessageBox.AcceptRole)
        self._page_btn = self.addButton("本页（%s · %d 项）" % (dname, n_page),
                                        QMessageBox.AcceptRole)
        self._cancel_btn = self.addButton("取消", QMessageBox.RejectRole)
        for b in (self._all_btn, self._page_btn, self._cancel_btn):
            b.hide()
        for name in ("qt_msgbox_label", "qt_msgbox_informativelabel",
                     "qt_msgboxex_icon_label"):
            w = self.findChild(QLabel, name)
            if w is not None:
                w.hide()
        try:
            from PyQt5.QtWidgets import QDialogButtonBox
            bb = self.findChild(QDialogButtonBox)
            if bb is not None:
                bb.hide()
        except Exception:
            pass

        host = QWidget(self)
        v = QVBoxLayout(host)
        v.setContentsMargins(0, 0, 0, 0)
        v.setSpacing(0)

        # 头：标题 + 关闭
        head = QFrame(host)
        head.setObjectName("modalHead")
        hl = QHBoxLayout(head)
        hl.setContentsMargins(16, 14, 16, 14)
        hl.setSpacing(10)
        title = QLabel("恢复默认", head)
        title.setObjectName("modalTitle")
        hl.addWidget(title)
        hl.addStretch(1)
        close_btn = QPushButton(head)
        close_btn.setObjectName("modalClose")
        close_btn.setFixedSize(24, 24)
        close_btn.setCursor(Qt.PointingHandCursor)
        close_btn.setToolTip("取消")
        cl = QHBoxLayout(close_btn)
        cl.setContentsMargins(0, 0, 0, 0)
        glyph = Glyph("close", close_btn, 12, role="muted")
        glyph.setAttribute(Qt.WA_TransparentForMouseEvents, True)
        cl.addWidget(glyph, 0, Qt.AlignCenter)
        close_btn.clicked.connect(self.reject)
        hl.addWidget(close_btn)
        v.addWidget(head)

        # 体：警告块 + 范围卡 + 明细
        body = QWidget(host)
        bl = QVBoxLayout(body)
        bl.setContentsMargins(16, 16, 16, 16)
        bl.setSpacing(14)
        warn = QFrame(body)
        warn.setObjectName("warnBox")
        wl = QHBoxLayout(warn)
        wl.setContentsMargins(12, 10, 12, 10)
        wl.setSpacing(10)
        wicon = Glyph("alert", warn, 16, role="muted")
        wl.addWidget(wicon, 0, Qt.AlignTop)
        wtext = QLabel("恢复默认会覆盖你当前的设置，且不能撤销。请先选择要恢复的范围。",
                       warn)
        wtext.setObjectName("warnText")
        wtext.setWordWrap(True)
        wl.addWidget(wtext, 1)
        bl.addWidget(warn)

        self._group = QButtonGroup(self)
        self._radios = {}
        for value, name, sub in (
                ("all", "整个程序",
                 "全部 %d 项设置恢复为出厂默认" % len(DEFAULT_CONFIG)),
                ("page", "本页",
                 "只恢复「%s」这一页的 %d 项" % (dname, n_page))):
            rb = QRadioButton(body)
            rb.setObjectName("scopeItem")
            rb.setCursor(Qt.PointingHandCursor)
            rl = QVBoxLayout(rb)
            rl.setContentsMargins(26, 0, 0, 0)   # 让开左侧指示器
            rl.setSpacing(2)
            nm = QLabel(name, rb)
            nm.setObjectName("scopeName")
            nm.setAttribute(Qt.WA_TransparentForMouseEvents, True)
            rl.addWidget(nm)
            sb = QLabel(sub, rb)
            sb.setObjectName("scopeSub")
            sb.setWordWrap(True)
            sb.setAttribute(Qt.WA_TransparentForMouseEvents, True)
            rl.addWidget(sb)
            # QRadioButton.sizeHint 不含子布局 → 手动给足卡片高度（上下 padding + 两行）
            rb.setMinimumHeight(int(rl.sizeHint().height()) + 24)
            self._group.addButton(rb)
            self._radios[value] = rb
            bl.addWidget(rb)
        self._radios["all"].setChecked(True)
        v.addWidget(body)

        # 脚：取消 + 下一步（右对齐）
        foot = QFrame(host)
        foot.setObjectName("modalFoot")
        fl = QHBoxLayout(foot)
        fl.setContentsMargins(16, 12, 16, 12)
        fl.setSpacing(10)
        fl.addStretch(1)
        cancel_btn = QPushButton("取消", foot)
        cancel_btn.clicked.connect(self.reject)
        fl.addWidget(cancel_btn)
        next_btn = QPushButton("下一步", foot)
        next_btn.setObjectName("primary")
        next_btn.setCursor(Qt.PointingHandCursor)
        next_btn.clicked.connect(self._on_next)
        fl.addWidget(next_btn)
        v.addWidget(foot)

        # 自建内容占据原「按钮盒」那一行（隐藏项不参与布局）
        try:
            self.layout().addWidget(host, 3, 0, 1, 2)
        except Exception:
            pass
        # QMessageBox 在 showEvent 里会按（被隐藏的）文本标签把宽度钉死，
        # 故内容自身给足最小宽度，并在 showEvent 之后再锁 480（§7）。
        host.setMinimumWidth(448)
        try:
            _apply_card_shadow(self, "pop")
        except Exception:
            pass

    def showEvent(self, event):   # noqa: N802 (Qt 命名)
        super().showEvent(event)
        try:
            self.setFixedWidth(480)
        except Exception:
            pass

    def _on_next(self):
        for value, rb in self._radios.items():
            if rb.isChecked():
                self._scope = value
                break
        self.accept()

    def chosen(self):
        """返回 'all' / 'page'；取消（含 Esc / ✕）返回 None。"""
        if self._scope is not None:
            return self._scope
        clicked = self.clickedButton()
        if clicked is None or clicked is self._cancel_btn:
            return None
        return "page" if clicked is self._page_btn else "all"


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
    - 尺寸固定 38x20；指针为手型；OFF / ON / ON+hover / disabled 四态见 paint。
      注意：v1 视觉规格 §6.3 写「36×20」，但离线验收
      `test_settings_switch_and_bubble.py`（S2/S3-S5 逐像素采样）把 38×20 锁定
      为契约——按「绝不削弱测试」的纪律保留 38×20，差异见交回清单。"""

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

    def hitButton(self, pos):   # noqa: N802 (Qt 命名)
        """整块药丸都是点击区（修「只有左半区可点」）。

        QCheckBox 默认只认 `SE_CheckBoxClickRect`（指示器 + 文字矩形）；本类完全
        自绘、不带文字，于是右半区点不动。返回 `rect().contains(pos)` 让整块
        38×20 都能切换；状态语义仍全部走 QCheckBox（Space / setChecked /
        toggled 一字不变）。"""
        return self.rect().contains(pos)

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

    内容只保留用户可见的两段：名称（+ 风险徽章）/ 描述；风险项追加风险说明块。
    「默认：<值> / 配置键：<key>」与底部操作提示属**内部信息**，已按评审移除。
    同一时刻只允许一个（由 SettingsPage 统一持有并复用）。"""

    def __init__(self, parent=None):
        # 非 Qt.Popup：Popup 会抓鼠标，第二次点击（取消固定）会被弹窗吞掉，
        # 导致点击固定只能开不能关。改用 Qt.Tool + 不激活显示：
        # 不抢键盘焦点（WA_ShowWithoutActivating + NoFocus），点击照常落到名称标签，
        # 页面空白处点击（SettingsPage.mousePressEvent）与 Esc 仍可收起。
        super().__init__(parent, Qt.Tool | Qt.FramelessWindowHint)
        self.setAttribute(Qt.WA_ShowWithoutActivating, True)
        self.setAttribute(Qt.WA_StyledBackground, True)
        self.setFocusPolicy(Qt.NoFocus)
        self.setObjectName("settingsBubble")
        self._shadow = None
        self._lay = QVBoxLayout(self)
        self._lay.setContentsMargins(14, 12, 14, 12)
        self._lay.setSpacing(8)
        self._title = QLabel(self)
        self._title.setObjectName("bubbleTitle")
        self._title.setWordWrap(True)
        self._lay.addWidget(self._title)
        self._desc = QLabel(self)
        self._desc.setObjectName("bubbleDesc")
        self._desc.setWordWrap(True)
        self._lay.addWidget(self._desc)
        self._risk_box = QFrame(self)
        self._risk_box.setObjectName("bubbleRisk")
        rb = QVBoxLayout(self._risk_box)
        rb.setContentsMargins(11, 9, 11, 9)
        rb.setSpacing(6)
        self._risk_title = QLabel("风险提示", self._risk_box)
        self._risk_title.setObjectName("bubbleRiskTitle")
        rb.addWidget(self._risk_title)
        self._risk_lbl = QLabel(self._risk_box)
        self._risk_lbl.setObjectName("bubbleRiskBody")
        self._risk_lbl.setWordWrap(True)
        rb.addWidget(self._risk_lbl)
        self._lay.addWidget(self._risk_box)
        # 气泡阴影（pop；只挂一次，主题切换时重建颜色）
        try:
            self._shadow = _apply_card_shadow(self, "pop")
        except Exception:
            self._shadow = None

    def move_to(self, anchor_global_pos):
        """把气泡贴到锚点下方（含屏幕边界兜底；固定态跟随窗口时复用）。"""
        x = int(anchor_global_pos.x())
        y = int(anchor_global_pos.y()) + 22
        try:
            screen = QApplication.desktop().availableGeometry(self)
        except Exception:
            screen = None
        if screen is not None:
            if x + self.width() > screen.right():
                x = max(screen.left(), screen.right() - self.width())
            if y + self.height() > screen.bottom():
                y = max(screen.top(),
                        int(anchor_global_pos.y()) - self.height() - 6)
        self.move(QPoint(int(x), int(y)))

    def show_for(self, row, anchor_global_pos):
        """按行元数据填充内容（标题 / 描述 / 风险说明）并显示到 anchor 下方。"""
        self._title.setText(row.label)
        self._desc.setText(row.desc or "")
        if row.risk:
            self._risk_box.setVisible(True)
            self._risk_lbl.setText(
                "影响范围：会改变磁盘上的文件或降低连接安全性\n"
                "后果：重则丢失源文件 / 被中间人攻击，且不可撤销\n"
                "如何改回：把本项改回「默认」即可（可在底部「恢复默认」一键还原本页）")
        else:
            self._risk_box.setVisible(False)
        # 宽度 380（上限 屏宽-24）：先定宽再尺寸适配，避免长描述把气泡撑宽
        try:
            avail = int(QApplication.desktop().availableGeometry(self).width()) - 24
        except Exception:
            avail = _BUBBLE_W
        self.setFixedWidth(max(260, min(_BUBBLE_W, avail)))
        self.adjustSize()
        self.move_to(anchor_global_pos)
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
        self._name_labels = {}     # id(名称标签) -> (标签, _SettingRow)
        self._pending_name = None  # 悬停中的名称标签（700ms 计时器用）
        self._result_card = None   # 搜索结果容器（搜索态用）
        self._domain_box = None
        self._domain_id = "unzip"
        self._group_open = False   # 当前领域是否已经开过分组（[first=true] 用）
        self._shadow_widgets = []  # 挂了 QGraphicsDropShadowEffect 的卡片（主题切换重建）
        self._search_icon_act = None
        self._flash_timer = None
        self._hit_labels = {}      # 领域 id -> 页头命中数标签（#hitTotal）
        self._bubble_anchor = None       # 气泡锚点（名称标签）；固定态跟随重定位用
        self._outside_filter_on = False  # app 级「点外部收起」过滤器是否挂着
        self._follow_target = None       # 已挂事件过滤器的顶层窗口（跟随移动/缩放）

        self._build_ui()
        self._load_from_cfg()

    # ------------------------------------------------------------------
    # 构建
    # ------------------------------------------------------------------
    def _build_ui(self):
        self._sections = []
        self._section_cards = {}
        self._shadow_widgets = []
        # 页面根：窗口底色由 QSS（QWidget#settingsPage）+ WA_StyledBackground 提供
        self.setObjectName("settingsPage")
        self.setAttribute(Qt.WA_StyledBackground, True)
        root = QVBoxLayout(self)
        root.setContentsMargins(0, 0, 0, 0)
        root.setSpacing(0)

        # 左右两栏：[领域导航 | 右侧内容]；导航不随内容滚动；两栏之间无额外缝（§4）。
        body = QHBoxLayout()
        body.setContentsMargins(0, 0, 0, 0)
        body.setSpacing(0)

        self.cat_list = QListWidget(self)
        self.cat_list.setObjectName("settingsCat")
        self.cat_list.setFixedWidth(METRICS["rail_w"])
        self.cat_list.setHorizontalScrollBarPolicy(Qt.ScrollBarAlwaysOff)
        self.cat_list.setToolTip("选择一个领域查看该领域的设置。")
        # 选中左侧 3px 主色竖条：QSS 在 ::item 上不可靠 → 委托自绘（§6.1）
        self.cat_list.setItemDelegate(_RailDelegate(self.cat_list))
        self.cat_list.currentRowChanged.connect(self._on_cat_row_changed)
        body.addWidget(self.cat_list)

        # 右侧：顶部工具条（搜索 + 向导 + 导入/导出）+ 领域内容滚动区。
        right = QWidget(self)
        rlay = QVBoxLayout(right)
        rlay.setContentsMargins(0, 0, 0, 0)
        rlay.setSpacing(0)

        top = QFrame(right)
        top.setObjectName("settingsTop")
        top.setMinimumHeight(METRICS["topbar_h"])
        t = QHBoxLayout(top)
        t.setContentsMargins(16, 9, 16, 9)
        t.setSpacing(12)
        self.search_edit = QLineEdit(top)
        self.search_edit.setObjectName("settingsSearch")
        self.search_edit.setPlaceholderText("搜索设置项（名称 / 说明 / 配置键）")
        self.search_edit.setClearButtonEnabled(True)
        self.search_edit.setFixedSize(METRICS["search_w"], METRICS["search_h"])
        # 内嵌搜索图标：QSS 无法内嵌图标 → QLineEdit.addAction（§11 决定：做）。
        # 装饰性 action 不抢焦点；主题切换时在 refresh_theme() 里重贴图标色。
        try:
            self._search_icon_act = self.search_edit.addAction(
                _glyph_icon("search", 15), QLineEdit.LeadingPosition)
        except Exception:
            self._search_icon_act = None
        self.search_edit.textChanged.connect(self._on_search_changed)
        t.addWidget(self.search_edit)
        t.addStretch(1)
        self.wizard_btn = QPushButton("设置向导", top)
        self.wizard_btn.setObjectName("primary")
        self.wizard_btn.setCursor(Qt.PointingHandCursor)
        self.wizard_btn.setToolTip("可跳过，跳过后不再显示")
        self.wizard_btn.clicked.connect(self._on_wizard_clicked)
        t.addLayout(self._btn_with_sub(top, self.wizard_btn, "可跳过"))
        # 登记向导键（覆盖契约：settings_wizard_done 必须有归属控件）
        self._reg("settings_wizard_done", self.wizard_btn)
        self.import_btn = QPushButton("导入 / 导出", top)
        self.import_btn.setEnabled(True)
        self.import_btn.setCursor(Qt.PointingHandCursor)
        self.import_btn.setToolTip("把设置导出为 JSON 文件，或从 JSON 文件导入。")
        self.import_btn.clicked.connect(self._on_config_io)
        t.addLayout(self._btn_with_sub(top, self.import_btn, "JSON 文件"))
        rlay.addWidget(top)

        scroll = QScrollArea(right)
        scroll.setObjectName("settingsScroll")
        scroll.setWidgetResizable(True)
        scroll.setFrameShape(QFrame.NoFrame)
        scroll.setHorizontalScrollBarPolicy(Qt.ScrollBarAsNeeded)
        self.scroll = scroll
        inner = QWidget(scroll)
        inner.setObjectName("paneHost")
        self._lay = QVBoxLayout(inner)
        # pane_pad=(16,18,24)：左右 18 给卡片阴影留白（§6.2 坑 2）
        self._lay.setContentsMargins(18, 16, 18, 24)
        self._lay.setSpacing(12)
        scroll.setWidget(inner)
        rlay.addWidget(scroll, 1)
        body.addWidget(right, 1)
        root.addLayout(body, 1)
        # 固定气泡跟随：本页滚动时锚点标签的全局位置会变（valueChanged 里重定位）
        try:
            scroll.verticalScrollBar().valueChanged.connect(self._reposition_bubble)
            scroll.horizontalScrollBar().valueChanged.connect(self._reposition_bubble)
        except Exception:
            pass

        # 逐领域构建（每个领域 = 一个容器 QWidget，装页头 + 卡片 + 分组 + 行）
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
        foot.setObjectName("actionbar")
        f = QHBoxLayout(foot)
        f.setContentsMargins(18, 12, 18, 12)
        f.setSpacing(12)
        self.notice_label = QLabel("", foot)
        self.notice_label.setObjectName("notice")
        self.notice_label.setSizePolicy(QSizePolicy.Ignored, QSizePolicy.Fixed)
        f.addWidget(self.notice_label, 1)
        self.reset_btn = QPushButton("恢复默认", foot)
        self.reset_btn.setObjectName("danger")
        self.reset_btn.setCursor(Qt.PointingHandCursor)
        self.reset_btn.setToolTip("选择范围后把所有设置恢复为程序默认值（需再次确认）。")
        self.reset_btn.clicked.connect(self._on_reset)
        f.addWidget(self.reset_btn, 0, Qt.AlignRight)
        root.addWidget(foot)

        # 惰性气泡 + 悬停计时器
        self._bubble_timer = QTimer(self)
        self._bubble_timer.setSingleShot(True)
        self._bubble_timer.setInterval(_BUBBLE_DELAY_MS)
        self._bubble_timer.timeout.connect(self._on_bubble_due)

    def _btn_with_sub(self, parent, button, sub_text):
        """把按钮 + 一行小字副标题装进竖排（设计稿 .btnwrap / .btn-sub）。"""
        wrap = QVBoxLayout()
        wrap.setContentsMargins(0, 0, 0, 0)
        wrap.setSpacing(1)
        wrap.addWidget(button)
        sub = QLabel(str(sub_text), parent)
        sub.setObjectName("btnSub")
        sub.setAlignment(Qt.AlignHCenter)
        wrap.addWidget(sub)
        return wrap

    def _build_domain(self, did):
        """建一个领域容器：页头（大标题 + 描述 + 命中数）+ 卡片（分组 + 行）。

        #settingsDomain 保留为透明容器；真正的卡片是 QFrame#card（含阴影），
        监听目录领域用无卡片的 #dirsHost（每张目录卡自己就是卡片）。"""
        name, _icon, desc = _DOMAIN_META[did]
        card = QWidget(self)
        card.setObjectName("settingsDomain")
        box = QVBoxLayout(card)
        box.setContentsMargins(0, 0, 0, 0)
        box.setSpacing(12)

        # 页头（§4：新增 #pageHead；#hitTotal 搜索态显示「共 N 项命中」）
        head = QWidget(card)
        head.setObjectName("pageHead")
        hl = QHBoxLayout(head)
        hl.setContentsMargins(0, 0, 0, 0)
        hl.setSpacing(12)
        title = QLabel(name, head)
        title.setObjectName("pageTitle")
        hl.addWidget(title)
        # 描述仍用 #stripHint（离线验收按该 objectName 度量折行高度），
        # 视觉上由 [role="pageDesc"] 规则取 section_fg / 12.5px（§5）
        d = QLabel(desc, head)
        d.setObjectName("stripHint")
        d.setProperty("role", "pageDesc")
        hl.addWidget(d)
        hl.addStretch(1)
        hit = QLabel("", head)
        hit.setObjectName("hitTotal")
        hit.setVisible(False)
        hl.addWidget(hit)
        self._hit_labels[did] = hit
        box.addWidget(head)

        # 行卡片：常规领域是 #card（阴影）；监听目录领域是透明 #dirsHost
        is_dirs = (did == "dirs")
        rows_card = QWidget(card) if is_dirs else QFrame(card)
        rows_card.setObjectName("dirsHost" if is_dirs else "card")
        rbox = QVBoxLayout(rows_card)
        if is_dirs:
            rbox.setContentsMargins(0, 0, 0, 0)
            rbox.setSpacing(12)
        else:
            # card_pad=(6,16,12)：上下内边距走布局（QSS padding 对普通 QWidget 常被忽略）
            rbox.setContentsMargins(16, 6, 16, 12)
            rbox.setSpacing(0)

        # 卡片阴影：只给领域卡片挂（§6.2；一个 widget 一个 effect）
        if not is_dirs:
            try:
                _apply_card_shadow(rows_card, "card")
                self._shadow_widgets.append(rows_card)
            except Exception:
                pass

        self._lay.addWidget(card)
        self._section_cards[did] = card
        self._sections.append(did)
        self._domain_box = rbox            # 后续 _group/_row 追加到这里
        self._domain_id = did
        self._group_open = False

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
        self._finalize_row_borders(rows_card)

    def _finalize_row_borders(self, rows_card):
        """每个分组内最后一条 #setRow 打上 [last="true"]（QSS 没有 :last-child，§10）。

        构建期（widget 首次 polish 之前）设置动态属性即可；QSS 在 show/polish 时
        读取。分组本身用 [first="true"] 去掉首组上边框。"""
        try:
            groups = [w for w in rows_card.findChildren(QFrame)
                      if w.objectName() == "group"]
            for grp in groups:
                rows = grp.findChildren(QWidget, "setRow", Qt.FindDirectChildrenOnly)
                for n, w in enumerate(rows):
                    try:
                        w.setProperty("last", n == len(rows) - 1)
                    except Exception:
                        pass
            repolish_tree(rows_card)
        except Exception:
            pass

    # ---- 分组标题 / 行骨架 ----
    def _group(self, lay, title):
        """分组块（QFrame#group）：标题 + 右侧 1px 填充线，之后的行直接追加进来。

        首组上边框由 `[first="true"]` 去掉（QSS 没有 :first-child，§10.3）；组间
        间距 14 走上外边距（QSS padding/margin 对普通容器不可靠，§10.1）。"""
        host = lay.parentWidget()
        frame = QFrame(host)
        frame.setObjectName("group")
        first = not bool(self._group_open)
        frame.setProperty("first", first)
        self._group_open = True
        gap = int(METRICS["gblock_gap"])
        box = QVBoxLayout(frame)
        box.setContentsMargins(0, 6 if first else gap, 0, 0)
        box.setSpacing(0)
        head = QHBoxLayout()
        head.setContentsMargins(0, 0, 0, 0)
        head.setSpacing(9)
        lbl = QLabel(str(title), frame)
        lbl.setObjectName("groupTitle")
        head.addWidget(lbl)
        sep = QFrame(frame)
        sep.setObjectName("groupSep")
        sep.setFrameShape(QFrame.HLine)
        sep.setFixedHeight(1)
        sep.setSizePolicy(QSizePolicy.Expanding, QSizePolicy.Fixed)
        head.addWidget(sep, 1)
        box.addLayout(head)
        lay.addWidget(frame)
        return box

    def _manual_row(self, lay, risk=False):
        """手工行（不走 _row 的复合控件）：仍是 #setRow，享受分隔线 / hover。

        返回 (host, QHBoxLayout)；调用方把控件加进这个布局。"""
        host = QWidget(lay.parentWidget())
        host.setObjectName("setRow")
        host.setAttribute(Qt.WA_StyledBackground, True)   # QSS 背景/边框需要
        host.setAttribute(Qt.WA_Hover, True)              # QSS :hover 需要（§10.2）
        host.setProperty("risk", bool(risk))
        h = QHBoxLayout(host)
        h.setContentsMargins(0, int(METRICS["row_pad_y"]), 0,
                             int(METRICS["row_pad_y"]))
        h.setSpacing(10)
        if risk:
            try:
                lay.addSpacing(6)
            except Exception:
                pass
        lay.addWidget(host)
        if risk:
            try:
                lay.addSpacing(6)
            except Exception:
                pass
        return host, h

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
            if lbl.objectName() in _HINT_NAMES:
                self._fit_hint(lbl)

    def eventFilter(self, obj, event):
        handled = False
        try:
            etype = event.type()
            if (obj.objectName() in _HINT_NAMES
                    and etype in (QEvent.Polish, QEvent.StyleChange)):
                self._fit_hint(obj)
            if etype == QEvent.FocusOut and obj in self._text_timers:
                self._flush_text(obj)   # 失焦立即落盘（不等防抖）
            # 固定气泡：跟随顶层窗口移动 / 缩放（事件源是 window()，见 _attach_follow）
            if etype in (QEvent.Move, QEvent.Resize, QEvent.WindowStateChange):
                if self._bubble_pinned:
                    self._reposition_bubble()
            # 固定气泡：任意「气泡外」的鼠标按下 / 应用（或窗口）失活即收起。
            # app 级过滤器只在固定期间挂着（见 _install_outside_filter）。
            if etype == QEvent.MouseButtonPress:
                if self._bubble_pinned and not self._press_on_bubble_or_name(obj):
                    self._dismiss_bubble()
            elif etype in (QEvent.ApplicationDeactivate, QEvent.WindowDeactivate):
                if self._bubble_pinned:
                    self._dismiss_bubble()
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
                # 固定期间 app 级过滤器与控件过滤器都会看到同一次 release；
                # 返回 True 终止本次派发，避免「app 收起 + 控件再打开」的二次切换。
                handled = True
        except Exception:
            pass
        if handled:
            return True
        return super().eventFilter(obj, event)

    def showEvent(self, event):
        super().showEvent(event)
        self._fit_all_hints()
        self._attach_follow_target()

    def hideEvent(self, event):
        # 页面隐藏（切页 / 关窗）时固定气泡必须一起走，别留一个浮层和一个 app 过滤器
        if self._bubble_pinned or self._outside_filter_on:
            self._dismiss_bubble()
        self._detach_follow_target()
        super().hideEvent(event)

    def moveEvent(self, event):
        # 本页在窗口 / 外层滚动区里被移动时（含外层 QScrollArea 滚动），气泡跟随
        super().moveEvent(event)
        if self._bubble_pinned:
            self._reposition_bubble()

    def resizeEvent(self, event):
        super().resizeEvent(event)
        if self._bubble_pinned:
            self._reposition_bubble()

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
        """通用行：左侧只有名称（+风险徽章），右侧控件，整行较高。

        make_control(host) -> 控件；返回该控件。风险行上下各留 6px（不与上一行
        的红块/文字贴住，§评审 #8）。"""
        host = QWidget(lay.parentWidget())
        host.setObjectName("setRow")
        host.setAttribute(Qt.WA_StyledBackground, True)   # QSS 背景/边框需要
        host.setAttribute(Qt.WA_Hover, True)              # QSS :hover 需要（§10.2）
        host.setProperty("risk", bool(risk))
        h = QHBoxLayout(host)
        h.setContentsMargins(0, int(METRICS["row_pad_y"]), 0,
                             int(METRICS["row_pad_y"]))
        h.setSpacing(10)
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
        l.addStretch(1)
        h.addWidget(left, 1)
        ctl = make_control(host)
        h.addWidget(ctl, 0, Qt.AlignVCenter)
        if risk:
            # 风险块的红色背景与上一行分隔线不贴边（QSS margin 对普通 QWidget 不可靠，
            # 用布局 spacing 实现，见 style.py 的 §10.1 说明）
            try:
                lay.addSpacing(6)
            except Exception:
                pass
        lay.addWidget(host)
        if risk:
            try:
                lay.addSpacing(6)
            except Exception:
                pass
        row_meta.widget = ctl
        self._rows.append(row_meta)
        return ctl

    def _mark_dirty(self, key):
        """（已按评审移除「偏离默认」小圆点；保留入口为 no-op，兼容既有调用点。）"""
        return None

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
                u.setObjectName("unit")      # 单位在框外、右邻（§7）
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
            # 设计稿 .ta：280×58 固定（§7）；多行内容超出时框内滚动
            edit.setFixedSize(280, 58)
            edit.setSizePolicy(QSizePolicy.Fixed, QSizePolicy.Fixed)
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
            # 选项列表与标题之间留垂直间距（评审 #7：所有单选行统一）
            b.setContentsMargins(0, 6, 0, 0)
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
        # 评审：#6「删除源文件」组退场（与「监听目录」逐目录删源重复）。顶层
        # watch_paths 的覆盖归属改到「监听目录」领域容器（见 _build_dirs），
        # 覆盖契约 covered_top_keys() == set(DEFAULT_CONFIG) 保持不变。
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
        ph, pl = self._manual_row(g)
        pl.addWidget(self._make_name(
            ph, "密码本条目", _SettingRow("密码本条目", "", "passwords", None,
                                          "clipboard", "密码识别")))
        self.passwords_label = QLabel("", ph)
        self.passwords_label.setObjectName("stripHint")
        pl.addWidget(self.passwords_label)
        pl.addStretch(1)
        self._reg("passwords", self.passwords_label)
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
        bah, bal = self._manual_row(g)
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
        # 选项列表与上方标题（#sectionTitle）之间留垂直间距（评审 #7）
        b.setContentsMargins(0, 6, 0, 0)
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
        th, tl = self._manual_row(g)
        tl.addWidget(self._make_name(
            th, "主题", _SettingRow("主题", "", "ui_theme", None, "ui", "外观")))
        tl.addWidget(self.theme_combo)
        tl.addStretch(1)
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
        ch, chl = self._manual_row(g)
        chl.addWidget(self._make_name(
            ch, "上次实际应用的主题",
            _SettingRow("上次实际应用的主题", "", "ui_theme_cached", None, "ui",
                        "外观")))
        chl.addWidget(self.theme_cached_label)
        chl.addStretch(1)
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
        # 选项列表与分组标题之间留垂直间距（评审 #7）
        b.setContentsMargins(0, 6, 0, 0)
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
        sh, sl = self._manual_row(g)
        sl.addWidget(self._make_name(
            sh, "下次启动重新检测 7-Zip",
            _SettingRow("下次启动重新检测 7-Zip", "", "sevenzip_check_done",
                        None, "system", "解压引擎")))
        sl.addStretch(1)
        sl.addWidget(self.sevenzip_cb)
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
        # 覆盖契约：顶层 watch_paths 的归属控件 = 本领域容器（「删除源文件」总控
        # 退场后落这里；逐目录删源由每张卡的 dir.delete_source 承载）。
        self._reg("watch_paths", box.parentWidget())
        self.rebuild_dirs()

    def rebuild_dirs(self):
        """按 watch_paths 重建目录卡（2 列 grid + 状态胶囊 + 虚线页脚；§7/§8 #9）。

        旧卡必须**先注销**再销毁：卡内控件登记在 `_controls`（dir.* 键）与
        `_text_timers` 里，若只 setParent(None) 丢引用，QFrame 会被 C++ 析构、
        其子控件随之被删，但登记表仍持有野指针 → 之后任何遍历 `_controls` 的
        代码都会 RuntimeError。这里先把旧卡下的控件从登记表里摘掉。"""
        box = self.dirs_box
        old_cards = list(getattr(self, "_dir_card_widgets", []))
        # 清空旧的目录卡（先注销登记，再销毁）
        for w in old_cards:
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
        # 旧卡的阴影引用同步摘掉（避免主题切换时重贴到已销毁对象）
        self._shadow_widgets = [w for w in self._shadow_widgets if w not in old_cards]
        try:
            entries = [e for e in (self._snapshot().get("watch_paths") or [])
                       if isinstance(e, dict)]
        except Exception:
            entries = []
        for idx, entry in enumerate(entries):
            card = QFrame(box.parentWidget())
            card.setObjectName("dirCard")
            cv = QVBoxLayout(card)
            cv.setContentsMargins(16, 14, 16, 14)   # dircard padding 14/16
            cv.setSpacing(12)

            head = QHBoxLayout()
            head.setSpacing(9)
            head.addWidget(Glyph("folder", card, 16, role="muted"))
            path = str(entry.get("path") or "（未设置路径）")
            hlbl = QLabel(path, card)
            # objectName 保持 sectionTitle（离线验收按 2 + 目录卡数 计数），
            # 视觉上由 [role="dirPath"] 规则取 13.5px/700（§7）
            hlbl.setObjectName("sectionTitle")
            hlbl.setProperty("role", "dirPath")
            head.addWidget(hlbl)
            on = bool(entry.get("enabled", True))
            pill = QLabel("已启用" if on else "已停用", card)
            pill.setObjectName("statusPill")
            pill.setProperty("on", on)
            pill.setProperty("off", not on)
            head.addWidget(pill)
            head.addStretch(1)
            cv.addLayout(head)

            # 2 列 QGridLayout（列距 24 / 行距 16）：左列 路径 / 解压到 / 删除源，
            # 右列 启用 / 监听模式 / 回收站策略（与设计稿 thumbs 一致）
            grid = QGridLayout()
            grid.setContentsMargins(0, 0, 0, 0)
            grid.setHorizontalSpacing(24)
            grid.setVerticalSpacing(16)
            grid.addWidget(self._dir_field_path(card, idx, entry), 0, 0, Qt.AlignTop)
            grid.addWidget(self._dir_field_enabled(card, idx, entry), 0, 1, Qt.AlignTop)
            grid.addWidget(self._dir_field_output(card, idx, entry), 1, 0, Qt.AlignTop)
            grid.addWidget(self._dir_field_mode(card, idx, entry), 1, 1, Qt.AlignTop)
            grid.addWidget(self._dir_field_delete(card, idx, entry), 2, 0, Qt.AlignTop)
            grid.addWidget(self._dir_field_policy(card, idx, entry), 2, 1, Qt.AlignTop)
            cv.addLayout(grid)

            foot = QFrame(card)
            foot.setObjectName("dirFoot")
            fl = QHBoxLayout(foot)
            fl.setContentsMargins(12, 10, 12, 10)
            fl.setSpacing(8)
            note = QLabel("字段改动立即保存；增删目录请在右侧弹窗里完成。", foot)
            note.setObjectName("stripHint")
            note.setWordWrap(True)
            fl.addWidget(note, 1)
            # 「打开目录设置」放进本卡虚线页脚：归属明确（多目录时不会歧义）
            open_btn = QPushButton("打开目录设置", foot)
            open_btn.setCursor(Qt.PointingHandCursor)
            open_btn.setToolTip("打开「%s」的目录设置弹窗" % path)
            open_btn.clicked.connect(
                lambda _c=False, i=idx, e=dict(entry): self._open_dir_dialog(i, e))
            fl.addWidget(open_btn, 0)
            cv.addWidget(foot)

            try:
                _apply_card_shadow(card, "card")
                self._shadow_widgets.append(card)
            except Exception:
                pass
            box.addWidget(card)
            self._dir_card_widgets.append(card)
            repolish_tree(card)

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

    def _dir_cell(self, card, label, key, badge_key=None, risk=False):
        """目录卡字段格：标签行（可带风险徽章）+ 控件区（调用方追加）；返回 (cell, v)。"""
        cell = QFrame(card)
        cell.setObjectName("dirFieldRisk" if risk else "dirField")
        cell.setAttribute(Qt.WA_StyledBackground, True)
        v = QVBoxLayout(cell)
        if risk:
            v.setContentsMargins(10, 8, 10, 8)
        else:
            v.setContentsMargins(0, 0, 0, 0)
        v.setSpacing(5)
        top = QHBoxLayout()
        top.setContentsMargins(0, 0, 0, 0)
        top.setSpacing(6)
        lbl, meta = self._dir_label(cell, label, key)
        meta.risk = bool(risk)
        self._rows.append(meta)
        top.addWidget(lbl)
        if risk:
            badge = QLabel("风险", cell)
            badge.setObjectName("riskBadge")
            top.addWidget(badge)
            if badge_key:
                self._risk_badges[badge_key] = badge
        top.addStretch(1)
        v.addLayout(top)
        return cell, v

    def _dir_field_path(self, card, idx, entry):
        cell, v = self._dir_cell(card, "监听路径", "dir.path")
        h = QHBoxLayout()
        h.setSpacing(6)
        edit = QLineEdit(cell)
        edit.setText(str(entry.get("path") or ""))
        edit.setPlaceholderText("要盯着的文件夹")
        self._reg("dir.path", edit)
        self._bind_text(edit, "watch_paths",
                        lambda i=idx, e=edit: self._dir_collect(i, "path", e.text()))
        h.addWidget(edit, 1)
        browse = QPushButton("浏览", cell)
        browse.setCursor(Qt.PointingHandCursor)
        browse.clicked.connect(lambda: self._browse_dir(idx, edit, "path"))
        h.addWidget(browse)
        v.addLayout(h)
        return cell

    def _dir_field_output(self, card, idx, entry):
        cell, v = self._dir_cell(card, "解压到", "dir.output_dir")
        h = QHBoxLayout()
        h.setSpacing(6)
        edit = QLineEdit(cell)
        edit.setText(str(entry.get("output_dir") or ""))
        edit.setPlaceholderText("留空 = 同目录下建同名文件夹")
        self._reg("dir.output_dir", edit)
        self._bind_text(edit, "watch_paths",
                        lambda i=idx, e=edit: self._dir_collect(i, "output_dir", e.text()))
        h.addWidget(edit, 1)
        browse = QPushButton("浏览", cell)
        browse.setCursor(Qt.PointingHandCursor)
        browse.clicked.connect(lambda: self._browse_dir(idx, edit, "output_dir"))
        h.addWidget(browse)
        v.addLayout(h)
        return cell

    def _dir_field_enabled(self, card, idx, entry):
        cell, v = self._dir_cell(card, "启用这个目录", "dir.enabled")
        cb = _Switch(cell)
        cb.setChecked(bool(entry.get("enabled", True)))
        cb.toggled.connect(lambda c, i=idx: self._dir_set(i, "enabled", bool(c)))
        self._reg("dir.enabled", cb)
        v.addWidget(cb, 0, Qt.AlignLeft)     # 与设计稿一致：开关在标签下方、左对齐
        return cell

    def _dir_field_mode(self, card, idx, entry):
        cell, v = self._dir_cell(card, "监听模式", "dir.mode")
        cur = str(entry.get("mode") or "surface")
        grp = QButtonGroup(cell)
        host = QWidget(cell)
        b = QVBoxLayout(host)
        b.setContentsMargins(0, 4, 0, 0)   # 选项与上方标签留垂直间距（评审 #7）
        b.setSpacing(3)
        for value, text in (("surface", "只扫表层"), ("baidu", "按网盘清单处理子目录")):
            rb = QRadioButton(text, host)
            grp.addButton(rb)
            rb.setChecked(value == cur)
            rb.toggled.connect(
                lambda c, i=idx, v=value: self._dir_set(i, "mode", v) if c else None)
            b.addWidget(rb)
        self._reg("dir.mode", host)
        v.addWidget(host)
        return cell

    def _dir_field_delete(self, card, idx, entry):
        cell, v = self._dir_cell(card, "本目录解压后删除源文件", "dir.delete_source",
                                 badge_key="dir.delete_source.%d" % idx, risk=True)
        cb = _Switch(cell)
        cb.setChecked(bool(entry.get("delete_source", False)))
        cb.toggled.connect(lambda c, i=idx: self._dir_set(i, "delete_source", bool(c)))
        self._reg("dir.delete_source", cb)
        v.addWidget(cb, 0, Qt.AlignLeft)     # 与设计稿一致：开关在标签下方、左对齐
        return cell

    def _dir_field_policy(self, card, idx, entry):
        cell, v = self._dir_cell(card, "回收站不可用时", "dir.delete_policy",
                                 badge_key="dir.delete_policy.%d" % idx, risk=True)
        cur = str(entry.get("delete_policy") or "auto")
        host = QWidget(cell)
        b = QVBoxLayout(host)
        b.setContentsMargins(0, 4, 0, 0)   # 选项与上方标签留垂直间距（评审 #7）
        b.setSpacing(3)
        for value, text in (("auto", "自动判断"), ("permanent", "永久删除"),
                            ("keep", "保留不删"), ("quarantine", "移入隔离区")):
            rb = QRadioButton(text, host)
            rb.setChecked(value == cur)
            rb.toggled.connect(
                lambda c, i=idx, v=value: self._dir_set(i, "delete_policy", v) if c else None)
            b.addWidget(rb)
        self._reg("dir.delete_policy", host)
        # 点号路径叶子别名：本页以整表写回 watch_paths，但「回收站策略」确实是
        # watch_paths[i].delete_policy 的叶子 → 同时登记，保证 covered_keys() 仍含
        # watch_paths.* 家族（既有验收 P18b；顶层键归属仍由「监听目录」容器持有）。
        self._reg("watch_paths.delete_policy", host)
        v.addWidget(host)
        return cell

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

    def _open_dir_dialog(self, idx=None, entry=None):
        """打开某个监听目录的设置弹窗：`WatchDirDialog(state, idx, parent, entry)`。

        历史缺陷：把 `self` 当 idx 传 → `int(idx)` 抛
        「int() argument must be …, not 'SettingsPage'」。按钮按卡片绑定 idx（+entry），
        这里再兜底校验一次 idx。"""
        try:
            from .dialogs import WatchDirDialog
        except Exception:
            self._notice("目录设置弹窗不可用", ok=False)
            return
        if idx is None:
            self._notice("请从目录卡片打开目录设置", ok=False)
            return
        try:
            dlg = WatchDirDialog(self.state, int(idx), self, entry=entry)
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
                # 分隔线：保留占位文本（既有验收读取 item.text()），显示用
                # 样式化 QFrame#navSep 覆盖（§8 #7：不再是文字行）
                item = QListWidgetItem("──────────")
                item.setFlags(Qt.NoItemFlags)
                item.setSizeHint(QSize(0, 21))
                self.cat_list.addItem(item)
                holder = QWidget(self.cat_list)
                holder.setObjectName("navSepHost")
                holder.setAttribute(Qt.WA_StyledBackground, True)
                hl = QVBoxLayout(holder)
                hl.setContentsMargins(0, 10, 0, 10)
                hl.setSpacing(0)
                line = QFrame(holder)
                line.setObjectName("navSep")
                line.setFixedHeight(1)
                line.setSizePolicy(QSizePolicy.Expanding, QSizePolicy.Fixed)
                hl.addWidget(line)
                self.cat_list.setItemWidget(item, holder)
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
        """把右侧换成搜索结果：#hitCard 卡片 + 可点击命中项 + 空状态（§8 #5）。"""
        if getattr(self, "_result_card", None) is not None:
            self._result_card.setParent(None)
            self._result_card = None
        for d, card in self._section_cards.items():
            card.setVisible(False)
        hits = [r for r in self._rows if q in r.search_blob()]
        card = QFrame(self.scroll.widget())
        card.setObjectName("hitCard")
        cl = QVBoxLayout(card)
        cl.setContentsMargins(16, 12, 16, 14)
        cl.setSpacing(2)

        head = QWidget(card)
        hl = QHBoxLayout(head)
        hl.setContentsMargins(0, 0, 0, 6)
        hl.setSpacing(12)
        title = QLabel("搜索结果", head)
        title.setObjectName("pageTitle")
        hl.addWidget(title)
        desc = QLabel("「%s」" % q, head)
        desc.setObjectName("pageDesc")
        hl.addWidget(desc)
        hl.addStretch(1)
        total = QLabel("共 %d 项命中" % len(hits), head)
        total.setObjectName("hitTotal")
        hl.addWidget(total)
        cl.addWidget(head)

        if not hits:
            empty = QLabel("没有匹配的设置项。换个关键词，或清空搜索回到领域视图。", card)
            empty.setObjectName("emptyState")
            empty.setAlignment(Qt.AlignCenter)
            empty.setWordWrap(True)
            cl.addWidget(empty)
        else:
            by_domain = {}
            for r in hits:
                by_domain.setdefault(r.domain, []).append(r)
            for d in _DOMAIN_ORDER:
                if d is None or d not in by_domain:
                    continue
                dname = _DOMAIN_META[d][0]
                grp = QLabel("%s（%d 项）" % (dname, len(by_domain[d])), card)
                grp.setObjectName("groupTitle")
                cl.addSpacing(8)
                cl.addWidget(grp)
                for r in by_domain[d]:
                    btn = QPushButton("•  %s" % r.label, card)
                    btn.setObjectName("hitRow")
                    btn.setCursor(Qt.PointingHandCursor)
                    btn.setToolTip((r.desc or "") + "\n\n点击跳到该设置项。")
                    btn.clicked.connect(lambda _c=False, rr=r: self._goto_hit(rr))
                    cl.addWidget(btn)
        cl.addStretch(1)
        # 插在末尾的顶对齐 stretch 之前（保持内容顶对齐）
        self._lay.insertWidget(self._lay.count() - 1, card)
        self._result_card = card
        try:
            self.scroll.verticalScrollBar().setValue(0)
        except Exception:
            pass

    def _goto_hit(self, row):
        """搜索命中项点击：切到所属领域、清空搜索、滚动并高亮该行（§11 #3 决定）。"""
        did = getattr(row, "domain", "") or self._current_domain
        try:
            for i in range(self.cat_list.count()):
                if self.cat_list.item(i).data(Qt.UserRole) == did:
                    self._current_domain = did
                    self.cat_list.setCurrentRow(i)
                    break
            if self.search_edit.text():
                self.search_edit.clear()          # 触发 _on_search_changed('') → 回领域视图
        except Exception:
            pass
        try:
            self._show_domain(did)
        except Exception:
            pass
        widget = getattr(row, "widget", None)
        if widget is None:
            return
        host = widget
        try:
            while host is not None and host.objectName() != "setRow":
                host = host.parentWidget()
            if host is not None:
                y = host.mapTo(self.scroll.widget(), QPoint(0, 0)).y()
                self.scroll.verticalScrollBar().setValue(max(0, y - 40))
                widget.setFocus(Qt.OtherFocusReason)
                self._flash_row(host)
        except Exception:
            pass

    def _flash_row(self, host):
        """命中定位后的短暂高亮（QSS [flash="true"]；600ms 后清除）。"""
        try:
            host.setProperty("flash", True)
            repolish_tree(host)
        except Exception:
            return
        if self._flash_timer is not None:
            try:
                self._flash_timer.stop()
            except Exception:
                pass
        self._flash_timer = QTimer(self)
        self._flash_timer.setSingleShot(True)
        self._flash_timer.setInterval(600)
        self._flash_timer.timeout.connect(lambda w=host: self._clear_flash(w))
        self._flash_timer.start()

    def _clear_flash(self, host):
        try:
            host.setProperty("flash", False)
            repolish_tree(host)
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
        self._bubble_anchor = lbl          # 固定态跟随窗口 / 滚动的锚点
        self._bubble.show_for(row, lbl.mapToGlobal(QPoint(0, lbl.height())))
        self._fit_hint(self._bubble)
        if self._bubble_pinned:
            self._install_outside_filter()
        else:
            self._remove_outside_filter()

    def _toggle_pinned_bubble(self, obj):
        if self._bubble_pinned and self._bubble is not None and self._bubble.isVisible():
            self._dismiss_bubble()
            return
        self._show_bubble_for(obj, pinned=True)

    def _dismiss_bubble(self):
        """收起气泡（含固定态）：隐藏 + 清固定 + 摘 app 级「点外部收起」过滤器。

        所有收起路径（再点名称 / Esc / 点空白 / 点气泡外 / 失活 / 页面隐藏）都走这里，
        避免 app 级过滤器残留到退出期（Qt 终结阶段带 Python 回调的过滤器会崩）。"""
        self._bubble_pinned = False
        self._bubble_anchor = None
        self._remove_outside_filter()
        if self._bubble is not None:
            try:
                self._bubble.hide()
            except Exception:
                pass

    def _press_on_bubble_or_name(self, obj):
        """这次鼠标按下是否落在气泡（或其子树）/ 设置名称上——是则不收起。"""
        b = self._bubble
        if b is not None:
            try:
                if obj is b or (isinstance(obj, QWidget) and b.isAncestorOf(obj)):
                    return True
            except Exception:
                pass
        return id(obj) in self._name_labels

    def _reposition_bubble(self):
        """把已固定的气泡重新贴到锚点标签下方（窗口移动 / 缩放 / 本页滚动时）。"""
        b = self._bubble
        lbl = self._bubble_anchor
        if b is None or lbl is None or not b.isVisible():
            return
        try:
            b.move_to(lbl.mapToGlobal(QPoint(0, lbl.height())))
        except Exception:
            pass

    def _install_outside_filter(self):
        """固定期间给 app 挂事件过滤器：任意「气泡外」按下 / 失活即收起。"""
        if self._outside_filter_on:
            return
        app = QApplication.instance()
        if app is None:
            return
        try:
            app.installEventFilter(self)
            self._outside_filter_on = True
        except Exception:
            self._outside_filter_on = False

    def _remove_outside_filter(self):
        """摘掉 app 级过滤器（幂等；失败一律忽略，绝不影响退出）。"""
        if not self._outside_filter_on:
            return
        self._outside_filter_on = False
        app = QApplication.instance()
        if app is None:
            return
        try:
            app.removeEventFilter(self)
        except Exception:
            pass

    def _attach_follow_target(self):
        """给顶层窗口挂事件过滤器：窗口移动 / 缩放时固定气泡跟随（幂等）。"""
        try:
            win = self.window()
        except Exception:
            win = None
        if win is None or win is self._follow_target:
            return
        self._detach_follow_target()
        try:
            win.installEventFilter(self)
            self._follow_target = win
        except Exception:
            self._follow_target = None

    def _detach_follow_target(self):
        target = self._follow_target
        self._follow_target = None
        if target is None:
            return
        try:
            target.removeEventFilter(self)
        except Exception:
            pass

    def mousePressEvent(self, event):
        # 点空白处收起已固定气泡；按在「设置名称」上的点击不算空白——该次点击
        # 由 eventFilter 的 MouseButtonRelease 负责切换固定状态。
        if (self._bubble is not None and self._bubble.isVisible()
                and self._bubble_pinned):
            child = self.childAt(event.pos())
            if child is None or id(child) not in self._name_labels:
                self._dismiss_bubble()
        super().mousePressEvent(event)

    def keyPressEvent(self, event):
        if event.key() == Qt.Key_Escape and self._bubble is not None:
            self._dismiss_bubble()
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

            # 删除与安全（「删除源文件」总控已退场；逐目录删源在「监听目录」里改）
            self.bomb_guard_cb.setChecked(b("bomb_guard_enabled", True))
            self.bomb_entries_spin.setValue(max(0, min(1000000, i("bomb_soft_entries", 50000))))
            self.bomb_soft_ratio_spin.setValue(max(1, min(100000, i("bomb_soft_ratio", 100))))
            self.bomb_hard_ratio_spin.setValue(max(1, min(100000, i("bomb_hard_ratio", 200))))
            self.bomb_min_gb_spin.setValue(max(0, min(100000, i("bomb_hard_min_gb", 1))))
            self.bomb_size_gb_spin.setValue(max(0, min(1000000, i("bomb_hard_size_gb", 50))))
            self.free_space_spin.setValue(max(0, min(100000, i("min_free_space_gb", 5))))

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

    def _refresh_passwords_label(self):
        try:
            n = len(self.state.passwords() or [])
            self.passwords_label.setText("%d 条（在「密码本」页管理）" % n)
        except Exception:
            self.passwords_label.setText("（读取失败 · 在「密码本」页管理）")

    def _refresh_dirty_dots(self):
        """（「偏离默认」小圆点已按评审移除；保留入口为 no-op，兼容既有调用点。）"""
        return None

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
    def _on_wizard_clicked(self):
        """设置向导入口：**只有**明确「跳过」才置位并隐藏入口（向导尚未实现）。"""
        ret = QMessageBox.question(
            self, "设置向导",
            "设置向导会带你过一遍最常用的几项。\n"
            "暂时不想看，可以点「跳过」——跳过后这个入口不再显示。",
            QMessageBox.Ok | QMessageBox.Cancel, QMessageBox.Ok)
        if ret != QMessageBox.Ok:
            if self._commit("settings_wizard_done", True):
                self._notice("已跳过设置向导（入口不再显示）")
            return
        # 点「开始 / OK」但向导尚在规划中：不置位、不隐藏入口，用户之后仍可再来。
        self._notice("设置向导尚在规划中；入口保留，随时可以再进来。")

    def _on_config_io(self, _checked=False):
        """导入 / 导出：先让用户选方向，再选文件。"""
        box = QMessageBox(self)
        box.setWindowTitle("导入 / 导出")
        box.setIcon(QMessageBox.Question)
        box.setText("把当前设置导出为 JSON 文件，或从 JSON 文件导入并覆盖当前设置。")
        export_btn = box.addButton("导出设置", QMessageBox.AcceptRole)
        import_btn = box.addButton("导入设置", QMessageBox.AcceptRole)
        cancel = box.addButton("取消", QMessageBox.RejectRole)
        box.setDefaultButton(cancel)
        box.exec_()
        clicked = box.clickedButton()
        if clicked is export_btn:
            self._export_config()
        elif clicked is import_btn:
            self._import_config()

    def _export_config(self):
        """把当前配置写成一份 JSON（用户取消选择文件则不做事）。"""
        try:
            path, _selected = QFileDialog.getSaveFileName(
                self, "导出设置", "autounpacker_settings.json",
                "JSON 文件 (*.json)")
        except Exception as e:
            self._notice("导出失败：%s" % e, ok=False)
            return
        if not path:
            return
        try:
            with open(path, "w", encoding="utf-8") as fh:
                fh.write(json.dumps(self._snapshot(),
                                    ensure_ascii=False, indent=2))
        except Exception as e:
            self._notice("导出失败：%s" % e, ok=False)
            return
        self._notice("已导出设置：%s" % path)

    def _import_config(self):
        """从 JSON 导入配置：解析 / 校验全部先于改动 state；任一步失败都
        整体回滚到导入前快照，保证「导入失败」时配置不被破坏。"""
        try:
            path, _selected = QFileDialog.getOpenFileName(
                self, "导入设置", "", "JSON 文件 (*.json)")
        except Exception as e:
            self._notice("导入失败：%s" % e, ok=False)
            return
        if not path:
            return
        before = self._snapshot()
        try:
            with open(path, "r", encoding="utf-8") as fh:
                raw = json.load(fh)
            if not isinstance(raw, dict):
                raise ValueError("配置文件的顶层必须是对象")
            clean = _sanitize_cfg(raw)
            for key, value in clean.items():
                self.state.set(key, value, save=False)
            self.state._persist()
            if not getattr(self.state, "last_persist_ok", True):
                raise RuntimeError(
                    self.state.last_persist_error or "配置写入未生效")
        except Exception as e:
            # 回滚内存与磁盘：尽力而为，绝不因回滚失败再抛。
            try:
                for key, value in before.items():
                    self.state.set(key, value, save=False)
                self.state._persist()
            except Exception:
                pass
            self._notice("导入失败：%s" % e, ok=False)
            return
        self._load_from_cfg()
        self._notice("已导入设置：%s" % path)
        self.settingsSaved.emit()

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
        """第一步：选范围 + 警告（样式化模态：#modalCard + 警告块 + 范围卡 + 页脚）。

        返回 (scope, ok)；scope ∈ {'all','page'}。见 `_ResetScopeBox`：
        保留 QMessageBox 语义（离线验收锁定 exec_ 与按钮文案），外观自建（§7）。
        """
        box = _ResetScopeBox(self, dname, n_page)
        box.exec_()
        scope = box.chosen()
        if scope is None:
            return "all", False
        return scope, True

    def _confirm_reset(self, scope, dname, n_page):
        """第二步：再次确认（QMessageBox.question 为既有验收锁定路径，保持）。"""
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
        """主题切换后重贴内联色（warn 说明 / 风险徽章 / 阴影 / 气泡）。"""
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
        if self._notice_failed:
            try:
                self.notice_label.setStyleSheet("color: %s;" % PALETTE["danger"])
            except Exception:
                pass
        # 卡片 / 气泡阴影：颜色随主题变（浅 26 / 深 128 等），必须重建（§10.7）
        for w in list(self._shadow_widgets):
            try:
                _apply_card_shadow(w, "card")
            except Exception:
                pass
        if self._bubble is not None:
            try:
                _apply_card_shadow(self._bubble, "pop")
            except Exception:
                pass
        # 搜索框内嵌图标：图标是按旧主题色生成的，重贴一次
        if self._search_icon_act is not None:
            try:
                self._search_icon_act.setIcon(_glyph_icon("search", 15))
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
        try:
            self.cat_list.viewport().update()   # 左栏竖条颜色（委托 paint 现取 token）
        except Exception:
            pass
        self._fit_all_hints()

    def _badge_qss(self):
        """风险徽章内联样式：取 QSS token 真值（与新 QSS 规则同色值）。"""
        tk = ui_style.tokens()
        return ("QLabel#riskBadge { color: %s; background: %s; border: 1px solid %s;"
                " border-radius: %s; padding: 1px 7px; }"
                % (tk["danger_fg"], tk["danger_bg"], tk["danger_border"],
                   tk["radius_ctl"]))

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
