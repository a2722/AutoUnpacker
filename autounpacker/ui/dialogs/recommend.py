# -*- coding: utf-8 -*-
"""首次运行的「推荐配置」询问弹窗 + 推荐配置的写入。

时机（客户口径 2026-09-30）：首次打开程序时问一次 —— 7-Zip 引导**之后**；
若本机已装 7-Zip（不弹安装提示），就直接问。两个选项：
  「继续使用默认配置」（默认侧，一个键都不动）
  「使用推荐配置」  （蓝色 #primary，醒目）

选了推荐配置，就在**默认值基础上**改这四项，其余键一律不动：
  temp_password_max                       50 -> 200
  url_trust.open.new_domain_action        ask -> auto_whitelist（自动信任）
  url_trust.fetch.new_domain_action       ask -> auto_whitelist（自动信任）
  experimental_enabled                    False -> True

外观沿用既有自绘模态卡（QMessageBox#modalCard / #modalHead / #primary），
不硬编码任何颜色；不 import page_settings / main_window（导入图必须无环）。
关键入口：RecommendConfigDialog.ask(parent) / apply_recommended_config(state) /
          RECOMMENDED_ITEMS
"""
from PyQt5.QtCore import Qt
from PyQt5.QtWidgets import (QFrame, QHBoxLayout, QLabel, QMessageBox,
                             QPushButton, QVBoxLayout, QWidget)

from ..widgets import Glyph

# (界面文案, 配置键路径, 目标值) —— 单一真源：界面清单与写入逻辑共用这张表
RECOMMENDED_ITEMS = (
    ("临时密码保留上限：50 → 200",
     "temp_password_max", 200),
    ("把二维码里的链接打开时，遇到新网站：询问 → 自动信任",
     "url_trust.open.new_domain_action", "auto_whitelist"),
    ("复制网址去识别二维码时，遇到新网站：询问 → 自动信任",
     "url_trust.fetch.new_domain_action", "auto_whitelist"),
    ("启用实验性功能", "experimental_enabled", True),
)


def apply_recommended_config(state):
    """按推荐配置写这四项（其余键一律不动）。返回实际写入的键路径列表。

    url_trust 是嵌套字典：整体取快照 → 只改两个用途的 new_domain_action → 整体写回
    （与 trust.add_trust_entry / remember_auto_domain 的写回口径一致；
    绝不往黑白名单里添任何条目）。任何一步失败都静默跳过，绝不抛进 UI。"""
    written = []
    for _label, path, val in RECOMMENDED_ITEMS:
        if "." in path:                 # 嵌套键在下面按用途整体写回
            continue
        try:
            state.set(path, val)
            written.append(path)
        except Exception:
            pass
    try:
        ut = dict(state.get("url_trust") or {})
        for purpose in ("open", "fetch"):
            sub = dict(ut.get(purpose) or {})
            sub["new_domain_action"] = "auto_whitelist"
            ut[purpose] = sub
        state.set("url_trust", ut)
        written += [p for _l, p, _v in RECOMMENDED_ITEMS if p.startswith("url_trust.")]
    except Exception:
        pass
    return written


