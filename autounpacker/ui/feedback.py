# -*- coding: utf-8 -*-
"""问题反馈：对话框 + 正文拼装 + mailto 链接。

设计取舍（2026-10-29 定稿）：
- **不建任何自建接口**。客户端里的提交地址一定能被挖出来，直连接口只会变成刷垃圾
  的入口。反馈走「系统邮件客户端（mailto:）+ 复制到剪贴板」两条路，零攻击面。
- `mailto:` 正文在 Windows 上实际可靠长度约 2 KB（再长会被 shell 或邮件客户端截断，
  RFC 6068 本身不限长但现实不可靠）。因此邮件正文超长时**截断并注明**，而
  「复制反馈信息」永远给出**完整**内容。
- 运行信息**只带版本 / 系统 / 最近 3 条错误日志**，全部显示在对话框里、发送前可删改，
  绝不静默附带任何文件内容或路径。

纯字符串部分（build_report / build_mailto / recent_errors）不依赖 Qt，便于离线单测。
"""
import platform
import sqlite3
import time
from urllib.parse import quote

from PyQt5.QtCore import QUrl, Qt
from PyQt5.QtGui import QDesktopServices
from PyQt5.QtWidgets import (QApplication, QCheckBox, QDialog, QHBoxLayout,
                             QLabel, QPlainTextEdit, QPushButton, QVBoxLayout)

FEEDBACK_EMAIL = "1825817051@qq.com"

# mailto 上限按「编码后的整条 URL」算：Windows shell 与常见邮件客户端在 2 KB 上下
# 开始截断。中文经百分号编码后 1 字符≈9 字符，所以只按字符数限制会严重超长
# （真机实测：1,640 字符的正文编码成 6,016 字符的 URL）。
MAILTO_URL_LIMIT = 1800
# 随信附带的最近失败任务数（每个任务附**完整**日志块，不是单条消息）。
RECENT_ERROR_TASKS = 3


def app_version():
    try:
        from .. import __version__
        return str(__version__)
    except Exception:
        return "unknown"


def system_desc():
    try:
        return "%s %s (%s)" % (platform.system(), platform.release(),
                               platform.machine())
    except Exception:
        return "unknown"


def recent_error_blocks(db_file, limit=RECENT_ERROR_TASKS):
    """最近 N 个失败任务的**完整**日志块（只读打开；任何异常返回空列表）。

    历史教训：一开始只取 3 条 error/warn 消息，结果同一任务的
    「[第1层] 解压失败」「=== 解压未完成 ===」「xxx 解压失败」三条挤在一起，
    等于只有三行、且没有 7-Zip 原始输出 —— 对排查毫无用处。正确口径是**按任务
    聚合**：一个失败任务 = 从「=== 处理: xxx ===」到「xxx 解压失败」的全部行
    （真机 13 行，含原始输出块）。
    """
    try:
        uri = "file:%s?mode=ro" % str(db_file).replace("?", "%3f")
        con = sqlite3.connect(uri, uri=True, timeout=3)
        try:
            tids = [r[0] for r in con.execute(
                "select task_id from log_index "
                "where lower(level) in ('error','warn','warning') "
                "and task_id is not null "
                "group by task_id order by max(id) desc limit ?",
                (max(0, int(limit)),)).fetchall()]
            blocks = []
            for tid in tids:
                rows = con.execute(
                    "select ts, level, text from log_index "
                    "where task_id=? order by id", (tid,)).fetchall()
                lines = []
                for ts, _lv, text in rows:
                    try:
                        when = time.strftime("%H:%M:%S",
                                             time.localtime(int(ts)))
                    except Exception:
                        when = "?"
                    lines.append("[%s] %s" % (when, str(text or "").strip()))
                if lines:
                    blocks.append("---- 失败任务 #%s ----\n%s"
                                  % (tid, "\n".join(lines)))
            return blocks
        finally:
            con.close()
    except Exception:
        return []


def build_report(user_text, include_info=True, db_file=None, email=None):
    """拼装反馈正文：用户描述 + （可选）运行信息。纯字符串，绝不读用户文件内容。

    email 非空时在**最前面**加一行「请发送到: xxx」——多数用户是用网页邮箱
    手动粘贴发送（系统 mailto 处理器通常没有登录态），粘贴后这行就是发送地址提醒。
    """
    parts = []
    body = (user_text or "").strip()
    if body:
        parts.append(body)
    if include_info:
        info = ["---- 运行信息（下方可删改，不需要的行直接删掉）----",
                "版本: %s" % app_version(),
                "系统: %s" % system_desc()]
        blocks = recent_error_blocks(db_file) if db_file else []
        if blocks:
            info.append("最近 %d 个失败任务的完整记录:" % len(blocks))
            info.extend(blocks)
        parts.append("\n".join(info))
    text = "\n\n".join(parts)
    if email:
        return ("请发送到: %s" % email) + ("\n\n" + text if text else "")
    return text


