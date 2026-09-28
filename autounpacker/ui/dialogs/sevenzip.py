# -*- coding: utf-8 -*-
"""SevenZipSetupDialog / _SevenZipOp：7-Zip 缺失或版本过低时的安装引导。

两种模式：
  - first_run（默认）：首次启动检测发现缺失/过低时的三按钮引导
    （隔离版/全局版/跳过），保留既有行为，新增 outcome 语义。
  - manage：设置页「7-Zip 管理…」入口，展示当前状态并支持
    安装隔离版/全局版、卸载隔离版、重新检测、关闭。
安装/卸载/重新检测都在后台线程执行（_SevenZipOp 信号桥回主线程），
UI 线程绝不直接跑子进程。网络下载只发生在用户点击后，不捆绑任何二进制。"""
import threading

from PyQt5.QtWidgets import (QVBoxLayout, QHBoxLayout, QLabel, QPushButton,
                             QFileDialog, QDialog)
from PyQt5.QtCore import QObject, pyqtSignal

from ... import sevenzip as sevenzip_manager
from ..style import PALETTE


def _status_text(info):
    """把 check_environment() 结果格式化成人类可读状态文案。

    - none            -> 未安装
    - ok  / isolated  -> 隔离版 x.yy
    - ok  / system    -> 系统版 x.yy
    - low             -> 版本过低（{模式} x.yy）
    - None/未知       -> 正在检测…（状态尚未就绪）
    """
    if not info:
        return "正在检测…"
    status = info.get("status")
    version_str = info.get("version_str") or "未知"
    if status == "none":
        return "未安装"
    mode = "隔离版" if info.get("mode") == "isolated" else "系统版"
    if status == "low":
        return f"版本过低（{mode} {version_str}）"
    if status == "ok":
        return f"{mode} {version_str}"
    return "未安装"


class _SevenZipOp(QObject):
    """7-Zip 后台操作信号（worker 线程 → GUI 主线程）。"""

    progress = pyqtSignal(str)
    done = pyqtSignal(str, bool)   # (消息, 是否成功)


