# -*- coding: utf-8 -*-
"""各类对话框：删除回溯、7-Zip 管理、设置、关闭行为、网址信任确认。"""
import threading

from PyQt5.QtWidgets import (QWidget, QVBoxLayout, QHBoxLayout, QLabel, QLineEdit, QPushButton, QCheckBox, QPlainTextEdit, QSpinBox, QMessageBox, QDialog, QTableWidget, QTableWidgetItem, QHeaderView, QAbstractItemView, QGroupBox, QRadioButton, QButtonGroup, QListWidget, QStackedWidget, QLayout)
from PyQt5.QtCore import Qt, QTimer, pyqtSignal, QObject
from PyQt5.QtGui import (QColor, QBrush)

from .. import trail as deletion_trail   # noqa: F401
from .. import sevenzip as sevenzip_manager  # noqa: F401
from .widgets import HotkeyEdit
from ..trust import trust_entry_categories

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


class DeleteTrailDialog(QDialog):
    """删除回溯：查看初始源文件记录，并从回收站还原已删除的源文件"""

    def __init__(self, parent=None):
        super().__init__(parent)
        self.setWindowTitle("删除回溯")
        self.resize(820, 520)

        lay = QVBoxLayout(self)
        lay.setContentsMargins(16, 14, 16, 14)
        lay.setSpacing(10)

        title = QLabel("删除回溯")
        title.setObjectName("appTitle")
        lay.addWidget(title)

        # 状态概览（彩色统计）
        self.stat_lbl = QLabel()
        self.stat_lbl.setObjectName("statcard")
        self.stat_lbl.setTextFormat(Qt.RichText)
        self.stat_lbl.setWordWrap(True)
        self.stat_lbl.setContentsMargins(12, 8, 12, 8)
        lay.addWidget(self.stat_lbl)

        guide = QLabel(
            "这里只记录最初始的源文件（多层解压产生的次级中间文件不会记录）。\n"
            "解压后删除的源文件先移入回收站，选中「已删除」记录可一键还原；"
            "回收站被清空后则无法还原。"
        )
        guide.setWordWrap(True)
        guide.setStyleSheet("color: #6b7688; font-size: 12px;")
        lay.addWidget(guide)

        self.table = QTableWidget(0, 4)
        self.table.setHorizontalHeaderLabels(["时间", "源文件", "状态", "说明"])
        self.table.setEditTriggers(QAbstractItemView.NoEditTriggers)
        self.table.setSelectionBehavior(QAbstractItemView.SelectRows)
        self.table.setSelectionMode(QAbstractItemView.SingleSelection)
        self.table.setAlternatingRowColors(True)
        self.table.verticalHeader().setVisible(False)
        self.table.verticalHeader().setDefaultSectionSize(30)
        self.table.horizontalHeader().setSectionResizeMode(1, QHeaderView.Stretch)
        self.table.horizontalHeader().setSectionResizeMode(3, QHeaderView.Stretch)
        lay.addWidget(self.table, 1)

        btns = QHBoxLayout()
        refresh_btn = QPushButton("刷新")
        refresh_btn.clicked.connect(self._refresh)
        restore_btn = QPushButton("还原选中")
        restore_btn.setObjectName("primary")
        restore_btn.clicked.connect(self._restore_selected)
        clear_btn = QPushButton("清空记录")
        clear_btn.setObjectName("danger")
        clear_btn.clicked.connect(self._clear_records)
        close_btn = QPushButton("关闭")
        close_btn.clicked.connect(self.accept)
        btns.addWidget(refresh_btn)
        btns.addWidget(restore_btn)
        btns.addStretch(1)
        btns.addWidget(clear_btn)
        btns.addWidget(close_btn)
        lay.addLayout(btns)

        self._refresh()

    @staticmethod
    def _status_item(status):
        """状态列：加粗 + 状态色 + 圆点，提升辨识度"""
        text = TRAIL_STATUS_TEXT.get(status, status or "—")
        color = TRAIL_STATUS_COLORS.get(status, "#555")
        item = QTableWidgetItem(f"● {text}")
        item.setForeground(QBrush(QColor(color)))
        f = item.font()
        f.setBold(True)
        item.setFont(f)
        return item

    def _refresh(self):
        self._records = deletion_trail.load_records()
        # 状态概览
        counts = {}
        for rec in self._records:
            st = rec.get("status", "")
            counts[st] = counts.get(st, 0) + 1
        parts = [f"共 <b>{len(self._records)}</b> 条"]
        for st in TRAIL_STATUS_ORDER:
            n = counts.get(st, 0)
            if n:
                color = TRAIL_STATUS_COLORS.get(st, "#555")
                label = TRAIL_STATUS_TEXT.get(st, st)
                parts.append(f'<span style="color:{color};font-weight:bold;">{label} {n}</span>')
        self.stat_lbl.setText("　·　".join(parts))
        # 表格
        self.table.setRowCount(len(self._records))
        for row, rec in enumerate(self._records):
            self.table.setItem(row, 0, QTableWidgetItem(rec.get("created_at", "")))
            name = QTableWidgetItem(rec.get("name", ""))
            self.table.setItem(row, 1, name)
            status = rec.get("status", "")
            self.table.setItem(row, 2, self._status_item(status))
            note = rec.get("note", "")
            if rec.get("failed_paths"):
                note = (note + " " if note else "") + "含无法还原的文件"
            self.table.setItem(row, 3, QTableWidgetItem(note))
        self.table.horizontalHeader().setSectionResizeMode(0, QHeaderView.ResizeToContents)
        self.table.horizontalHeader().setSectionResizeMode(2, QHeaderView.ResizeToContents)

    def _selected_record(self):
        row = self.table.currentRow()
        if 0 <= row < len(self._records):
            return self._records[row]
        return None

    def _restore_selected(self):
        rec = self._selected_record()
        if rec is None:
            QMessageBox.information(self, "删除回溯", "请先选中一条记录")
            return
        if rec.get("status") != "deleted":
            QMessageBox.information(self, "删除回溯", "只有「已删除（回收站）」状态的记录可以还原")
            return
        ok, msg = deletion_trail.restore_record(rec["id"])
        QMessageBox.information(
            self, "还原结果", msg if ok else f"还原失败\n{msg}")
        self._refresh()

    def _clear_records(self):
        if QMessageBox.question(
                self, "删除回溯", "确定清空所有回溯记录？\n（不影响回收站里的文件）",
                QMessageBox.Yes | QMessageBox.No) != QMessageBox.Yes:
            return
        deletion_trail.save_records([])
        self._refresh()


