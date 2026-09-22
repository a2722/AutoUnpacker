# -*- coding: utf-8 -*-
"""ui/widgets 包共享底层：主题 token 取色、repolish、线性图标、状态灯、任务行格式化等 helper 与常量。

取色规则：只用 style.py 的 QSS token（经 style.tokens() 取）与 PALETTE；
动态属性（selected/active/checked/urgency）在 setProperty 后必须 repolish()。
"""

from PyQt5.QtWidgets import QLabel, QWidget
from PyQt5.QtCore import Qt, QRectF, QPointF
from PyQt5.QtGui import QBrush, QColor, QPainter, QPainterPath, QPen

from ...config import _HK_NAME_BY_VK
from ..style import PALETTE
from .. import style as ui_style
# 系统主题变化广播：WM_SETTINGCHANGE 的 lParam 为 "ImmersiveColorSet" 时表示深浅色变了
WM_SETTINGCHANGE = 0x001A


try:
    import win32gui
except ImportError:
    win32gui = None


def _key_display_name(qt_key):
    """Qt.Key -> 显示名（用于 HotkeyEdit 捕获后回显）。"""
    if Qt.Key_A <= qt_key <= Qt.Key_Z:
        return chr(qt_key)
    if Qt.Key_0 <= qt_key <= Qt.Key_9:
        return chr(qt_key)
    if Qt.Key_F1 <= qt_key <= Qt.Key_F24:
        return "F%d" % (qt_key - Qt.Key_F1 + 1)
    return _HK_NAME_BY_VK.get(qt_key)


# ===========================================================================
# M2：主窗口重写用的新控件家族
# ---------------------------------------------------------------------------
# 取色规则：只用 style.py 的 QSS token（自绘控件经 style.tokens() 取）与 PALETTE；
# 动态属性（selected/active/checked/urgency）在 setProperty 后必须 repolish()。
# ===========================================================================


_DIR_STATE_ALIASES = {
    "running": "extracting", "idle": "listening", "wait": "waiting",
    "err": "error", "failed": "error", "failure": "error",
}


def dir_state_key(state):
    """把外部状态值规整成 DirState：listening|extracting|waiting|paused|error。"""
    s = str(state or "").strip().lower()
    return _DIR_STATE_ALIASES.get(s, s or "listening")


def _tk(name, fallback=""):
    """取当前主题的 QSS token 值（自绘控件取色用；异常回退）。"""
    try:
        return ui_style.tokens().get(name) or fallback
    except Exception:
        return fallback


def repolish(w):
    """重算动态属性选择器：setProperty(...) 之后必须调用（Qt 不会自动重算 QSS）。"""
    try:
        w.style().unpolish(w)
        w.style().polish(w)
        w.update()
    except Exception:
        pass


def repolish_tree(w):
    """连同 QLabel 子控件一起 repolish（属性选择器的后代规则需要子控件重算）。"""
    repolish(w)
    try:
        for child in w.findChildren(QLabel):
            repolish(child)
    except Exception:
        pass


def _clear_layout(lay):
    """清空布局并销毁其中的控件（takeAt 保证布局项不残留）。"""
    try:
        while lay.count():
            item = lay.takeAt(0)
            w = item.widget() if item is not None else None
            if w is not None:
                try:
                    w.setParent(None)
                    w.deleteLater()
                except Exception:
                    pass
    except Exception:
        pass


