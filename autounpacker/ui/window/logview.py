# -*- coding: utf-8 -*-
"""日志视图助手：着色 + 可点链接渲染、已复制链接降级/恢复、复制气泡、日志落盘分流。

Stage 6f 从 ui/main_window.py 原样拆出；函数体/签名/文档字符串逐字未改。
归属：MainWindow 经 `from .window.logview import ...` 再导出使用；
      share_flow 经 `from .logview import _share_log` 共用日志落盘。
"""
import html
import re
import time

from PyQt5.QtCore import QEvent, QObject, QTimer, Qt
from PyQt5.QtGui import QTextCursor, QTextCharFormat, QColor, QCursor
from PyQt5.QtWidgets import QLabel, QToolTip

from ...utils import split_urls
from ..style import PALETTE, tokens
from ..widgets import show_toast
from .consts import _HUB_LOG_PREFIX, _LOG_VALUE_LABELS

# `_restore_link_in_view` 以 `MainWindow._block_span_has_anchor` 判定 anchor：
# logview 不能 import ..main_window（导入图会成环），由 main_window 模块底部的
# 兼容层在类定义完成后注入；未注入时该调用落入函数内既有 try/except（不恢复、不抛）。
MainWindow = None


def _find_log_urls(text):
    """返回 [(start, end, url)]：行内所有 http(s)/www 链接及其真实边界区间。

    统一委托 utils.split_urls：URL 边界与尾部标点规则只有一份。渲染与点击定位
    共用本函数，保证「能渲染成链接的」与「能点中的」完全一致。剔除尾部标点后
    为空则不收录；一行内多个链接全部收录。"""
    return split_urls(text)


# 应用内「动作链接」：日志里出现这些短语时渲染成可点击锚点，点击**跳转**（不是复制）。
# 关键约束：显示文本 = 短语本身（与 block 纯文本逐字一致）——QPlainTextEdit 没有
# anchorClicked，定位靠 block 纯文本 / fragment；若显示文本与存储文本不一致，
# _rerender_log_view（用 block.text() 重画）会让锚点在主题切换后丢失。
_APP_LINK_PREFIX = "app://"
# 主热键注册失败时日志里给出的「打开设置页」动作短语（显示文本即短语本身）。
_APP_LINK_SETTINGS_LABEL = "打开设置-外观与快捷键"
_APP_LINK_SETTINGS_TARGET = "app://settings/ui"
_LOG_ACTIONS = (
    (_APP_LINK_SETTINGS_LABEL, _APP_LINK_SETTINGS_TARGET),
)


def _find_log_actions(text):
    """返回 [(start, end, href)]：text 里所有动作短语及其跳转目标。

    短语的显示文本就是它本身（_LOG_ACTIONS 的键）：渲染期按区间包锚点、
    点击期按 fragment.anchorHref 判定，重载 / 主题重绘都能稳定复现。"""
    out = []
    body = str(text or "")
    for phrase, href in _LOG_ACTIONS:
        if not phrase:
            continue
        pos = 0
        while True:
            i = body.find(phrase, pos)
            if i < 0:
                break
            out.append((i, i + len(phrase), href))
            pos = i + len(phrase)
    out.sort(key=lambda t: t[0])
    return out


