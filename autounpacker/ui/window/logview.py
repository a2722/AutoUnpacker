# -*- coding: utf-8 -*-
"""日志视图助手：着色 + 可点链接渲染、已复制链接降级/恢复、复制气泡、日志落盘分流。

Stage 6f 从 ui/main_window.py 原样拆出；函数体/签名/文档字符串逐字未改。
归属：MainWindow 经 `from .window.logview import ...` 再导出使用；
      share_flow 经 `from .logview import _share_log` 共用日志落盘。
"""
import html
import threading

from PyQt5.QtGui import QTextCursor, QTextCharFormat, QColor, QCursor

from ...utils import split_urls
from ..style import PALETTE
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
