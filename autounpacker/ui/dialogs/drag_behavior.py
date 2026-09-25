# -*- coding: utf-8 -*-
"""DragBehaviorDialog：主界面固定胶囊「拖拽行为」的设置弹窗（拖入文件后做什么）。"""
from PyQt5.QtWidgets import (QWidget, QVBoxLayout, QHBoxLayout, QLabel,
                             QCheckBox, QDialog, QFrame, QPushButton)
from PyQt5.QtCore import Qt, pyqtSignal

from ..widgets import Glyph
from .common import TRASH_HINT_NORMAL


# 配置键 -> 默认值（唯一真源；缺键一律按默认，保证默认拖放行为与旧版逐字一致）
DROP_DEFAULTS = {
    "drop_enabled": True,          # 总开关：关 = 拖入文件不再自动处理
    "drop_qr_recognize": True,     # 拖入图片时识别二维码
    "drop_nested": True,           # 智能穿透嵌套解压（关 = 只解第一层）
    "drop_delete_source": False,   # 解压成功后删除源文件（默认关：拖放不删源）
}


class DragBehaviorDialog(QDialog):
    """拖拽行为设置（对应目录胶囊条上的固定「拖拽行为」胶囊）。

    结构：头部（图标 + 标题 + 关闭）/ 表单（总开关 + 三个行为开关 + 固定行为说明，
    读写 QCheckBox 自带的状态）/ 底部（取消 / 保存）。保存只 accept()，由宿主写回
    配置并刷新胶囊状态——弹窗绝不直接碰配置。

    默认（开 / 识别二维码 / 智能穿透 / 不删源）与旧版拖放行为完全一致：本弹窗
    只把既有硬编码变成可配置项，不改动任何默认语义。Esc / 取消 = reject()；
    遮罩与居中复用 WatchDirDialog 的极简自带实现（无父窗口时自动忽略）。
    """

    notice = pyqtSignal(str)   # 一行提示（宿主写日志）

    def __init__(self, values=None, parent=None):
        super().__init__(parent)
        v = values if isinstance(values, dict) else {}
        self._scrim = None
        self.setWindowTitle("拖拽行为设置")
        self.setModal(True)
        self.setFixedWidth(560)

        root = QVBoxLayout(self)
        root.setContentsMargins(0, 0, 0, 0)
        root.setSpacing(0)
        root.addWidget(self._build_head())
        body = QWidget(self)
        body_lay = QVBoxLayout(body)
        body_lay.setContentsMargins(16, 14, 16, 14)
        body_lay.setSpacing(11)
        self._build_body(body_lay, v)
        root.addWidget(body)
        root.addWidget(self._build_foot())

    # ---- 公开 API ----
    def values(self):
        """当前面板上的 4 个 drop_* 值（保存前宿主可只读获取）。"""
        return {
            "drop_enabled": bool(self.enabled_cb.isChecked()),
            "drop_qr_recognize": bool(self.qr_cb.isChecked()),
            "drop_nested": bool(self.nested_cb.isChecked()),
            "drop_delete_source": bool(self.del_cb.isChecked()),
        }

    # ---- 头部 / 表单 / 底部 ----
    def _field_label(self, text):
        lbl = QLabel(text, self)
        lbl.setObjectName("fLabel")
        lbl.setFixedWidth(62)
        return lbl

    def _hint(self, text, parent):
        lbl = QLabel(text, parent)
        lbl.setObjectName("dlgHint")
        lbl.setWordWrap(True)
        return lbl

    def _build_head(self):
        head = QFrame(self)
        head.setObjectName("dlgHead")
        lay = QHBoxLayout(head)
        lay.setContentsMargins(16, 14, 16, 12)
        lay.setSpacing(10)
        lay.addWidget(Glyph("bolt", head, 20, role="accent"))
        title = QLabel("拖拽行为设置", head)
        title.setObjectName("dTitle")
        lay.addWidget(title)
        sub = QLabel("文件拖入主界面后执行什么", head)
        sub.setObjectName("dlgPath")
        lay.addWidget(sub)
        lay.addStretch(1)
        close_btn = QPushButton(head)
        close_btn.setObjectName("iconBtn")
        close_btn.setFixedSize(30, 30)
        close_btn.setToolTip("关闭")
        close_btn.setCursor(Qt.PointingHandCursor)
        close_lay = QHBoxLayout(close_btn)
        close_lay.setContentsMargins(0, 0, 0, 0)
        close_lay.addWidget(Glyph("close", close_btn, 16), 0, Qt.AlignCenter)
        close_btn.clicked.connect(self.reject)
        lay.addWidget(close_btn)
        return head

    def _build_body(self, lay, values):
        def flag(key):
            return bool(values.get(key, DROP_DEFAULTS[key]))

        # 总开关：关 = 拖入文件不再自动处理（只记一行日志）
        row1 = QHBoxLayout()
        row1.setSpacing(9)
        row1.addWidget(self._field_label("总开关"), 0, Qt.AlignTop)
        col1 = QVBoxLayout()
        col1.setSpacing(4)
        self.enabled_cb = QCheckBox("拖拽解压", self)
        self.enabled_cb.setChecked(flag("drop_enabled"))
        col1.addWidget(self.enabled_cb)
        col1.addWidget(self._hint(
            "关闭后，拖入窗口的文件不再自动处理（只记一行日志）；"
            "通过监听目录发现文件的方式不受影响。", self))
        row1.addLayout(col1, 1)
        lay.addLayout(row1)

        # 二维码识别：只对图片扩展名的文件生效（伪装成图片的压缩包仍按压缩包处理）
        row2 = QHBoxLayout()
        row2.setSpacing(9)
        row2.addWidget(self._field_label("拖入图片"), 0, Qt.AlignTop)
        col2 = QVBoxLayout()
        col2.setSpacing(4)
        self.qr_cb = QCheckBox("识别图片中的二维码", self)
        self.qr_cb.setChecked(flag("drop_qr_recognize"))
        col2.addWidget(self.qr_cb)
        col2.addWidget(self._hint(
            "只对图片扩展名的文件生效；伪装成图片的压缩包仍按压缩包处理。", self))
        row2.addLayout(col2, 1)
        lay.addLayout(row2)

        # 嵌套穿透：关 = 只解第一层
        row3 = QHBoxLayout()
        row3.setSpacing(9)
        row3.addWidget(self._field_label("嵌套解压"), 0, Qt.AlignTop)
        col3 = QVBoxLayout()
        col3.setSpacing(4)
        self.nested_cb = QCheckBox("智能穿透嵌套解压", self)
        self.nested_cb.setChecked(flag("drop_nested"))
        col3.addWidget(self.nested_cb)
        col3.addWidget(self._hint(
            "关闭后只解第一层（最深 1 层），压缩包里的压缩包不再继续解。", self))
        row3.addLayout(col3, 1)
        lay.addLayout(row3)

        # 删除源文件：默认关；删除是移入回收站（与目录设置同一口径）
        row4 = QHBoxLayout()
        row4.setSpacing(9)
        row4.addWidget(self._field_label("源文件"), 0, Qt.AlignTop)
        col4 = QVBoxLayout()
        col4.setSpacing(4)
        self.del_cb = QCheckBox("解压成功后删除源文件", self)
        self.del_cb.setChecked(flag("drop_delete_source"))
        col4.addWidget(self.del_cb)
        hint_row = QHBoxLayout()
        hint_row.setSpacing(7)
        hint_row.addWidget(Glyph("shield", self, 13, role="muted"))
        hint_row.addWidget(self._hint(TRASH_HINT_NORMAL, self), 1)
        col4.addLayout(hint_row)
        row4.addLayout(col4, 1)
        lay.addLayout(row4)

        # 固定行为说明（只读）：这些是拖放入口的既有约定，不提供开关
        note_row = QHBoxLayout()
        note_row.setSpacing(7)
        note_row.addWidget(Glyph("info", self, 13, role="muted"))
        note_row.addWidget(self._hint(
            "固定行为（不可设置）：只处理被拖入的文件本身 · 输出到文件所在目录 · "
            "非首卷分卷请拖首卷。", self), 1)
        lay.addLayout(note_row)

    def _build_foot(self):
        foot = QFrame(self)
        foot.setObjectName("dlgFoot")
        lay = QHBoxLayout(foot)
        lay.setContentsMargins(16, 12, 16, 14)
        lay.setSpacing(8)
        lay.addStretch(1)
        self.cancel_btn = QPushButton("取消", foot)
        self.cancel_btn.clicked.connect(self.reject)
        lay.addWidget(self.cancel_btn)
        self.save_btn = QPushButton("保存", foot)
        self.save_btn.setObjectName("primary")
        self.save_btn.setDefault(True)
        self.save_btn.clicked.connect(self._on_save)
        lay.addWidget(self.save_btn)
        return foot

    # ---- 交互 ----
    def _on_save(self):
        try:
            self.notice.emit("拖拽行为设置已更新")
        except Exception:
            pass
        self.accept()

    def keyPressEvent(self, event):
        if event.key() == Qt.Key_Escape:
            self.reject()
            return
        super().keyPressEvent(event)

    # ---- 遮罩（极简自带实现，与目录设置弹窗同一套） ----
    def _ensure_scrim(self):
        if self._scrim is not None:
            return
        parent = self.parentWidget()
        if parent is None:
            return
        try:
            sc = QFrame(parent)
            sc.setObjectName("dlgScrim")
            sc.setAttribute(Qt.WA_TransparentForMouseEvents, True)
            sc.setAttribute(Qt.WA_StyledBackground, True)
            sc.setStyleSheet("background: rgba(0,0,0,0.34);")
            sc.setGeometry(parent.rect())
            sc.show()
            sc.raise_()
            self._scrim = sc
        except Exception:
            self._scrim = None

    def _destroy_scrim(self):
        sc = self._scrim
        self._scrim = None
        if sc is None:
            return
        try:
            sc.hide()
            sc.setParent(None)
            sc.deleteLater()
        except Exception:
            pass

    def showEvent(self, event):
        self._ensure_scrim()
        super().showEvent(event)
        self._center_on_parent()

    def hideEvent(self, event):
        self._destroy_scrim()
        super().hideEvent(event)

    def closeEvent(self, event):
        self._destroy_scrim()
        super().closeEvent(event)

    def _center_on_parent(self):
        try:
            parent = self.parentWidget()
            if parent is None:
                return
            self.adjustSize()
            pg = parent.frameGeometry()
            self.move(pg.center().x() - self.width() // 2,
                      pg.center().y() - self.height() // 2)
        except Exception:
            pass
