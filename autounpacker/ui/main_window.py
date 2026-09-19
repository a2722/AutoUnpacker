# -*- coding: utf-8 -*-
"""主窗口 MainWindow：监听卡片管理、托盘、日志、暂停/恢复、全局快捷键、网址信任、拖放临时解压。

职责：- 组装主界面（监听路径卡片、日志区、进度条）并定时消费 Hub 队列
- 托盘图标与最小化到托盘、单实例事件响应、Esc/Ctrl+W 关闭行为
- 全局热键注册（RegisterHotKey + 原生事件过滤）、网址信任确认弹窗调度
- 拖入文件临时解压、首次启动 7-Zip 检测
关键入口：MainWindow / _first_run_7z_check()
依赖：PyQt5、hub、state、extract、dialogs、widgets、password_book、trust
注意：stdout 捕获与 Qt 插件路径由 app.main 统一处理，本模块不重复安装
"""
import html
import queue
import re
import threading
import time
import types

from PyQt5.QtWidgets import (QApplication, QMainWindow, QWidget, QVBoxLayout, QHBoxLayout, QPushButton, QPlainTextEdit, QSystemTrayIcon, QMenu, QShortcut, QMessageBox, QStackedWidget, QDialog, QScrollArea, QFrame)
from PyQt5.QtCore import Qt, QTimer, QEvent
from PyQt5.QtGui import QKeySequence, QTextCursor, QTextCharFormat, QColor, QCursor

from .. import extract as smart_extract    # noqa: F401
from .. import trail as deletion_trail     # noqa: F401
from .. import sevenzip as sevenzip_manager  # noqa: F401
from .. import baidu_manifest as bm
from .. import db
from .. import hub
from ..config import (parse_hotkey, HOTKEY_ID, HOTKEY_ID_SHARE,
                      HOTKEY_ID_SHARE_CODE, MOD_NOREPEAT)
from ..trust import add_trust_entry
from .password_book import PasswordBookDialog
from ..utils import (_norm_path_for_cfg, split_urls, is_baidu_pan_url,
                     watch_path_conflict)
from .widgets import (WatchCard,  # noqa: F401  （M3 起主界面不再创建，保留给目录弹窗/后续里程碑）
                      RainbowBorderButton,  # noqa: F401  （M3 起主界面不再使用，保留导入）
                      make_tray_icon, _HotkeyFilter, NavTabs, DirChipStrip,
                      show_toast)
from . import style as ui_style
from .style import PALETTE
from . import pages as ui_pages
from .pages import TaskPage, LogPage, StatusBar
from .page_pwbook import PasswordBookPage
from .page_trail import TrailPage
from .page_settings import SettingsPage
from .dialogs import (SettingsDialog, DeleteTrailDialog, SevenZipSetupDialog,
                      CloseActionDialog, TrustAskDialog, WatchDirDialog)

try:
    import win32api
    import win32con
    import win32gui
    import winerror
except ImportError:
    win32api = win32con = win32gui = winerror = None

# d3：挑选文件期间可能耗时很久，而分享会话的 sekey 寿命未知——提交前若已超过该
# 秒数（从 prepare 起算），先重新 prepare 并按 fs_id 重映射选择，再提交。
SHARE_PREP_STALE_SEC = 120
# 挑选窗最长等待秒数：超时未选择按「取消」处理，避免忙标志被永久占用。
SHARE_PICK_WAIT_SEC = 1800
# 二维码解码「预定任务」时效：手势落空时登记，解码出百度分享链接后自动补按；
# 超过该秒数未等到 share_link 事件即作废（避免几秒后的无关分享被误当成该次手势）。
# 注意：仅作「无 config 可用」时的缺省值；实际等待秒数见 config 的
# share_gesture_wait_sec（默认同为 60，clamp 5..600，见 _share_gesture_wait_sec）。
PENDING_SHARE_TTL_SEC = 60
# 后台拉起忙标志最长存活秒数：worker 卡死（如 os.startfile 悬挂）时强制复位，
# 避免后续所有手势被永久挡住；复位会记一行日志。
SHARE_INVOKE_BUSY_MAX_SEC = 180
# 「手势刚拉起、同一链接又经解析管线出现」的静默去重窗口（秒）：手势（含预定
# 派发）拉起某 surl 后，同 surl 经 share_link 自动分支再次出现时不再重复拉起，
# 也绝不弹「重复分享」确认（那就是同一次用户意图）。
SHARE_GESTURE_DEDUP_SEC = 30
# 预定任务的 kind → 文案。(登记日志, 托盘通知正文)；托盘标题两路都用「二维码正在解析」。
# Alt+2（share）文案已验收，逐字不动；Alt+3（share_code）为固定提取码那一路。
# 将来把 Alt+2/Alt+3 合并成一个热键时，只需改这里的条目 + _dispatch_pending_share 的一行。
PENDING_SHARE_TEXT = {
    "share": (
        "[分享] 二维码正在解析，已登记预定任务：解析出百度分享链接后自动拉起",
        "解析完成后若是百度分享链接，将自动拉起客户端（无需再按）"),
    "share_code": (
        "[分享] 二维码正在解析，已登记预定任务：解析出百度分享链接后按固定提取码自动拉起",
        "解析完成后若是百度分享链接，将按固定提取码自动拉起客户端（无需再按）"),
}
# kind → 手势标签（仅用于「已刷新」等需要说明是哪一路的日志）。
PENDING_SHARE_TAG = {"share": "Alt+2", "share_code": "Alt+3"}

# ---------------------------------------------------------------------------
# 拖放二维码图片：只对以下扩展名做「魔数 → 二维码」识别。先看扩展名（廉价），
# 再看前 32 字节魔数；绝不为「一眼就是图片」的文件跑昂贵的多格式归档扫描。
# 伪装成图片扩展名的压缩包（PK/7z/Rar 魔数）仍回落到归档路径，既有能力不破。
# ---------------------------------------------------------------------------
IMAGE_EXTS = {"png", "jpg", "jpeg", "bmp", "gif", "webp", "tif", "tiff", "ico"}
# 拖放二维码图片识别的大小上限：与 extract.POLYGLOT_FRONT_SCAN_LIMIT /
# TRANSLATION_MAX_SIZE 同量级（16MB），超过只如实记一行日志、不做识别。
_QR_DROP_MAX_BYTES = 16 * 1024 * 1024
# 伪装成图片扩展名的压缩包魔数：命中即回落归档路径（含空归档 PK\x05\x06）。
_QR_ARCHIVE_MAGICS = (b"PK\x03\x04", b"PK\x05\x06",
                      b"7z\xbc\xaf\x27\x1c", b"Rar!\x1a\x07")

# ---------------------------------------------------------------------------
# 日志里的网址：渲染成可点 <a>，单击静默复制一次后降级为普通文本。
# 渲染（_render_log_html）与点击定位（MainWindow._hit_log_link）共用
# utils.split_urls 的同一套 URL 边界（中文/全角括号处必停），避免两处规则漂移。
# ---------------------------------------------------------------------------
# Hub.log 入队的日志行自带 "[HH:MM:SS] " 前缀（见 hub.log）。_drain 会把它们也交给
# _append_log；这些行已由 Hub.log 写入 log_index，此处据此跳过，避免重复计数。
_HUB_LOG_PREFIX = re.compile(r"^\[\d{2}:\d{2}:\d{2}\] ")
# 捕获类日志的「值」标签：命中后把「值」渲染成等宽 + 淡底 chip（新标签在此追加）。
_LOG_VALUE_LABELS = ("已捕获临时密码:", "已捕获临时密码：")

# 工作台标签页键 -> 页面栈索引（顺序与 _build_ui 里 addWidget 的顺序一致）
_PAGE_KEYS = ("tasks", "log", "password", "trail", "settings")


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

# ---------------------------------------------------------------------------
# 分辨率 / 文本缩放（DPI）自适应：Qt 开启 HiDPI 后屏幕 API 全部返回「逻辑像素」
# （= 物理像素 / 缩放系数），窗口尺寸只需在逻辑像素里收敛；QSS 的 px 与字号
# 同为逻辑像素，会自动缩放——任何地方都不要再自己乘缩放系数。
# ---------------------------------------------------------------------------
# 期望窗口四周留出的逻辑边距（不贴死屏幕边缘 / 不压住任务栏）
_SCREEN_MARGIN = 40
# 最小窗口下限（逻辑像素）：屏幕再小也保留基本操作空间；
# 但下限绝不突破「可用区 - 2*边距」硬上限（见 fit_min_size）。
_MIN_FLOOR_W = 720
_MIN_FLOOR_H = 460
# 窄于该宽度（逻辑像素）时收起次要装饰（胶囊条提示文案 + 底栏播报），
# 否则两条常显栏的自然宽度（胶囊条 ≈794、底栏 ≈853）会把控件挤出窗宽。
_CHROME_COMPACT_W = 940


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


def _find_log_urls(text):
    """返回 [(start, end, url)]：行内所有 http(s)/www 链接及其真实边界区间。

    统一委托 utils.split_urls：URL 边界与尾部标点规则只有一份。渲染与点击定位
    共用本函数，保证「能渲染成链接的」与「能点中的」完全一致。剔除尾部标点后
    为空则不收录；一行内多个链接全部收录。"""
    return split_urls(text)


def _log_value_spans(text):
    """返回 [(start, end)]：text 里应被渲染成「值 chip」的区间。

    只认 _LOG_VALUE_LABELS 里的标签；值 = 标签后跳过空格、到第一个空白/行尾为止
    的连续片段（空值不收录）。纯字符串处理，绝不抛异常。"""
    spans = []
    for label in _LOG_VALUE_LABELS:
        pos = 0
        while True:
            i = text.find(label, pos)
            if i < 0:
                break
            s = i + len(label)
            while s < len(text) and text[s] == " ":
                s += 1
            e = s
            while e < len(text) and not text[e].isspace():
                e += 1
            if e > s:
                spans.append((s, e))
            pos = i + len(label)
    return spans


def _render_log_html(msg, color, degraded=None):
    """把一行日志转成 HTML。

    颜色角色（冻结，「蓝色 = 仅可交互」）：
    1. `[HH:MM:SS] ` 时间前缀恒为暗色 muted（PALETTE['log_ts']），单独一个 span；
    2. 行内容按级别着色（info 用中性默认色，不再用蓝色）；
    3. 捕获类日志的值用等宽字体 + 淡底 chip（log_value_bg/log_value_fg），
       不使用级别色，也不加下划线；
    4. 全行只有 http(s)/www 链接是蓝色 + 下划线 + 手型光标（点击一次即复制并降级）。
    degraded：正文坐标下「已经点击复制过」的链接区间；命中即渲染成普通暗色
    （log_ts、无下划线、无 anchor）——重载/主题重绘后已复制链接绝不复活。
    逐片段 html.escape（链接单独 escape），QPlainTextEdit 解析后 block 纯文本仍
    等原始 msg，点击侧据同一 msg 用 _find_log_urls 精确定位。
    """
    m = _HUB_LOG_PREFIX.match(msg)
    ts = m.group(0) if m else ""
    body = msg[len(ts):]
    url_spans = _find_log_urls(body)
    degraded_spans = set(degraded or ())
    value_spans = []
    for s, e in _log_value_spans(body):
        if any(not (e <= us or s >= ue) for us, ue, _u in url_spans):
            continue          # 值 chip 与链接重叠：链接优先（唯一可交互文本）
        value_spans.append((s, e))
    marks = [(s, e, "url", url) for s, e, url in url_spans]
    marks += [(s, e, "value", None) for s, e in value_spans]
    marks.sort(key=lambda t: t[0])
    parts = []
    pos = 0
    for s, e, kind, url in marks:
        if s < pos:
            continue
        parts.append(html.escape(body[pos:s]))
        if kind == "url":
            esc = html.escape(url, quote=True)
            if (s, e) in degraded_spans:
                # 已复制过：与时间戳同色、无下划线、无 anchor（不再可点）
                parts.append(
                    f'<span style="color:{PALETTE["log_ts"]}">{esc}</span>')
            else:
                parts.append(
                    f'<a href="{esc}" style="color:{PALETTE["log_link"]};'
                    f'text-decoration:underline">{esc}</a>')
        else:
            parts.append(
                f'<span style="font-family:Consolas,\'Cascadia Mono\',monospace;'
                f'background-color:{PALETTE["log_value_bg"]};'
                f'color:{PALETTE["log_value_fg"]}">{html.escape(body[s:e])}</span>')
        pos = e
    parts.append(html.escape(body[pos:]))
    inner = "".join(parts)
    if ts:
        return (f'<span style="color:{PALETTE["log_ts"]}">{html.escape(ts)}</span>'
                f'<span style="color:{color}">{inner}</span>')
    return f'<span style="color:{color}">{inner}</span>'


def _copy_toast(win, text, pos=None):
    """在点击位置附近弹「已复制/复制失败」小气泡（module 级：只绑旧方法的桩也可用）。

    pos 缺省用点击时记下的全局坐标（_last_link_toast_pos），再缺省用当前光标位置；
    任何异常一律吞掉——提示气泡绝不打断主流程。返回气泡控件（建不出来则 None，
    调用方据此在失败回执里撤下它，避免「已复制」「复制失败」同屏）。"""
    try:
        if pos is None:
            pos = getattr(win, "_last_link_toast_pos", None)
        if pos is None:
            pos = QCursor.pos()
        return show_toast(win, pos, text)
    except Exception:
        return None


def _log_body(text):
    """去掉 Hub 的 "[HH:MM:SS] " 前缀后的行正文。

    降级记录跨「实时行（可能无前缀）→ 重载行（task_log_line 补前缀）」按正文
    匹配，前缀有无都不影响同一条逻辑行。"""
    return _HUB_LOG_PREFIX.sub("", str(text or ""))


def _record_degraded_link(win, box, hit):
    """登记一次「已点击复制」的链接（hit = _hit_log_link 的 (block, start, end, url)）。

    该记录有两个用途：1) 重载/主题重绘时把同一行重新渲染成降级文本（F3d/F3e：
    已复制链接绝不复活）；2) 复制失败回执按它精确恢复**被点击的那一行**（而不是
    整个文档里第一个已无 anchor 的同 URL 片段）。返回记录 dict 或 None。"""
    try:
        block, start, end, url = hit
    except Exception:
        return None
    if box is None or block is None or not url:
        return None
    try:
        raw = block.text()
        text = _log_body(raw)
        prefix = len(raw) - len(text)
        rec = {"view": box, "text": text, "start": int(start) - prefix,
               "end": int(end) - prefix, "url": str(url)}
        recs = getattr(win, "_log_degraded", None)
        if not isinstance(recs, list):
            recs = []
            win._log_degraded = recs       # 宿主不可写时异常即弃（见外层 except）
        recs.append(rec)
        while len(recs) > 200:             # 只留最近若干条，绝不无界增长
            del recs[0]
        return rec
    except Exception:
        return None


def _degraded_spans_for(win, box, msg):
    """该行正文里已被复制过、应渲染成降级文本的链接区间（无记录返回空表）。"""
    out = []
    try:
        body = _log_body(msg)
        for rec in getattr(win, "_log_degraded", None) or ():
            try:
                if rec.get("view") is not box or rec.get("text") != body:
                    continue
                s, e = int(rec.get("start", -1)), int(rec.get("end", -1))
                if 0 <= s < e <= len(body):
                    out.append((s, e))
            except Exception:
                continue
    except Exception:
        pass
    return out


def _pop_degraded_link(win, url):
    """取走该 URL 最早一条降级记录（复制失败按点击先后逐条恢复）。"""
    recs = getattr(win, "_log_degraded", None)
    if not isinstance(recs, list) or not url:
        return None
    for i, rec in enumerate(list(recs)):
        try:
            if str(rec.get("url")) == str(url):
                return recs.pop(i)
        except Exception:
            continue
    return None


def _restore_link_span(block, start, end, url):
    """给 block 上的 [start,end) 重套可点样式（anchor + 链接蓝 + 下划线）。"""
    try:
        fmt = QTextCharFormat()
        fmt.setAnchor(True)
        fmt.setAnchorHref(str(url or ""))
        fmt.setForeground(QColor(PALETTE["log_link"]))
        fmt.setFontUnderline(True)
        cursor = QTextCursor(block)
        cursor.setPosition(block.position() + int(start))
        cursor.setPosition(block.position() + int(end), QTextCursor.KeepAnchor)
        cursor.setCharFormat(fmt)
    except Exception:
        pass


def _restore_degraded_in_view(rec):
    """按降级记录精确恢复被点击的那一行（行正文 + 正文内区间都匹配）。

    刻意不再退回「第一个已无 anchor 的片段」：同一 URL 出现在多行时会恢复错行，
    反而把被点击的行留在降级态。返回是否恢复成功。"""
    if not isinstance(rec, dict):
        return False
    box = rec.get("view")
    text = str(rec.get("text") or "")
    try:
        start, end = int(rec.get("start", -1)), int(rec.get("end", -1))
    except Exception:
        return False
    if not text or start < 0 or end <= start:
        return False
    try:
        block = box.document().firstBlock()
    except Exception:
        return False
    while block.isValid():
        raw = block.text()
        body = _log_body(raw)
        if body == text:
            prefix = len(raw) - len(body)
            _restore_link_span(block, prefix + start, prefix + end,
                               rec.get("url"))
            return True
        block = block.next()
    return False


def _rerender_log_view(win, box):
    """主题切换后按当前调色板重画已画出的行（保留已复制链接的降级状态）。

    逐块按正文重渲染：行文本与块数都不变（不用 clear 重建），滚动位置与选区
    自然保留；纯文本模式（log_colors_enabled=False）本就没有内联色，直接跳过。"""
    if box is None:
        return
    try:
        if not bool(win.state.snapshot().get("log_colors_enabled", True)):
            return
    except Exception:
        pass
    try:
        doc = box.document()
        cursor = QTextCursor(doc)
        cursor.beginEditBlock()
        for i in range(doc.blockCount()):
            block = doc.findBlockByNumber(i)
            if not block.isValid():
                continue
            text = block.text()
            if not text:
                continue
            cur = QTextCursor(block)
            cur.setPosition(block.position())
            cur.setPosition(block.position() + block.length() - 1,
                            QTextCursor.KeepAnchor)
            cur.insertHtml(_render_log_html(
                text, win._log_color_for(text),
                _degraded_spans_for(win, box, text)))
        cursor.endEditBlock()
    except Exception:
        pass


def _restore_link_in_view(box, url):
    """在指定日志视图里尽力恢复可点样式：找第一个已无 anchor 的该 URL 片段重套。

    module 级实现：既有日志桩只绑定 `_restore_log_link`，不应依赖新增的实例方法。"""
    if not url:
        return
    try:
        doc = box.document()
        cursor = doc.find(url)
        while not cursor.isNull():
            block = cursor.block()
            base = block.position()
            start = cursor.selectionStart() - base
            end = cursor.selectionEnd() - base
            if not MainWindow._block_span_has_anchor(block, start, end):
                _restore_link_span(block, start, end, url)
                return
            cursor = doc.find(url, cursor)
    except Exception:
        pass


def _log_view_for(win, obj):
    """obj 属于哪个日志视图的 viewport（该任务日志 / 运行日志页），否则 None。

    module 级实现：既有日志桩以非 QWidget 桩直接调用 eventFilter，不能依赖
    桩上未绑定的新增实例方法。"""
    lb = getattr(win, "log_box", None)
    try:
        if lb is not None and obj is lb.viewport():
            return lb
    except Exception:
        pass
    lp = getattr(win, "log_page", None)
    try:
        if lp is not None and obj is lp.log_view.viewport():
            return lp.log_view
    except Exception:
        pass
    return None


def _accepts_in_task_view(win, record):
    """该任务日志视图是否接收该记录（module 级：测试桩未绑定同名方法也可用）。

    无 task_page（旧桩/旧调用）时保持旧行为：一律接收。"""
    tp = getattr(win, "task_page", None)
    if tp is None:
        return True
    try:
        return tp.accepts_record(record)
    except Exception:
        return True


def _append_log_html(win, box, msg):
    """按既定「着色 + 可点链接」管线把一行写入指定视图（多视图共用，渲染器只有一份）。

    module 级实现：既有测试直接以非 QWidget 桩调用 MainWindow._append_log，
    桩上不会绑定实例方法，故渲染助手不能依赖 self. 查找。"""
    try:
        colored = bool(win.state.snapshot().get("log_colors_enabled", True))
    except Exception:
        colored = True
    if not colored:
        # 纯文本模式：不做链接渲染（无 <a>、无点击）。已知取舍：关掉彩色即无链接。
        box.appendPlainText(msg)
        return
    box.appendHtml(_render_log_html(msg, win._log_color_for(msg),
                                    _degraded_spans_for(win, box, msg)))


# ---------------------------------------------------------------------------
# 提取码取值 / 缺码小窗：module 级实现 + 类方法薄包装。
#
# 之所以放 module 级：既有大量测试用「非 QWidget 桩」直接调用
# `mw.MainWindow._open_recent_share(stub)` / `_share_ask_code`，这些桩只绑定了
# 它当时触达的方法名；若新逻辑改调 `self._effective_share_code(...)`，这些桩会
# 因缺方法而 AttributeError。module 级函数让旧桩继续可用，同时类上仍暴露
# `MainWindow._effective_share_code` / `_show_share_code_window` 作为正式助手。
# ---------------------------------------------------------------------------

def _share_log(win, msg):
    """把一行日志写到主窗：优先 `_append_log`（Qt 线程），异常/缺失时退回 hub.log。"""
    fn = getattr(win, "_append_log", None)
    if callable(fn):
        try:
            fn(msg)
            return
        except Exception:
            pass
    try:
        win.hub.log(msg)
    except Exception:
        pass


def _persist_log(win, msg):
    """固定提取码绑定的诊断日志（线程安全分流，module 级以免依赖实例绑定）。

    Qt 主线程（面板/热键路径）直接 `_append_log` 落日志；后台 worker 线程
    （Alt+3 提交成功后绑定）绝不能碰控件，改走 `hub.log` 由 `_drain` 消费。"""
    try:
        if threading.current_thread() is threading.main_thread():
            _share_log(win, msg)
            return
    except Exception:
        pass
    try:
        win.hub.log(msg)
    except Exception:
        pass


def _share_dlg_target(dlg):
    """读小窗归属的目标 (surl, share_uk)。

    优先冻结访问器 `target_surl()` / `target_uk()`；尚未落地时回退旧属性
    `surl` / `share_uk`。任何异常都返回 ("", "")，绝不向外抛。"""
    if dlg is None:
        return "", ""
    out = []
    for fn_name, attr in (("target_surl", "surl"), ("target_uk", "share_uk")):
        val = ""
        fn = getattr(dlg, fn_name, None)
        if callable(fn):
            try:
                val = fn() or ""
            except Exception:
                val = ""
        if not val:
            try:
                val = getattr(dlg, attr, "") or ""
            except Exception:
                val = ""
        out.append(str(val).strip())
    return out[0], out[1]