def _app_anchor_of_block(block, offset):
    """点击偏移处的 fragment 是否带 app:// 动作锚点：是则返回 (start, end, href)。

    动作链接的显示文本与 block 纯文本逐字一致，渲染期已包成 <a href="app://…">；
    点击期直接读 fragment 的 anchorHref 最稳（不必按短语在点击侧重新解析）。
    刻意做成**模块级**函数（与 _fold_href_of_block 同风格）：点击管线不新增实例
    属性，宿主桩只需绑定 MainWindow 既有方法即可继续工作。"""
    try:
        it = block.begin()
        while not it.atEnd():
            fr = it.fragment()
            it += 1
            if not fr.isValid():
                continue
            fs = fr.position() - block.position()
            fe = fs + fr.length()
            if fs <= offset < fe:
                href = str(fr.charFormat().anchorHref() or "")
                if href.startswith(_APP_LINK_PREFIX):
                    return fs, fe, href
                return None
    except Exception:
        pass
    return None


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
    4. 全行只有 http(s)/www 链接是蓝色 + 下划线 + 手型光标（点击一次即复制并降级）；
    动作短语（见 _LOG_ACTIONS）同样是蓝色 + 下划线，但点击是**跳转**（不复制、不降级）。
    degraded：正文坐标下「已经点击复制过」的链接区间；命中即渲染成普通暗色
    （log_ts、无下划线、无 anchor）——重载/主题重绘后已复制链接绝不复活。
    逐片段 html.escape（链接单独 escape），QPlainTextEdit 解析后 block 纯文本仍
    等原始 msg，点击侧据同一 msg 用 _find_log_urls 精确定位。
    """
    m = _HUB_LOG_PREFIX.match(msg)
    ts = m.group(0) if m else ""
    body = msg[len(ts):]
    url_spans = _find_log_urls(body)
    action_spans = _find_log_actions(body)
    degraded_spans = set(degraded or ())
    value_spans = []
    for s, e in _log_value_spans(body):
        if any(not (e <= us or s >= ue) for us, ue, _u in url_spans):
            continue          # 值 chip 与链接重叠：链接优先（唯一可交互文本）
        if any(not (e <= ax or s >= ay) for ax, ay, _h in action_spans):
            continue          # 值 chip 与动作短语重叠：动作优先
        value_spans.append((s, e))
    marks = [(s, e, "url", url) for s, e, url in url_spans]
    marks += [(s, e, "action", href) for s, e, href in action_spans]
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
        elif kind == "action":
            # 动作链接：href 是 app:// 目标，显示文本仍是原文短语（与 block 纯文本
            # 逐字一致，主题重绘按 block.text() 也能重新识别）。点击只跳转、绝不降级。
            parts.append(
                f'<a href="{html.escape(url, quote=True)}"'
                f' style="color:{PALETTE["log_link"]};text-decoration:underline">'
                f'{html.escape(body[s:e])}</a>')
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


# ---------------------------------------------------------------------------
# 日志视图折叠（显示层）：7-Zip 原始输出整块折成一行、连续相同的日志行合成一行，
# 点击展开 / 收起。
#
# 只影响「视图」：日志文件与生产者一行都不动（折叠不落盘、不裁剪、不改任何数据）。
# 7-Zip 块边界靠生产者契约的两行标记（中间每行都带 hub 时间戳前缀，标记是唯一边界）：
#   起始：含 "--- 7-Zip 原始输出 ---"
#   收尾：含 "--- 7-Zip 原始输出结束（共 N 行）---"（N = 原始行数）
# 安全阀：未收尾的块（旧版本日志 / 写入中断）按行数上限或超时原样吐出，绝不吞行。
# 另有两道「绝不吞行」的闸门：块内再见起始标记（把上一个块按隐式收尾折起来）、
# 有限快照收尾（重载/重装视图结束即把仍在缓冲的块原样吐出）。
#
# 重复串折叠（kind="repeat"）：不在块内时，连续两条「正文完全相同」的行（正文 =
# 去掉时间戳 + [第N层] 前缀后的文本，见 _fold_line_head）建立候选——第 2 条先藏起，
# 第 3 条起整串都不再显示（并收回已经显示过的第 1 条）；正文变化 / 遇到起始标记 /
# 超时 / 快照收尾时结算：>= _REPEAT_FOLD_MIN 条合成一条折叠行，否则把尚未显示的
# 原始行原样吐回。两条以内的相同行不折（原样显示）。与 7-Zip 块严格互斥，绝不跨块
# 边界折叠；块内的相同行仍按 7-Zip 块整块折叠，绝不单独折成重复串。
# ---------------------------------------------------------------------------

_FOLD_START_MARK = "--- 7-Zip 原始输出 ---"
_FOLD_END_MARK = "7-Zip 原始输出结束"
_FOLD_AFFORDANCE = "—— 点击"
_FOLD_HREF_PREFIX = "fold://"
# 时间戳前缀：视图行是 "[HH:MM:SS] "（Hub 队列 / task_log_line 补），日志文件是
# "[YYYY-MM-DD HH:MM:SS] "；折叠行按原样保留拿到的那种，看起来仍像原生日志。
_FOLD_TS_RE = re.compile(
    r"^(?:\[\d{4}-\d{2}-\d{2}[ T]\d{2}:\d{2}:\d{2}\]\s*)?(?:\[\d{2}:\d{2}:\d{2}\]\s*)?")
_FOLD_LAYER_RE = re.compile(r"^(\[[^\[\]]*?层\]\s*)")
# 安全阀上限：实测真实块就是 739 行（分卷缺卷时 7z 逐条报 Unavailable data），上限
# 必须高于真实块——否则块还没收尾就被冲掉，折叠对真实场景失效；10 秒超时仍是主兜底。
_FOLD_MAX_LINES = 2000
_FOLD_TIMEOUT_SEC = 10.0
# 重复串折叠：阈值 3 行——1~2 条相同行折起来反而更难读，不折（原样显示，绝不吞行）。
_REPEAT_FOLD_MIN = 3
# 重复串超时：轮询类重复可持续几分钟（实测 115 条 / 约 4 分钟），兜底间隔用 60s，
# 长串绝不被切碎；不足阈值（成不了折）的候选仍用普通 10s，尽早把藏起的行原样吐回。
_REPEAT_TIMEOUT_SEC = 60.0
# 单个重复串的缓冲上限（与 7-Zip 块同思路的安全阀）：宁可多分几折，绝不无界增长。
_REPEAT_MAX_LINES = 20000
# 视图模型（_view_items）上限：只留最近这么多项，长时间运行也绝不无界增长。
_VIEW_ITEMS_MAX = 3000
# 置顶折叠条（置顶 = 已展开的块滚过头后，折叠头钉在视图顶部）：
# 复用视图的行高与调色板，尺寸/配色不另造一套。
_FOLD_PIN_OBJECT_NAME = "logFoldPin"
_FOLD_PIN_MAX_WALK = 3000     # 向上找折叠头 block 的最大步数（与视图行数上限同量级）
_FOLD_PIN_PAD_X = 6
_FOLD_PIN_PAD_Y = 2


def _fold_line_head(line):
    """起始行 -> (时间戳前缀, "[第N层] " 前缀)，都按原样保留（折叠行仍像原生日志）。"""
    text = str(line or "")
    m = _FOLD_TS_RE.match(text)
    ts = m.group(0) if m else ""
    rest = text[len(ts):]
    m2 = _FOLD_LAYER_RE.match(rest)
    return ts, (m2.group(1) if m2 else "")


def _fold_href(fold):
    """折叠项的锚点 href：点击侧据此精确回查折叠项，不依赖块号/绝对位置。"""
    try:
        return "%s%d" % (_FOLD_HREF_PREFIX, int(fold.get("id")))
    except Exception:
        return ""


def _fold_href_of_block(block):
    """block 上的 fold:// 锚点 href（无则 None）：整行可点，命中判定只看锚点。"""
    try:
        it = block.begin()
        while not it.atEnd():
            fr = it.fragment()
            it += 1
            if fr.isValid():
                href = str(fr.charFormat().anchorHref() or "")
                if href.startswith(_FOLD_HREF_PREFIX):
                    return href
    except Exception:
        pass
    return None


