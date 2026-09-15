# -*- coding: utf-8 -*-
"""共享密码本对话框：长期密码编辑 + 临时密码查看，所有监听目录共用。

职责：- PasswordBookDialog 提供长期密码（每行一个）编辑、排序、查重
- 编辑特殊用户固定提取码（share_uk → 提取码 + 是否「需要挑选」，每行一条）
- 只读展示运行期临时密码，QTimer 轮询实时同步（不打断用户选中/滚动）
- 保存时写入 state（长期密码/固定提取码存 toolbox.db）
关键入口：PasswordBookDialog / parse_password_text() / parse_share_code_text() / format_share_code_text()
依赖：PyQt5、state.AppState
注意：密码按换行分隔（不再用逗号）；临时密码由后台剪贴板线程写入 state，此处只展示
"""
from PyQt5.QtCore import Qt, QTimer
from PyQt5.QtGui import QTextCursor
from PyQt5.QtWidgets import (
    QDialog, QVBoxLayout, QHBoxLayout, QLabel,
    QPlainTextEdit, QCheckBox, QPushButton, QMessageBox,
    QSplitter, QWidget,
)

from .ui.style import PALETTE

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


def parse_share_code_text(text):
    """把「分享者UK 提取码 [pick] [#备注]」多行文本解析为
    [{"share_uk","code","note","pick"}, ...]，pick 为 0/1。

    空行/注释行/格式不合法的行一律跳过；同一 UK 重复出现时以最后一行为准。
    接受格式：以空白（空格/制表符）分隔，「#」之后为备注（可省略）；UK 必须是
    纯数字，「提取码」必须是 1~16 位 ASCII 字母或数字。第三列若为 pick/挑选/1
    （pick 不分大小写）则标记「需要挑选」（pick=1），否则该列并入备注文本。
    任何输入都不抛异常。
    """
    result = []
    index = {}
    for line in str(text or "").splitlines():
        p = line.strip()
        if not p or p.startswith("#"):
            continue
        body, _, note = p.partition("#")
        parts = body.split()
        if len(parts) < 2:
            continue
        uk, code = parts[0], parts[1]
        if not (uk.isascii() and uk.isdigit()):
            continue
        if not (1 <= len(code) <= 16) or not (code.isascii() and code.isalnum()):
            continue
        rest = parts[2:]
        pick = 0
        if rest and rest[0].lower() in ("pick", "挑选", "1"):
            pick = 1
            rest = rest[1:]
        note = note.strip() or " ".join(rest)
        item = {"share_uk": uk, "code": code, "note": note, "pick": pick}
        if uk in index:
            # 同一 UK 重复：最后一行的提取码/备注/pick 生效，位置沿用首次出现（与 db 一致）
            index[uk].update(item)
        else:
            index[uk] = item
            result.append(item)
    return result


def format_share_code_text(items):
    """把固定提取码列表格式化为编辑框文本：每行「分享者UK 提取码 [pick] [#备注]」。

    与 parse_share_code_text 互为往返：pick=1 写成 pick 标记，再解析回来仍为 1；
    pick=0 不写标记。非法条目跳过，任何输入都不抛异常。
    """
    lines = []
    for it in items or []:
        if not isinstance(it, dict):
            continue
        uk = str(it.get("share_uk") or "").strip()
        code = str(it.get("code") or "").strip()
        if not uk or not code:
            continue
        try:
            pick = 1 if int(it.get("pick") or 0) else 0
        except Exception:
            pick = 1 if it.get("pick") else 0
        note = str(it.get("note") or "").strip()
        line = f"{uk} {code} pick" if pick else f"{uk} {code}"
        if note:
            line += f" #{note}"
        lines.append(line)
    return "\n".join(lines)


