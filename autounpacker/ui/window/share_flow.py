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
                     SHARE_INVOKE_BUSY_MAX_SEC, PENDING_SHARE_TAG,
                     QUEUED_SHARE_INTENT_MAX, _SHARE_ASK_NOTIFIED)
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


def _compact_win(win):
    """精简模式宿主判定（惰性）：返回可用的 CompactWindow，否则 None。

    冻结条件：`win` 非空、`win._compact_window` 非空、且
    `win.state.get("ui_compact", False)` 为真。**任何缺失/异常一律 None**——
    既有大量离线测试用非 QWidget 桩（无 `_compact_window` / `state.get`）直接调这
    些函数，必须继续走原浮窗路径。绝不抛异常。"""
    if win is None:
        return None
    try:
        cw = getattr(win, "_compact_window", None)
        if cw is None:
            return None
        if not bool(win.state.get("ui_compact", False)):
            return None
        return cw
    except Exception:
        return None


def _ask_surface(win):
    """当前**活动的**取码表面：精简模式返回 CODE 页，完整模式返回浮窗；否则 None。

    只认「未结束」的请求（`is_active()`）——已提交/已忽略/已超时的不算。
    """
    cw = _compact_win(win)
    if cw is not None:
        try:
            page = getattr(cw, "code_page", None)
            if page is not None and page.is_active():
                return page
        except Exception:
            pass
        return None
    dlg = getattr(win, "_share_ask_dlg", None)
    if dlg is not None and _share_window_alive(dlg):
        try:
            if dlg.is_active():
                return dlg
        except Exception:
            pass
    return None


def _pending_ask(win):
    """未结束的「缺提取码」请求快照（跨模式交接用）；没有则 None。

    返回 dict：surl / url / share_uk / force_pick / deadline。
    `deadline` 是**绝对截止时刻**，交接后倒计时连续、绝不重置（见 dialogs/common）。
    """
    surf = _ask_surface(win)
    if surf is None:
        return None
    out = {"surl": "", "url": "", "share_uk": "", "force_pick": False,
           "deadline": None}
    try:
        t_surl, t_uk = _share_dlg_target(surf)
        out["surl"], out["share_uk"] = t_surl, t_uk
    except Exception:
        pass
    try:
        out["url"] = str(getattr(surf, "url", "") or "")
    except Exception:
        pass
    try:
        out["force_pick"] = bool(getattr(surf, "force_pick", False))
    except Exception:
        pass
    try:
        fn = getattr(surf, "deadline", None)
        out["deadline"] = fn() if callable(fn) else None
    except Exception:
        out["deadline"] = None
    # 目标全空 ⇒ 认不出是哪个分享，交接没有意义（宁可不弹，也不弹一个空窗）
    if not (out["surl"] or out["share_uk"] or out["url"]):
        return None
    return out


def _end_ask_surface(win, from_compact):
    """静默结束**来源模式**的取码表面（**绝不回调解**）：交接前先收掉旧表面，避免双份。

    `from_compact` 必须由调用方在**模式切换前**判定并传入：切换后 `is_compact()`
    已翻新，按它推导会找错表面（这正是「交接空转、小窗照样丢」的原因）。
    - 精简：`cw.leave_code()`（abandon + 回 HOME）；
    - 完整：摘掉 `on_decision` 再 close（closeEvent 的 ignore 回调因此不会发生），
      并清空 `win._share_ask_dlg`。
    返回是否收掉了某个表面。
    """
    if from_compact:
        cw = getattr(win, "_compact_window", None)
        if cw is None:
            return False
        try:
            cw.leave_code()
            return True
        except Exception:
            return False
    dlg = getattr(win, "_share_ask_dlg", None)
    if dlg is None:
        return False
    try:
        dlg.on_decision = None      # 先摘回调：close 只关窗、不产生 ignore 决策
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
    return True