def _fold_head_text(fold, expanded, affordance=True):
    """折叠头行文本：保留原时间戳 + [第N层] 前缀，按 kind 区分两种折叠。

    7-Zip 块："[21:48:43] [第3层] 7-Zip 原始输出（739 行）—— 点击展开"；
    重复串（kind="repeat"）："[21:48:43] [第3层] <正文>（重复 115 次）—— 点击展开"。

    affordance=False（搜索/级别过滤中整块展开）时只留摘要，不给「点击收起」入口——
    那种状态下收起会藏住命中的行。"""
    ts = str(fold.get("ts") or "")
    layer = str(fold.get("layer") or "")
    try:
        count = max(0, int(fold.get("count") or 0))
    except Exception:
        count = 0
    if str(fold.get("kind") or "") == "repeat":
        text = "%s%s%s（重复 %d 次）" % (ts, layer, str(fold.get("body") or ""), count)
    else:
        text = "%s%s7-Zip 原始输出（%d 行）" % (ts, layer, count)
    if affordance:
        text += "—— 点击收起" if expanded else "—— 点击展开"
    return text


def _anchor_fold_affordance(block, text, href):
    """给折叠头行的「—— 点击展开/收起」片段套上 fold:// 锚点（整行可点，样式只改该片段）。"""
    i = str(text).rfind(_FOLD_AFFORDANCE)
    if i < 0 or not href:
        return
    _restore_link_span(block, i, len(text), href)


def _fold_tip_text(fold):
    """折叠行悬停提示（紧凑一行），按 kind 区分。

    隐式收尾的块（旧版本日志没有收尾标记）只在**提示**里说明一句；折叠行本身的
    文字与正常块完全一致（不给视图文字加任何特例）。"""
    try:
        count = max(0, int(fold.get("count") or 0))
    except Exception:
        count = 0
    if str(fold.get("kind") or "") == "repeat":
        if bool(fold.get("expanded")):
            return "已展开 %d 行重复日志；点击收起" % count
        return "已折叠 %d 行重复日志；点击展开" % count
    if bool(fold.get("expanded")):
        return "已展开 %d 行 7-Zip 原始输出；点击收起" % count
    if fold.get("implicit"):
        return "已折叠 %d 行 7-Zip 原始输出（该块无收尾标记）；点击展开" % count
    return "已折叠 %d 行 7-Zip 原始输出；点击展开" % count


