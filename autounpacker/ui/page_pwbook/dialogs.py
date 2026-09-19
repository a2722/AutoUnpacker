# -*- coding: utf-8 -*-
"""新增 / 编辑口令、固定提取码及批量文本编辑的私有对话框
（阶段6e 自 ui/page_pwbook.py 纯搬移；行格式解析 / 格式化复用 password_book）。"""
from PyQt5.QtCore import Qt
from PyQt5.QtWidgets import (QCheckBox, QDialog, QHBoxLayout, QLabel,
                             QLineEdit, QPlainTextEdit, QPushButton,
                             QVBoxLayout)

from ...passwords.book import format_share_code_text, parse_share_code_text

from .data import _valid_share_code, _valid_share_uk


# ---------------------------------------------------------------------------
# 新增 / 编辑口令小对话框
# ---------------------------------------------------------------------------

class _PasswordEditDialog(QDialog):
    """新增 / 编辑单条口令与备注：口令直接明文显示；空口令不可保存。

    新增（初始口令为空）时提供「批量导入」：同一对话框切换为多行输入（一行一个
    口令），备注对每个口令生效；「返回单条」切回单条表单。编辑既有口令不提供批量
    入口（避免把「改一条」误变成「加一批」）。对话框只收集输入，不写库。
    """

    def __init__(self, parent=None, title="新增口令", password="", note=""):
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


# ---------------------------------------------------------------------------
# 新增 / 编辑固定提取码小对话框 + 批量文本编辑对话框
# ---------------------------------------------------------------------------

class _ShareEditDialog(QDialog):
    """新增 / 编辑单条固定提取码：分享者UK + 提取码 + 需挑选 + 备注。

    UK 只接受纯数字、提取码只接受 1~16 位字母或数字——与文本行格式的解析口径
    一致，保证行编辑写入的数据能被「批量编辑（文本）」完整往返。
    """

    def __init__(self, parent=None, title="新增提取码", share_uk="", code="",
                 note="", pick=0):
        super().__init__(parent)
        self.setWindowTitle(str(title))
        self.setMinimumWidth(420)
        lay = QVBoxLayout(self)
        lay.setContentsMargins(16, 14, 16, 14)
        lay.setSpacing(10)
        head = QLabel(str(title), self)
        head.setObjectName("appTitle")
        lay.addWidget(head)
        hint = QLabel("分享者UK = 分享页上传者的数字 uid；提取码为 1~16 位字母或数字。\n"
                      "「需挑选」表示只下载选中的文件；不勾选则整包下载。", self)
        hint.setObjectName("stripHint")
        hint.setWordWrap(True)
        lay.addWidget(hint)

        uk_label = QLabel("分享者UK（纯数字）：", self)
        uk_label.setObjectName("stripHint")
        lay.addWidget(uk_label)
        self.uk_edit = QLineEdit(self)
        self.uk_edit.setPlaceholderText("例如：3567282991")
        self.uk_edit.setText(str(share_uk or ""))
        lay.addWidget(self.uk_edit)

        code_label = QLabel("提取码：", self)
        code_label.setObjectName("stripHint")
        lay.addWidget(code_label)
        self.code_edit = QLineEdit(self)
        self.code_edit.setPlaceholderText("例如：ab12")
        self.code_edit.setText(str(code or ""))
        lay.addWidget(self.code_edit)

        self.pick_cb = QCheckBox("需要挑选下载文件（不勾选则整包下载）", self)
        self.pick_cb.setChecked(bool(pick))
        lay.addWidget(self.pick_cb)

        note_label = QLabel("备注（可选，仅本机可见）：", self)
        note_label.setObjectName("stripHint")
        lay.addWidget(note_label)
        self.note_edit = QLineEdit(self)
        self.note_edit.setPlaceholderText("例如：老王分享 / 需要挑选的网盘作者")
        self.note_edit.setText(str(note or ""))
        lay.addWidget(self.note_edit)

        btns = QHBoxLayout()
        btns.addStretch(1)
        self.save_btn = QPushButton("保存", self)
        self.save_btn.setObjectName("primary")
        self.save_btn.setCursor(Qt.PointingHandCursor)
        self.save_btn.clicked.connect(self.accept)
        cancel = QPushButton("取消", self)
        cancel.clicked.connect(self.reject)
        btns.addWidget(self.save_btn)
        btns.addWidget(cancel)
        lay.addLayout(btns)

        self._sync_save()
        self.uk_edit.textChanged.connect(self._sync_save)
        self.code_edit.textChanged.connect(self._sync_save)
        self.uk_edit.returnPressed.connect(self._on_return)
        self.code_edit.returnPressed.connect(self._on_return)
        self.uk_edit.setFocus()

    def _valid(self):
        return (_valid_share_uk(self.uk_edit.text())
                and _valid_share_code(self.code_edit.text()))

    def _sync_save(self, _text=None):
        self.save_btn.setEnabled(self._valid())

    def _on_return(self):
        if self.save_btn.isEnabled():
            self.accept()

    def values(self):
        """(分享者UK, 提取码, 备注, 需挑选 0/1)。"""
        return (str(self.uk_edit.text()).strip(),
                str(self.code_edit.text()).strip(),
                str(self.note_edit.text()).strip(),
                1 if self.pick_cb.isChecked() else 0)