class _SevenZipOp(QObject):
    """7-Zip 后台操作信号（worker 线程 → GUI 主线程）。"""

    progress = pyqtSignal(str)
    done = pyqtSignal(str, bool)   # (消息, 是否成功)


class SevenZipSetupDialog(QDialog):
    """首次启动/手动检查发现 7-Zip 缺失或版本过低时的安装引导。

    - 隔离版：下载官方安装包静默安装到 %APPDATA%\\AutoUnpacker\\7z（不污染项目/全局）
    - 全局版：下载官方安装包安装到系统（会触发 UAC）
    - 跳过：仅使用内置 ZIP 引擎
    下载只发生在用户点击后，不捆绑任何二进制文件。"""

    def __init__(self, state, hub, info, parent=None):
        super().__init__(parent)
        self.state = state
        self.hub = hub
        self.info = info
        self._sig = _SevenZipOp()
        self._sig.progress.connect(self._on_progress)
        self._sig.done.connect(self._on_done)
        self.setWindowTitle("7-Zip 检测")
        self.setModal(True)
        self.resize(500, 300)

        lay = QVBoxLayout(self)
        lay.setContentsMargins(16, 14, 16, 14)
        lay.setSpacing(10)

        title = QLabel("需要 7-Zip 才能解压 RAR/7z/tar 等格式")
        title.setObjectName("appTitle")
        lay.addWidget(title)

        self.msg_lbl = QLabel(self._build_message(info))
        self.msg_lbl.setWordWrap(True)
        lay.addWidget(self.msg_lbl)

        self.progress_lbl = QLabel("")
        self.progress_lbl.setWordWrap(True)
        self.progress_lbl.setStyleSheet("color: #1f6feb;")
        lay.addWidget(self.progress_lbl)

        row = QHBoxLayout()
        self.isolated_btn = QPushButton("下载安装隔离版（推荐）")
        self.isolated_btn.setObjectName("primary")
        self.isolated_btn.clicked.connect(lambda: self._install("isolated"))
        self.global_btn = QPushButton("下载安装全局版")
        self.global_btn.clicked.connect(lambda: self._install("global"))
        self.skip_btn = QPushButton("跳过，仅使用 ZIP")
        self.skip_btn.clicked.connect(self.reject)
        row.addWidget(self.isolated_btn)
        row.addWidget(self.global_btn)
        row.addWidget(self.skip_btn)
        lay.addLayout(row)

        note = QLabel(
            "隔离版仅存放在 %APPDATA%\\AutoUnpacker\\7z，不污染项目目录；"
            "卸载可在「设置 → 7-Zip 管理」完成。密码经 stdin 管道传给 7z，"
            "不会出现在命令行（任务管理器/WMI 看不到）。\n"
            "注：7-Zip 官方安装器要求管理员授权，隔离版安装时也会弹出一次 UAC，"
            "确认后仍只写入 %APPDATA%（不写入 Program Files）。")
        note.setWordWrap(True)
        note.setStyleSheet("color: #6b7688; font-size: 12px;")
        lay.addWidget(note)

    def _build_message(self, info):
        if info["status"] == "low":
            mode = "隔离版" if info["mode"] == "isolated" else "系统版"
            return (f"检测到 {mode} 7-Zip 版本过低（{info['version_str']}）。\n"
                    f"该版本无法通过 stdin 安全传递密码，密码只能拼在命令行，"
                    f"会被任务管理器/WMI 窥探。\n"
                    f"请安装 7-Zip "
                    f"{sevenzip_manager.MIN_VERSION[0]}.{sevenzip_manager.MIN_VERSION[1]:02d}"
                    f" 或更高版本：")
        return ("未检测到可用的 7-Zip。\n"
                "没有 7-Zip 时只能解压 ZIP 格式（使用内置引擎）；"
                "RAR/7z/tar/gz 等格式需要 7-Zip。\n请选择安装方式：")

    def _install(self, kind):
        self._set_busy(True)

        def _prog(s):
            self._sig.progress.emit(s)

        def worker():
            try:
                if kind == "isolated":
                    p = sevenzip_manager.install_isolated(progress=_prog)
                    msg = f"隔离版安装成功：{p}"
                else:
                    p = sevenzip_manager.install_global(progress=_prog)
                    msg = f"全局版安装成功：{p}"
                self.hub.log(f"7-Zip {kind} 安装成功: {p}")
                self._sig.done.emit(msg, True)
            except Exception as e:
                self.hub.log(f"7-Zip {kind} 安装失败: {e}")
                self._sig.done.emit(f"安装失败：{e}", False)

        threading.Thread(target=worker, daemon=True).start()

    def _set_busy(self, busy):
        for b in (self.isolated_btn, self.global_btn, self.skip_btn):
            b.setEnabled(not busy)

    def _on_progress(self, s):
        self.progress_lbl.setText(s)

    def _on_done(self, msg, ok):
        self._set_busy(False)
        self.progress_lbl.setText(msg)
        QMessageBox.information(self, "7-Zip 安装", msg)
        if ok:
            self.accept()


