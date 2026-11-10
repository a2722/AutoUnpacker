# -*- coding: utf-8 -*-
"""百度网盘分享「拉起客户端下载」全链路（2.F：把分享链接交给网盘客户端下载）。

职责：
- 给定一条公开分享链接（含提取码），走完「分享页 → 校验提取码 → 列文件 →
  分享转存下载（拿 filelist 令牌）→ 唤起客户端 → 轮询校验」的完整链路，最终让
  **百度网盘客户端**自己去下载该分享里的文件（不下载、不登录、不碰网页，绕开 IDM）。
- 链路按「准备 / 提交」两段拆开：`prepare_share()` 只跑到列文件并解析分享页，把
  可下载条目交给调用方（UI 可先让用户勾选）；`commit_download()` 再对选中条目做
  转存下载并唤起客户端（`/api/sharedownload` 本就接收 fid_list / path_list，子集
  与整包走的是同一套协议，无需新接口）。

关键入口：
- `prepare_share(share_url, pwd="")` → (ok, prep)：跑 **0~4 步**，返回文件清单；
- `commit_download(prep, fs_ids=None)` → (ok, detail)：跑 **5~10 步**，提交（子集）；
- `invoke_download(share_url, pwd="", on_wake=None)` → (ok, detail)：兼容保留的
  薄封装，等价于 prepare + 提交全部条目；`on_wake` 在唤起客户端成功之后、复核
  轮询之前回调一次（供上层「唤醒即通知」）。

依赖：标准库（http.cookiejar / json / os / re / subprocess / ssl / time /
      urllib.request / urllib.parse）。**不引入任何第三方依赖**。

注意：
- 本协议是抓包反推并**端到端验证通过**的，字段名 / 顺序 / 编码一律照搬，不要
  「优化」或改动（`filelist` 是不透明服务端令牌，必须原样回传客户端）；
- 两段共用同一个 CookieJar / OpenerDirector：`prepare_share` 把它放进 prep 的内部
  键 `_op`（连同原始分享链接 `_share_url`），`commit_download` 原样复用，保证请求
  会话与旧的单函数版本逐字节一致；
- 第 9 步唤起 `baiduyunguanjia://evoked-download/…` **前**会用 `AssocQueryStringW`
  做一次只读探测，把「协议关联到哪个处理器 / 有没有关联」写进日志，便于排查
  「点了没反应 / 弹商店窗口」。探测结果**不参与任何拦截**：无论查到什么都照旧
  `os.startfile`，主流程行为与旧版一致；
- **登录态保护门**：`commit_download` 在发出任何百度请求之前先做「客户端是否在
  运行」检查（`_WAKE_GUARD_ENABLED`，默认开）。客户端未运行时直接拒绝提交、一个
  请求都不发——因为此刻投递唤醒会用「无登录态」冷启动客户端，导致用户被迫重新
  登录。UI 可在整条流程开始前用 `client_ready()` 做只读预检；确需绕过时置
  `_WAKE_GUARD_ENABLED = False`；
- Referer 用分享链接（invoker 用站点根）；
- 铁律：本模块不向调用方抛异常，任何失败一律 `(False, 原因)`。
- §4 防反爬：`/share/verify` 每次调用最多发 **1 次**，失败即停（不换码重试）；
  两次 verify 之间强制间隔 ≥ `_VERIFY_MIN_GAP` 秒；一旦要求图形验证码立即放弃。
"""
import gzip
import http.cookiejar
import json
import logging
import os
import random
import re
import ssl
import subprocess
import threading
import time
import urllib.parse
import urllib.request
import zlib

from . import captcha
from .db import MIN_WAKE_VERSION, client_version, parse_version, version_below
from .manifest import detect_dead_share_page
from ..paths import DATA_DIR

# 本模块的进程内日志（版本探测软告警 / 诊断走标准 logging；未配置 handler 时
# 由 lastResort 兜底，绝不抛异常、绝不影响链路）。
_log = logging.getLogger(__name__)


# 公共查询串：chunlei Web 端固定参数，末尾的 `=` 不能省。
_Q = "channel=chunlei&web=1&app_id=250528&clienttype=0&bdstoken="
_S = "https://pan.baidu.com"
# UA 与抓包里的 Edge 一字不差：2026-11 起 /api/sharedownload 前的风控会看
# 客户端指纹一致性（UA 说是 Chrome 120、请求头却缺 sec-ch-ua/Sec-Fetch，
# 反而比"老老实实一个 UA"更可疑）。
_UA = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
       "(KHTML, like Gecko) Chrome/157.0.0.0 Safari/537.36 Edg/157.0.0.0")
_CH_UA = '"Not-A.Brand";v="99", "Microsoft Edge";v="157", "Chromium";v="157"'
# sharedownload 的 cv：抓包里 Web 端固定带 `ct=pcygj&cv=7.14.1.6`
# （pcygj = 网盘 PC 客户端通道）。我们不带上它时，请求看起来就是"裸网页 API"。
_DEFAULT_CV = "7.14.1.6"
# cookie 落盘文件名（DATA_DIR 下）：跨运行复用 BAIDUID/PANWEB/BDCLND 等，
# 让服务端看到"有历史的客户端"而不是每次全新设备。
_COOKIE_NAME = "baidu_cookies.txt"
_COOKIE_LOCK = threading.Lock()
# 分享链接路径段（/s/<surl_full>）；提取码从查询串取。
_SURL_RE = re.compile(r"/s/([A-Za-z0-9_-]+)")
# 百度「安全验证」页形态：/share/init?surl=<裸 surl>（前导 1 已被百度去掉）。
# 剪贴板里经常正是这种形态（如 …/share/init?surl=0S6Avqw…&pwd=abcd），它和
# `/s/1<surl>` 指向同一个分享；`manifest.parse_share_url` 早已两种都认
# （所以「已记录」能成功），这里必须跟上，否则拉起步（_parse_share）会误报
# 「不是有效的百度分享链接」。
_SURL_QUERY_RE = re.compile(r"[?&]surl=([A-Za-z0-9_-]+)")

# 新版分享的提取码是强制必填项：空码去提交必然失败。`prepare_share` 在死链判定之后、
# 任何 tplconfig/verify 请求之前就此短路，把原因交回上层去询问用户（供测试/上层引用）。
NEED_CODE_REASON = "该分享需要提取码"

# 分享被百度风控打标时的统一提示（risklabel / taskquery / list.hitrisk 共用）。
_RISK_BLOCKED_REASON = (
    "该分享已被百度风控标记（命中 risklabel），自动拉起大概率只会撞验证码："
    "建议先在浏览器里手动打开一次这个分享，稍后再试。")

# 唤起已发出、但后续 `/api/invoker/check` 复核返回 errno≠0 时的统一前缀。
# 上层据此把「唤醒已发出、客户端未确认接单」与普通拉起失败区分开，对已发出的
# 「已请求客户端下载」通知补一条纠正通知（时延整改：通知提前，复核降级为静默）。
CHECK_FAIL_PREFIX = "唤起后校验失败"

# 整条分享链路的聚合截止预算（秒）：prepare / commit 共享同一份预算
# （prepare_share 把它放进 prep 的内部键 `_deadline`）。单次 HTTP 请求的
# timeout=15 保持不变；本常量兜住「多步串联」的总时长（含 verify 节流、分页
# list、复核轮询），任一步骤异常变慢时链路会在预算耗尽后的下一个检查点终止，
# 不会把管线拖到无限长。
SHARE_CHAIN_DEADLINE_SEC = 90


def _chain_deadline(prep=None):
    """返回本次链路的聚合截止时刻（墙钟秒）。

    prep 里已有 `_deadline`（prepare_share 写入的内部键）时沿用同一预算，
    让 prepare → commit 两段共享一个总超时；否则从此刻起算一个新预算。
    **绝不抛异常**：返回 None 表示时钟不可用，调用方按「永不超时」处理。
    """
    try:
        d = float((prep or {}).get("_deadline"))
        if d > 0:
            return d
    except Exception:
        pass
    try:
        return time.time() + SHARE_CHAIN_DEADLINE_SEC
    except Exception:
        return None


