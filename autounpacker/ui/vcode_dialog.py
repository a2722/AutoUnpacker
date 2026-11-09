# -*- coding: utf-8 -*-
"""百度分享链路的图形验证码手动输入对话框（自动 OCR 用尽后的兜底）。

外观与 `uc_window.UcCodeDialog` 同一套朴素风格（置顶小窗、12px 边距、
说明标签 + 输入框 + 按钮行），不引入新的视觉语言。

返回值协议（与 share.py 的手动兜底循环约定）：
- 输入 4 位文本 → 用户确认的手输验证码；
- `""`（空串）→ 用户点了「换一张」（上层重新取图后再弹本窗）；
- `None` → 用户取消（放弃本次手动兜底）。

「换一张」优先走 `refetch` 回调原地换图；上层没给 refetch（默认）时按
空串收尾交给上层重取 —— 因为 vcode_str 与会话里的这张图严格配对，
取图必须发生在持有 opener 的 worker 侧，本对话框绝不在 UI 线程发请求。
"""
from PyQt5.QtCore import Qt
from PyQt5.QtGui import QPixmap
from PyQt5.QtWidgets import (QDialog, QHBoxLayout, QLabel, QLineEdit,
                             QPushButton, QVBoxLayout)


class VcodeDialog(QDialog):
    """验证码图片 + 4 位输入框 + 换一张 / 确定 / 取消（模态小窗）。"""

    def __init__(self, parent=None, img_bytes=b"", attempt=1, refetch=None):
        super().__init__(parent)
        self._refetch = refetch          # 可选：() -> bytes|None，原地换图
        self._refetch_clicked = False    # 无 refetch 时「换一张」按空串收尾
        self.setWindowTitle("百度验证码")
        self.setWindowFlag(Qt.WindowStaysOnTopHint, True)
        self.setWindowFlag(Qt.WindowContextHelpButtonHint, False)
        self.setFixedSize(340, 210)
        lay = QVBoxLayout(self)
        lay.setContentsMargins(12, 12, 12, 10)
        lay.setSpacing(8)

        self.tip_label = QLabel(
            "自动识别连续失败，请手动输入图中的 4 位字符（第 %s 张）："
            % int(attempt or 1))
        self.tip_label.setStyleSheet("color:#888;")
        self.tip_label.setWordWrap(True)
        lay.addWidget(self.tip_label)

        # 原图约 120x40，放大 2 倍显示便于辨认。
        self.img_label = QLabel(self)
        self.img_label.setAlignment(Qt.AlignCenter)
        self.img_label.setFixedHeight(88)
        lay.addWidget(self.img_label)
        self._set_image(img_bytes)

        row = QHBoxLayout()
        self.edit = QLineEdit(self)
        self.edit.setPlaceholderText("输入 4 位字符")
        self.edit.setMaxLength(4)
        self.edit.returnPressed.connect(self.accept)
        row.addWidget(self.edit, 1)
        self.btn_refetch = QPushButton("换一张", self)
        self.btn_refetch.clicked.connect(self._on_refetch)
        row.addWidget(self.btn_refetch)
        lay.addLayout(row)

        btns = QHBoxLayout()
        btns.addStretch(1)
        self.btn_cancel = QPushButton("取消", self)
        self.btn_cancel.clicked.connect(self.reject)
        btns.addWidget(self.btn_cancel)
        self.btn_ok = QPushButton("确定", self)
        self.btn_ok.setDefault(True)
        self.btn_ok.clicked.connect(self.accept)
        btns.addWidget(self.btn_ok)
        lay.addLayout(btns)

    def _set_image(self, img_bytes):
        """把字节流贴到图片标签；坏图不抛，只显示占位文字。"""
        try:
            pm = QPixmap()
            if img_bytes and pm.loadFromData(bytes(img_bytes)):
                pm = pm.scaled(pm.width() * 2, pm.height() * 2,
                               Qt.KeepAspectRatio, Qt.SmoothTransformation)
                self.img_label.setPixmap(pm)
                return
        except Exception:
            pass
        self.img_label.setText("（图片加载失败）")

    def _on_refetch(self):
        """「换一张」：有 refetch 回调就原地换图；否则按空串收尾交给上层重取。"""
        if self._refetch is not None:
            try:
                img = self._refetch()
            except Exception:
                img = None
            if img:
                self._refetch_clicked = False
                self.edit.clear()
                self._set_image(img)
                return
        # 无 refetch（或取图失败）：让上层重新取图后再弹窗。
        self._refetch_clicked = True
        self.accept()

    def result_text(self):
        """手输文本（strip 后）；「换一张」收尾返回空串；取消由 exec_ 结果区分。"""
        if self._refetch_clicked:
            return ""
        return self.edit.text().strip()


def ask_vcode(parent, img_bytes, attempt, refetch=None):
    """弹一次验证码输入窗 → 4 位文本 / ""（换一张）/ None（取消）。绝不抛。"""
    try:
        dlg = VcodeDialog(parent, img_bytes=img_bytes, attempt=attempt,
                          refetch=refetch)
        if dlg.exec_() != QDialog.Accepted:
            return None
        return dlg.result_text()
    except Exception:
        return None
