# -*- coding: utf-8 -*-
"""窗口尺寸/滚动助手：标签键循环、DPI 自适应收敛（纯函数）、可滚动页面容器。

Stage 6f 从 ui/main_window.py 原样拆出；函数体/签名/文档字符串逐字未改。
归属：MainWindow 经 `from .window.chrome import ...` 再导出使用。
"""
from PyQt5.QtWidgets import QScrollArea, QFrame
from PyQt5.QtCore import Qt

from .consts import _SCREEN_MARGIN, _MIN_FLOOR_W, _MIN_FLOOR_H


def _cycle_key(keys, current, step):
    """在标签键序列上按 step 前进/后退一格并循环（末页->首页 / 首页->末页）。

    keys 少于 2 个时原样返回 current：切换无意义，且不得越界/崩溃。
    current 不在 keys 中（如启动早期）时从第一个算起。
    """
    keys = list(keys)
    if len(keys) <= 1:
        return current
    try:
        idx = keys.index(str(current))
    except ValueError:
        idx = 0
    return keys[(idx + int(step)) % len(keys)]


def fit_window_size(desired_w, desired_h, avail_w, avail_h, margin=_SCREEN_MARGIN):
    """把期望窗口尺寸收敛进屏幕可用区（全部为逻辑像素）。纯函数，便于离线单测。

    返回 (w, h) = min(期望值, 可用区 - 2*margin)；屏幕小到装不下边距时
    仍至少留 1px 可显示，绝不返回 0/负值。
    """
    cap_w = max(1, int(avail_w) - 2 * int(margin))
    cap_h = max(1, int(avail_h) - 2 * int(margin))
    return max(1, min(int(desired_w), cap_w)), max(1, min(int(desired_h), cap_h))


def fit_min_size(hint_w, hint_h, avail_w, avail_h, margin=_SCREEN_MARGIN,
                 floor_w=_MIN_FLOOR_W, floor_h=_MIN_FLOOR_H):
    """窗口最小尺寸 = min(max(布局最小尺寸, 下限), 可用区)。纯函数，便于离线单测。

    - 「布局最小尺寸」是 QWidget.minimumSizeHint()（可能被长路径/长文件名顶大）；
    - 「下限」保证窗口不被拖成不可操作的小条；
    - 「可用区」是**硬上限**：setMinimumSize 绝不能要求超过屏幕能显示的尺寸，
      否则窗口必有一部分永远在屏外（高缩放 + 小屏的典型故障）。
    """
    cap_w = max(1, int(avail_w) - 2 * int(margin))
    cap_h = max(1, int(avail_h) - 2 * int(margin))
    w = min(max(int(hint_w), int(floor_w)), cap_w)
    h = min(max(int(hint_h), int(floor_h)), cap_h)
    return max(1, w), max(1, h)


class _PageScroll(QScrollArea):
    """页面滚动容器：把内层最小高度抬到其首选高度（显示后才算得准）。

    QScrollArea 只在「视口 < 内层最小尺寸」时给滚动条；页面隐藏时 sizeHint
    偏小，若只在窗口显示时算一次，首次切到该页时仍会被压扁（日志页实测
    hint 327 -> 397）。这里每次容器显示都补算一次；只增不减，避免抖动。
    """

    def showEvent(self, event):
        super().showEvent(event)
        inner = self.widget()
        if inner is None:
            return
        try:
            h = int(inner.sizeHint().height())
            if h > int(inner.minimumHeight()):
                inner.setMinimumHeight(h)
        except Exception:
            pass


def _make_scrollable_page(inner):
    """把页面内容放进纵向滚动容器（小屏 / 高缩放下不被压扁）。

    QScrollArea(widgetResizable=True)：装得下时内层页面被撑满视口，外观与
    不加容器完全一致；装不下时出现滚动条、页面按自身最小高度渲染——绝不把
    控件压到最小尺寸以下（200% 缩放 + 小屏时任务表会被压成一条线）。
    无边框 + NoFocus：不改既有视觉与 Tab 焦点链（滚轮滚动不受影响）。
    """
    sa = _PageScroll()
    sa.setObjectName("pageScroll")
    sa.setWidgetResizable(True)
    sa.setFrameShape(QFrame.NoFrame)
    sa.setFocusPolicy(Qt.NoFocus)
    sa.setHorizontalScrollBarPolicy(Qt.ScrollBarAsNeeded)
    sa.setVerticalScrollBarPolicy(Qt.ScrollBarAsNeeded)
    sa.setWidget(inner)
    return sa