class LogFold:
    """日志视图的显示层折叠缓冲（纯 Python，无 Qt 依赖，离线可测）。

    feed() 逐行吃入，返回本次应渲染的显示项：
      ("line", 原文)    原样渲染一行；
      ("fold", 折叠项)  折叠头行（7-Zip 块 或 重复串，见折叠项的 kind 字段）；
      ("retract", n)    把视图里最近 n 条原始行收回（重复折叠「第 1 条已显示」的追溯
                        收起——由 FoldController 落到视图模型上）。

    两种缓冲互斥，绝不跨边界混合：
    1) 7-Zip 原始输出块：块内原始行先缓冲、收尾行到达才吐出一条折叠项；块内再见起始
       标记 / 行数上限 / 超时 / 快照收尾 finish() 一律把未收尾块原样吐出——缓冲绝不
       吞掉任何一行，更不吞后续无关日志。折叠头显示的行数恒等于实际缓冲行数。
    2) 重复串候选：不在块内时，第 2 条「正文完全相同」（正文 = 去掉时间戳 + [第N层]
       前缀，见 _fold_line_head）的行建立候选——它先藏起，第 1 条保持可见（还不能
       确定后面是否继续重复）；第 3 条确认成串时收回已显示的第 1 条、整串不再显示。
       正文变化 / 起始标记 / 超时 / finish() 时结算：长度 >= repeat_min 吐出一条
       repeat 折叠项；否则把尚未显示的原始行原样吐回（第 1 条本来就可见，绝不重复
       吐、绝不吞）。第 2 条候选的兜底是普通 timeout（尽快吐回），成串后换更长的
       repeat_timeout（长串绝不被切碎）。
    """

    def __init__(self, max_lines=_FOLD_MAX_LINES, timeout=_FOLD_TIMEOUT_SEC,
                 repeat_min=_REPEAT_FOLD_MIN, repeat_timeout=_REPEAT_TIMEOUT_SEC,
                 repeat_max_lines=_REPEAT_MAX_LINES):
        self.max_lines = int(max_lines)
        self.timeout = float(timeout)
        self.repeat_min = int(repeat_min)
        self.repeat_timeout = float(repeat_timeout)
        self.repeat_max_lines = int(repeat_max_lines)
        self.pending = None      # 正在缓冲的 7-Zip 块（None = 不在块内）
        self.repeat = None       # 正在缓冲的重复串候选（None = 无）
        self.last = None         # 最近一条已显示、仍可与后续行配对的普通行
        self._seq = 0

    def feed(self, line, now=None):
        """吃进一行；返回应渲染的显示项列表（空 = 仍在缓冲，暂不显示）。"""
        text = str(line or "")
        now = time.time() if now is None else float(now)
        if self.pending is not None:
            return self._feed_in_block(text, now)
        if self.repeat is not None:
            return self._feed_repeat(text, now)
        if _FOLD_START_MARK in text:
            self.last = None     # 块内整块隐藏：块前的普通行不再参与重复配对
            self._seq += 1
            ts, layer = _fold_line_head(text)
            self.pending = {"id": self._seq, "ts": ts, "layer": layer,
                            "start": text, "lines": [], "count": 0,
                            "expanded": False, "at": now}
            return []
        return self._feed_plain(text, now)

    def flush(self):
        """安全阀：结算重复候选，并把 7-Zip 缓冲原样吐出（起始行 + 已缓冲原始行）。"""
        out = self._close_repeat()
        pend, self.pending = self.pending, None
        if pend is not None:
            out.append(("line", pend.get("start") or ""))
            out.extend(("line", ln) for ln in pend.get("lines") or ())
        return out

    def finish(self):
        """有限快照收尾：把仍在缓冲的行原样吐出（重载/重装视图等快照路径必须调用）。

        与 flush() 只差语义：flush() 是实时路径的超时安全阀，finish() 是「这份日志
        读到头了」——两条路径都绝不吞行，实现共用一份，绝不各写一套。"""
        return self.flush()

    def stale(self, now=None):
        """缓冲是否已超时（宿主定时器据此决定要不要 flush）；超时秒数按缓冲类型区分。"""
        item = self.pending if self.pending is not None else self.repeat
        if item is None:
            return False
        now = time.time() if now is None else float(now)
        try:
            return (now - float(item.get("at") or now)) > self._timeout_for(item)
        except Exception:
            return False

    def pending_arm(self):
        """当前缓冲的定时器武装信息 (键, 超时秒)：宿主据此武装/重启定时器。

        键 None = 没有需要兜底的缓冲（不必武装）。7-Zip 块整块用普通 timeout；
        重复候选在成串（可折）前用普通 timeout（成不了折、尽快原样吐回），成串后
        换更长的 repeat_timeout（长重复串绝不被切碎）。键或秒数变化 = 重新计时。"""
        if self.pending is not None:
            return ("zip", id(self.pending)), self.timeout
        run = self.repeat
        if run is not None:
            foldable = len(run.get("lines") or ()) >= self.repeat_min
            return ("repeat", run.get("id"), foldable), self._timeout_for(run)
        return None, None

    def _timeout_for(self, item):
        """该缓冲适用的兜底秒数：重复串成串后用更长的 repeat_timeout。"""
        if str(item.get("kind") or "") == "repeat":
            if len(item.get("lines") or ()) >= self.repeat_min:
                return self.repeat_timeout
        return self.timeout

    def _feed_plain(self, text, now):
        """不在任何缓冲内的一行：与上一条已显示的普通行比对，开启/继续重复串。"""
        ts, layer = _fold_line_head(text)
        body = text[len(ts) + len(layer):]
        last = self.last
        if last is not None and last.get("body") == body:
            # 第 2 条相同：建立候选（本条先藏起；第 1 条暂留视图，等第 3 条确认成串）
            self._seq += 1
            self.repeat = {"id": self._seq, "kind": "repeat",
                           "ts": last.get("ts") or "",
                           "layer": last.get("layer") or "",
                           "body": body,
                           "lines": [last.get("line") or "", text],
                           "count": 0, "expanded": False, "at": now,
                           "head_visible": True}
            self.last = None
            return []
        self.last = {"body": body, "ts": ts, "layer": layer, "line": text}
        return [("line", text)]

    def _feed_repeat(self, text, now):
        """重复串候选中的一行：同正文继续攒；起始标记/异正文先结算再交给正常路径。"""
        run = self.repeat
        if _FOLD_START_MARK in text:
            # 块边界优先：先结算重复候选，起始标记重新开 7-Zip 块（绝不跨块折叠）。
            return self._close_repeat(now) + self.feed(text, now)
        ts, layer = _fold_line_head(text)
        body = text[len(ts) + len(layer):]
        if body == run.get("body"):
            run["lines"].append(text)
            self.last = None
            out = []
            if run.get("head_visible") and len(run["lines"]) >= self.repeat_min:
                # 第 3 条确认成串：收回已显示的第 1 条，从此整串不再显示（计数不撒谎）
                run.pop("head_visible", None)
                run["at"] = now
                out.append(("retract", 1))
            if len(run["lines"]) > self.repeat_max_lines or self.stale(now):
                out.extend(self._close_repeat(now))
            return out
        return self._close_repeat(now) + self._feed_plain(text, now)

    def _close_repeat(self, now=None):
        """结算重复串候选：够阈值吐一条 repeat 折叠项，否则把尚未显示的原始行吐回。"""
        run, self.repeat = self.repeat, None
        if run is None:
            return []
        lines = list(run.get("lines") or ())
        if len(lines) >= self.repeat_min:
            run["count"] = len(lines)
            run["expanded"] = False
            run.pop("head_visible", None)
            run.pop("at", None)
            self.last = None
            return [("fold", run)]
        head_visible = bool(run.pop("head_visible", False))
        outgoing = lines[1:] if head_visible else lines
        if lines:
            # 候选串短于阈值：第 1 条已显示过，只补吐还没显示的；并把它作为新的可配对尾行
            last_line = lines[-1]
            ts, layer = _fold_line_head(last_line)
            self.last = {"body": run.get("body") or "", "ts": ts,
                         "layer": layer, "line": last_line}
        return [("line", ln) for ln in outgoing]

    def _feed_in_block(self, text, now):
        pend = self.pending
        if _FOLD_START_MARK in text:
            # 块内又见起始标记 = 上一个块已经结束（旧版本日志没有收尾标记 / 写入中断）：
            # 按「隐式收尾」把它折成一行，本行重新开块。块被下一块的起始标记明确界定，
            # 行数取实际缓冲行数（绝不谎报），点击即可展开——旧格式块不再整段铺在视图里，
            # 也绝不吞掉后面的内容（旧实现会把后面全吞掉，上一版则整段原样吐出）。
            pend["count"] = len(pend["lines"])
            pend["implicit"] = True
            pend["end"] = None
            pend.pop("at", None)
            self.pending = None
            out = [("fold", pend)]
            out.extend(self.feed(text, now))
            return out
        if _FOLD_END_MARK in text:
            # FIX 3（关键）：标签必须等于「实际被藏住的行数」= 已缓冲原始行数；
            # 收尾标记里解析出的 N 只来自生产者，可能与缓冲不一致（旧版本/截断），
            # 一律以缓冲为准——折叠头绝不谎报行数。
            pend["count"] = len(pend["lines"])
            pend["end"] = text
            pend.pop("at", None)
            self.pending = None
            return [("fold", pend)]
        pend["lines"].append(text)
        if len(pend["lines"]) > self.max_lines or self.stale(now):
            return self.flush()
        return []


