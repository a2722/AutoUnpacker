# -*- coding: utf-8 -*-
"""TrustAskDialog / CloseActionDialog：新网址信任询问与关闭行为三选一。"""
from PyQt5.QtWidgets import (QVBoxLayout, QHBoxLayout, QLabel, QLineEdit,
                             QPushButton, QCheckBox, QDialog)
from PyQt5.QtCore import Qt

from ...trust import trust_entry_categories
from ..style import PALETTE


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
        risk = trust_entry_categories(host, resolve=False)
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