def _carry_ask_across_mode(win, to_compact, ask=None, from_compact=None):
    """模式切换时**不丢失**取码小窗：把未结束的请求交接到目标模式的表面。

    - 切到精简：浮窗 → CompactWindow 的 CODE 页；
    - 切回完整：CODE 页 → 浮窗；
    两边共用同一 `deadline`（倒计时连续）。没有未结束请求时是 no-op。

    `ask` / `from_compact` 由调用方在**切换前**取好传入（切换后模式已翻新，
    再推导就会指错表面）。两个都不传时按「切换前就是当前模式」退化为自行判定。
    任何异常都吞掉（切换界面绝不因这条增强分支失败）。返回是否发生了交接。
    """
    try:
        if from_compact is None:
            from_compact = bool(_compact_win(win) is not None)
    except Exception:
        from_compact = False
    if ask is None:
        try:
            ask = _pending_ask(win)
        except Exception:
            ask = None
    if not ask:
        return False
    try:
        _end_ask_surface(win, bool(from_compact))
    except Exception:
        pass
    try:
        _show_share_code_window(win, ask.get("surl") or "", ask.get("url") or "",
                                ask.get("share_uk") or "",
                                deadline=ask.get("deadline"))
        try:
            setattr(win, "_share_ask_force_pick", bool(ask.get("force_pick")))
        except Exception:
            pass
        return True
    except Exception:
        return False


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

    精简模式（`_compact_win` 可用）：没有 `win._share_ask_dlg`，归属判定改用
    `cw.code_open_for()`（复用同一「同 surl 或同 uk」口径）；命中同一分享则
    `cw.leave_code()`（pop CODE 页回 HOME），日志语义与浮窗路径完全一致。
    CODE 页自身的倒计时/回调由 CompactWindow 内部管理，这里绝不再产生 decision。

    Qt 主线程调用；返回是否真的关掉了某扇窗。绝不抛异常。"""
    cw = _compact_win(win)
    if cw is not None:
        t_surl, t_uk = "", ""
        try:
            t_surl, t_uk = cw.code_open_for()
        except Exception:
            t_surl, t_uk = "", ""
        try:
            t_surl = str(t_surl or "").strip()
        except Exception:
            t_surl = ""
        try:
            t_uk = str(t_uk or "").strip()
        except Exception:
            t_uk = ""
        surl_s = str(surl or "").strip()
        uk_s = str(uk or "").strip()
        same = bool((surl_s and t_surl and surl_s == t_surl)
                    or (uk_s and t_uk and uk_s == t_uk))
        if same:
            try:
                cw.leave_code()
            except Exception:
                pass
            _share_log(win, note or "[分享] 已成功提取，提取码小窗已关闭（填写任务完成）")
            return True
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


def _queue_share_intent(win, intent):
    """忙时把手动拉起意图排进队列，由 `_drain` 在忙标志释放后按序补执行。

    同一时刻只允许一条分享管线：Alt+2/Alt+3 在忙时产生的**手动**意图绝不丢弃
    （旧行为只记一行「请稍候」后 return，这次按压永久丢失），而是排进
    `win._queued_share_intents`；`_share_invoke_busy` 释放后由 `_drain` 每 200ms
    的 tick 按 FIFO 补执行。列表惰性创建（轻量桩没有该属性），达到
    QUEUED_SHARE_INTENT_MAX 上限时不再排队并记一行。绝不抛异常。"""
    try:
        q = getattr(win, "_queued_share_intents", None)
        if not isinstance(q, list):
            q = []
            setattr(win, "_queued_share_intents", q)
        url = str(intent.get("url") or "")
        if len(q) >= QUEUED_SHARE_INTENT_MAX:
            _share_log(win, f"[分享] 排队的手动拉起已达上限，本次未排队: {url}")
            return
        q.append(intent)
        # 标签只用于日志说明是哪一路手势：意图自带 tag 优先，否则按 kind 反查
        # PENDING_SHARE_TAG（invoke→Alt+2 / pick→Alt+3）。
        _kind = "share_code" if intent.get("kind") == "pick" else "share"
        _tag = str(intent.get("tag") or PENDING_SHARE_TAG.get(_kind, _kind))
        _share_log(win,
                   f"[分享] 上一个拉起尚未结束，已排到其后自动拉起（{_tag}）: {url}")
    except Exception:
        pass


def _pop_queued_share_intent(win):
    """取出并移除队首的排队手动拉起意图；队列为空/属性缺失/不是列表一律 None。

    只做列表 `pop(0)`，不解释 dict 内容——kind 派发口径由 `_drain` 的补执行块
    统一决定。绝不抛异常。"""
    try:
        q = getattr(win, "_queued_share_intents", None)
        if isinstance(q, list) and q:
            return q.pop(0)
    except Exception:
        pass
    return None


def _call_start_share_pick(win, url, surl, pwd, manual=False, item=None,
                           force_pick=False):
    """调用 `win._start_share_pick` 并转发 `force_pick`；兼容不接受该参数的旧桩。

    真实实现接受 `force_pick`；既有大量测试把 `_start_share_pick` 换成不含该形参
    的桩。这里按签名判断：不接受时退回位置调用（挑选手势对桩不可观测，本就不会执行
    worker），绝不因签名差异让调用抛错。module 级定义使旧桩（未绑定本方法）可用。"""
    fn = win._start_share_pick
    accepts = True
    try:
        import inspect
        params = inspect.signature(fn).parameters
        accepts = ("force_pick" in params) or any(
            p.kind == inspect.Parameter.VAR_KEYWORD for p in params.values())
    except Exception:
        accepts = True
    if accepts:
        return fn(url, surl, pwd, manual=manual, item=item,
                  force_pick=force_pick)
    return fn(url, surl, pwd, manual=manual, item=item)


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


def _notify_fallback_allowed(hub, title):
    """兜底直投 `hub.q` 前的门控检查：镜像 `Hub.notify` 对标题的过滤口径。

    `hub.notify` 正常时会按 `notify_enabled` 总开关 + `NOTIFY_KEYS[title]` 过滤，
    但它写日志文件若抛异常，调用方会退到「直接投 `hub.q`」的兜底分支——那条路径
    原先不受任何开关约束，导致这些气泡无法关闭。本函数把同一套口径搬过来：

      - `state` 为 None（轻量宿主 / 测试桩）：不过滤（与 Hub.notify 一致）；
      - `notify_enabled` 为假：整体关闭，不投递；
      - title 命中 `NOTIFY_KEYS`：值可为单键或键元组，**全部为真**才允许
        （元组 = 分组开关 + 专用开关叠加，见 Hub.notify）；
      - title 不在映射内：只受总开关约束。

    读快照失败按「允许」处理：宁可送达也不静默丢通知（与兜底「不丢提示」的初衷
    一致）。只读 `Hub.NOTIFY_KEYS`，不改动 hub.py。
    """
    state = getattr(hub, "state", None)
    if state is None:
        return True
    try:
        cfg = state.snapshot()
    except Exception:
        return True
    if not cfg.get("notify_enabled", True):
        return False
    from ...hub import Hub
    key = Hub.NOTIFY_KEYS.get(title)
    keys = key if isinstance(key, (tuple, list)) else (key,)
    for k in keys:
        if k and not cfg.get(k, True):
            return False
    return True


def _share_notify_via(win, title, msg):
    """托盘气泡（module 级）：三级收口。

    1) 宿主既有 `_share_notify`（真实实现内部走 `hub.notify`，受通知总开关 +
       分组开关过滤）；
    2) `hub.notify`；
    3) 直接投 `hub.q`——给轻量宿主（测试桩只有 `hub.q`、既无 `_share_notify`
       也无 `hub.notify`）兜底，否则通知会被静默丢掉，用户看不到任何提示。
       兜底同样受 `notify_enabled` + `NOTIFY_KEYS` 门控（见 _notify_fallback_allowed）。
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
    if not _notify_fallback_allowed(getattr(win, "hub", None), title):
        return
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


