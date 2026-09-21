# -*- coding: utf-8 -*-
"""分享流程助手：提取码取值优先级、缺码小窗、手势去重、线程安全回执。

Stage 6f 从 ui/main_window.py 原样拆出；函数体/签名/文档字符串逐字未改
（仅函数内相对导入深度随目录调整）。归属：MainWindow 经
`from .window.share_flow import ...` 再导出使用。
"""
import time

from PyQt5.QtWidgets import QApplication, QWidget

from ...utils import split_urls, is_baidu_pan_url
from ... import baidu_manifest as bm
from .consts import (PENDING_SHARE_TTL_SEC, SHARE_GESTURE_DEDUP_SEC,
                     SHARE_INVOKE_BUSY_MAX_SEC, _SHARE_ASK_NOTIFIED)
from .logview import _share_log

# ---------------------------------------------------------------------------
# 提取码取值 / 缺码小窗：module 级实现 + 类方法薄包装。
#
# 之所以放 module 级：既有大量测试用「非 QWidget 桩」直接调用
# `mw.MainWindow._open_recent_share(stub)` / `_share_ask_code`，这些桩只绑定了
# 它当时触达的方法名；若新逻辑改调 `self._effective_share_code(...)`，这些桩会
# 因缺方法而 AttributeError。module 级函数让旧桩继续可用，同时类上仍暴露
# `MainWindow._effective_share_code` / `_show_share_code_window` 作为正式助手。
# ---------------------------------------------------------------------------


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


def _close_share_ask_dlg(win, surl=None, uk=None, url=None, note=None):
    """成功提取后关闭「属于同一分享」的缺提取码小窗（只关闭、绝不回调）。

    小窗的唯一职责是收集提取码；一旦该分享被成功拉起（客户端已确认）或挑选提交
    成功，它的任务即告完成，必须立刻消失，绝不赖到 120s 超时。

    `note` 可覆盖收尾日志：链接失效等「非成功」场景关闭小窗时，不能用「已成功
    提取」这句会误导人的文案（见 main_window `_drain` 的 share_dead 分支）。

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
    _share_log(win, note or "[分享] 已成功提取，提取码小窗已关闭（填写任务完成）")
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
                    from ...monitors import QRMonitor as _QRM
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
    """托盘气泡（module 级）：三级收口。

    1) 宿主既有 `_share_notify`（真实实现内部走 `hub.notify`，受通知总开关 +
       分组开关过滤）；
    2) `hub.notify`；
    3) 直接投 `hub.q`——给轻量宿主（测试桩只有 `hub.q`、既无 `_share_notify`
       也无 `hub.notify`）兜底，否则通知会被静默丢掉，用户看不到任何提示。
    """
    fn = getattr(win, "_share_notify", None)
    if callable(fn):
        try:
            fn(title, msg)
            return
        except Exception:
            pass
    try:
        win.hub.notify(title, msg)
        return
    except Exception:
        pass
    try:
        win.hub.q.put({"type": "notify", "title": title, "msg": msg})
    except Exception:
        pass


# 手动手势（Alt+2）重复拉起的「再按一次确认」窗口（秒）：第一次只提醒不拉起，
# 窗口内再按一次同一手势才真正强制拉起。
MANUAL_REARM_SEC = 30


def _share_already_launched(win, key):
    """该分享本次运行是否已拉起过。

    优先宿主方法 `_share_needs_consent`（老测试桩可能没绑定它），取不到回退
    baidu_manifest 的进程内计数器，再回退本对象的集合。任何异常都按「未拉起过」
    处理（保持旧桩可用、绝不因这条增强分支打断手动手势）。
    """
    fn = getattr(win, "_share_needs_consent", None)
    if callable(fn):
        try:
            return bool(fn(key))
        except Exception:
            pass
    try:
        if int(bm.share_launch_count(key)) > 0:
            return True
    except Exception:
        pass
    return key in (getattr(win, "_share_launched_surls", None) or ())


def _manual_reinvoke_guard(win, surl, url):
    """手动手势（Alt+2）重复拉起拦截：需「再按一次」才强制拉起。

    同一分享本次运行已拉起过时：第一次按压只记一行 + 弹「重复」托盘提醒并进入
    「待确认」，**绝不拉起**；在再确认窗口内再按一次同一手势才强制拉起一次。
    这样误触（手滑再按一次 Alt+2）不会真的再下载一遍——旧行为「手动即明确同意、
    直接再下载」正是误触重复下载的根因；确需重下的人只需再按一次，比弹模态框更轻，
    也不打断连续操作。**未拉起过则一律放行**（首次拉起行为完全不变）。

    返回 True = 已拦截（调用方必须立即 return，不拉起）；False = 放行。
    """
    key = str(surl or "").strip()
    if not key or not _share_already_launched(win, key):
        return False
    now = time.time()
    armed = getattr(win, "_manual_reinvoke_armed", None)
    if (isinstance(armed, dict) and armed.get("surl") == key
            and (now - float(armed.get("at") or 0)) <= MANUAL_REARM_SEC):
        win._manual_reinvoke_armed = None
        _share_log(win, f"[分享] 已确认（第二次 Alt+2），强制再次拉起: {key}")
        return False
    win._manual_reinvoke_armed = {"surl": key, "at": now}
    _share_log(win,
               f"[分享] 该分享本次运行已拉起过，已拦下（不重复下载）；"
               f"如确需再拉一次，请在 {MANUAL_REARM_SEC} 秒内再按一次 Alt+2: {key}")
    _share_notify_via(
        win, "重复的分享链接",
        f"{url}\n本次运行已拉起过该分享，已拦下避免重复下载。"
        f"如确需强制再拉一次，请再按一次 Alt+2 确认。")
    return True


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
        from ..dialogs import ShareCodeAskDialog
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