def build_mailto(report, email=FEEDBACK_EMAIL, limit=MAILTO_URL_LIMIT):
    """拼 mailto: 链接；按**编码后长度**截断，并注明改用「复制反馈信息」。

    subject/body 均 URL 编码。正文过长时逐步收缩到 URL 长度上限内，尾部补一句
    提示 —— 完整内容始终可以用「复制反馈信息」取得（不限长度）。
    """
    subject = "AutoUnpacker 反馈 v%s" % app_version()
    head = "mailto:%s?subject=%s&body=" % (email, quote(subject, safe=""))
    text = report or ""
    budget = limit - len(head)
    if budget > 0 and len(quote(text, safe="")) > budget:
        note = "\n...(内容过长已截断，完整内容请用「复制反馈信息」)"
        note_enc = quote(note, safe="")
        budget = max(0, budget - len(note_enc))
        while text and len(quote(text, safe="")) > budget:
            text = text[: int(len(text) * 0.9)]
        text = text.rstrip() + note
    return head + quote(text, safe="")


GITHUB_ISSUE_URL = "https://github.com/a2722/AutoUnpacker/issues/new"
# GitHub issues/new 的预填参数长度实测上限（未登录时探测所得）：
#   编码后 body ≈ 2,000 → 302 跳登录且**保留 return_to**（正常）
#   编码后 body ≈ 4,995 → 302 跳登录但 return_to 被丢弃（登录后落到首页，预填白费）
#   编码后 body ≈ 6,993 → **HTTP 500**（浏览器显示 "Whoops, something went wrong!"）
# 因此正文只在很短时才预填；超长一律换成一句占位提示，完整内容靠剪贴板。
GITHUB_BODY_URL_LIMIT = 2500


def link_color():
    """取当前主题的链接色（style.PALETTE 是扁平字典，取不到就退回深色主题的蓝）。"""
    try:
        from . import style as ui_style
        pal = getattr(ui_style, "PALETTE", None) or {}
        if isinstance(pal, dict):
            v = pal.get("log_link")
            if isinstance(v, str) and v.startswith("#"):
                return v
            inner = pal.get(ui_style.current_theme())
            if isinstance(inner, dict):
                v = inner.get("log_link")
                if isinstance(v, str) and v.startswith("#"):
                    return v
    except Exception:
        pass
    return "#3794ff"


def github_body_fits(report, limit=GITHUB_BODY_URL_LIMIT):
    """正文是否短到可以安全预填进 GitHub 链接（按编码后长度判断）。"""
    return len(quote(report or "", safe="")) <= limit


def build_github_url(report, base=GITHUB_ISSUE_URL, limit=GITHUB_BODY_URL_LIMIT):
    """拼 GitHub「新建 Issue」链接（预填标题，正文按长度决定预填还是占位）。"""
    title = "反馈: AutoUnpacker v%s" % app_version()
    head = "%s?title=%s&body=" % (base, quote(title, safe=""))
    body = quote(report or "", safe="")
    if len(body) > limit:
        body = quote("（正文已复制到剪贴板，请在此处右键粘贴后提交）", safe="")
    return head + body