# ---- 线性图标（24x24 设计坐标；stroke 风格对齐 mockups/assets/parts.js 的 SVG）----
_GLYPHS = {
    "plus": {"lines": [[(12, 5), (12, 19)], [(5, 12), (19, 12)]]},
    "close": {"lines": [[(5, 5), (19, 19)], [(19, 5), (5, 19)]]},
    "check": {"lines": [[(4, 12.5), (9, 17.5), (20, 6.5)]]},
    "gear": {"circles": [((12, 12), 3)],
             "lines": [[(12, 3), (12, 5.5)], [(12, 18.5), (12, 21)],
                       [(4.2, 7.5), (6.4, 8.8)], [(17.6, 15.2), (19.8, 16.5)],
                       [(4.2, 16.5), (6.4, 15.2)], [(17.6, 8.8), (19.8, 7.5)]]},
    "alert": {"lines": [[(12, 3), (21, 19), (3, 19), (12, 3)],
                        [(12, 9), (12, 14)], [(12, 17), (12, 17.01)]]},
    "refresh": {"arcs": [((12, 12), 8, 45, 315)],
                "lines": [[(20, 4), (20, 9)], [(20, 4), (15, 4)]]},
    "search": {"circles": [((11, 11), 6)], "lines": [[(16, 16), (20, 20)]]},
    "external": {"lines": [[(14, 4), (20, 4), (20, 10)], [(20, 4), (12, 12)],
                           [(18, 14), (18, 19), (5, 19), (5, 7), (10, 7)]]},
    "trash": {"lines": [[(4, 7), (20, 7)], [(9, 7), (9, 5), (15, 5), (15, 7)],
                        [(6, 7), (7, 20), (17, 20), (18, 7)],
                        [(10, 11), (10, 17)], [(14, 11), (14, 17)]]},
    "folder": {"lines": [[(3, 7), (3, 19), (21, 19), (21, 9), (11, 9), (9, 7), (3, 7)]]},
    "archive": {"rects": [((3, 4, 18, 4), 1)],
                "lines": [[(5, 8), (5, 18)], [(19, 8), (19, 18)], [(5, 18), (19, 18)],
                          [(10, 12), (14, 12)]]},
    "terminal": {"rects": [((3, 4, 18, 16), 2)],
                 "lines": [[(7, 9), (10, 12), (7, 15)], [(13, 15), (17, 15)]]},
    "queue": {"lines": [[(4, 6), (20, 6)], [(4, 12), (20, 12)], [(4, 18), (13, 18)]],
              "circles": [((18.5, 18.5), 2.5)]},
    "key": {"circles": [((8, 12), 4)],
            "lines": [[(12, 12), (21, 12)], [(18, 12), (18, 15)], [(15, 12), (15, 14)]]},
    "history": {"arcs": [((12, 12), 9, 40, 330)],
                "lines": [[(3, 4), (3, 9), (8, 9)], [(12, 8), (12, 12), (15, 14)]]},
    "shield": {"lines": [[(12, 3), (19, 6), (19, 12), (12, 21), (5, 12), (5, 6), (12, 3)]]},
    "bolt": {"lines": [[(13, 3), (5, 14), (11, 14), (10, 21), (18, 10), (12, 10), (13, 3)]]},
    "dashboard": {"rects": [((3, 3, 7, 9), 1), ((14, 3, 7, 5), 1),
                            ((14, 11, 7, 10), 1), ((3, 15, 7, 6), 1)]},
    "pause": {"lines": [[(9, 5), (9, 19)], [(15, 5), (15, 19)]]},
    "download": {"lines": [[(12, 4), (12, 14)], [(8, 10), (12, 14), (16, 10)],
                           [(4, 19), (20, 19)]]},
    "more": {"circles": [((5, 12), 1.6), ((12, 12), 1.6), ((19, 12), 1.6)]},
    "info": {"circles": [((12, 12), 9)],
             "lines": [[(12, 11), (12, 16)], [(12, 7.5), (12, 7.51)]]},
}


def _draw_glyph(painter, name, rect, color, width=1.8):
    """把一个线性图标画进 rect（24x24 设计坐标等比缩放）。"""
    g = _GLYPHS.get(name)
    if not g:
        return
    painter.save()
    painter.setRenderHint(QPainter.Antialiasing, True)
    pen = QPen(QColor(color))
    pen.setWidthF(float(width))
    pen.setCapStyle(Qt.RoundCap)
    pen.setJoinStyle(Qt.RoundJoin)
    painter.setPen(pen)
    painter.setBrush(Qt.NoBrush)
    sx = rect.width() / 24.0
    sy = rect.height() / 24.0

    def _x(v):
        return rect.x() + v * sx

    def _y(v):
        return rect.y() + v * sy

    for poly in g.get("lines", []):
        path = QPainterPath()
        path.moveTo(_x(poly[0][0]), _y(poly[0][1]))
        for pt in poly[1:]:
            path.lineTo(_x(pt[0]), _y(pt[1]))
        painter.drawPath(path)
    for (cx, cy), r in g.get("circles", []):
        painter.drawEllipse(QPointF(_x(cx), _y(cy)), r * sx, r * sy)
    for (rx, ry, rw, rh), rad in g.get("rects", []):
        painter.drawRoundedRect(
            QRectF(_x(rx), _y(ry), rw * sx, rh * sy), rad * sx, rad * sy)
    for (cx, cy), r, a1, a2 in g.get("arcs", []):
        painter.drawArc(QRectF(_x(cx - r), _y(cy - r), 2 * r * sx, 2 * r * sy),
                        int(a1 * 16), int((a2 - a1) * 16))
    painter.restore()


def _qcolor(spec, fallback="#000000"):
    """把 token 颜色字符串转 QColor：QColor 不认 CSS 的 rgba(...)，这里手动解析。"""
    s = str(spec or "").strip()
    try:
        if s.startswith("rgba(") and s.endswith(")"):
            parts = [p.strip() for p in s[5:-1].split(",")]
            if len(parts) == 4:
                r, g, b = (int(round(float(p))) for p in parts[:3])
                a = float(parts[3])
                if a <= 1.0:
                    a = int(round(a * 255.0))
                return QColor(max(0, min(255, r)), max(0, min(255, g)),
                              max(0, min(255, b)), max(0, min(255, int(a))))
        return QColor(s)
    except Exception:
        return QColor(fallback)