def _share_window_alive(dlg):
    """小窗是否仍可复用：已超时（`_timed_out`）或已关闭（isVisible 为假）即失效。

    120s 到点只关闭、**不回调**，所以 `_share_ask_dlg` 引用可能仍指向已死窗口——
    必须显式判活，否则会把超时窗当「同一个」复用，导致作废的码仍被读取。"""
    if dlg is None:
        return False
    try:
        if getattr(dlg, "_timed_out", False):
            return False
    except Exception:
        pass
    vis = getattr(dlg, "isVisible", None)
    if callable(vis):
        try:
            return bool(vis())
        except Exception:
            return True
    return True


def _share_code_in_window(win, surl, share_uk):
    """小窗里是否有一个「属于同一分享且通过校验」的码。

    返回 (code, dlg)：code 为空串表示不可用。`current_code()`（冻结访问器）是
    最高优先级取值来源；未落地时视同无码。归属判定：同 surl 或同 share_uk；
    超时/已关闭的旧窗一律视为无码。"""
    dlg = getattr(win, "_share_ask_dlg", None)
    if dlg is None or not _share_window_alive(dlg):
        return "", dlg
    code = ""
    fn = getattr(dlg, "current_code", None)
    if callable(fn):
        try:
            code = str(fn() or "").strip()
        except Exception:
            code = ""
    if not code:
        return "", dlg
    t_surl, t_uk = _share_dlg_target(dlg)
    surl = str(surl or "").strip()
    share_uk = str(share_uk or "").strip()
    same = bool((t_surl and surl and t_surl == surl)
                or (share_uk and t_uk and share_uk == t_uk))
    if not same:
        return "", dlg
    return code, dlg


def _close_share_ask_dlg(win, surl=None, uk=None, url=None):
    """成功提取后关闭「属于同一分享」的缺提取码小窗（只关闭、绝不回调）。

    小窗的唯一职责是收集提取码；一旦该分享被成功拉起（客户端已确认）或挑选提交
    成功，它的任务即告完成，必须立刻消失，绝不赖到 120s 超时。

    归属判定（复用 `_share_dlg_target`）：小窗 target_surl 等于 surl、或 target_uk
    等于 uk、或小窗记录的原链接等于 url——三者任一命中才算「同一分享」，绝不动别
    的分享的窗。关闭前把 `on_decision` 置 None：closeEvent 会走 `_finish("ignore")`，
    摘掉回调保证成功路径绝不产生第二次 decision（`_finish` 只回调一次）。同时显式
    停掉倒计时，并清空 `win._share_ask_dlg`。最后记一行**不含明文提取码**的日志。

    Qt 主线程调用；返回是否真的关掉了某扇窗。绝不抛异常。"""
    dlg = getattr(win, "_share_ask_dlg", None)
    if dlg is None:
        return False
    t_surl, t_uk = _share_dlg_target(dlg)
    surl_s = str(surl or "").strip()
    uk_s = str(uk or "").strip()
    url_s = str(url or "").strip()
    same = bool((surl_s and t_surl and surl_s == t_surl)
                or (uk_s and t_uk and uk_s == t_uk))
    if not same and url_s:
        try:
            same = bool(str(getattr(dlg, "url", "") or "").strip() == url_s)
        except Exception:
            same = False
    if not same:
        return False
    try:
        dlg.on_decision = None
    except Exception:
        pass
    try:
        timer = getattr(dlg, "_timer", None)
        if timer is not None:
            timer.stop()
    except Exception:
        pass
    try:
        dlg.close()
    except Exception:
        pass
    try:
        win._share_ask_dlg = None
    except Exception:
        pass
    _share_log(win, "[分享] 已成功提取，提取码小窗已关闭（填写任务完成）")
    return True


def _notify_share_used(win, surl=None, uk=None, url=None):
    """线程安全：把「该分享已成功拉起」回执投回 Qt 线程，由 `_drain` 关闭同分享小窗。

    worker 线程（`_invoke_share_worker` / `_pick_share_worker`）绝不能碰控件，只能
    经 hub.q 转交；`_drain` 收到 `share_used` 后在 Qt 线程调用 `_close_share_ask_dlg`。
    异常一律吞掉，绝不影响拉起本身。"""
    try:
        win.hub.q.put({"type": "share_used", "surl": surl, "uk": uk, "url": url})
    except Exception:
        pass


def _effective_share_code(win, surl, share_uk, rec_pwd, code_source):
    """唯一的分享提取码取值助手（优先级冻结），返回 `(code, source)`。

    1. 小窗里填的码（且该窗属于同一 surl/share_uk、`current_code()` 非空）
       → `("window")`（最高优先，用户手填即意图）
    2. 记录自带码且来源权威（`code_source` 属 `url`/`text`/`window`/空）
       → `(rec_pwd, code_source)`。`window` = 用户在小窗里亲手填过并写回记录的码，
       与 `url`/`text` 同为权威（「以小窗填写的为准」）；`code_source` 缺失按权威
       空来源处理，兼容旧记录。
    3. 记录码为空、或 `code_source == "recent"`（抓取时的猜测）
       → 用 `fresh_code_from_history`（严格 120s，取最新）**重新取值**，
          并用 `since_ts` 收紧到「晚于上一条分享记录」的码（原子配对，见 D）：
          取到 → `("recent")`；取不到且存在更早的分享记录 → `("", "")`
          （绝不把属于别的分享的旧猜测码发去 verify）
    4. 全无 → `("", "")`

    绝不抛异常。"""
    try:
        code, _dlg = _share_code_in_window(win, surl, share_uk)
        if code:
            return (code, "window")
    except Exception:
        pass
    try:
        pwd = str(rec_pwd or "").strip()
    except Exception:
        pwd = ""
    src = "" if code_source is None else str(code_source)
    # `window`：用户在小窗亲手填的码（写回记录时的来源标记），与 url/text 同为
    # 权威来源——小窗关闭后也不该被更新的剪贴板候选覆盖。
    if pwd and src in ("url", "text", "window", ""):
        return (pwd, src)
    # 记录码为空或来源是猜测 → 重新取 120s 内最新的码，并收紧到「晚于上一条
    # 分享记录」：早于它的码属于更早的分享，套到当前链接上就是 errno=-9。
    fresh = None
    since_ts = 0.0
    try:
        since_ts = bm.latest_share_ts(exclude_surl=surl)
    except Exception:
        since_ts = 0.0
    try:
        entries = win.state.temp_password_entries()
        try:
            fresh, _ = bm.fresh_code_from_history(entries, since_ts=since_ts)
        except TypeError:
            # 兼容只接受旧签名 (entries[, ttl, now]) 的桩：退回旧调用，
            # since_ts 是可选增强，语义不因替身缺参而中断。
            fresh, _ = bm.fresh_code_from_history(entries)
    except Exception:
        fresh = None
    if fresh:
        return (str(fresh).strip(), "recent")
    # 有更早的分享记录（存在绑定上下文）时，绝不再回退「来源=recent」的猜测码：
    # 旧猜测很可能属于别的分享；返回空让拉起路径诚实放弃，绝不误发 verify。
    if pwd and not since_ts:
        return (pwd, src)
    return ("", "")


def _clipboard_share_target(win):
    """按压时刻就地读剪贴板：是 pan.baidu 分享链接则返回 `(url, surl, pwd)`，否则 None。

    A 快路径（零等待）：只用剪贴板**当前这一段文本**，URL 边界/域名判断全部复用
    `utils.split_urls` + `utils.is_baidu_pan_url` + `bm.parse_share_url`，不新增
    正则；提取码也优先取自同一段文本（URL 的 `?pwd=` 优先，再复用 QRMonitor 的
    `_extract_pwd_code` 关键字解析）。命中后**绝不**把这段文本再交给 QRMonitor
    管线（避免重复抓页/记录/双拉起）。
    无 QApplication / 剪贴板不可用 / 非分享链接一律返回 None。绝不抛异常。
    """
    try:
        app = QApplication.instance()
        if app is None:
            return None
        cb = app.clipboard()
        text = str(cb.text() or "") if cb is not None else ""
        if not text.strip():
            return None
        for _s, _e, url in split_urls(text):
            if not is_baidu_pan_url(url):
                continue
            p = bm.parse_share_url(url)
            if not p:
                continue
            pwd = str(p.get("pwd") or "").strip()
            if not pwd:
                try:
                    from ..monitors import QRMonitor as _QRM
                    pwd = str(_QRM._extract_pwd_code(text) or "").strip()
                except Exception:
                    pwd = ""
            return (str(p.get("url") or url), str(p.get("surl") or ""), pwd)
        return None
    except Exception:
        return None


def _share_input_inflight(win):
    """当前是否有分享输入（文本/图片/放行抓取）仍在 QRMonitor 管线里处理。

    经 hub 的在途计数判断（覆盖「入队→抓页→解码→记录写入」整条链，见
    hub.share_input_pending）；hub 桩没有该能力时一律按 False。绝不抛异常。"""
    try:
        fn = getattr(getattr(win, "hub", None), "share_input_pending", None)
        if callable(fn):
            return bool(fn())
    except Exception:
        pass
    return False


def _share_gesture_wait_sec(win):
    """手势意图等待秒数：读 config 的 share_gesture_wait_sec（clamp 5..600）。

    配置缺失/损坏一律回退 PENDING_SHARE_TTL_SEC（同默认 60）。绝不抛异常。"""
    try:
        v = int(win.state.snapshot().get(
            "share_gesture_wait_sec", PENDING_SHARE_TTL_SEC))
    except Exception:
        v = PENDING_SHARE_TTL_SEC
    return max(5, min(600, v))


def _share_uk_for_surl(surl):
    """按 surl 从已记录的分享里取 share_uk（没有/异常返回 ""）。绝不抛异常。"""
    try:
        rec = (bm._TRACK.get("shares") or {}).get(str(surl or "")) or {}
        return str(rec.get("share_uk") or "")
    except Exception:
        return ""


def _mark_gesture_launch(win, surl):
    """登记「该 surl 刚由手势拉起」的时间戳（静默去重窗口用）。绝不抛异常。"""
    try:
        key = str(surl or "").strip()
        if not key:
            return
        d = getattr(win, "_share_gesture_launch_ts", None)
        if not isinstance(d, dict):
            d = {}
            win._share_gesture_launch_ts = d
        d[key] = time.time()
    except Exception:
        pass


def _gesture_launched_recently(win, surl, window=None):
    """该 surl 是否在去重窗口内刚被手势（含预定派发）拉起过。

    用于 share_link 自动分支：同一次用户意图已经拉起该链接，管线再报到同链接时
    不再重复拉起、也不弹「重复分享」确认。窗口缺省 SHARE_GESTURE_DEDUP_SEC。
    桩对象没有该状态时一律 False。绝不抛异常。"""
    try:
        key = str(surl or "").strip()
        if not key:
            return False
        d = getattr(win, "_share_gesture_launch_ts", None)
        if not isinstance(d, dict):
            return False
        ts = float(d.get(key) or 0)
        win_sec = SHARE_GESTURE_DEDUP_SEC if window is None else float(window)
        return ts > 0 and (time.time() - ts) <= win_sec
    except Exception:
        return False


def _share_invoke_busy_stale(win):
    """忙标志是否已超过 SHARE_INVOKE_BUSY_MAX_SEC（worker 卡死）。纯读，绝不抛异常。"""
    try:
        if not getattr(win, "_share_invoke_busy", False):
            return False
        started = float(getattr(win, "_share_invoke_started_ts", 0) or 0)
        return bool(started) and (time.time() - started) > SHARE_INVOKE_BUSY_MAX_SEC
    except Exception:
        return False


def _call_start_share_pick(win, url, surl, pwd, manual=False, item=None,
                           bind_code_uk=None):
    """调用 `win._start_share_pick` 并转发 `bind_code_uk`；兼容不接受该参数的旧桩。

    真实实现接受 `bind_code_uk`；既有大量测试把 `_start_share_pick` 换成不含该形参
    的桩。这里按签名判断：不接受时退回位置调用（绑定语义对桩不可观测，本就不会执行
    worker），绝不因签名差异让调用抛错。module 级定义使旧桩（未绑定本方法）可用。"""
    fn = win._start_share_pick
    accepts = True
    try:
        import inspect
        params = inspect.signature(fn).parameters
        accepts = ("bind_code_uk" in params) or any(
            p.kind == inspect.Parameter.VAR_KEYWORD for p in params.values())
    except Exception:
        accepts = True
    if accepts:
        return fn(url, surl, pwd, manual=manual, item=item,
                  bind_code_uk=bind_code_uk)
    return fn(url, surl, pwd, manual=manual, item=item)


def _prefill_share_code_window(dlg, code):
    """复用小窗时预填「最新已知码」：只在框内当前码为空时填入，绝不覆盖用户输入。

    优先冻结的可写方法（set_code / prefill_code / prefill，若将来落地）；
    否则回退已知输入框属性（once_edit / code_edit / mapped_edit）。"""
    code = str(code or "").strip()
    if not code or dlg is None:
        return
    try:
        fn = getattr(dlg, "current_code", None)
        if callable(fn) and str(fn() or "").strip():
            return                      # 用户已填：不覆盖
    except Exception:
        pass
    for name in ("set_code", "prefill_code", "prefill"):
        fn = getattr(dlg, name, None)
        if callable(fn):
            try:
                fn(code)
                return
            except Exception:
                pass
    for attr in ("once_edit", "code_edit", "mapped_edit"):
        ed = getattr(dlg, attr, None)
        if ed is not None and hasattr(ed, "setText"):
            try:
                if not str(ed.text() or "").strip():
                    ed.setText(code)
                    return
            except Exception:
                pass


def _share_parent_usable(win):
    """取码小窗的主窗可用性：QWidget 且可见、未最小化才算可用。

    非 QWidget（既有测试桩）无法判定，按可用处理以保持旧桩行为。"""
    if win is None:
        return False
    if not isinstance(win, QWidget):
        return True
    try:
        if not win.isVisible():
            return False
        if win.isMinimized():
            return False
    except Exception:
        return False
    return True


def _share_notify_via(win, title, msg):
    """托盘气泡（module 级）：优先宿主既有 `_share_notify`，否则退回 `hub.notify`。"""
    fn = getattr(win, "_share_notify", None)
    if callable(fn):
        try:
            fn(title, msg)
            return
        except Exception:
            pass
    try:
        win.hub.notify(title, msg)
    except Exception:
        pass


# 「缺码且小窗弹不出来」的提示去重标记：调用方（Alt+2/Alt+3/询问路径）先发声并置位，
# _show_share_code_window 的兜底提示看到标记即静默——一次用户动作绝不双响。
_SHARE_ASK_NOTIFIED = "_share_ask_notified"


def _take_share_ask_notified(win):
    """取走去重标记：True = 本次缺码动作已由调用方发过提示，兜底提示应静默。"""
    try:
        if getattr(win, _SHARE_ASK_NOTIFIED, False):
            setattr(win, _SHARE_ASK_NOTIFIED, False)
            return True
    except Exception:
        pass
    return False


def _announce_ask_code_hidden(win, url):
    """主窗不可用（隐藏到托盘/最小化）时的缺码提示：唯一一条日志 + 唯一一条气泡。

    文案只承诺「打开主界面后可弹出小窗」，**绝不**承诺当前弹不出来的窗口（旧文案
    「请在右侧小窗填写」「已在提取码小窗等待填写」都会误导用户）。发声后置位去重
    标记，随后 `_show_share_code_window` 的兜底提示会静默，不再重复提醒。"""
    _share_log(win, "[分享] 该分享缺少提取码，主界面未显示，已跳过拉起"
                    "（打开主界面后可填写，或稍后按 Alt+2/Alt+3）")
    _share_notify_via(
        win, "分享缺少提取码",
        "该分享缺少提取码。三种方式：打开主界面即会弹出小窗填写；"
        "或按 Alt+2 用临时码；或把「链接 + 提取码」整段复制到剪贴板。\n"
        + str(url or ""))
    try:
        setattr(win, _SHARE_ASK_NOTIFIED, True)
    except Exception:
        pass


def _share_pan_open_blocked(win, url):
    """实验性模式下封禁「显式用浏览器打开 pan.baidu」：True=已拦截并记一行。

    与信任流程无关（信任只管询问/放行）；这是总开关授予的第二条：静默通道
    换显式打开封禁。配置/状态读不到或异常时一律按「不拦截」处理。"""
    try:
        if not is_baidu_pan_url(url):
            return False
        if not win.state.snapshot().get("experimental_enabled"):
            return False
    except Exception:
        return False
    _share_log(win, "已开启实验性：pan.baidu 网址改走静默通道，不在浏览器打开")
    return True


def _show_share_code_window(win, surl, url, share_uk, mapped_code="",
                            open_browser=False):
    """把「缺提取码小窗」收敛到唯一入口（Qt 主线程调用）。

    - 已有小窗且属于同一分享 → 复用并预填最新已知码（不覆盖用户已填内容）；
    - 已有小窗属于别的分享 → 关掉旧的换新的（同一时刻只允许一个小窗）；
    - 主窗缺失/隐藏（托盘）/最小化时**不再悬浮独立小窗**：只记一行日志 +
      一条托盘气泡（hub.notify 路径），返回 None，由调用方既有「无码回退」继续；
    - `open_browser`：仅在新开/复用小窗之外需要「未知分享者探针」时置真；
      已在弹小窗时不重复开浏览器；实验性开启时 pan.baidu 网址绝不显式打开。

    任何异常都不向外抛，返回小窗对象或 None。"""
    # UX-5：小窗是挂在主窗上的子工具窗——主窗不可见时不打扰用户（托盘气泡 + 日志）。
    # 去重：调用方（Alt+2/Alt+3/询问路径）若已就本次缺码动作发过提示，这里不再重复
    # 提醒——一次用户动作只有一条气泡，且文案由最先发声的一侧给出（绝不承诺弹不出的窗）。
    if not _share_parent_usable(win):
        if not _take_share_ask_notified(win):
            _share_log(win, "[分享] 主窗口未显示，提取码小窗不弹出（可稍后按 Alt+2/Alt+3）")
            _share_notify_via(
                win, "分享缺少提取码",
                "该分享缺少提取码。三种方式：打开主界面即会弹出小窗填写；"
                "或按 Alt+2 用临时码；或把「链接 + 提取码」整段复制到剪贴板。\n"
                + str(url or ""))
        return None
    dlg = getattr(win, "_share_ask_dlg", None)
    if dlg is not None and not _share_window_alive(dlg):
        # 已超时/关闭的旧窗：丢弃引用，按「不同分享」重建一个新窗。
        try:
            dlg.on_decision = None
        except Exception:
            pass
        try:
            dlg.close()
        except Exception:
            pass
        try:
            win._share_ask_dlg = None
        except Exception:
            pass
        dlg = None
    t_surl, t_uk = _share_dlg_target(dlg)
    surl_s = str(surl or "").strip()
    uk_s = str(share_uk or "").strip()
    same = bool(dlg is not None and (
        (t_surl and surl_s and t_surl == surl_s)
        or (uk_s and t_uk and uk_s == t_uk)))
    if same:
        _prefill_share_code_window(dlg, mapped_code)
        try:
            dlg.show()
        except Exception:
            pass
        return dlg
    if dlg is not None:
        # 不同分享：关旧换新。先摘掉旧窗回调，避免其关闭时的 ignore 回写
        # 把刚建好的新窗引用清掉（`_on_share_code_decision` 会把引用置 None）。
        try:
            dlg.on_decision = None
        except Exception:
            pass
        try:
            dlg.close()
        except Exception:
            pass
        try:
            win._share_ask_dlg = None
        except Exception:
            pass
    try:
        from .dialogs import ShareCodeAskDialog
    except Exception:
        _share_log(win, "[分享] 缺少提取码询问组件，已跳过")
        return None
    try:
        # 传 hub= 让 120s 超时日志（「分享询问超时(120s)，已关闭丢弃」）真正出现。
        new_dlg = ShareCodeAskDialog(parent=win, surl=surl, url=url,
                                     share_uk=share_uk, mapped_code=mapped_code,
                                     hub=getattr(win, "hub", None))
    except Exception as e:
        _share_log(win, f"[分享] 打开提取码询问失败: {e}")
        return None
    try:
        new_dlg.on_decision = (
            lambda kind, code: win._on_share_code_decision(
                kind, code, url, surl, share_uk))
    except Exception:
        pass
    try:
        win._share_ask_dlg = new_dlg
    except Exception:
        pass
    try:
        new_dlg.show()
    except Exception:
        try:
            win._share_ask_dlg = None
        except Exception:
            pass
        return None
    if open_browser:
        # 未知分享者：沿用既有「浏览器打开分享页做探针」行为（只在新弹小窗时一次）。
        # UX-4：实验性开启时 pan.baidu 一律静默，绝不显式打开浏览器。
        if not _share_pan_open_blocked(win, url):
            try:
                import webbrowser
                webbrowser.open(str(url), new=2)
                _share_log(win, f"[分享] 未知分享者，已在浏览器打开分享页: {url}")
            except Exception as e:
                _share_log(win, f"[分享] 打开分享页失败: {e}")
    return new_dlg