def _deadline_hit(deadline):
    """聚合预算是否已耗尽（墙钟到点）。deadline 为 None → 永不判超时。绝不抛异常。"""
    try:
        return deadline is not None and time.time() >= deadline
    except Exception:
        return False


def _timeout_reason():
    """聚合超时的统一失败原因（上层原样展示给用户）。"""
    return f"分享链路超时（{SHARE_CHAIN_DEADLINE_SEC}s）"

# 调 tasklist 时的无窗口标志（与 extract.py 一致；老版本 Python 无此常量时退化 0）。
_CREATE_NO_WINDOW = getattr(subprocess, "CREATE_NO_WINDOW", 0)

# 百度网盘客户端进程映像名（本机实测：客户端运行时三者都在）。
# BaiduNetdisk.exe 是主客户端进程（登录态的宿主）；YunDetectService.exe /
# BaiduNetdiskHost.exe 是随主进程拉起的辅助进程。命中其中任意一个即视为
# 「客户端已在运行」——对「是否可安全投递」而言，辅助进程在也说明主链路已起。
# 这是**内置默认**：用户可用配置键 baidu_client_proc_names 覆盖（百度更换客户端
# 进程名时无需改代码，走 _effective_client_proc_names 解析生效列表）。
_DEFAULT_CLIENT_PROC_NAMES = ("BaiduNetdisk.exe", "YunDetectService.exe",
                              "BaiduNetdiskHost.exe")
# 兼容旧引用：默认列表的别名（真正生效的列表一律走 _effective_client_proc_names）。
_CLIENT_PROC_NAMES = _DEFAULT_CLIENT_PROC_NAMES

# 生效进程名列表的解析上限：单项 64 字符、最多 32 项（防脏配置撑爆匹配表）。
_CLIENT_PROC_NAME_MAX_LEN = 64
_CLIENT_PROC_NAME_MAX_ITEMS = 32


def _effective_client_proc_names(cfg=None):
    """返回生效的客户端进程映像名列表（**永不为空**，默认与内置三进程名逐字一致）。

    配置项 `baidu_client_proc_names`（字符串）的解析口径：
    - 半角 `,` 与全角 `，` 都当分隔符；逐项 strip、丢弃空项；
    - 大小写不敏感去重（保留首次出现的写法）；
    - 单项截 64 字符、最多 32 项；
    - 解析结果为空（键缺失 / 空串 / 纯分隔符 / 非字符串）时回退内置默认
      `_DEFAULT_CLIENT_PROC_NAMES`——**绝不返回空列表**。
    cfg 为 None 时按需读取 config.json（`load_config()`），读取失败同样回退
    内置默认。本函数绝不抛异常。
    """
    raw = None
    try:
        if cfg is None:
            from .. import config as app_config
            cfg = app_config.load_config()
        raw = (cfg or {}).get("baidu_client_proc_names")
    except Exception:
        raw = None
    if not isinstance(raw, str):
        raw = ""
    out = []
    seen = set()
    try:
        for part in raw.replace("，", ",").split(","):
            name = part.strip()[:_CLIENT_PROC_NAME_MAX_LEN]
            if not name or name.lower() in seen:
                continue
            seen.add(name.lower())
            out.append(name)
            if len(out) >= _CLIENT_PROC_NAME_MAX_ITEMS:
                break
    except Exception:
        out = []
    return out if out else list(_DEFAULT_CLIENT_PROC_NAMES)

# 客户端未运行时是否禁止投递唤醒（默认禁止，保护登录态）
_WAKE_GUARD_ENABLED = True


def _now_ms():
    """当前墙钟毫秒（各接口的 t 参数用）。"""
    return int(time.time() * 1000)


# ── §4 防反爬：verify 节流 ──────────────────────────────────────────────────
# 两次 /share/verify 之间的最小间隔（秒）。铁律是「一次分享最多 1 次 verify，
# 失败即停」，本常量只兜底「同一进程内先后多次调用」时的间隔，避免紧挨着重试。
_VERIFY_MIN_GAP = 1.0
_LAST_VERIFY_TS = 0.0


def _verify_wait():
    """verify POST 之前，把与上一次 verify 的间隔补足到 _VERIFY_MIN_GAP 秒。

    在后台 worker 线程内运行，允许短暂 time.sleep；等待量 = 目标间隔 - 已过
    时间，恒 ≤ _VERIFY_MIN_GAP（只补差值，不做惩罚性延长），且绝不抛异常。
    """
    global _LAST_VERIFY_TS
    try:
        gap = _VERIFY_MIN_GAP - (time.time() - _LAST_VERIFY_TS)
        if gap > 0:
            time.sleep(gap if gap < _VERIFY_MIN_GAP else _VERIFY_MIN_GAP)
        _LAST_VERIFY_TS = time.time()
    except Exception:
        pass


def _cookie_path():
    """cookie 落盘路径；拿不到目录时返回空串（退化为进程内 jar）。"""
    try:
        return str(DATA_DIR / _COOKIE_NAME)
    except Exception:
        return ""


def _load_cookiejar():
    """读取上次落盘的 cookie；文件缺失/损坏一律退化成空 jar，绝不抛异常。

    整段外层兜底：本机 `os` 可能被测试替换成桩、`DATA_DIR` 也可能不可写，
    这些都不该让「拿不到历史 cookie」升级成整条链路失败。
    """
    try:
        path = _cookie_path()
        if not path:
            return http.cookiejar.CookieJar()
        jar = http.cookiejar.MozillaCookieJar(path)
        try:
            exists = os.path.exists(path)
        except Exception:
            exists = False
        if exists:
            try:
                with _COOKIE_LOCK:
                    jar.load(ignore_discard=True, ignore_expires=True)
            except Exception:
                jar = http.cookiejar.MozillaCookieJar(path)
        return jar
    except Exception:
        return http.cookiejar.CookieJar()


def _persist(op):
    """把 opener 的 CookieJar 落盘（失败静默：cookie 只是"锦上添花"）。"""
    try:
        cj = getattr(op, "_cj", None)
        if not isinstance(cj, http.cookiejar.MozillaCookieJar):
            return
        with _COOKIE_LOCK:
            cj.save(ignore_discard=True, ignore_expires=True)
    except Exception:
        pass


def _build_opener(jar=None):
    """构造共用 OpenerDirector（CookieJar + 忽略证书校验的 HTTPS）。

    默认**复用落盘的 cookie jar**：浏览器每次请求都带着稳定的 BAIDUID/PANWEB，
    我们如果每次新建空 jar，等于"每跑一次都是一个全新设备首次访问"，风控分直接拉满。
    """
    ctx = ssl.create_default_context()
    ctx.check_hostname = False
    ctx.verify_mode = ssl.CERT_NONE
    cj = jar if jar is not None else _load_cookiejar()
    op = urllib.request.build_opener(
        urllib.request.HTTPCookieProcessor(cj),
        urllib.request.HTTPSHandler(context=ctx))
    op.addheaders = [("User-Agent", _UA)]
    try:
        op._cj = cj
    except Exception:
        pass
    return op