class FoldController(QObject):
    """日志视图折叠控制器（一套折叠机制服务多个日志视图）。

    拥有：折叠缓冲（LogFold）、视图模型（view_items）、id 映射（folds）、超时定时器、
    展开态、命中判定（fold_at）、点击/悬停处理与置顶折叠条。两种折叠共用同一套视图：
    7-Zip 原始输出块 与 连续相同行（kind="repeat"）；重复折叠的「追溯收起」由
    ("retract", n) 显示项落到视图模型上（收回已画出的原始行再画折叠头）。
    渲染由宿主注入 render(view, msg)——着色/链接管线只有一份实现，本类绝不另造一套。
    事件过滤器只消费「折叠相关」事件（命中折叠头的移动/点击、置顶条上的点击），
    其余事件一律返回 False：链接悬停/点击复制/拖选行为完全不受影响。
    """

    def __init__(self, view, render=None, force_open=None, parent=None):
        super().__init__(parent)
        self.view = view
        self.buffer = LogFold()
        self.folds = {}                  # 折叠 id -> 折叠项（点击侧按锚点回查）
        self.view_items = []             # 视图模型：("line", 原文) / ("fold", 折叠项)
        self.timer = QTimer(self)
        self.timer.setSingleShot(True)
        self.timer.setInterval(int(_FOLD_TIMEOUT_SEC * 1000))
        self.timer.timeout.connect(self.on_timeout)
        self._render = render            # callable(view, msg)
        self._force_open = force_open    # callable() -> bool（搜索/过滤时整块展开）
        self._pin = None                 # 置顶折叠条（惰性创建）
        self._pin_fold_id = None         # 置顶条当前指向的折叠 id
        self._pin_cache = (None, None)   # (firstVisibleBlock 号, 解析出的折叠项)
        self._pin_count = None           # 上次解析时的块数（变小 = 发生过裁剪）
        self._tip_key = None
        try:
            self.view.viewport().installEventFilter(self)
            self.view.viewport().setMouseTracking(True)
            self.view.installEventFilter(self)
            self.view.verticalScrollBar().valueChanged.connect(self._on_scroll)
        except Exception:
            pass

    # ---- 缓冲 / 模型 ----
    def reset(self):
        """重载/清空：停表、重开缓冲、丢弃展开态与 id 映射（展开态不跨重载保留）。"""
        try:
            self.timer.stop()
        except Exception:
            pass
        self.buffer = LogFold()
        self.folds = {}
        self.view_items = []
        self.invalidate_pin()
        self.update_pin()

    def feed(self, msg):
        """喂一行日志：只有缓冲吐出的显示项才渲染（块内原始行先攒着）。

        超时定时器按缓冲类型武装/重启：7-Zip 块起 10 秒仍未见收尾行就原样吐出（不随
        每行重启）；重复串在成串前用普通间隔（藏起的行要尽快吐回），成串后换更长的
        间隔（长串绝不被切碎）。两者的武装信息都由缓冲的 pending_arm() 给出，定时器
        只在「缓冲代次或间隔」变化时重启（不是每行重启）。"""
        for line in (str(msg).splitlines() or [""]):
            before_key, before_secs = self.buffer.pending_arm()
            for item in self.buffer.feed(line):
                self.append_item(item)
            after_key, after_secs = self.buffer.pending_arm()
            if after_key is not None and (after_key != before_key
                                          or after_secs != before_secs):
                self.arm_timer(after_secs)

    def arm_timer(self, secs):
        """按缓冲当前类型给出的间隔（重新）武装兜底定时器（单发、到点走 on_timeout）。"""
        try:
            self.timer.stop()
            self.timer.setInterval(max(1, int(float(secs) * 1000)))
            self.timer.start()
        except Exception:
            pass

    def finish(self):
        """有限快照收尾（重载 / 重装该任务日志）：停表并把缓冲原样吐出。

        FIX 2：快照的末尾绝不因为「还没等到收尾行」而藏住不显示——旧版本日志的
        未收尾块在快照结束处立即按原始行渲染；10 秒超时兜底只服务实时（流式）路径。"""
        try:
            self.timer.stop()
        except Exception:
            pass
        try:
            items = self.buffer.finish()
        except Exception:
            items = []
        for item in items:
            self.append_item(item)

    def on_timeout(self):
        """超时安全阀：缓冲到期 -> 未收尾的 7-Zip 块原样吐出 / 重复串结算（折或原样吐出），
        绝不吞行。"""
        try:
            items = self.buffer.flush()
        except Exception:
            items = []
        for item in items:
            self.append_item(item)

    def force_open(self):
        """搜索/级别过滤生效时为 True：整块展开渲染（折叠绝不藏住命中的行）。"""
        try:
            return bool(self._force_open()) if self._force_open is not None else False
        except Exception:
            return False

    def is_open(self, fold):
        """该折叠项当前是否展开（用户展开过，或搜索/级别过滤要求整块可见）。"""
        return bool(fold.get("expanded")) or self.force_open()

    def append_item(self, item):
        """记入视图模型并渲染；模型只留最近若干项，避免长时间运行无界增长。

        ("retract", n) 是显示层的追溯收起（重复折叠第 1 条已画出的情形）：把最近 n 条
        原始行从视图模型收回并重画——绝不针对折叠头/其他项，n 只会收回原始行。"""
        if item[0] == "retract":
            self.retract_items(item[1])
            return
        self.view_items.append(item)
        if item[0] == "fold":
            try:
                self.folds[int(item[1].get("id"))] = item[1]
            except Exception:
                pass
        if len(self.view_items) > _VIEW_ITEMS_MAX:
            del self.view_items[:len(self.view_items) - _VIEW_ITEMS_MAX]
            self.prune_folds()
        self.render_item(item)
        self.update_pin()

    def retract_items(self, n):
        """收回视图里最近 n 条已渲染的原始行（重复折叠的追溯收起）。

        只从编辑器**末尾**删掉这 n 个块（O(n)），绝不整页重画：本视图的重画是
        着色级的 O(总行数) 操作——日志刷新时每个重复串都触发一次，会让「打开/关闭
        详情」这类强制刷新变得极慢（实测 1600 行日志一次刷新 = 4 次整页重画、
        3910 次着色行渲染，是唯一重画时的 3.4 倍）。定点删除失败才退回整页重画。"""
        try:
            n = max(0, int(n))
        except Exception:
            return
        removed = 0
        while removed < n and self.view_items and self.view_items[-1][0] == "line":
            self.view_items.pop()
            removed += 1
        if not removed:
            return
        if not self._remove_tail_blocks(removed):
            self.rerender_view()
            return
        self.invalidate_pin()
        self.update_pin()

    def _remove_tail_blocks(self, n):
        """从编辑器末尾删除 n 个块（原始行各占一块），不碰 view_items、不整页重画。

        追溯收起的行必定是「刚渲染的最后几行」（见 LogFold 的成串时序），所以定点
        删末尾块与整页重画在视图上等价，但代价从 O(总行数) 降为 O(n)。返回是否成功。"""
        try:
            doc = self.view.document()
            if doc.blockCount() < n:
                return False
            cur = QTextCursor(doc)
            cur.movePosition(QTextCursor.End)
            cur.beginEditBlock()
            for _ in range(int(n)):
                cur.movePosition(QTextCursor.StartOfBlock, QTextCursor.KeepAnchor)
                cur.removeSelectedText()
                if not cur.atStart():
                    cur.deletePreviousChar()
            cur.endEditBlock()
            return True
        except Exception:
            return False

    def prune_folds(self):
        """丢弃已不在视图模型里的折叠项（id 映射绝不无界增长）。"""
        live = set()
        for kind, payload in self.view_items:
            if kind == "fold":
                try:
                    live.add(int(payload.get("id")))
                except Exception:
                    pass
        if len(self.folds) > len(live) + 64:
            self.folds = {k: v for k, v in self.folds.items() if k in live}

    def render_item(self, item):
        kind, payload = item
        if kind == "fold":
            self.render_fold(payload)
        else:
            self.render_line(payload)

    def render_fold(self, fold):
        """折叠头行：折叠态 1 行；展开态 = 头行 + 全部原始行（原样、原色）。"""
        force = self.force_open()
        text = _fold_head_text(fold, self.is_open(fold), affordance=not force)
        self.render_line(text)                # 走宿主既有管线（着色/纯文本都一致）
        if not force and self.view is not None:
            # 只给「—— 点击展开/收起」片段套锚点：整行可点，样式仍像原生日志
            try:
                _anchor_fold_affordance(self.view.document().lastBlock(), text,
                                        _fold_href(fold))
            except Exception:
                pass
        if self.is_open(fold):
            for line in fold.get("lines") or ():
                self.render_line(line)

    def render_line(self, msg):
        """渲染一行：宿主管线优先，缺失/异常时退回纯文本（与折叠无关的行也走这里）。"""
        if self._render is not None:
            try:
                self._render(self.view, msg)
                return
            except Exception:
                pass
        try:
            self.view.appendPlainText(str(msg))
        except Exception:
            pass

    def fold_at(self, pos):
        """pos 命中的折叠项：整行可点（行内含 fold:// 锚点即算命中）；无则 None。"""
        if pos is None:
            return None
        try:
            block = self.view.cursorForPosition(pos).block()
            href = _fold_href_of_block(block)
            if href:
                try:
                    return self.folds.get(int(href[len(_FOLD_HREF_PREFIX):]))
                except Exception:
                    return None
            return self.fold_by_text(block.text())   # 纯文本模式兜底（无锚点）
        except Exception:
            return None

    def fold_by_text(self, text):
        """无锚点兜底：按折叠头行文本回查（只有纯文本模式会走到）。"""
        for kind, payload in reversed(self.view_items):
            if kind != "fold":
                continue
            if text in (_fold_head_text(payload, False), _fold_head_text(payload, True)):
                return payload
        return None

    def toggle(self, fold):
        """展开/收起一个折叠块并整页重画（视图有 3000 行上限，重画很便宜）。"""
        if not isinstance(fold, dict):
            return False
        if self.force_open():
            return False      # 搜索/级别过滤中：保持展开，绝不藏住命中行
        fold["expanded"] = not bool(fold.get("expanded"))
        self.rerender_view()
        return True

    def rerender_view(self):
        """按当前展开态整页重画（视图模型不动；滚动位置尽力保留）。"""
        sb = None
        value = 0
        try:
            sb = self.view.verticalScrollBar()
            value = sb.value()
        except Exception:
            sb = None
        try:
            self.view.clear()
        except Exception:
            pass
        for item in self.view_items:
            self.render_item(item)
        try:
            if sb is not None:
                sb.setValue(min(value, sb.maximum()))
        except Exception:
            pass
        self.invalidate_pin()
        self.update_pin()

    def export_text(self):
        """导出/复制文本：折叠块按原始行完整展开（绝不因折叠少行）。

        视图里折叠行只占一行，但导出/复制出去的内容必须与落盘日志同量级：按视图
        模型重建「未折叠」文本。7-Zip 块 = 起始行 + 全部原始行 + 收尾行；重复串
        （kind="repeat"）= 全部原始行（原始行里本来就带时间戳，不再补任何东西）。"""
        out = []
        for kind, payload in self.view_items:
            if kind == "fold":
                if str(payload.get("kind") or "") == "repeat":
                    out.extend(str(ln) for ln in payload.get("lines") or ())
                    continue
                out.append(str(payload.get("start") or ""))
                out.extend(str(ln) for ln in payload.get("lines") or ())
                if payload.get("end"):
                    out.append(str(payload["end"]))
            else:
                out.append(str(payload))
        return "\n".join(out)

    # ---- 置顶折叠条 ----
    def invalidate_pin(self):
        """作废「顶部折叠」解析缓存（重绘 / 重置 / 尺寸变化后必须调用）。"""
        self._pin_cache = (None, None)

    def _on_scroll(self, _value=None):
        self.update_pin()

    def pin_fold(self):
        """viewport 顶部落在哪个「已展开」折叠块体内（没有则 None）。

        定位不依赖任何块号映射：取 firstVisibleBlock，向上逐块找带 fold:// 锚点的
        折叠头（最多 _FOLD_PIN_MAX_WALK 步）；顶部到该折叠头的距离必须落在块体内，
        否则顶部已在块后面的普通日志上，不置顶。结果按顶部块号缓存，滚动时绝大
        多数调用直接命中缓存（缓存只在重绘/重置/尺寸变化/裁剪时作废）。"""
        try:
            if not self.view.isVisible() or self.view.blockCount() <= 0:
                return None
            if not any(bool(f.get("expanded")) for f in self.folds.values()):
                return None      # 没有任何展开块：置顶条不可能出现（省一次向上回溯）
            first = self.view.firstVisibleBlock()
            if not first.isValid():
                return None
            key = first.blockNumber()
            if self._pin_cache[0] == key:
                return self._pin_cache[1]
            fold = self._resolve_pin_fold(first)
            self._pin_cache = (key, fold)
            return fold
        except Exception:
            return None

    def _resolve_pin_fold(self, first):
        """顶部块 -> 它所在的已展开折叠项（不在任何块体内则 None）。"""
        head, href = self._header_above(first)
        if head is None or not href:
            return None
        try:
            fold = self.folds.get(int(href[len(_FOLD_HREF_PREFIX):]))
        except Exception:
            fold = None
        if not isinstance(fold, dict) or not fold.get("expanded"):
            return None
        delta = first.blockNumber() - head.blockNumber()
        if delta <= 0 or delta > len(fold.get("lines") or ()):
            return None      # 顶部已在块体之外（块后面的普通日志 / 折叠头本身）
        return fold

    def _header_above(self, first):
        """从 first 向上找最近的折叠头 block，返回 (block, href)（找不到 None, None）。"""
        block = first
        hops = 0
        while block.isValid() and hops <= _FOLD_PIN_MAX_WALK:
            href = _fold_href_of_block(block)
            if href:
                return block, href
            block = block.previous()
            hops += 1
        return None, None

    def update_pin(self):
        """按当前滚动位置显示/隐藏置顶折叠条（空视图/隐藏视图绝不显示）。"""
        try:
            count = self.view.blockCount()
            if self._pin_count is not None and count < self._pin_count:
                self.invalidate_pin()   # 触发过 3000 行裁剪：块号整体位移，缓存作废
            self._pin_count = count
            fold = self.pin_fold()
            if fold is None:
                self._pin_fold_id = None
                if self._pin is not None and self._pin.isVisible():
                    self._pin.hide()
                return
            pin = self.ensure_pin()
            if pin is None:
                return
            text = _fold_head_text(fold, True)
            fm = self.view.fontMetrics()
            height = max(1, fm.height() + 2 * _FOLD_PIN_PAD_Y)
            width = max(1, self.view.viewport().width())
            if (pin.isVisible() and pin.width() == width
                    and pin.height() == height and pin.text() == text):
                self._pin_fold_id = fold.get("id")   # 文本/几何都没变：不做无谓重绘
                return
            if pin.text() != text:
                pin.setText(text)
            pin.setGeometry(0, 0, width, height)
            self._pin_fold_id = fold.get("id")
            pin.show()
            pin.raise_()
        except Exception:
            pass

    def ensure_pin(self):
        """惰性创建置顶折叠条（QLabel 子控件；主题切换由 refresh_theme 重贴色）。"""
        if self._pin is not None:
            return self._pin
        try:
            pin = QLabel(self.view.viewport())
            pin.setObjectName(_FOLD_PIN_OBJECT_NAME)
            pin.setCursor(Qt.PointingHandCursor)
            pin.setAutoFillBackground(True)
            pin.hide()
            pin.installEventFilter(self)
            self._pin = pin
            self._style_pin()
        except Exception:
            self._pin = None
        return self._pin

    def _style_pin(self):
        """置顶条配色：不透明底 + 底部分隔线，全部取自既有主题 token（日志文字不透出来）。

        用 QLabel#objectName 选择器：QSS 里 QLabel 默认 background: transparent，
        只有带上 objectName 的高优先级规则才画得出不透明底。"""
        if self._pin is None:
            return
        try:
            tk = tokens()
            self._pin.setStyleSheet(
                "QLabel#%s { background-color: %s; color: %s;"
                " border-bottom: 1px solid %s; padding: 0 %dpx; }"
                % (_FOLD_PIN_OBJECT_NAME, tk.get("log_bg"), tk.get("log_fg"),
                   tk.get("log_border") or PALETTE["log_ts"], _FOLD_PIN_PAD_X))
        except Exception:
            pass

    def refresh_theme(self):
        """主题切换：置顶条按新调色板重贴并重算显隐。"""
        self._style_pin()
        self.invalidate_pin()
        self.update_pin()

    def collapse_pinned(self):
        """点击置顶条：收起它指向的折叠块，并把视图滚到该折叠头（位置不迷惑）。"""
        fold = None
        try:
            fold = self.folds.get(int(self._pin_fold_id))
        except Exception:
            fold = None
        if not isinstance(fold, dict) or not self.toggle(fold):
            self.update_pin()
            return False
        if not bool(fold.get("expanded")):
            self._scroll_to_fold(fold)
        self.update_pin()
        return True

    def _scroll_to_fold(self, fold):
        """把折叠头滚到 viewport 顶部（收起后仍一眼看到自己在哪一块）。

        QPlainTextEdit 的纵向滚动条单位就是块号（value == firstVisibleBlock 的
        块号）：直接按块号设值即可精确置顶，绝不做像素/块号混算。"""
        block = self.fold_header_block(fold)
        if block is None:
            return
        try:
            sb = self.view.verticalScrollBar()
            sb.setValue(max(sb.minimum(), min(sb.maximum(), block.blockNumber())))
        except Exception:
            pass

    def fold_header_block(self, fold):
        """按 fold:// 锚点在文档里回查折叠头 block（重画后 href 仍随折叠项走）。"""
        href = _fold_href(fold)
        if not href:
            return None
        try:
            block = self.view.document().firstBlock()
            while block.isValid():
                if _fold_href_of_block(block) == href:
                    return block
                block = block.next()
        except Exception:
            pass
        return None

    # ---- 事件处理（只消费折叠相关事件；其余一律放行）----
    def eventFilter(self, obj, event):
        try:
            if self._pin is not None and obj is self._pin:
                return self._pin_event(event)
            viewport = self.view.viewport()
            if obj is viewport:
                et = event.type()
                if et == QEvent.Resize:
                    self.invalidate_pin()
                    self.update_pin()
                elif et == QEvent.Show:
                    self.update_pin()
                elif et == QEvent.Hide:
                    if self._pin is not None:
                        self._pin.hide()
                elif et == QEvent.MouseMove:
                    return self._mouse_move(event)
                elif et == QEvent.MouseButtonRelease:
                    return self._mouse_release(event)
                elif et == QEvent.Leave:
                    self._hide_tip()
                return False
            if obj is self.view:
                et = event.type()
                if et in (QEvent.Show, QEvent.Resize):
                    self.update_pin()
                elif et == QEvent.Hide and self._pin is not None:
                    self._pin.hide()
                return False
        except Exception:
            pass
        return False

    def _pin_event(self, event):
        """置顶条上的鼠标：按下即消费（绝不透给视图/链接管线），松开左键即收起。"""
        et = event.type()
        if et == QEvent.MouseButtonPress:
            return True
        if et == QEvent.MouseButtonRelease:
            if event.button() == Qt.LeftButton:
                self.collapse_pinned()
            return True
        if et == QEvent.MouseMove:
            try:
                self._pin.setCursor(Qt.PointingHandCursor)
            except Exception:
                pass
        return False

    def _mouse_move(self, event):
        """悬停折叠头：手型 + 紧凑提示；命中才消费事件（其余交给链接悬停管线）。"""
        fold = self.fold_at(event.pos())
        if fold is None:
            self._hide_tip()
            return False
        try:
            self.view.viewport().setCursor(Qt.PointingHandCursor)
        except Exception:
            pass
        self._show_tip(fold, event)
        return True

    def _mouse_release(self, event):
        """点击折叠头：整行展开/收起（拖选中时不干扰；未命中放行给链接点击管线）。"""
        if event.button() != Qt.LeftButton:
            return False
        try:
            if self.view.textCursor().hasSelection():
                return False
        except Exception:
            pass
        fold = self.fold_at(event.pos())
        if fold is None:
            return False
        return bool(self.toggle(fold))

    def _show_tip(self, fold, event):
        """折叠行悬停提示：同一行只在首次悬停时弹一次（避免每次 MouseMove 重弹闪烁）。"""
        key = (id(fold), bool(fold.get("expanded")))
        if self._tip_key == key:
            return
        self._tip_key = key
        try:
            gpos = event.globalPos()
        except Exception:
            gpos = None
        if gpos is None:
            return
        try:
            QToolTip.showText(gpos, _fold_tip_text(fold))
        except Exception:
            pass

    def _hide_tip(self):
        """离开折叠行：撤下悬停提示（没弹过就什么都不做）。"""
        if self._tip_key is None:
            return
        self._tip_key = None
        try:
            QToolTip.hideText()
        except Exception:
            pass


