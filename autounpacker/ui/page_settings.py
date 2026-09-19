# -*- coding: utf-8 -*-
"""设置页（正式页面）：把全部配置项做成页内分区表单，替代过渡占位页。

职责：
- SettingsPage：9 个分区覆盖 config.DEFAULT_CONFIG 的全部键；每个控件右侧
  都带「立即生效 / 需重启」标签（常量 TAG_LIVE / TAG_RESTART），绝不虚标；
- 「保存」：逐键走 AppState.set()（config.save_config 原子写）后再回读校验，
  成功/失败如实回报（save_config 自身吞异常，不回读就会假报成功）；
- 「恢复默认」：二次确认后写回 config._sanitize_cfg(DEFAULT_CONFIG)（与
  load_config 的口径一致），再回填控件；
- 目录（分区索引）：左侧 QListWidget#settingsCat 列在滚动区之外，点击滚动
  页内 self.scroll 到对应卡片；滚动卡片时反向同步选中项（best-effort）；
- 监听目录不再在本页增删改（页头胶囊条 → WatchDirDialog 已覆盖），本页只把
  watch_paths 逐条原样写回，绝不丢字段；
- #stripHint 描述行：11px CJK 墨迹几乎顶满 em 框，QLabel 折行高度按
  fontMetrics().height() 算、绘制按 lineSpacing() 排，默认上下各裁 1px；
  由 QSS padding + polish 后的 _fit_hint() 兜底（见 style.py 注释）；
- 主题：走既有 ui_style.resolve_theme + apply_theme + ui_theme_cached + 回调
  路径，绝不手写 QSS、绝不新增主题 token。

关键入口：SettingsPage；常量 TAG_LIVE / TAG_RESTART
依赖：PyQt5、config（DEFAULT_CONFIG/_sanitize_cfg/load_config）、style（PALETTE）、
      widgets（Glyph/HotkeyEdit）
注意：本页是「表单 + 保存」模型：所有控件只在点「保存」时统一写入。
注意：集成（把本页装进主窗口标签壳）由集成步骤完成；本模块只提供页面与信号：
      settingsSaved / settingsReset / watchPathsChanged / hotkeyChanged(str) /
      themeChanged(str) / notice(str)。
注意：本模块不联网、不起线程、不重启应用；所有异常都转成页内 notice 提示。
注意（allow: SIZE_OK）：按任务要求「单文件承载全部设置表单、不得新建兄弟模块」，
      9 个分区 + 全部顶层键覆盖 + 全部私有助手必然内聚于此；不拆分是为了让
      「键 -> 控件 -> 生效标签」的覆盖契约在一个文件里可直接审计。
"""
import json

from PyQt5.QtCore import QEvent, Qt, pyqtSignal
from PyQt5.QtWidgets import (QApplication, QButtonGroup, QCheckBox, QComboBox,
                             QFileDialog, QFrame, QHBoxLayout, QLabel,
                             QLineEdit, QListWidget,
                             QMessageBox, QPlainTextEdit, QPushButton,
                             QRadioButton, QScrollArea, QSizePolicy, QSpinBox,
                             QVBoxLayout, QWidget)

from ..config import DEFAULT_CONFIG, _sanitize_cfg
from ..config import load_config
from . import style as ui_style
from .style import PALETTE
from .widgets import Glyph, HotkeyEdit

# 生效方式标签：每个控件都必须挂其中之一（测试会逐键断言）
TAG_LIVE = "立即生效"
TAG_RESTART = "需重启"
_RESTART_TIP = "该设置需要重启程序后才会完全生效（保存会立即写入配置，下次启动时应用）。"

# 主题偏好与显示名（与 dialogs.SettingsDialog 的选项一一对应）
_THEME_ITEMS = (("跟随系统", "auto"), ("浅色", "fluent"), ("深色", "devtool"))
_THEME_NAMES = {"auto": "跟随系统", "fluent": "浅色（Fluent）",
                "devtool": "深色（DevTool）"}

# 二维码打开网页后的剪贴板联动（group id 必须与 state 的取值顺序一致）
_CLIP_ACTIONS = ((0, "none"), (1, "code"), (2, "url"))

# 「保存」回读校验时跳过的复合键（列表/字典无法逐值比对，另有专门断言）
_VERIFY_SKIP = ("watch_paths", "url_trust", "url_redirect_rules")


def _deepcopy(value):
    """深拷贝配置值（配置里只有 JSON 可序列化类型，json 往返最省事）。"""
    return json.loads(json.dumps(value))


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
    """[{'from','to'}] -> 编辑框文本（保存与回填的互逆表示）。"""
    lines = []
    for r in rules or []:
        if isinstance(r, dict) and r.get("from") and r.get("to"):
            lines.append("%s -> %s" % (r["from"], r["to"]))
    return "\n".join(lines)