def _req_headers(referer, ctype=None):
    """对齐 Edge 的请求头。

    抓包（2026-11-10）里浏览器每次必带、而 urllib 默认缺失的正是这几项：
    Origin / X-Requested-With / Accept / Accept-Language /
    Accept-Encoding(=identity 是 Python 的招牌) / Sec-Fetch-* / sec-ch-ua*。
    """
    h = {
        "User-Agent": _UA,
        "Accept": "application/json, text/plain, */*",
        "Accept-Language": "zh-CN,zh;q=0.9,en;q=0.8",
        "Accept-Encoding": "gzip, deflate",
        "Connection": "keep-alive",
        "Origin": _S,
        "Referer": referer,
        "X-Requested-With": "XMLHttpRequest",
        "sec-ch-ua": _CH_UA,
        "sec-ch-ua-mobile": "?0",
        "sec-ch-ua-platform": '"Windows"',
        "Sec-Fetch-Site": "same-origin",
        "Sec-Fetch-Mode": "cors",
        "Sec-Fetch-Dest": "empty",
    }
    if ctype:
        h["Content-Type"] = ctype
    return h


def _decode_body(resp):
    """按 Content-Encoding 解压（我们声明了 gzip/deflate，就必须自己解）。"""
    raw = resp.read()
    enc = (resp.headers.get("Content-Encoding") or "").lower()
    try:
        if "gzip" in enc:
            raw = gzip.decompress(raw)
        elif "deflate" in enc:
            try:
                raw = zlib.decompress(raw)
            except Exception:
                raw = zlib.decompress(raw, -zlib.MAX_WBITS)
    except Exception:
        pass
    return raw.decode("utf-8", "replace")


def _human_gap(lo=2.0, hi=3.5):
    """验证码重试之间的人类节奏抖动。

    实测（2026-11-10）：同一分享 40 秒内连发 3 次 sharedownload 必吃
    errno=-20，隔 2 分钟单发又恢复正常 —— 说明拦截主要看**频率**。
    所以这里给 2.0~3.5s 的随机间隔，宁可慢一点也别把风控分越推越高。
    """
    try:
        time.sleep(random.uniform(lo, hi))
    except Exception:
        pass


def _get(op, url, referer):
    """GET 文本（gzip 容错）。"""
    rq = urllib.request.Request(url, headers=_req_headers(referer))
    with op.open(rq, timeout=15) as r:
        text = _decode_body(r)
    _persist(op)
    return text


def _post(op, url, data, referer):
    """POST 表单（urlencoded，gzip 容错）。data 用 dict（保持插入顺序）。"""
    body = urllib.parse.urlencode(data).encode()
    rq = urllib.request.Request(url, data=body, headers=_req_headers(
        referer, "application/x-www-form-urlencoded; charset=UTF-8"))
    with op.open(rq, timeout=15) as r:
        text = _decode_body(r)
    _persist(op)
    return text


def _bridge_mode(cfg=None):
    """取「下载请求通道」配置：off / headless / visible（默认 headless）。"""
    try:
        if cfg is None:
            from ..config import load_config
            cfg = load_config()
        m = str(cfg.get("baidu_bridge_mode") or "headless").strip().lower()
        return m if m in ("off", "headless", "visible") else "headless"
    except Exception:
        return "headless"


def _bridge_fetch_token(share_url, prep, fids, paths, mode, captcha_hook):
    """用真 Edge 在页面里重放「取 filelist 令牌」；失败返回 {'ok': False}。

    只在直连被风控（errno=-20）时调用。**绝不抛异常**：桥挂了就当没发生，
    调用方继续走原来的 OCR 兜底。
    """
    try:
        from . import browser_bridge
        if not browser_bridge.available():
            return {"ok": False, "reason": "没有找到 Edge"}

        def _solver(img):
            solved = captcha.solve(img)
            if solved is None:
                return None
            text, conf = solved
            if captcha.is_ambiguous(text, conf):
                return None
            return (text, conf)

        def _ev(e):
            payload = dict(e) if isinstance(e, dict) else {"kind": "event"}
            payload["via"] = "bridge"
            _emit_captcha(captcha_hook, payload)

        return browser_bridge.share_download_prep(
            share_url, prep.get("pwd") or "", fids, paths, mode=mode,
            timeout=90.0, vcode_solver=_solver, on_event=_ev, max_vcode=2)
    except Exception as e:
        return {"ok": False, "reason": "浏览器桥异常：%s" % e}


def _risklabel_blocked(op, uk, share_id, pwd, referer):
    """分享风控标记预检（2026-11 新握手）；确认被标记时返回中文原因。

    浏览器点「下载」之前必走 `/share/risklabel` → `/share/taskquery?scene=risk_label`，
    命中标记时随后的 sharedownload 会直接甩验证码。我们提前问一次：
    被标记就**一个 sharedownload 都不发**地失败，省掉用户 OCR 和一次风控分。

    只做提前止损：接口不存在 / errno≠0 / 字段缺失 / 异常 → 一律返回 None（放行），
    绝不因为一次探测失败就挡住正常下载。
    """
    try:
        qp = urllib.parse.quote(str(pwd or ""))
        q = f"uk={uk}&share_id={share_id}&pwd={qp}&{_Q}"
        j = json.loads(_post(op, f"{_S}/share/risklabel?{q}", {}, referer))
        if not isinstance(j, dict) or j.get("errno") not in (0, None):
            return None
        if j.get("hit_risklabel") == 1:
            return _RISK_BLOCKED_REASON
        taskid = j.get("taskid")
        if taskid:
            q2 = (f"uk={uk}&share_id={share_id}&taskid={taskid}"
                  f"&scene=risk_label&pwd={qp}&{_Q}")
            j2 = json.loads(_post(op, f"{_S}/share/taskquery?{q2}", {}, referer))
            if isinstance(j2, dict) and j2.get("hit_risklabel") == 1:
                return _RISK_BLOCKED_REASON
    except Exception:
        return None
    return None


def _parse_share(share_url, pwd):
    """把分享链接拆成 (surl_full, surl, pwd)。

    两种链接形态都要认（同一分享的两种写法，端到端等价）：
    - `/s/<surl_full>` 形态：surl_full = `/s/` 之后的完整段；
    - `/share/init?surl=<裸 surl>` 形态（百度「安全验证」页，剪贴板里最常见）：
      查询串里的值就是**裸 surl**（前导 1 已被百度去掉），据此还原
      surl_full = `1` + 裸 surl（与日志里 `/s/1<surl>` 的重建口径一致）。

    - surl_full = 上述完整段（`/share/tplconfig` 用）；
    - surl      = 去掉开头一个 `1`（`/share/verify`、`/share/list` 用）；
    - pwd       = 参数优先，其次 URL 查询串里的 `pwd`。
    解析不出路径段时抛 ValueError（由外层统一转成 (False, …)）。
    """
    u = str(share_url or "").strip()
    m = _SURL_RE.search(u)
    if m:
        surl_full = m.group(1)
    else:
        m2 = _SURL_QUERY_RE.search(u)
        if not m2:
            raise ValueError("不是有效的百度分享链接")
        surl_full = "1" + m2.group(1)
    surl = surl_full[1:] if surl_full.startswith("1") else surl_full
    if not pwd:
        try:
            q = urllib.parse.parse_qs(urllib.parse.urlsplit(u).query)
            pwd = (q.get("pwd") or [""])[0]
        except Exception:
            pwd = ""
    return surl_full, surl, str(pwd or "")


def is_need_code_reason(reason):
    """失败原因是否属于「空码闸门」给出的「该分享需要提取码」。绝不抛异常。"""
    try:
        return str(reason or "").strip() == NEED_CODE_REASON
    except Exception:
        return False


# 「偶发网络超时 / 连接层失败」的失败标记（小写比对）。分享链路里的 urlopen 超时、
# 连接被重置/拒绝、DNS 解析失败等属于**偶发网络问题**，与「死链」「缺码」等确定性
# 失败不同：链接本身没问题，稍后重试即可。上层据此换成更友好、可操作的提示文案。
# 刻意**不含**泛化的「网络错误」前缀——那类原因信息量不足，仍走通用文案。
NETWORK_TIMEOUT_MARKERS = (
    "timed out", "timeout", "urlopen error",
    "connection reset", "connection aborted", "connection refused",
    "remote end closed", "remote disconnected",
    "getaddrinfo", "name resolution", "temporary failure in name",
    "eof occurred", "read operation timed out",
    "网络超时", "连接超时", "连接被重置", "连接被拒绝", "无法连接",
)