class PasswordBookDialog(QDialog):
    """共享密码本子窗口：长期密码（换行分隔）+ 临时密码管理"""

    def __init__(self, state, parent=None):
        super().__init__(parent)
        self.state = state
        self.setWindowTitle("共享密码本")
        self.resize(520, 660)

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
            "参与解压尝试；超过有效期或保留上限会自动清理。勾选下方选项可自动加入长期密码本。"
        )
        guide.setWordWrap(True)
        lay.addWidget(guide)

        self.edit = QPlainTextEdit()
        self.edit.setPlaceholderText("每行一个密码，例如：\n1234\nabc123\nqwerty")
        self.edit.setPlainText("\n".join(state.passwords()))

        # 排序 / 查重
        tool_row = QHBoxLayout()
        sort_btn = QPushButton("排序（升序）")
        sort_btn.clicked.connect(self._sort)
        dedup_btn = QPushButton("查重清理")
        dedup_btn.clicked.connect(self._dedup)
        self.count_lbl = QLabel()
        self.count_lbl.setStyleSheet(f"color: {PALETTE['muted']};")
        tool_row.addWidget(sort_btn)
        tool_row.addWidget(dedup_btn)
        tool_row.addStretch(1)
        tool_row.addWidget(self.count_lbl)

        self.auto_cb = QCheckBox("自动收录剪贴板临时密码")
        self.auto_cb.setToolTip("把剪贴板捕获的临时密码自动加入长期密码本。")
        self.auto_cb.setChecked(state.auto_add())

        # 长期密码区（编辑框 + 工具行 + 自动加入开关）
        perm_box = QWidget()
        perm_lay = QVBoxLayout(perm_box)
        perm_lay.setContentsMargins(0, 0, 0, 0)
        perm_lay.setSpacing(8)
        perm_lay.addWidget(self.edit, 1)
        perm_lay.addLayout(tool_row)
        perm_lay.addWidget(self.auto_cb)

        # 临时密码区（标题行 + 只读框 + 清空按钮）
        self.temp_edit = QPlainTextEdit()
        self.temp_edit.setReadOnly(True)
        self.temp_edit.setMinimumHeight(50)
        self.temp_edit.setPlaceholderText("（无）")
        temp_box = QWidget()
        temp_lay = QVBoxLayout(temp_box)
        temp_lay.setContentsMargins(0, 0, 0, 0)
        temp_lay.setSpacing(8)
        temp_head = QHBoxLayout()
        temp_head.addWidget(QLabel("临时密码（超时/超量自动清理）："))
        temp_head.addStretch(1)
        clear_btn = QPushButton("清空临时密码")
        clear_btn.clicked.connect(self._clear_temp)
        temp_head.addWidget(clear_btn)
        temp_lay.addLayout(temp_head)
        temp_lay.addWidget(self.temp_edit, 1)

        # 上下两块用分隔条隔开，可拖动调节高度（拉高窗口不再只让上方变高）
        split = QSplitter(Qt.Vertical)
        split.addWidget(perm_box)
        split.addWidget(temp_box)
        split.setStretchFactor(0, 3)
        split.setStretchFactor(1, 1)
        split.setSizes([340, 140])
        lay.addWidget(split, 1)

        # 特殊用户固定提取码区（标题行 + 编辑框）：预填 state 中的数据（与解析器可往返）
        try:
            share_text = format_share_code_text(state.share_code_map() or [])
        except Exception:
            share_text = ""

        self.share_edit = QPlainTextEdit()
        self.share_edit.setMinimumHeight(60)
        self.share_edit.setPlaceholderText(
            "每行一条：分享者UK 提取码 [pick] [#备注]，例如：\n"
            "3567282991 ab12 #老王\n"
            "3567282991 ab12 pick #老王\n"
            "第三列写 pick / 挑选 / 1 表示该分享者需要挑选下载文件（不写则整包下载）；\n"
            "分享者UK 是分享页上传者的数字 uid（share_uk）；提取码为 4 位字符。")
        self.share_edit.setPlainText(share_text)
        share_box = QWidget()
        share_lay = QVBoxLayout(share_box)
        share_lay.setContentsMargins(0, 0, 0, 0)
        share_lay.setSpacing(8)
        share_head = QHBoxLayout()
        share_head.addWidget(QLabel("特殊用户固定提取码（每行：分享者UK 提取码 [pick] [#备注]）："))
        share_head.addStretch(1)
        self.share_count_lbl = QLabel()
        self.share_count_lbl.setStyleSheet(f"color: {PALETTE['muted']};")
        share_head.addWidget(self.share_count_lbl)
        share_lay.addLayout(share_head)
        share_lay.addWidget(self.share_edit)
        lay.addWidget(share_box)

        self._update_count()
        self.share_edit.textChanged.connect(self._update_share_count)
        self._update_share_count()

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

    def _update_share_count(self):
        n = len(parse_share_code_text(self.share_edit.toPlainText()))
        self.share_count_lbl.setText(f"共 {n} 条")

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
        try:
            self.state.set_share_code_map(
                parse_share_code_text(self.share_edit.toPlainText()))
        except Exception:
            pass
        super().accept()