def _manual_reinvoke_guard(win, surl, url, hotkey="Alt+2"):
    """手动手势（Alt+2/Alt+3）重复拉起拦截：需「再按一次」才强制拉起。

    同一分享本次运行已拉起过时：第一次按压只记一行 + 弹「重复」托盘提醒并进入
    「待确认」，**绝不拉起**；在再确认窗口内再按一次同一手势才强制拉起一次。
    这样误触（手滑再按一次 Alt+2）不会真的再下载一遍——旧行为「手动即明确同意、
    直接再下载」正是误触重复下载的根因；确需重下的人只需再按一次，比弹模态框更轻，
    也不打断连续操作。**未拉起过则一律放行**（首次拉起行为完全不变）。

    `hotkey`：本次手势的名字，只影响文案与「待确认」归属——只有**同一手势**的第二
    次按压才算确认；异手势按压视为它自己的第一次（布防改归它），绝不会替别的手势
    放行。默认 "Alt+2" 时文案逐字不变。

    返回 True = 已拦截（调用方必须立即 return，不拉起）；False = 放行。
    """
    key = str(surl or "").strip()
    if not key or not _share_already_launched(win, key):
        return False
    hk = str(hotkey or "Alt+2")
    now = time.time()
    armed = getattr(win, "_manual_reinvoke_armed", None)
    if (isinstance(armed, dict) and armed.get("surl") == key
            and str(armed.get("hotkey") or "Alt+2") == hk
            and (now - float(armed.get("at") or 0)) <= MANUAL_REARM_SEC):
        win._manual_reinvoke_armed = None
        _share_log(win, f"[分享] 已确认（第二次 {hk}），强制再次拉起: {key}")
        return False
    win._manual_reinvoke_armed = {"surl": key, "at": now, "hotkey": hk}
    _share_log(win,
               f"[分享] 该分享本次运行已拉起过，已拦下（不重复下载）；"
               f"如确需再拉一次，请在 {MANUAL_REARM_SEC} 秒内再按一次 {hk}: {key}")
    _share_notify_via(
        win, "重复的分享链接",
        f"{url}\n本次运行已拉起过该分享，已拦下避免重复下载。"
        f"如确需强制再拉一次，请再按一次 {hk} 确认。")
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
    标记，随后 `_show_share_code_window` 的兜底提示会静默，不再重复提醒。

    精简模式（`_compact_win` 可用）：主窗隐藏**不是**丢弃理由——强制显示小窗，
    CODE 页由紧随其后的 `_show_share_code_window` 进入（那里才有 surl/share_uk），
    绝不静默丢弃（spec §6.2 最后一条）。"""
    cw = _compact_win(win)
    if cw is not None:
        try:
            cw.show_home(raise_=True)
        except Exception:
            pass
        _share_log(win, "[分享] 精简模式：主界面未显示，改为在精简小窗中填写提取码")
        return
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


def _show_share_code_window(win, surl, url, share_uk, open_browser=False,
                           deadline=None):
    """把「缺提取码小窗」收敛到唯一入口（Qt 主线程调用）。

    - 已有小窗且属于同一分享 → 复用（不覆盖用户已填内容）；
    - 已有小窗属于别的分享 → 关掉旧的换新的（同一时刻只允许一个小窗）；
    - 主窗缺失/隐藏（托盘）/最小化时**不再悬浮独立小窗**：只记一行日志 +
      一条托盘气泡（hub.notify 路径），返回 None，由调用方既有「无码回退」继续；
    - `open_browser`：仅在新开/复用小窗之外需要「未知分享者探针」时置真；
      已在弹小窗时不重复开浏览器；实验性开启时 pan.baidu 网址绝不显式打开。

    精简模式（`_compact_win` 可用）：**不建任何浮窗**，也不走 `_share_parent_usable`
    的「主窗不可用就丢弃」路径——强制显示 CompactWindow 并进入/复用其 CODE 页
    （单例与 120s 倒计时由 CompactWindow 内部处理，`force_pick` 原样转交）；
    仍返回宿主对象（CompactWindow），不是 dialog。精简分支异常时记一行并回退下方
    原路径（非精简路径逐字不变）。

    任何异常都不向外抛，返回小窗对象或 None。"""
    cw = _compact_win(win)
    if cw is not None:
        try:
            cw.show_home(raise_=True)
            _force_pick = bool(getattr(win, "_share_ask_force_pick", False))
            _is_new_page = cw.enter_code(
                surl, url, share_uk, force_pick=_force_pick,
                open_browser=bool(open_browser), deadline=deadline)
            # 新开 CODE 页时 `enter_code` 内部已跑过一次未知分享者探针（实验性静默
            # 拦截的说明行也已在其中记过）；本块只补「复用」这一次——复用时
            # `enter_code` 跳过探针，才轮到本块探一次，绝不重复同一行。
            if open_browser and not _is_new_page:
                # 未知分享者：沿用既有「浏览器打开分享页做探针」行为。
                # UX-4：实验性开启时 pan.baidu 一律静默，绝不显式打开浏览器。
                if not _share_pan_open_blocked(win, url):
                    try:
                        import webbrowser
                        webbrowser.open(str(url), new=2)
                        _share_log(win, f"[分享] 未知分享者，已在浏览器打开分享页: {url}")
                    except Exception as e:
                        _share_log(win, f"[分享] 打开分享页失败: {e}")
            return cw
        except Exception as e:
            _share_log(win, f"[分享] 精简模式提取码页打开失败，回退浮窗: {e}")
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
                                     share_uk=share_uk,
                                     hub=getattr(win, "hub", None),
                                     deadline=deadline)
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