def is_network_timeout_reason(reason):
    """失败原因是否属于「偶发网络超时 / 连接失败」。绝不抛异常。

    仅用于把失败提示换成更友好的文案（链接本身没坏、稍后重试即可），
    **不改变任何重试行为**——分享链路维持「一次失败即停」的防反爬口径。
    """
    try:
        t = str(reason or "").lower()
        return any(m in t for m in NETWORK_TIMEOUT_MARKERS)
    except Exception:
        return False


def _html_ids(html):
    """从分享页 HTML 取 (share_uk, shareid)（`window.yunData` / locals 都能命中）。

    真实页面里数字写成 `"share_uk":"1102408115653","shareid":21895586648`
    （**键带引号**），而 window.yunData 里是 `share_uk: data.share_uk`（变量引用）。
    旧正则要求 `share_uk` 后紧跟 `=`/`:`，对带引号键两种都命中不了 → 第 4 步会误判
    「未解析到 share_uk / shareid」而整条准备流程失败。两种写法都要认。"""
    t = html or ""
    uk = re.search(r'"share_uk"\s*:\s*"?(\d+)', t) or \
        re.search(r'share_uk\s*[=:]\s*["\']?(\d+)', t)
    sid = re.search(r'"shareid"\s*:\s*"?(\d+)', t) or \
        re.search(r'shareid\s*[=:]\s*["\']?(\d+)', t)
    return (uk.group(1) if uk else None, sid.group(1) if sid else None)


def prepare_share(share_url, pwd=""):
    """分享「准备」段：跑完 0~4 步，只列清单、不下载（供 UI 先勾选文件）。

    流程（与旧 invoke_download 的 0~4 步逐字节一致）：
    0. 预热：先访问分享页（可能 404），吞异常只为拿 cookie；
    1. `/share/tplconfig` 取 sign / timestamp；
    2. `/share/verify` 校验提取码 → randsk（sekey）；§4：本次调用最多 1 次 verify，
       失败即停，两次 verify 之间至少间隔 `_VERIFY_MIN_GAP` 秒；
    3. `/share/list` 分页收齐分享根目录下的**全部**条目（按 fs_id 去重、保序）；
    4. 分享页取 share_uk / shareid（不在 URL 里，只能从 HTML 解析）。

    `pwd` 为空时**不再发起任何 tplconfig/verify 请求**：新版分享的提取码是强制
    必填项，空码提交只会白烧本次调用唯一的一次、且间隔受限的 verify 配额，多发
    一次注定失败的请求，还可能被风控视为非人类。此时直接返回
    `(False, NEED_CODE_REASON)`，把原因交回上层去询问用户；step0 的那一次页面
    GET 仍会发生（用于死链判定，死链结论优先于缺码）。

    返回 (ok: bool, data)：
    - 成功：data 为 dict，公开键：
      {"surl_full","surl","pwd","sign","ts","randsk","share_uk","shareid",
       "entries":[{"fs_id","path","name","size","isdir"}, …]}
      entries 顺序 = 服务端返回顺序（已按 fs_id 去重，保持现行为）；`isdir=True`
      的目录项也保留（勾选目录 = 整目录下载）。另有三个下划线内部键：`_op`
      （共用 OpenerDirector）、`_share_url`（原始分享链接）与 `_client_version`
      （本次链路开始时软探测的客户端版本，供 commit_download 进成功文案；
      未知为 None），供 commit_download 原样复用，UI 无需关心、也不应改动。
    - 失败：data 为 str（中文原因）。**绝不抛异常。**
    """
    try:
        surl_full, surl, pwd = _parse_share(share_url, pwd)
        share_url = str(share_url or "").strip()
        referer_share = share_url
        op = _build_opener()
        # 聚合预算：整条 prepare 从这里起算；写进 prep 让 commit 共享同一预算。
        deadline = _chain_deadline()

        # 0. 预热：先访问分享页（可能 404），吞掉异常只为拿 cookie。
        #    （2.F）顺手留住页面 HTML 做「失效分享」探测：命中即在 verify 之前短路
        #    返回——绝不消耗本流程唯一一次、且间隔受限的 verify 配额。
        if _deadline_hit(deadline):
            return False, _timeout_reason()
        try:
            page0 = _get(op, f"{_S}/s/{surl_full}?pwd={pwd}", referer_share)
        except Exception:
            page0 = ""
        dead = detect_dead_share_page(page0)
        if dead:
            return False, dead

        # 空码闸门：新版分享的提取码是强制必填项，拿空 pwd 去 verify 必然失败
        # （只会白烧本次调用唯一的一次配额、多发一次注定失败的请求）。
        # 没有码就到此为止，交回上层去询问用户。位置刻意排在死链判定之后：
        # 这样「裸链接」也能先拿到准确的「链接已失效」结论，而不是误报成缺码。
        if not str(pwd or "").strip():
            return False, NEED_CODE_REASON

        # 1. tplconfig：取 sign / timestamp。字段未齐时给出明确中文原因，
        #    不再让裸 ["data"] 取值抛 KeyError 被外层兜成英文 "'data'"。
        if _deadline_hit(deadline):
            return False, _timeout_reason()
        tpl = json.loads(_get(
            op,
            f"{_S}/share/tplconfig?surl={surl_full}"
            f"&fields=sign,timestamp,jsToken"
            f"&view_mode=1&{_Q}",
            referer_share))
        if not isinstance(tpl, dict):
            return False, "分享信息获取失败（返回结构异常）"
        errno = tpl.get("errno")
        if errno not in (None, 0):
            return False, f"分享信息获取失败（errno={errno}）"
        d = tpl.get("data")
        if not isinstance(d, dict) or "sign" not in d or "timestamp" not in d:
            return False, "分享信息获取失败（未返回 sign/timestamp）"
        sign, ts = d["sign"], d["timestamp"]
        # 被风控盯上时服务端会额外下发 jsToken，浏览器会原样带进
        # sharedownload；正常会话里这一项为空。
        js_token = str(d.get("jsToken") or "")

        # 2. verify：校验提取码 → randsk（即 sekey），后面 extra 要用。
        #    §4：本次调用最多 1 次 verify——失败即停，不换码、不重试；
        #    两次 verify 之间至少间隔 _VERIFY_MIN_GAP 秒。
        if _deadline_hit(deadline):
            return False, _timeout_reason()
        _verify_wait()
        try:
            vbody = _post(
                op,
                f"{_S}/share/verify?surl={surl}&{_Q}&t={_now_ms()}&bioc=1",
                {"pwd": pwd, "vcode": "", "vcode_str": ""},
                referer_share)
        except Exception as e:
            return False, ("网络错误：share/verify 请求失败（不是提取码错误）："
                           + str(e).replace("\n", " ").strip())
        try:
            vj = json.loads(vbody)
        except Exception:
            return False, "share/verify 返回内容无法解析（不是提取码错误）"
        if not isinstance(vj, dict):
            return False, "share/verify 返回结构异常（不是提取码错误）"
        if (vj.get("vcode_str") or vj.get("vcode")) or "验证码" in (vbody or ""):
            return False, "需要图形验证码：本次已放弃（不再重试，避免触发风控）"
        verr = vj.get("errno")
        if verr not in (None, 0):
            # 空码闸门（见本文件 step 0.5）已保证走到这里 pwd 必非空，
            # 因此原「未提供提取码」分支不可达，已删除。
            return False, f"提取码校验失败（errno={verr}）：提取码可能不正确"
        randsk = vj.get("randsk")
        if not randsk:
            return False, f"share/verify 未返回 randsk（errno={vj.get('errno')}）"

        # 3. list：分页收齐分享根目录下的**全部**条目（整包 / 全选下载）。
        #    每页 50 条，按 fs_id 去重、保持服务端返回顺序；停止条件：
        #    无 list / errno≠0 / 本页不足 50 条 / 页数达到硬上限 20（防死循环）。
        #    这里同时保留每条的元数据（fs_id / path / name / size / isdir），
        #    供 UI 展示与后续「子集提交」使用；字段一律防御式取值，缺字段不崩。
        entries = []
        seen_fs = set()
        for p in range(1, 21):
            if _deadline_hit(deadline):
                return False, _timeout_reason()
            try:
                resp = json.loads(_get(
                    op,
                    f"{_S}/share/list?web=5&app_id=250528&desc=1&showempty=0"
                    f"&page={p}&num=50&order=time&shorturl={surl}&root=1"
                    f"&view_mode=1&{_Q}",
                    referer_share))
            except Exception:
                break
            if resp.get("errno"):
                break
            # 服务端在这一步就把"是否被风控打标"告诉页面了（浏览器正常返回 0）。
            # 打标时继续下载只会换来验证码，直接给出可执行的提示。
            if resp.get("hitrisk") == 1:
                return False, _RISK_BLOCKED_REASON
            lst = resp.get("list")
            if not isinstance(lst, list):
                break
            for it in lst:
                if not isinstance(it, dict):
                    continue
                fs = it.get("fs_id")
                path = it.get("path")
                if fs is None or path is None or fs in seen_fs:
                    continue
                seen_fs.add(fs)
                name = it.get("server_filename")
                try:
                    size = int(it.get("size"))
                except Exception:
                    size = 0
                entries.append({
                    "fs_id": str(fs),
                    "path": path,
                    "name": name if name is not None else "",
                    "size": size,
                    "isdir": bool(it.get("isdir")),
                })
            if len(lst) < 50:
                break
        if not entries:
            return False, "分享列表为空"

        # 4. 分享页取 share_uk / shareid（不在 URL 里，只能从 HTML 解析）。
        if _deadline_hit(deadline):
            return False, _timeout_reason()
        try:
            page = _get(op, f"{_S}/s/{surl_full}?pwd={pwd}", referer_share)
        except Exception:
            page = ""
        share_uk, shareid = _html_ids(page)
        if not (share_uk and shareid):
            return False, "分享页未解析到 share_uk / shareid（链接可能失效或需提取码）"

        # 4.5 风控标记预检：确认被标记就到此为止，绝不把一次注定撞验证码的
        #     sharedownload 发出去（B 档）。
        if _deadline_hit(deadline):
            return False, _timeout_reason()
        blocked = _risklabel_blocked(op, share_uk, shareid, pwd, referer_share)
        if blocked:
            return False, blocked

        # 客户端版本软探测（detect-and-report，不属于链路步骤）：每次准备只探测
        # 一次，结果随 prep 带到提交段进成功文案（None = 版本未知，文案保持干净）。
        # 只读、极快、绝不阻断任何后续步骤（见 _probe_client_version）。
        ver = _probe_client_version()

        return True, {
            "surl_full": surl_full,
            "surl": surl,
            "pwd": pwd,
            "sign": sign,
            "ts": ts,
            "jsToken": js_token,
            "randsk": randsk,
            "share_uk": share_uk,
            "shareid": shareid,
            "entries": entries,
            "_op": op,
            "_share_url": share_url,
            "_deadline": deadline,
            "_client_version": ver,
        }
    except Exception as e:
        return False, str(e)


