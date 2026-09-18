# -*- coding: utf-8 -*-
"""各类对话框：删除回溯、7-Zip 管理、设置、关闭行为确认、网址信任确认、目录设置。

职责：- DeleteTrailDialog 展示删除回溯记录并一键还原
- SevenZipSetupDialog 检测/安装/卸载 7-Zip（隔离版与全局版）
- SettingsDialog 全部配置项编辑（监听路径、通知、信任名单、快捷键等）
- CloseActionDialog 关闭行为询问；TrustAskDialog 新网址信任确认
- ShareCodeAskDialog 分享缺提取码时贴主窗右缘的非阻塞取码小窗（120s 到点关闭作废）
- WatchDirDialog 目录设置弹窗（对应原型 12；监听模式为两张平铺卡，严禁下拉框）
关键入口：SettingsDialog / SevenZipSetupDialog / DeleteTrailDialog / TrustAskDialog /
          ShareCodeAskDialog / WatchDirDialog
依赖：PyQt5、trail、sevenzip、trust、widgets
注意：7-Zip 安装/卸载在后台线程执行（_SevenZipOp），UI 仅投递任务
"""
import inspect
import re
import threading

from PyQt5.QtWidgets import (QWidget, QVBoxLayout, QHBoxLayout, QLabel, QLineEdit, QPushButton, QCheckBox, QPlainTextEdit, QSpinBox, QMessageBox, QDialog, QTableWidget, QTableWidgetItem, QHeaderView, QAbstractItemView, QGroupBox, QRadioButton, QButtonGroup, QListWidget, QStackedWidget, QLayout, QComboBox, QScrollArea, QFrame, QApplication, QShortcut, QFileDialog, QProgressBar)
from PyQt5.QtCore import (Qt, QTimer, pyqtSignal, QObject, QRect, QRegularExpression,
                          QEvent, QPoint, QPropertyAnimation, QEasingCurve)
from PyQt5.QtGui import (QColor, QBrush, QKeySequence, QRegularExpressionValidator)

from .. import trail as deletion_trail   # noqa: F401
from .. import sevenzip as sevenzip_manager  # noqa: F401
from .widgets import (HotkeyEdit, TRAIL_STATUS_COLORS, Glyph, LayoutButton,
                      ModeSelector, DIR_STATE_TEXT, dir_state_key)
from .style import PALETTE
from . import style as ui_style
from ..trust import trust_entry_categories

TRAIL_STATUS_TEXT = {
    "recorded": "已记录（处理中）",
    "kept": "未删除",
    "deleted": "已删除（回收站）",
    "restored": "已还原",
    "failed": "解压失败",
}

# 状态颜色统一由 widgets/style 提供（TRAIL_STATUS_COLORS = PALETTE["trail"]，
# 与主题同一对象，切换主题后就地更新），此处不再重复定义。

TRAIL_STATUS_ORDER = ["deleted", "restored", "kept", "failed", "recorded"]

# 分享缺提取码取码小窗的超时（秒）：到点自动关闭并丢弃框内内容（不回调）。
SHARE_ASK_TIMEOUT_SEC = 120

# 取码小窗几何常量（px）：
# SHARE_ASK_EDGE_MARGIN —— 与**主窗右缘**的间隙（贴着主窗、略向外一点）；
#   主窗不可用（无父 / 测试桩直接构造）时，退回旧行为「距屏幕可用区右边距」。
# SHARE_ASK_WINDOW_WIDTH —— 小窗固定宽度。
SHARE_ASK_EDGE_MARGIN = 16
SHARE_ASK_WINDOW_WIDTH = 300