class SettingsDialog(QDialog):
    """设置：通知开关、剪贴板联动、二维码识别、轮询间隔等，改动即时生效并保存。"""

    def __init__(self, state, hub, parent=None, on_hotkey_change=None):
        super().__init__(parent)
        self.state = state
        self.hub = hub
        self._hotkey_cb = on_hotkey_change
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

    def _on_trust_mode(self, btn):
        """「网址信任」页新域名默认行为单选：选择即写回 url_trust 配置。"""
        for val, rb in self._na_buttons.items():
            if rb is btn:
                ut = dict(self.state.snapshot().get("url_trust") or {})
                ut["new_domain_action"] = val
                self.state.set("url_trust", ut)
                break

    def _on_builtin_blacklist(self, checked):
        """「网址信任」页内置敏感地址拦截开关。"""
        ut = dict(self.state.snapshot().get("url_trust") or {})
        ut["builtin_blacklist"] = bool(checked)
        self.state.set("url_trust", ut)

    def _trust_list_editor(self, key):
        """黑白名单编辑框：每行一个域名，停止输入 400ms 后自动保存。"""
        ut = self.state.snapshot().get("url_trust") or {}
        edit = QPlainTextEdit("\n".join(str(x) for x in (ut.get(key) or [])))
        edit.setMaximumHeight(120)
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
                cur[key] = lines
                self.state.set("url_trust", cur)
            except Exception:
                pass

        timer.timeout.connect(_save)
        edit.textChanged.connect(lambda: timer.start())
        return edit

    @staticmethod
    def _page_title(text):
        lbl = QLabel(text)
        lbl.setObjectName("sectionTitle")
        return lbl

    def _page_widget(self, title, *items):
        """构建一个设置页：标题 + 若干控件/布局（布局铺满，控件靠上）。"""
        w = QWidget()
        lay = QVBoxLayout(w)
        lay.setContentsMargins(4, 0, 4, 0)
        lay.setSpacing(10)
        lay.addWidget(self._page_title(title))
        for it in items:
            if isinstance(it, QLayout):
                lay.addLayout(it)
            else:
                lay.addWidget(it)
        lay.addStretch(1)
        return w

    def _build_pages(self):
        """按分类构建页面：每页 = (分类名, 页面控件)。"""
        pages = []

        # ---------- 通知 ----------
        noti_box = QGroupBox("通知")
        nl = QVBoxLayout(noti_box)
        self.notify_cb = self._cfg_cb("notify_enabled", "总开关（关闭后不弹任何通知）", True)
        self.notify_archive_cb = self._cfg_cb("notify_archive", "发现压缩包", True)
        self.notify_success_cb = self._cfg_cb("notify_success", "解压完成", True)
        self.notify_failure_cb = self._cfg_cb("notify_failure", "解压失败", True)
        self.notify_error_cb = self._cfg_cb("notify_error", "解压出错", True)
        nl.addWidget(self.notify_cb)
        self._notify_subs = (self.notify_archive_cb, self.notify_success_cb,
                             self.notify_failure_cb, self.notify_error_cb)
        for cb in self._notify_subs:
            nl.addWidget(cb)

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
            "qr_url_redirect", "二维码链接域名重定向（drive.uc.cn → fast.uc.cn）", True)
        clip_box = QGroupBox("二维码打开网页后剪贴板联动")
        cl = QVBoxLayout(clip_box)
        self.clip_group = QButtonGroup(self)
        opts = [
            ("none", "不处理（保持剪贴板原样）"),
            ("code", "恢复「最近的非图片复制内容」（如提取码）到剪贴板"),
            ("url", "把「二维码解码出来的内容」写回剪贴板"),
        ]
        current = str(self.state.snapshot().get("qr_clipboard_action", "none"))
        for value, label in opts:
            rb = QRadioButton(label)
            rb.setChecked(value == current)
            self.clip_group.addButton(rb, opts.index((value, label)))
            cl.addWidget(rb)
        self.clip_group.buttonClicked.connect(
            lambda b: self.state.set("qr_clipboard_action",
                                     opts[self.clip_group.id(b)][0]))
        self.qr_url_cb = self._cfg_cb(
            "qr_url_enabled", "复制 http(s) 网址时自动识别二维码图片并打开", True)
        self.url_exclude_cb = self._cfg_cb(
            "url_exclude_temp_password",
            "带 :// 的网址不记录为临时密码（xxxx.com 域名形式仍记录）", True)
        pages.append(("二维码与剪贴板",
                      self._page_widget("二维码与剪贴板", self.qr_cb,
                                        self.redirect_cb, clip_box,
                                        self.qr_url_cb, self.url_exclude_cb)))

        # ---------- 网址信任 ----------
        ut = self.state.snapshot().get("url_trust") or {}
        na_box = QGroupBox("遇到未信任的新域名时（默认处理方式）")
        na_lay = QVBoxLayout(na_box)
        self._na_grp = QButtonGroup(na_box)
        self._na_grp.setExclusive(True)
        self._na_buttons = {}
        current_na = str(ut.get("new_domain_action", "ask"))
        for val, label in (("ask", "弹窗询问（每次询问，默认推荐）"),
                           ("auto_whitelist", "自动信任并打开（公网新域名自动加入白名单）"),
                           ("auto_blacklist", "自动拒绝（公网新域名自动加入黑名单）")):
            rb = QRadioButton(label)
            rb.setChecked(val == current_na)
            self._na_grp.addButton(rb)
            self._na_buttons[val] = rb
            na_lay.addWidget(rb)
        self._na_grp.buttonClicked.connect(self._on_trust_mode)

        self.builtin_cb = QCheckBox(
            "拦截内置敏感地址（私网 / 回环 / 链路本地 / 元数据 / 保留地址）")
        self.builtin_cb.setChecked(bool((ut or {}).get("builtin_blacklist", True)))
        self.builtin_cb.stateChanged.connect(self._on_builtin_blacklist)
        self.tls_cb = self._cfg_cb(
            "tls_skip_verify",
            "允许不验证 HTTPS 证书（不推荐，仅证书有问题的站点才需要）", False)

        wl_label = QLabel("白名单（每行一个域名，自动包含其全部子域；"
                          "可覆盖内置敏感地址拦截）")
        wl_label.setWordWrap(True)
        wl_label.setStyleSheet("color: #3d4756; font-size: 12px;")
        self.wl_edit = self._trust_list_editor("whitelist")
        bl_label = QLabel("黑名单（每行一个域名，优先级最高；命中即静默拒绝）")
        bl_label.setWordWrap(True)
        bl_label.setStyleSheet("color: #3d4756; font-size: 12px;")
        self.bl_edit = self._trust_list_editor("blacklist")
        note = QLabel("说明：私网 / 回环 / 链路本地 / 元数据等内置敏感地址默认拒绝，"
                      "即使选择「自动信任」也不会放行，只有手动加入白名单才会信任。")
        note.setWordWrap(True)
        note.setStyleSheet("color: #c0392b; font-size: 12px;")
        pages.append(("网址信任",
                      self._page_widget("网址信任", na_box, self.builtin_cb,
                                        self.tls_cb, wl_label, self.wl_edit,
                                        bl_label, self.bl_edit, note)))

        # ---------- 解压 ----------
        self.merge_cb = self._cfg_cb(
            "promote_merge",
            "提升时同名文件夹无文件冲突则合并（有同名文件仍重命名 (N)）", True)
        self.translate_cb = self._cfg_cb(
            "translation_move_enabled",
            "翻译 JSON 自动归位（<10MB 单 json 移入同名大文件夹，"
            "小文件夹先出现时监控 5 分钟）", True)
        pages.append(("解压", self._page_widget("解压", self.merge_cb, self.translate_cb)))

        # ---------- 全局快捷键 ----------
        hot_box = QGroupBox("全局快捷键（唤起主界面）")
        hl = QVBoxLayout(hot_box)
        hl.setSpacing(6)
        self.hotkey_enable_cb = self._cfg_cb(
            "hotkey_enabled", "启用（主界面隐藏到托盘时也能唤起）", True)
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

        # ---------- 7-Zip 管理 ----------
        pages.append(("7-Zip 管理",
                      self._page_widget("7-Zip 管理", self._build_sevenzip_group())))

        # ---------- 常规 ----------
        interval_row = QHBoxLayout()
        interval_row.addWidget(QLabel("轮询间隔(s)"))
        self.interval_spin = QSpinBox()
        self.interval_spin.setRange(1, 30)
        self.interval_spin.setValue(int(self.state.snapshot().get("poll_interval", 2)))
        self.interval_spin.valueChanged.connect(
            lambda v: self.state.set("poll_interval", int(v)))
        interval_row.addWidget(self.interval_spin)
        interval_row.addStretch(1)
        self.logcolor_cb = self._cfg_cb("log_colors_enabled", "运行日志按事件类型着色", True)

        # 关闭窗口行为（与 closeEvent 三选一弹窗联动，选择即同步到此设置）
        close_box = QGroupBox("关闭窗口行为（点击右上角 × 时）")
        close_lay = QVBoxLayout(close_box)
        self._close_grp = QButtonGroup(close_box)
        self._close_rbs = {}
        for val, label in (("ask", "每次询问（每次关闭都弹出选择）"),
                           ("tray", "隐藏到托盘（程序继续后台运行）"),
                           ("exit", "关闭程序（停止所有监听）")):
            rb = QRadioButton(label)
            self._close_grp.addButton(rb)
            self._close_rbs[val] = rb
            close_lay.addWidget(rb)
        cur = self.state.snapshot().get("close_action", "ask")
        if cur in self._close_rbs:
            self._close_rbs[cur].setChecked(True)
        self._close_grp.buttonClicked.connect(self._on_close_action)
        pages.append(("常规", self._page_widget(
            "常规", interval_row, self.logcolor_cb, close_box)))

        # 填充左侧分类列表与右侧页面栈
        for name, widget in pages:
            self._cat_list.addItem(name)
            self._stack.addWidget(widget)

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
        note.setStyleSheet("color: #6b7688; font-size: 12px;")
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