class SevenZipSetupDialog(QDialog):
    """首次启动/手动检查发现 7-Zip 缺失或版本过低时的安装引导。

    - 隔离版：下载官方安装包静默安装到 %APPDATA%\\AutoUnpacker\\7z（不污染项目/全局）
    - 全局版：下载官方安装包安装到系统（会触发 UAC）
    - 跳过（仅 first_run）：仅使用内置 ZIP 引擎
    下载只发生在用户点击后，不捆绑任何二进制文件。

    `mode="first_run"`（默认）保持既有三按钮引导；`mode="manage"` 是设置页
    入口，展示当前状态并提供安装/卸载/重新检测。`self.outcome` 记录结束原因：
    closed（默认）/ skipped（点跳过）/ installed（安装成功）/ failed（安装尝试失败）。"""

    def __init__(self, state, hub, info, parent=None, mode="first_run"):
        super().__init__(parent)
        self.state = state
        self.hub = hub
        self.info = info
        self.mode = mode
        self.outcome = "closed"       # closed / skipped / installed / failed
        self._sig = _SevenZipOp()
        self._sig.progress.connect(self._on_progress)
        self._sig.done.connect(self._on_done)
        self._pending_op = None       # 当前后台操作：install / uninstall / recheck
        self._busy = False
        self._action_buttons = []     # 忙碌时统一禁用的按钮
        self._checked_info = info     # 最近一次检测结果（manage 状态刷新用）
        self._recheck_started = False
        self.setWindowTitle("7-Zip 检测" if mode != "manage" else "7-Zip 管理")
        self.setModal(True)
        self.resize(560, 340)

        lay = QVBoxLayout(self)
        lay.setContentsMargins(16, 14, 16, 14)
        lay.setSpacing(10)

        if mode == "manage":
            self._build_manage(lay)
        else:
            self._build_first_run(lay)

    # ------------------------------------------------------------------
    # first_run：既有三按钮引导
    # ------------------------------------------------------------------
    def _build_first_run(self, lay):
        title = QLabel("需要 7-Zip 才能解压 RAR/7z/tar 等格式")
        title.setObjectName("appTitle")
        lay.addWidget(title)

        self.msg_lbl = QLabel(self._build_message(self.info))
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
        self.local_btn = QPushButton("使用本地 7-Zip 包…")
        self.local_btn.setToolTip(
            "选择已下载的 AutoUnpacker-7zip.zip，离线安装隔离版（零网络）。")
        self.local_btn.clicked.connect(self._install_local)
        self.skip_btn = QPushButton("跳过，仅使用 ZIP")
        self.skip_btn.clicked.connect(self._skip)
        row.addWidget(self.isolated_btn)
        row.addWidget(self.global_btn)
        row.addWidget(self.local_btn)
        row.addWidget(self.skip_btn)
        lay.addLayout(row)
        self._action_buttons = [self.isolated_btn, self.global_btn,
                                self.local_btn, self.skip_btn]

        note = QLabel(
            "没有 7-Zip 时只能解压 ZIP 格式（使用内置引擎）；"
            "RAR/7z/tar/gz 等格式需要 7-Zip。\n"
            "隔离版仅存放在 %APPDATA%\\AutoUnpacker\\7z，不污染项目目录；"
            "卸载可在「设置 → 7-Zip 管理」完成。密码经 stdin 管道传给 7z，"
            "不会出现在命令行（任务管理器/WMI 看不到）。\n"
            "注：隔离版默认下载 7-Zip 官方免安装包（portable）到 %APPDATA%，"
            "不需要管理员权限、也不会弹出 UAC；只有该方式失败时才会回退到官方"
            "安装器，届时需要一次管理员授权。\n"
            "离线应急：把 AutoUnpacker-7zip.zip 放到 AutoUnpacker.exe 同目录，"
            "或点「使用本地 7-Zip 包…」手动选择。")
        note.setWordWrap(True)
        note.setStyleSheet(f"color: {PALETTE['muted']}; font-size: 12px;")
        lay.addWidget(note)

    # ------------------------------------------------------------------
    # manage：设置页入口
    # ------------------------------------------------------------------
    def _build_manage(self, lay):
        title = QLabel("7-Zip 管理")
        title.setObjectName("appTitle")
        lay.addWidget(title)

        status_row = QHBoxLayout()
        status_cap = QLabel("当前状态：")
        self.status_lbl = QLabel(_status_text(self.info))
        self.status_lbl.setObjectName("roval")
        status_row.addWidget(status_cap)
        status_row.addWidget(self.status_lbl)
        status_row.addStretch(1)
        lay.addLayout(status_row)

        self.msg_lbl = QLabel(
            "没有 7-Zip 时只能解压 ZIP 格式（使用内置引擎）；"
            "RAR/7z/tar/gz 等格式需要 7-Zip。\n"
            "隔离版仅存放在 %APPDATA%\\AutoUnpacker\\7z，不污染项目目录；"
            "密码经 stdin 管道传给 7z，不会出现在命令行（任务管理器/WMI 看不到）。\n"
            "注：隔离版默认下载 7-Zip 官方免安装包（portable）到 %APPDATA%，"
            "不需要管理员权限、也不会弹出 UAC；只有该方式失败时才会回退到官方"
            "安装器，届时需要一次管理员授权。\n"
            "离线应急：把 AutoUnpacker-7zip.zip 放到 AutoUnpacker.exe 同目录，"
            "或点「使用本地 7-Zip 包…」手动选择。")
        self.msg_lbl.setWordWrap(True)
        lay.addWidget(self.msg_lbl)

        self.progress_lbl = QLabel("")
        self.progress_lbl.setWordWrap(True)
        self.progress_lbl.setStyleSheet(f"color: {PALETTE['accent_text']};")
        lay.addWidget(self.progress_lbl)

        row = QHBoxLayout()
        self.isolated_btn = QPushButton("安装隔离版（推荐）")
        self.isolated_btn.setObjectName("primary")
        self.isolated_btn.clicked.connect(lambda: self._install("isolated"))
        self.global_btn = QPushButton("安装全局版")
        self.global_btn.clicked.connect(lambda: self._install("global"))
        self.local_btn = QPushButton("使用本地 7-Zip 包…")
        self.local_btn.setToolTip(
            "选择已下载的 AutoUnpacker-7zip.zip，离线安装隔离版（零网络）。")
        self.local_btn.clicked.connect(self._install_local)
        self.uninstall_btn = QPushButton("卸载隔离版")
        self.uninstall_btn.setToolTip("仅卸载 %APPDATA%\\AutoUnpacker\\7z 下的隔离版。")
        self.uninstall_btn.clicked.connect(self._uninstall)
        self.recheck_btn = QPushButton("重新检测")
        self.recheck_btn.clicked.connect(self._recheck)
        self.close_btn = QPushButton("关闭")
        self.close_btn.clicked.connect(self.reject)
        row.addWidget(self.isolated_btn)
        row.addWidget(self.global_btn)
        row.addWidget(self.local_btn)
        row.addWidget(self.uninstall_btn)
        row.addWidget(self.recheck_btn)
        row.addWidget(self.close_btn)
        lay.addLayout(row)
        self._action_buttons = [self.isolated_btn, self.global_btn,
                                self.local_btn, self.uninstall_btn,
                                self.recheck_btn, self.close_btn]
        self._update_uninstall_enabled()

    # ------------------------------------------------------------------
    # 首次运行：进入 manage 时自动重新检测一次，拿到真实状态
    # ------------------------------------------------------------------
    def showEvent(self, event):
        super().showEvent(event)
        if self.mode == "manage" and not self._recheck_started:
            self._recheck_started = True
            self._recheck()

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

    # ------------------------------------------------------------------
    # 后台操作
    # ------------------------------------------------------------------
    def _install(self, kind, bundle=None):
        """kind="isolated" 可带 bundle：本地 AutoUnpacker-7zip.zip 路径（零网络）。"""
        if self._busy:
            return
        self._pending_op = "install"
        self._set_busy(True)
        self.progress_lbl.setStyleSheet(f"color: {PALETTE['accent_text']};")

        def _prog(s):
            self._sig.progress.emit(s)

        def worker():
            try:
                if kind == "isolated":
                    p = sevenzip_manager.install_isolated(progress=_prog,
                                                          bundle=bundle)
                    msg = f"隔离版安装成功：{p}"
                else:
                    p = sevenzip_manager.install_global(progress=_prog)
                    msg = f"全局版安装成功：{p}"
                self.hub.log(f"7-Zip {kind} 安装成功: {p}")
                self._sig.done.emit(msg, True)
            except Exception as e:
                self.hub.log(f"7-Zip {kind} 安装失败: {e}")
                err_text = str(e).lower()
                offline = any(k in err_text for k in (
                    "certificate_verify_failed", "sslcertverificationerror",
                    "ssl", "urlopen", "certificate", "无法访问", "timed out",
                    "getaddrinfo", "ssl.c"))
                if offline:
                    hint = ("疑似网络不通或系统缺少根证书（无法访问 7-zip.org）。\n"
                            "离线也能装好 7-Zip：\n"
                            "① 把 AutoUnpacker-7zip.zip 放到 AutoUnpacker.exe "
                            "同目录后重试；\n"
                            "② 点「使用本地 7-Zip 包…」选择已下载的包；\n"
                            "③ 到 7-zip.org 手动安装后点「重新检测」。")
                else:
                    hint = "可到 7-zip.org 手动下载安装后点「重新检测」，或稍后重试。"
                self._sig.done.emit(
                    f"安装失败：{e}\n{hint}\n[{type(e).__name__}]", False)

        threading.Thread(target=worker, daemon=True).start()

    def _install_local(self):
        """手动选择本地 AutoUnpacker-7zip.zip，走隔离版同一条后台安装线程。"""
        if self._busy:
            return
        path, _selected = QFileDialog.getOpenFileName(
            self, "选择 7-Zip 免安装包", "",
            "7-Zip 免安装包 (*.zip);;所有文件 (*)")
        if not path:
            return
        self._install("isolated", bundle=path)

    def _uninstall(self):
        if self._busy:
            return
        self._pending_op = "uninstall"
        self._set_busy(True)
        self.progress_lbl.setStyleSheet(f"color: {PALETTE['accent_text']};")
        self.progress_lbl.setText("正在卸载隔离版…")

        def _prog(s):
            self._sig.progress.emit(s)

        def worker():
            try:
                ok, msg = sevenzip_manager.uninstall_isolated(progress=_prog)
                self.hub.log(f"7-Zip 隔离版卸载: {msg}")
                self._sig.done.emit(msg, bool(ok))
            except Exception as e:
                self.hub.log(f"7-Zip 隔离版卸载失败: {e}")
                self._sig.done.emit(f"卸载失败：{e}", False)

        threading.Thread(target=worker, daemon=True).start()

    def _recheck(self):
        if self._busy:
            return
        self._pending_op = "recheck"
        self._set_busy(True)
        self.progress_lbl.setStyleSheet(f"color: {PALETTE['accent_text']};")
        self.progress_lbl.setText("正在重新检测 7-Zip…")

        def worker():
            try:
                info = sevenzip_manager.check_environment()
                self._checked_info = info
                self.hub.log(f"7-Zip 重新检测: {info.get('status')}")
                self._sig.done.emit(_status_text(info), True)
            except Exception as e:
                self.hub.log(f"7-Zip 重新检测失败: {e}")
                self._sig.done.emit(f"检测失败：{e}", False)

        threading.Thread(target=worker, daemon=True).start()

    def _set_busy(self, busy):
        self._busy = bool(busy)
        for b in self._action_buttons:
            b.setEnabled(not busy)
        if not busy:
            self._update_uninstall_enabled()

    def _update_uninstall_enabled(self):
        """「卸载隔离版」仅在隔离版副本存在时可用（文件系统判定，不跑子进程）。"""
        btn = getattr(self, "uninstall_btn", None)
        if btn is None:
            return
        try:
            exists = bool(sevenzip_manager.ISOLATED_BIN.exists())
        except Exception:
            exists = False
        btn.setEnabled((not self._busy) and exists)

    def _skip(self):
        self.outcome = "skipped"
        self.reject()

    def _on_progress(self, s):
        self.progress_lbl.setText(s)

    def _on_done(self, msg, ok):
        self._set_busy(False)
        op = self._pending_op
        self._pending_op = None
        self.progress_lbl.setStyleSheet(
            f"color: {PALETTE['accent_text'] if ok else PALETTE['warn_text']};")
        self.progress_lbl.setText(msg)
        if op == "install":
            if ok:
                self.outcome = "installed"
                self.accept()
            else:
                # 安装尝试失败：保持打开，允许用户重试或「重新检测」
                self.outcome = "failed"
        elif op == "uninstall":
            # 卸载后无论成败都重新检测真实状态，刷新状态与按钮
            self._recheck()
        elif op == "recheck":
            info = self._checked_info
            if ok and info is not None:
                try:
                    self.status_lbl.setText(_status_text(info))
                except Exception:
                    pass
            self._update_uninstall_enabled()


def open_sevenzip_manage(state, hub, parent=None, info=None):
    """打开「7-Zip 管理…」——设置页入口与日志动作链接共用的唯一公开入口。

    查看状态 / 安装隔离版·全局版 / 卸载隔离版 / 重新检测都由对话框内部的后台
    线程完成；这里只负责模态打开，只在本机操作、不登记任何配置键。
    异常一律吞掉并记日志（返回 False），绝不影响调用方的主流程。
    返回 True 表示对话框正常打开过（不代表用户做了什么选择）。"""
    try:
        dlg = SevenZipSetupDialog(state, hub, info, parent, mode="manage")
        dlg.exec_()
        return True
    except Exception as e:
        try:
            if hub:
                hub.log(f"打开 7-Zip 管理失败: {e}")
        except Exception:
            pass
        return False