# 以下 4 个 module 级助手是折叠机制的旧入口（宿主曾直接调用）：生产路径已由
# FoldController 统一接管（运行日志页 / 该任务日志两个视图共用一套），这里保留
# 原样实现供旧调用方与回归测试使用，不改变任何行为。
def _fold_item_at(win, box, pos):
    """pos 处命中的折叠项（仅运行日志页的视图；无则 None）。"""
    lp = getattr(win, "log_page", None)
    if lp is None or box is None or box is not getattr(lp, "log_view", None):
        return None
    try:
        return lp.fold_at(pos)
    except Exception:
        return None


def _toggle_log_fold(win, box, pos):
    """点击折叠头行：展开/收起并整页重画。返回是否已处理（未命中返回 False）。"""
    fold = _fold_item_at(win, box, pos)
    if fold is None:
        return False
    lp = getattr(win, "log_page", None)
    try:
        return bool(lp.toggle_fold(fold))
    except Exception:
        return False


def _show_fold_tip(win, fold, event):
    """折叠行悬停提示：同一行只在首次悬停时弹一次（避免每次 MouseMove 重弹闪烁）。"""
    key = (id(fold), bool(fold.get("expanded")))
    if getattr(win, "_fold_tip_key", None) == key:
        return
    win._fold_tip_key = key
    try:
        gpos = event.globalPos()
    except Exception:
        gpos = None
    if gpos is None:
        return
    try:
        QToolTip.showText(gpos, _fold_tip_text(fold))
    except Exception:
        pass


def _hide_fold_tip(win):
    """离开折叠行：撤下悬停提示（没弹过就什么都不做）。"""
    if getattr(win, "_fold_tip_key", None) is None:
        return
    win._fold_tip_key = None
    try:
        QToolTip.hideText()
    except Exception:
        pass


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
            href = _fold_href_of_block(block)   # 折叠头行：重绘后锚点必须还在
            cur = QTextCursor(block)
            cur.setPosition(block.position())
            cur.setPosition(block.position() + block.length() - 1,
                            QTextCursor.KeepAnchor)
            cur.insertHtml(_render_log_html(
                text, win._log_color_for(text),
                _degraded_spans_for(win, box, text)))
            if href:
                _anchor_fold_affordance(doc.findBlockByNumber(i), text, href)
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
