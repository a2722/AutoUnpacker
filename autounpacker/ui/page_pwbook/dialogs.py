# -*- coding: utf-8 -*-
"""新增 / 编辑口令的私有对话框（阶段6e 自 ui/page_pwbook.py 纯搬移）。"""
from PyQt5.QtCore import Qt
from PyQt5.QtWidgets import (QDialog, QHBoxLayout, QLabel,
                             QLineEdit, QPlainTextEdit, QPushButton,
                             QVBoxLayout)


# ---------------------------------------------------------------------------
# 新增 / 编辑口令小对话框
# ---------------------------------------------------------------------------

class _PasswordEditDialog(QDialog):
    """新增 / 编辑单条口令与备注：口令直接明文显示；空口令不可保存。

    新增（初始口令为空）时提供「批量导入」：同一对话框切换为多行输入（一行一个
    口令），备注对每个口令生效；「返回单条」切回单条表单。编辑既有口令不提供批量
    入口（避免把「改一条」误变成「加一批」）。对话框只收集输入，不写库。

    focus_note=True（双击备注列进入编辑）时键盘焦点直接锁进备注栏，光标停在
    备注文本末尾，打开即可输入；默认仍是焦点在口令栏。
    """

    def __init__(self, parent=None, title="新增口令", password="", note="",
                 focus_note=False):
        super().__init__(parent)
        self.setWindowTitle(str(title))
        self.setMinimumWidth(380)
        self._batch = False
        self._batch_ok = not bool(str(password or "").strip())   # 仅新增支持批量
        lay = QVBoxLayout(self)
        lay.setContentsMargins(16, 14, 16, 14)
        lay.setSpacing(10)
        head = QLabel(str(title), self)
        head.setObjectName("appTitle")
        lay.addWidget(head)
        hint = QLabel("口令存入本机 toolbox.db 的长期密码本；解压时按从上到下的顺序尝试。",
                      self)
        hint.setObjectName("stripHint")
        hint.setWordWrap(True)
        lay.addWidget(hint)
        self.edit = QLineEdit(self)
        self.edit.setPlaceholderText("输入口令")
        self.edit.setText(str(password or ""))
        lay.addWidget(self.edit)
        self.batch_edit = QPlainTextEdit(self)
        self.batch_edit.setPlaceholderText(
            "每行一个口令（空行忽略；重复行自动去重）")
        self.batch_edit.setMinimumHeight(180)
        self.batch_edit.hide()
        lay.addWidget(self.batch_edit)
        note_label = QLabel("备注（可选，仅本机可见）：", self)
        note_label.setObjectName("stripHint")
        lay.addWidget(note_label)
        self.note_edit = QLineEdit(self)
        self.note_edit.setPlaceholderText("例如：老王分享 / 某网盘提取码")
        self.note_edit.setText(str(note or ""))
        lay.addWidget(self.note_edit)
        btns = QHBoxLayout()
        self.batch_btn = None
        if self._batch_ok:
            self.batch_btn = QPushButton("批量导入", self)
            self.batch_btn.setCursor(Qt.PointingHandCursor)
            self.batch_btn.setToolTip("切换到多行输入：每行一个口令，备注对全部口令生效")
            self.batch_btn.clicked.connect(self._toggle_batch)
            btns.addWidget(self.batch_btn)
        btns.addStretch(1)
        self.save_btn = QPushButton("保存", self)
        self.save_btn.setObjectName("primary")
        self.save_btn.setCursor(Qt.PointingHandCursor)
        self.save_btn.setEnabled(bool(str(password or "").strip()))
        self.save_btn.clicked.connect(self.accept)
        cancel = QPushButton("取消", self)
        cancel.clicked.connect(self.reject)
        btns.addWidget(self.save_btn)
        btns.addWidget(cancel)
        lay.addLayout(btns)
        self.edit.textChanged.connect(self._sync_save)
        self.batch_edit.textChanged.connect(self._sync_save)
        self.edit.returnPressed.connect(self._on_return)
        self.edit.setFocus()
        if focus_note:
            self.note_edit.setFocus()
            self.note_edit.setCursorPosition(len(self.note_edit.text()))

    def _toggle_batch(self):
        """单条 / 批量输入切换（同一表单；备注字段两种模式共用）。"""
        self._batch = not self._batch
        self.edit.setVisible(not self._batch)
        self.batch_edit.setVisible(self._batch)
        if self.batch_btn is not None:
            self.batch_btn.setText("返回单条" if self._batch else "批量导入")
            self.batch_btn.setToolTip(
                "返回单条口令输入" if self._batch
                else "切换到多行输入：每行一个口令，备注对全部口令生效")
        self.save_btn.setText("导入" if self._batch else "保存")
        self._sync_save()
        if self._batch:
            self.batch_edit.setFocus()
        else:
            self.edit.setFocus()

    def is_batch(self):
        """当前是否处于批量导入模式。"""
        return bool(self._batch)

    def batch_values(self):
        """批量模式输入 -> (去重后的非空口令列表, 空行数, 批内重复行数)。不写库。"""
        values = []
        seen = set()
        empty = 0
        dup = 0
        for line in str(self.batch_edit.toPlainText()).splitlines():
            item = line.strip()
            if not item:
                empty += 1
                continue
            if item in seen:
                dup += 1
                continue
            seen.add(item)
            values.append(item)
        return values, empty, dup

    def _sync_save(self, _text=None):
        if self._batch:
            values, _empty, _dup = self.batch_values()
            self.save_btn.setEnabled(bool(values))
        else:
            self.save_btn.setEnabled(bool(self.edit.text().strip()))

    def _on_return(self):
        if self.save_btn.isEnabled():
            self.accept()

    def password(self):
        if self._batch:
            values, _empty, _dup = self.batch_values()
            return values[0] if values else ""
        return str(self.edit.text()).strip()

    def note(self):
        return str(self.note_edit.text()).strip()
