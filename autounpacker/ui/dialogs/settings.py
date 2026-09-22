# -*- coding: utf-8 -*-
"""SettingsDialog：全部配置项编辑（通知/二维码/信任名单/快捷键/更新/7-Zip 管理等）。"""
import threading

from PyQt5.QtWidgets import (QWidget, QVBoxLayout, QHBoxLayout, QLabel,
                             QPushButton, QCheckBox, QPlainTextEdit, QSpinBox,
                             QMessageBox, QDialog, QGroupBox, QRadioButton,
                             QButtonGroup, QListWidget, QStackedWidget,
                             QLayout, QComboBox, QScrollArea, QFrame)
from PyQt5.QtCore import Qt, QTimer

from ... import sevenzip as sevenzip_manager
from ..style import PALETTE
from ..widgets import HotkeyEdit
from .. import style as ui_style
from .sevenzip import SevenZipSetupDialog, _SevenZipOp


class SettingsDialog(QDialog):
    """设置：通知开关、剪贴板联动、二维码识别、轮询间隔等，改动即时生效并保存。"""

    def __init__(self, state, hub, parent=None, on_hotkey_change=None,
                 on_theme_change=None):
        super().__init__(parent)
        self.state = state
        self.hub = hub
        self._hotkey_cb = on_hotkey_change
        self._theme_cb = on_theme_change
        self.setWindowTitle("设置")
        self.setModal(True)
        self.resize(640, 520)
        self.setMinimumSize(580, 420)

        root = QVBoxLayout(self)
        root.setContentsMargins(12, 14, 12, 12)
        root.setSpacing(10)

        # 左：分类列表；右：具体选项（QStackedWidget 随分类切换）
        body = QHBoxLayout()
        body.setSpacing(10)
        self._cat_list = QListWidget()
        self._cat_list.setObjectName("settingsCat")
        self._cat_list.setFixedWidth(150)
        body.addWidget(self._cat_list)
        self._stack = QStackedWidget()
        body.addWidget(self._stack, 1)
        root.addLayout(body, 1)

        btn_row = QHBoxLayout()
        btn_row.addStretch(1)
        close_btn = QPushButton("关闭")
        close_btn.clicked.connect(self.close)
        btn_row.addWidget(close_btn)
        root.addLayout(btn_row)

        self._build_pages()
        self._cat_list.currentRowChanged.connect(self._stack.setCurrentIndex)
        self._cat_list.setCurrentRow(0)
        # 高度按「最高的一页」自适应，并**延后到布局稳定后再算**：立刻算时，
        # 切换过主题/风格后 sizeHint 可能还没刷新，首开会又矮又出滚动条。
        # 宽度维持 640，最小宽度仍允许横向压缩（长标签换行）。
        self._fitted = False   # 只自适应一次，之后用户拖动窗口高度不再回弹
        self.resize(640, 560)
        QTimer.singleShot(0, self._fit_to_content)

    def _fit_to_content(self):
        """按最高的一页 + 非页面部分 计算窗口高度（与当前主题/风格无关）。

        只在首次成功时自适应一次；此后用户拖动改变高度不再被强制回弹。"""
        if self._fitted:
            return
        try:
            page_h = 0
            for i in range(self._stack.count()):
                w = self._stack.widget(i)
                if w is not None:
                    page_h = max(page_h, w.sizeHint().height())
            chrome = max(0, self.sizeHint().height()
                         - self._stack.sizeHint().height())
            target = min(max(480, page_h + chrome + 24), 640)
            self.setMinimumHeight(420)
            self.resize(640, target)
            self._fitted = True
        except Exception:
            pass

    def showEvent(self, event):
        super().showEvent(event)
        # 显示后再算一次：切换主题/风格后 sizeHint 可能滞后，延后一点更稳
        QTimer.singleShot(30, self._fit_to_content)

    # ---------- 页面构建 ----------
    def _cfg_cb(self, key, text, default):
        """配置开关：勾选状态即写回配置（即时生效）。"""
        cb = QCheckBox(text)
        cb.setChecked(bool(self.state.snapshot().get(key, default)))
        cb.stateChanged.connect(lambda s, k=key: self.state.set(k, bool(s)))
        return cb

    def _on_close_action(self, btn):
        """「常规」页关闭行为单选：选择即写回 close_action 配置。"""
        for val, rb in self._close_rbs.items():
            if rb is btn:
                self.state.set("close_action", val)
                break

    def _on_trust_mode(self, purpose, btn):
        """「网址信任」页某用途的新域名默认行为单选：选择即写回该用途配置。"""
        for val, rb in (self._na_buttons.get(purpose) or {}).items():
            if rb is btn:
                ut = dict(self.state.snapshot().get("url_trust") or {})
                sub = dict(ut.get(purpose) or {}) if isinstance(ut.get(purpose), dict) else {}
                sub["new_domain_action"] = val
                ut[purpose] = sub
                self.state.set("url_trust", ut)
                break

    def _on_builtin_blacklist(self, checked):
        """「网址信任」页内置敏感地址拦截开关。"""
        ut = dict(self.state.snapshot().get("url_trust") or {})
        ut["builtin_blacklist"] = bool(checked)
        self.state.set("url_trust", ut)

    # ---------- 版本与更新 ----------
    def _check_update(self):
        """点击「检查更新」：后台线程请求 GitHub Releases API。

        绝不主动调用；结果以文本行展示（不弹窗）：
        - 网络失败 → 「无法连接 GitHub，请稍后再试」
        - 有新版本 → 启用「前往下载更新」按钮
        - 已是最新 → 提示当前已是最新版本
        """
        from ... import updater
        self._check_btn.setEnabled(False)
        self._update_btn.setEnabled(False)
        self._update_status.setText("正在检查更新…")
        self._update_status.setStyleSheet(f"color: {PALETTE['muted2']}; font-size: 12px;")

        def _worker():
            status, latest = updater.check_latest_version()
            self._update_result_cache = (status, latest)
            # 投递回主线程执行 UI 更新（跨线程安全）
            QTimer.singleShot(0, self._check_update_done)

        self._update_result_cache = None   # 先置空，再启动线程（避免竞态覆盖）
        threading.Thread(target=_worker, daemon=True).start()
        # Qt 跨线程结果回传：worker 线程完成后用 QTimer.singleShot(0) 把
        # 回调投递回主线程执行（Qt 定时器队列线程安全，不直接跨线程碰 UI）。

    def _check_update_done(self):
        """worker 完成后在主线程执行（QTimer.singleShot 投递回主线程）。"""
        from ... import updater
        self._check_btn.setEnabled(True)
        status, latest = self._update_result_cache
        try:
            from ... import __version__ as _ver
        except Exception:
            _ver = "0.0.0"
        if status != updater.STATUS_OK or not latest:
            # 网络失败 / 解析失败：不弹窗，提示行告知无法连接
            self._update_status.setText("无法连接 GitHub，请检查网络后重试")
            self._update_status.setStyleSheet(f"color: {PALETTE['danger']}; font-size: 12px;")
            return
        cmp = updater.compare_versions(_ver, latest)
        if cmp == 1:
            self._update_status.setText(
                f"发现新版本 {latest}（当前 {_ver}）")
            self._update_status.setStyleSheet(f"color: {PALETTE['info']}; font-size: 12px;")
            self._latest_version = latest            # 供「前往下载更新」使用
            self._update_btn.setEnabled(True)   # 有更新才允许点击
        elif cmp == 0:
            self._update_status.setText(f"当前已是最新版本（{_ver}）")
            self._update_status.setStyleSheet(f"color: {PALETTE['success']}; font-size: 12px;")
        else:
            self._update_status.setText(f"当前版本（{_ver}）高于远端最新版")
            self._update_status.setStyleSheet(f"color: {PALETTE['muted2']}; font-size: 12px;")

    def _open_update_page(self):
        """点击「前往下载更新」：执行自动更新（下载→校验→更新脚本→重启）。

        进度显示在状态行（不弹窗）；数据文件（config/toolbox.db/日志等）
        不会被覆盖。"""
        from ... import updater
        latest = getattr(self, "_latest_version", None)
        if not latest:
            self._update_status.setText("请先点击「检查更新」")
            self._update_status.setStyleSheet(f"color: {PALETTE['danger']}; font-size: 12px;")
            return
        self._update_btn.setEnabled(False)
        self._check_btn.setEnabled(False)
        self._update_status.setText("正在准备更新…")
        self._update_status.setStyleSheet(f"color: {PALETTE['muted2']}; font-size: 12px;")

        def _progress(text):
            self._update_status.setText(text)
            self._update_status.setStyleSheet(f"color: {PALETTE['muted2']}; font-size: 12px;")

        def _worker():
            status, msg = updater.apply_update(latest, progress_cb=_progress)
            self._update_result_cache = (status, msg)
            QTimer.singleShot(0, self._update_apply_done)

        self._update_result_cache = None
        threading.Thread(target=_worker, daemon=True).start()

    def _update_apply_done(self):
        """自动更新流程结束后的 UI 收尾。"""
        status, msg = self._update_result_cache
        if status == "ok":
            self._update_status.setText(msg)
            self._update_status.setStyleSheet(f"color: {PALETTE['success']}; font-size: 12px;")
            # 更新脚本已启动，程序即将被重启；给用户一点阅读时间
            QTimer.singleShot(1500, self.close)
        else:
            self._update_status.setText(msg)
            self._update_status.setStyleSheet(f"color: {PALETTE['danger']}; font-size: 12px;")
            self._check_btn.setEnabled(True)
            self._update_btn.setEnabled(True)

    def _trust_list_editor(self, key, purpose):
        """某用途的白/黑名单编辑框：每行一个域名，停止输入 400ms 后自动保存。"""
        ut = self.state.snapshot().get("url_trust") or {}
        sub = ut.get(purpose) if isinstance(ut.get(purpose), dict) else {}
        edit = QPlainTextEdit("\n".join(str(x) for x in ((sub or {}).get(key) or [])))
        edit.setMaximumHeight(110)
        timer = QTimer(edit)
        timer.setSingleShot(True)
        timer.setInterval(400)

        def _save():
            lines = []
            for ln in edit.toPlainText().splitlines():
                ln = ln.strip().lower()
                if ln and ln not in lines:
                    lines.append(ln)
            try:
                cur = dict(self.state.snapshot().get("url_trust") or {})
                sub2 = dict(cur.get(purpose) or {}) if isinstance(cur.get(purpose), dict) else {}
                sub2[key] = lines
                cur[purpose] = sub2
                self.state.set("url_trust", cur)
            except Exception:
                pass

        timer.timeout.connect(_save)
        edit.textChanged.connect(lambda: timer.start())
        return edit

    def _trust_section(self, purpose, title, tip):
        """构建某用途（open/fetch）的信任配置分区：默认行为单选 + 白/黑名单。"""
        ut = self.state.snapshot().get("url_trust") or {}
        sub = ut.get(purpose) if isinstance(ut.get(purpose), dict) else {}
        box = QGroupBox(title)
        box.setToolTip(tip)
        lay = QVBoxLayout(box)
        lay.setSpacing(6)

        cap = QLabel("遇到未信任的新域名时")
        cap.setStyleSheet(f"color: {PALETTE['muted2']}; font-size: 12px;")
        lay.addWidget(cap)
        grp = QButtonGroup(box)
        grp.setExclusive(True)
        self._na_buttons[purpose] = {}
        current = str((sub or {}).get("new_domain_action", "ask"))
        for val, label, t in (
                ("none", "无操作",
                 "不打开、不询问、也不记录，静默跳过。"),
                ("ask", "弹窗询问（默认）",
                 "每次遇到本用途下未信任的新域名都弹窗询问。"),
                ("auto_whitelist", "自动信任",
                 "公网新域名自动放行并加入本用途白名单。"),
                ("auto_blacklist", "自动拒绝",
                 "公网新域名自动拒绝并加入本用途黑名单。")):
            rb = QRadioButton(label)
            rb.setChecked(val == current)
            rb.setToolTip(t)
            grp.addButton(rb)
            self._na_buttons[purpose][val] = rb
            lay.addWidget(rb)
        grp.buttonClicked.connect(lambda btn, p=purpose: self._on_trust_mode(p, btn))

        wl_label = QLabel("白名单（每行一个域名，含全部子域）")
        wl_label.setWordWrap(True)
        wl_label.setToolTip("命中即信任，可覆盖内置敏感地址拦截。")
        wl_label.setStyleSheet(f"color: {PALETTE['muted2']}; font-size: 12px;")
        lay.addWidget(wl_label)
        lay.addWidget(self._trust_list_editor("whitelist", purpose))
        bl_label = QLabel("黑名单（每行一个域名，优先级最高）")
        bl_label.setWordWrap(True)
        bl_label.setToolTip("命中即静默拒绝。")
        bl_label.setStyleSheet(f"color: {PALETTE['muted2']}; font-size: 12px;")
        lay.addWidget(bl_label)
        lay.addWidget(self._trust_list_editor("blacklist", purpose))
        return box

    @staticmethod
    def _page_title(text):
        lbl = QLabel(text)
        lbl.setObjectName("sectionTitle")
        return lbl

    def _page_widget(self, title, *items):
        """构建一个设置页：标题 + 若干控件/布局（内容装入滚动区，过高可滚动）。"""
        inner = QWidget()
        lay = QVBoxLayout(inner)
        lay.setContentsMargins(4, 0, 4, 0)
        lay.setSpacing(10)
        lay.addWidget(self._page_title(title))
        for it in items:
            if isinstance(it, QLayout):
                lay.addLayout(it)
            else:
                lay.addWidget(it)
        lay.addStretch(1)
        scroll = QScrollArea()
        scroll.setWidgetResizable(True)
        scroll.setWidget(inner)
        scroll.setFrameShape(QFrame.NoFrame)
        scroll.setHorizontalScrollBarPolicy(Qt.ScrollBarAsNeeded)
        outer = QWidget()
        olay = QVBoxLayout(outer)
        olay.setContentsMargins(0, 0, 0, 0)
        olay.addWidget(scroll)
        return outer

    def _build_pages(self):
        """按分类构建页面：每页 = (分类名, 页面控件)。"""
        pages = []

        # ---------- 通知 ----------
        noti_box = QGroupBox("通知")
        nl = QVBoxLayout(noti_box)
        self.notify_cb = self._cfg_cb("notify_enabled", "通知总开关", True)
        self.notify_cb.setToolTip("关闭后不弹出任何通知（运行日志仍会记录）。")
        self.notify_archive_cb = self._cfg_cb("notify_archive", "发现压缩包", True)
        self.notify_success_cb = self._cfg_cb("notify_success", "解压完成", True)
        self.notify_failure_cb = self._cfg_cb("notify_failure", "解压失败", True)
        self.notify_error_cb = self._cfg_cb("notify_error", "解压出错", True)
        nl.addWidget(self.notify_cb)
        for cb in (self.notify_archive_cb, self.notify_success_cb,
                   self.notify_failure_cb, self.notify_error_cb):
            nl.addWidget(cb)

        # 托盘提示：主界面隐藏 / 已在运行 / 有待确认网址时弹出的托盘气泡，
        # 同样受总开关约束，另可各自单独关闭。
        tray_label = QLabel("托盘提示")
        tray_label.setStyleSheet(f"color: {PALETTE['muted']}; font-size: 12px;")
        nl.addSpacing(4)
        nl.addWidget(tray_label)
        self.notify_trayed_cb = self._cfg_cb("notify_trayed", "已最小化到托盘", True)
        self.notify_running_cb = self._cfg_cb(
            "notify_already_running", "程序已在运行时提示", True)
        self.notify_running_cb.setToolTip("再次启动程序时，提示已在运行并打开主界面。")
        self.notify_trust_cb = self._cfg_cb(
            "notify_trust_pending", "有新的网址等待确认", True)
        for cb in (self.notify_trayed_cb, self.notify_running_cb,
                   self.notify_trust_cb):
            nl.addWidget(cb)

        # 网盘任务（实验性：百度网盘任务库）
        baidu_label = QLabel("网盘任务（实验性）")
        baidu_label.setStyleSheet(f"color: {PALETTE['muted']}; font-size: 12px;")
        nl.addSpacing(4)
        nl.addWidget(baidu_label)
        self.notify_baidu_done_cb = self._cfg_cb(
            "notify_baidu_done", "网盘下载批次完成", True)
        self.notify_baidu_done_cb.setToolTip(
            "实验性功能开启时：一个下载批次全部任务完成时通知。")
        self.notify_baidu_leftover_cb = self._cfg_cb(
            "notify_baidu_leftover", "启动时有未完成的网盘任务", True)
        self.notify_baidu_leftover_cb.setToolTip(
            "实验性功能开启时：启动发现仍有未完成的网盘任务时通知。")
        self.notify_baidu_dup_cb = self._cfg_cb(
            "notify_baidu_dup", "新任务与历史下载重复", False)
        self.notify_baidu_dup_cb.setToolTip(
            "实验性功能开启时：新任务在下载历史里已存在（同名同大小）时通知。")
        for cb in (self.notify_baidu_done_cb, self.notify_baidu_leftover_cb,
                   self.notify_baidu_dup_cb):
            nl.addWidget(cb)

        self._notify_subs = (self.notify_archive_cb, self.notify_success_cb,
                             self.notify_failure_cb, self.notify_error_cb,
                             self.notify_trayed_cb, self.notify_running_cb,
                             self.notify_trust_cb,
                             self.notify_baidu_done_cb,
                             self.notify_baidu_leftover_cb,
                             self.notify_baidu_dup_cb)

        def _on_notify_master(s):
            on = bool(s)
            for cb in self._notify_subs:
                cb.setEnabled(on)
        self.notify_cb.stateChanged.connect(_on_notify_master)
        _on_notify_master(self.notify_cb.isChecked())
        pages.append(("通知", self._page_widget("通知", noti_box)))

        # ---------- 二维码与剪贴板 ----------
        self.qr_cb = self._cfg_cb("qr_enabled", "启用剪贴板二维码识别", True)
        self.redirect_cb = self._cfg_cb(
            "qr_url_redirect", "二维码链接域名重定向", True)
        self.redirect_cb.setToolTip(
            "打开前重写链接域名，例如 drive.uc.cn → fast.uc.cn。")
        clip_box = QGroupBox("二维码打开网页后剪贴板联动")
        cl = QVBoxLayout(clip_box)
        self.clip_group = QButtonGroup(self)
        opts = [
            ("none", "不处理（保持原样）", "打开网页后不改动剪贴板。"),
            ("code", "恢复最近复制的提取码",
             "把最近一次复制的非图片内容（如提取码）写回剪贴板，方便直接粘贴。"),
            ("url", "写回二维码内容",
             "把二维码解码出来的整段内容写回剪贴板。"),
        ]
        current = str(self.state.snapshot().get("qr_clipboard_action", "none"))
        for value, label, tip in opts:
            rb = QRadioButton(label)
            rb.setChecked(value == current)
            rb.setToolTip(tip)
            self.clip_group.addButton(rb, opts.index((value, label, tip)))
            cl.addWidget(rb)
        self.clip_group.buttonClicked.connect(
            lambda b: self.state.set("qr_clipboard_action",
                                     opts[self.clip_group.id(b)][0]))
        self.qr_url_cb = self._cfg_cb(
            "qr_url_enabled", "复制网址时识别二维码图片并打开", True)
        self.qr_url_cb.setToolTip(
            "复制 http(s) 网址时自动访问；若返回的是二维码图片，则解码后按设置打开。")

        # 临时密码：父（宽松：网址排除）→ 子（更严格：智能过滤），以及有效期/上限
        tp_box = QGroupBox("临时密码")
        tp_lay = QVBoxLayout(tp_box)
        self.url_exclude_cb = self._cfg_cb(
            "url_exclude_temp_password", "网址排除", True)
        self.url_exclude_cb.setToolTip(
            "带 :// 的网址不记为临时密码；xxxx.com 这类无协议头的域名形式仍会记录。\n"
            "关闭则照单全收（连网址也收）。")
        tp_lay.addWidget(self.url_exclude_cb)
        # 子项：比父更严格，缩进显示，仅在父项开启时可用
        self.tempfilter_cb = self._cfg_cb(
            "temp_password_filter", "智能过滤", True)
        self.tempfilter_cb.setToolTip(
            "在「网址排除」基础上更严格：再排除多行文本、文件路径/UNC、\n"
            "带常见扩展名的文件名、含句读标点的句子（且只收 <60 字符）。")
        child_wrap = QWidget()
        child_lay = QVBoxLayout(child_wrap)
        child_lay.setContentsMargins(24, 0, 0, 0)
        child_lay.addWidget(self.tempfilter_cb)
        tp_lay.addWidget(child_wrap)
        self.tempfilter_cb.setEnabled(self.url_exclude_cb.isChecked())
        self.url_exclude_cb.stateChanged.connect(
            lambda s: self.tempfilter_cb.setEnabled(bool(s)))

        # 有效期 + 保留上限（均可自行设置，默认 24h / 200 条）
        temp_row = QHBoxLayout()
        temp_row.addWidget(QLabel("有效期(h)"))
        self.temp_ttl_spin = QSpinBox()
        self.temp_ttl_spin.setRange(1, 24 * 365)
        self.temp_ttl_spin.setValue(
            int(self.state.snapshot().get("temp_password_ttl_hours", 24)))
        self.temp_ttl_spin.valueChanged.connect(
            lambda v: self.state.set("temp_password_ttl_hours", int(v)))
        temp_row.addWidget(self.temp_ttl_spin)
        temp_row.addSpacing(16)
        temp_row.addWidget(QLabel("保留上限(条)"))
        self.temp_max_spin = QSpinBox()
        self.temp_max_spin.setRange(1, 100000)
        self.temp_max_spin.setValue(
            int(self.state.snapshot().get("temp_password_max", 200)))
        self.temp_max_spin.valueChanged.connect(
            lambda v: self.state.set("temp_password_max", int(v)))
        temp_row.addWidget(self.temp_max_spin)
        temp_row.addStretch(1)
        tp_lay.addLayout(temp_row)

        pages.append(("二维码与剪贴板",
                      self._page_widget("二维码与剪贴板", self.qr_cb,
                                        self.redirect_cb, clip_box,
                                        self.qr_url_cb, tp_box)))

        # ---------- 网址信任（按用途拆两套：open=自动打开浏览器 / fetch=下载识别二维码）----------
        ut = self.state.snapshot().get("url_trust") or {}
        self._na_buttons = {}
        self.builtin_cb = QCheckBox("拦截内置敏感地址")
        self.builtin_cb.setToolTip(
            "私网 / 回环 / 链路本地 / 元数据 / 保留地址默认拒绝（防 SSRF）。两用途共享。")
        self.builtin_cb.setChecked(bool((ut or {}).get("builtin_blacklist", True)))
        self.builtin_cb.stateChanged.connect(self._on_builtin_blacklist)
        self.tls_cb = self._cfg_cb(
            "tls_skip_verify", "允许不验证 HTTPS 证书", False)
        self.tls_cb.setToolTip(
            "不推荐；仅当站点证书有问题时才需要，开启有中间人攻击风险。")

        open_box = self._trust_section(
            "open", "自动在浏览器打开（二维码解出的链接）",
            "程序识别到二维码、要自动在浏览器打开其链接时的信任判定。")
        fetch_box = self._trust_section(
            "fetch", "下载识别二维码（复制的网址）",
            "程序拉取你复制的网址、判断它是不是二维码图片时的信任判定。\n"
            "例如网盘分享链接不可能是二维码，可在此加入黑名单以免多余访问。")
        note = QLabel("说明：私网 / 回环 / 链路本地 / 元数据等内置敏感地址默认拒绝，"
                      "即使选择「自动信任」也不会放行，只有手动加入白名单才会信任。\n"
                      "两套名单互不影响：同一域名可「自动打开」放行、同时「下载识别」拒绝。")
        note.setWordWrap(True)
        note.setStyleSheet(f"color: {PALETTE['danger']}; font-size: 12px;")
        # 分享链路的「例外」必须说明清楚：实验性开启时，抓取公开分享页发生在
        # 网址信任判定之前（monitors.py:1661 与 1344 先于 decide_host），因此**不受**
        # 上面两套名单限制；否则用户会以为把 pan.baidu.com 加进黑名单就能拦住它。
        # 纯只读说明文字，不引入任何逻辑（不改名单、不改判定）。
        # 前缀刻意用「⚠ 分享链路例外」而**不用**「⚠ 实验性提示」：实验分组里已有一条
        # 「⚠ 实验性提示…」，既有测试按该前缀断言"恰有一个"，沿用会撞车。
        self.trust_share_note = QLabel(
            "⚠ 分享链路例外：开启「实验性功能」后，分享链路抓取公开分享页时"
            "不受上述两套名单限制（必须先抓一次分享页才能拿到 shareid/share_uk，"
            "否则无法交给网盘客户端下载）。所以把 pan.baidu.com 加进"
            "「下载识别二维码」的黑名单，拦不住分享链路的这次抓取；"
            "其他网址的打开/抓取仍照常按名单判定。")
        self.trust_share_note.setWordWrap(True)
        self.trust_share_note.setStyleSheet(
            f"color: {PALETTE['warn_text']}; font-size: 12px;")
        pages.append(("网址信任",
                      self._page_widget("网址信任", self.builtin_cb,
                                        self.tls_cb, open_box, fetch_box, note,
                                        self.trust_share_note)))

        # ---------- 解压 ----------
        self.merge_cb = self._cfg_cb(
            "promote_merge", "同名文件夹无冲突则合并", True)
        self.merge_cb.setToolTip(
            "解压提升时，同名文件夹内无文件冲突则合并；有同名文件仍重命名为 (N)。")
        self.translate_cb = self._cfg_cb(
            "translation_move_enabled", "翻译 JSON 自动归位", True)
        self.translate_cb.setToolTip(
            "小于 10MB 的单 json 文件夹，若文件名命中某大文件夹名则移入该文件夹；\n"
            "小文件夹先出现时监控 5 分钟等待目标。")
        pages.append(("解压", self._page_widget("解压", self.merge_cb, self.translate_cb)))

    # ---------- 全局快捷键 ----------
        hot_box = QGroupBox("全局快捷键")
        hot_box.setToolTip("用于唤起主界面。")
        hl = QVBoxLayout(hot_box)
        hl.setSpacing(6)
        self.hotkey_enable_cb = self._cfg_cb(
            "hotkey_enabled", "启用全局快捷键", True)
        self.hotkey_enable_cb.setToolTip("主界面隐藏到托盘时也能用它唤起。")
        self.hotkey_enable_cb.stateChanged.connect(
            lambda s: self._notify_hotkey_change())
        hl.addWidget(self.hotkey_enable_cb)
        hrow = QHBoxLayout()
        hrow.addWidget(QLabel("快捷键"))
        self.hotkey_edit = HotkeyEdit()
        current = str(self.state.snapshot().get("hotkey", "")).strip()
        if current:
            self.hotkey_edit.setText(current)
        self.hotkey_edit.comboChanged.connect(self._on_hotkey_changed)
        hrow.addWidget(self.hotkey_edit, 1)
        clear_btn = QPushButton("清除")
        clear_btn.clicked.connect(self._clear_hotkey)
        hrow.addWidget(clear_btn)
        hl.addLayout(hrow)
        pages.append(("全局快捷键", self._page_widget("全局快捷键", hot_box)))

        # 实验性 2.F 分享快捷键：单独成组，归入「实验性」页（属性/回调/配置键不变）
        share_hot_box = QGroupBox("分享快捷键（实验性）")
        share_hot_box.setToolTip("实验性 2.F：用客户端下载最近分享 / 固定提取码手势。")
        shl = QVBoxLayout(share_hot_box)
        shl.setSpacing(6)
        # 第二个可选快捷键：用客户端下载最近分享（默认留空 = 不设置）
        srow = QHBoxLayout()
        srow.addWidget(QLabel("用客户端下载分享"))
        self.hotkey_share_edit = HotkeyEdit()
        share_current = str(self.state.snapshot().get("hotkey_share", "")).strip()
        if share_current:
            self.hotkey_share_edit.setText(share_current)
        self.hotkey_share_edit.comboChanged.connect(self._on_share_hotkey_changed)
        srow.addWidget(self.hotkey_share_edit, 1)
        share_clear_btn = QPushButton("清除")
        share_clear_btn.clicked.connect(self._clear_share_hotkey)
        srow.addWidget(share_clear_btn)
        shl.addLayout(srow)
        # 第三个可选快捷键：用映射里的固定提取码下载最近分享（默认留空 = 不设置）
        code_row = QHBoxLayout()
        code_row.addWidget(QLabel("固定提取码手势"))
        self.hotkey_share_code_edit = HotkeyEdit()
        code_current = str(
            self.state.snapshot().get("hotkey_share_code", "")).strip()
        if code_current:
            self.hotkey_share_code_edit.setText(code_current)
        self.hotkey_share_code_edit.setToolTip(
            "用映射里的固定提取码下载最近分享；留空 = 不设置（默认）。")
        self.hotkey_share_code_edit.comboChanged.connect(
            self._on_share_code_hotkey_changed)
        code_row.addWidget(self.hotkey_share_code_edit, 1)
        code_clear_btn = QPushButton("清除")
        code_clear_btn.clicked.connect(self._clear_share_code_hotkey)
        code_row.addWidget(code_clear_btn)
        shl.addLayout(code_row)
        share_hint = QLabel("留空 = 不设置（默认）。")
        share_hint.setStyleSheet(f"color: {PALETTE['muted']}; font-size: 12px;")
        shl.addWidget(share_hint)

        # ---------- 7-Zip 管理 ----------
        pages.append(("7-Zip 管理",
                      self._page_widget("7-Zip 管理", self._build_sevenzip_group())))

        # ---------- 常规 ----------
        # 主题：跟随系统 / 浅色 / 深色（切换即时生效并记住偏好）
        theme_row = QHBoxLayout()
        theme_row.addWidget(QLabel("主题"))
        self.theme_cb = QComboBox()
        self.theme_cb.addItem("跟随系统", "auto")
        self.theme_cb.addItem("浅色", "fluent")
        self.theme_cb.addItem("深色", "devtool")
        _cur = str(self.state.snapshot().get("ui_theme", "auto") or "auto").lower()
        self.theme_cb.setCurrentIndex(
            0 if _cur == "auto" else (1 if _cur == "fluent" else 2))
        self.theme_cb.setToolTip(
            "跟随系统：按 Windows 的「应用」深浅色自动选择\n"
            "（启动不做检测、显示后再纠正，不影响启动速度）。\n"
            "浅色 = Fluent 方案；深色 = DevTool 方案。切换即时生效。")
        self.theme_cb.currentIndexChanged.connect(self._on_theme_changed)
        self.theme_cb.setMinimumWidth(150)
        theme_row.addWidget(self.theme_cb)
        theme_row.addStretch(1)

        interval_row = QHBoxLayout()
        interval_row.addWidget(QLabel("轮询间隔(s)"))
        self.interval_spin = QSpinBox()
        self.interval_spin.setRange(1, 30)
        self.interval_spin.setValue(int(self.state.snapshot().get("poll_interval", 2)))
        self.interval_spin.valueChanged.connect(
            lambda v: self.state.set("poll_interval", int(v)))
        interval_row.addWidget(self.interval_spin)
        interval_row.addStretch(1)
        self.logcolor_cb = self._cfg_cb("log_colors_enabled", "日志按事件着色", True)
        self.logcolor_cb.setToolTip("运行日志按成功 / 失败 / 等待等类型着色。")

        # 关闭窗口行为（与 closeEvent 三选一弹窗联动，选择即同步到此设置）
        close_box = QGroupBox("关闭窗口行为")
        close_box.setToolTip("点击右上角 × 时的动作。")
        close_lay = QVBoxLayout(close_box)
        self._close_grp = QButtonGroup(close_box)
        self._close_rbs = {}
        for val, label, tip in (
                ("ask", "每次询问", "每次关闭都弹出选择。"),
                ("tray", "隐藏到托盘", "程序继续在后台运行。"),
                ("exit", "关闭程序", "停止所有监听与剪贴板监控。")):
            rb = QRadioButton(label)
            rb.setToolTip(tip)
            self._close_grp.addButton(rb)
            self._close_rbs[val] = rb
            close_lay.addWidget(rb)
        cur = self.state.snapshot().get("close_action", "ask")
        if cur in self._close_rbs:
            self._close_rbs[cur].setChecked(True)
        self._close_grp.buttonClicked.connect(self._on_close_action)

        # 版本与更新：只显示版本号；「检查更新」需用户手动点击才联网，
        # 绝不主动拉取；结果以文本行展示（不弹窗），发现新版本才启用更新按钮。
        ver_box = QGroupBox("版本与更新")
        ver_lay = QVBoxLayout(ver_box)
        ver_row = QHBoxLayout()
        try:
            from ... import __version__ as _ver
        except Exception:
            _ver = "未知"
        self._ver_label = QLabel(f"当前版本：{_ver}")
        ver_row.addWidget(self._ver_label)
        ver_row.addStretch(1)
        self._check_btn = QPushButton("检查更新")
        self._check_btn.setObjectName("primary")
        self._check_btn.clicked.connect(self._check_update)
        ver_row.addWidget(self._check_btn)
        self._update_btn = QPushButton("前往下载更新")
        self._update_btn.setEnabled(False)
        self._update_btn.clicked.connect(self._open_update_page)
        ver_row.addWidget(self._update_btn)
        ver_lay.addLayout(ver_row)
        # 检查结果文本行（状态提示 / 新版本提示 / 网络失败提示）
        self._update_status = QLabel("")
        self._update_status.setWordWrap(True)
        self._update_status.setStyleSheet(f"color: {PALETTE['muted2']}; font-size: 12px;")
        ver_lay.addWidget(self._update_status)
        # 实验性功能（默认关闭）
        exp_box = QGroupBox("实验性功能")
        exp_lay = QVBoxLayout(exp_box)
        self.experimental_cb = self._cfg_cb(
            "experimental_enabled", "开启实验性功能", False)
        self.experimental_cb.setToolTip(
            "实验性、默认关闭。当前用途：只读探测百度网盘客户端的本地任务库\n"
            "（BaiduYunGuanjia.db），用于还原下载批次、目录结构与分卷。\n"
            "只读打开、短连接、不写不锁，不影响正在运行的网盘客户端。")
        exp_lay.addWidget(self.experimental_cb)
        # 检测到分享链接时自动拉起客户端下载（实验性，默认关）
        self.baidu_auto_invoke_cb = self._cfg_cb(
            "baidu_auto_invoke", "检测到分享链接时自动拉起客户端下载", False)
        self.baidu_auto_invoke_cb.setToolTip(
            "实验性、默认关闭。开启后：复制到百度网盘分享链接时，程序会自动把它交给\n"
            "网盘客户端下载（整包，分享里的文件全下）。注意：会自动触发下载，请确认\n"
            "分享链接来源可信；也可随时改用托盘菜单「用客户端打开最近分享」手动触发。")
        exp_lay.addWidget(self.baidu_auto_invoke_cb)
        # 2.F 整条链路都在实验开关下：总开关没开时子选项灰掉（避免误以为能生效）
        self.baidu_auto_invoke_cb.setEnabled(self.experimental_cb.isChecked())
        self.experimental_cb.stateChanged.connect(
            lambda s: self.baidu_auto_invoke_cb.setEnabled(bool(s)))
        # 分享下载前先让我挑选文件（实验性，默认关；同受实验总开关约束）
        self.baidu_pick_cb = self._cfg_cb(
            "baidu_pick_before_download", "分享下载前先让我挑选文件（实验性）", False)
        self.baidu_pick_cb.setToolTip(
            "实验性、默认关闭。开启后：网盘分享下载前先弹出文件清单，勾选后再下载\n"
            "（适合分享里文件很多、只想下载其中一部分的场景）。")
        exp_lay.addWidget(self.baidu_pick_cb)
        self.baidu_pick_cb.setEnabled(self.experimental_cb.isChecked())
        self.experimental_cb.stateChanged.connect(
            lambda s: self.baidu_pick_cb.setEnabled(bool(s)))
        # 无登录态提示：实验链路不携带浏览器登录态、不用浏览器 cookie；客户端未运行
        # 时唤起可能让它进入未登录状态。仅一行说明文字（不可编辑、不弹窗）。
        # 与上面两个子开关同样受实验总开关约束（关时一并灰掉），不引入新逻辑。
        self.share_nologin_hint = QLabel(
            "⚠ 实验性提示：该链路不携带浏览器登录态，也不使用你的浏览器 cookie。"
            "若百度网盘客户端未在运行，唤起可能让客户端进入未登录状态，届时需要重新登录。"
            "为避免这一点，自动拉起前会先检查客户端进程，未运行时将跳过并提示你。")
        self.share_nologin_hint.setWordWrap(True)
        self.share_nologin_hint.setStyleSheet(
            f"color: {PALETTE['warn_text']}; font-size: 12px;")
        exp_lay.addWidget(self.share_nologin_hint)
        self.share_nologin_hint.setEnabled(self.experimental_cb.isChecked())
        self.experimental_cb.stateChanged.connect(
            lambda s: self.share_nologin_hint.setEnabled(bool(s)))
        # 手动诊断按钮：显式、只读、一次性，不受实验开关限制（始终可用）
        diag_row = QHBoxLayout()
        self.netdisk_diag_btn = QPushButton("立即读取网盘任务库")
        self.netdisk_diag_btn.setToolTip(
            "手动执行一次只读诊断：读取网盘客户端本地任务库\n"
            "（BaiduYunGuanjia.db）并展示行数、轮询状态与活动任务。")
        self.netdisk_diag_btn.clicked.connect(self._diagnose_netdisk_db)
        diag_row.addWidget(self.netdisk_diag_btn)
        diag_row.addStretch(1)
        exp_lay.addLayout(diag_row)

        pages.append(("常规", self._page_widget(
            "常规", theme_row, interval_row, self.logcolor_cb, close_box,
            ver_box)))
        # 实验性相关 UI（实验开关分组 + 分享快捷键分组）统一归入独立的「实验性」页
        pages.append(("实验性", self._page_widget(
            "实验性", exp_box, share_hot_box)))

        # 「常规」提到最前：一打开设置页就是常规
        for i, (name, _w) in enumerate(pages):
            if name == "常规":
                pages.insert(0, pages.pop(i))
                break
        # 「实验性」紧随「常规」之后（索引 1），其余页面顺序保持不变
        for i, (name, _w) in enumerate(pages):
            if name == "实验性":
                pages.insert(1, pages.pop(i))
                break

        # 填充左侧分类列表与右侧页面栈
        for name, widget in pages:
            self._cat_list.addItem(name)
            self._stack.addWidget(widget)

    # ---------- 主题 ----------
    def _on_theme_changed(self, _idx=0):
        """切换界面主题：记住偏好 + 即时应用（auto 时才读注册表）+ 通知主窗口。"""
        try:
            val = self.theme_cb.currentData() or "auto"
            self.state.set("ui_theme", val)
            from PyQt5.QtWidgets import QApplication
            want = ui_style.resolve_theme(val)
            ui_style.apply_theme(QApplication.instance(), want)
            self.state.set("ui_theme_cached", want)
            if self._theme_cb is not None:
                self._theme_cb(want)
        except Exception as e:
            try:
                self.hub.log(f"切换主题失败: {e}")
            except Exception:
                pass

    # ---------- 网盘任务库诊断 ----------
    @staticmethod
    def _netdisk_diag_text(info):
        """把 diagnose() 的 dict 结果整理成中文可读文本。"""
        lines = [
            f"数据库路径：{info.get('db_path') or '未找到'}",
            f"选择原因：{info.get('db_source') or '—'}",
        ]
        dl = info.get("download_file_rows")
        lines.append("download_file 行数："
                     + ("读取失败" if dl is None else str(dl)))
        hl = info.get("history_rows")
        lines.append("历史行数：" + ("读取失败" if hl is None else str(hl)))
        ival = info.get("interval")
        ival_txt = f"{float(ival):g}" if isinstance(ival, (int, float)) else "未知"
        degraded = bool(info.get("degraded"))
        lines.append(f"当前轮询状态：{'降级' if degraded else '正常'}"
                     f"（间隔 {ival_txt} 秒）")
        lines.append(f"最近一次读失败原因：{info.get('error') or '无'}")
        lines.append("活动任务清单：")
        tasks = info.get("active") or []
        if not tasks:
            lines.append("  当前无活动下载任务")
        else:
            for t in tasks[:30]:
                if isinstance(t, dict):
                    path = t.get("local_path")
                    size = t.get("file_size")
                else:
                    path, size = None, None
                size_txt = f"（{size} 字节）" if size is not None else ""
                lines.append(f"  · {path or '(无路径)'}{size_txt}")
            if len(tasks) > 30:
                lines.append(f"  …等共 {len(tasks)} 个")
        if not info.get("found"):
            lines.append("\n未找到网盘任务库：实验性功能或百度网盘客户端可能不可用。")
        return "\n".join(lines)

    def _diagnose_netdisk_db(self):
        """「立即读取网盘任务库」：手动执行一次只读诊断并以只读弹窗展示。

        懒加载 baidu_task 模块，绝不因模块缺失/损坏影响设置对话框；
        整个流程包在 try/except 里，任何失败只以弹窗报告、绝不抛出。"""
        try:
            from ...baidu_task import diagnose
            from . import QPlainTextEdit  # 兼容旧模块全局补丁：从包属性动态取值（test_startup_log_brief 打桩）
            info = diagnose()
            text = self._netdisk_diag_text(info)
            try:
                # 启动日志已瘦身为一行摘要，批次/分卷/条目明细挪到这里（手动、只读）
                from ...baidu_manifest import summarize, format_summary
                s = summarize(info.get("db_path"))
                text += "\n\n" + "\n".join(
                    format_summary(s, max_batches=50, max_vols=50))
            except Exception as e:
                text += f"\n\n（批次明细读取失败：{e}）"
            dlg = QDialog(self)
            dlg.setWindowTitle("网盘任务库诊断")
            dlg.setModal(True)
            dlg.setMinimumSize(480, 320)
            dlg.resize(560, 420)
            lay = QVBoxLayout(dlg)
            lay.setContentsMargins(14, 12, 14, 12)
            lay.setSpacing(10)
            edit = QPlainTextEdit(text)
            edit.setReadOnly(True)   # 只读，长路径可滚动、可选中复制
            lay.addWidget(edit, 1)
            row = QHBoxLayout()
            row.addStretch(1)
            close_btn = QPushButton("关闭")
            close_btn.clicked.connect(dlg.accept)
            row.addWidget(close_btn)
            lay.addLayout(row)
            dlg.exec_()
        except Exception as e:
            QMessageBox.warning(
                self, "网盘任务库诊断",
                f"诊断失败：{e}\n\n实验性功能模块或百度网盘客户端可能不可用。")

    # ---------- 全局快捷键 ----------
    def _notify_hotkey_change(self):
        if self._hotkey_cb is not None:
            try:
                self._hotkey_cb()
            except Exception:
                pass

    def _on_hotkey_changed(self, combo):
        self.state.set("hotkey", combo)
        self._notify_hotkey_change()

    def _clear_hotkey(self):
        self.hotkey_edit.setText("")
        self.state.set("hotkey", "")
        self._notify_hotkey_change()

    def _on_share_hotkey_changed(self, combo):
        self.state.set("hotkey_share", combo)
        self._notify_hotkey_change()

    def _clear_share_hotkey(self):
        self.hotkey_share_edit.setText("")
        self.state.set("hotkey_share", "")
        self._notify_hotkey_change()

    def _on_share_code_hotkey_changed(self, combo):
        self.state.set("hotkey_share_code", combo)
        self._notify_hotkey_change()

    def _clear_share_code_hotkey(self):
        self.hotkey_share_code_edit.setText("")
        self.state.set("hotkey_share_code", "")
        self._notify_hotkey_change()

    # ---------- 7-Zip 管理 ----------
    def _build_sevenzip_group(self):
        self._7z_sig = _SevenZipOp()
        self._7z_sig.progress.connect(self._on_7z_progress)
        self._7z_sig.done.connect(self._on_7z_done)
        self._7z_busy = False

        box = QGroupBox("7-Zip 管理")
        gl = QVBoxLayout(box)
        gl.setSpacing(8)

        self._7z_status = QLabel()
        self._7z_status.setWordWrap(True)
        gl.addWidget(self._7z_status)

        r1 = QHBoxLayout()
        self._7z_check_btn = QPushButton("立即检查")
        self._7z_check_btn.clicked.connect(self._7z_check_now)
        self._7z_iso_btn = QPushButton("下载隔离版")
        self._7z_iso_btn.clicked.connect(lambda: self._7z_run("isolated"))
        self._7z_glob_btn = QPushButton("安装全局版")
        self._7z_glob_btn.clicked.connect(lambda: self._7z_run("global"))
        r1.addWidget(self._7z_check_btn)
        r1.addWidget(self._7z_iso_btn)
        r1.addWidget(self._7z_glob_btn)
        gl.addLayout(r1)

        r2 = QHBoxLayout()
        self._7z_uniso_btn = QPushButton("卸载隔离版")
        self._7z_uniso_btn.clicked.connect(lambda: self._7z_run("uniso"))
        self._7z_unsys_btn = QPushButton("卸载系统版")
        self._7z_unsys_btn.clicked.connect(lambda: self._7z_run("unsys"))
        r2.addWidget(self._7z_uniso_btn)
        r2.addWidget(self._7z_unsys_btn)
        r2.addStretch(1)
        gl.addLayout(r2)

        note = QLabel("版本检查仅在「首次启动」或点击「立即检查」时进行；\n"
                      "密码经 stdin 管道传给 7z，不会出现在命令行（任务管理器/WMI 看不到）。\n"
                      "隔离版安装会弹一次 UAC（官方安装器要求），但仍只写入 %APPDATA%。")
        note.setWordWrap(True)
        note.setStyleSheet(f"color: {PALETTE['muted']}; font-size: 12px;")
        gl.addWidget(note)

        self._7z_refresh_status()
        return box

    def _7z_set_btns_enabled(self, enabled):
        for b in (self._7z_check_btn, self._7z_iso_btn, self._7z_glob_btn,
                  self._7z_uniso_btn, self._7z_unsys_btn):
            b.setEnabled(enabled)

    def _7z_refresh_status(self):
        """后台探测 7-Zip 状态（不阻塞 GUI）。"""
        self._7z_status.setText("正在检查 7-Zip…")

        def worker():
            try:
                info = sevenzip_manager.check_environment()
            except Exception as e:
                self._7z_sig.progress.emit(f"检查失败：{e}")
                return
            if info["status"] == "ok":
                mode = "隔离版" if info["mode"] == "isolated" else "系统版"
                txt = (f"状态：正常（{mode} {info['version_str']}）\n"
                       f"{info['path']}")
            elif info["status"] == "low":
                mode = "隔离版" if info["mode"] == "isolated" else "系统版"
                txt = (f"状态：版本过低（{mode} {info['version_str']}）\n"
                       f"{info['path']}\n低于 "
                       f"{sevenzip_manager.MIN_VERSION[0]}.{sevenzip_manager.MIN_VERSION[1]:02d}"
                       f"，无法安全传密码，请升级或改用隔离版。")
            else:
                txt = "状态：未安装 7-Zip（仅支持 ZIP 解压）"
            self._7z_sig.progress.emit(txt)

        threading.Thread(target=worker, daemon=True).start()

    def _7z_check_now(self):
        """手动立即检查：正常则提示；缺失/过低则弹出安装引导。"""
        self._7z_refresh_status()

        def worker():
            try:
                info = sevenzip_manager.check_environment()
            except Exception as e:
                self._7z_sig.done.emit(f"检查失败：{e}", False)
                return
            if info["status"] == "ok":
                self._7z_sig.done.emit("7-Zip 版本正常，无需处理", True)
            else:
                def show():
                    dlg = SevenZipSetupDialog(self.state, self.hub, info, self)
                    dlg.exec_()
                    self._7z_refresh_status()
                QTimer.singleShot(0, show)

        threading.Thread(target=worker, daemon=True).start()

    def _7z_run(self, kind):
        if self._7z_busy:
            return
        self._7z_busy = True
        self._7z_set_btns_enabled(False)

        def _prog(s):
            self._7z_sig.progress.emit(s)

        def worker():
            try:
                if kind == "isolated":
                    p = sevenzip_manager.install_isolated(progress=_prog)
                    msg = f"隔离版安装成功：{p}"
                    self._7z_sig.done.emit(msg, True)
                elif kind == "global":
                    p = sevenzip_manager.install_global(progress=_prog)
                    msg = f"全局版安装成功：{p}"
                    self._7z_sig.done.emit(msg, True)
                elif kind == "uniso":
                    ok, msg = sevenzip_manager.uninstall_isolated()
                    self._7z_sig.done.emit(msg, ok)
                else:
                    ok, msg = sevenzip_manager.uninstall_system(progress=_prog)
                    self._7z_sig.done.emit(msg, ok)
                self.hub.log(f"7-Zip 操作完成（{kind}）")
            except Exception as e:
                self.hub.log(f"7-Zip 操作失败（{kind}）: {e}")
                self._7z_sig.done.emit(f"操作失败：{e}", False)

        threading.Thread(target=worker, daemon=True).start()

    def _on_7z_progress(self, msg):
        self._7z_status.setText(msg)

    def _on_7z_done(self, msg, ok):
        self._7z_busy = False
        self._7z_set_btns_enabled(True)
        QMessageBox.information(self, "7-Zip 管理", msg)
        self._7z_refresh_status()