class FeedbackDialog(QDialog):
    """反馈窗口：**复制为主**（网页邮箱粘贴发送），GitHub Issue 为次，邮件客户端兜底。"""

    def __init__(self, parent=None, db_file=None):
        super().__init__(parent)
        self.setWindowTitle("问题反馈")
        self.setMinimumWidth(660)
        self._db_file = db_file

        root = QVBoxLayout(self)

        self.mail_link = QLabel(
            '反馈邮箱：<a href="#copy-email" style="color:%s">%s</a>'
            '　（点邮箱即复制地址；或点「复制全部」→ 网页邮箱 → 粘贴发送）'
            % (link_color(), FEEDBACK_EMAIL), self)
        self.mail_link.setWordWrap(True)
        self.mail_link.setTextFormat(Qt.RichText)
        # 只有链接可点（悬停自动变手型），其余文字保持普通文本
        self.mail_link.setTextInteractionFlags(Qt.LinksAccessibleByMouse)
        self.mail_link.linkActivated.connect(self._copy_email)
        root.addWidget(self.mail_link)

        tip = QLabel("请描述你遇到的问题（越具体越好：什么时候、在做什么、界面报了什么）：", self)
        tip.setWordWrap(True)
        root.addWidget(tip)

        self.edit = QPlainTextEdit(self)
        self.edit.setPlaceholderText(
            "例如：解压 xxx.7z 时报「密码错误」，但密码本里明明有正确密码。")
        self.edit.setMinimumHeight(120)
        root.addWidget(self.edit)

        self.chk_info = QCheckBox(
            "附带运行信息（版本 / 系统 / 最近 3 个失败任务的完整记录）—— 下方可删改", self)
        self.chk_info.setChecked(True)
        root.addWidget(self.chk_info)

        self.info_box = QPlainTextEdit(self)
        # 可编辑：界面上承诺「可删改」，就必须真能改（含删掉本地路径等敏感行）。
        # 只有勾选框被切换时才重新生成，用户的手改不会被刷新冲掉。
        self.info_box.setReadOnly(False)
        self.info_box.setMaximumHeight(150)
        root.addWidget(self.info_box)

        row = QHBoxLayout()
        self.copy_btn = QPushButton("复制全部（含邮箱）", self)
        self.copy_btn.setDefault(True)
        self.copy_btn.clicked.connect(self._copy)
        self.github_btn = QPushButton("在 GitHub 提 Issue", self)
        self.github_btn.clicked.connect(self._send_github)
        self.mail_btn = QPushButton("用邮件客户端", self)
        self.mail_btn.clicked.connect(self._send_mailto)
        close_btn = QPushButton("关闭", self)
        close_btn.clicked.connect(self.reject)
        row.addWidget(self.copy_btn)
        row.addWidget(self.github_btn)
        row.addWidget(self.mail_btn)
        row.addStretch(1)
        row.addWidget(close_btn)
        root.addLayout(row)

        self.hint = QLabel(
            "提示：GitHub 需要账号（免费注册；点按钮会打开网页并预填内容），"
            "但 Issue 是<b>公开可见</b>的，介意的话点「复制全部」用上面的邮箱发。", self)
        self.hint.setWordWrap(True)
        root.addWidget(self.hint)

        self.chk_info.toggled.connect(self._refresh_info)
        self._refresh_info()

    # ---- 内部 ----
    def _report(self, with_email=False):
        """正文 = 用户描述 + 信息框**当前内容**（含用户的手动删改），不再二次生成。"""
        user = self.edit.toPlainText().strip()
        info = (self.info_box.toPlainText().strip()
                if self.chk_info.isChecked() else "")
        body = "\n\n".join(p for p in (user, info) if p)
        if with_email:
            head = "请发送到: %s" % FEEDBACK_EMAIL
            return head + ("\n\n" + body if body else "")
        return body

    def _refresh_info(self, *_):
        self.info_box.setPlainText(
            build_report("", self.chk_info.isChecked(), self._db_file))
        self.info_box.setEnabled(self.chk_info.isChecked())

    def _set_hint(self, text):
        try:
            self.hint.setText(text)
        except Exception:
            pass

    def _copy_email(self, *_):
        """点邮箱 → 只复制邮箱地址本身（方便先填收件人）。"""
        try:
            QApplication.clipboard().setText(FEEDBACK_EMAIL)
        except Exception:
            return
        self._set_hint("邮箱已复制：%s —— 去网页邮箱粘贴到收件人即可。" % FEEDBACK_EMAIL)
        QApplication.processEvents()

    def _copy(self):
        """主路径：整段（含「请发送到: 邮箱」首行）进剪贴板，用户粘到网页邮箱发送。"""
        QApplication.clipboard().setText(self._report(with_email=True))
        self.copy_btn.setText("已复制 ✓")
        if self.edit.toPlainText().strip():
            self._set_hint("已复制全文，打开网页邮箱（如 mail.qq.com）粘贴后发送即可。")
        else:
            self._set_hint("已复制。别忘了在邮件里写一句你遇到的问题，否则不好定位。")
        QApplication.processEvents()

    def _send_github(self):
        """GitHub 渠道：先把全文复制好，再打开新建 Issue 页。

        正文太长时不预填（GitHub 会丢 return_to，甚至 500），改用占位文本，
        让用户登录后粘贴剪贴板里的完整内容。
        """
        text = self._report(with_email=True)
        try:
            QApplication.clipboard().setText(text)
        except Exception:
            pass
        fits = github_body_fits(text)
        url = build_github_url(text)
        opened = False
        try:
            opened = bool(QDesktopServices.openUrl(QUrl(url)))
        except Exception:
            opened = False
        if not opened:
            self._set_hint("没能打开浏览器。可手动访问：%s" % GITHUB_ISSUE_URL)
            return
        if fits:
            self._set_hint(
                "已打开 GitHub：登录（或免费注册）→ 标题与正文已预填 → "
                "拉到底点绿色「Create」提交。全文也已复制，粘贴可兜底。")
        else:
            self._set_hint(
                "已打开 GitHub（标题已预填）：登录（或免费注册）→ 在正文框"
                "右键粘贴（全文已复制）→ 点「Create」提交。")

    def _send_mailto(self):
        """邮件客户端（可选）：仅装了 Outlook 等才用得上，系统默认处理器常无登录态。"""
        url = build_mailto(self._report(with_email=True))
        opened = False
        try:
            opened = bool(QDesktopServices.openUrl(QUrl(url)))
        except Exception:
            opened = False
        if not opened:
            self._copy()
            self._set_hint("没有可用的邮件客户端，已改为复制。粘贴到网页邮箱发到 %s 即可。"
                           % FEEDBACK_EMAIL)