class SettingsPage(QWidget):
    """设置页：分区表单 + 保存 / 恢复默认；覆盖 DEFAULT_CONFIG 全部键。

    宿主接入（集成步骤）：`SettingsPage(state, hub, parent,
    on_hotkey_change=..., on_theme_change=...)`——两个回调与旧 SettingsDialog
    同名同义（分别是「重新注册全局快捷键」「主题已切换」），可原样传入
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
        self._tags = {}            # 配置键 -> TAG_LIVE / TAG_RESTART
        self._tag_labels = {}      # 配置键 -> [标签控件]
        self._sections = []        # 分区标题（保持插入顺序，与目录一一对应）
        self._section_cards = {}   # 分区标题 -> 卡片 QFrame（目录跳转 / 滚动同步用）
        self._cat_syncing = False  # 目录 <-> 滚动条 同步重入保护
        self._warn_labels = []     # 需要随主题重贴 warn 色的说明文字
        self._delete_dirty = False     # 用户是否改过「删除源文件」主控
        self._theme_pref = "auto"
        self._notice_failed = False

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

        # 左右两栏：[目录 | 页内滚动区]；两者都在页的最外层，目录不随内容滚动。
        body = QHBoxLayout()
        body.setContentsMargins(12, 0, 0, 0)
        body.setSpacing(8)

        self.cat_list = QListWidget(self)
        self.cat_list.setObjectName("settingsCat")
        self.cat_list.setFixedWidth(160)
        self.cat_list.setHorizontalScrollBarPolicy(Qt.ScrollBarAlwaysOff)
        self.cat_list.setToolTip("点击跳转到对应分区。")
        self.cat_list.currentRowChanged.connect(self._on_cat_row_changed)
        self.cat_list.hide()            # 无分区时隐藏（_refresh_catalog 里恢复）
        body.addWidget(self.cat_list)

        scroll = QScrollArea(self)
        scroll.setObjectName("settingsScroll")
        scroll.setWidgetResizable(True)
        scroll.setFrameShape(QFrame.NoFrame)
        scroll.setHorizontalScrollBarPolicy(Qt.ScrollBarAsNeeded)
        self.scroll = scroll
        inner = QWidget(scroll)
        self._lay = QVBoxLayout(inner)
        self._lay.setContentsMargins(12, 12, 12, 10)
        self._lay.setSpacing(10)
        scroll.setWidget(inner)
        body.addWidget(scroll, 1)
        root.addLayout(body, 1)

        # 用户在卡片区滚动时保持目录选中同步（best-effort，重入由 _cat_syncing 保护）
        scroll.verticalScrollBar().valueChanged.connect(self._sync_cat_to_scroll)

        head = QHBoxLayout()
        head.setSpacing(8)
        title = QLabel("设置", inner)
        title.setObjectName("appTitle")
        head.addWidget(title)
        head.addStretch(1)
        self._lay.addLayout(head)
        self._lay.addWidget(self._hint(
            "所有修改点「保存」后写入配置；带「需重启」标签的项在下次启动生效。", inner))

        # 分区顺序 = 目录顺序；每条 _build_*_section 内部调用 _section() 建卡。
        self._build_extract_section()      # 解压行为
        self._build_general_section()      # 常规
        self._build_ui_section()           # 界面
        self._build_share_section()        # 分享与手势
        self._build_qr_section()           # 二维码与剪贴板
        self._build_notify_section()       # 通知与实验性
        self._build_trust_section()        # 网址信任
        self._build_hotkey_section()       # 全局快捷键
        self._build_close_section()        # 托盘与关闭
        self._lay.addStretch(1)
        self._refresh_catalog()

        # 底部动作条（固定在滚动区之外）
        foot = QFrame(self)
        foot.setObjectName("dlgFoot")
        f = QHBoxLayout(foot)
        f.setContentsMargins(12, 8, 12, 10)
        f.setSpacing(8)
        self.reset_btn = QPushButton("恢复默认", foot)
        self.reset_btn.setObjectName("danger")
        self.reset_btn.setCursor(Qt.PointingHandCursor)
        self.reset_btn.setToolTip("把所有设置恢复为程序默认值（需二次确认）。")
        self.reset_btn.clicked.connect(self._on_reset)
        f.addWidget(self.reset_btn)
        f.addStretch(1)
        self.notice_label = QLabel("", foot)
        self.notice_label.setObjectName("stripHint")
        self.notice_label.setSizePolicy(QSizePolicy.Ignored, QSizePolicy.Fixed)
        f.addWidget(self.notice_label)
        self.save_btn = QPushButton("保存", foot)
        self.save_btn.setObjectName("primary")
        self.save_btn.setCursor(Qt.PointingHandCursor)
        self.save_btn.setDefault(True)
        self.save_btn.clicked.connect(self._on_save)
        f.addWidget(self.save_btn)
        root.addWidget(foot)

    # ---- 分区骨架 / 行骨架 ----
    def _section(self, icon, title, desc=None):
        """建一个分区卡片（复用 pages.py 的 card + sectionTitle idiom），返回其布局。"""
        card = QFrame(self)
        card.setObjectName("card")
        box = QVBoxLayout(card)
        box.setContentsMargins(14, 12, 14, 12)
        box.setSpacing(6)
        head = QHBoxLayout()
        head.setSpacing(8)
        head.addWidget(Glyph(icon, card, 13, role="muted"))
        lbl = QLabel(title, card)
        lbl.setObjectName("sectionTitle")
        head.addWidget(lbl)
        head.addStretch(1)
        box.addLayout(head)
        if desc:
            box.addWidget(self._hint(desc, card))
        self._lay.addWidget(card)
        self._sections.append(title)
        self._section_cards[title] = card
        return box

    def _reg(self, key, widget):
        """登记某配置键对应的控件（同一键可多个控件）。"""
        self._controls.setdefault(str(key), []).append(widget)
        return widget

    def _tagged(self, row, keys, tag):
        """给一行加右侧生效方式标签，并把标签登记到这些键上。"""
        row.addStretch(1)
        lbl = QLabel(tag, self)
        lbl.setObjectName("stripHint")
        if tag == TAG_RESTART:
            lbl.setToolTip(_RESTART_TIP)
        row.addWidget(lbl)
        self._watch_hint(lbl)
        for k in (keys if isinstance(keys, (list, tuple)) else [keys]):
            self._tags.setdefault(str(k), tag)
            self._tag_labels.setdefault(str(k), []).append(lbl)
        return lbl

    def _hint(self, text, parent=None, warn=False):
        lbl = QLabel(str(text), parent if parent is not None else self)
        lbl.setObjectName("stripHint")
        lbl.setWordWrap(True)
        if warn:
            lbl.setStyleSheet("color: %s;" % PALETTE["warn_text"])
            self._warn_labels.append(lbl)
        return self._watch_hint(lbl)

    def _sub_label(self, lay, text):
        lbl = QLabel(str(text), self)
        lbl.setObjectName("stripHint")
        lay.addWidget(lbl)
        self._watch_hint(lbl)
        return lbl

    # ---- #stripHint 折行高度兜底（修 CJK 描述行裁切） ----
    def _watch_hint(self, lbl):
        """让页面在标签 polish / 样式变化后重算其最小高度（返回同一标签）。

        注意：**此处不立刻计算** fontMetrics——构造期样式表尚未 polish，
        字号还不是 11px；lineSpacing 只在 polish 后才可信。"""
        try:
            lbl.installEventFilter(self)
        except Exception:
            pass
        return lbl

    def _fit_hint(self, lbl):
        """把说明标签的最小高度抬到 lineSpacing() + 2。

        QLabel(setWordWrap=True) 的 heightForWidth 按 fontMetrics().height()（11）
        计算，绘制线盒却按 lineSpacing()（13）排；默认上下各裁 1px、左缘也无余量。
        lineSpacing 只在样式表 polish 后（11px 字号）才可信，所以此处**不在构造时
        计算**，而在 polish / 样式变化 / 显示后由本方法兜底；左缘余量由 QSS
        `padding: 0 1px` 提供（纵向 padding 无效，见 style.py 注释）。"""
        try:
            need = int(lbl.fontMetrics().lineSpacing()) + 2
            if lbl.minimumHeight() < need:
                lbl.setMinimumHeight(need)
        except Exception:
            pass

    def _fit_all_hints(self):
        for lbl in self.findChildren(QLabel):
            if lbl.objectName() == "stripHint":
                self._fit_hint(lbl)

    def eventFilter(self, obj, event):
        try:
            if (obj.objectName() == "stripHint"
                    and event.type() in (QEvent.Polish, QEvent.StyleChange)):
                self._fit_hint(obj)
        except Exception:
            pass
        return super().eventFilter(obj, event)

    def showEvent(self, event):
        super().showEvent(event)
        self._fit_all_hints()

    # ---- 目录（分区索引） ----
    def _refresh_catalog(self):
        """按 _sections 顺序重建目录，并记住每项对应的卡片（唯一真源）。"""
        rows = [t for t in self._sections if t in self._section_cards]
        self.cat_list.blockSignals(True)
        self.cat_list.clear()
        for title in rows:
            self.cat_list.addItem(title)
        if rows:
            self.cat_list.setCurrentRow(0)
        self.cat_list.blockSignals(False)
        self.cat_list.setVisible(bool(rows))

    def _on_cat_row_changed(self, row):
        """点目录项 → 滚动**页内**滚动区到对应卡片（绝不碰外层 pageScroll）。"""
        if self._cat_syncing or row < 0 or row >= len(self._sections):
            return
        card = self._section_cards.get(self._sections[row])
        if card is None:
            return
        self._cat_syncing = True
        try:
            self.scroll.ensureWidgetVisible(card, 0, 8)
        finally:
            self._cat_syncing = False

    def _sync_cat_to_scroll(self, value):
        """页内滚动时把目录选中项同步到视口顶部所在卡片（best-effort）。"""
        if self._cat_syncing or not self._sections:
            return
        row = 0
        for n, title in enumerate(self._sections):
            card = self._section_cards.get(title)
            if card is None:
                continue
            if card.y() <= int(value) + 1:
                row = n
            else:
                break
        if self.cat_list.currentRow() == row:
            return
        self._cat_syncing = True
        try:
            self.cat_list.setCurrentRow(row)
        finally:
            self._cat_syncing = False

    def _check_row(self, lay, key, text, tag=TAG_LIVE, tip=None, extra_keys=None):
        cb = QCheckBox(text, self)
        if tip:
            cb.setToolTip(tip)
        row = QHBoxLayout()
        row.setSpacing(8)
        row.addWidget(cb)
        self._reg(key, cb)
        self._tagged(row, [key] + list(extra_keys or []), tag)
        lay.addLayout(row)
        return cb

    def _spin_row(self, lay, keys, label, spin, tag=TAG_LIVE, tip=None):
        if tip:
            spin.setToolTip(tip)
        row = QHBoxLayout()
        row.setSpacing(8)
        lbl = QLabel(label, self)
        lbl.setObjectName("fLabel")
        lbl.setFixedWidth(96)
        row.addWidget(lbl)
        row.addWidget(spin)
        for k in keys:
            self._reg(k, spin)
        self._tagged(row, list(keys), tag)
        lay.addLayout(row)
        return spin

    def _radio_col(self, lay, keys, options, tag=TAG_LIVE):
        """竖排单选组（options=[(value,label,tip)]），返回 {value: radio} 与容器。"""
        host = QWidget(self)
        box = QVBoxLayout(host)
        box.setContentsMargins(0, 0, 0, 0)
        box.setSpacing(3)
        grp = QButtonGroup(host)
        radios = {}
        for value, label, tip in options:
            rb = QRadioButton(label, host)
            if tip:
                rb.setToolTip(tip)
            grp.addButton(rb)
            radios[value] = rb
            box.addWidget(rb)
        row = QHBoxLayout()
        row.addWidget(host)
        for k in keys:
            self._reg(k, host)
        self._tagged(row, list(keys), tag)
        lay.addLayout(row)
        return radios, grp

    # ------------------------------------------------------------------
    # 分区：解压行为
    # ------------------------------------------------------------------
    def _build_extract_section(self):
        box = self._section(
            "archive", "解压行为",
            "解压引擎与产物整理方式；「删除源文件」是对所有监听目录的总控（各目录可单独覆盖）。")
        self.delete_master_cb = QCheckBox("解压成功后删除源文件（所有监听目录）", self)
        self.delete_master_cb.setToolTip(
            "对所有监听目录统一设置 delete_source；删除是把源文件移入回收站，"
            "可在「删除回溯」页一键还原。各目录单独设置可在「目录设置」弹窗里改。")
        self.delete_master_cb.clicked.connect(lambda _s: self._set_delete_dirty())
        drow = QHBoxLayout()
        drow.setSpacing(8)
        drow.addWidget(self.delete_master_cb)
        self._reg("watch_paths.delete_source", self.delete_master_cb)
        # 本页不再持有监听目录的增删改控件（页头胶囊条 → WatchDirDialog 负责），
        # 但删除源文件总控仍写回 watch_paths 的 delete_source 字段；为保持
        # 「DEFAULT_CONFIG 顶层键都有归属控件」的契约，把顶层 watch_paths 也归到
        # 这个总控上（它是本页唯一触及 watch_paths 的控件）。
        self._reg("watch_paths", self.delete_master_cb)
        self._tagged(drow, ["watch_paths", "watch_paths.delete_source"], TAG_LIVE)
        box.addLayout(drow)
        self.delete_state_label = self._hint("", self)
        box.addWidget(self.delete_state_label)

        self.output_time_cb = self._check_row(
            box, "output_time_now", "解压成功后把产物顶层时间戳校准为现在",
            TAG_LIVE, "避免旧日期的大文件夹在大目录里沉底。")
        self.pair_split_cb = self._check_row(
            box, "pair_split_enabled", "跨名分卷链配对（唯一总闸 · 默认开）",
            TAG_LIVE,
            "「7z 验证通过即配对」现已并入基础解压逻辑、默认强制开启；本开关是唯一总闸："
            "关闭后不再把疑似改名的兄弟尾卷自动改名为首卷系列。"
            "断号 / 模糊 / 未通过验证本来就只提示、不动作。")
        self.promote_merge_cb = self._check_row(
            box, "promote_merge", "同名文件夹无文件冲突则合并",
            TAG_LIVE, "解压提升时，同名文件夹内无文件冲突则合并；有同名文件仍重命名为 (N)。")
        self.translate_cb = self._check_row(
            box, "translation_move_enabled", "翻译 JSON 自动归位",
            TAG_LIVE,
            "小于 10MB 的单 json 文件夹，若文件名命中某大文件夹名则移入该文件夹；"
            "小文件夹先出现时监控 5 分钟等待目标。")

    def _set_delete_dirty(self):
        self._delete_dirty = True
        self._refresh_delete_state()

    # ------------------------------------------------------------------
    # 分区：分享与手势
    # ------------------------------------------------------------------
    def _build_share_section(self):
        box = self._section(
            "bolt", "分享与手势",
            "全局快捷键触发「用客户端下载最近分享」的等待策略；留空 = 不设置该快捷键。")
        self.share_wait_spin = QSpinBox(self)
        self.share_wait_spin.setRange(5, 600)
        self.share_wait_spin.setSuffix(" 秒")
        self.share_wait_spin.setToolTip(
            "分享手势最多等待「解析中链接」多少秒；超时取消、绝不回退旧链接。\n"
            "有效范围 5~600 秒：低于 5 按 5、高于 600 按 600 保存。")
        self._spin_row(box, ["share_gesture_wait_sec"], "分享等待", self.share_wait_spin,
                       TAG_LIVE,
                       "分享手势最多等待「解析中链接」多少秒（超时取消、绝不回退旧链接）。"
                       "有效范围 5~600 秒，超出按边界保存。")
        self.share_wait_hint = self._hint(
            "范围 5~600 秒：低于 5 按 5、高于 600 按 600 保存（config 净化同口径）。", self)
        box.addWidget(self.share_wait_hint)

        self.hotkey_share_edit = HotkeyEdit(self)
        self.hotkey_share_clear = QPushButton("清除", self)
        self.hotkey_share_clear.setObjectName("ghostSm")
        self.hotkey_share_clear.setCursor(Qt.PointingHandCursor)
        self.hotkey_share_clear.clicked.connect(self.hotkey_share_edit.clear)
        row = QHBoxLayout()
        row.setSpacing(8)
        lbl = QLabel("分享下载", self)
        lbl.setObjectName("fLabel")
        lbl.setFixedWidth(96)
        row.addWidget(lbl)
        row.addWidget(self.hotkey_share_edit, 1)
        row.addWidget(self.hotkey_share_clear)
        self._reg("hotkey_share", self.hotkey_share_edit)
        self._tagged(row, ["hotkey_share"], TAG_LIVE)
        box.addLayout(row)

        self.hotkey_share_code_edit = HotkeyEdit(self)
        self.hotkey_share_code_clear = QPushButton("清除", self)
        self.hotkey_share_code_clear.setObjectName("ghostSm")
        self.hotkey_share_code_clear.setCursor(Qt.PointingHandCursor)
        self.hotkey_share_code_clear.clicked.connect(self.hotkey_share_code_edit.clear)
        row2 = QHBoxLayout()
        row2.setSpacing(8)
        lbl2 = QLabel("固定提取码", self)
        lbl2.setObjectName("fLabel")
        lbl2.setFixedWidth(96)
        row2.addWidget(lbl2)
        row2.addWidget(self.hotkey_share_code_edit, 1)
        row2.addWidget(self.hotkey_share_code_clear)
        self._reg("hotkey_share_code", self.hotkey_share_code_edit)
        self._tagged(row2, ["hotkey_share_code"], TAG_LIVE)
        box.addLayout(row2)

        self.hotkey_display_label = self._hint("", self)
        box.addWidget(self.hotkey_display_label)

    # ------------------------------------------------------------------
    # 分区：界面
    # ------------------------------------------------------------------
    def _build_ui_section(self):
        box = self._section("dashboard", "界面", "主题与任务历史展示。")
        self.theme_combo = QComboBox(self)
        for label, value in _THEME_ITEMS:
            self.theme_combo.addItem(label, value)
        self.theme_combo.setMinimumWidth(150)
        self.theme_combo.setToolTip(
            "跟随系统：按 Windows 的「应用」深浅色自动选择（启动不做检测、显示后纠正）。\n"
            "浅色 = Fluent 方案；深色 = DevTool 方案。保存后立即应用。")
        row = QHBoxLayout()
        row.setSpacing(8)
        lbl = QLabel("主题", self)
        lbl.setObjectName("fLabel")
        lbl.setFixedWidth(96)
        row.addWidget(lbl)
        row.addWidget(self.theme_combo)
        self._reg("ui_theme", self.theme_combo)
        self._tagged(row, ["ui_theme"], TAG_LIVE)
        box.addLayout(row)

        self.theme_cached_label = self._hint("", self)
        self.theme_cached_label.setToolTip(
            "自动维护：启动时先用上次实际应用的主题出首屏，显示后再按「主题」偏好纠正。")
        self._reg("ui_theme_cached", self.theme_cached_label)
        crow = QHBoxLayout()
        crow.setSpacing(8)
        crow.addWidget(self.theme_cached_label, 1)
        self._tagged(crow, ["ui_theme_cached"], TAG_LIVE)
        box.addLayout(crow)

        self.task_limit_spin = QSpinBox(self)
        self.task_limit_spin.setRange(1, 100000)
        self._spin_row(box, ["task_history_limit"], "任务历史上限", self.task_limit_spin,
                       TAG_LIVE,
                       "只清理终态任务，非终态永不删除；磁盘上的旧数据在下次启动时清理，"
                       "列表立即按新上限查询（界面最多显示 5000 条）。")
        self.logcolor_cb = self._check_row(
            box, "log_colors_enabled", "日志按事件着色",
            TAG_LIVE, "运行日志按成功 / 失败 / 等待等类型着色。")

    # ------------------------------------------------------------------
    # 分区：常规
    # ------------------------------------------------------------------
    def _build_general_section(self):
        box = self._section("gear", "常规", "轮询、7-Zip 检测与密码本相关。")
        self.interval_spin = QSpinBox(self)
        self.interval_spin.setRange(1, 30)
        self._spin_row(box, ["poll_interval"], "轮询间隔(s)", self.interval_spin,
                       TAG_LIVE, "监听目录扫描间隔（秒），保存后下一轮轮询即生效。")
        self.sevenzip_cb = self._check_row(
            box, "sevenzip_check_done",
            "首次 7-Zip 检测已完成（取消勾选 = 下次启动重新检测）",
            TAG_RESTART,
            "程序只在首次启动时检测 7-Zip；取消勾选后，下次启动会重新检测，"
            "缺失或版本过低时弹出安装引导。")
        self.auto_add_cb = self._check_row(
            box, "auto_add_clipboard_password", "捕获到新密码时自动加入长期密码本",
            TAG_LIVE, "与「密码本」页里的同名开关是同一项配置。")
        prow = QHBoxLayout()
        prow.setSpacing(8)
        plbl = QLabel("密码本条目", self)
        plbl.setObjectName("fLabel")
        plbl.setFixedWidth(96)
        prow.addWidget(plbl)
        self.passwords_label = QLabel("", self)
        self.passwords_label.setObjectName("stripHint")
        prow.addWidget(self.passwords_label)
        self._reg("passwords", self.passwords_label)
        self._tagged(prow, ["passwords"], TAG_LIVE)
        box.addLayout(prow)
        box.addWidget(self._hint("长期密码本在「密码本」页管理；此处只读显示条目数。"))

    # ------------------------------------------------------------------
    # 分区：通知与实验性
    # ------------------------------------------------------------------
    def _build_notify_section(self):
        box = self._section(
            "alert", "通知与实验性",
            "通知总开关关闭后不弹任何提示（运行日志仍记录）；实验性功能默认关闭。")
        self.notify_cb = self._check_row(
            box, "notify_enabled", "通知总开关", TAG_LIVE,
            "关闭后不弹出任何通知（运行日志仍会记录）。")
        self._sub_label(box, "解压事件")
        self.notify_archive_cb = self._check_row(box, "notify_archive", "发现压缩包")
        self.notify_success_cb = self._check_row(box, "notify_success", "解压完成")
        self.notify_failure_cb = self._check_row(box, "notify_failure", "解压失败")
        self.notify_error_cb = self._check_row(box, "notify_error", "解压出错")
        self._sub_label(box, "托盘提示")
        self.notify_trayed_cb = self._check_row(box, "notify_trayed", "已最小化到托盘")
        self.notify_running_cb = self._check_row(
            box, "notify_already_running", "程序已在运行时提示",
            TAG_LIVE, "再次启动程序时，提示已在运行并打开主界面。")
        self.notify_trust_cb = self._check_row(
            box, "notify_trust_pending", "有新的网址等待确认")
        self._sub_label(box, "网盘任务（实验性）")
        self.notify_baidu_done_cb = self._check_row(
            box, "notify_baidu_done", "网盘下载批次完成",
            TAG_LIVE, "实验性功能开启时：一个下载批次全部任务完成时通知。")
        self.notify_baidu_leftover_cb = self._check_row(
            box, "notify_baidu_leftover", "启动时有未完成的网盘任务",
            TAG_LIVE, "实验性功能开启时：启动发现仍有未完成的网盘任务时通知。")
        self.notify_baidu_dup_cb = self._check_row(
            box, "notify_baidu_dup", "新任务与历史下载重复",
            TAG_LIVE, "实验性功能开启时：新任务在下载历史里已存在（同名同大小）时通知。")

        self.experimental_cb = self._check_row(
            box, "experimental_enabled", "开启实验性功能（默认关）",
            TAG_LIVE,
            "实验性、默认关闭。当前用途：只读探测百度网盘客户端的本地任务库，"
            "用于还原下载批次、目录结构与分卷；只读打开、短连接、不写不锁。")
        self.baidu_auto_invoke_cb = self._check_row(
            box, "baidu_auto_invoke", "检测到分享链接时自动拉起客户端下载",
            TAG_LIVE,
            "开启后：复制到百度网盘分享链接时，程序自动把它交给网盘客户端下载（整包）。"
            "会自动触发下载，请确认来源可信；也可随时用托盘菜单手动触发。")
        self.baidu_pick_cb = self._check_row(
            box, "baidu_pick_before_download", "分享下载前先让我挑选文件",
            TAG_LIVE, "分享里文件很多、只想下载其中一部分时使用。")
        brow = QHBoxLayout()
        brow.setSpacing(8)
        blbl = QLabel("任务库路径", self)
        blbl.setObjectName("fLabel")
        blbl.setFixedWidth(96)
        brow.addWidget(blbl)
        self.baidu_db_edit = QLineEdit(self)
        self.baidu_db_edit.setPlaceholderText("留空 = 自动探测网盘客户端任务库")
        self.baidu_db_edit.setToolTip("BaiduYunGuanjia.db 路径；留空自动探测。")
        brow.addWidget(self.baidu_db_edit, 1)
        self.baidu_db_browse_btn = QPushButton("浏览", self)
        self.baidu_db_browse_btn.setCursor(Qt.PointingHandCursor)
        self.baidu_db_browse_btn.clicked.connect(self._browse_baidu_db)
        brow.addWidget(self.baidu_db_browse_btn)
        self._reg("baidu_task_db", self.baidu_db_edit)
        self._tagged(brow, ["baidu_task_db"], TAG_LIVE)
        box.addLayout(brow)
        self.share_nologin_hint = self._hint(
            "⚠ 实验性提示：该链路不携带浏览器登录态，也不使用浏览器 cookie。"
            "若百度网盘客户端未在运行，唤起可能让客户端进入未登录状态；"
            "因此自动拉起前会先检查客户端进程，未运行时跳过并提示。", self, warn=True)
        box.addWidget(self.share_nologin_hint)

        self._notify_subs = (self.notify_archive_cb, self.notify_success_cb,
                             self.notify_failure_cb, self.notify_error_cb,
                             self.notify_trayed_cb, self.notify_running_cb,
                             self.notify_trust_cb, self.notify_baidu_done_cb,
                             self.notify_baidu_leftover_cb, self.notify_baidu_dup_cb)
        self._exp_subs = (self.baidu_auto_invoke_cb, self.baidu_pick_cb,
                          self.baidu_db_edit, self.baidu_db_browse_btn,
                          self.share_nologin_hint)
        self.notify_cb.toggled.connect(lambda _s: self._sync_notify_enabled())
        self.experimental_cb.toggled.connect(lambda _s: self._sync_experimental())

    def _browse_baidu_db(self):
        try:
            path, _flt = QFileDialog.getOpenFileName(
                self, "选择网盘任务库", "", "数据库 (*.db);;所有文件 (*)")
        except Exception:
            path = ""
        if path:
            self.baidu_db_edit.setText(path)

    def _sync_notify_enabled(self):
        on = bool(self.notify_cb.isChecked())
        for cb in self._notify_subs:
            cb.setEnabled(on)

    def _sync_experimental(self):
        on = bool(self.experimental_cb.isChecked())
        for w in self._exp_subs:
            w.setEnabled(on)

    # ------------------------------------------------------------------
    # 分区：二维码与剪贴板
    # ------------------------------------------------------------------
    def _build_qr_section(self):
        box = self._section(
            "search", "二维码与剪贴板", "剪贴板二维码识别、链接识别与临时密码过滤。")
        self.qr_cb = self._check_row(box, "qr_enabled", "启用二维码识别（剪贴板图片 / 拖入图片）",
                                     TAG_LIVE)
        self.qr_redirect_cb = self._check_row(
            box, "qr_url_redirect", "二维码链接域名重定向", TAG_LIVE,
            "打开前重写链接域名，例如 drive.uc.cn → fast.uc.cn。")
        self.qr_url_cb = self._check_row(
            box, "qr_url_enabled", "复制网址时识别二维码图片并打开", TAG_LIVE,
            "复制 http(s) 网址时自动访问；若返回的是二维码图片，则解码后按设置打开。")
        self._sub_label(box, "二维码打开网页后剪贴板联动")
        self.clip_radios, self._clip_group = self._radio_col(
            box, ["qr_clipboard_action"],
            (("none", "不处理（保持原样）", "打开网页后不改动剪贴板。"),
             ("code", "恢复最近复制的提取码",
              "把最近一次复制的非图片内容（如提取码）写回剪贴板，方便直接粘贴。"),
             ("url", "写回二维码内容", "把二维码解码出来的整段内容写回剪贴板。")),
            TAG_LIVE)
        self._sub_label(box, "临时密码")
        self.url_exclude_cb = self._check_row(
            box, "url_exclude_temp_password", "网址排除", TAG_LIVE,
            "带 :// 的网址不记为临时密码；xxxx.com 这类无协议头的域名形式仍会记录。"
            "关闭则照单全收（连网址也收）。")
        self.temp_filter_cb = self._check_row(
            box, "temp_password_filter", "智能过滤（在「网址排除」基础上更严格）",
            TAG_LIVE,
            "再排除多行文本、文件路径/UNC、带常见扩展名的文件名、含句读标点的句子"
            "（且只收 <60 字符）。")
        self.url_exclude_cb.toggled.connect(
            lambda s: self.temp_filter_cb.setEnabled(bool(s)))
        self.ttl_spin = QSpinBox(self)
        self.ttl_spin.setRange(1, 24 * 365)
        self.temp_max_spin = QSpinBox(self)
        self.temp_max_spin.setRange(1, 100000)
        trow = QHBoxLayout()
        trow.setSpacing(6)
        t1 = QLabel("有效期(h)", self)
        t1.setObjectName("fLabel")
        trow.addWidget(t1)
        trow.addWidget(self.ttl_spin)
        t2 = QLabel("保留上限(条)", self)
        t2.setObjectName("fLabel")
        trow.addWidget(t2)
        trow.addWidget(self.temp_max_spin)
        self._reg("temp_password_ttl_hours", self.ttl_spin)
        self._reg("temp_password_max", self.temp_max_spin)
        self._tagged(trow, ["temp_password_ttl_hours", "temp_password_max"], TAG_LIVE)
        box.addLayout(trow)
        box.addWidget(self._hint(
            "临时密码生命周期 = 本次系统启动（程序重启不丢，系统重启失效），"
            "再叠加有效期与条数上限。"))

    # ------------------------------------------------------------------
    # 分区：网址信任
    # ------------------------------------------------------------------
    def _build_trust_section(self):
        box = self._section(
            "shield", "网址信任",
            "按用途拆两套信任判定（自动打开 / 下载识别），各自独立；内置敏感地址两套共享。")
        self.trust_card = box.parentWidget()
        self._reg("url_trust", self.trust_card)
        self.trust_builtin_cb = self._check_row(
            box, "url_trust.builtin_blacklist", "拦截内置敏感地址", TAG_LIVE,
            "私网 / 回环 / 链路本地 / 元数据 / 保留地址默认拒绝（防 SSRF）。两用途共享。",
            extra_keys=["url_trust"])
        self.tls_cb = self._check_row(
            box, "tls_skip_verify", "允许不验证 HTTPS 证书", TAG_LIVE,
            "不推荐；仅当站点证书有问题时才需要，开启有中间人攻击风险。")
        self._trust_radios = {}
        self.trust_editors = {}
        self._build_trust_purpose(
            box, "open", "自动在浏览器打开（二维码解出的链接）",
            "程序识别到二维码、要自动在浏览器打开其链接时的信任判定。")
        self._build_trust_purpose(
            box, "fetch", "下载识别二维码（复制的网址）",
            "程序拉取你复制的网址、判断它是不是二维码图片时的信任判定。")
        self.rules_edit = QPlainTextEdit(self)
        self.rules_edit.setMaximumHeight(72)
        self.rules_edit.setPlaceholderText("每行一条：源域名 -> 目标域名")
        self.rules_edit.setToolTip(
            "二维码链接域名重定向规则，例如：drive.uc.cn -> fast.uc.cn\n"
            "只替换主机名，路径 / 查询 / 锚点原样保留。")
        self._sub_label(box, "域名重定向规则")
        box.addWidget(self.rules_edit)
        self._reg("url_redirect_rules", self.rules_edit)
        trow = QHBoxLayout()
        trow.addStretch(1)
        self._tagged(trow, ["url_redirect_rules"], TAG_LIVE)
        box.addLayout(trow)
        self.trust_note = self._hint(
            "说明：私网 / 回环 / 链路本地 / 元数据等内置敏感地址默认拒绝，"
            "即使选择「自动信任」也不会放行，只有手动加入白名单才会信任。"
            "两套名单互不影响：同一域名可「自动打开」放行、同时「下载识别」拒绝。",
            self)
        self.trust_note.setStyleSheet("color: %s;" % PALETTE["danger"])
        box.addWidget(self.trust_note)
        box.addWidget(self._hint(
            "⚠ 分享链路例外：开启「实验性功能」后，分享链路抓取公开分享页时不受上述"
            "两套名单限制（必须先抓一次分享页才能拿到 shareid/share_uk）。"
            "把 pan.baidu.com 加进「下载识别」的黑名单，拦不住分享链路的这次抓取。",
            self, warn=True))

    def _build_trust_purpose(self, lay, purpose, title, tip):
        """某用途的信任分区：新域名默认行为单选 + 白/黑名单编辑框。"""
        self._sub_label(lay, title)
        radios, _grp = self._radio_col(
            lay, ["url_trust.%s.new_domain_action" % purpose],
            (("none", "无操作（默认）", "不打开、不询问、也不记录，静默跳过。"),
             ("ask", "弹窗询问", "每次遇到本用途下未信任的新域名都弹窗询问。"),
             ("auto_whitelist", "自动信任", "公网新域名自动放行并加入本用途白名单。"),
             ("auto_blacklist", "自动拒绝", "公网新域名自动拒绝并加入本用途黑名单。")),
            TAG_LIVE)
        self._trust_radios[purpose] = radios
        hosts = {}
        for key, label, hint in (
                ("whitelist", "白名单（每行一个域名，含全部子域）",
                 "命中即信任，可覆盖内置敏感地址拦截。"),
                ("blacklist", "黑名单（每行一个域名，优先级最高）", "命中即静默拒绝。")):
            edit = QPlainTextEdit(self)
            edit.setMaximumHeight(64)
            edit.setToolTip(hint)
            self._sub_label(lay, label)
            lay.addWidget(edit)
            self._reg("url_trust.%s.%s" % (purpose, key), edit)
            rows = QHBoxLayout()
            rows.addStretch(1)
            self._tagged(rows, ["url_trust.%s.%s" % (purpose, key)], TAG_LIVE)
            lay.addLayout(rows)
            hosts[key] = edit
        self.trust_editors[purpose] = hosts

    # ------------------------------------------------------------------
    # 分区：全局快捷键
    # ------------------------------------------------------------------
    def _build_hotkey_section(self):
        box = self._section("terminal", "全局快捷键", "用于唤起主界面；保存后立即重新注册。")
        self.hotkey_enable_cb = self._check_row(
            box, "hotkey_enabled", "启用全局快捷键", TAG_LIVE,
            "主界面隐藏到托盘时也能用它唤起。")
        self.hotkey_edit = HotkeyEdit(self)
        self.hotkey_clear_btn = QPushButton("清除", self)
        self.hotkey_clear_btn.setObjectName("ghostSm")
        self.hotkey_clear_btn.setCursor(Qt.PointingHandCursor)
        self.hotkey_clear_btn.clicked.connect(self.hotkey_edit.clear)
        row = QHBoxLayout()
        row.setSpacing(8)
        lbl = QLabel("唤起主界面", self)
        lbl.setObjectName("fLabel")
        lbl.setFixedWidth(96)
        row.addWidget(lbl)
        row.addWidget(self.hotkey_edit, 1)
        row.addWidget(self.hotkey_clear_btn)
        self._reg("hotkey", self.hotkey_edit)
        self._tagged(row, ["hotkey"], TAG_LIVE)
        box.addLayout(row)
        box.addWidget(self._hint(
            "点击输入框后按下组合键（需含 Ctrl/Alt/Win 之一）；清空后保存 = 不设置。"))

    # ------------------------------------------------------------------
    # 分区：托盘与关闭
    # ------------------------------------------------------------------
    def _build_close_section(self):
        box = self._section(
            "close", "托盘与关闭", "点击右上角 × 时的行为；程序以托盘方式常驻。")
        self._close_rbs, self._close_group = self._radio_col(
            box, ["close_action"],
            (("ask", "每次询问", "每次关闭都弹出选择。"),
             ("tray", "隐藏到托盘", "程序继续在后台运行。"),
             ("exit", "关闭程序", "停止所有监听与剪贴板监控。")),
            TAG_LIVE)
        box.addWidget(self._hint(
            "选择「关闭程序」会停止全部监听；如只想暂时收起界面，请选「隐藏到托盘」。"))

    # ------------------------------------------------------------------
    # 回填（构造 / 保存 / 恢复默认共用）
    # ------------------------------------------------------------------
    def _snapshot(self):
        try:
            return self.state.snapshot() or {}
        except Exception:
            return {}

    def _load_from_cfg(self):
        """把 state.snapshot() 的当前配置回填到所有控件（并重置主控脏标记）。"""
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

        # 通知
        self.notify_cb.setChecked(b("notify_enabled", True))
        self.notify_archive_cb.setChecked(b("notify_archive", True))
        self.notify_success_cb.setChecked(b("notify_success", True))
        self.notify_failure_cb.setChecked(b("notify_failure", True))
        self.notify_error_cb.setChecked(b("notify_error", True))
        self.notify_trayed_cb.setChecked(b("notify_trayed", True))
        self.notify_running_cb.setChecked(b("notify_already_running", True))
        self.notify_trust_cb.setChecked(b("notify_trust_pending", True))
        self.notify_baidu_done_cb.setChecked(b("notify_baidu_done", True))
        self.notify_baidu_leftover_cb.setChecked(b("notify_baidu_leftover", True))
        self.notify_baidu_dup_cb.setChecked(b("notify_baidu_dup", False))
        self._sync_notify_enabled()

        # 实验性
        self.experimental_cb.setChecked(b("experimental_enabled", False))
        self.baidu_auto_invoke_cb.setChecked(b("baidu_auto_invoke", False))
        self.baidu_pick_cb.setChecked(b("baidu_pick_before_download", False))
        self.baidu_db_edit.setText(s("baidu_task_db"))
        self._sync_experimental()

        # 二维码与剪贴板
        self.qr_cb.setChecked(b("qr_enabled", True))
        self.qr_redirect_cb.setChecked(b("qr_url_redirect", True))
        self.qr_url_cb.setChecked(b("qr_url_enabled", True))
        cur_clip = s("qr_clipboard_action", "none") or "none"
        for gid, value in _CLIP_ACTIONS:
            if value == cur_clip and value in self.clip_radios:
                self.clip_radios[value].setChecked(True)
                break
        self.url_exclude_cb.setChecked(b("url_exclude_temp_password", True))
        self.temp_filter_cb.setChecked(b("temp_password_filter", True))
        self.temp_filter_cb.setEnabled(self.url_exclude_cb.isChecked())
        self.ttl_spin.setValue(max(1, min(24 * 365, i("temp_password_ttl_hours", 24))))
        self.temp_max_spin.setValue(
            max(1, min(100000, i("temp_password_max", 200))))

        # 网址信任
        self._load_trust(cfg)
        self.tls_cb.setChecked(b("tls_skip_verify", False))
        self.rules_edit.setPlainText(
            _format_redirect_rules(cfg.get("url_redirect_rules")))

        # 解压
        self.output_time_cb.setChecked(b("output_time_now", True))
        self.pair_split_cb.setChecked(b("pair_split_enabled", True))
        self.promote_merge_cb.setChecked(b("promote_merge", True))
        self.translate_cb.setChecked(b("translation_move_enabled", True))

        # 分享与手势
        self.share_wait_spin.setValue(
            max(5, min(600, i("share_gesture_wait_sec", 60))))
        self.hotkey_share_edit.setText(s("hotkey_share"))
        self.hotkey_share_code_edit.setText(s("hotkey_share_code"))
        self._refresh_hotkey_display(cfg)

        # 界面
        pref = s("ui_theme", "auto").lower()
        index = 0
        for n, (_label, value) in enumerate(_THEME_ITEMS):
            if value == pref:
                index = n
                break
        self.theme_combo.setCurrentIndex(index)
        self._theme_pref = pref if pref in ("auto", "fluent", "devtool") else "auto"
        cached = s("ui_theme_cached").lower()
        self.theme_cached_label.setText(
            "上次实际应用：%s" % _THEME_NAMES.get(cached, "（未记录）"))
        self.task_limit_spin.setValue(
            max(1, min(100000, i("task_history_limit", 500))))
        self.logcolor_cb.setChecked(b("log_colors_enabled", True))

        # 常规
        self.interval_spin.setValue(max(1, min(30, i("poll_interval", 2))))
        self.sevenzip_cb.setChecked(b("sevenzip_check_done", False))
        self.auto_add_cb.setChecked(b("auto_add_clipboard_password", False))
        self._refresh_passwords_label()

        # 全局快捷键
        self.hotkey_enable_cb.setChecked(b("hotkey_enabled", True))
        self.hotkey_edit.setText(s("hotkey"))
        self._hotkeys_at_load = (s("hotkey").strip(),
                                 s("hotkey_share").strip(),
                                 s("hotkey_share_code").strip(),
                                 b("hotkey_enabled", True))

        # 托盘与关闭
        cur_close = s("close_action", "ask") or "ask"
        if cur_close not in self._close_rbs:
            cur_close = "ask"
        self._close_rbs[cur_close].setChecked(True)

        # 删除源文件主控（各目录可单独覆盖；只有用户真的改过才统一覆盖）
        entries = [e for e in (cfg.get("watch_paths") or []) if isinstance(e, dict)]
        del_val, del_same = _common(entries, "delete_source")
        self._delete_dirty = False
        if del_same:
            self.delete_master_cb.setChecked(bool(del_val))
        else:
            self.delete_master_cb.setChecked(False)
        self._refresh_delete_state()
        self._clear_notice()

    def _load_trust(self, cfg):
        ut = cfg.get("url_trust") or {}
        if not isinstance(ut, dict):
            ut = {}
        self.trust_builtin_cb.setChecked(bool(ut.get("builtin_blacklist", True)))
        for purpose in ("open", "fetch"):
            sub = ut.get(purpose) if isinstance(ut.get(purpose), dict) else {}
            action = str((sub or {}).get("new_domain_action", "none"))
            radios = self._trust_radios.get(purpose) or {}
            if action not in radios:
                action = "none"
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
                "当前：%d/%d 个目录已开启（勾选并保存 = 统一覆盖全部）" % (n, len(entries)))
        elif n:
            self.delete_state_label.setText("当前：全部 %d 个目录均已开启" % n)
        else:
            self.delete_state_label.setText("当前：全部目录均保留源文件")

    def _refresh_hotkey_display(self, cfg=None):
        if cfg is None:
            cfg = self._snapshot()

        def show(key):
            return str(cfg.get(key) or "").strip() or "（未设置）"

        self.hotkey_display_label.setText(
            "当前快捷键：唤起 %s · 分享下载 %s · 固定提取码 %s"
            % (show("hotkey"), show("hotkey_share"), show("hotkey_share_code")))

    def _refresh_passwords_label(self):
        try:
            n = len(self.state.passwords() or [])
            self.passwords_label.setText("%d 条（在「密码本」页管理）" % n)
        except Exception:
            self.passwords_label.setText("（读取失败 · 在「密码本」页管理）")

    # ------------------------------------------------------------------
    # 保存
    # ------------------------------------------------------------------
    def _collect_values(self):
        """把全部可编辑控件收成一个 {配置键: 新值} 字典（只读项不在内）。"""
        cfg = self._snapshot()
        v = {}
        v["notify_enabled"] = bool(self.notify_cb.isChecked())
        v["notify_archive"] = bool(self.notify_archive_cb.isChecked())
        v["notify_success"] = bool(self.notify_success_cb.isChecked())
        v["notify_failure"] = bool(self.notify_failure_cb.isChecked())
        v["notify_error"] = bool(self.notify_error_cb.isChecked())
        v["notify_trayed"] = bool(self.notify_trayed_cb.isChecked())
        v["notify_already_running"] = bool(self.notify_running_cb.isChecked())
        v["notify_trust_pending"] = bool(self.notify_trust_cb.isChecked())
        v["notify_baidu_done"] = bool(self.notify_baidu_done_cb.isChecked())
        v["notify_baidu_leftover"] = bool(self.notify_baidu_leftover_cb.isChecked())
        v["notify_baidu_dup"] = bool(self.notify_baidu_dup_cb.isChecked())
        v["qr_enabled"] = bool(self.qr_cb.isChecked())
        v["qr_url_redirect"] = bool(self.qr_redirect_cb.isChecked())
        v["qr_url_enabled"] = bool(self.qr_url_cb.isChecked())
        v["qr_clipboard_action"] = self._clip_value()
        v["url_exclude_temp_password"] = bool(self.url_exclude_cb.isChecked())
        v["temp_password_filter"] = bool(self.temp_filter_cb.isChecked())
        v["temp_password_ttl_hours"] = int(self.ttl_spin.value())
        v["temp_password_max"] = int(self.temp_max_spin.value())
        v["promote_merge"] = bool(self.promote_merge_cb.isChecked())
        v["translation_move_enabled"] = bool(self.translate_cb.isChecked())
        v["output_time_now"] = bool(self.output_time_cb.isChecked())
        v["pair_split_enabled"] = bool(self.pair_split_cb.isChecked())
        v["log_colors_enabled"] = bool(self.logcolor_cb.isChecked())
        v["hotkey_enabled"] = bool(self.hotkey_enable_cb.isChecked())
        v["hotkey"] = str(self.hotkey_edit.text()).strip()
        v["hotkey_share"] = str(self.hotkey_share_edit.text()).strip()
        v["hotkey_share_code"] = str(self.hotkey_share_code_edit.text()).strip()
        v["poll_interval"] = int(self.interval_spin.value())
        v["task_history_limit"] = int(self.task_limit_spin.value())
        v["sevenzip_check_done"] = bool(self.sevenzip_cb.isChecked())
        v["auto_add_clipboard_password"] = bool(self.auto_add_cb.isChecked())
        v["share_gesture_wait_sec"] = int(self.share_wait_spin.value())
        v["ui_theme"] = self._theme_value()
        v["close_action"] = self._close_value()
        v["tls_skip_verify"] = bool(self.tls_cb.isChecked())
        v["url_redirect_rules"] = _parse_redirect_rules(self.rules_edit.toPlainText())
        v["experimental_enabled"] = bool(self.experimental_cb.isChecked())
        v["baidu_task_db"] = str(self.baidu_db_edit.text()).strip()
        v["baidu_auto_invoke"] = bool(self.baidu_auto_invoke_cb.isChecked())
        v["baidu_pick_before_download"] = bool(self.baidu_pick_cb.isChecked())
        v["url_trust"] = self._collect_trust()
        v["watch_paths"] = self._collect_watch_paths(cfg)
        return v

    def _clip_value(self):
        for gid, value in _CLIP_ACTIONS:
            rb = self.clip_radios.get(value)
            if rb is not None and rb.isChecked():
                return value
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
            action = "none"
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

    def _collect_watch_paths(self, cfg):
        """监听目录条目（逐条原样写回；只在删源主控被改过时才统一覆盖）。

        每个目录的字段（path/enabled/output_dir/delete_source/mode）都从配置快照
        深拷贝后原样返回——绝不丢键、绝不误改未动过的字段。"""
        entries = [dict(e) for e in (cfg.get("watch_paths") or [])
                   if isinstance(e, dict)]
        if self._delete_dirty:
            val = bool(self.delete_master_cb.isChecked())
            for e in entries:
                e["delete_source"] = val
        return entries

    @staticmethod
    def _empty_indices(entries):
        return [n for n, e in enumerate(entries or [])
                if not str(e.get("path") or "").strip()]

    def _on_save(self):
        """保存：逐键走 state.set（既有保存路径）→ 回读校验 → 如实回报。"""
        try:
            values = self._collect_values()
        except Exception as e:
            self._notice("保存失败：%s" % e, ok=False)
            return
        empties = self._empty_indices(values.get("watch_paths"))
        if empties:
            ret = QMessageBox.question(
                self, "路径校验",
                "有 %d 条监听目录路径为空；保存会移除这些空条目（其余设置照常保存）。\n"
                "继续保存？" % len(empties),
                QMessageBox.Yes | QMessageBox.No, QMessageBox.No)
            if ret != QMessageBox.Yes:
                self._notice("保存已取消：请先处理空路径条目", ok=False)
                return
            entries = values["watch_paths"]
            values["watch_paths"] = [e for n, e in enumerate(entries)
                                     if n not in empties]
        try:
            for key, value in values.items():
                self.state.set(key, value)
        except Exception as e:
            self._notice("保存失败：%s" % e, ok=False)
            return
        # 快捷键：仅当相关值变了才重新注册（与旧设置对话框同义）
        changed_hotkeys = self._hotkeys_at_load != (
            values["hotkey"], values["hotkey_share"],
            values["hotkey_share_code"], values["hotkey_enabled"])
        # 主题：走既有应用路径（resolve_theme + apply_theme + ui_theme_cached + 回调）
        theme_error = ""
        if str(values.get("ui_theme")) != self._theme_pref:
            theme_error = self._apply_theme_path(values.get("ui_theme"))
        if changed_hotkeys and self._hotkey_cb is not None:
            try:
                self._hotkey_cb()
            except Exception as e:
                theme_error = theme_error or ("快捷键重新注册失败：%s" % e)
        self._load_from_cfg()
        if theme_error:
            self._notice("设置已保存，但%s" % theme_error, ok=False)
        elif not self._verify_saved(values):
            self._notice("保存失败：配置写入未生效（config.json 是否可写？）", ok=False)
            return
        else:
            self._notice("已保存 · 立即生效（需重启项见各行标签）")
        self.settingsSaved.emit()
        if changed_hotkeys:
            self.hotkeyChanged.emit(values["hotkey"])

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
            elif str(got) != str(want):
                return False
        return True

    # ------------------------------------------------------------------
    # 恢复默认
    # ------------------------------------------------------------------
    def _on_reset(self):
        """恢复默认：二次确认后写回净化后的 DEFAULT_CONFIG（与 load_config 同口径）。"""
        ret = QMessageBox.question(
            self, "恢复默认",
            "确定把所有设置恢复为默认值？\n"
            "监听目录、快捷键、主题等都会一并重置，且无法撤销。",
            QMessageBox.Yes | QMessageBox.No, QMessageBox.No)
        if ret != QMessageBox.Yes:
            self._notice("已取消恢复默认")
            return
        old_pref = self._theme_pref
        old_hotkeys = self._hotkeys_at_load
        try:
            defaults = _sanitize_cfg(_deepcopy(DEFAULT_CONFIG))
            for key, value in defaults.items():
                self.state.set(key, _deepcopy(value))
        except Exception as e:
            self._notice("恢复默认失败：%s" % e, ok=False)
            return
        self._load_from_cfg()
        if self._theme_pref != old_pref:
            self._apply_theme_path(self._theme_pref)
        if old_hotkeys != self._hotkeys_at_load:
            if self._hotkey_cb is not None:
                try:
                    self._hotkey_cb()
                except Exception as e:
                    self._notice("已恢复默认，但快捷键重新注册失败：%s" % e, ok=False)
                    return
            self.hotkeyChanged.emit(
                str(self._snapshot().get("hotkey") or "").strip())
        self._notice("已恢复默认设置")
        self.settingsReset.emit()

    # ------------------------------------------------------------------
    # 主题（既有路径）
    # ------------------------------------------------------------------
    def _apply_theme_path(self, pref):
        """主题切换走既有路径；失败只回报、不抛出。返回错误文本（无错误为空串）。"""
        pref = str(pref or "auto").lower()
        if pref not in ("auto", "fluent", "devtool"):
            pref = "auto"
        try:
            self.state.set("ui_theme", pref)
            want = ui_style.resolve_theme(pref)
            ui_style.apply_theme(QApplication.instance(), want)
            self.state.set("ui_theme_cached", want)
            self._theme_pref = pref
            self.theme_cached_label.setText(
                "上次实际应用：%s" % _THEME_NAMES.get(want, want))
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

    def _notice(self, text, ok=True):
        text = str(text)
        self._notice_failed = not ok
        self.notice_label.setText(text)
        self.notice_label.setToolTip(text)
        try:
            self.notice_label.setStyleSheet(
                "" if ok else "color: %s;" % PALETTE["danger"])
        except Exception:
            pass
        self.notice.emit(text)

    def refresh_theme(self):
        """主题切换后重贴内联色（warn 说明 / 失败提示）；图标自行重绘。"""
        for lbl in self._warn_labels:
            try:
                lbl.setStyleSheet("color: %s;" % PALETTE["warn_text"])
            except Exception:
                pass
        try:
            self.trust_note.setStyleSheet("color: %s;" % PALETTE["danger"])
        except Exception:
            pass
        if self._notice_failed:
            try:
                self.notice_label.setStyleSheet(
                    "color: %s;" % PALETTE["danger"])
            except Exception:
                pass
        for glyph in self.findChildren(Glyph):
            try:
                glyph.update()
            except Exception:
                pass
        # 11px 字体随新样式表重贴：说明行高度兜底需要按新 lineSpacing 重算
        self._fit_all_hints()

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

    def apply_mode(self, key):
        """该键的生效方式标签文本（TAG_LIVE / TAG_RESTART，未登记返回 None）。"""
        return self._tags.get(str(key))

    def tag_label_for(self, key):
        labels = self._tag_labels.get(str(key)) or []
        return labels[0] if labels else None

    def section_titles(self):
        return list(self._sections)