class MainWindow(QMainWindow):
    def __init__(self, state, hub, show_event=None, pauser=None):
        super().__init__()
        self.state = state
        self.hub = hub
        self.show_event = show_event
        self.pauser = pauser
        self.setWindowTitle("AutoUnpacker")
        # M3 工作台布局（标签页 + 胶囊条 + 248px 右栏）：默认宽高对齐原型 11。
        # DPI/分辨率自适应：初始尺寸按屏幕可用区（逻辑像素）收敛，见 fit_window_size。
        _aw, _ah = self._available_geometry()
        self.resize(*fit_window_size(1180, 760, _aw, _ah))
        self.setWindowIcon(make_tray_icon())
        self.setAcceptDrops(True)   # 支持拖入文件临时解压
        self._build_ui()
        self._drain_timer = QTimer(self)
        self._drain_timer.timeout.connect(self._drain)
        self._drain_timer.start(200)
        # 任务生命周期刷新（防抖）：后台每次写 tasks 表投一条 {"type":"task"}，这里用
        # 单次定时器合并——首个事件后 250ms 内到达的事件不再各自触发，整表重建被
        # 限制在 ≤4 次/秒（解压时哪怕日志刷屏也不会引发刷新风暴）。
        self._tasks_refresh_timer = QTimer(self)
        self._tasks_refresh_timer.setSingleShot(True)
        self._tasks_refresh_timer.setInterval(250)
        self._tasks_refresh_timer.timeout.connect(self._on_tasks_refresh_timer)
        self.rebuild_cards()
        self._refresh_all()
        # 先按当前（已被屏幕收敛的）宽度做紧凑化，再算最小尺寸：装饰收起后
        # minimumSizeHint 才是放大 200% 时真实需要的下限。
        self._apply_chrome_compact()
        # 最小尺寸必须在 show 之前收敛：布局最小尺寸被长内容顶大时，
        # 超过屏幕的最小尺寸会让窗口在显示时被撑到屏外（见 fit_min_size）。
        self._apply_screen_limits()
        self._setup_tray()
        if self.show_event is not None:
            self._show_check = QTimer(self)
            self._show_check.timeout.connect(self._check_show_request)
            self._show_check.start(400)
        app = QApplication.instance()
        if app is not None:
            self._hotkey_filter = _HotkeyFilter(
                self._show_window, self._on_system_theme_changed,
                self._on_share_hotkey,
                on_hotkey_share_code=self._on_share_code_hotkey)
            app.installNativeEventFilter(self._hotkey_filter)
        # 全局快捷键：**等窗口显示后再注册**。在 __init__ 里立刻注册时，winId()
        # 拿到的原生窗口句柄可能尚未“坐实”，偶发 RegisterHotKey 失败(1400 无效句柄)；
        # 延后注册 + 失败重试可彻底消除这个启动偶发。
        # 全局热键的**真实注册结果**（None=未尝试/不适用，True=已注册，False=失败）。
        # 底栏据此诚实标注「（未生效）」，不再拿配置值冒充已生效（见 _sync_hotkey_display）。
        self._hotkey_ok = {"main": None, "share": None, "share_code": None}
        QTimer.singleShot(600, self._register_hotkey)
        # 主界面快捷键：Esc / Ctrl+W 触发关闭（走 close_action 逻辑：
        # 询问弹窗 / 隐藏到托盘 / 关闭程序）。仅主界面激活时生效，
        # 模态对话框（设置/密码本等）打开时不干扰。
        self._esc_sc = QShortcut(QKeySequence("Esc"), self)
        self._esc_sc.activated.connect(self.close)
        self._cw_sc = QShortcut(QKeySequence("Ctrl+W"), self)
        self._cw_sc.activated.connect(self.close)
        # 标签页切换：浏览器惯例 Ctrl+PgDn=下一个 / Ctrl+PgUp=上一个（循环）。
        # 与 Esc/Ctrl+W 同为窗口级 QShortcut：仅主界面激活时生效。
        self._next_tab_sc = QShortcut(QKeySequence("Ctrl+PgDown"), self)
        self._next_tab_sc.activated.connect(lambda: self._cycle_page(1))
        self._prev_tab_sc = QShortcut(QKeySequence("Ctrl+PgUp"), self)
        self._prev_tab_sc.activated.connect(lambda: self._cycle_page(-1))
        # 网址信任：挂起的询问请求 + 当前打开的确认弹窗（防叠加）
        self._pending_trust = []
        self._trust_dlg = None
        # 分享「拉起」：同一时刻只允许一个后台拉起任务（防重复），
        # 自动/手动两条路径共用该忙标志
        self._share_invoke_busy = False
        # 忙标志起始时刻：worker 卡死超过 SHARE_INVOKE_BUSY_MAX_SEC 时强制复位
        self._share_invoke_started_ts = 0.0
        # 无登录态实验链路：本进程首次真正自动拉起时提醒一次（手动路径绝不提醒）
        self._share_nologin_warned = False
        # d7：同一分享本次运行重复拉起需征得用户同意。进程内兜底集合：
        # baidu_manifest 计数器不可用（或测试桩）时保证「只在首见自动拉起」。
        self._share_launched_surls = set()
        # 手势拉起时间戳（surl -> ts）：同一链接随后经解析管线自动分支再次出现时
        # 静默去重，避免「一次手势 → 两次拉起」（见 _gesture_launched_recently）。
        self._share_gesture_launch_ts = {}
        # 二维码解码期「预定任务」：手势在「无分享记录 + 正在解码二维码」时落空，
        # 登记一次性补按（{kind, at}），解码出百度分享链接（share_link 事件）后自动
        # 执行；超过 PENDING_SHARE_TTL_SEC 作废。Alt+3 本次不接线，kind 备用。
        self._pending_share_gesture = None
        # d3：当前打开的「提取码询问」/「文件挑选」面板（防叠加）。后台 worker 只
        # 通过 hub 队列请求，控件一律在 Qt 线程构造。
        self._share_ask_dlg = None
        self._share_pick_dlg = None
        # 拖放在途文件去重：同一文件的真实身份（绝对路径 + 大小 + mtime）为键，
        # 防止同一文件被拖入两次（或 重试 连点）而并发解压进同一输出目录、互相
        # 抢占任务行终态。键在起线程前置位、worker 的 finally 里必定移除（异常也不漏）。
        self._drop_inflight = {}
        self._drop_inflight_lock = threading.Lock()

    def _build_ui(self):
        """M3 工作台：NavTabs + DirChipStrip + 页面栈（任务/日志/密码本/回溯/设置）+ StatusBar。

        组件树见 FINAL-SPEC §1；旧的 WatchCard 滚动卡列表不再常驻主页面（只在目录弹窗用）。
        """
        # ---- 工作台运行期状态（M3）----
        self._failed_filter = False       # 失败过滤唯一状态（任务页分段 + 底栏按钮共用）
        self._dir_states = {}             # 规范化目录 -> (state, progress, name)
        self._strip_sig = None            # 目录签名：变化才重建筛选 chip（不丢用户选择）
        self._watchdir_dlg = None         # 当前打开的目录设置弹窗（移除目录后关闭它）
        # M3-QA：日志页/右栏缓存。滤镜集合与数据都没变时切页不再重查重画；
        # 新日志、滤镜变化、清空、显式刷新都会置脏（见 _append_log/_mark_log_dirty）。
        self._log_cache_sig = None
        self._log_cache_dirty = True
        self._meta_cache_dirty = True

        central = QWidget()
        self.setCentralWidget(central)
        root = QVBoxLayout(central)
        root.setContentsMargins(0, 0, 0, 0)
        root.setSpacing(0)

        # 顶部：标签页 + 暂停按钮（原型 11 的 tabbar actions）
        top = QWidget(central)
        top.setObjectName("navTabs")      # 借用 QSS 的 navTabs 底边线（与内层同一条线）
        top.setAttribute(Qt.WA_StyledBackground, True)
        top_lay = QHBoxLayout(top)
        top_lay.setContentsMargins(0, 0, 12, 0)
        top_lay.setSpacing(8)
        self.nav_tabs = NavTabs(top)
        self.nav_tabs.set_tabs([("tasks", "任务"), ("log", "运行日志"),
                                ("password", "密码本"), ("trail", "删除回溯"),
                                ("settings", "设置")])
        self.nav_tabs.currentChanged.connect(self._on_nav_changed)
        top_lay.addWidget(self.nav_tabs, 1)
        self.pause_btn = QPushButton("暂停", top)
        self.pause_btn.setObjectName("pause")
        self.pause_btn.setFixedWidth(52)
        self.pause_btn.setCursor(Qt.PointingHandCursor)
        self.pause_btn.clicked.connect(self._toggle_pause)
        top_lay.addWidget(self.pause_btn, 0, Qt.AlignVCenter)
        root.addWidget(top)

        # 目录胶囊条（三个页面常显）
        self.chip_strip = DirChipStrip(central)
        self.chip_strip.dirActivated.connect(self._on_chip_dir_activated)
        self.chip_strip.addRequested.connect(self._add_path)
        self.chip_strip.netdiskRequested.connect(self._add_baidu_download_dir)
        self.add_btn = self.chip_strip.add_btn     # 兼容旧属性（见 _update_rainbow）
        root.addWidget(self.chip_strip)

        # 页面栈
        self.pages = QStackedWidget(central)

        # 该任务日志视图：沿用既有 _append_log 管线（着色 / 可点链接 / 静默复制）
        self.log_box = QPlainTextEdit()
        self.log_box.setMaximumBlockCount(3000)
        self.log_box.setReadOnly(True)
        self.log_box.setMinimumHeight(60)
        self.log_box.viewport().installEventFilter(self)
        self.log_box.viewport().setMouseTracking(True)

        self.task_page = TaskPage(self.log_box, self.pages)
        self.task_page.taskActivated.connect(self._on_task_activated)
        self.task_page.taskDeselected.connect(lambda: self._select_task(None))
        self.task_page.actionTriggered.connect(self._on_task_action)
        self.task_page.copyRequested.connect(self._copy_task_log)
        self.task_page.scopeChanged.connect(lambda _s: self._refresh_tasks())
        self.task_page.resultFilterChanged.connect(self._set_failed_filter)
        self.task_page.logScopeChanged.connect(lambda _s: self._refill_task_log())
        self.task_page.refreshRequested.connect(self._refresh_all)
        self.pages.addWidget(_make_scrollable_page(self.task_page))

        self.log_page = LogPage(self._append_log_to, self.pages)
        self.log_page.filtersChanged.connect(self._reload_log_page)
        self.log_page.taskActivated.connect(self._on_need_task_activated)
        self.log_page.actionTriggered.connect(self._on_needs_action)
        self.log_page.notice.connect(self._append_log)
        # 「清空」是显式动作：下一次切回日志页时重查一次（保持既有行为）
        try:
            self.log_page.clear_btn.clicked.connect(self._mark_log_dirty)
        except Exception:
            pass
        # 运行日志页也接入同一套悬停/点击管线（渲染器已共用，事件过滤在这里补齐）
        try:
            self.log_page.log_view.viewport().installEventFilter(self)
            self.log_page.log_view.viewport().setMouseTracking(True)
        except Exception:
            pass
        self.pages.addWidget(_make_scrollable_page(self.log_page))

        # M4：三个正式页常驻标签壳（构造一次复用）；过渡占位页与「打开旧弹窗」按钮已移除。
        # notice 一律走日志页同款既有通道（_append_log），不新造提示机制。
        self.password_page = PasswordBookPage(self.state, self.pages)
        self.password_page.notice.connect(self._append_log)
        self.password_page.changed.connect(self._refresh_badges)
        self.trail_page = TrailPage(self.pages)
        self.trail_page.notice.connect(self._append_log)
        self.settings_page = SettingsPage(
            self.state, self.hub, self.pages,
            on_hotkey_change=self._register_hotkey,
            on_theme_change=self.on_theme_changed)
        self.settings_page.notice.connect(self._append_log)
        self.settings_page.watchPathsChanged.connect(self.rebuild_cards)
        self.settings_page.hotkeyChanged.connect(
            lambda _hk: self._sync_hotkey_display())
        self.settings_page.settingsSaved.connect(self._on_settings_saved)
        for page in (self.password_page, self.trail_page, self.settings_page):
            self.pages.addWidget(_make_scrollable_page(page))
        root.addWidget(self.pages, 1)

        # 底栏：左状态区 + 右纵向播报
        self.statusbar = StatusBar(central)
        self.statusbar.failRequested.connect(self._on_fail_requested)
        root.addWidget(self.statusbar)

        # 进度一律走 StatusBar（细进度 + 百分比）与目录胶囊，不再保留无父级的
        # 兼容进度条：父级为空的 QWidget 一旦 show() 就会变成独立浮窗（旧 _drain
        # 分支曾如此，用户可见）。此处已彻底移除，杜绝浮窗复现。

        # 初始显示（设置弹窗可能改配置，打开时再同步）
        try:
            self.statusbar.set_poll_interval(
                self.state.snapshot().get("poll_interval", 2))
        except Exception:
            pass
        self._sync_hotkey_display()
        try:
            if not bool(self.state.snapshot().get("show_status_tips", True)):
                self.statusbar.ticker.pause()
                self.statusbar.ticker.hide()
        except Exception:
            pass

    # ---------- 拖放临时解压 ----------
    def dragEnterEvent(self, e):
        """只接受拖入的文件（含多个），目录或链接不接受。"""
        if e.mimeData().hasUrls():
            urls = e.mimeData().urls()
            if urls and any(u.isLocalFile() for u in urls):
                e.acceptProposedAction()
                return
        e.ignore()

    def dropEvent(self, e):
        """拖入一个或多个文件：每个文件在后台线程做智能解压。

        - 只处理拖入的文件本身；同目录其他文件不处理（除非是它自己的分卷兄弟）
        - 输出到文件所在目录（default_output_dir 自动建同名目录）
        - 分卷：拖入首卷（.001）正常处理；拖入非首卷（.002）提示跳过，
          等待首卷；伪装分卷名的完整包正常处理
        """
        paths = []
        for u in e.mimeData().urls():
            if u.isLocalFile():
                from pathlib import Path
                p = Path(u.toLocalFile())
                if p.is_file():
                    paths.append(p)
        if not paths:
            e.ignore()
            return
        e.acceptProposedAction()
        for p in paths:
            self._handle_drop_file(p)

    def _handle_drop_file(self, path):
        """后台线程处理单个拖入文件（不阻塞界面）。"""
        import os as _os
        from pathlib import Path
        path = Path(path)

        # 目录：不支持，跳过
        if path.is_dir():
            self.hub.log(f"拖放: 目录不可解压，跳过: {path.name}")
            return

        # 下载未完成：跳过
        if smart_extract.is_incomplete_download(path):
            self.hub.log(f"拖放: 文件未下载完成，暂不解压: {path.name}")
            return

        # 非首卷分卷：等待首卷，不单独解压
        if smart_extract.is_non_first_volume(path.name):
            self.hub.log(f"拖放: 这是非首卷分卷，请拖入首卷（如 .001）统一处理: {path.name}")
            return

        # 移动安装包等不自动解压
        if smart_extract.is_do_not_extract(path.name):
            self.hub.log(f"拖放: 移动安装包/交付物，保持原样: {path.name}")
            return

        # 二维码图片：拖入即识别。廉价前置过滤——先只认图片扩展名，再读前 32 字节
        # 魔数；这样「一眼就是图片」的文件绝不进入昂贵的多格式归档扫描。
        if path.suffix.lower().lstrip(".") in IMAGE_EXTS:
            header = b""
            try:
                with open(path, "rb") as _fh:
                    header = _fh.read(32)
            except OSError:
                header = b""
            # 伪装成图片扩展名的压缩包：回落既有归档路径，既有能力不破。
            if not any(header.startswith(m) for m in _QR_ARCHIVE_MAGICS):
                try:
                    _size = path.stat().st_size
                except OSError:
                    _size = 0
                if _size > _QR_DROP_MAX_BYTES:
                    self.hub.log(
                        f"拖放: 图片过大，未做二维码识别（上限 16MB）: {path.name}")
                    return
                qr_monitor = getattr(self.hub, "qr_monitor", None)
                try:
                    from ..monitors import QRMonitor
                    # 契约：QRMonitor.is_image_bytes 为公开静态方法；旧版本仅有
                    # 私有同名实现，取其回落以免拖入真实二维码图片时崩溃。
                    _img_check = (getattr(QRMonitor, "is_image_bytes", None)
                                  or getattr(QRMonitor, "_is_image_bytes", None))
                except Exception:
                    _img_check = None
                if callable(_img_check) and _img_check(header):
                    if qr_monitor is None:
                        self.hub.log(
                            f"拖放: 二维码监控不可用，跳过图片识别: {path.name}")
                        return
                    # PIL 读盘 + PNG 编码可能阻塞：放入短命守护线程，绝不卡 UI。
                    # 此路径绝不做解压、绝不建任务行（二维码解码不是解压任务，
                    # 与剪贴板二维码路径同一语义）。
                    def _feed_qr(p=path, m=qr_monitor):
                        try:
                            ok = m.feed_image_file(str(p), force=True)
                        except Exception as e:
                            self.hub.log(f"拖放: 二维码图片处理出错: {p.name}: {e}")
                            return
                        if ok:
                            self.hub.notify("识别二维码图片", f"拖入的图片: {p.name}")
                            self.hub.log(f"拖放: 已识别二维码图片: {p.name}")
                    threading.Thread(target=_feed_qr, daemon=True).start()
                    return

        # 非压缩包且不是分卷：跳过
        if not smart_extract.is_archive_file(path) and not smart_extract.is_volume_name(path.name):
            self.hub.log(f"拖放: 不是压缩包，跳过: {path.name}")
            return

        # 在途去重：同一文件（真实身份 = 绝对路径 + 大小 + mtime）正在解压时忽略
        # 重复拖入 / 连点重试，避免并发解压进同一输出目录、互相抢占任务行终态。
        # 键在起线程前置位，worker 的 finally 必定移除（异常路径也不漏）。
        try:
            _st = path.stat()
            inflight_key = f"{path.resolve()}|{_st.st_size}|{int(_st.st_mtime)}"
        except Exception:
            inflight_key = str(path)
        with self._drop_inflight_lock:
            if inflight_key in self._drop_inflight:
                self.hub.log(f"拖放: 该文件正在解压中，忽略重复拖入: {path.name}")
                return
            self._drop_inflight[inflight_key] = int(time.time())

        self.hub.notify("发现压缩包", f"拖放解压: {path.name}")

        # 与监听路径同一套任务行语义：拖入即建行（queued），让队列无需任何操作
        # 就能立刻看到它；source_dir 取文件所在目录（与 find_open_task 的重试复用键
        # 对齐），失败时 tid=0 也不影响解压本身。
        tid = 0
        try:
            tid = (db.find_open_task(str(path.parent), path.name)
                   or db.add_task(
                       file_name=path.name,
                       file_size=(path.stat().st_size if path.exists() else None),
                       source_dir=str(path.parent), output_dir=None,
                       mode="drop", state="queued"))
        except Exception:
            tid = 0
        if tid:
            self._emit_tasks_changed()

        def _run():
            # 线程级日志上下文：拖放任务也挂到「该任务日志」下（与监听路径同一套
            # set/clear 语义），否则解压全程日志无处归档、任务日志页空白。桩 hub
            # 没有此能力时静默跳过，既有测试不受影响。
            _set_ctx = getattr(self.hub, "set_log_context", None)
            _clr_ctx = getattr(self.hub, "clear_log_context", None)
            if callable(_set_ctx):
                try:
                    _set_ctx(source_dir=str(path.parent), task_id=tid)
                except Exception:
                    pass
            try:
                # 跨名分卷链配对（与监听路径同一套）：验证通过即把改名兄弟卷改成首卷系列名，
                # 让 7-Zip 能正确重拼；失败/无链则原样继续。
                if smart_extract.is_volume_name(path.name):
                    try:
                        from ..monitors import FolderWatcher as _FolderWatcher
                        _FolderWatcher(self.state, self.hub, self.pauser).pair_split_for_drop(path)
                    except Exception:
                        pass
                try:
                    engine = smart_extract.create_engine("auto")
                except BaseException as e:
                    try:
                        engine = smart_extract.create_engine("zip")
                    except BaseException:
                        self.hub.log(f"拖放: {path.name} 无法初始化解压引擎: {e}")
                        self.hub.notify("智能解压失败", f"{path.name}\n7-Zip 不可用")
                        if tid:
                            try:
                                db.update_task_state(
                                    tid, "failed", error="7-Zip 不可用",
                                    finished_at=int(time.time()))
                            except Exception:
                                pass
                            self._emit_tasks_changed()
                        return
                passwords = self.state.all_passwords()
                options = {
                    "enable_nested": True,
                    "max_depth": 10,
                    "max_size_ratio": 100.0,
                    "use_dict": False,
                    "default_password": None,
                    "mode": "direct",
                }
                args = types.SimpleNamespace(
                    move_to=None,
                    delete_source=False,   # 拖放不删除源文件
                    run_script=None, script_args=[],
                    promote_to=None,
                    promote_merge=bool(self.state.snapshot().get("promote_merge", True)),
                )
                self.hub.q.put({"type": "progress_start"})
                if tid:
                    try:
                        db.update_task_state(tid, "extracting",
                                             started_at=int(time.time()))
                    except Exception:
                        pass
                    self._emit_tasks_changed()
                try:
                    result = smart_extract.extract_one(
                        engine, str(path), None, passwords, options, args,
                        progress_cb=self._progress_cb, pauser=self.pauser)
                finally:
                    self.hub.q.put({"type": "progress_done"})
                if result and result["success"]:
                    msg = (f"拖放解压完成: {path.name} 穿透 "
                           f"{result['depth_reached']} 层，共 "
                           f"{len(result['extracted_files'])} 个文件")
                    self.hub.log(msg)
                    self.hub.notify("智能解压完成", msg)
                    if tid:
                        try:
                            db.update_task_state(
                                tid, "done", finished_at=int(time.time()),
                                output_dir=(result.get("promoted_dir") or None),
                                layer=result.get("depth_reached"))
                        except Exception:
                            pass
                        self._emit_tasks_changed()
                else:
                    err = (result or {}).get("error") or "未知错误"
                    self.hub.log(f"拖放解压失败: {path.name} ({err})")
                    self.hub.notify("智能解压失败", f"{path.name}\n{err}")
                    if tid:
                        try:
                            db.update_task_state(
                                tid, ("need_password" if "密码" in err else "failed"),
                                error=err, finished_at=int(time.time()))
                        except Exception:
                            pass
                        self._emit_tasks_changed()
            except Exception as ex:
                self.hub.log(f"拖放处理出错: {path.name}: {ex}")
                self.hub.notify("智能解压出错", f"{path.name}\n{ex}")
                if tid:
                    try:
                        db.update_task_state(
                            tid, ("need_password" if "密码" in str(ex) else "failed"),
                            error=str(ex), finished_at=int(time.time()))
                    except Exception:
                        pass
                    self._emit_tasks_changed()
            finally:
                # 终态处理完毕才清上下文（成功/失败/差错/早退全覆盖），绝不让陈旧
                # 上下文污染本线程后续日志；在途键同理必须移除，异常路径也不漏。
                if callable(_clr_ctx):
                    try:
                        _clr_ctx()
                    except Exception:
                        pass
                try:
                    with self._drop_inflight_lock:
                        self._drop_inflight.pop(inflight_key, None)
                except Exception:
                    pass

        threading.Thread(target=_run, daemon=True).start()

    def _progress_cb(self, ratio, layer, name):
        """解压引擎进度回调 → GUI 队列（_drain 更新进度条）。ratio=None=忙碌。"""
        try:
            self.hub.q.put({"type": "progress", "ratio": ratio,
                            "layer": layer, "name": name})
        except Exception:
            pass

    def _setup_tray(self):
        self.tray = QSystemTrayIcon(make_tray_icon(), self)
        self.tray.setToolTip("AutoUnpacker")
        menu = QMenu()
        show = menu.addAction("显示主界面")
        show.triggered.connect(self._show_window)
        hide = menu.addAction("隐藏到托盘")
        hide.triggered.connect(self._hide_window)
        # 2.F「用客户端下载最近分享」：整条链路属实验性功能，未开启时整项隐藏。
        self._open_share_action = menu.addAction("用客户端打开最近分享")
        self._open_share_action.triggered.connect(self._open_recent_share)
        # d3：用分享者的「固定提取码」下载最近分享（同为实验性，未开启时整项隐藏）。
        self._open_share_code_action = menu.addAction("用固定提取码下载最近分享")
        self._open_share_code_action.triggered.connect(self._open_recent_share_with_code)
        # 菜单每次展开前刷新一次可见性（配置可能刚被改过，无需额外的变更通知）
        menu.aboutToShow.connect(self._refresh_share_menu)
        menu.addSeparator()
        quit_ = menu.addAction("退出")
        quit_.triggered.connect(self._quit)
        self.tray.setContextMenu(menu)
        self.tray.activated.connect(self._on_tray_activated)
        self.tray.show()

    def _on_tray_activated(self, reason):
        if reason == QSystemTrayIcon.DoubleClick:
            self._toggle_window()

    def _toggle_window(self):
        if self.isVisible():
            self._hide_window()
        else:
            self._show_window()

    def _show_window(self):
        self.showNormal()
        self.raise_()
        self.activateWindow()
        self._process_pending_trust()

    def _hide_window(self):
        self.hide()

    def _quit(self):
        self.tray.hide()
        QApplication.instance().quit()

    def _check_show_request(self):
        if self.show_event is None:
            return
        try:
            import win32event
            if win32event.WaitForSingleObject(self.show_event, 0) == win32event.WAIT_OBJECT_0:
                win32event.ResetEvent(self.show_event)
                self._show_window()
                snap = self.state.snapshot()
                if (hasattr(self, "tray") and snap.get("notify_enabled", True)
                        and snap.get("notify_already_running", True)):
                    self.tray.showMessage(
                        "AutoUnpacker", "程序已在运行，已打开主界面。",
                        QSystemTrayIcon.Information, 2000)
        except Exception:
            pass

    def closeEvent(self, event):
        action = self.state.snapshot().get("close_action", "ask")
        if action == "tray":
            event.ignore()
            self._hide_window()
            self._notify_trayed()
            return
        if action == "exit":
            event.accept()
            self._quit()
            return
        # 每次询问：弹二选一（关闭程序 / 隐藏到托盘）+「不再提示」勾选
        action, remember = self._ask_close_action()
        if action is None:
            # 用户取消（按弹窗 X / Esc）：中止关闭，主界面保持原样
            event.ignore()
            return
        if remember:
            self.state.set("close_action", action)
        if action == "exit":
            event.accept()
            self._quit()
        else:
            event.ignore()
            self._hide_window()
            self._notify_trayed()

    def _ask_close_action(self):
        """关闭主界面时弹窗：二选一 +「不再提示」勾选（可取消）。

        返回 (action, remember)：
        - action: "exit" 关闭程序 / "tray" 隐藏到托盘（本次立即执行）；
                  None = 用户按标题栏 X / Esc 取消，调用方应中止关闭；
        - remember: 是否勾选「不再提示」（勾选则把 action 同步进设置，
          以后关闭默认照此执行；不勾选则本次执行后下次仍询问）。"""
        return CloseActionDialog.ask(self)

    def _notify_trayed(self):
        cfg = self.state.snapshot()
        if cfg.get("notify_enabled", True) and cfg.get("notify_trayed", True):
            self.tray.showMessage(
                "AutoUnpacker", "已最小化到托盘，右键托盘图标可退出。",
                QSystemTrayIcon.Information, 2500)

    # ---------- 控制 ----------
    def _open_settings(self):
        dlg = SettingsDialog(self.state, self.hub, self,
                             on_hotkey_change=self._register_hotkey,
                             on_theme_change=self.on_theme_changed)
        dlg.exec_()
        self._on_settings_saved()

    def _on_settings_saved(self):
        """设置保存后的主窗同步：轮询间隔 / 底栏热键文案（旧弹窗与设置页共用）。"""
        # 设置里可能改了轮询间隔 / 快捷键：回到主界面立刻同步到底栏显示
        try:
            self.statusbar.set_poll_interval(
                self.state.snapshot().get("poll_interval", 2))
        except Exception:
            pass
        self._sync_hotkey_display()

    def _add_path(self):
        """「添加目录」：新建空条目并直接打开目录设置弹窗；取消/关闭则回收空条目。"""
        cfg = self.state.snapshot()
        entry = {"path": "", "enabled": True, "output_dir": "",
                 "delete_source": False, "delete_policy": "auto", "mode": "surface"}
        cfg["watch_paths"].append(entry)
        self.state.set("watch_paths", cfg["watch_paths"])
        idx = len(cfg["watch_paths"]) - 1
        self.rebuild_cards()
        code = self._open_watchdir_dialog(idx)
        if code != QDialog.Accepted:
            # 取消/关闭：回收刚才的空条目（用户若在弹窗里已移除，条目已不存在）
            try:
                paths = self._watch_paths()
                if (0 <= idx < len(paths)
                        and not str(paths[idx].get("path") or "").strip()):
                    self._remove_path(idx)
            except Exception:
                pass

    def _add_baidu_download_dir(self):
        """从百度网盘本地任务库识别下载目录并加入监听（实验性功能）。"""
        if not self.state.snapshot().get("experimental_enabled", False):
            QMessageBox.information(
                self, "百度网盘下载目录",
                "该功能依赖实验性功能，请先在「设置 → 常规」开启「实验性功能」。")
            return
        root = None
        try:
            from ..baidu_task import detect_download_root
            root = detect_download_root(
                self.state.snapshot().get("baidu_task_db") or None)
        except Exception as e:
            self.hub.log(f"识别百度网盘下载目录失败: {e}")
        if root is None:
            QMessageBox.information(
                self, "百度网盘下载目录",
                "未能从百度网盘任务库识别到下载目录。\n"
                "（确认网盘客户端有下载历史，或该库路径未被改动）")
            return
        try:
            entries = list(self.state.snapshot().get("watch_paths") or [])
            # 实验性功能开启时统一走重叠检测：相等 / 祖先 / 子孙都拦截，避免
            # 同一批下载文件被两条监听路径重复处理（旧代码只挡精确重复）。
            conflict = watch_path_conflict(entries, str(root))
            if conflict is not None:
                QMessageBox.warning(
                    self, "百度网盘下载目录",
                    "该目录与已有监听路径重叠，可能重复处理同一批文件：\n"
                    f"{root}\n↔ {conflict.get('path')}")
                return
            entries.append({"path": str(root), "enabled": True,
                            "output_dir": "", "delete_source": False,
                            "delete_policy": "auto",
                            "mode": "baidu"})
            self.state.set("watch_paths", entries)
            self.rebuild_cards()
            self.hub.log(f"已把百度网盘下载目录加入监听: {root}")
            QMessageBox.information(self, "百度网盘下载目录",
                                    f"已添加监听路径：\n{root}")
        except Exception as e:
            self.hub.log(f"添加百度网盘下载目录失败: {e}")
            QMessageBox.warning(self, "百度网盘下载目录", f"添加失败：{e}")

    def _open_password_book(self):
        dlg = PasswordBookDialog(self.state, self)
        dlg.exec_()

    def _open_delete_trail(self):
        dlg = DeleteTrailDialog(self)
        dlg.exec_()

    def _remove_path(self, idx):
        cfg = self.state.snapshot()
        if 0 <= idx < len(cfg["watch_paths"]):
            cfg["watch_paths"].pop(idx)
        self.state.set("watch_paths", cfg["watch_paths"])
        self.rebuild_cards()

    # ---------- 目录胶囊条 / 任务页 / 日志页（M3 工作台） ----------
    def _watch_paths(self):
        """当前监听路径条目（永远返回 list[dict]，异常退化为空表）。"""
        try:
            paths = self.state.snapshot().get("watch_paths") or []
        except Exception:
            paths = []
        return [p for p in paths if isinstance(p, dict)]

    def _dir_entries(self):
        """监听路径 + 缓存的目录状态 -> DirChipStrip / 筛选 chip 用的 entries。"""
        entries = []
        for e in self._watch_paths():
            ent = dict(e)
            key = _norm_path_for_cfg(str(e.get("path") or ""))
            cached = self._dir_states.get(key)
            if cached is not None:
                ent["state"] = cached[0]
                if cached[1] is not None:
                    ent["progress"] = cached[1]
                if cached[2]:
                    ent["name"] = cached[2]
            elif not e.get("enabled", True):
                ent["state"] = "paused"
            else:
                ent["state"] = "listening"
            entries.append(ent)
        return entries

    def _update_rainbow(self):
        """引导态：没有任何监听路径条目时驱动「添加目录」按钮彩虹（§9 允许保留）。

        M3-QA 修复：胶囊条的添加按钮改为 RainbowLayoutButton（保留虚线 add 样式），
        这里继续走同一开关；有路径时 set_rainbow(False) 即停表，平时零后台开销。"""
        try:
            entries = [w for w in self.state.snapshot().get("watch_paths", [])
                       if isinstance(w, dict)]
            fn = getattr(self.add_btn, "set_rainbow", None)
            if callable(fn):
                fn(not entries)
        except Exception:
            pass

    def rebuild_cards(self):
        """兼容旧入口（_add_path/_remove_path/_add_baidu_download_dir/主题切换都会调）：

        M3 起不再创建 WatchCard 卡列表，改为刷新目录胶囊条与日志页筛选 chips；
        只有路径集合变化时才重建 chips（主题切换等场景不丢用户的选择）。"""
        entries = self._dir_entries()
        sig = tuple(str(e.get("path") or "") for e in entries)
        try:
            self.chip_strip.set_dirs(entries)
        except Exception:
            pass
        if sig != self._strip_sig:
            self._strip_sig = sig
            try:
                self.log_page.set_paths(entries)
            except Exception:
                pass
            self._reload_log_page(force=True)   # 路径集合变了：强制重查一次
        else:
            for e in entries:
                raw = str(e.get("path") or "")
                if raw:
                    try:
                        self.log_page.update_dir_state(raw, e.get("state"))
                    except Exception:
                        pass
        self._update_rainbow()

    # ---------- 工作台刷新 / 联动 ----------
    def _task_limit(self):
        """任务查询上限：config.task_history_limit（缺省 500，钳到 1..5000）。"""
        try:
            n = int(self.state.snapshot().get("task_history_limit", 500) or 500)
        except Exception:
            n = 500
        return max(1, min(5000, n))

    def _log_limit(self):
        """日志视图查询上限：与 QPlainTextEdit 的 3000 行上限一致。"""
        return 3000

    def _refresh_all(self):
        """一次性刷新：任务表 + 统计/需要处理/徽标 + 日志页 + 该任务日志。

        显式刷新一律绕过缓存重查（force=True）。"""
        self._refresh_tasks()
        self._refresh_meta(force=True)
        self._reload_log_page(force=True)
        self._refill_task_log()

    def _refresh_tasks(self, preserve_view=False):
        """按「范围 + 失败过滤（唯一状态）」装载任务表，并同步底栏失败块。

        preserve_view=True：生命周期刷新（事件驱动）专用——保留用户选中与滚动位置；
        默认 False 保持既有「显式刷新 / 换范围后回到顶部」的行为。
        """
        try:
            counts = db.count_tasks() or {}
        except Exception:
            counts = {}
        try:
            rows = db.list_tasks(
                scope=self.task_page.scope(),
                result_filter=("failed" if self._failed_filter else None),
                limit=self._task_limit()) or []
        except Exception:
            rows = []
        try:
            self.task_page.set_tasks(rows, counts, preserve_view=preserve_view)
        except Exception:
            pass
        try:
            self.statusbar.set_failed_count(int(counts.get("failed", 0) or 0))
        except Exception:
            pass

    def _on_tasks_refresh_timer(self):
        """防抖定时器到点：执行一次保留视图的任务表刷新。"""
        try:
            self._refresh_tasks(preserve_view=True)
        except Exception:
            pass

    def _schedule_tasks_refresh(self):
        """请求一次任务表刷新（合并突发事件）。

        若定时器已在计时则不重启——保证连续事件流下刷新率有上界（约每 250ms 一次），
        且首个事件后 250ms 必刷一次（不会因事件连绵而饿死）。
        """
        t = getattr(self, "_tasks_refresh_timer", None)
        if t is None:
            return
        try:
            if not t.isActive():
                t.start()
        except Exception:
            pass

    def _emit_tasks_changed(self):
        """上报任务生命周期事件（尽力而为）：Hub 实现 tasks_changed 才投递。

        供主窗口自身的任务写路径（拖放解压 / 忽略任务）复用；测试桩 Hub 未实现时静默跳过。
        """
        try:
            cb = getattr(self.hub, "tasks_changed", None)
            if cb is not None:
                cb()
        except Exception:
            pass

    def _refresh_meta(self, force=False):
        """刷新右栏「今日统计」+「需要处理」+ NavTabs 徽标（全部取真实数据）。

        M3-QA：结果带缓存——没有新日志（_append_log 置脏）时重复切页不再重查
        这三块 DB/文件数据；force=True 供 _refresh_all 等显式刷新使用。"""
        if not force and not getattr(self, "_meta_cache_dirty", True):
            return
        self._meta_cache_dirty = False
        try:
            self.log_page.set_stats(ui_pages.today_stats())
        except Exception:
            pass
        try:
            self.log_page.set_needs(self._need_items())
        except Exception:
            pass
        self._refresh_badges()

    def _need_items(self):
        """failed + need_password 两类任务 -> NeedsAttentionCard 行（各取最近若干条）。"""
        items = []
        try:
            failed = db.list_tasks(scope="history", result_filter="failed",
                                   limit=20) or []
        except Exception:
            failed = []
        for t in failed:
            items.append(ui_pages.needs_item(t))
        try:
            queue = db.list_tasks(scope="queue", limit=100) or []
        except Exception:
            queue = []
        n_pw = 0
        for t in queue:
            if str(t.get("state")) == "need_password" and n_pw < 20:
                items.append(ui_pages.needs_item(t))
                n_pw += 1
        return items

    def _refresh_badges(self):
        """标签徽标：运行日志（今日日志数）/ 密码本（长期密码数）/ 删除回溯（记录数）。"""
        try:
            self.nav_tabs.set_badge("log", ui_pages.count_today_logs())
        except Exception:
            pass
        try:
            self.nav_tabs.set_badge("password", len(self.state.passwords() or []))
        except Exception:
            pass
        try:
            self.nav_tabs.set_badge("trail", len(deletion_trail.load_records() or []))
        except Exception:
            pass

    def _reload_log_page(self, force=False):
        """按日志页当前三滤镜重查 db 并整页重载（全局行永不被路径挡住由 db/页面保证）。

        M3-QA：滤镜签名未变且没有新日志时直接跳过——重复切到日志页不再打 DB、
        不再整页重画（重复切换实测个位数毫秒）；新日志/滤镜变化/清空/
        显式刷新会置脏或改变签名，从而触发重查。"""
        lp = getattr(self, "log_page", None)
        if lp is None:
            return
        try:
            sig = (lp.levels(), frozenset(lp.sources() or ()), lp.keyword(),
                   self._log_limit())
        except Exception:
            sig = None
        if (not force and sig is not None
                and not getattr(self, "_log_cache_dirty", True)
                and sig == getattr(self, "_log_cache_sig", None)):
            return
        try:
            rows = db.query_logs(levels=None, sources=(lp.sources() or None),
                                 keyword=(lp.keyword() or None),
                                 limit=self._log_limit()) or []
        except Exception:
            rows = []
        try:
            lp.reload(rows)
        except Exception:
            pass
        self._log_cache_sig = sig
        self._log_cache_dirty = False

    def _mark_log_dirty(self):
        """置脏：下一次切回日志页时重查一次（清空按钮等显式动作调用）。"""
        self._log_cache_dirty = True

    def _refill_task_log(self):
        """重装「该任务日志」视图：当前任务 -> db.task_logs；全部 -> 全局日志流。"""
        tp = getattr(self, "task_page", None)
        box = getattr(self, "log_box", None)
        if tp is None or box is None:
            return
        rows = []
        try:
            if tp.log_scope() == "all":
                rows = list(reversed(db.query_logs(limit=self._log_limit()) or []))
            else:
                tid = tp.current_task_id()
                if tid is not None:
                    rows = db.task_logs(tid, limit=2000) or []
        except Exception:
            rows = []
        try:
            box.clear()
        except Exception:
            pass
        for r in rows:
            try:
                self._append_log_to(box, ui_pages.task_log_line(r))
            except Exception:
                pass
        try:
            tp.set_log_empty(not rows, tp.log_empty_copy())
        except Exception:
            pass

    def _select_task(self, task_id, name=None):
        """选中任务（None = 清空）：更新徽标 + 重装该任务日志。"""
        if task_id is None:
            try:
                self.task_page.set_current_task(None)
            except Exception:
                pass
            self._refill_task_log()
            return
        if name is None:
            try:
                task = db.get_task(task_id) or {}
            except Exception:
                task = {}
            name = task.get("file_name")
        try:
            self.task_page.set_current_task(
                {"id": int(task_id), "file_name": name}, name)
        except Exception:
            pass
        self._refill_task_log()

    def _on_task_activated(self, task_id):
        self._select_task(task_id)

    def _on_task_action(self, task_id, kind):
        if str(kind) == "retry":
            self._retry_task(task_id)
        elif str(kind) == "open_dir":
            self._open_task_dir(task_id)

    def _on_need_task_activated(self, task_id):
        """「需要处理」整行点击：跳到任务页并选中该任务（只看它自己的日志）。"""
        self._goto_page("tasks")
        try:
            if self.task_page.table.select_task(task_id):
                return
        except Exception:
            pass
        self._select_task(task_id)

    def _on_needs_action(self, task_id, kind):
        kind = str(kind)
        if kind == "retry":
            self._retry_task(task_id)
        elif kind == "open_dir":
            self._open_task_dir(task_id)
        elif kind == "input_password":
            self._input_password_for_task(task_id)
        elif kind == "ignore":
            self._ignore_task(task_id)

    def _retry_task(self, task_id):
        """重试 failed / need_password / canceled 任务。

        监听线程对「文件身份未变」的文件有去重表（FolderWatcher.traced），主界面
        无法安全地把它重新排队；这里复用**现有拖放入口** `_handle_drop_file` 做真
        重试：源文件仍在监听目录 -> 立即后台重新解压（会用上新加入密码本的密码）；
        源文件已不在 -> 记一行明确提示。绝不新造 worker、绝不假报「已排队」。"""
        try:
            task = db.get_task(task_id) or {}
        except Exception:
            task = {}
        state = str(task.get("state") or "")
        name = str(task.get("file_name") or "该任务")
        if state not in ("failed", "need_password", "canceled"):
            self._append_log(f"[任务] {name} 当前不需要重试（状态：{state or '未知'}）")
            return
        path = None
        try:
            import os
            src = str(task.get("source_dir") or "")
            if src and name:
                cand = os.path.join(src, name)
                if os.path.isfile(cand):
                    path = cand
        except Exception:
            path = None
        if path is None:
            self._append_log(
                f"[任务] 重试失败：源文件已不在监听目录（{name}）；"
                f"请重新下载或把文件拖进窗口")
            return
        self._append_log(f"[任务] 重试：已把 {name} 重新交给解压引擎（拖放入口）")
        try:
            self._handle_drop_file(path)
        except Exception as e:
            self._append_log(f"[任务] 重试出错: {e}")

    def _open_task_dir(self, task_id):
        """打开该任务的输出目录；不可用时退化到源目录，再退化为一行日志提示。"""
        try:
            task = db.get_task(task_id) or {}
        except Exception:
            task = {}
        import os
        for key, label in (("output_dir", "输出目录"), ("source_dir", "源目录")):
            path = str(task.get(key) or "").strip()
            if not path:
                continue
            try:
                if os.path.isdir(path):
                    os.startfile(path)          # 仅 Windows；本程序只支持 Windows
                    return
            except Exception as e:
                self._append_log(f"[任务] 打开{label}失败: {e}")
                return
        self._append_log("[任务] 该任务还没有可打开的目录（输出目录未生成）")

    def _copy_task_log(self):
        """复制该任务日志到剪贴板；日志为空时退化为复制文件名。"""
        try:
            text = self.log_box.toPlainText() if self.log_box is not None else ""
        except Exception:
            text = ""
        if not text.strip():
            cur = self.task_page.current_task() or {}
            text = str(cur.get("file_name") or "")
        if not text.strip():
            self._append_log("[任务] 没有可复制的内容")
            return
        try:
            QApplication.clipboard().setText(text)
            self._append_log("[任务] 已复制该任务日志到剪贴板")
        except Exception as e:
            self._append_log(f"[任务] 复制失败: {e}")

    def _input_password_for_task(self, task_id):
        """待密码任务的「输入密码」：跳到「密码本」页补码（不再弹旧密码本弹窗）。"""
        self._append_log(
            "缺少解压密码：已在所有来源（固定密码本 · 临时密码 · 分享提取码）中查找，"
            "均无匹配密码；请到「密码本」页补充后点「重试」。")
        self._goto_page("password")

    def _ignore_task(self, task_id):
        """忽略待密码/失败任务：标记为已取消（终态），从「需要处理」列表移除。"""
        try:
            task = db.get_task(task_id) or {}
        except Exception:
            task = {}
        if str(task.get("state") or "") not in ("need_password", "failed"):
            return
        try:
            db.update_task_state(int(task_id), "canceled",
                                 finished_at=int(time.time()))
        except Exception:
            pass
        self._emit_tasks_changed()
        self._append_log(f"[任务] 已忽略: {task.get('file_name') or task_id}")
        self._refresh_all()

    # ---------- 失败过滤（唯一状态，双向同步） ----------
    def _set_failed_filter(self, failed):
        """任务页「失败」分段与底栏「N 个失败」共用的唯一翻转点。"""
        self._failed_filter = bool(failed)
        try:
            self.task_page.set_result_filter(self._failed_filter)
        except Exception:
            pass
        if self._failed_filter:
            self._goto_page("tasks")
            try:
                self.task_page.table.scroll_to_top()
            except Exception:
                pass
        self._refresh_tasks()

    def _on_fail_requested(self):
        """底栏失败块点击：与分段一样翻转同一状态（开启时跳任务页 + 滚到顶）。"""
        self._set_failed_filter(not self._failed_filter)

    # ---------- 页面切换 ----------
    def _cycle_page(self, step):
        """Ctrl+PgDn / Ctrl+PgUp：按标签顺序循环切页（末页->首页 / 首页->末页）。"""
        try:
            keys = [t.key for t in self.nav_tabs.tabs()]
            cur = self.nav_tabs.current()
        except Exception:
            return
        self._goto_page(_cycle_key(keys, cur, step))

    def _goto_page(self, key):
        """程序化跳页：set_current 不发信号，这里显式同步 NavTabs 与页面栈。"""
        try:
            self.nav_tabs.set_current(key)
        except Exception:
            pass
        self._on_nav_changed(key)

    def _on_nav_changed(self, key):
        try:
            idx = _PAGE_KEYS.index(str(key))
        except ValueError:
            idx = 0
        try:
            self.pages.setCurrentIndex(idx)
        except Exception:
            pass
        if str(key) == "tasks":
            self._refresh_tasks()
        elif str(key) == "log":
            self._refresh_meta()
            self._reload_log_page()
        elif str(key) == "password":
            self._refresh_badges()
            try:
                self.password_page.reload()     # 切回重查：新捕获的口令立即可见
            except Exception:
                pass
        elif str(key) == "trail":
            self._refresh_badges()
            try:
                self.trail_page.reload()        # 切回重读回溯记录
            except Exception:
                pass

    # ---------- 目录胶囊 / 目录设置弹窗 ----------
    def _on_chip_dir_activated(self, idx):
        """点击目录胶囊：打开目录设置弹窗（不跳页、不展开）。"""
        if not (0 <= int(idx) < len(self._watch_paths())):
            return
        self._open_watchdir_dialog(int(idx))

    def _open_watchdir_dialog(self, idx):
        """打开 WatchDirDialog 并接线保存/移除；返回 exec_ 的返回码。"""
        dlg = WatchDirDialog(self.state, int(idx), self)
        self._watchdir_dlg = dlg
        dlg.saved.connect(self._on_watchdir_saved)
        dlg.removeRequested.connect(self._on_watchdir_remove_requested)
        try:
            self.chip_strip.set_highlight(int(idx))
        except Exception:
            pass
        try:
            code = dlg.exec_()
        finally:
            self._watchdir_dlg = None
            try:
                self.chip_strip.set_highlight(-1)
            except Exception:
                pass
        return code

    def _on_watchdir_saved(self, idx):
        """目录设置保存：立即刷新胶囊条与筛选 chips（state.update_path 已写回配置）。"""
        self.rebuild_cards()

    def _on_watchdir_remove_requested(self, idx):
        """弹窗「移除目录」：二次确认后移除（弹窗自身只发信号，确认由宿主负责）。"""
        paths = self._watch_paths()
        label = ""
        if 0 <= int(idx) < len(paths):
            label = str(paths[int(idx)].get("path") or "")
        ret = QMessageBox.question(
            self, "移除目录",
            "确定移除该监听目录？\n%s" % (label or "（空路径）"),
            QMessageBox.Yes | QMessageBox.No, QMessageBox.No)
        if ret != QMessageBox.Yes:
            return
        self._remove_path(int(idx))
        dlg = self._watchdir_dlg
        if dlg is not None:
            try:
                dlg.reject()               # 条目已移除：关闭弹窗，避免继续编辑已删条目
            except Exception:
                pass

    def _on_dir_state(self, item):
        """dir_state 消息 -> 对应胶囊 + 日志页筛选灯（规范化路径回映射到条目索引）。"""
        try:
            key = str(item.get("dir") or "")
            state = item.get("state") or "listening"
            progress = item.get("progress")
            name = item.get("name")
            self._dir_states[key] = (state, progress, name)
            for i, entry in enumerate(self._watch_paths()):
                raw = str(entry.get("path") or "")
                if raw and _norm_path_for_cfg(raw) == key:
                    self.chip_strip.update_dir_state(i, state, progress, name)
                    self.log_page.update_dir_state(raw, state)
                    return
        except Exception:
            pass

    def _append_log_to(self, box, msg):
        """把一行按既定「着色 + 可点链接」管线写入指定视图（薄包装，见 _append_log_html）。

        「该任务日志」与「运行日志页」两处共用本方法（渲染器只有一份，绝不复制）。"""
        _append_log_html(self, box, msg)

    def _task_view_accepts(self, record):
        """该任务日志视图是否接收（薄包装，见 _accepts_in_task_view）。"""
        return _accepts_in_task_view(self, record)

    def _sync_hotkey_display(self):
        """把配置里的主热键同步到底栏「快捷键 <config>」与播报末句。

        不谎报：配置了组合键但**注册失败**（如已被其他程序占用）时，追加
        「（未生效）」标记，并把原因放进 tooltip；注册成功或尚未尝试时保持
        原文案（只用配置值）。"""
        try:
            combo = str(self.state.snapshot().get("hotkey", "")).strip()
        except Exception:
            combo = ""
        ok = getattr(self, "_hotkey_ok", {}).get("main")
        display = combo
        tip = ""
        if combo and ok is False:
            # 短标记优先：底栏横向空间有限，失败原因放 tooltip，避免长文案被挤压。
            display = combo + "（未生效）"
            tip = "快捷键 %s 注册失败（可能已被其他程序占用），当前不可用。" % combo
        try:
            self.statusbar.set_hotkey(display)
        except Exception:
            pass
        try:
            self.statusbar.hotkey_label.setToolTip(tip)
        except Exception:
            pass

    def _repolish_dynamic(self):
        """主题切换后重算动态属性选择器（Qt 不会自动重算，必须显式 repolish）。"""
        try:
            self.nav_tabs.set_current(self.nav_tabs.current())
        except Exception:
            pass
        try:
            for chip in self.chip_strip.chips():
                chip.set_selected(chip.is_selected())
        except Exception:
            pass
        try:
            for seg in (self.task_page.seg_scope, self.task_page.seg_result,
                        self.task_page.seg_logscope, self.log_page.seg_level):
                seg.set_current(seg.current())
        except Exception:
            pass
        try:
            for chip in self.log_page.filters.chips():
                chip.set_active(chip.is_active())
        except Exception:
            pass

    def _log_color_for(self, msg):
        """关键词 → 日志行颜色（_append_log 与链接降级共用，保证降级后与原行同色）。

        UX-3 颜色角色：蓝色只属于**可交互**的链接（log_link），行文字不再用蓝色——
        info 类关键词一律落到中性默认色，避免「蓝字看着也能点」的误导。"""
        m = msg
        if "失败" in m or "出错" in m or "错误" in m:
            return PALETTE["log_error"]      # 错误：红
        if "完成" in m or "成功" in m or "开始监听" in m:
            return PALETTE["log_success"]    # 成功：绿
        if ("发现压缩包" in m or "开始智能解压" in m or "已捕获临时密码" in m
                or "识别到二维码" in m or "正在打开" in m or "归位" in m
                or "翻译" in m or "网址" in m):
            return PALETTE["log_default"]    # 信息：中性（蓝色留给可点链接）
        if ("分卷" in m or "下载未完成" in m or "密码" in m
                or "超时" in m or "监控" in m or "等待" in m):
            return PALETTE["log_wait"]       # 等待/提示：黄
        return PALETTE["log_default"]        # 默认

    def _append_log(self, msg, record=None, shown=None):
        """按事件类型着色追加日志，并按视图规则分流（M3）。

        - record 为 None：主线程直接调用（分享/密码本/托盘等），按全局日志处理；
        - record 为 dict：来自 hub 队列（text/level/source_dir/task_id）或 db 行。
        - shown：仅当「落库正文」与「显示文本」需要不同时给出（分享记录行显示补
          "[HH:MM:SS] " 前缀、落库保持原正文；见 _drain 的 share_link 分支）。
        分流目标：
        - 「该任务日志」：当前任务模式只收同 task_id；「全部」模式收全量（与日志页同源）；
        - 「运行日志页」：按级别+路径+搜索三滤镜判定（全局行永不被路径挡住）。
        两处复用同一个 _append_log_to 渲染器，绝不复制着色/链接管线。
        """
        # 主线程独有的日志行（未经 Hub.log 路由）也写入 log_index；已由 Hub.log
        # 落库的行带 "[HH:MM:SS] " 前缀（_drain 转发），跳过以免重复计数。
        # 新日志 = 日志页/右栏统计都可能变：置脏，下一次装载时重查（缓存失效点）。
        self._log_cache_dirty = True
        self._meta_cache_dirty = True
        if record is None and not _HUB_LOG_PREFIX.match(msg):
            try:
                db.add_log_index(int(time.time()), hub.guess_level(msg), msg)
            except Exception:
                pass
        view_msg = msg if shown is None else shown
        if _accepts_in_task_view(self, record):
            box = getattr(self, "log_box", None)
            if box is not None:
                _append_log_html(self, box, view_msg)
        lp = getattr(self, "log_page", None)
        if lp is not None:
            try:
                if lp.accepts_record(record, msg):
                    lp.append_line(view_msg)
            except Exception:
                pass

    # ---------- 日志链接：悬停手型 + 单击静默复制 + 降级 ----------
    def _log_view_for(self, obj):
        """obj 属于哪个日志视图的 viewport（薄包装，见 module 级 _log_view_for）。"""
        return _log_view_for(self, obj)

    def _hit_log_link(self, pos, box=None):
        """定位点击处「尚未使用过」的链接：返回 (block, start, end, url) 或 None。

        坐标系：pos 为目标视图 viewport 坐标（cursorForPosition 所需）。命中判定用
        「点击那一刻」该 block 的 position() + block 内偏移现场构造，不缓存绝对位置
        或 block 号——3000 行上限会裁掉旧 block 使编号整体位移，任何持久映射都会错位。
        box 缺省为 self.log_box（该任务日志），运行日志页由 eventFilter 显式传入。
        """
        if box is None:
            box = self.log_box
        cursor = box.cursorForPosition(pos)
        block = cursor.block()
        offset = cursor.positionInBlock()
        for s, e, url in _find_log_urls(block.text()):
            if s <= offset < e:
                if self._block_span_has_anchor(block, s, e):
                    return block, s, e, url
                return None   # 已降级的链接：不再触发
        return None

    @staticmethod
    def _block_span_has_anchor(block, start, end):
        """该 block 上 [start, end) 区间是否仍带 anchorHref（即尚未被点过而降级）。

        注意：QPlainTextEdit 的富文本格式**不在** `block.layout().formats()` 里
        （实测恒为空），而在该 block 的文本 fragment（`block.begin()` 迭代出的
        QTextFragment.charFormat()）上。因此这里遍历 fragment 判定，语义与「span 在
        layout formats 里带 anchorHref」完全一致：只有**还没被点过**的链接才带 href。
        """
        try:
            it = block.begin()
            while not it.atEnd():
                fr = it.fragment()
                it += 1
                if not fr.isValid():
                    continue
                fs = fr.position() - block.position()
                fe = fs + fr.length()
                if fs <= start and fe >= end:
                    return bool(fr.charFormat().anchorHref())
        except Exception:
            pass
        return False

    def eventFilter(self, obj, event):
        """日志视图事件过滤器：悬停手型光标 + 单击未使用链接静默复制。

        该任务日志与运行日志页共用同一套命中/降级/回滚管线（见 _hit_log_link）。"""
        try:
            box = _log_view_for(self, obj)
            if box is not None:
                et = event.type()
                if et == QEvent.MouseMove:
                    hit = self._hit_log_link(event.pos(), box)
                    box.viewport().setCursor(
                        Qt.PointingHandCursor if hit else Qt.IBeamCursor)
                elif et == QEvent.MouseButtonRelease:
                    # 用户正在拖选大段日志时绝不干扰：存在选区直接放过。
                    if (event.button() == Qt.LeftButton
                            and not box.textCursor().hasSelection()):
                        hit = self._hit_log_link(event.pos(), box)
                        if hit is not None:
                            # 传点击的全局坐标：气泡要弹在点击附近（QMouseEvent 必有）
                            try:
                                gpos = event.globalPos()
                            except Exception:
                                gpos = None
                            self._on_log_link_clicked(*hit, pos=gpos, box=box)
                            return True
        except Exception:
            pass
        return False

    def _on_log_link_clicked(self, block, start, end, url, pos=None, box=None):
        """点击未使用链接：立即降级为普通文本色（即时反馈）并请求监控线程静默复制。

        pos：点击处的**全局**坐标（eventFilter 传 event.globalPos()；缺省用
        QCursor.pos()），用于在点击附近弹「已复制」小气泡。box：点击所在的日志
        视图（eventFilter 传入；旧调用可不传，只影响重载后保持降级与失败精确恢复）。
        复制本身是异步的（worker 线程写剪贴板，避免被本程序当成新输入）：投递成功
        即弹「已复制」，连请求都投不出去则弹「复制失败」并回滚链接；异步失败的回执
        同样会弹「复制失败」并恢复可点样式（见 _drain 的 clip_done 分支）。"""
        self._degrade_log_link(block, start, end, url)
        rec = _record_degraded_link(self, box, (block, start, end, url))
        try:
            self._last_link_toast_pos = pos if pos is not None else QCursor.pos()
        except Exception:
            self._last_link_toast_pos = None
        queued = False
        try:
            self.hub.clip_echo_q.put(url)
            queued = True
        except Exception:
            # hub 桩可能没有该属性：回滚链接，让用户能再点一次
            queued = False
        if queued:
            toast = _copy_toast(self, "已复制", pos)
            if isinstance(rec, dict):
                # 失败回执时先撤下这条气泡，避免与「复制失败」同屏出现
                rec["toast"] = toast
        else:
            self._restore_log_link(url)
            _copy_toast(self, "复制失败", pos)

    def _degrade_log_link(self, block, start, end, url):
        """就地去掉 [start,end) 的 anchor，并按「蓝色 = 仅可交互」降级。

        降级后的片段：无 anchorHref（_hit_log_link 不再命中、悬停恢复 IBeam）、
        无下划线、用 PALETTE['log_ts'] 暗灰（与时间戳同一色）。位置用「当前
        block.position() + block 内偏移」现场计算，随 block 移动自动跟随，不依赖
        任何跨 block 的绝对位置缓存。
        """
        fmt = QTextCharFormat()
        fmt.setAnchor(False)
        fmt.setAnchorHref("")
        fmt.setFontUnderline(False)
        fmt.setForeground(QColor(PALETTE["log_ts"]))
        base = block.position()
        cursor = QTextCursor(block)
        cursor.setPosition(base + start)
        cursor.setPosition(base + end, QTextCursor.KeepAnchor)
        cursor.setCharFormat(fmt)

    def _restore_log_link(self, url):
        """复制失败时恢复可点样式：优先精确恢复「被点击的那一行」。

        回执只带 URL，不带来源视图/行。若本进程记得该 URL 的降级记录（点击时登记：
        视图 + 行正文 + 区间），就按记录恢复并且顺带撤下可能还在屏上的「已复制」
        气泡；找不到记录才退回旧兜底（两个视图里第一个已无 anchor 的片段）。"""
        if not url:
            return
        rec = _pop_degraded_link(self, url)
        if rec is not None:
            # 先撤下可能还在屏上的「已复制」气泡（幂等；已自关时静默）
            try:
                toast = rec.get("toast")
                if toast is not None:
                    toast.close_toast()
            except Exception:
                pass
            _restore_degraded_in_view(rec)
            return
        targets = []
        lb = getattr(self, "log_box", None)
        if lb is not None:
            targets.append(lb)
        lp = getattr(self, "log_page", None)
        try:
            if lp is not None:
                targets.append(lp.log_view)
        except Exception:
            pass
        for box in targets:
            _restore_link_in_view(box, url)

    # ---------- 主题（深浅色）----------
    def _on_system_theme_changed(self):
        """系统深浅色切换（WM_SETTINGCHANGE）→ 仅当偏好为 auto 时跟随。"""
        try:
            pref = str(self.state.snapshot().get("ui_theme", "auto")).lower()
            if pref != "auto":
                return
            want = ui_style.detect_system_theme()
            if want == ui_style.current_theme():
                return
            ui_style.apply_theme(QApplication.instance(), want)
            self.on_theme_changed(want)
            self.state.set("ui_theme_cached", want)
        except Exception:
            pass

    def on_theme_changed(self, theme):
        """主题已切换：刷新胶囊/筛选 chips + 重算动态属性 + 重设标题栏 + 记一条日志。"""
        try:
            self.rebuild_cards()
        except Exception:
            pass
        try:
            self._repolish_dynamic()
        except Exception:
            pass
        try:
            self.statusbar.refresh_theme()
        except Exception:
            pass
        try:
            self.log_page.refresh_theme()
        except Exception:
            pass
        # M4：三个正式页的内联色也在同一条主题切换路径里重贴（不另造机制）
        for page in (getattr(self, "password_page", None),
                     getattr(self, "trail_page", None),
                     getattr(self, "settings_page", None)):
            try:
                refresh = getattr(page, "refresh_theme", None)
                if callable(refresh):
                    refresh()
            except Exception:
                pass
        # 已画出的日志行内联色是渲染时写死的：按新调色板逐块重画（保留已复制
        # 链接的降级状态），否则切主题后旧行仍是旧色（F3e）。
        try:
            _rerender_log_view(self, getattr(self, "log_box", None))
        except Exception:
            pass
        try:
            _rerender_log_view(self, self.log_page.log_view)
        except Exception:
            pass
        try:
            QTimer.singleShot(0, self._apply_titlebar)
        except Exception:
            pass
        try:
            self.hub.log(f"界面主题已切换: {theme}")
        except Exception:
            pass

    # ---------- 屏幕自适应：初始几何 / 最小尺寸 / 跨屏守卫 ----------
    def _available_geometry(self, screen=None):
        """（指定/主）屏可用区域（逻辑像素）；取不到时退化 1280x720，绝不抛异常。"""
        try:
            if screen is None:
                app = QApplication.instance()
                screen = app.primaryScreen() if app is not None else None
            if screen is not None:
                g = screen.availableGeometry()
                return max(1, g.width()), max(1, g.height())
        except Exception:
            pass
        return 1280, 720

    def _apply_screen_limits(self, screen=None):
        """把最小尺寸收敛到（指定/主）屏可用区（规则见 fit_min_size）。

        跨屏时必须传目标屏：否则最小尺寸会继续按主屏算，窗口换到小屏后
        连缩都缩不下去（resize 被旧的最小尺寸挡住）。
        """
        try:
            aw, ah = self._available_geometry(screen)
            hint = self.minimumSizeHint()
            mw_, mh_ = fit_min_size(hint.width(), hint.height(), aw, ah)
            self.setMinimumSize(mw_, mh_)
            return mw_, mh_
        except Exception:
            return None

    def _sync_page_min_heights(self):
        """把五个页面的最小高度对齐其首选高度（窗口显示后执行一次）。

        页面装在纵向 QScrollArea 里（见 _make_scrollable_page），但默认最小
        高度仍只有内部控件的 min（任务页 ≈223）——窗口再矮时表格会被压到
        一行都显示不全。抬高到 sizeHint 后，窗口装不下时由滚动条兜底：
        表格保持「表头 + 完整行」，而不是被压成一条线。
        （页面首次显示的补算在 _PageScroll.showEvent 里，二者互补。）
        """
        for page in (self.task_page, self.log_page, self.password_page,
                     self.trail_page, self.settings_page):
            try:
                h = int(page.sizeHint().height())
                if h > 0:
                    page.setMinimumHeight(h)
            except Exception:
                pass
        # 页面最小高度变了：窗口最小尺寸需要按新 hint 重算一次
        self._apply_screen_limits()

    def _chrome_hint_label(self):
        """胶囊条说明文案标签（对象名 stripHint），缓存引用避免每次重找。"""
        lbl = getattr(self, "_chrome_hint_lbl", None)
        if lbl is None:
            try:
                lbl = self.chip_strip.findChild(QWidget, "stripHint")
            except Exception:
                lbl = None
            self._chrome_hint_lbl = lbl
        return lbl

    def _apply_chrome_compact(self):
        """窗口过窄时收起次要装饰（响应式），保证常显控件一个都不被裁切。

        阈值 _CHROME_COMPACT_W 来自两条常显栏的自然宽度（胶囊条含提示文案
        ≈794 逻辑像素、底栏含 336px 播报 ≈853）。窄于该量级时收起「提示
        文案 + 播报 + 使用提示标签」这类装饰；真实控件（添加目录 / 胶囊 /
        状态 / 进度 / 失败 / 快捷键）永不隐藏。
        """
        try:
            compact = int(self.width()) < _CHROME_COMPACT_W
            hint = self._chrome_hint_label()
            if hint is not None:
                hint.setVisible(not compact)
            sb = getattr(self, "statusbar", None)
            if sb is not None:
                for name in ("ticker", "tip_icon", "tip_label"):
                    obj = getattr(sb, name, None)
                    if obj is not None:
                        obj.setVisible(not compact)
        except Exception:
            pass

    def resizeEvent(self, event):
        super().resizeEvent(event)
        self._apply_chrome_compact()

    def _install_screen_watch(self):
        """窗口显示后挂上「屏 / 缩放变化」监视；Qt5 无法自动重排时至少留日志。"""
        try:
            wh = self.windowHandle()
        except Exception:
            wh = None
        if wh is not None:
            sig = getattr(wh, "screenChanged", None)
            if sig is not None:
                try:
                    sig.connect(self._on_screen_changed)
                except Exception:
                    pass
        try:
            getter = getattr(self, "screen", None)
            scr = getter() if getter is not None else None
            if scr is None:
                app = QApplication.instance()
                scr = app.primaryScreen() if app is not None else None
        except Exception:
            scr = None
        if scr is not None:
            sig = getattr(scr, "logicalDotsPerInchChanged", None)
            if sig is not None:
                try:
                    sig.connect(self._on_screen_dpi_changed)
                except Exception:
                    pass

    def _on_screen_changed(self, screen):
        """窗口被拖到另一块屏时：先记日志，再在超屏时收敛回该屏可用区。

        Qt5 的 HiDPI 缩放只在启动时确定：多显示器不同缩放比例时，跨屏后
        **不会**自动重排（Windows 平台仅部分支持）。这里不假装自适应成功——
        写一行明确日志提示可能需要重启，并把大于新屏的窗口收回来，避免窗口
        大半在屏外。
        """
        try:
            g = screen.availableGeometry()
            dpr = float(screen.devicePixelRatio())
            self._append_log(
                "[界面] 检测到窗口跨屏：可用区 %dx%d 逻辑像素、缩放 %.0f%%；"
                "Qt5 跨屏不会自动重排缩放，若界面比例不对请重启程序"
                % (g.width(), g.height(), dpr * 100.0))
            # 顺序：先按新屏放宽最小尺寸，否则超屏窗口 resize 不动
            self._apply_screen_limits(screen)
            w, h = fit_window_size(self.width(), self.height(),
                                   g.width(), g.height())
            if (w, h) != (self.width(), self.height()):
                self.resize(w, h)
        except Exception:
            pass

    def _on_screen_dpi_changed(self, dpi):
        """同一块屏上系统文本缩放变化（WM_DPICHANGED）：记日志并做防溢出。"""
        try:
            now = time.time()
            if now - float(getattr(self, "_dpi_log_ts", 0.0)) < 1.0:
                return
            self._dpi_log_ts = now
            self._append_log(
                "[界面] 检测到系统文本缩放变化（逻辑 DPI %.0f）；"
                "Qt5 不会自动重排已有窗口，界面若有错位请重启程序" % float(dpi))
            try:
                getter = getattr(self, "screen", None)
                scr = getter() if getter is not None else None
            except Exception:
                scr = None
            self._apply_screen_limits(scr)
        except Exception:
            pass

    def _apply_titlebar(self):
        """深色主题时把 Windows 原生标题栏也变深。

        DWMWA_USE_IMMERSIVE_DARK_MODE：新系统属性号 20，旧版 Win10 用 19（两个都试）。
        """
        try:
            import ctypes
            from ctypes import wintypes
            dark = 1 if ui_style.current_theme() == "devtool" else 0
            hwnd = wintypes.HWND(int(self.winId()))
            v = ctypes.c_int(dark)
            dwm = ctypes.windll.dwmapi
            for attr in (20, 19):
                try:
                    if dwm.DwmSetWindowAttribute(hwnd, attr, ctypes.byref(v),
                                                 ctypes.sizeof(v)) == 0:
                        break
                except Exception:
                    continue
        except Exception:
            pass

    def showEvent(self, event):
        super().showEvent(event)
        if not getattr(self, "_titlebar_done", False):
            self._titlebar_done = True
            QTimer.singleShot(0, self._apply_titlebar)
        if not getattr(self, "_page_min_done", False):
            self._page_min_done = True   # 显示后 QSS 字体才定型：页面最小高度此时才准
            QTimer.singleShot(0, self._sync_page_min_heights)
        if not getattr(self, "_screen_watch_done", False):
            self._screen_watch_done = True
            QTimer.singleShot(0, self._install_screen_watch)

    def _drain(self):
        # 二维码「预定任务」过期清理：每轮 tick（200ms）一次；无任务时 O(1) 早退。
        # 超时仍未见 share_link 事件 → 作废 + 记一行 + 一条托盘通知；**绝不**回退到
        # 缓存的旧记录（那正是「拉错链接」的根源）。等待秒数读 config 的
        # share_gesture_wait_sec（缺省 60，clamp 5..600）。
        _pend0 = getattr(self, "_pending_share_gesture", None)
        if _pend0 and (time.time() - _pend0.get("at", 0)) > _share_gesture_wait_sec(self):
            self._pending_share_gesture = None
            _pend_kind = _pend0.get("kind") or "share"
            self._append_log(
                "[分享] 手势等待超时，已取消（未解析出新链接，避免拉错）；"
                f"二维码预定任务已超时作废（{PENDING_SHARE_TAG.get(_pend_kind, _pend_kind)}）")
            self._share_notify(
                "分享手势超时",
                "手势等待超时，已取消（未解析出新链接，避免拉错）")
        # 忙标志看门狗：worker 卡死超过 SHARE_INVOKE_BUSY_MAX_SEC → 强制复位并记一行，
        # 避免后续所有手势被永久挡住（复位只在超时后发生，正常的 finally 复位不受影响）。
        if _share_invoke_busy_stale(self):
            try:
                self._share_invoke_busy = False
            except Exception:
                pass
            self._append_log(
                f"[分享] 上一个拉起已超时（>{SHARE_INVOKE_BUSY_MAX_SEC}s），已强制复位")
        while True:
            try:
                item = self.hub.q.get_nowait()
            except queue.Empty:
                break
            if item["type"] == "log":
                # 记录与文本都交给 _append_log：它按「该任务日志 / 运行日志页」规则分流
                # （Hub.log 已写过 log_index，带 "[HH:MM:SS] " 前缀的行不再重复入库）。
                try:
                    self._append_log(item.get("msg", ""), item)
                except TypeError:
                    # 兼容只接受单个 msg 的旧日志桩（新签名多一个 record 参数）
                    self._append_log(item.get("msg", ""))
            elif item["type"] == "notify":
                if hasattr(self, "tray"):
                    self.tray.showMessage(
                        item["title"], item["msg"], QSystemTrayIcon.Information, 4000)
            elif item["type"] == "dir_state":
                # 监听线程的目录状态：映射到胶囊 / 筛选灯（规范化路径回查条目索引）
                try:
                    self._on_dir_state(item)
                except Exception:
                    pass
            elif item["type"] == "progress_start":
                # 进度只在 StatusBar 呈现；不再有兼容进度条可 show()（防浮窗）
                try:
                    self.statusbar.set_progress(None)
                except Exception:
                    pass
            elif item["type"] == "progress":
                try:
                    self.statusbar.set_progress(item.get("ratio"))
                except Exception:
                    pass
            elif item["type"] == "progress_done":
                try:
                    self.statusbar.clear_progress()
                except Exception:
                    pass
            elif item["type"] == "task":
                # 任务生命周期变化（后台线程写 tasks 表后投递）：合并为一次任务表
                # 刷新（防抖定时器），无需切页即可看到新增/状态迁移；刷新保选中/滚动。
                self._schedule_tasks_refresh()
            elif item["type"] == "clip_done":
                # 日志链接静默复制回执：成功什么都不做（点击时链接已降级、气泡已弹）；
                # 失败则提示一句 + 弹「复制失败」气泡 + 尽力恢复可点样式，让用户能再点一次。
                if not item.get("ok"):
                    self._append_log("[日志] 复制链接失败，可再点一次")
                    self._restore_log_link(item.get("url"))
                    _copy_toast(self, "复制失败")
            elif item["type"] == "url_trust_ask":
                self._handle_trust_ask(item)
            elif item["type"] == "share_link":
                # 分享记录行：显示补 "[HH:MM:SS] " 前缀（与重载后 task_log_line 的形态
                # 一致：同一逻辑行不再因刷新而变形），落库仍是原正文（存储格式不变）。
                # 旧日志桩只接受单参数 → TypeError 兜底退回单参数调用。
                _share_body = (
                    f"[分享] 已记录: {item.get('url')}"
                    f"（托盘菜单「用客户端打开最近分享」可拉起客户端）")
                _share_shown = time.strftime("[%H:%M:%S] ") + _share_body
                try:
                    self._append_log(_share_body, None, _share_shown)
                except TypeError:
                    self._append_log(_share_shown)
                # 预定任务优先：手势曾在二维码解码期落空，此刻记录已入库 → 按登记
                # 时的 kind 派发（唯一分派点，等价于用户此刻按下对应热键）。命中即
                # 消费该任务，并跳过本事件的 baidu_auto_invoke 分支（否则重复拉起）。
                _pend = getattr(self, "_pending_share_gesture", None)
                if (_pend
                        and time.time() - _pend.get("at", 0)
                        <= _share_gesture_wait_sec(self)):
                    self._pending_share_gesture = None
                    self._dispatch_pending_share(_pend)
                    continue
                # 实验性：开启「自动拉起」时才处理（默认关）。
                # 链路含网络 IO，禁止阻塞 Qt 事件循环 → 交给后台线程。
                if (self.state.snapshot().get("experimental_enabled")
                        and self.state.snapshot().get("baidu_auto_invoke")):
                    url = item.get("url")
                    pwd = item.get("pwd") or ""
                    surl = item.get("surl") or url   # d7 去重键：surl（缺失时退回 url）
                    if _gesture_launched_recently(self, surl):
                        # 同一次用户意图刚经手势（含预定派发）拉起：管线再报同链接时
                        # 不再重复拉起，也不弹「重复分享」确认（那就是意图的回声）。
                        self._append_log(
                            f"[分享] 该分享刚由手势拉起，已跳过本次自动拉起: {surl}")
                    elif not pwd:
                        # d3：空提取码绝不自动拉起。先看该分享者有无固定映射：
                        #   有 → 直接弹「询问」（不浪费一次探针）；
                        #   无 → 起后台线程先 prepare_share(url, "") 探测是否根本不需要
                        #        提取码，确需提取码才把「询问」请求投回 Qt 线程。
                        mapped = None
                        try:
                            mapped = bm.mapped_code(item.get("share_uk"))
                        except Exception:
                            mapped = None
                        if mapped:
                            self._share_ask_code(item, url, surl)
                        else:
                            self._start_share_pick(url, surl, "", manual=False,
                                                   item=item)
                    elif self._share_needs_consent(surl):
                        # d7：同一分享本次运行已拉起过 → 未经用户同意不再自动拉起
                        self._confirm_share_reinvoke(surl, url, pwd)
                    else:
                        self._start_share_pick(url, surl, pwd, manual=False)
                # 触发小窗（三触发之一）：链接本身无码、也查不到固定码 → 弹/聚焦
                # 提取码小窗（**不管** baidu_auto_invoke 开关、不管是否手势）。有码
                # 则不弹；未知分享者顺手开一次浏览器探针（已在弹小窗时不重复开）。
                # 刻意放在 baidu_auto_invoke 分支之后：不改变其既有顺序与语义。
                try:
                    if self.state.snapshot().get("experimental_enabled"):
                        _pwd = str(item.get("pwd") or "").strip()
                        if not _pwd:
                            _mapped = None
                            try:
                                _mapped = bm.mapped_code(item.get("share_uk"))
                            except Exception:
                                _mapped = None
                            if not _mapped:
                                _show_share_code_window(
                                    self, item.get("surl"), item.get("url"),
                                    item.get("share_uk"), mapped_code="",
                                    open_browser=True)
                except Exception:
                    pass
            elif item["type"] == "share_pick":
                # 后台 worker 请求挑选文件：在 Qt 线程构造挑选窗（worker 绝不碰控件）
                self._show_share_pick(item)
            elif item["type"] == "share_ask":
                # 后台探测发现该分享需要提取码：在 Qt 线程弹询问面板（worker 绝不碰控件）
                self._share_ask_code(item.get("item") or {}, item.get("url"),
                                     item.get("surl"))
            elif item["type"] == "share_used":
                # 成功拉起回执（worker 线程投递）：该分享已成功提取，关闭同分享的
                # 缺码小窗——它只为填写提取码而生，绝不让它赖到 120s 超时。
                _close_share_ask_dlg(self, item.get("surl"), item.get("uk"),
                                     item.get("url"))

    def _refresh_share_menu(self):
        """按「实验性功能」总开关刷新分享菜单项可见性（整条 2.F 属实验性）。"""
        try:
            visible = bool(self.state.snapshot().get("experimental_enabled"))
            if hasattr(self, "_open_share_action"):
                self._open_share_action.setVisible(visible)
            if hasattr(self, "_open_share_code_action"):
                self._open_share_code_action.setVisible(visible)
        except Exception:
            pass

    def _register_pending_share(self, kind, inflight=False):
        """二维码解码期/分享输入在途「预定任务」共享登记助手（Alt+2 / Alt+3 共用，勿复制粘贴）。

        返回 True 表示已登记或已刷新（调用方应立即 return，不再走各自的原提示）；
        返回 False 表示当前既无在途输入也不在解码期、或实验性功能已关 → 调用方保持
        各自原文案逐字不变。
        `inflight=True`：本次来自「分享输入在途」判定（覆盖整条解析链，含抓页/记录
        写入），日志/通知使用等待解析的文案；默认 False 保持原二维码文案逐字不变。
        防御性调用：hub 桩可能没有 qr_decoding_active / share_input_pending 方法，
        不可用或抛异常一律按 False，绝不让主界面因这条增强分支崩溃。"""
        try:
            if not self.state.snapshot().get("experimental_enabled"):
                return False
        except Exception:
            return False
        inflight_busy = _share_input_inflight(self)
        decoding = False
        try:
            _fn = getattr(self.hub, "qr_decoding_active", None)
            if callable(_fn):
                decoding = bool(_fn())
        except Exception:
            decoding = False
        if not (inflight_busy or decoding):
            return False
        now = time.time()
        pending = getattr(self, "_pending_share_gesture", None)
        if pending and now - pending.get("at", 0) <= _share_gesture_wait_sec(self):
            # 同一时刻只保留一个预定任务：后按的手势为准，刷新 kind 与 at；
            # 只记一行说明现在记的是哪一路，不重复弹托盘通知。
            pending["kind"] = kind
            pending["at"] = now
            if inflight:
                self._append_log(
                    f"[分享] 已刷新手势意图（{PENDING_SHARE_TAG.get(kind, kind)}）")
            else:
                self._append_log(
                    f"[分享] 已刷新二维码预定任务（{PENDING_SHARE_TAG.get(kind, kind)}）")
            return True
        self._pending_share_gesture = {"kind": kind, "at": now}
        if inflight:
            self._append_log("[分享] 已记住手势：等待当前解析完成后再拉起")
            self._share_notify(
                "分享链接正在解析",
                "检测到分享链接正在解析，解析完成后将自动拉起客户端（无需再按）")
            return True
        reg_log, notify_msg = PENDING_SHARE_TEXT.get(
            kind, PENDING_SHARE_TEXT["share"])
        self._append_log(reg_log)
        self._share_notify("二维码正在解析", notify_msg)
        return True

    def _dispatch_pending_share(self, pend):
        """按 kind 派发预定任务（唯一分派点）：完全等价于此刻按下对应热键。

        Alt+2 → `_open_recent_share()`；Alt+3 → `_open_recent_share_with_code()`（与
        `_on_share_code_hotkey` 调用的同一入口，参数由入口内部从当前记录现算，绝不
        在此自行拼参数语义）。将来合并 Alt+2/Alt+3 只需改这个 if/elif 的一行。"""
        kind = pend.get("kind") or "share"
        # 派发期标记：剪贴板/在途判定都不该再把这次「已确定的派发」重新压回等待，
        # 否则 worker 的 finally 尚未归还计数时会再次登记、最终等成超时。
        # try/finally 保证无论桩替身是否清理标记，标记都不会泄漏到下一次手势。
        try:
            self._share_dispatch_pending = True
        except Exception:
            pass
        try:
            if kind == "share_code":
                self._append_log(
                    "[分享] 二维码解析完成，按预定任务（固定提取码）自动拉起…（Alt+3）")
                self._open_recent_share_with_code()
            else:
                self._append_log("[分享] 二维码解析完成，按预定任务自动拉起…（Alt+2）")
                self._open_recent_share()
        finally:
            try:
                self._share_dispatch_pending = False
            except Exception:
                pass

    def _launch_clipboard_share(self, url, surl, pwd):
        """A 快路径（Alt+2）：用剪贴板同一段文本解析出的链接+码直接拉起。

        码为空时只允许用「收紧口径」的历史码补齐（晚于上一条分享记录、或根本
        没有更早的分享记录）；仍无码则诚实放弃并把提取码小窗摆到用户面前，
        绝不把来源不明的猜测码发给 /share/verify（errno=-9 的根因）。"""
        try:
            self._append_log(f"[分享] 剪贴板内已有分享链接，直接拉起: {url}")
            code, _src = _effective_share_code(
                self, surl, None, pwd, "text" if pwd else "")
            if not code:
                self._append_log(
                    "[分享] 缺少可确认的提取码，已放弃拉起（避免拉错链接）")
                if _share_parent_usable(self):
                    self._share_notify(
                        "分享缺少提取码",
                        f"{url}\n该分享需要提取码：请在右侧小窗填写提取码。")
                else:
                    # 主窗不可用：小窗弹不出来，绝不承诺「右侧小窗」——走统一诚实
                    # 文案（三种方式）并置去重标记，随后 _show_share_code_window
                    # 的兜底提示静默，一次用户动作只有一条气泡。
                    _announce_ask_code_hidden(self, url)
                _show_share_code_window(self, surl, url, None, mapped_code="")
                return
            if _src == "recent":
                self._append_log(
                    f"[分享] 剪贴板链接缺提取码：已按最近复制的提取码补上 -> {code}")
            # 手势成功拉到：清掉可能残留的预定任务，避免事后重复拉起。
            self._pending_share_gesture = None
            _mark_gesture_launch(self, surl)
            self._bump_share_launch(surl)
            self._start_share_invoke(url, code, manual=True)
        except Exception as e:
            self._append_log(f"拉起客户端出错: {e}")

    def _launch_clipboard_share_code(self, url, surl, pwd):
        """A 快路径（Alt+3）：剪贴板同一段文本的链接，取值后走「准备→挑选→提交」。

        取值顺序与 Alt+3 冻结口径一致：小窗码 > 文本自带码 > 收紧历史 >
        该分享者固定映射（仅当该链接已被记录、能拿到 share_uk 时）。仍无码 →
        诚实放弃并摆出提取码小窗。"""
        try:
            self._append_log(
                f"[分享] 剪贴板内已有分享链接，按固定提取码手势处理: {url}")
            _uk = _share_uk_for_surl(surl)
            code, _src = _effective_share_code(
                self, surl, _uk, pwd, "text" if pwd else "")
            if not code:
                try:
                    code = bm.mapped_code(_uk) or ""
                except Exception:
                    code = ""
            if not code:
                self._append_log(
                    "[分享] 缺少可确认的提取码，已放弃拉起（避免拉错链接）")
                if _share_parent_usable(self):
                    self._share_notify(
                        "分享缺少提取码",
                        f"{url}\n该分享需要提取码：请在右侧小窗填写提取码。")
                else:
                    # 主窗不可用：与 Alt+2 快路径同一口径——诚实三方式文案 + 去重，
                    # 绝不先发一条承诺「右侧小窗」的气泡。
                    _announce_ask_code_hidden(self, url)
                _show_share_code_window(self, surl, url, _uk or None, mapped_code="")
                return
            # 手势成功拉到：清掉可能残留的预定任务，避免事后重复拉起（与 Alt+2 一致）。
            self._pending_share_gesture = None
            _mark_gesture_launch(self, surl)
            _call_start_share_pick(self, url, surl, code, manual=True,
                                   bind_code_uk=_uk or None)
        except Exception as e:
            self._append_log(f"按固定提取码下载出错: {e}")

    def _open_recent_share(self):
        """托盘动作：把最近捕获的分享链接交给网盘客户端下载（2.F『拉起』全链路）。

        按压时先就地读剪贴板（A 快路径，零等待）：是 pan.baidu 分享链接就用
        **同一段文本**里的码拉起该链接；否则若当前有分享输入正在解析（B，覆盖
        入队→抓页→解码→记录写入整条链）则登记意图、等解析完成后再拉起；两者
        皆无才回退最近记录（C）。
        走完整分享下载令牌链路后，用 `baiduyunguanjia://evoked-download/…` 唤起
        客户端，由客户端自己完成下载（不下载、不登录、不开网页）。
        链路约 20s（含轮询），必须后台执行，否则会冻住整个界面。"""
        try:
            # 整条 2.F 属实验性功能：全局热键也可能被按下，这里必须再校验一次。
            if not self.state.snapshot().get("experimental_enabled"):
                self._append_log("实验性功能未开启，「用客户端下载分享」不可用")
                return
            # A. 按压时刻的剪贴板：命中分享链接就从该文本就地解析（零等待）。
            #    绝不把文本再交给 QRMonitor 管线（避免重复抓页/记录/双拉起）。
            _fresh = _clipboard_share_target(self)
            if _fresh:
                self._launch_clipboard_share(*_fresh)
                return
            # B. 有分享输入正在解析（图片/网址/解码/记录写入任一环节）→ 记住手势，
            #    绝不立刻拉起旧记录（正是「拉错上一条」的根因）；解析出链接后由
            #    share_link 事件按 kind 自动补按一次。派发期不重复压回等待。
            if (not getattr(self, "_share_dispatch_pending", False)
                    and _share_input_inflight(self)):
                if self._register_pending_share("share", inflight=True):
                    return
            from .. import baidu_task as bt
            rec = bt.last_share()
            if not rec:
                # 手势落空但二维码正在解码中 → 共享助手登记预定任务（Alt+2=share）；
                # 返回 False（不在解码期/实验性关）时保持原提示逐字不变。
                if self._register_pending_share("share"):
                    return
                self._append_log("还没有记录到百度分享链接（复制一下分享链接即可）")
                return
            self._append_log(
                f"[分享] 剪贴板无分享链接，沿用最近记录: {rec.get('url')}")
            # 失效短路：该分享已在本进程被标记失效（抓页/探测/提交任一环节命中）→
            # 零网络请求、**绝不**唤起客户端，直接日志 + 托盘通知。用户手势唤起时
            # 最需要这条：不盯着日志的人也立刻知道链接已失效。
            try:
                dead = bm.share_dead(rec.get("surl") or rec.get("url"))
            except Exception:
                dead = ""
            if dead:
                self._append_log(f"[分享] 该分享链接已失效，已跳过拉起：{dead}")
                self._share_notify("分享链接已失效", f"{rec.get('url')}\n{dead}")
                return
            # 统一取值助手（优先级冻结）：小窗码 > 权威记录码 > 重新取 120s 内最近码。
            # 关键修复：`code_source == "recent"`（抓取时按历史猜的码）不再被黏死——
            # 手势时重新取值，取到更新的剪贴板码就覆盖旧猜测并写回记录（记录自愈），
            # 避免几天前/几分钟前的旧猜测覆盖用户后来复制的正确码。
            _code, _src = _effective_share_code(
                self, rec.get("surl") or rec.get("url"),
                rec.get("share_uk"), rec.get("pwd"), rec.get("code_source"))
            _rec_src = "" if rec.get("code_source") is None else str(rec.get("code_source"))
            _rec_authoritative = (bool((rec.get("pwd") or "").strip())
                                  and _rec_src in ("url", "text", "window", ""))
            # 回写守卫：权威记录码绝不被「recent」猜测覆盖（这正是旧猜测码污染记录
            # 的来源之一）；仅当新值不是猜测、或记录本无权威码时才回写。
            if (_code and _code != (rec.get("pwd") or "").strip()
                    and not (_src == "recent" and _rec_authoritative)):
                rec["pwd"] = _code
                rec["code_source"] = _src
                _srcname = {"window": "小窗中填写", "recent": "最近捕获",
                            "url": "链接自带", "text": "文本内嵌"}.get(
                                _src, _src or "未知来源")
                self._append_log(
                    f"分享缺提取码：已按{_srcname}的提取码补上 -> {_code}"
                    f"（来源 {_src or '未知'}）")
            # 补码后仍无提取码：新版分享提取码必填，空码提交必然失败 —— 不再死胡同：
            # 弹/聚焦提取码小窗并保留可操作提示。零网络请求、绝不唤起客户端、也不
            # 累加 d7 拉起计数（否则之后真正成功的那次会被"重复拉起"误挡）。
            if not (rec.get("pwd") or "").strip():
                # D：收紧后仍拿不到「可确认与当前链接同源」的码 → 诚实放弃，绝不
                # 把来源不明的猜测码发给 /share/verify（errno=-9 的根因）。
                self._append_log(
                    "[分享] 缺少可确认的提取码，已放弃拉起（避免拉错链接）")
                _mapped = ""
                try:
                    _mapped = bm.mapped_code(rec.get("share_uk")) or ""
                except Exception:
                    _mapped = ""
                if _mapped:
                    self._append_log("[分享] 该分享者配有固定提取码，需用「固定提取码」手势")
                    self._share_notify(
                        "分享缺少提取码",
                        f"{rec.get('url')}\n该分享者配有固定提取码：请用「固定提取码」"
                        f"手势（托盘菜单 / Alt+3）下载。")
                    return
                if _share_parent_usable(self):
                    self._append_log(
                        "[分享] 该分享缺少提取码，已跳过拉起（请在右侧小窗填写提取码，"
                        "或按 Alt+2 用临时码）")
                    self._share_notify(
                        "分享缺少提取码",
                        f"{rec.get('url')}\n该分享需要提取码：请在右侧小窗填写提取码"
                        f"（或按 Alt+2 用临时码）。")
                else:
                    # 主窗不可用：小窗弹不出来 —— 提示改为诚实文案（唯一发声点）
                    _announce_ask_code_hidden(self, rec.get("url"))
                _show_share_code_window(
                    self, rec.get("surl") or rec.get("url"), rec.get("url"),
                    rec.get("share_uk"), mapped_code=_mapped)
                return
            # 手势成功拉到：清掉可能残留的预定任务，避免事后重复拉起。
            self._pending_share_gesture = None
            _surl = rec.get("surl") or rec.get("url")
            # 手势时间戳：管线随后再报到同一链接时静默去重（见 _drain 自动分支）。
            _mark_gesture_launch(self, _surl)
            # 手动路径即用户明确同意：直接拉起，同时计数（与自动路径共用 d7 计数）
            self._bump_share_launch(_surl)
            self._start_share_invoke(rec.get("url"), rec.get("pwd") or "", manual=True)
        except Exception as e:
            self._append_log(f"拉起客户端出错: {e}")

    def _on_share_hotkey(self):
        """全局热键：用客户端下载最近分享（等价托盘菜单那项）。"""
        self._open_recent_share()

    def _open_recent_share_with_code(self):
        """托盘动作/热键：用固定/小窗提取码下载最近分享（d3 显式手势，成功才绑定）。

        与「用客户端打开最近分享」并列，但走新的「准备 →（可选）挑选 → 提交」管线；
        按压时同样先就地读剪贴板（A 快路径）：命中分享链接即按该链接取值，否则在
        有输入解析在途时登记意图等解析完成，最后才回退最近记录。
        取值顺序：小窗码 > 文本自带码/权威记录码（收紧历史）> 分享者固定映射。
        仍无码时**不再死胡同**：弹/聚焦提取码小窗，等用户填完走「本次使用」或直接下载。
        本次携带 `bind_code_uk`，由 worker 在**提交成功后**才把码写入固定映射。"""
        try:
            # 整条 2.F 属实验性功能：全局热键也可能被按下，这里必须再校验一次。
            if not self.state.snapshot().get("experimental_enabled"):
                self._append_log("实验性功能未开启，「用固定提取码下载分享」不可用")
                return
            # A. 按压时刻的剪贴板：命中分享链接就从该文本就地解析（零等待）。
            _fresh = _clipboard_share_target(self)
            if _fresh:
                self._launch_clipboard_share_code(*_fresh)
                return
            # B. 有分享输入正在解析 → 记住手势（kind=share_code，绝不与 Alt+2 混用）。
            if (not getattr(self, "_share_dispatch_pending", False)
                    and _share_input_inflight(self)):
                if self._register_pending_share("share_code", inflight=True):
                    return
            from .. import baidu_task as bt
            rec = bt.last_share()
            if not rec:
                # 手势落空但二维码正在解码中 → 共享助手登记预定任务（Alt+3=share_code）；
                # 返回 False（不在解码期/实验性关）时保持原提示逐字不变。
                if self._register_pending_share("share_code"):
                    return
                self._append_log("还没有记录到百度分享链接（复制一下分享链接即可）")
                return
            self._append_log(
                f"[分享] 剪贴板无分享链接，沿用最近记录: {rec.get('url')}")
            # 失效短路：固定提取码也救不活失效链接 → 零网络请求、绝不唤起客户端，
            # 日志 + 托盘通知并说明固定提取码也无效。
            try:
                dead = bm.share_dead(rec.get("surl") or rec.get("url"))
            except Exception:
                dead = ""
            if dead:
                self._append_log(
                    f"[分享] 该分享链接已失效，固定提取码也无法下载，已跳过：{dead}")
                self._share_notify(
                    "分享链接已失效",
                    f"{rec.get('url')}\n{dead}（固定提取码也无法下载）")
                return
            # 取值顺序（冻结）：小窗码 > 取值助手 > 该分享者固定映射码。
            code, _src = _effective_share_code(
                self, rec.get("surl") or rec.get("url"),
                rec.get("share_uk"), rec.get("pwd"), rec.get("code_source"))
            if not code:
                try:
                    code = bm.mapped_code(rec.get("share_uk")) or ""
                except Exception:
                    code = ""
            if not code:
                # 无固定码也不再死胡同：弹/聚焦提取码小窗，给出可操作提示。
                # D：先诚实标注「没有可确认与当前链接同源的码」。
                self._append_log(
                    "[分享] 缺少可确认的提取码，已放弃拉起（避免拉错链接）")
                _mapped = ""
                try:
                    _mapped = bm.mapped_code(rec.get("share_uk")) or ""
                except Exception:
                    _mapped = ""
                if _share_parent_usable(self):
                    self._append_log(
                        "[分享] 该分享者未配置固定提取码，该分享需要提取码："
                        "请在右侧小窗填写（或按 Alt+2 用临时码）")
                    self._share_notify(
                        "分享缺少提取码",
                        f"{rec.get('url')}\n该分享需要提取码：请在右侧小窗填写提取码"
                        f"（或按 Alt+2 用临时码）。")
                else:
                    # 主窗不可用：小窗弹不出来 —— 提示改为诚实文案（唯一发声点）
                    _announce_ask_code_hidden(self, rec.get("url"))
                _show_share_code_window(
                    self, rec.get("surl") or rec.get("url"), rec.get("url"),
                    rec.get("share_uk"), mapped_code=_mapped)
                return
            # 手势成功拉到：清掉可能残留的预定任务，避免事后重复拉起（与 Alt+2 一致）。
            self._pending_share_gesture = None
            _mark_gesture_launch(self, rec.get("surl") or rec.get("url"))
            # 计数已移入 _start_share_pick：手动路径同样计入（明确同意）。
            # Alt+3 = 绑定并下载：携带 bind_code_uk，worker 提交成功后才写库。
            _call_start_share_pick(self, rec.get("url"), rec.get("surl"), code,
                                   manual=True,
                                   bind_code_uk=rec.get("share_uk"))
        except Exception as e:
            self._append_log(f"按固定提取码下载出错: {e}")

    def _on_share_code_hotkey(self):
        """全局热键：用固定提取码下载最近分享（等价托盘菜单那项）。"""
        self._open_recent_share_with_code()

    def _share_gate_blocked(self, manual, where=""):
        """后台线程预检：百度网盘客户端未在运行时，立即中断本次分享下载。

        只读检查 `baidu_share.client_ready()`（`tasklist` 进程检查约数百毫秒，**只能在
        后台线程**调用）；不碰任何 Qt 控件，日志走线程安全的 `self.hub.log`，托盘提醒
        走 `_share_notify`（hub 队列）。客户端未运行 → 记日志 + 提醒后返回 True，调用方
        据此直接结束，后续网络请求与弹窗一概不再发生。预检自身任何异常都按「放行」
        处理（返回 False），绝不因预检逻辑打断流程——提交时 `commit_download` 内
        仍有最终安全网兜底。"""
        try:
            from .. import baidu_share as bs
            if bs.client_ready():
                return False
        except Exception as e:
            tag = ("手动" if manual else "自动") + (f"·{where}" if where else "")
            self.hub.log(f"[分享] 客户端预检异常（{tag}），继续按原流程尝试: {e}")
            return False
        self.hub.log("[分享] 客户端未在运行，已中断本次分享下载（先打开百度网盘客户端再重试）")
        self._share_notify(
            "分享下载已中断",
            "百度网盘客户端未在运行，已中断本次分享下载：客户端未运行时拉起会进入"
            "未登录态、可能需重新登录。请先打开客户端后重试。")
        return True

    def _start_share_invoke(self, url, pwd, manual=False):
        """统一的分享拉起入口：忙则跳过，否则起后台 daemon 线程（不阻塞界面）。

        `invoke_download` 的复核轮询最多约 4.5s，但**绝不能**在 UI 线程调用。
        忙标志带看门狗：超过 SHARE_INVOKE_BUSY_MAX_SEC 仍未复位（worker 卡死）
        则强制复位并记一行，避免后续手势被永久挡住。"""
        if self._share_invoke_busy and _share_invoke_busy_stale(self):
            self._share_invoke_busy = False
            self._append_log(
                f"[分享] 上一个拉起已超时（>{SHARE_INVOKE_BUSY_MAX_SEC}s），已强制复位")
        if self._share_invoke_busy:
            self._append_log("[分享] 上一个拉起尚未结束，已跳过"
                             if not manual else "[分享] 上一个拉起尚未结束，请稍候")
            return
        self._share_invoke_busy = True
        try:
            self._share_invoke_started_ts = time.time()
        except Exception:
            pass
        # 看门狗提速：真实拉起已开始，让随后 30s 内即使空闲也按 active 间隔轮询，
        # 客户端接单的新任务能更快被读到（失败静默，绝不影响拉起本身）。位置在
        # 忙检查通过之后、线程启动之前：被忙标志跳过的请求不该提速，提速窗口也要
        # 覆盖整条链路的复核期。导入沿用文件内既有的 `from .. import …` 局部风格。
        try:
            from .. import baidu_watch
            baidu_watch.boost_interval(30)
        except Exception:
            pass
        threading.Thread(target=self._invoke_share_worker,
                         args=(url, pwd, manual), daemon=True).start()

    def _invoke_share_worker(self, url, pwd, manual):
        """后台线程：跑完整拉起链路（唤醒一发出即通知，复核轮询最多约 4.5s）。

        线程内**不碰 UI**：日志走线程安全的 `self.hub.log`，托盘提示走 `hub.q`
        队列（由 `_drain()` 在主线程消费）。异常一律吞掉，忙标志在 finally 复位。"""
        try:
            if self._share_gate_blocked(manual, where="拉起客户端"):
                return
            self.hub.log(f"[分享] {'手动' if manual else '自动'}拉起客户端下载…: {url}")
            from .. import baidu_task as bt
            from .. import baidu_share as bs
            # 唤醒即通知：链路一发出唤醒就告知「已请求客户端下载」，不等复核轮询。
            # 一次性守卫保证最多触发一次（on_wake 本就被链路只回调一次，这里再兜一层，
            # 避免将来链路变化导致重复通知）。
            fired = {"done": False}

            def on_wake(detail):
                if fired["done"]:
                    return
                fired["done"] = True
                try:
                    self.hub.log(f"[分享] 已请求客户端下载（{detail}）")
                    self.hub.q.put({"type": "notify", "title": "用客户端下载分享",
                                    "msg": str(url)})
                except Exception:
                    pass

            ok, detail = bt.invoke_download(url, pwd=pwd, on_wake=on_wake)
            if ok:
                # 通知已在唤醒那一刻发出，这里只补一行链路完成日志（不再重复通知）。
                self.hub.log(f"[分享] 链路完成: {detail}")
                # 已确认唤起客户端：该分享的缺码小窗任务完成，转交 Qt 线程关闭。
                _notify_share_used(self, url=url)
            else:
                self.hub.log(f"[分享] 拉起失败: {detail}")
                # 失效判定优先：标记 + 「已失效」通知（用户手势后不该一片安静）。
                try:
                    dead = bm.is_dead_share_reason(detail)
                except Exception:
                    dead = False
                if dead:
                    try:
                        bm.mark_share_dead(url, detail)
                    except Exception:
                        pass
                    self._share_notify("分享链接已失效", f"{url}\n{detail}")
                else:
                    # 失败分流顺序：唤醒后校验失败（对已发通知的纠正）→ 缺码（空码
                    # 闸门）→ 手动失败兜底。两个判定助手都在 baidu_share（bt 未转发），
                    # 故直接引用本地已导入的 bs；异常一律按「否」处理。
                    try:
                        check_failed = str(detail or "").startswith(bs.CHECK_FAIL_PREFIX)
                    except Exception:
                        check_failed = False
                    try:
                        need_code = bs.is_need_code_reason(detail)
                    except Exception:
                        need_code = False
                    if check_failed:
                        # 唤醒已发出、通知已发，但客户端未确认接单：补一条纠正通知。
                        # 无论手动/自动都发——它是对已发通知的纠正，不是新的噪音。
                        self._share_notify(
                            "拉起后校验失败",
                            f"{url}\n{detail}\n（客户端未确认接单，可在看板确认）")
                    elif need_code:
                        # 空码闸门：不是"拉起失败"，而是"缺提取码"——给可操作的提示。
                        self._share_notify(
                            "分享缺少提取码",
                            f"{url}\n该分享需要提取码：把「链接 + 提取码」整段复制到剪贴板后"
                            f"再试，或用「固定提取码」手势（托盘菜单 / Alt+3）。")
                    elif manual:
                        # 用户明确按了手势却毫无反馈是最糟的体验：手动失败必须通知。
                        # 自动路径（manual=False）保持只有日志，避免噪音。
                        self._share_notify("分享拉起失败", f"{url}\n{detail}")
        except Exception as e:
            self.hub.log(f"[分享] 拉取出错: {e}")
        finally:
            self._share_invoke_busy = False

    # ---------- d3：分享「准备 →（可选）挑选 → 提交」管线 ----------
    def _start_share_pick(self, url, surl, pwd, manual=False, item=None,
                          bind_code_uk=None):
        """统一的分享管线入口：忙则跳过，否则起后台 daemon 线程（不阻塞界面）。

        `prepare_share` / `commit_download` / `list_share_dir` 均含网络 IO，**绝不能**
        在 UI 线程调用。与旧的 `_start_share_invoke` 共用同一个忙标志：同一时刻只允许
        一个分享管线。`item` 仅用于空提取码探测：确认需要提取码时把它回传给 Qt 线程弹
        询问面板（不传则询问面板按未知分享者处理）。

        `bind_code_uk` 非空表示本次是「绑定并下载」（Alt+3 / 小窗 mapped 按钮）：
        由 worker 在**提交成功后**把 `(share_uk, pwd)` 写入固定提取码映射。

        d7 计数在**忙检查通过之后**才计入（被忙标志跳过的启动不计），询问路径最终也
        汇入本入口，故同样计入一次。
        忙标志带看门狗：超过 SHARE_INVOKE_BUSY_MAX_SEC 仍未复位（worker 卡死）
        则强制复位并记一行，避免后续手势被永久挡住。"""
        if self._share_invoke_busy and _share_invoke_busy_stale(self):
            self._share_invoke_busy = False
            self._append_log(
                f"[分享] 上一个拉起已超时（>{SHARE_INVOKE_BUSY_MAX_SEC}s），已强制复位")
        if self._share_invoke_busy:
            self._append_log("[分享] 上一个拉起尚未结束，已跳过"
                             if not manual else "[分享] 上一个拉起尚未结束，请稍候")
            return
        # 小窗统一取值：属于该分享的小窗里若有校验通过的码，覆盖本次 pwd（覆盖
        # Alt+3 / 询问 / 自动三条路）。此处**只**读小窗，不重算 recent——自动路径的
        # 码由调用方决定，绝不在此改口径。
        try:
            _w_uk = ""
            try:
                _w_uk = (item or {}).get("share_uk") or ""
            except Exception:
                _w_uk = ""
            # Alt 路径 item=None：与 `_effective_share_code`（用 rec.share_uk）同口径，
            # 补取记录里的 share_uk，让「同 surl **或** 同 share_uk 即视为同窗」全部
            # 由 `_share_code_in_window` 一处判定，不再出现两套归属判据。
            if not _w_uk:
                try:
                    from .. import baidu_task as _bt
                    _w_uk = (_bt.last_share() or {}).get("share_uk") or ""
                except Exception:
                    _w_uk = ""
            _wcode, _ = _share_code_in_window(self, surl, _w_uk)
            if _wcode:
                pwd = _wcode
        except Exception:
            pass
        # 一次性提醒（本进程内只发一次；手动路径绝不提醒）：自动链路不携带浏览器
        # 登录态，客户端未运行时被唤起会进入未登录状态、可能被迫重新登录。
        if not manual and not self._share_nologin_warned:
            self._share_nologin_warned = True
            self.hub.log(
                "[分享] 实验性自动拉起不携带登录态：若客户端未运行可能需重新登录")
            try:
                self.hub.q.put({
                    "type": "notify",
                    "title": "实验性自动拉起",
                    "msg": "实验性自动拉起不携带登录态：若客户端未运行可能需重新登录"})
            except Exception:
                pass
        self._share_invoke_busy = True
        try:
            self._share_invoke_started_ts = time.time()
        except Exception:
            pass
        self._bump_share_launch(surl)
        threading.Thread(target=self._pick_share_worker,
                         args=(url, surl, pwd, manual, item, bind_code_uk),
                         daemon=True).start()

    def _pick_share_worker(self, url, surl, pwd, manual, item=None,
                           bind_code_uk=None):
        """后台线程：跑「准备 →（可选）挑选 → 提交」管线（网络 IO，绝不阻塞界面）。

        线程内**不碰 UI**：日志走线程安全的 `self.hub.log`，托盘提示与挑选窗/询问窗
        请求都走 `hub.q`（由 `_drain()` 在 Qt 线程消费/构造）。需要挑选时本线程阻塞
        等待用户在 Qt 线程做出的选择。空提取码时先探测该分享是否根本不需要提取码，
        确需提取码才把「询问」请求投回 Qt 线程。异常一律吞掉，忙标志在 finally 复位。"""
        try:
            if self._share_gate_blocked(manual, where="准备管线"):
                return
            from .. import baidu_share as bs
            # 唤醒即通知：与 `_invoke_share_worker` 同一策略——链路一发出唤醒就
            # 告知「已请求客户端下载」，不等复核轮询。通知标题/正文与原先成功分支
            # 保持逐字一致，只把时机提前；一次性守卫保证最多触发一次。
            fired = {"done": False}

            def on_wake(detail):
                if fired["done"]:
                    return
                fired["done"] = True
                try:
                    self.hub.log(f"[分享] 提交成功: 已请求客户端下载（{detail}）")
                    self.hub.q.put({"type": "notify", "title": "分享下载",
                                    "msg": str(url)})
                    # 提交成功（已请求客户端下载）：该分享的小窗任务完成。
                    _notify_share_used(self, surl=surl, uk=bind_code_uk, url=url)
                except Exception:
                    pass

            if not pwd:
                # 空提取码：先在后台线程探测是否根本不需要提取码（网络 IO）。
                self.hub.log(f"[分享] 自动探测分享是否需要提取码…: {url}")
                ok, prep = bs.prepare_share(url, "")
                if not ok:
                    # 失效分流：探测失败若是「链接已失效」，问提取码毫无意义 →
                    # 标记 + 通知 + 直接结束，绝不投递 share_ask 去询问提取码。
                    try:
                        dead = bm.is_dead_share_reason(prep)
                    except Exception:
                        dead = False
                    if dead:
                        self.hub.log(f"[分享] {prep}")
                        try:
                            bm.mark_share_dead(surl, prep)
                        except Exception:
                            pass      # 标记失败绝不影响本次分流
                        self._share_notify("分享链接已失效", f"{url}\n{prep}")
                        return
                    self.hub.log("[分享] 该分享需要提取码，改为询问用户")
                    try:
                        self.hub.q.put({"type": "share_ask", "item": item or {},
                                        "url": url, "surl": surl})
                    except Exception as e:
                        self.hub.log(f"[分享] 无法请求提取码询问: {e}")
                    return
                self.hub.log("[分享] 该分享无需提取码，继续下载")
            else:
                self.hub.log(f"[分享] {'手动' if manual else '自动'}准备分享下载…: {url}")
                ok, prep = bs.prepare_share(url, pwd)
                if not ok:
                    # 失效链接即便带了提取码也失败：标记后以「分享链接已失效」通知，
                    # 与普通「准备失败」区分开（用户一眼知道不是码的问题）。
                    try:
                        dead = bm.is_dead_share_reason(prep)
                    except Exception:
                        dead = False
                    if dead:
                        self.hub.log(f"[分享] {prep}")
                        try:
                            bm.mark_share_dead(surl, prep)
                        except Exception:
                            pass
                        self._share_notify("分享链接已失效", f"{url}\n{prep}")
                        return
                    self.hub.log(f"[分享] 准备失败: {prep}")
                    self._share_notify("分享下载失败", f"{url}\n{prep}")
                    return
            prep_ts = time.time()
            if self._share_pick_wanted(prep.get("share_uk")):
                # 需要挑选：把 prep 交给 Qt 线程弹挑选窗，阻塞等待用户选择。
                req = {"type": "share_pick", "prep": prep, "url": url,
                       "pwd": pwd, "surl": surl, "ts": prep_ts,
                       "event": threading.Event(), "pairs": None,
                       "cancelled": True, "answered": False}
                self.hub.log("[分享] 该分享者需要挑选文件，等待用户选择…")
                try:
                    self.hub.q.put(req)
                except Exception as e:
                    self.hub.log(f"[分享] 无法请求挑选窗: {e}")
                    return
                req["event"].wait(SHARE_PICK_WAIT_SEC)
                if req.get("cancelled"):
                    self.hub.log("[分享] 未选择任何文件，已取消本次下载")
                    return
                pairs = req.get("pairs") or []
                # 陈旧检测：挑选可能耗时很久，sekey 寿命未知 → 提交前重新 prepare，
                # 并按 fs_id 把选择重映射到新 prep 的条目上。
                try:
                    if time.time() - prep_ts > SHARE_PREP_STALE_SEC:
                        self.hub.log("[分享] 挑选耗时较长，提交前重新准备分享…")
                        ok2, prep = bs.prepare_share(url, pwd)
                        if not ok2:
                            self.hub.log(f"[分享] 重新准备失败: {prep}")
                            self._share_notify("分享下载失败", f"{url}\n{prep}")
                            return
                        pairs = self._remap_share_pairs(pairs, prep)
                        if not pairs:
                            self.hub.log(
                                "[分享] 重新准备后已选文件全部失效，已取消本次下载")
                            self._share_notify(
                                "分享下载失败",
                                f"{url}\n重新准备后已选文件全部失效，已取消")
                            return
                except Exception as e:
                    self.hub.log(f"[分享] 重新准备出错: {e}")
                    return
                ok3, detail = bs.commit_download(prep, pairs=pairs, on_wake=on_wake)
            else:
                ok3, detail = bs.commit_download(prep, None, on_wake=on_wake)
            if ok3:
                # 通知已在唤醒那一刻发出，这里只补一行链路完成日志（不再重复通知）；
                # 子集选择相关的既有日志信息一律保留。
                self.hub.log(f"[分享] 链路完成: {detail}")
                # 确认完成：关闭同分享小窗（on_wake 已投过一次，重复投递为幂等空操作）。
                _notify_share_used(self, surl=surl, uk=bind_code_uk, url=url)
                # 绑定只在**提交成功后**落库：失败（ok3 假）只本次生效，绝不写库。
                # `_persist_share_code` 内的诊断日志已做线程安全分流（worker 线程走
                # hub 队列，见 `_persist_log`），故可安全在 worker 中调用。
                if bind_code_uk:
                    self._persist_share_code(bind_code_uk, pwd)
            else:
                self.hub.log(f"[分享] 提交失败: {detail}")
                self._share_notify("分享下载失败", f"{url}\n{detail}")
        except Exception as e:
            self.hub.log(f"[分享] 准备出错: {e}")
        finally:
            self._share_invoke_busy = False

    def _share_pick_flag(self, share_uk):
        """读该分享者的「需要挑选」标记：AppState 优先，其次 db；不可用按 0（整包）。

        `find_share_entry` 是并行新增接口，缺失（未落地 / 旧实现 / 查询失败）时一律
        按「不需要挑选」处理，绝不因此中断下载。"""
        try:
            ent = None
            state = getattr(self, "state", None)
            if state is not None and hasattr(state, "find_share_entry"):
                ent = state.find_share_entry(share_uk)
            if ent is None:
                from .. import db as _db
                ent = _db.find_share_entry(share_uk)
            if isinstance(ent, dict):
                return ent.get("pick")
        except Exception:
            pass
        return 0

    def _share_pick_wanted(self, share_uk):
        """是否需要弹文件挑选窗：分享者标记 pick，或全局「分享前总是挑选」开关。

        全局开关 `baidu_pick_before_download`（默认关，实验性）读取失败/缺失一律按
        关闭处理，绝不因此中断下载。返回布尔值。"""
        try:
            if self.state.snapshot().get("baidu_pick_before_download", False):
                return True
        except Exception:
            pass
        return bool(self._share_pick_flag(share_uk))

    def _share_notify(self, title, msg):
        """托盘气泡（线程安全）：只向 hub 队列投递，由 `_drain()` 在 Qt 线程消费。"""
        try:
            self.hub.q.put({"type": "notify", "title": title, "msg": msg})
        except Exception:
            pass

    def _show_share_pick(self, req):
        """Qt 线程：按 worker 准备好的 prep 弹出文件挑选窗（惰性导入）。

        `on_expand` 交给挑选窗按需调用（由挑选窗自行后台执行），`on_commit` 回传用户
        选择并唤醒等待中的 worker；真正的提交仍在 worker 线程完成。隐藏到托盘时绝不
        弹窗（非置顶窗弹在托盘里没有意义，且会把 worker 卡满等待），直接按取消唤醒并
        投递一条托盘提示。挑选组件缺失时退化为整包提交，不让用户干等、也不改变既有
        行为。`finished`（接受/取消/×/Esc 都会触发）兜底唤醒，避免用户关闭挑选窗后
        worker 一直阻塞。"""
        if not self.isVisible():
            self._abort_share_pick(req, "需要你选择文件：请打开主界面后重试")
            self._share_notify("分享需要选择文件",
                               "该分享需要你选择文件，请打开主界面后重试。\n"
                               + str(req.get("url") or ""))
            return
        try:
            from .. import baidu_share as bs
            from .share_files import ShareFilesDialog
        except Exception:
            self.hub.log("[分享] 缺少文件挑选组件，改为整包提交")
            req["pairs"] = None
            req["cancelled"] = False
            self._wake_share_pick(req)
            return
        prep = req.get("prep") or {}
        entries = prep.get("entries") or []
        try:
            dlg = ShareFilesDialog(
                self, entries,
                (lambda path: bs.list_share_dir(prep, path)),
                (lambda pairs: self._on_share_pick_commit(req, pairs)),
                title="选择要下载的文件",
                subtitle=req.get("url") or "")
        except TypeError:
            # 兼容旧签名（无 title/subtitle 关键字）
            try:
                dlg = ShareFilesDialog(
                    self, entries,
                    (lambda path: bs.list_share_dir(prep, path)),
                    (lambda pairs: self._on_share_pick_commit(req, pairs)))
            except Exception as e:
                self._abort_share_pick(req, f"挑选窗创建失败: {e}")
                return
        except Exception as e:
            self._abort_share_pick(req, f"挑选窗创建失败: {e}")
            return
        self._share_pick_dlg = dlg
        try:
            # QDialog.finished 覆盖「下载选中 / 取消 / × / Esc」所有关闭方式；
            # 未提交就关闭时按取消唤醒 worker，已提交时只做收尾、不覆盖选择。
            dlg.finished.connect(lambda _code, r=req: self._on_share_pick_closed(r))
        except Exception:
            pass
        try:
            dlg.show()
        except Exception:
            self._share_pick_dlg = None
            self._abort_share_pick(req, "挑选窗无法显示")

    def _on_share_pick_commit(self, req, pairs):
        """挑选窗提交回调（Qt 线程）：记录选择并唤醒 worker（提交仍在 worker 线程）。"""
        self._share_pick_dlg = None
        try:
            req["answered"] = True
            req["pairs"] = list(pairs or [])
            req["cancelled"] = not req["pairs"]
        except Exception:
            req["answered"] = True
            req["pairs"] = None
            req["cancelled"] = True
        self._wake_share_pick(req)

    def _abort_share_pick(self, req, reason):
        """挑选窗不可用：记一行日志并唤醒 worker 按「取消」处理。"""
        try:
            self.hub.log(f"[分享] {reason}")
        except Exception:
            pass
        try:
            req["answered"] = True
        except Exception:
            pass
        req["pairs"] = None
        req["cancelled"] = True
        self._wake_share_pick(req)

    def _wake_share_pick(self, req):
        """唤醒等待挑选结果的 worker（只对 threading.Event 置位，异常一律吞掉）。"""
        ev = req.get("event")
        if ev is not None:
            try:
                ev.set()
            except Exception:
                pass

    def _on_share_pick_closed(self, req):
        """挑选窗关闭（QDialog.finished）：未提交时按取消唤醒 worker。

        finished 覆盖「下载选中 / 取消 / × / Esc」全部关闭方式。用户未点「下载选中」
        就关闭时 worker 仍在等待，必须按取消唤醒，否则会把忙标志占满整个等待窗口；
        已提交（req["answered"]，由提交回调置位并已清空面板指针）时只返回，绝不覆盖
        已提交的选择、也不在真实提交之后再动面板指针。"""
        try:
            if req.get("answered"):
                return
            self._share_pick_dlg = None
            self._abort_share_pick(req, "挑选窗已关闭")
        except Exception:
            pass

    def _remap_share_pairs(self, pairs, prep):
        """把挑选结果 (fs_id, path) 按 fs_id 重映射到新 prep 的根清单条目上。

        重新 prepare 后根层条目的 path 可能变化；根清单里查不到 fs_id 的选择
        （含无法核实是否仍存在的嵌套条目）一律**丢弃**并计数，避免提交已消失的
        条目。全部被丢弃时返回空列表，由调用方中止本次下载。记一行日志说明丢弃
        了多少个。"""
        try:
            index = {}
            for e in (prep.get("entries") or []):
                if isinstance(e, dict) and e.get("fs_id") is not None:
                    index[str(e.get("fs_id"))] = e.get("path")
            out = []
            dropped = 0
            for it in (pairs or []):
                try:
                    fid, pth = it[0], it[1]
                except Exception:
                    dropped += 1
                    continue
                new_path = index.get(str(fid))
                if new_path:
                    out.append((fid, new_path))
                else:
                    dropped += 1
            if dropped:
                self.hub.log(f"[分享] 重新准备后有 {dropped} 个已选条目未在根清单中，"
                             f"已丢弃")
            return out
        except Exception:
            return pairs

    # ---------- d3：空提取码处理（弹/聚焦提取码小窗，绝不自动套用固定码） ----------
    def _share_ask_code(self, item, url, surl):
        """空提取码处理（Qt 线程）：弹/聚焦顶层提取码小窗（主窗隐藏时同样弹）。

        未知分享者（has_map=False）额外用浏览器打开分享页做「探针」，方便用户查看；
        有固定映射（has_map=True）只弹窗、不开浏览器。已在弹小窗（或复用）时不再
        重复开浏览器。主窗隐藏在托盘时额外补一条托盘气泡：新小窗会弹出来，气泡仅
        作补充提醒。任何异常都不向外抛。"""
        try:
            share_uk = item.get("share_uk")
            has_map = bool(item.get("has_map"))
            mapped = None
            try:
                mapped = bm.mapped_code(share_uk)
            except Exception:
                mapped = None
            visible = True
            try:
                visible = bool(self.isVisible())
            except Exception:
                visible = True
            if not visible:
                # 小窗弹不出来：以本处为唯一发声点（诚实文案：打开主界面后才能弹，
                # 不再谎称「已在提取码小窗等待填写」）；兜底提示看到标记会静默。
                _announce_ask_code_hidden(self, url)
            _show_share_code_window(self, surl, url, share_uk,
                                    mapped_code=mapped, open_browser=not has_map)
        except Exception as e:
            self._append_log(f"[分享] 处理缺少提取码的分享出错: {e}")

    def _on_share_code_decision(self, kind, code, url, surl, share_uk):
        """提取码询问回调（Qt 线程）：mapped=成功后才绑定；once=只本次；ignore=忽略。"""
        try:
            self._share_ask_dlg = None
            if kind == "ignore" or not code:
                self._append_log(f"[分享] 已忽略缺少提取码的分享: {surl}")
                return
            if kind == "mapped" and not str(share_uk or "").strip():
                # 无法绑定到具体分享者：立即如实说明「仅本次生效」，不再假装成功。
                self._append_log(
                    "[分享] 未识别到分享者，固定提取码未能保存，仅本次生效")
            # mapped：绑定交给 worker，**提交成功后**才写库（与 Alt+3 同源）；
            # once：绝不写库。
            # manual=True：本回调只由小窗/询问面板按钮触发，用户明确点击 = 手动，
            # 绝不吃「实验性自动拉起不携带登录态」托盘提示（与 Alt+2/Alt+3 一致）。
            bind_uk = share_uk if kind == "mapped" else None
            _call_start_share_pick(self, url, surl, code, manual=True,
                                   bind_code_uk=bind_uk)
        except Exception as e:
            self._append_log(f"[分享] 处理提取码选择出错: {e}")

    def _effective_share_code(self, surl, share_uk, rec_pwd, code_source):
        """正式取值助手（module 级 `_effective_share_code` 的薄包装，见其文档）。

        优先级冻结：小窗码 > 权威记录码 > 重新取 120s 内最近码 > 空。"""
        return _effective_share_code(self, surl, share_uk, rec_pwd, code_source)

    def _show_share_code_window(self, surl, url, share_uk, mapped_code=""):
        """正式缺码小窗入口（module 级 `_show_share_code_window` 的薄包装）。

        同一时刻只允许一个小窗：同分享复用并预填、异分享关旧换新；主窗隐藏也弹。"""
        return _show_share_code_window(self, surl, url, share_uk,
                                       mapped_code=mapped_code)

    def _persist_share_code(self, share_uk, code):
        """把用户选择的固定提取码写入映射（pick=0）；旧签名不支持 pick 时退回 3 参。

        由 `state.add_share_code`（内部走 db，带锁，线程安全）落库；诊断日志经
        `_persist_log` 分流，故本方法在 worker 线程调用也安全。持久化可能因未识别
        到分享者（share_uk 为空）等原因返回假值；此时明确告知用户未能保存，绝不
        谎报「已保存」。"""
        note = "自动加入(d3面板)"
        try:
            saved = self.state.add_share_code(share_uk, code, note, 0)
        except TypeError:
            try:
                saved = self.state.add_share_code(share_uk, code, note)
            except Exception as e:
                _persist_log(self, f"[分享] 保存固定提取码失败: {e}")
                return
        except Exception as e:
            _persist_log(self, f"[分享] 保存固定提取码失败: {e}")
            return
        if not saved:
            _persist_log(self, "[分享] 未识别到分享者，固定提取码未能保存，仅本次生效")
        else:
            _persist_log(self, "[分享] 已把提取码绑定到该分享者")

    # ---------- 分享重复拉起（d7）：同一 surl 本次运行再次拉起需用户同意 ----------
    def _share_needs_consent(self, surl):
        """该分享本次运行是否已拉起过？是 → 再次自动拉起前必须征得用户同意。

        计数优先读 baidu_manifest 的进程内计数器（跨模块可见、重启即忘）；
        计数器不可用时退回本对象的进程内集合（口径一致：只在首见自动）。"""
        key = str(surl or "")
        try:
            return int(bm.share_launch_count(key)) > 0
        except Exception:
            return key in getattr(self, "_share_launched_surls", ())

    def _bump_share_launch(self, surl):
        """记录「该分享已被拉起一次」。仅在拉起真正开始时调用（自动/同意/手动）。

        优先写 baidu_manifest 计数器；写失败不影响拉起本身，进程内集合
        始终同步更新，作为计数器不可用时的兜底。"""
        key = str(surl or "")
        try:
            bm.bump_share_launch(key)
        except Exception:
            pass
        try:
            self._share_launched_surls.add(key)
        except Exception:
            pass

    def _confirm_share_reinvoke(self, surl, url, pwd):
        """同一分享本次运行重复出现：先征得同意，用户点「是」才再次拉起（d7）。

        主界面可见：弹模态确认（默认「否」）；隐藏到托盘时不弹任何窗口
        （模态框不能凭空出现在托盘里），改为记日志 + 托盘气泡走队列提醒，
        等用户主动打开主界面后自行走托盘菜单/热键。两条路径都不自动拉起。"""
        if self.isVisible():
            ret = QMessageBox.question(
                self, "重复的分享链接",
                f"本次运行已拉起过该分享（{surl}），是否再次用客户端下载？",
                QMessageBox.Yes | QMessageBox.No, QMessageBox.No)
            if ret == QMessageBox.Yes:
                # 计数已移入 _start_share_pick：同意后同样计入
                self._start_share_pick(url, surl, pwd, manual=False)
            else:
                self._append_log(f"[分享] 用户取消了重复拉起: {surl}")
            return
        self._append_log(f"[分享] 该分享本次运行已拉起过，需确认后才会再次拉起: {surl}")
        try:
            self.hub.q.put({"type": "notify", "title": "重复的分享链接",
                            "msg": f"本次运行已拉起过该分享（{surl}），"
                                   f"如需再次下载请打开主界面确认。"})
        except Exception:
            pass

    # ---------- 网址信任：挂起队列 / 非置顶询问弹窗 / 决策回写 ----------
    def _handle_trust_ask(self, req):
        """主窗口收到待确认网址：可见则弹非置顶询问窗，隐藏则挂起+托盘提示。"""
        if self.isVisible():
            if not self._show_trust_dialog(req):
                self._pending_trust.append(req)   # 已有弹窗打开，排队等下一个
        else:
            self._pending_trust.append(req)
            snap = self.state.snapshot()
            if (snap.get("notify_enabled", True) and hasattr(self, "tray")
                    and snap.get("notify_trust_pending", True)):
                try:
                    self.tray.showMessage(
                        "网址信任确认", "有新的网址等待确认，打开主界面后处理。",
                        QSystemTrayIcon.Information, 3000)
                except Exception:
                    pass

    def _show_trust_dialog(self, req):
        """弹非置顶询问窗（不 raise/activate，不打断用户当前操作）。
        已有弹窗打开时返回 False（请求留在挂起队列）。"""
        if getattr(self, "_trust_dlg", None) is not None:
            return False
        dlg = TrustAskDialog(self, req.get("url", ""), req.get("host"),
                             req.get("category"), req.get("purpose", "open"))
        dlg.on_decision = lambda dec, r=req: self._on_trust_decision(r, dec)
        self._trust_dlg = dlg
        dlg.show()
        return True

    def _process_pending_trust(self):
        """主界面变为可见时处理挂起的信任询问（无限挂起，不丢请求）。"""
        while self._pending_trust and getattr(self, "_trust_dlg", None) is None:
            req = self._pending_trust.pop(0)
            if not self._show_trust_dialog(req):
                self._pending_trust.insert(0, req)
                break

    def _on_trust_decision(self, req, decision):
        """用户对信任询问做出选择：持久化黑白名单 + 放行或跳过。"""
        self._trust_dlg = None
        host = req.get("host") or ""
        purpose = req.get("purpose", "open") or "open"
        if decision in ("trust", "block") and host:
            try:
                key = "whitelist" if decision == "trust" else "blacklist"
                ut = add_trust_entry(self.state.snapshot(), host, key, purpose)
                self.state.set("url_trust", ut)
                kind = "已永久信任" if decision == "trust" else "已永久拒绝"
                label = "自动打开" if purpose == "open" else "下载识别"
                self.hub.log(f"{kind}[{label}]域名: {host}")
            except Exception as e:
                self.hub.log(f"信任名单保存失败: {e}")
        if decision in ("open_once", "trust"):
            # 放行：投递给 QRMonitor 执行（避免在 UI 线程做网络操作）
            try:
                self.hub.url_grant_q.put((req.get("url", ""), req.get("purpose", "open")))
            except Exception:
                pass
        self._process_pending_trust()

    # ---------- 解压暂停（唯一的总开关，涵盖原「停止监听」） ----------
    def _toggle_pause(self):
        if self.pauser is None:
            return
        pausing = not self.pauser.is_paused()
        self.pauser.set_paused(pausing)
        if pausing:
            self.pause_btn.setText("继续")
            self.pause_btn.setProperty("paused", True)
            self.hub.log("已暂停：停止监听与剪贴板监控，正在解压的任务已挂起")
        else:
            self.pause_btn.setText("暂停")
            self.pause_btn.setProperty("paused", False)
            self.hub.log("已恢复：继续监听与解压")
        self.pause_btn.style().unpolish(self.pause_btn)
        self.pause_btn.style().polish(self.pause_btn)
        # 底栏运行状态与暂停按钮同步（灯色/文案）
        try:
            self.statusbar.set_paused(pausing)
        except Exception:
            pass

    # ---------- 全局快捷键 ----------
    def _register_hotkey(self, _retry=0):
        if not isinstance(_retry, int):
            _retry = 0
        self._unregister_hotkey()
        self._hotkey_ok["main"] = None   # 本轮结果未知：未配置/未启用都按「不适用」处理
        if win32gui is None:
            return
        try:
            if not self.state.snapshot().get("hotkey_enabled", True):
                return
            combo = str(self.state.snapshot().get("hotkey", "")).strip()
            if not combo or combo.lower() in ("无", "none", "null"):
                return
            parsed = parse_hotkey(combo)
            if parsed is None:
                self.hub.log(f"快捷键配置无效，未注册: {combo}")
                self._hotkey_ok["main"] = False
                return
            mods, vk = parsed
            hwnd = int(self.winId())
            if not hwnd:
                self._hotkey_ok["main"] = False
                return
            # pywin32 的 RegisterHotKey 成功时返回 None（不是 True），
            # 所以不依赖返回值：没有抛异常即注册成功。
            win32gui.RegisterHotKey(hwnd, HOTKEY_ID, mods | MOD_NOREPEAT, vk)
            self._hotkey_ok["main"] = True
            self.hub.log(f"全局快捷键已注册: {combo}")
        except Exception as e:
            self._hotkey_ok["main"] = False
            # 启动瞬间偶发失败（例如窗口句柄尚未就绪，error 1400 "无效的窗口句柄"）。
            # 稍后重试：最多 3 次，避免「偶发注册不上」让热键长期失效。
            if _retry < 3:
                self.hub.log(f"全局快捷键注册失败，稍后重试({_retry + 1}/3): {e}")
                QTimer.singleShot(1200, lambda: self._register_hotkey(_retry + 1))
            else:
                self.hub.log(f"全局快捷键注册失败: {e}")
        finally:
            # 主热键无论走哪条分支（含上面的提前 return），都顺带注册两个分享热键
            self._register_share_hotkey()
            self._register_share_code_hotkey()
            # 底栏「快捷键 <config>」与播报末句按**真实注册结果**显示：
            # 注册失败追加「（未生效）」，成功/未尝试保持原配置值文案。
            self._sync_hotkey_display()

    def _register_share_hotkey(self):
        """注册「用客户端下载最近分享」的全局热键（可选，默认不设置）。

        与主热键不同：分享热键是可选功能，注册失败不重试、不打扰；未启用
        全局热键、未配置（空 / 无 / none / null）时静默跳过。异常一律吞掉。"""
        if win32gui is None:
            return
        self._hotkey_ok["share"] = None
        try:
            if not self.state.snapshot().get("hotkey_enabled", True):
                return
            combo = str(self.state.snapshot().get("hotkey_share", "")).strip()
            if not combo or combo.lower() in ("无", "none", "null"):
                return
            parsed = parse_hotkey(combo)
            if parsed is None:
                self.hub.log(f"分享快捷键配置无效，未注册: {combo}")
                self._hotkey_ok["share"] = False
                return
            mods, vk = parsed
            hwnd = int(self.winId())
            if not hwnd:
                self._hotkey_ok["share"] = False
                return
            win32gui.RegisterHotKey(hwnd, HOTKEY_ID_SHARE, mods | MOD_NOREPEAT, vk)
            self._hotkey_ok["share"] = True
            self.hub.log(f"分享快捷键已注册: {combo}")
        except Exception as e:
            self._hotkey_ok["share"] = False
            self.hub.log(f"分享快捷键注册失败: {e}")

    def _register_share_code_hotkey(self):
        """注册「用固定提取码下载最近分享」的全局热键（可选，默认不设置）。

        与分享热键同口径：可选功能，注册失败不重试、不打扰；未启用全局热键、
        未配置（空 / 无 / none / null）时静默跳过。异常一律吞掉。"""
        if win32gui is None:
            return
        self._hotkey_ok["share_code"] = None
        try:
            if not self.state.snapshot().get("hotkey_enabled", True):
                return
            combo = str(self.state.snapshot().get("hotkey_share_code", "")).strip()
            if not combo or combo.lower() in ("无", "none", "null"):
                return
            parsed = parse_hotkey(combo)
            if parsed is None:
                self.hub.log(f"固定提取码快捷键配置无效，未注册: {combo}")
                self._hotkey_ok["share_code"] = False
                return
            mods, vk = parsed
            hwnd = int(self.winId())
            if not hwnd:
                self._hotkey_ok["share_code"] = False
                return
            win32gui.RegisterHotKey(hwnd, HOTKEY_ID_SHARE_CODE, mods | MOD_NOREPEAT, vk)
            self._hotkey_ok["share_code"] = True
            self.hub.log(f"固定提取码快捷键已注册: {combo}")
        except Exception as e:
            self._hotkey_ok["share_code"] = False
            self.hub.log(f"固定提取码快捷键注册失败: {e}")

    def _unregister_hotkey(self):
        if win32gui is None:
            return
        try:
            hwnd = int(self.winId())
            win32gui.UnregisterHotKey(hwnd, HOTKEY_ID)
        except Exception:
            pass
        try:
            hwnd = int(self.winId())
            win32gui.UnregisterHotKey(hwnd, HOTKEY_ID_SHARE)
        except Exception:
            pass
        try:
            hwnd = int(self.winId())
            win32gui.UnregisterHotKey(hwnd, HOTKEY_ID_SHARE_CODE)
        except Exception:
            pass


def _first_run_7z_check(state, hub, parent):
    """首次启动的 7-Zip 检查：缺失/过低时弹窗询问安装方式。

    在后台线程检测（7z 未装时纯文件系统判断，装了时一次 7z i），
    不阻塞启动；结果只在需要处理时才在主线程弹窗。"""
    def worker():
        try:
            info = sevenzip_manager.check_environment()
        except Exception as e:
            hub.log(f"首次 7-Zip 检查失败: {e}")
            return
        if info["status"] == "ok":
            return
        hub.log(f"首次启动检测: 7-Zip {info['status']}"
                + (f"（{info['version_str']}）" if info["version_str"] else ""))
        def show():
            dlg = SevenZipSetupDialog(state, hub, info, parent)
            dlg.exec_()
        QTimer.singleShot(0, show)
    threading.Thread(target=worker, daemon=True).start()

