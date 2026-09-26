# -*- coding: utf-8 -*-
"""Scrim：覆盖主窗的暗化遮罩，并且**它就是那个模态窗口**。

项目没有 dim-mask 原语；要点（见 Oracle 设计）：
- 被模态阻塞的窗口收不到任何鼠标事件 -> 不能靠事件过滤器抓「外点」；
- 模态窗口自身的 transient 子窗口不受其模态阻塞；
- Windows 上「被拥有」的窗口永远在属主之上，所以遮罩当模态窗口、弹窗当它的子窗。
"""
from PyQt5.QtCore import Qt
from PyQt5.QtGui import QColor, QPainter
from PyQt5.QtWidgets import QWidget

DIM_ALPHA = 88   # ≈ rgba(0,0,0,.34)


class Scrim(QWidget):
    def __init__(self, main_window):
        super().__init__(main_window,
                         Qt.Tool | Qt.FramelessWindowHint
                         | Qt.WindowDoesNotAcceptFocus)
        self.setAttribute(Qt.WA_TranslucentBackground, True)
        self.setAttribute(Qt.WA_ShowWithoutActivating, True)
        self.setFocusPolicy(Qt.NoFocus)
        self.setWindowModality(Qt.ApplicationModal)   # 这个窗口负责阻塞主窗
        self._main = main_window
        self._dismiss = None
        self.setGeometry(main_window.frameGeometry())
        try:
            main_window.installEventFilter(self)      # 主窗移动/缩放时跟随
        except Exception:
            pass

    def set_dismiss(self, fn):
        self._dismiss = fn

    def mousePressEvent(self, e):
        if self._dismiss:
            self._dismiss()
        e.accept()                                    # 吞掉：后面的控件不响应

    def paintEvent(self, e):
        QPainter(self).fillRect(self.rect(), QColor(0, 0, 0, DIM_ALPHA))

    def eventFilter(self, obj, ev):
        from PyQt5.QtCore import QEvent
        if obj is self._main and ev.type() in (QEvent.Move, QEvent.Resize):
            try:
                self.setGeometry(self._main.frameGeometry())
            except Exception:
                pass
        return super().eventFilter(obj, ev)
