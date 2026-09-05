# -*- coding: utf-8 -*-
"""共享密码本模块：所有监听目录共用一个长期密码本 + 运行期临时密码"""
from PyQt5.QtCore import QTimer
from PyQt5.QtGui import QTextCursor
from PyQt5.QtWidgets import (
    QDialog, QVBoxLayout, QHBoxLayout, QLabel,
    QPlainTextEdit, QCheckBox, QPushButton, QMessageBox,
)

def parse_password_text(text):
    """按行解析密码文本（换行分隔），去掉空行并去重，保持顺序"""
    result = []
    seen = set()
    for line in (text or "").splitlines():
        p = line.strip()
        if p and p not in seen:
            seen.add(p)
            result.append(p)
    return result


class PasswordBookDialog(QDialog):
    """共享密码本子窗口：长期密码（换行分隔）+ 临时密码管理"""

    def __init__(self, state, parent=None):
        super().__init__(parent)
        self.state = state
        self.setWindowTitle("共享密码本")
        self.resize(520, 540)

        lay = QVBoxLayout(self)
        lay.setContentsMargins(16, 14, 16, 14)
        lay.setSpacing(10)

        title = QLabel("共享密码本")
        title.setObjectName("appTitle")
        lay.addWidget(title)

        guide = QLabel(
            "所有监听目录共用一个密码本，解压时按从上到下的顺序依次尝试。\n"
            "每行一个密码（用换行分隔，不再使用逗号）。\n"
            "程序运行期间，剪贴板复制的短文本（少于 60 字符）会自动作为临时密码\n"
            "参与解压尝试；退出程序即清空。勾选下方选项可自动加入长期密码本。"
        )
        guide.setWordWrap(True)
        lay.addWidget(guide)

        self.edit = QPlainTextEdit()
        self.edit.setPlaceholderText("每行一个密码，例如：\n1234\nabc123\nqwerty")
        self.edit.setPlainText("\n".join(state.passwords()))
        lay.addWidget(self.edit, 1)

        # 排序 / 查重
        tool_row = QHBoxLayout()
        sort_btn = QPushButton("排序（升序）")
        sort_btn.clicked.connect(self._sort)
        dedup_btn = QPushButton("查重清理")
        dedup_btn.clicked.connect(self._dedup)
        self.count_lbl = QLabel()
        self.count_lbl.setStyleSheet("color: #6b7688;")
        tool_row.addWidget(sort_btn)
        tool_row.addWidget(dedup_btn)
        tool_row.addStretch(1)
        tool_row.addWidget(self.count_lbl)
        lay.addLayout(tool_row)
        self._update_count()

        self.auto_cb = QCheckBox("自动将剪贴板捕获的临时密码添加到长期密码本")
        self.auto_cb.setChecked(state.auto_add())
        lay.addWidget(self.auto_cb)

        temp_row = QHBoxLayout()
        temp_row.addWidget(QLabel("本次运行临时密码："))
        # 临时密码可能积压很多，用只读文本框 + 最大高度限制显示，
        # 超出部分滚动查看，避免把密码本区域挤占。
        self.temp_edit = QPlainTextEdit()
        self.temp_edit.setReadOnly(True)
        self.temp_edit.setMaximumHeight(110)
        self.temp_edit.setPlaceholderText("（无）")
        clear_btn = QPushButton("清空临时密码")
        clear_btn.clicked.connect(self._clear_temp)
        temp_row.addWidget(self.temp_edit, 1)
        temp_row.addWidget(clear_btn)
        lay.addLayout(temp_row)

        btns = QHBoxLayout()
        btns.addStretch(1)
        ok = QPushButton("保存")
        ok.setObjectName("primary")
        ok.clicked.connect(self.accept)
        cancel = QPushButton("取消")
        cancel.clicked.connect(self.reject)
        btns.addWidget(ok)
        btns.addWidget(cancel)
        lay.addLayout(btns)

        self._refresh_temp()

        # 临时密码由后台剪贴板线程写入 state，窗口打开期间需要实时显示：
        # 轻量轮询刷新（内容变化才重绘，避免每次全量 setPlainText）。
        self._timer = QTimer(self)
        self._timer.setInterval(500)
        self._timer.timeout.connect(self._refresh_temp_if_changed)
        self._timer.start()
        self.finished.connect(self._timer.stop)

    def _refresh_temp_if_changed(self):
        """把后台线程新增的临时密码实时同步到窗口。

        不打扰用户正在做的操作：
        - 用户正在选中/聚焦/鼠标悬停在临时密码框（拉选复制、滚轮浏览）
          时跳过本轮，操作完后的下一个 tick 再同步。
        - 其余情况只在末尾增量追加新增行（临时密码只会追加在列表末尾），
          且通过独立文档级光标插入——不移动控件光标、不触发 ensureCursorVisible
          滚动，绝不打断阅读位置。"""
        if (self.temp_edit.hasFocus() or self.temp_edit.underMouse()
                or self.temp_edit.textCursor().hasSelection()):
            return
        temp = self.state.temp_passwords()
        text = "\n".join(temp) if temp else ""
        cur = self.temp_edit.toPlainText()
        if text == cur:
            return
        if cur and text.startswith(cur):
            # 追加模式：只插入新增的部分，已有内容/选中/滚动位置不动
            delta = text[len(cur):]
            if delta.startswith("\n"):
                delta = delta[1:]
            if delta:
                tc = QTextCursor(self.temp_edit.document())
                tc.movePosition(QTextCursor.End)
                tc.insertText("\n" + delta)
        else:
            # 非常规变化（如清空后重建），才做全量替换
            self.temp_edit.setPlainText(text)

    def _refresh_temp(self):
        temp = self.state.temp_passwords()
        if temp:
            self.temp_edit.setPlainText("\n".join(temp))
        else:
            self.temp_edit.clear()

    def _clear_temp(self):
        self.state.clear_temp_passwords()
        self._refresh_temp()

    def _lines(self):
        """当前编辑框中的非空密码行"""
        return [l.strip() for l in self.edit.toPlainText().splitlines() if l.strip()]

    def _set_lines(self, lines):
        self.edit.setPlainText("\n".join(lines))
        self._update_count()

    def _update_count(self):
        self.count_lbl.setText(f"共 {len(self._lines())} 条")

    def _sort(self):
        """按字母升序排序（忽略大小写），不修改密码内容"""
        lines = self._lines()
        lines.sort(key=str.casefold)
        self._set_lines(lines)

    def _dedup(self):
        """移除重复密码（保留首次出现），报告移除数量"""
        lines = self._lines()
        before = len(lines)
        seen = set()
        out = []
        for p in lines:
            if p not in seen:
                seen.add(p)
                out.append(p)
        removed = before - len(out)
        self._set_lines(out)
        if removed:
            QMessageBox.information(
                self, "查重", f"共 {before} 条，移除重复 {removed} 条，剩余 {len(out)} 条。\n"
                              "点击「保存」后生效。")
        else:
            QMessageBox.information(self, "查重", f"共 {before} 条，未发现重复。")

    def accept(self):
        self.state.set_passwords(parse_password_text(self.edit.toPlainText()))
        self.state.set_auto_add(self.auto_cb.isChecked())
        super().accept()
