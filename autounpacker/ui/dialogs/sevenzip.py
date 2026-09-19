# -*- coding: utf-8 -*-
"""SevenZipSetupDialog / _SevenZipOp：7-Zip 缺失或版本过低时的安装引导。"""
import threading

from PyQt5.QtWidgets import (QVBoxLayout, QHBoxLayout, QLabel, QPushButton,
                             QMessageBox, QDialog)
from PyQt5.QtCore import QObject, pyqtSignal

from ... import sevenzip as sevenzip_manager
from ..style import PALETTE


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
