# -*- coding: utf-8 -*-
"""点击反馈小气泡（日志链接「已复制 / 复制失败」）：无边框、绝不抢焦点、自动消失。

_ACTIVE_TOASTS 是包内唯一的共享强引用列表（本模块定义，包 __init__ 重导出），
ToastBubble 与 show_toast 都只操作这一个列表对象。"""

from PyQt5.QtWidgets import QLabel, QApplication, QGraphicsOpacityEffect, QWidget
from PyQt5.QtCore import Qt, QTimer, QPropertyAnimation, QEasingCurve
# ---------------------------------------------------------------------------
# 点击反馈小气泡（日志链接「已复制 / 复制失败」）：无边框、绝不抢焦点、自动消失
# ---------------------------------------------------------------------------
# 活动气泡强引用表：顶层 ToolTip 窗没有父控件接管生命周期，不持有引用会被 GC
# 提前回收；close_toast() 关闭时自行注销，不留悬挂引用。
_ACTIVE_TOASTS = []


class ToastBubble(QLabel):
    """自动消失的提示气泡：定位在给定全局坐标附近，msec 毫秒后淡出自关。

    - 顶层 ToolTip 窗（Qt.ToolTip | Qt.FramelessWindowHint）：不进任务栏；
    - WA_TransparentForMouseEvents + WA_ShowWithoutActivating：鼠标事件穿透、
      绝不抢焦点（点击日志链接时不打断用户正在进行的操作）；
    - 越出屏幕可用区时自动收进，底部放不下则翻到坐标点上方；
    - QTimer 以自身为 parent，随控件销毁；QGraphicsOpacityEffect 淡出失败
      也必须关闭（异常安全，绝不让气泡卡在屏幕上）。
    """

    def __init__(self, global_pos, text, msec=900, parent=None):
        super().__init__(parent)
        self.setObjectName("toastBubble")
        self.setWindowFlags(Qt.ToolTip | Qt.FramelessWindowHint)
        self.setAttribute(Qt.WA_TransparentForMouseEvents, True)
        self.setAttribute(Qt.WA_ShowWithoutActivating, True)
        self.setText(str(text))
        self._closed = False
        self._fade_anim = None
        self.adjustSize()
        self._move_near(global_pos)
        self._timer = QTimer(self)
        self._timer.setSingleShot(True)
        try:
            self._timer.setInterval(max(1, int(msec)))
        except Exception:
            self._timer.setInterval(900)
        self._timer.timeout.connect(self._fade_out)
        self._timer.start()

    # ---- 定位 ----
    def _available_geometry(self, global_pos):
        """给定全局坐标所在屏幕的可用区；取不到返回 None（此时不做钳制）。"""
        try:
            scr = QApplication.screenAt(global_pos)
            if scr is None:
                scr = QApplication.primaryScreen()
            if scr is not None:
                return scr.availableGeometry()
        except Exception:
            pass
        return None

    def _move_near(self, global_pos):
        """默认显示在坐标点右下方；越界则收进屏幕，底部放不下翻到点上方。"""
        try:
            x = int(global_pos.x()) + 12
            y = int(global_pos.y()) + 16
            geo = self._available_geometry(global_pos)
            if geo is not None:
                w, h = self.width(), self.height()
                right = geo.x() + geo.width() - w
                bottom = geo.y() + geo.height() - h
                if x > right:
                    x = right
                if y > bottom:
                    y = int(global_pos.y()) - h - 8
                x = max(geo.x(), x)
                y = max(geo.y(), y)
            self.move(x, y)
        except Exception:
            pass

    # ---- 关闭 ----
    def _fade_out(self):
        """到点淡出（约 150ms）；任何特效失败都立即关闭，绝不留下悬挂气泡。"""
        if self._closed:
            return
        try:
            eff = QGraphicsOpacityEffect(self)
            self.setGraphicsEffect(eff)
            anim = QPropertyAnimation(eff, b"opacity", self)
            anim.setDuration(150)
            anim.setStartValue(1.0)
            anim.setEndValue(0.0)
            anim.setEasingCurve(QEasingCurve.OutCubic)
            anim.finished.connect(self.close_toast)
            self._fade_anim = anim
            anim.start()
        except Exception:
            self.close_toast()

    def close_toast(self):
        """立即关闭并注销（幂等；计时器随控件一起销毁）。"""
        if self._closed:
            return
        self._closed = True
        try:
            self._timer.stop()
        except Exception:
            pass
        try:
            if self in _ACTIVE_TOASTS:
                _ACTIVE_TOASTS.remove(self)
        except Exception:
            pass
        try:
            self.hide()
        except Exception:
            pass
        try:
            self.deleteLater()
        except Exception:
            pass


def show_toast(anchor_widget, global_pos, text, msec=900):
    """在 global_pos（全局坐标）附近弹一个自动消失的气泡，返回控件或 None。

    anchor_widget：宿主控件，仅用于建立 transient 关系；不是 QWidget（如既有
    测试桩）时忽略。任何创建失败都静默返回 None——提示气泡绝不打断主流程。
    """
    try:
        parent = anchor_widget if isinstance(anchor_widget, QWidget) else None
        toast = ToastBubble(global_pos, text, msec, parent)
        _ACTIVE_TOASTS.append(toast)
        toast.show()
        return toast
    except Exception:
        return None