class RecommendConfigDialog(QMessageBox):
    """「要不要用推荐配置」自绘模态卡（与「清除日志」同一套卡外观）。

    返回语义：`ask()` -> True = 用户选了「使用推荐配置」；其余（继续默认 / ✕ / Esc /
    构造或执行异常）一律 False，绝不抛进 UI。样式全部来自既有 QSS
    （#modalCard / #modalHead / #warnBox 之外的普通行 / #primary），不硬编码颜色。
    """

    def __init__(self, parent=None):
        super().__init__(parent)
        self.setObjectName("modalCard")
        self.setWindowTitle("推荐配置")
        self.setIcon(QMessageBox.NoIcon)
        self.setWindowFlags(Qt.Dialog | Qt.FramelessWindowHint)
        self.setModal(True)
        self._recommended = False
        # 标准按钮（保留 QMessageBox 契约：buttons()/clickedButton() 仍可用）并隐藏
        self._use_btn = self.addButton("使用推荐配置", QMessageBox.AcceptRole)
        self._def_btn = self.addButton("继续使用默认配置", QMessageBox.RejectRole)
        for b in (self._use_btn, self._def_btn):
            b.hide()
        for name in ("qt_msgbox_label", "qt_msgbox_informativelabel",
                     "qt_msgboxex_icon_label"):
            w = self.findChild(QLabel, name)
            if w is not None:
                w.hide()
        try:
            from PyQt5.QtWidgets import QDialogButtonBox
            bb = self.findChild(QDialogButtonBox)
            if bb is not None:
                bb.hide()
        except Exception:
            pass

        host = QWidget(self)
        v = QVBoxLayout(host)
        v.setContentsMargins(0, 0, 0, 0)
        v.setSpacing(0)

        # 头：标题 + 关闭（关闭 = 继续使用默认配置）
        head = QFrame(host)
        head.setObjectName("modalHead")
        hl = QHBoxLayout(head)
        hl.setContentsMargins(16, 14, 16, 14)
        hl.setSpacing(10)
        title = QLabel("推荐配置", head)
        title.setObjectName("modalTitle")
        hl.addWidget(title)
        hl.addStretch(1)
        close_btn = QPushButton(head)
        close_btn.setObjectName("modalClose")
        close_btn.setFixedSize(24, 24)
        close_btn.setCursor(Qt.PointingHandCursor)
        close_btn.setToolTip("继续使用默认配置")
        cl = QHBoxLayout(close_btn)
        cl.setContentsMargins(0, 0, 0, 0)
        glyph = Glyph("close", close_btn, 12, role="muted")
        glyph.setAttribute(Qt.WA_TransparentForMouseEvents, True)
        cl.addWidget(glyph, 0, Qt.AlignCenter)
        close_btn.clicked.connect(self.reject)
        hl.addWidget(close_btn)
        v.addWidget(head)

        # 体：一句话 + 四项清单 + 兜底说明
        body = QWidget(host)
        bl = QVBoxLayout(body)
        bl.setContentsMargins(16, 16, 16, 16)
        bl.setSpacing(10)
        lead = QLabel("要不要按推荐配置起步？推荐配置会在默认值基础上打开四项：", body)
        lead.setWordWrap(True)
        bl.addWidget(lead)
        items = QLabel(
            "\n".join("· " + label for label, _p, _v in RECOMMENDED_ITEMS), body)
        items.setWordWrap(True)
        try:
            items.setTextInteractionFlags(Qt.NoTextInteraction)
        except Exception:
            pass
        bl.addWidget(items)
        hint = QLabel("只改这四项，其余保持默认；选「继续使用默认配置」则一个键都不动。",
                      body)
        hint.setObjectName("stripHint")
        hint.setWordWrap(True)
        bl.addWidget(hint)
        v.addWidget(body)

        # 脚：继续使用默认配置 + 使用推荐配置（#primary = 主题蓝，醒目）
        foot = QFrame(host)
        foot.setObjectName("modalFoot")
        fl = QHBoxLayout(foot)
        fl.setContentsMargins(16, 12, 16, 12)
        fl.setSpacing(10)
        fl.addStretch(1)
        keep_btn = QPushButton("继续使用默认配置", foot)
        keep_btn.setCursor(Qt.PointingHandCursor)
        keep_btn.clicked.connect(self.reject)
        fl.addWidget(keep_btn)
        use_btn = QPushButton("使用推荐配置", foot)
        use_btn.setObjectName("primary")
        use_btn.setCursor(Qt.PointingHandCursor)
        use_btn.setDefault(True)
        use_btn.clicked.connect(self._on_use)
        fl.addWidget(use_btn)
        v.addWidget(foot)

        # 自建内容占据原「按钮盒」那一行（隐藏项不参与布局）
        try:
            self.layout().addWidget(host, 3, 0, 1, 2)
        except Exception:
            pass
        host.setMinimumWidth(448)

    def showEvent(self, event):   # noqa: N802 (Qt 命名)
        super().showEvent(event)
        try:
            self.setFixedWidth(480)
        except Exception:
            pass

    def _on_use(self):
        self._recommended = True
        self.accept()

    def use_recommended(self):
        """是否选了「使用推荐配置」（✕ / Esc / 继续默认 都为 False）。"""
        if self._recommended:
            return True
        return self.clickedButton() is self._use_btn

    @classmethod
    def ask(cls, parent=None):
        """弹出询问并返回布尔；构造/执行失败一律按「继续使用默认配置」处理。"""
        try:
            dlg = cls(parent)
            dlg.exec_()
            return dlg.use_recommended()
        except Exception:
            return False