def _lamp_color(state):
    """目录状态 -> 灯色（只由状态决定；跟着当前主题 token 走）。"""
    key = dir_state_key(state)
    if key == "extracting":
        return PALETTE["success"]
    if key in ("waiting", "missing"):
        return _tk("nbar_warn", PALETTE["muted"])
    if key == "error":
        return PALETTE["danger"]
    if key == "paused":
        return PALETTE["muted"]
    return PALETTE["accent"]


class _StatusLamp(QWidget):
    """状态灯（自绘圆点）：颜色只由 set_state 决定，选中/筛选态绝不影响它。"""

    SIZE = 8

    def __init__(self, parent=None):
        super().__init__(parent)
        self._state = "listening"
        self.setFixedSize(self.SIZE, self.SIZE)
        self.setAttribute(Qt.WA_TransparentForMouseEvents, True)

    def set_state(self, state):
        self._state = dir_state_key(state)
        self.update()

    def state(self):
        return self._state

    def color_name(self):
        """当前灯色的 #rrggbb（断言用）。"""
        return QColor(_lamp_color(self._state)).name()

    def paintEvent(self, event):
        p = QPainter(self)
        p.setRenderHint(QPainter.Antialiasing, True)
        p.setPen(Qt.NoPen)
        p.setBrush(QBrush(QColor(_lamp_color(self._state))))
        p.drawEllipse(QRectF(0.5, 0.5, self.width() - 1.0, self.height() - 1.0))
        p.end()


_STATE_ALIASES = {
    "pending": "queued", "waiting": "need_password", "wait": "need_password",
    "error": "failed", "success": "done", "cancelled": "canceled",
    "running": "extracting", "ok": "done",
}


def _task_state_key(row):
    s = str(row.get("state") or "").strip().lower()
    return _STATE_ALIASES.get(s, s)


# 显示行指纹用的字段：任一字段变化都会改变表格内容（用于跳过无变化的整表重建）
_TASK_SIG_KEYS = ("id", "state", "state_text", "file_name", "file", "output_dir",
                  "out", "size", "file_size", "pwd", "password_src", "cost",
                  "started_at", "finished_at", "created_at", "error", "progress")


def _task_rows_signature(rows):
    """显示行集合的廉价指纹：只比较会影响展示的字段，不做任何格式化。"""
    return tuple(tuple(r.get(k) for k in _TASK_SIG_KEYS)
                 for r in (rows or []) if isinstance(r, dict))


def _row_file(row):
    return str(row.get("file_name") or row.get("file") or "")


def _row_out(row):
    return str(row.get("output_dir") or row.get("out") or "")


def _fmt_size(n):
    try:
        v = float(n)
    except Exception:
        return "—"
    if v <= 0:
        return "—"
    for div, name in ((1024 ** 3, "GB"), (1024 ** 2, "MB"), (1024, "KB")):
        if v >= div:
            x = v / div
            txt = ("%.1f" % x) if x < 100 else ("%.0f" % x)
            if txt.endswith(".0"):
                txt = txt[:-2]
            return "%s %s" % (txt, name)
    return "%d B" % int(v)


def _fmt_cost(row):
    try:
        st = float(row.get("started_at") or 0)
        ft = float(row.get("finished_at") or 0)
    except Exception:
        return "—"
    if st <= 0 or ft <= 0 or ft < st:
        return "—"
    secs = int(round(ft - st))
    return "%02d:%02d" % (secs // 60, secs % 60)


def _fmt_pwd(row):
    src = str(row.get("password_src") or "").strip()
    if not src:
        state = _task_state_key(row)
        if state == "need_password":
            return "未命中"
        if state == "done":
            return "无需"
        return "—"
    if "#" in src:
        kind, _, num = src.partition("#")
        return "命中 #%s" % num if kind in ("book", "dict") else src
    if src == "filename":
        return "命中 文件名"
    return src


def _pill_colors(state):
    """状态胶囊的 (底, 边, 字) 三色；全部来自 token/PALETTE。"""
    if state == "extracting":
        return _tk("accent_soft"), _tk("ctl_focus"), PALETTE["accent_text"]
    if state == "need_password":
        return _tk("pause_bg"), _tk("pause_border"), _tk("pause_fg")
    if state == "done":
        return _tk("accent_soft"), _tk("card_border"), PALETTE["success"]
    if state == "failed":
        return PALETTE["warn_bg"], _tk("danger_border"), _tk("danger_fg")
    return _tk("cat_hover"), _tk("card_border"), PALETTE["muted2"]
