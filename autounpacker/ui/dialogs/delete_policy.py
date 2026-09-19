# -*- coding: utf-8 -*-
"""DeletePolicyAskDialog：卷无回收站时的一次性删除策略询问（隔离区/保留/永久）。"""
from PyQt5.QtWidgets import (QVBoxLayout, QHBoxLayout, QLabel, QPushButton,
                             QDialog)
from PyQt5.QtCore import Qt

from ..style import PALETTE


class DeletePolicyAskDialog(QDialog):
    """卷无回收站时的源文件删除策略询问：一次性三选一（移入隔离区 / 保留源文件 /
    永久删除）。

    仅在「目录所在卷确定没有可用回收站」且用户开启「解压成功后删除源文件」时弹出
    一次。与项目其它询问弹窗一致用普通 QDialog（QMessageBox 在 Windows 上会禁用
    标题栏关闭键）。默认/首选项为「移入隔离区」（可还原，最安全）；按 X / Esc 关闭
    返回 None，表示暂不选择（保持默认 auto，下次仍会询问，绝不静默永久删除）。
    """

    def __init__(self, parent=None):
        super().__init__(parent)
        self._choice = None   # "quarantine" / "keep" / "permanent" / None=取消
        self.setWindowTitle("删除源文件方式")
        self.setWindowFlags(Qt.Dialog | Qt.WindowTitleHint
                            | Qt.WindowSystemMenuHint | Qt.WindowCloseButtonHint)
        self.setModal(True)
        self.resize(500, 290)

        lay = QVBoxLayout(self)
        lay.setContentsMargins(18, 16, 18, 14)
        lay.setSpacing(10)

        title = QLabel("该目录所在磁盘没有可用的回收站")
        title.setObjectName("appTitle")
        lay.addWidget(title)

        info = QLabel(
            "你已开启「解压成功后删除源文件」，但该磁盘的回收站不可用，"
            "删除将无法从回收站还原。\n\n"
            "· 移入隔离区 —— 源文件移入目录下的 _已删除 文件夹，可随时还原或彻底删除（推荐）；\n"
            "· 保留源文件 —— 解压成功后保留原文件，绝不删除；\n"
            "· 永久删除 —— 解压成功后直接永久删除源文件，无法还原。\n\n"
            "此选择只询问一次，之后可在「目录设置」里随时修改。"
            "按 Esc / 标题栏 × 等同于暂不选择。")
        info.setWordWrap(True)
        info.setStyleSheet(f"color: {PALETTE['muted2']}; font-size: 13px;")
        lay.addWidget(info)

        btns = QHBoxLayout()
        btns.addStretch(1)
        quar_btn = QPushButton("移入隔离区")
        quar_btn.setObjectName("primary")
        quar_btn.setDefault(True)
        quar_btn.clicked.connect(self._choose_quarantine)
        keep_btn = QPushButton("保留源文件")
        keep_btn.clicked.connect(self._choose_keep)
        perm_btn = QPushButton("永久删除")
        perm_btn.setObjectName("danger")
        perm_btn.clicked.connect(self._choose_permanent)
        btns.addWidget(quar_btn)
        btns.addWidget(keep_btn)
        btns.addWidget(perm_btn)
        lay.addLayout(btns)

    def _choose_quarantine(self):
        self._choice = "quarantine"
        self.accept()

    def _choose_keep(self):
        self._choice = "keep"
        self.accept()

    def _choose_permanent(self):
        self._choice = "permanent"
        self.accept()

    @staticmethod
    def ask(parent=None):
        dlg = DeletePolicyAskDialog(parent)
        dlg.exec_()
        return dlg._choice