def _call_decision(cb, kind, code, url, surl, share_uk):
    """按注入 callable 可接受的位置参数个数回调 on_decision，兼容旧/新签名。

    冻结词表：kind ∈ {"mapped", "once", "ignore"}（ignore 时 code 为空串）。
    - 新接线：cb(kind, code, url, surl, share_uk)
    - 旧接线（仍闭包 url/surl/uk 的 2 参 lambda）：cb(kind, code)
    """
    try:
        params = list(inspect.signature(cb).parameters.values())
        positional = sum(1 for p in params if p.kind in (
            p.POSITIONAL_ONLY, p.POSITIONAL_OR_KEYWORD))
        var = any(p.kind == p.VAR_POSITIONAL for p in params)
        use5 = var or positional >= 5
    except (TypeError, ValueError):
        use5 = True
    if use5:
        cb(kind, code, url, surl, share_uk)
    else:
        cb(kind, code)


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
        guide.setStyleSheet(f"color: {PALETTE['muted']}; font-size: 12px;")
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
        self.progress_lbl.setStyleSheet(f"color: {PALETTE['accent_text']};")
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
        note.setStyleSheet(f"color: {PALETTE['muted']}; font-size: 12px;")
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
        from .. import updater
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
        from .. import updater
        self._check_btn.setEnabled(True)
        status, latest = self._update_result_cache
        try:
            from .. import __version__ as _ver
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
        from .. import updater
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
        current = str((sub or {}).get("new_domain_action", "none"))
        for val, label, t in (
                ("none", "无操作（默认）",
                 "不打开、不询问、也不记录，静默跳过。"),
                ("ask", "弹窗询问",
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
            from .. import __version__ as _ver
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
            from ..baidu_task import diagnose
            info = diagnose()
            text = self._netdisk_diag_text(info)
            try:
                # 启动日志已瘦身为一行摘要，批次/分卷/条目明细挪到这里（手动、只读）
                from ..baidu_manifest import summarize, format_summary
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
        info.setStyleSheet(f"color: {PALETTE['muted2']}; font-size: 13px;")
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
                f"color: {PALETTE['warn_text']}; background: {PALETTE['warn_bg']}; "
                f"border: 1px solid {PALETTE['warn_border']};"
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
        info.setStyleSheet(f"color: {PALETTE['muted2']}; font-size: 13px;")
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


class _CodeLineEdit(QLineEdit):
    """4 位提取码输入框：额外记住「setText 被 maxLength 截断」的越界输入。

    QLineEdit.setText() 不经过校验器，且按 maxLength 静默截断（"abcde" ->
    "abcd"），于是越界输入在框内看起来像合法 4 位码。这里在截断发生前记录
    越界标记，供 current_code() 判为非法；用户实际键入/粘贴会清掉该标记，
    回到正常校验路径（不改变可见外观与既有控件风格）。
    """

    def __init__(self, parent=None):
        super().__init__(parent)
        self._overlong = False

    def setText(self, text):
        raw = str(text or "")
        ml = self.maxLength()
        # 先置标记再 super().setText()：textChanged 监听者能立刻读到最终状态
        self._overlong = bool(ml >= 0 and len(raw) > ml)
        super().setText(raw)

    def keyPressEvent(self, event):
        self._overlong = False
        super().keyPressEvent(event)

    def is_overlong(self):
        return self._overlong


class ShareCodeAskDialog(QDialog):
    """分享缺提取码时贴在主窗右缘的非阻塞取码小窗（120s 到点自动关闭丢弃）。

    识别到分享链接、但剪贴板附近没有有效提取码时，由调用方用 show() 展示：
    挂在**主窗上的子工具窗**（Qt.Tool | FramelessWindowHint，不做全局置顶），
    贴着主窗右缘滑出、跟随主窗移动/缩放；主窗隐藏/最小化时一并隐藏——那时
    调用方根本不会创建本窗（改为日志 + 托盘气泡，见 main_window）。不进任务栏、
    不抢焦点（不 raise_() / 不 activateWindow()，且设 WA_ShowWithoutActivating）。
    默认 120 秒倒计时，逐秒可见（「剩余 Ns」）；到点只关闭并丢弃框内内容，绝不
    自动用框里的码发起任何操作。

    三个按钮即回调词表（语义见下），一律经 _finish 恰好回调一次，回调不向外抛异常：
      「本次使用」   -> on_decision("once", code)
      「绑定并下载」 -> on_decision("mapped", code)
      「忽略」/关闭  -> on_decision("ignore", "")

    只读访问器（供接线侧读取本窗当前状态）：
      current_code() -> 通过校验的 4 位码，否则 ""
      target_surl() / target_uk() -> 本窗对应的 surl / share_uk
    本类只发回调、绝不写库；持久化由调用方负责。
    """

    def __init__(self, parent, surl, url, share_uk, mapped_code="",
                 timeout_sec=SHARE_ASK_TIMEOUT_SEC, on_decision=None,
                 state=None, hub=None):
        # UX-5：挂到传进来的主窗上（子工具窗，随主窗移动/隐藏）。非 QWidget
        # （既有测试桩）按无父处理；无父时几何退回旧「贴屏幕右缘」行为。
        p = parent if isinstance(parent, QWidget) else None
        super().__init__(p)
        self.surl = str(surl or "").strip()
        self.url = str(url or "").strip()
        self.share_uk = str(share_uk or "").strip()
        self.on_decision = on_decision   # 由调用方注入：def (kind, code[, url, surl, uk])
        self._state = state
        self._hub = hub
        self._done = False
        self._timed_out = False
        try:
            self._timeout_sec = max(1, int(timeout_sec))
        except Exception:
            self._timeout_sec = SHARE_ASK_TIMEOUT_SEC
        self._remain_sec = self._timeout_sec

        self.setWindowTitle("分享缺提取码")
        # 子工具窗 + 无边框：父窗存在时始终位于主窗之上（但不越过整个桌面）；
        # UX-5 明确去掉 WindowStaysOnTopHint。不抢焦点：不 raise_/activateWindow，
        # 且 WA_ShowWithoutActivating 保证 show() 不激活本窗。
        self.setWindowFlags(Qt.Tool | Qt.FramelessWindowHint)
        self.setAttribute(Qt.WA_ShowWithoutActivating, True)
        self.setModal(False)   # 沿用本项目「非阻塞提示」做法，绝不 exec_()

        root = QVBoxLayout(self)
        root.setContentsMargins(0, 0, 0, 0)
        root.setSpacing(0)
        card = QFrame()
        card.setObjectName("card")
        root.addWidget(card)

        lay = QVBoxLayout(card)
        lay.setContentsMargins(12, 10, 12, 12)
        lay.setSpacing(8)

        # 标题行：标题 + 逐秒倒计时 + 关闭（等价「忽略」）
        head = QHBoxLayout()
        head.setSpacing(6)
        title = QLabel("分享缺提取码")
        title.setStyleSheet(
            f"color: {PALETTE['accent_text']}; font-weight: bold; font-size: 14px;")
        head.addWidget(title)
        head.addStretch(1)
        self.timeout_label = QLabel(f"剩余 {self._remain_sec}s")
        self.timeout_label.setStyleSheet(
            f"color: {PALETTE['muted']}; font-size: 12px;")
        head.addWidget(self.timeout_label)
        close_btn = QPushButton("✕")
        close_btn.setFixedSize(22, 22)
        close_btn.setToolTip("忽略并关闭（Esc）")
        close_btn.setStyleSheet(
            "QPushButton { border: none; background: transparent; padding: 0;"
            f" color: {PALETTE['muted']}; font-size: 13px; }}"
            f"QPushButton:hover {{ color: {PALETTE['danger']}; }}")
        close_btn.clicked.connect(self._on_ignore_clicked)
        head.addWidget(close_btn)
        lay.addLayout(head)

        meta = QLabel(f"分享者：{self.share_uk or '未知'}")
        meta.setStyleSheet(f"color: {PALETTE['muted2']}; font-size: 12px;")
        meta.setTextInteractionFlags(Qt.TextSelectableByMouse)
        lay.addWidget(meta)

        # 链接太长时中间截断，完整链接放 tooltip（可选中复制）
        url_label = QLabel()
        url_label.setTextInteractionFlags(Qt.TextSelectableByMouse)
        url_label.setStyleSheet(f"color: {PALETTE['muted']}; font-size: 12px;")
        url_label.setText("链接：" + self.fontMetrics().elidedText(
            self.url, Qt.ElideMiddle, SHARE_ASK_WINDOW_WIDTH - 60))
        url_label.setToolTip(self.url)
        lay.addWidget(url_label)

        code_cap = QLabel("提取码（4 位）")
        code_cap.setStyleSheet(
            f"color: {PALETTE['muted2']}; font-size: 12px;")
        lay.addWidget(code_cap)

        self.code_edit = _CodeLineEdit()
        self.code_edit.setPlaceholderText("请输入 4 位提取码")
        self.code_edit.setMaxLength(4)
        self.code_edit.setValidator(QRegularExpressionValidator(
            QRegularExpression("[A-Za-z0-9]{0,4}"), self))
        prefill = str(mapped_code or "").strip()
        if prefill:
            self.code_edit.setText(prefill)
        lay.addWidget(self.code_edit)

        self.hint_label = QLabel("请输入 4 位提取码（字母或数字）")
        self.hint_label.setWordWrap(True)
        self.hint_label.setStyleSheet(
            f"color: {PALETTE['muted']}; font-size: 12px;")
        lay.addWidget(self.hint_label)

        self.once_btn = QPushButton("本次使用（Alt+2）")
        self.once_btn.setObjectName("primary")
        self.once_btn.clicked.connect(self._on_once_clicked)
        lay.addWidget(self.once_btn)

        self.mapped_btn = QPushButton("绑定并下载（Alt+3）")
        self.mapped_btn.clicked.connect(self._on_mapped_clicked)
        lay.addWidget(self.mapped_btn)

        foot = QHBoxLayout()
        foot.addStretch(1)
        self.ignore_btn = QPushButton("忽略")
        self.ignore_btn.clicked.connect(self._on_ignore_clicked)
        foot.addWidget(self.ignore_btn)
        lay.addLayout(foot)

        # 码无效时两个下载按钮置灰（有效即恢复），行内提示随状态变色
        self.code_edit.textChanged.connect(self._refresh_state)
        self._refresh_state()

        # Alt+2 / Alt+3：鼠标路径的键盘等价（仅本窗激活时生效，不注册全局热键）
        QShortcut(QKeySequence("Alt+2"), self).activated.connect(
            self._on_once_clicked)
        QShortcut(QKeySequence("Alt+3"), self).activated.connect(
            self._on_mapped_clicked)

        # 逐秒倒计时：单个 1s 重复 QTimer；到点走 _on_timeout（只关闭、不回调）
        self._timer = QTimer(self)
        self._timer.setInterval(1000)
        self._timer.setSingleShot(False)
        self._timer.timeout.connect(self._tick)
        self._timer.start()

        self.setFixedWidth(SHARE_ASK_WINDOW_WIDTH)
        # UX-5：跟随父窗 Move/Resize 的锚定状态（showEvent 安装、hideEvent 卸载，
        # 绝不留悬挂过滤器；滑出动画引用保存在 _slide_anim，随控件一起销毁）。
        self._anchor_parent = None
        self._slide_anim = None
        self._place_beside_parent()

    # ---- 只读访问器（供接线侧读取本窗当前状态；命名不得更改）----
    def current_code(self):
        """返回框内通过校验的 4 位码（字母/数字），否则返回空串。"""
        txt = self.code_edit.text().strip()
        if getattr(self.code_edit, "is_overlong", lambda: False)():
            return ""
        if re.fullmatch(r"[A-Za-z0-9]{4}", txt):
            return txt
        return ""

    def target_surl(self):
        """本窗对应的 surl（可能为空串）。"""
        return self.surl

    def target_uk(self):
        """本窗对应的 share_uk（可能为空串）。"""
        return self.share_uk

    # ---- 状态刷新 / 按钮入口 ----
    def _refresh_state(self):
        """按框内内容刷新两个下载按钮可用态与行内提示（码无效即置灰）。"""
        raw = self.code_edit.text().strip()
        valid = bool(self.current_code())
        self.once_btn.setEnabled(valid)
        self.mapped_btn.setEnabled(valid)
        if not raw:
            self.hint_label.setText("请输入 4 位提取码（字母或数字）")
            self.hint_label.setStyleSheet(
                f"color: {PALETTE['muted']}; font-size: 12px;")
        elif valid:
            self.hint_label.setText("提取码格式有效")
            self.hint_label.setStyleSheet(
                f"color: {PALETTE['success']}; font-size: 12px;")
        else:
            self.hint_label.setText("提取码需为 4 位字母或数字")
            self.hint_label.setStyleSheet(
                f"color: {PALETTE['danger']}; font-size: 12px;")

    def _submit(self, kind):
        """按钮统一入口：码有效才提交（无效仅刷新提示，不回调）。"""
        if self._done or self._timed_out:
            return
        code = self.current_code()
        if not code:
            self._refresh_state()
            return
        self._finish(kind, code)
        self.close()

    def _on_once_clicked(self):
        self._submit("once")

    def _on_mapped_clicked(self):
        self._submit("mapped")

    def _on_ignore_clicked(self):
        if self._done or self._timed_out:
            return
        self._finish("ignore", "")
        self.close()

    # ---- 倒计时 / 超时 ----
    def _update_countdown(self):
        try:
            self.timeout_label.setText(f"剩余 {self._remain_sec}s")
        except Exception:
            pass

    def _tick(self):
        """逐秒递减；归零即走超时关闭（关闭 + 丢弃，不回调）。"""
        if self._done or self._timed_out:
            return
        self._remain_sec = max(0, self._remain_sec - 1)
        self._update_countdown()
        if self._remain_sec <= 0:
            self._on_timeout()

    def _on_timeout(self):
        """到点：停表、记一行日志、关闭并丢弃框内内容——绝不回调 on_decision。"""
        if self._done or self._timed_out:
            return
        self._timed_out = True
        self._remain_sec = 0
        self._update_countdown()
        try:
            self._timer.stop()
        except Exception:
            pass
        try:
            if self._hub is not None:
                self._hub.log(
                    f"分享询问超时({self._timeout_sec}s)，已关闭丢弃: {self.surl}")
        except Exception:
            pass
        self.close()

    def _finish(self, kind, code):
        """统一出口：on_decision 只回调一次，并停掉倒计时计时器。"""
        if self._done:
            return
        self._done = True
        try:
            self._timer.stop()
        except Exception:
            pass
        cb = self.on_decision
        self.on_decision = None
        if cb is not None:
            try:
                _call_decision(cb, kind, code, self.url, self.surl, self.share_uk)
            except Exception:
                pass

    def reject(self):
        # Esc 关闭：等价「忽略」（超时/已作答时不再重复回调）
        if not self._timed_out:
            self._finish("ignore", "")
        super().reject()

    def closeEvent(self, event):
        # 关闭按钮 / 代码关闭：等价「忽略」；超时关闭已置 _timed_out，不再回调
        if not self._timed_out:
            self._finish("ignore", "")
        super().closeEvent(event)

    # ---- 几何：优先贴主窗右缘（跟随主窗）；无父时退回旧「贴屏幕右缘」 ----
    def _available_geometry(self):
        try:
            scr = QApplication.primaryScreen()
            if scr is not None:
                return scr.availableGeometry()
        except Exception:
            pass
        return QRect(0, 0, 1280, 800)

    def _screen_geometry_for(self, widget):
        """widget 所在屏幕的可用区；取不到时退回主屏（_available_geometry）。"""
        try:
            scr = widget.screen()
            if scr is not None:
                return scr.availableGeometry()
        except Exception:
            pass
        return self._available_geometry()

    def _place_right_edge(self):
        """无父回退：贴屏幕可用区右缘、纵向居中（避开底部托盘区）。"""
        try:
            geo = self._available_geometry()
            self.layout().activate()
            self.adjustSize()
            w = self.width()
            h = self.height()
            x = geo.x() + geo.width() - w - SHARE_ASK_EDGE_MARGIN
            y = geo.y() + max(SHARE_ASK_EDGE_MARGIN, (geo.height() - h) // 2)
            self.move(x, y)
        except Exception:
            pass

    def _parent_target_pos(self):
        """小窗目标全局位置：贴主窗右缘、与主窗顶部对齐，再收进屏幕可用区。

        返回 QPoint；无父（或父窗几何不可读）返回 None，由调用方走 _place_right_edge。"""
        p = self.parentWidget()
        if p is None:
            return None
        try:
            fg = p.frameGeometry()
            if fg.width() <= 0 or fg.height() <= 0:
                fg = p.geometry()
        except Exception:
            return None
        try:
            self.layout().activate()
            self.adjustSize()
        except Exception:
            pass
        w, h = self.width(), self.height()
        x = fg.x() + fg.width() + SHARE_ASK_EDGE_MARGIN
        y = fg.y() + SHARE_ASK_EDGE_MARGIN
        geo = self._screen_geometry_for(p)
        if geo is not None:
            x = min(x, geo.x() + geo.width() - w)
            y = min(y, geo.y() + geo.height() - h)
            x = max(geo.x(), x)
            y = max(geo.y(), y)
        return QPoint(x, y)

    def _place_beside_parent(self, animate=False):
        """把窗摆到主窗右缘；animate=True 时用约 160ms 的「从主窗右缘滑出」动画。

        起始点刻意放在目标点**左侧**（叠进主窗右缘约 12px），再向右滑到贴边位置：
        视觉上是「从程序右侧平移出来」，而不是从屏幕外侧滑进来。"""
        target = self._parent_target_pos()
        if target is None:
            self._place_right_edge()
            return
        if animate:
            try:
                start = QPoint(target.x() - 28, target.y())
                self.move(start)
                anim = QPropertyAnimation(self, b"pos", self)
                anim.setDuration(160)
                anim.setEasingCurve(QEasingCurve.OutCubic)
                anim.setStartValue(start)
                anim.setEndValue(target)
                self._slide_anim = anim
                anim.start()
                return
            except Exception:
                pass
        try:
            self.move(target)
        except Exception:
            pass

    # ---- 跟随主窗：Move/Resize 重锚定；主窗隐藏/最小化则一并隐藏 ----
    def _install_parent_filter(self):
        """在父窗上安装事件过滤器（重复安装前先移除，绝不叠加/悬挂）。"""
        p = self.parentWidget()
        if p is None or self._anchor_parent is p:
            return
        self._remove_parent_filter()
        try:
            p.installEventFilter(self)
            self._anchor_parent = p
        except Exception:
            self._anchor_parent = None

    def _remove_parent_filter(self):
        p = self._anchor_parent
        self._anchor_parent = None
        if p is None:
            return
        try:
            p.removeEventFilter(self)
        except Exception:
            pass

    def showEvent(self, event):
        super().showEvent(event)
        try:
            self._install_parent_filter()
        except Exception:
            pass
        try:
            self._place_beside_parent(animate=True)
        except Exception:
            pass

    def hideEvent(self, event):
        try:
            self._remove_parent_filter()
        except Exception:
            pass
        super().hideEvent(event)

    def eventFilter(self, obj, event):
        """父窗事件：移动/缩放即重锚定；父窗隐藏/最小化则把自己也藏起来。"""
        try:
            p = self._anchor_parent
            if p is not None and obj is p:
                et = event.type()
                if et in (QEvent.Move, QEvent.Resize):
                    if self.isVisible():
                        self._place_beside_parent(animate=False)
                elif et == QEvent.WindowStateChange:
                    if p.isMinimized() or not p.isVisible():
                        self.hide()
                elif et == QEvent.Hide:
                    self.hide()
        except Exception:
            pass
        return super().eventFilter(obj, event)


class WatchDirDialog(QDialog):
    """目录设置弹窗（对应原型 12）：宽 600、模态、居中于父窗口。

    由目录胶囊点击后打开：`WatchDirDialog(state, idx, parent)`。结构：
    - 头部：目录图标 + 标题 + 等宽路径 + 实时状态徽标 + 关闭；
    - 表单：监听路径 / 解压到 / 监听模式（**两张平铺卡，严禁 QComboBox**）/
      启用监听 + 解压成功后删除源文件 / 回收站说明 /「当前正在处理」卡片；
    - 底部：移除目录（危险，左）+ 取消 / 保存（右）。

    保存走现有通道：变动的字段逐个 `state.update_path(idx, field, value)`，
    随后 emit saved(idx)。「移除目录」只 emit removeRequested(idx)——
    二次确认由宿主负责，本弹窗绝不弹确认框。Esc / 取消 = reject()。

    遮罩：项目没有 dim-mask 原语，这里用父窗口的一个 rgba(0,0,0,.34) 子控件
    自带实现（showEvent 建、hideEvent/closeEvent 拆；无父窗口时自动忽略），
    全部包在 try/except 中，任何失败都不影响弹窗本身。
    """

    saved = pyqtSignal(int)
    removeRequested = pyqtSignal(int)

    def __init__(self, state, idx, parent=None):
        super().__init__(parent)
        self.state = state
        self.idx = int(idx)
        self._scrim = None
        self._orig = self._load_entry()
        self._state_key = dir_state_key(self._orig.get("state") or "listening")
        self._progress = None
        try:
            if self._orig.get("progress") is not None:
                self._progress = int(round(float(self._orig["progress"])))
        except Exception:
            self._progress = None
        self._current_name = ""
        self._current_layer = None

        self.setWindowTitle("目录设置")
        self.setModal(True)
        self.setFixedWidth(600)

        root = QVBoxLayout(self)
        root.setContentsMargins(0, 0, 0, 0)
        root.setSpacing(0)
        root.addWidget(self._build_head())
        body = QWidget(self)
        body_lay = QVBoxLayout(body)
        body_lay.setContentsMargins(16, 14, 16, 14)
        body_lay.setSpacing(11)
        self._build_body(body_lay)
        root.addWidget(body)
        root.addWidget(self._build_foot())
        self._refresh_state_badge()
        self._render_current()

    # ---- 读取入口 ----
    def _load_entry(self):
        """取当前 entry：优先 state.snapshot()，退化到 state.cfg；异常一律空字典。"""
        for getter in (lambda: self.state.snapshot(),
                       lambda: getattr(self.state, "cfg", {})):
            try:
                data = getter() or {}
                paths_cfg = data.get("watch_paths") or []
                if 0 <= self.idx < len(paths_cfg) and isinstance(paths_cfg[self.idx], dict):
                    return dict(paths_cfg[self.idx])
            except Exception:
                continue
        return {}

    def _field_label(self, text):
        lbl = QLabel(text, self)
        lbl.setObjectName("fLabel")
        lbl.setFixedWidth(62)
        return lbl

    # ---- 头部 / 表单 / 底部 ----
    def _build_head(self):
        head = QFrame(self)
        head.setObjectName("dlgHead")
        lay = QHBoxLayout(head)
        lay.setContentsMargins(16, 14, 16, 12)
        lay.setSpacing(10)
        lay.addWidget(Glyph("folder", head, 20, role="accent"))
        title = QLabel("目录设置", head)
        title.setObjectName("dTitle")
        lay.addWidget(title)
        full_path = str(self._orig.get("path") or "")
        self.path_label = QLabel(head)
        self.path_label.setObjectName("dlgPath")
        try:
            self.path_label.setText(self.path_label.fontMetrics().elidedText(
                full_path or "—", Qt.ElideMiddle, 240))
        except Exception:
            self.path_label.setText(full_path or "—")
        self.path_label.setToolTip(full_path)
        lay.addWidget(self.path_label)
        lay.addStretch(1)
        self.state_badge = QLabel(head)
        self.state_badge.setObjectName("dlgState")
        lay.addWidget(self.state_badge)
        close_btn = QPushButton(head)
        close_btn.setObjectName("iconBtn")
        close_btn.setFixedSize(30, 30)
        close_btn.setToolTip("关闭")
        close_btn.setCursor(Qt.PointingHandCursor)
        close_lay = QHBoxLayout(close_btn)
        close_lay.setContentsMargins(0, 0, 0, 0)
        close_lay.addWidget(Glyph("close", close_btn, 16), 0, Qt.AlignCenter)
        close_btn.clicked.connect(self.reject)
        lay.addWidget(close_btn)
        return head

    def _build_body(self, lay):
        # 监听路径
        row1 = QHBoxLayout()
        row1.setSpacing(9)
        row1.addWidget(self._field_label("监听路径"))
        self.path_edit = QLineEdit(str(self._orig.get("path") or ""), self)
        self.path_edit.setPlaceholderText("监听目录路径")
        row1.addWidget(self.path_edit, 1)
        browse1 = QPushButton("浏览", self)
        browse1.clicked.connect(self._browse_path)
        row1.addWidget(browse1)
        lay.addLayout(row1)

        # 解压到
        row2 = QHBoxLayout()
        row2.setSpacing(9)
        row2.addWidget(self._field_label("解压到"))
        self.out_edit = QLineEdit(str(self._orig.get("output_dir") or ""), self)
        self.out_edit.setPlaceholderText("留空 · 同目录建同名文件夹")
        row2.addWidget(self.out_edit, 1)
        browse2 = QPushButton("浏览", self)
        browse2.clicked.connect(self._browse_out)
        row2.addWidget(browse2)
        lay.addLayout(row2)

        # 监听模式：两张平铺卡（严禁 QComboBox）
        row3 = QHBoxLayout()
        row3.setSpacing(9)
        row3.addWidget(self._field_label("监听模式"), 0, Qt.AlignTop)
        self.mode_sel = ModeSelector(self)
        self.mode_sel.set_modes([
            ("surface", "表层 · 安全",
             "只处理监听目录最外一层的压缩包，行为与旧版一致，不会深入子目录。",
             "推荐"),
            ("baidu", "百度清单 · 含子目录",
             "额外按网盘任务清单处理下载到子目录里的压缩包与分卷，"
             "清单不可用时自动退回表层。", None),
        ])
        self.mode_sel.set_mode(str(self._orig.get("mode") or "surface"))
        row3.addWidget(self.mode_sel, 1)
        lay.addLayout(row3)

        # 开关
        row4 = QHBoxLayout()
        row4.setSpacing(9)
        row4.addWidget(self._field_label("开关"))
        self.enabled_cb = QCheckBox("启用监听", self)
        self.enabled_cb.setChecked(bool(self._orig.get("enabled", True)))
        row4.addWidget(self.enabled_cb)
        row4.addSpacing(14)
        self.del_cb = QCheckBox("解压成功后删除源文件", self)
        self.del_cb.setChecked(bool(self._orig.get("delete_source", False)))
        row4.addWidget(self.del_cb)
        row4.addStretch(1)
        lay.addLayout(row4)

        # 回收站说明
        hint_row = QHBoxLayout()
        hint_row.setSpacing(7)
        hint_row.addWidget(Glyph("shield", self, 13, role="muted"))
        hint = QLabel(
            "删除是把源文件移入回收站，可在「删除回溯」标签页一键还原，不会永久丢失。",
            self)
        hint.setObjectName("dlgHint")
        hint.setWordWrap(True)
        hint_row.addWidget(hint, 1)
        lay.addLayout(hint_row)

        # 当前正在处理
        self.current_card = QFrame(self)
        self.current_card.setObjectName("dlgCurrent")
        c_lay = QVBoxLayout(self.current_card)
        c_lay.setContentsMargins(14, 12, 14, 12)
        c_lay.setSpacing(6)
        c_top = QHBoxLayout()
        c_top.setSpacing(8)
        c_top.addWidget(Glyph("archive", self.current_card, 13, role="muted"))
        c_title = QLabel("当前正在处理：", self.current_card)
        c_title.setObjectName("dlgHint")
        c_top.addWidget(c_title)
        self.current_name = QLabel("", self.current_card)
        self.current_name.setObjectName("dlgPath")
        self.current_name.setStyleSheet("font-weight: 600;")
        c_top.addWidget(self.current_name)
        c_top.addStretch(1)
        self.current_pct = QLabel("", self.current_card)
        self.current_pct.setObjectName("dlgPath")
        c_top.addWidget(self.current_pct)
        c_lay.addLayout(c_top)
        self.current_bar = QProgressBar(self.current_card)
        self.current_bar.setObjectName("thinProg")
        self.current_bar.setTextVisible(False)
        self.current_bar.setRange(0, 100)
        self.current_bar.setValue(0)
        c_lay.addWidget(self.current_bar)
        lay.addWidget(self.current_card)

    def _build_foot(self):
        foot = QFrame(self)
        foot.setObjectName("dlgFoot")
        lay = QHBoxLayout(foot)
        lay.setContentsMargins(16, 12, 16, 14)
        lay.setSpacing(8)
        self.remove_btn = LayoutButton(foot)
        self.remove_btn.setObjectName("danger")
        self.remove_btn.setCursor(Qt.PointingHandCursor)
        rm_lay = QHBoxLayout(self.remove_btn)
        rm_lay.setContentsMargins(0, 0, 0, 0)
        rm_lay.setSpacing(6)
        rm_lay.addWidget(Glyph("trash", self.remove_btn, 13, role="danger"))
        rm_lay.addWidget(QLabel("移除目录", self.remove_btn))
        self.remove_btn.clicked.connect(self._on_remove)
        lay.addWidget(self.remove_btn)
        lay.addStretch(1)
        self.cancel_btn = QPushButton("取消", foot)
        self.cancel_btn.clicked.connect(self.reject)
        lay.addWidget(self.cancel_btn)
        self.save_btn = QPushButton("保存", foot)
        self.save_btn.setObjectName("primary")
        self.save_btn.setDefault(True)
        self.save_btn.clicked.connect(self._on_save)
        lay.addWidget(self.save_btn)
        return foot

    # ---- 状态展示 ----
    def _refresh_state_badge(self):
        text = DIR_STATE_TEXT.get(self._state_key, self._state_key)
        if self._state_key == "extracting" and self._progress is not None:
            text += " %d%%" % int(self._progress)
        try:
            self.state_badge.setText("● " + text)
            color = PALETTE["success"]
            if self._state_key == "error":
                color = PALETTE["danger"]
            elif self._state_key in ("paused", "waiting"):
                color = PALETTE["muted"]
            elif self._state_key == "listening":
                color = PALETTE["accent_text"]
            self.state_badge.setStyleSheet("color: %s;" % color)
        except Exception:
            pass

    def _render_current(self):
        name = str(self._current_name or "")
        layer_txt = ""
        if self._current_layer is not None and str(self._current_layer) != "":
            layer_txt = " · 第 %s 层" % self._current_layer
        try:
            if name:
                name = self.current_name.fontMetrics().elidedText(
                    name, Qt.ElideMiddle, 240)
        except Exception:
            pass
        text = (name + layer_txt) if name else (layer_txt.lstrip(" ·") or "—")
        self.current_name.setText(text)
        self.current_name.setToolTip(text)
        pct = self._progress
        self.current_pct.setText(("%d%%" % int(pct)) if pct is not None else "")
        self.current_bar.setValue(int(pct) if pct is not None else 0)

    # ---- 公开 API ----
    def set_current(self, name, layer=None, progress=None):
        """「当前正在处理」卡片：文件名 · 第 N 层 + 百分比 + 细进度条。"""
        self._current_name = str(name or "")
        self._current_layer = layer
        if progress is not None:
            try:
                self._progress = max(0, min(100, int(round(float(progress)))))
            except Exception:
                self._progress = 0
            self._state_key = "extracting"
            self._refresh_state_badge()
        self._render_current()

    def entry(self):
        """当前面板上的值（保存前宿主可只读获取）。"""
        return {
            "path": self.path_edit.text().strip(),
            "output_dir": self.out_edit.text().strip(),
            "mode": self.mode_sel.mode() or "surface",
            "enabled": bool(self.enabled_cb.isChecked()),
            "delete_source": bool(self.del_cb.isChecked()),
        }

    # ---- 交互 ----
    def _on_save(self):
        cur = self.entry()
        for field in ("path", "output_dir", "mode", "enabled", "delete_source"):
            value = cur.get(field)
            if value != self._orig.get(field):
                try:
                    self.state.update_path(self.idx, field, value)
                except Exception:
                    pass
        try:
            self.saved.emit(self.idx)
        except Exception:
            pass
        self.accept()

    def _on_remove(self):
        """只发信号：二次确认由宿主负责（本弹窗绝不弹确认框）。"""
        try:
            self.removeRequested.emit(self.idx)
        except Exception:
            pass

    def _browse_path(self):
        try:
            d = QFileDialog.getExistingDirectory(self, "选择监听目录")
        except Exception:
            d = ""
        if d:
            self.path_edit.setText(d)

    def _browse_out(self):
        try:
            d = QFileDialog.getExistingDirectory(self, "选择解压输出目录")
        except Exception:
            d = ""
        if d:
            self.out_edit.setText(d)

    def keyPressEvent(self, event):
        if event.key() == Qt.Key_Escape:
            self.reject()
            return
        super().keyPressEvent(event)

    # ---- 遮罩（极简自带实现） ----
    def _ensure_scrim(self):
        if self._scrim is not None:
            return
        parent = self.parentWidget()
        if parent is None:
            return
        try:
            sc = QFrame(parent)
            sc.setObjectName("dlgScrim")
            sc.setAttribute(Qt.WA_TransparentForMouseEvents, True)
            sc.setAttribute(Qt.WA_StyledBackground, True)
            sc.setStyleSheet("background: rgba(0,0,0,0.34);")
            sc.setGeometry(parent.rect())
            sc.show()
            sc.raise_()
            self._scrim = sc
        except Exception:
            self._scrim = None

    def _destroy_scrim(self):
        sc = self._scrim
        self._scrim = None
        if sc is None:
            return
        try:
            sc.hide()
            sc.setParent(None)
            sc.deleteLater()
        except Exception:
            pass

    def showEvent(self, event):
        self._ensure_scrim()
        super().showEvent(event)
        self._center_on_parent()

    def hideEvent(self, event):
        self._destroy_scrim()
        super().hideEvent(event)

    def closeEvent(self, event):
        self._destroy_scrim()
        super().closeEvent(event)

    def _center_on_parent(self):
        try:
            parent = self.parentWidget()
            if parent is None:
                return
            self.adjustSize()
            pg = parent.frameGeometry()
            self.move(pg.center().x() - self.width() // 2,
                      pg.center().y() - self.height() // 2)
        except Exception:
            pass

