# -*- coding: utf-8 -*-
"""PW 页：新增永久口令（口令必填 + 备注选填；不弹窗，行内警示）。

字段与现实现对齐（`ui/page_pwbook/dialogs.py` 的 `_PasswordEditDialog` 单条模式）：
- 口令：必填、明文 `QLineEdit`；空口令时「保存」禁用；
- 备注：选填，placeholder `例如：老王分享`；
- 判重：提交前 `password in db.get_passwords()`（与 `page_pwbook/page.py`
  `_on_add` 同款逻辑），命中 -> 行内警示 `该口令已在密码本中`，**停留本页**；
- 保存：只走 `host.state.add_password_row(password, note=note)`
  （→ `db.add_password(source="manual")`）；本页绝不自己写 INSERT；
- 成功 -> `finished(True)`（由窗口轻提示 + 回 HOME）；取消 / Esc -> `finished(False)`。

红线（规格 §0 D4）：提取码与口令是两种东西；本页只收「永久口令」，
不得出现任何「提取码保存到口令本」的入口。
"""
from PyQt5.QtCore import Qt, pyqtSignal
from PyQt5.QtWidgets import (QHBoxLayout, QLabel, QLineEdit, QPushButton,
                             QVBoxLayout, QWidget)

from ... import db


class PwPage(QWidget):
    """新增永久口令页：保存成功发 finished(True)，取消/返回发 finished(False)。"""

    finished = pyqtSignal(bool)

    def __init__(self, host, parent=None):
        super().__init__(parent)
        self._host = host
        self.setObjectName("compactPwPage")

        root = QVBoxLayout(self)
        root.setContentsMargins(12, 10, 12, 12)
        root.setSpacing(8)

        head = QHBoxLayout()
        head.setSpacing(6)
        self.back_btn = QPushButton("‹ 返回", self)
        self.back_btn.setObjectName("compactBack")
        self.back_btn.setCursor(Qt.PointingHandCursor)
        self.back_btn.setFocusPolicy(Qt.NoFocus)
        self.back_btn.setToolTip("取消并返回主界面（Esc）")
        self.back_btn.clicked.connect(lambda *_: self.request_cancel())
        head.addWidget(self.back_btn)
        title = QLabel("新增永久口令", self)
        title.setObjectName("compactPageTitle")
        head.addWidget(title)
        head.addStretch(1)
        root.addLayout(head)

        cap_pwd = QLabel("口令（必填）", self)
        cap_pwd.setObjectName("compactField")
        root.addWidget(cap_pwd)

        self.pwd_edit = QLineEdit(self)
        self.pwd_edit.setPlaceholderText("输入口令")
        self.pwd_edit.setEchoMode(QLineEdit.Normal)   # 明文（与现对话框一致）
        root.addWidget(self.pwd_edit)

        cap_note = QLabel("备注（选填）", self)
        cap_note.setObjectName("compactField")
        root.addWidget(cap_note)

        self.note_edit = QLineEdit(self)
        self.note_edit.setPlaceholderText("例如：老王分享")
        root.addWidget(self.note_edit)

        self.warn = QLabel("", self)
        self.warn.setObjectName("compactWarn")
        self.warn.setWordWrap(True)
        self.warn.hide()
        root.addWidget(self.warn)

        root.addStretch(1)

        foot = QHBoxLayout()
        foot.addStretch(1)
        self.save_btn = QPushButton("保存", self)
        self.save_btn.setObjectName("primary")
        self.save_btn.setCursor(Qt.PointingHandCursor)
        self.save_btn.setEnabled(False)
        self.save_btn.clicked.connect(lambda *_: self._on_save())
        self.cancel_btn = QPushButton("取消", self)
        self.cancel_btn.setCursor(Qt.PointingHandCursor)
        self.cancel_btn.clicked.connect(lambda *_: self.request_cancel())
        foot.addWidget(self.save_btn)
        foot.addWidget(self.cancel_btn)
        root.addLayout(foot)

        self.pwd_edit.textChanged.connect(self._on_pwd_changed)
        self.pwd_edit.returnPressed.connect(self._on_return)
        self.note_edit.returnPressed.connect(self._on_return)

    # ---- 生命周期 ----
    def reset(self):
        """每次进入本页前清空表单、隐藏行内警示（不触库）。"""
        try:
            self.pwd_edit.setText("")
            self.note_edit.setText("")
            self._set_warn("")
            self.save_btn.setEnabled(False)
            self.pwd_edit.setFocus()
        except Exception:
            pass

    def request_cancel(self):
        """Esc / `‹ 返回` / `取消`：回 HOME（由窗口按普通历史处理）。"""
        try:
            self.finished.emit(False)
        except Exception:
            pass

    # ---- 输入 / 保存 ----
    def _on_pwd_changed(self, _text=None):
        self.save_btn.setEnabled(bool(self.pwd_edit.text().strip()))
        # 用户重新编辑即清掉上一次的行内警示（与「停留本页继续改」的交互一致）
        self._set_warn("")

    def _on_return(self):
        if self.save_btn.isEnabled():
            self._on_save()

    def _set_warn(self, text):
        try:
            self.warn.setText(str(text or ""))
            self.warn.setVisible(bool(text))
        except Exception:
            pass

    def _on_save(self):
        """保存：先查重（行内警示、停留本页），再走 state.add_password_row。"""
        pwd = self.pwd_edit.text().strip()
        if not pwd:
            return
        note = self.note_edit.text().strip()
        # 提交前必须查重：与 page_pwbook/page.py 的 _on_add 同一判定入口
        try:
            exists = pwd in (db.get_passwords() or [])
        except Exception:
            exists = False
        if exists:
            self._set_warn("该口令已在密码本中")
            return
        rid = 0
        try:
            rid = self._host.state.add_password_row(pwd, note=note)
        except Exception:
            rid = 0
        if not rid:
            # add_password_row 返回 0 = 无法写入（重复行会被 INSERT OR IGNORE 忽略，
            # 但前置查重已挡住重复；这里按真实失败处理，绝不假报成功）
            self._set_warn("新增失败：无法写入密码本")
            return
        try:
            self.finished.emit(True)
        except Exception:
            pass
