# -*- coding: utf-8 -*-
"""点击反馈小气泡（日志链接「已复制 / 复制失败」）：无边框、绝不抢焦点、自动消失。

_ACTIVE_TOASTS 是包内唯一的共享强引用列表（本模块定义，包 __init__ 重导出），
ToastBubble 与 show_toast 都只操作这一个列表对象。"""

from PyQt5.QtWidgets import QLabel, QApplication, QGraphicsOpacityEffect, QWidget
from PyQt5.QtCore import (Qt, QTimer, QPropertyAnimation, QEasingCurve, QPoint)

from ..textfit import fit_text_heights
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
        # 顶层无边框窗默认自带不透明窗口底色：QSS 只把 #toastBubble 的背景画成
        # 圆角，圆角外的四个方角仍是窗口底色（真机上就是黑角）。必须在首次 show
        # 之前置半透明，圆角外才真正透明（桌面透出）。
        self.setAttribute(Qt.WA_TranslucentBackground, True)
        self.setAttribute(Qt.WA_TransparentForMouseEvents, True)
        self.setAttribute(Qt.WA_ShowWithoutActivating, True)
        self.setText(str(text))
        # CJK 墨迹盒顶/底 1~2px 裁切兜底（顶层 ToolTip 也是最终绘制面；只抬不降）。
        # 必须在 adjustSize() 之前：最小高度参与 sizeHint 约束。
        fit_text_heights(self)
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


def show_toast_top(anchor_widget, text, msec=1800, top_offset=12):
    """**应用内顶部居中**的临时提示（非阻塞），返回控件或 None。

    为什么单独一个入口：像「该口令已在密码本中」这类"只是告诉你一声"的反馈，
    过去用 QMessageBox.information 弹模态框 —— 用户必须点 OK 才能继续，等于把
    任何一次误重复操作都变成一次打断。这里改用气泡：不抢焦点（
    WA_ShowWithoutActivating + WA_TransparentForMouseEvents）、不拦鼠标、
    1.8 秒自动淡出，用户可以完全无视它继续干活。

    定位：以 anchor_widget 所在**顶层窗口**为准，水平居中、顶部下移 top_offset。
    ToastBubble 的宽度只有构造后才知道，所以先按中线坐标建，再按实际宽度回正；
    该窗口不在屏幕上（隐藏 / 最小化）时退回气泡自带的屏幕钳制，不会跑到屏幕外。

    任何异常静默返回 None —— 提示绝不打断主流程（调用方也不需要 try）。
    """
    try:
        parent = anchor_widget if isinstance(anchor_widget, QWidget) else None
        win = anchor_widget.window() if isinstance(anchor_widget, QWidget) else None
        geo = (win or anchor_widget).geometry() if (win or anchor_widget) else None
        if geo is None:
            return None
        center_x = geo.center().x()
        pos = QPoint(center_x, geo.top() + int(top_offset))
        toast = show_toast(anchor_widget, pos, text, msec)
        if toast is not None:
            # 回正：按实际宽度水平居中（失败也不影响已显示的提示）
            try:
                toast.move(center_x - toast.width() // 2,
                           int(geo.top()) + int(top_offset))
            except Exception:
                pass
        return toast
    except Exception:
        return None