class _ShareTextDialog(QDialog):
    """批量编辑固定提取码：每行「分享者UK 提取码 [pick] [#备注]」（与旧弹窗同格式）。

    只做文本 <-> 行列表的往返，解析 / 格式化完全复用 password_book 的既有助手，
    行格式与语义保持不变。保存由调用方经 set_share_code_map 整表覆盖落库。
    """

    def __init__(self, parent=None, rows=None):
        super().__init__(parent)
        self.setWindowTitle("批量编辑固定提取码")
        self.resize(560, 460)
        lay = QVBoxLayout(self)
        lay.setContentsMargins(16, 14, 16, 14)
        lay.setSpacing(10)
        head = QLabel("批量编辑固定提取码", self)
        head.setObjectName("appTitle")
        lay.addWidget(head)
        hint = QLabel(
            "每行一条：分享者UK 提取码 [pick] [#备注]，例如：\n"
            "3567282991 ab12 #老王\n"
            "3567282991 ab12 pick #老王\n"
            "第三列写 pick / 挑选 / 1 表示该分享者需要挑选下载文件（不写则整包下载）；\n"
            "空行、以 # 开头的注释行与格式不合法的行会被忽略。", self)
        hint.setObjectName("stripHint")
        hint.setWordWrap(True)
        lay.addWidget(hint)

        self.edit = QPlainTextEdit(self)
        self.edit.setPlaceholderText("每行一条：分享者UK 提取码 [pick] [#备注]")
        self.edit.setPlainText(format_share_code_text(rows or []))
        lay.addWidget(self.edit, 1)

        count_row = QHBoxLayout()
        count_row.addStretch(1)
        self.count_lbl = QLabel("", self)
        self.count_lbl.setObjectName("stripHint")
        count_row.addWidget(self.count_lbl)
        lay.addLayout(count_row)

        btns = QHBoxLayout()
        btns.addStretch(1)
        self.save_btn = QPushButton("保存", self)
        self.save_btn.setObjectName("primary")
        self.save_btn.setCursor(Qt.PointingHandCursor)
        self.save_btn.clicked.connect(self.accept)
        cancel = QPushButton("取消", self)
        cancel.clicked.connect(self.reject)
        btns.addWidget(self.save_btn)
        btns.addWidget(cancel)
        lay.addLayout(btns)

        self.edit.textChanged.connect(self._update_count)
        self._update_count()
        self.edit.setFocus()

    def _update_count(self):
        n = len(parse_share_code_text(self.edit.toPlainText()))
        self.count_lbl.setText("共 %d 条" % n)

    def items(self):
        """当前文本解析出的固定提取码列表（与 parse_share_code_text 同一语义）。"""
        return parse_share_code_text(self.edit.toPlainText())