class CloseActionDialog(QDialog):
    """关闭主界面弹窗：二选一（关闭程序 / 隐藏到托盘）+「不再提示」勾选。

    用普通 QDialog 而非 QMessageBox：QMessageBox 在 Windows 上会把标题栏
    关闭键（X）禁用，用户无法取消；QDialog 显式带上 WindowCloseButtonHint
    后 X 可用——点 X / 按 Esc 即取消（返回 None），关闭操作中止、主界面
    保持原样，绝不强迫二选一。"""

    def __init__(self, parent=None):
        super().__init__(parent)
        self.setWindowTitle("关闭主界面")
        self.setWindowFlags(Qt.Dialog | Qt.WindowTitleHint
                            | Qt.WindowSystemMenuHint | Qt.WindowCloseButtonHint)
        self.setModal(True)
        self.resize(460, 270)

        lay = QVBoxLayout(self)
        lay.setContentsMargins(18, 16, 18, 14)
        lay.setSpacing(10)

        title = QLabel("关闭主界面后，希望程序如何运行？")
        title.setObjectName("appTitle")
        lay.addWidget(title)

        info = QLabel(
            "程序的核心工作在后台完成，关闭窗口并不等于停止服务。\n\n"
            "· 隐藏到托盘 —— 继续后台监听，双击托盘图标可随时恢复；\n"
            "· 关闭程序 —— 停止所有监听与剪贴板监控。\n\n"
            "勾选「不再提示」后，本次选择将保存为以后的默认行为；\n"
            "不勾选则仅本次生效，下次关闭仍会询问。\n"
            "如误触关闭，按 Esc 或点标题栏 × 即可取消。")
        info.setWordWrap(True)
        info.setStyleSheet("color: #3d4756; font-size: 13px;")
        lay.addWidget(info)

        self.remember_cb = QCheckBox("不再提示（保存本次选择为默认行为）")
        lay.addWidget(self.remember_cb)

        btns = QHBoxLayout()
        btns.addStretch(1)
        exit_btn = QPushButton("关闭程序")
        exit_btn.setObjectName("danger")
        exit_btn.clicked.connect(self._choose_exit)
        tray_btn = QPushButton("隐藏到托盘")
        tray_btn.setObjectName("primary")
        tray_btn.setDefault(True)
        tray_btn.clicked.connect(self._choose_tray)
        btns.addWidget(exit_btn)
        btns.addWidget(tray_btn)
        lay.addLayout(btns)

        self._result = None   # "exit" / "tray" / None=取消

    def _choose_exit(self):
        self._result = "exit"
        self.accept()

    def _choose_tray(self):
        self._result = "tray"
        self.accept()

    @staticmethod
    def ask(parent=None):
        dlg = CloseActionDialog(parent)
        dlg.exec_()
        return dlg._result, dlg.remember_cb.isChecked()