def list_share_dir(prep, path):
    """分享「按需展开」：列出分享内某个子目录（`dir=` 参数）。

    复用 `prepare_share` 建立的会话（`prep["_op"]` 里已 verify 过的 cookie），
    所以展开目录**不会再多花一次 verify**，一次展开通常只花 1 个 list 请求。
    UI 懒加载就用它：用户点开哪层才列哪层。

    path 为分享内的绝对路径（形如 "/教程分享/video"）。
    返回 (ok: bool, entries|reason)：
      成功 -> entries 与 prepare_share 的 entries 同构
              （{"fs_id","path","name","size","isdir"}，按服务端顺序、按 fs_id 去重）
      失败 -> reason 为中文原因。
    只做只读列表：不下载、不重试、绝不抛异常。
    注意：`prep["_op"]` 是不可序列化的会话句柄，不要把 prep 写进 JSON。
    """
    try:
        op = prep.get("_op") if isinstance(prep, dict) else None
        surl = prep.get("surl") if isinstance(prep, dict) else None
        if op is None or not surl:
            return False, "会话已失效（请重新 prepare_share）"
        p0 = str(path or "").strip()
        if not p0 or not p0.startswith("/"):
            return False, "目录路径必须以 / 开头"
        referer = prep.get("_share_url") or ""
        entries = []
        seen_fs = set()
        for p in range(1, 21):
            try:
                resp = json.loads(_get(
                    op,
                    f"{_S}/share/list?web=5&app_id=250528&desc=1&showempty=0"
                    f"&page={p}&num=200&order=time&shorturl={surl}"
                    f"&root=0&dir={urllib.parse.quote(p0, safe='')}&view_mode=1&{_Q}",
                    referer))
            except Exception as e:
                if entries:
                    break
                return False, "列目录失败：" + str(e).replace("\n", " ").strip()
            if not isinstance(resp, dict):
                break
            if resp.get("errno"):
                if entries:
                    break
                return False, f"列目录被拒绝（errno={resp.get('errno')}）"
            lst = resp.get("list")
            if not isinstance(lst, list):
                break
            for it in lst:
                if not isinstance(it, dict):
                    continue
                fs = it.get("fs_id")
                pth = it.get("path")
                if fs is None or pth is None or fs in seen_fs:
                    continue
                seen_fs.add(fs)
                name = it.get("server_filename")
                try:
                    size = int(it.get("size"))
                except Exception:
                    size = 0
                entries.append({
                    "fs_id": str(fs),
                    "path": pth,
                    "name": name if name is not None else "",
                    "size": size,
                    "isdir": bool(it.get("isdir")),
                })
            if len(lst) < 200:
                break
        if not entries:
            return False, "该目录为空或不可见"
        return True, entries
    except Exception as e:
        return False, str(e)


def _client_running(cfg=None):
    """百度网盘客户端是否已在运行（只读进程表，无第三方依赖）。

    用 `tasklist` 过滤生效的客户端进程名（`_effective_client_proc_names`：默认
    内置三进程名，用户可用配置键 baidu_client_proc_names 覆盖）；任何异常一律
    返回 False（保守：宁可不唤起）。
    `BaiduNetdisk.exe` 是主客户端进程，另外两个是随它拉起的辅助进程；本机实测
    客户端运行时三者都在，命中任意一个即视为「客户端已在运行」。
    中文 Windows 的 tasklist 输出可能是 GBK，解码按 GBK → cp936 → UTF-8 依次尝试，
    最后兜底 UTF-8/replace，绝不让解码错误逃逸。
    """
    try:
        r = subprocess.run(
            ["tasklist", "/FO", "CSV", "/NH"],
            stdout=subprocess.PIPE, stderr=subprocess.DEVNULL,
            creationflags=_CREATE_NO_WINDOW, timeout=10)
        raw = r.stdout or b""
    except Exception:
        return False
    text = None
    for enc in ("gbk", "cp936", "utf-8"):
        try:
            text = raw.decode(enc)
            break
        except Exception:
            continue
    if text is None:
        try:
            text = raw.decode("utf-8", "replace")
        except Exception:
            return False
    low = text.lower()
    for name in _effective_client_proc_names(cfg):
        if name.lower() in low:
            return True
    return False


def client_ready(cfg=None):
    """给 UI 用的只读检查：客户端是否可安全投递（现在是「客户端必须在运行」）。

    返回 bool，**绝不抛异常**；`_WAKE_GUARD_ENABLED = False`（门已关）时恒为 True。
    cfg 可显式传入配置快照（缺省时按需读配置），便于测试注入。
    """
    try:
        if not _WAKE_GUARD_ENABLED:
            return True
        # 默认路径保持 `_client_running()` 的无参调用（既有测试/调用方以 0 参桩替换）；
        # 只有显式传入 cfg 时才透传。
        if cfg is None:
            return bool(_client_running())
        return bool(_client_running(cfg))
    except Exception:
        return False


def _wake_detail(bid, seq, version=None):
    """唤起客户端后的统一简述（`on_wake` 回调与成功返回值**共用同一措辞**）。

    抽成助手是为了让「唤醒即通知」的通知文案与链路最终 `ok` 的文案逐字一致，
    避免两处各写一遍字符串日后漂移。探测到客户端版本（`client_version()`）时
    带上「客户端 x.y.z」；版本未知时保持原文案（不带版本，逐字兼容旧行为）。
    """
    if version:
        return (f"已唤起客户端下载（客户端 {version}，"
                f"browserId={bid}, seq={seq}）")
    return f"已唤起客户端下载（browserId={bid}, seq={seq}）"


def _probe_client_version():
    """链路准备阶段的客户端版本软探测：记录日志并返回版本串（探测不到返回 None）。

    只做「探测 + 记录」，**绝不阻断 / 延迟 / 重试**任何一步：
    - 探测到版本：info 一行；已知版本低于 `MIN_WAKE_VERSION`（百度网页端
      GUANJIA_VERSION_COMPARE 的软阈值）时再加**一条** warning，仅提示；
    - 探测不到：debug 一行，成功文案不带版本。
    任何异常一律吞掉并按「版本未知」处理。
    """
    try:
        ver = client_version()
    except Exception:
        ver = None
    try:
        if ver:
            _log.info("百度网盘客户端版本：%s", ver)
            pv = parse_version(ver)
            if pv is not None and version_below(pv, MIN_WAKE_VERSION):
                _log.warning(
                    "百度网盘客户端版本 %s 低于 %s：新版「唤起下载」可能不被支持"
                    "（仅提示，不阻断）", ver,
                    ".".join(str(x) for x in MIN_WAKE_VERSION))
        else:
            _log.debug("未能探测到百度网盘客户端版本（继续正常唤起）")
    except Exception:
        pass
    return ver


def _protocol_client_exe(proto="baiduyunguanjia"):
    """只读查询该协议关联的处理器 exe；查不到 / 异常返回 None。

    用 `AssocQueryStringW`（资源管理器自己的解析器）：它天然处理 ProgID 间接层
    （`baiduyunguanjia` 默认值 → ProgID → shell\\open\\command），不会像「直接读
    `HKCR\\baiduyunguanjia\\shell\\open\\command`」那样漏判形态 B。

    **仅用于诊断日志**，不参与任何拦截判断（作用域：只增强可排查性，不改主流程）。
    """
    try:
        import ctypes
        from ctypes import wintypes

        ASSOCSTR_EXECUTABLE = 2

        buf = ctypes.create_unicode_buffer(1024)
        n = wintypes.DWORD(len(buf))
        hr = ctypes.windll.shlwapi.AssocQueryStringW(
            0, ASSOCSTR_EXECUTABLE, proto, "open", buf,
            ctypes.byref(n)) & 0xFFFFFFFF
        if hr == 0 and buf.value:
            return buf.value
        return None
    except Exception:
        return None


def _wake_client(uri):
    """唤起 baiduyunguanjia 协议。**只补诊断日志，不拦截、不改主流程。**

    背景：旧实现直接 `os.startfile`。协议没注册（客户端没装 / 重装系统后没修）时
    Windows 会弹「在 Microsoft Store 中查找应用」，而 `os.startfile` 不抛异常 ——
    表面上像成功，日志里却没有任何线索，非常难排查。

    因此这里只在唤起**前**做一次只读探测，把结果写进日志：
    - 能解析出处理器且文件存在 → debug 记录路径；
    - 能解析但文件不存在 → warning（客户端可能被卸载/移动）；
    - 完全查不到关联 → warning（可能没装 / 协议未注册）。
    然后**无条件**照旧 `os.startfile` —— 探测结果绝不影响是否唤起。

    返回 (ok, reason)：仅当 `os.startfile` 自身抛异常时返回 False。绝不抛异常。
    """
    try:
        # 诊断段**完全隔离**：任何异常（探测失败、os 被替换成桩、日志系统异常…）
        # 都只吞掉，绝不影响下面的唤起 —— 否则「增强排查」反而会挡住主流程。
        try:
            exe = _protocol_client_exe()
            if exe and os.path.isfile(exe):
                _log.debug("唤起客户端前：协议关联处理器 = %s", exe)
            elif exe:
                _log.warning(
                    "唤起客户端前：协议关联指向的处理器不存在（%s）——客户端可能已被"
                    "卸载或移动。仍按原流程尝试唤起", exe)
            else:
                _log.warning(
                    "唤起客户端前：未查到 baiduyunguanjia 协议关联——客户端可能未安装或"
                    "协议未注册。仍按原流程尝试唤起；若没弹出客户端，请安装/修复百度网盘客户端")
        except Exception:
            pass
        os.startfile(str(uri))
        return True, ""
    except Exception as e:
        return False, "唤起百度网盘客户端失败：%s" % e


def _filelist_token(sd):
    """从 sharedownload 响应里取 filelist 令牌（字符串）；取不到返回 None。

    正常结构是 `{"list": "<不透明令牌>"}`；极少数返回为 list/dict，退化为取
    首个元素的 dlink。绝不抛异常。"""
    try:
        blob = sd.get("list")
        if isinstance(blob, str) and blob:
            return blob
        first = blob[0] if isinstance(blob, list) else blob
        blob = first.get("dlink") if isinstance(first, dict) else None
        return blob or None
    except Exception:
        return None


def _emit_captcha(hook, ev):
    """上报一条验证码事件给调用方；回调自身异常一律吞掉，绝不影响链路。"""
    if hook is None:
        return
    try:
        hook(ev)
    except Exception:
        pass