class TrustAskDialog(QDialog):
    """网址信任询问弹窗（非置顶、不抢焦点）。

    后台线程识别到「未信任的域名即将被自动访问/自动打开」时，由主窗口弹出
    本弹窗让用户决定。以 show() 非模态展示，不 raise_()/activateWindow()，
    绝不打断用户当前操作；主窗口隐藏时请求进入挂起队列，等主界面可见再弹
    （无限挂起，不丢请求）。按 X / Esc 关闭等价「拒绝打开」。

    四选一：
    - open_once  本次打开（不持久化，本次执行）
    - trust      永久信任（写入白名单，含子域，本次执行）
    - deny_once  拒绝打开（不持久化，本次跳过）
    - block      永久拒绝（写入黑名单，含子域，本次跳过）
    """

    def __init__(self, parent, url, host, category, purpose):
        super().__init__(parent)
        self.on_decision = None   # 由 MainWindow 注入：def (decision)
        self._decision = "deny_once"   # 默认按拒绝处理（X/Esc/异常关闭不执行）
        self.setWindowTitle("网址信任确认")
        self.setWindowFlags(Qt.Dialog | Qt.WindowTitleHint
                            | Qt.WindowSystemMenuHint | Qt.WindowCloseButtonHint)
        self.setModal(False)
        self.resize(520, 300)

        lay = QVBoxLayout(self)
        lay.setContentsMargins(18, 16, 18, 14)
        lay.setSpacing(10)

        title = QLabel("识别到新网址，需要你确认")
        title.setObjectName("appTitle")
        lay.addWidget(title)

        url_edit = QLineEdit(url)
        url_edit.setReadOnly(True)
        lay.addWidget(url_edit)

        # 风险标注：内置黑名单类别（内网/回环/元数据等）红色警告
        risk = trust_entry_categories(host)
        if risk and risk != "public":
            warn = QLabel(
                "⚠ 该地址属于「内网 / 回环 / 链路本地 / 保留地址」等敏感类别，\n"
                "自动访问或打开可能带来安全风险。请确认是否真的信任它。")
            warn.setWordWrap(True)
            warn.setStyleSheet(
                "color: #c0392b; background: #ffe4e4; border: 1px solid #f2c2c2;"
                " border-radius: 6px; padding: 8px;")
            lay.addWidget(warn)

        verb = "自动访问（下载内容以识别二维码图片）" if purpose == "fetch" \
            else "自动在浏览器中打开"
        info = QLabel(
            f"程序即将{verb}该网址。\n\n"
            "· 本次打开 —— 仅此一次，下次仍会询问；\n"
            "· 永久信任 —— 加入白名单（含其全部子域），此后静默自动处理；\n"
            "· 拒绝打开 —— 仅此一次跳过；\n"
            "· 永久拒绝 —— 加入黑名单（含其全部子域），此后静默拒绝。\n\n"
            "如不确定，建议选择「拒绝打开」。按 Esc / 标题栏 × 等同于拒绝。")
        info.setWordWrap(True)
        info.setStyleSheet("color: #3d4756; font-size: 13px;")
        lay.addWidget(info)

        row1 = QHBoxLayout()
        row1.addStretch(1)
        open_btn = QPushButton("本次打开")
        open_btn.setObjectName("primary")
        open_btn.clicked.connect(lambda: self._choose("open_once"))
        trust_btn = QPushButton("永久信任")
        trust_btn.clicked.connect(lambda: self._choose("trust"))
        row1.addWidget(open_btn)
        row1.addWidget(trust_btn)
        lay.addLayout(row1)

        row2 = QHBoxLayout()
        row2.addStretch(1)
        deny_btn = QPushButton("拒绝打开")
        deny_btn.setDefault(True)
        deny_btn.clicked.connect(lambda: self._choose("deny_once"))
        block_btn = QPushButton("永久拒绝")
        block_btn.setObjectName("danger")
        block_btn.clicked.connect(lambda: self._choose("block"))
        row2.addWidget(deny_btn)
        row2.addWidget(block_btn)
        lay.addLayout(row2)

    def _choose(self, decision):
        self._decision = decision
        self.accept()

    def done(self, r):
        super().done(r)
        cb = self.on_decision
        self.on_decision = None
        if cb is not None:
            try:
                cb(self._decision)
            except Exception:
                pass