def commit_download(prep, fs_ids=None, pairs=None, on_wake=None,
                    captcha_hook=None, captcha_manual=None):
    """分享「提交」段：用 prep 调 sharedownload 并唤起客户端（步骤 5~10）。

    选中规则（优先级从高到低）：
    - `pairs` 非 None：显式给出 `[(fs_id, path), ...]`，**可来自任意层级**。
      懒加载展开出来的子目录条目不在 `prep["entries"]` 里，只有这条路径才能
      提交嵌套文件。`fid_list` / `path_list` 按给定顺序一一对应；
      过滤掉残缺项后若为空，返回 `(False, "未选中任何可下载条目")`。
    - `fs_ids` 为 None：提交 `prep["entries"]` 的全部条目（整包，兼容旧行为）；
    - `fs_ids` 非空：只提交命中集合的条目——按 `prep["entries"]` 的顺序过滤，
      `fid_list` / `path_list` 一一对应；若一个都没命中，返回
      `(False, "未选中任何可下载条目")`，**绝不**退化成「下载全部」。

    流程（与旧 invoke_download 的 5~10 步逐字节一致）：
    5. `/api/sharedownload` 转存下载 → `list` 字段（字符串令牌 = filelist）；
    6. `/api/invoker/get` → browserId；
    7. `/api/invoker/online` 上报在线；
    8. `/api/invoker/send` 投递 downloadInfo → seq；
    9. `_wake_client()` 唤起客户端：唤起前把协议关联探测结果写进日志（只诊断，
       不拦截），随后照旧 `os.startfile`，紧接着
       **立即回调 `on_wake(detail)`**（在复核轮询之前，供上层「唤醒即通知」）。
       成功文案带上 prepare 阶段探测到的客户端版本「客户端 x.y.z」（未知则不带；
       低于软阈值只在 prepare 阶段记一条 warning，绝不阻断）。
    10. 复核轮询 `/api/invoker/check`：最多 3 次、每次间隔 1.5s（上限 4.5s），
        status==2 提前跳出；errno≠0 才算失败；超时容忍（视为已唤起）。

    参数：
    - `on_wake`：可选回调。唤起成功后、复核轮询之前调用一次，收到
      `_wake_detail(bid, seq, 版本或 None)` 字符串；回调自身异常一律吞掉，
      绝不影响链路。
    - `captcha_hook`：可选回调 `hook(event: dict)`。sharedownload 命中风控
      （errno=-20）进入图形验证码重试时，每一轮的跳过 / 识别 / 手动结果都会
      以事件字典上报（kind: skip/try/manual），供上层记日志与调试通知；
      回调异常一律吞掉。
    - `captcha_manual`：可选回调 `manual(img_bytes, attempt) -> str | None`。
      自动 OCR 用尽仍失败时调用一次（用户手输兜底）：返回 4 位文本即带
      vcode_input/vcode_str 再提交一次；返回 None（取消）或仍失败则按原
      失败分支返回。工作线程内同步调用，UI 需自行投递回主线程弹窗。

    返回 (ok: bool, detail: str)：成功时 detail 为简述，失败时为原因。**绝不抛异常。**
    """
    try:
        # 登录态保护门：在发出任何百度请求（第 5 步 sharedownload 起）之前先检查
        # 客户端是否在运行。未运行就直接返回——此刻投递唤醒会冷启动一个无登录态
        # 的客户端，导致用户被迫重新登录；既然不可能成功，就一个请求都不花。
        if _WAKE_GUARD_ENABLED and not _client_running():
            return False, ("网盘客户端未在运行：已跳过拉起（避免用无登录态的方式冷启动客户端，"
                           "那会导致你被迫重新登录）。请先手动打开百度网盘客户端后重试。")

        # 聚合预算：prep 带 `_deadline`（prepare 起的）则共享同一总预算，
        # 否则从此刻起算。每一步网络请求前都检查，超时立即失败（绝不发起新请求）。
        deadline = _chain_deadline(prep)

        entries = prep.get("entries") if isinstance(prep, dict) else None
        if not isinstance(entries, list) or not entries:
            return False, "分享列表为空"

        # 选定要提交的条目。
        # - `pairs` 优先：显式 (fs_id, path)，**可来自任意层级**（懒加载展开的子目录
        #   条目不在 prep["entries"] 里，只有它才能提交嵌套文件）；
        # - 否则按 `fs_ids` 在根层 entries 里过滤（保持旧行为与顺序）。
        if pairs is not None:
            selected = []
            for it in pairs:
                # 必须是真正的二元组：字符串也能按下标取值，不校验会被静默当成一对。
                if not isinstance(it, (tuple, list)) or len(it) < 2:
                    continue
                fid, pth = it[0], it[1]
                if fid is None or not isinstance(pth, str) or not pth.strip():
                    continue
                selected.append({"fs_id": str(fid), "path": pth})
            if not selected:
                return False, "未选中任何可下载条目"
        elif fs_ids is None:
            selected = entries
        else:
            want = set()
            for x in fs_ids:
                want.add(str(x))
            selected = [e for e in entries
                        if isinstance(e, dict) and str(e.get("fs_id")) in want]
            if not selected:
                return False, "未选中任何可下载条目"

        # 还原为服务端原始数值型 fs_id，保证提交字节与旧版一致。
        fids, paths = [], []
        for e in selected:
            paths.append(e.get("path"))
            v = e.get("fs_id")
            try:
                v = int(v)
            except Exception:
                pass
            fids.append(v)

        sign = prep.get("sign")
        ts = prep.get("ts")
        randsk = prep.get("randsk")
        share_uk = prep.get("share_uk")
        shareid = prep.get("shareid")
        share_url = prep.get("_share_url") or f"{_S}/s/{prep.get('surl_full')}"
        referer_share = share_url
        referer_api = _S + "/"
        op = prep.get("_op")
        if op is None:
            op = _build_opener()

        # 5. sharedownload：转存下载 → list 字段（字符串令牌 = filelist）。
        #    把选中的 fs_id / path 一次性提交：服务端只回一个不透明的 `list`
        #    令牌，该令牌已覆盖 fid_list 里的每一个条目，客户端据此下载。
        #    百度单次上限约 999 个（超了会返回 31075），此处不拆分、原样提交；
        #    失败时照实返回错误，不静默丢弃任何条目。
        if _deadline_hit(deadline):
            return False, _timeout_reason()
        sd_url = (f"{_S}/api/sharedownload?{_Q}"
                  f"&sign={urllib.parse.quote(sign)}&timestamp={ts}")
        js_token = str(prep.get("jsToken") or "")
        if js_token:
            sd_url += "&jsToken=" + urllib.parse.quote(js_token)
        sd_form = {"encrypt": "1",
                   "extra": json.dumps({"sekey": urllib.parse.unquote(randsk)},
                                       ensure_ascii=False),
                   "product": "share",
                   # 浏览器固定带这两项（pcygj = 网盘 PC 客户端通道 + 客户端版本）；
                   # 缺了它们，服务端看到的是一句"裸网页 API 调用"。
                   "ct": "pcygj",
                   "cv": str(prep.get("_client_version") or _DEFAULT_CV),
                   "timestamp": str(ts),
                   "uk": share_uk,
                   "primaryid": shareid,
                   "fid_list": json.dumps(fids),
                   "path_list": json.dumps(paths)}
        sd = json.loads(_post(op, sd_url, sd_form, referer_share))
        blob = _filelist_token(sd)

        # 5.05 浏览器桥（实验性）：直连被风控时，把「取令牌」交给真 Edge 在页面里
        #      重放一次（页面自带 ab_sr / ndut_fmt / jsToken，是直连永远拿不到的）。
        #      桥失败**不改任何后续行为**，照旧走下面的 OCR 兜底。
        if not blob and sd.get("errno") == -20:
            _bm = _bridge_mode()
            if _bm != "off":
                br = _bridge_fetch_token(share_url, prep, fids, paths, _bm,
                                         captcha_hook)
                if br.get("ok"):
                    blob = br.get("list") or ""
                    _log.info("浏览器桥已取到 filelist 令牌（mode=%s）", _bm)
                    _emit_captcha(captcha_hook, {"kind": "bridge", "ok": True,
                                                 "mode": _bm})
                else:
                    _log.info("浏览器桥未成功（mode=%s）：%s", _bm, br.get("reason"))
                    _emit_captcha(captcha_hook, {"kind": "bridge", "ok": False,
                                                 "mode": _bm,
                                                 "reason": br.get("reason")})

        # 5.1 风控图形验证码（errno=-20）：自动 OCR 重试，最多 MAX_ATTEMPTS 张。
        #     必须复用同一个 opener（同一 CookieJar）：vcode_str 与会话绑定。
        #     易混字符（v/y、8/b…）或低置信度**不硬猜**，直接换下一张；每轮
        #     结果都经 captcha_hook 上报（含成功），供上层观察成功率。
        if not blob and sd.get("errno") == -20:
            for attempt in range(1, captcha.MAX_ATTEMPTS + 1):
                if _deadline_hit(deadline):
                    break
                if attempt > 1:
                    _human_gap()
                vcode_str, img = captcha.fetch_vcode(op, share_url)
                if not vcode_str or not img:
                    _emit_captcha(captcha_hook, {
                        "kind": "skip", "attempt": attempt, "text": "",
                        "confidence": 0.0, "reason": "fetch_fail"})
                    continue
                solved = captcha.solve(img)
                if solved is None:
                    # 无可用 OCR 解释器 / 子进程失败：不硬猜，换下一张。
                    _emit_captcha(captcha_hook, {
                        "kind": "skip", "attempt": attempt, "text": "",
                        "confidence": 0.0, "reason": "no_ocr"})
                    continue
                text, conf = solved
                if captcha.is_ambiguous(text, conf):
                    _emit_captcha(captcha_hook, {
                        "kind": "skip", "attempt": attempt, "text": text,
                        "confidence": conf, "reason": "ambiguous"})
                    continue
                if _deadline_hit(deadline):
                    break
                form2 = dict(sd_form)
                form2["vcode_input"] = text
                form2["vcode_str"] = vcode_str
                sd = json.loads(_post(op, sd_url, form2, referer_share))
                blob = _filelist_token(sd)
                _emit_captcha(captcha_hook, {
                    "kind": "try", "attempt": attempt, "text": text,
                    "confidence": conf, "ok": bool(blob),
                    "errno": sd.get("errno"),
                    "show_msg": sd.get("show_msg")})
                if blob or sd.get("errno") != -20:
                    # 成功，或已变成其它错误（不再是验证码问题）→ 退出重试。
                    break

        # 5.2 自动用尽仍是验证码问题 → 手动输入兜底（UI 弹窗，仅当上层给了回调）。
        #     回调返回空串 = 用户点「换一张」：重新取图再问（手动同样最多 3 张，
        #     vcode_str 始终与当前这张图配对，绝不错配提交）。
        if not blob and sd.get("errno") == -20 and captcha_manual is not None:
            for mi in range(1, captcha.MAX_ATTEMPTS + 1):
                if _deadline_hit(deadline):
                    break
                if mi > 1:
                    _human_gap()
                vcode_str, img = captcha.fetch_vcode(op, share_url)
                if not vcode_str or not img:
                    break
                manual_text = None
                try:
                    manual_text = captcha_manual(img, captcha.MAX_ATTEMPTS + mi)
                except Exception:
                    manual_text = None
                if manual_text is None:
                    _emit_captcha(captcha_hook, {
                        "kind": "manual",
                        "attempt": captcha.MAX_ATTEMPTS + mi,
                        "ok": False, "cancelled": True,
                        "errno": sd.get("errno"),
                        "show_msg": sd.get("show_msg")})
                    break
                manual_text = str(manual_text).strip()
                if not manual_text:
                    continue  # 换一张
                form2 = dict(sd_form)
                form2["vcode_input"] = manual_text
                form2["vcode_str"] = vcode_str
                sd = json.loads(_post(op, sd_url, form2, referer_share))
                blob = _filelist_token(sd)
                _emit_captcha(captcha_hook, {
                    "kind": "manual", "attempt": captcha.MAX_ATTEMPTS + mi,
                    "ok": bool(blob), "cancelled": False,
                    "errno": sd.get("errno"),
                    "show_msg": sd.get("show_msg")})
                if blob or sd.get("errno") != -20:
                    break

        if not blob:
            # 失败信息带全关键字段（errno / show_msg / request_id），便于排查
            # 是验证码问题还是别的风控/参数错误。
            return False, (
                f"sharedownload 未取到 filelist 令牌"
                f"（errno={sd.get('errno')}，"
                f"show_msg={sd.get('show_msg')}，"
                f"request_id={sd.get('request_id')}）")

        # 6. invoker/get：拿 browserId。
        if _deadline_hit(deadline):
            return False, _timeout_reason()
        bid = json.loads(_get(
            op, f"{_S}/api/invoker/get?{_Q}&t={_now_ms()}", referer_api))["browserId"]
        if not bid:
            return False, "invoker/get 未取到 browserId"

        # 7. invoker/online：上报客户端在线。
        if _deadline_hit(deadline):
            return False, _timeout_reason()
        _get(op, f"{_S}/api/invoker/online?browserId={bid}&{_Q}&t={_now_ms()}",
             referer_api)

        # 8. invoker/send：把下载令牌投递给客户端 → seq。
        if _deadline_hit(deadline):
            return False, _timeout_reason()
        info = {"method": "DownloadShareItems", "uk": "0", "checkuser": False,
                "filelist": blob, "share_url": share_url,
                "src_from": "wp-download_web_share",
                "src_type": "web_sharelink_page"}
        j6 = json.loads(_post(
            op, f"{_S}/api/invoker/send?{_Q}&t={_now_ms()}",
            {"browserId": bid,
             "downloadInfo": json.dumps(info, ensure_ascii=False,
                                        separators=(",", ":"))},
            referer_api))
        seq = j6.get("seq")
        if seq is None:
            return False, f"invoker/send 未返回 seq（errno={j6.get('errno')}）"

        # 9. 唤起客户端下载。
        if _deadline_hit(deadline):
            return False, _timeout_reason()
        # 客户端版本：prepare_share 已在链路开始时软探测一次并随 prep 带来
        # （手工构造的 prep 视为版本未知）——只进成功文案，绝不阻断 / 延迟唤醒。
        ver = prep.get("_client_version") if isinstance(prep, dict) else None
        wake = (f"baiduyunguanjia://evoked-download/?browserId={bid}&seq={seq}"
                f"&src_from=wp-download_web_share&src_type=web_sharelink_page")
        ok_wake, wake_err = _wake_client(wake)
        if not ok_wake:
            # 系统拒绝了唤起（最常见：客户端未安装 / 协议没注册）。绝不能继续把
            # 「已请求客户端下载」回报给用户 —— 那正是旧实现在协议缺失时的假成功。
            return False, wake_err

        # 9.5 唤醒已发出：先回调 on_wake（在复核轮询之前），让上层立即通知用户
        #     「已请求客户端下载」。复核只作为静默兜底，不再阻塞这条告知。
        if on_wake is not None:
            try:
                on_wake(_wake_detail(bid, seq, ver))
            except Exception:
                pass

        # 10. 复核 check：status==2 即客户端已接单；errno≠0 视为失败；超时容忍。
        #     最多 3 次（上限 4.5s）：通知已在唤起那一刻发出，这里只是静默复核，
        #     没必要再白等满 12s。
        err = None
        for _ in range(3):
            # 预算耗尽就不再白等复核：唤醒已发出，按既有的「超时容忍」成功返回。
            if _deadline_hit(deadline):
                break
            time.sleep(1.5)
            try:
                chk = json.loads(_get(
                    op,
                    f"{_S}/api/invoker/check?browserId={bid}&seq={seq}"
                    f"&{_Q}&t={_now_ms()}",
                    referer_api))
            except Exception:
                continue
            if chk.get("errno") not in (None, 0):
                err = chk.get("errno")
                break
            if chk.get("status") == 2:
                break
        if err is not None:
            return False, f"{CHECK_FAIL_PREFIX}（errno={err}）"
        return True, _wake_detail(bid, seq, ver)
    except Exception as e:
        return False, str(e)


def invoke_download(share_url, pwd="", on_wake=None,
                    captcha_hook=None, captcha_manual=None):
    """兼容保留：prepare_share + commit_download(prep, None) 的薄封装。

    签名与 (ok, detail) 返回值、以及所有现有调用方都不受影响——行为等价于旧的
    「下载该分享的全部条目」。需要让用户挑选文件时，请改用 `prepare_share()` 拿
    `entries`，再用 `commit_download(prep, 选中的 fs_id 列表)` 提交子集。
    `on_wake` 原样透传给 `commit_download`（唤醒成功后、复核轮询之前调用一次）；
    `captcha_hook` / `captcha_manual` 同样原样透传（图形验证码事件与手动兜底）。
    固定流程编号见 prepare_share（0~4）与 commit_download（5~10）。
    **绝不抛异常。**
    """
    ok, prep = prepare_share(share_url, pwd)
    if not ok:
        return False, prep
    return commit_download(prep, None, on_wake=on_wake,
                           captcha_hook=captcha_hook,
                           captcha_manual=captcha_manual)
