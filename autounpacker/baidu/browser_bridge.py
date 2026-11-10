# -*- coding: utf-8 -*-
"""浏览器桥：借真 Edge 的手发百度网盘请求（页面内 fetch）。

## 为什么需要它

2026-11 起 `/api/sharedownload` 的风控要看"页面会话"：`ab_sr`、`ndut_fmt`、
`jsToken` 全部由页面 JS 生成，裸 HTTP 客户端永远拿不到，所以会频繁撞
`errno=-20`（图形验证码）。而真浏览器打开同一个分享页就没有验证码。

本模块做的事：起一个**独立 profile 的 Edge**（不碰用户自己的浏览器、不需要登录），
打开分享页，然后**在页面上下文里** `fetch()` 那几个接口 —— 请求的 cookie、指纹、
TLS/HTTP2 特征全部由浏览器原生提供。

## 两种模式（用户可选）

- `headless`（默认，静默）：`--headless=new`，实测 **0 个可见窗口**，任务栏无图标；
  再把 headless 的三个马脚补掉：UA 里的 `HeadlessChrome`、`screen 800x600`、
  缺失的 `navigator.userAgentData`。
- `visible`（真身）：普通窗口，看得见；指纹 100% 真实，适合 headless 仍被拦时用。

## 纪律

- 任何失败都只返回原因，**绝不抛异常到调用方**，更不允许影响主链路；
- 进程常驻复用（首次 ~0.2s 起端口，之后每次 0.3~0.8s）；闲置 `IDLE_CLOSE_SEC`
  秒后自动关掉，不长期占 130MB；
- Edge 找不到 / 起不来 / CDP 超时 → 一律判"桥不可用"，调用方原样走旧链路。
"""

import json
import logging
import os
import random
import socket
import subprocess
import sys
import threading
import time
import urllib.parse

from . import netpref
from .cdp import CDP, CDPError, WebSocket
from ..paths import DATA_DIR

_log = logging.getLogger(__name__)

# Edge 启动参数里用到的常量（UA 与抓包里 Edge 157 一字不差）。
_UA = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
       "(KHTML, like Gecko) Chrome/157.0.0.0 Safari/537.36 Edg/157.0.0.0")
_UA_META = {
    "brands": [
        {"brand": "Not-A.Brand", "version": "99"},
        {"brand": "Microsoft Edge", "version": "157"},
        {"brand": "Chromium", "version": "157"},
    ],
    "fullVersionList": [
        {"brand": "Not-A.Brand", "version": "99.0.0.0"},
        {"brand": "Microsoft Edge", "version": "157.0.0.0"},
        {"brand": "Chromium", "version": "157.0.0.0"},
    ],
    "fullVersion": "157.0.4341.42",
    "platform": "Windows",
    "platformVersion": "15.0.0",
    "architecture": "x86",
    "bitness": "64",
    "mobile": False,
    "model": "",
}

# headless 专属：把缺失的 navigator.userAgentData 补成 Edge 的形态。
# 这是唯一"伪造"的一项——headless 模式下该 API 整个不存在，属明显特征。
_UA_DATA_SHIM = r"""
(() => {
  try {
    if (navigator.userAgentData) return;
    const brands = %s;
    const full = %s;
    const uad = {
      brands: brands,
      mobile: false,
      platform: 'Windows',
      getHighEntropyValues: () => Promise.resolve({
        brands: brands, fullVersionList: full, mobile: false,
        platform: 'Windows', platformVersion: '15.0.0',
        architecture: 'x86', bitness: '64', model: '',
        uaFullVersion: '157.0.4341.42', wow64: false
      }),
      toJSON: () => ({ brands: brands, mobile: false, platform: 'Windows' })
    };
    Object.defineProperty(navigator, 'userAgentData',
                          { get: () => uad, configurable: true });
  } catch (e) {}
})();
""" % (json.dumps(_UA_META["brands"]), json.dumps(_UA_META["fullVersionList"]))

_VALID_MODES = ("off", "headless", "visible")
# 闲置多久自动关掉浏览器（秒）。太短会反复冷启动，太长白占内存。
IDLE_CLOSE_SEC = 180.0
# 页面加载 / 单次页面内 fetch 的上限
LOAD_TIMEOUT = 25.0
FETCH_TIMEOUT = 30.0

_EDGE_CANDIDATES = (
    r"C:\Program Files (x86)\Microsoft\Edge\Application\msedge.exe",
    r"C:\Program Files\Microsoft\Edge\Application\msedge.exe",
)

_lock = threading.RLock()
_sess = None            # 当前会话（_Session）
_last_used = 0.0


# ---------------------------------------------------------------- Edge 定位
def find_edge():
    """定位 msedge.exe：环境变量 → 注册表 App Paths → 常见安装路径。"""
    env = (os.environ.get("AUTOUNPACKER_EDGE") or "").strip()
    if env and os.path.isfile(env):
        return env
    try:
        import winreg
        for root in (winreg.HKEY_LOCAL_MACHINE, winreg.HKEY_CURRENT_USER):
            for view in (0, getattr(winreg, "KEY_WOW64_32KEY", 0)):
                try:
                    key = winreg.OpenKey(
                        root,
                        r"SOFTWARE\Microsoft\Windows\CurrentVersion\App Paths\msedge.exe",
                        0, winreg.KEY_READ | view)
                    with key:
                        p = winreg.QueryValue(key, None)
                        if p and os.path.isfile(p):
                            return p
                except OSError:
                    continue
    except Exception:
        pass
    for p in _EDGE_CANDIDATES:
        if os.path.isfile(p):
            return p
    return None


def available():
    """桥是否可用（只查 Edge 是否存在，不启动）。"""
    return find_edge() is not None


def _profile_dir():
    try:
        d = os.path.join(str(DATA_DIR), "edge-profile")
        os.makedirs(d, exist_ok=True)
        return d
    except Exception:
        return os.path.join(os.environ.get("TEMP", "."), "autounpacker-edge-profile")


def _free_port():
    s = socket.socket()
    try:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]
    finally:
        s.close()


def _http_json(url, timeout=2.0):
    import urllib.request
    with urllib.request.urlopen(url, timeout=timeout) as r:
        return json.loads(r.read().decode("utf-8", "replace"))


# ---------------------------------------------------------------- 会话
def _launch_args(edge, mode, port, profile, map_ip=None):
    """拼 Edge 启动参数（纯函数，便于离线测试）。

    map_ip 非空时加 --host-resolver-rules=MAP pan.baidu.com <ip>：
    把百度域名**钉死到 IPv4**（百度对 v6 出站的分享下载几乎必弹验证码；
    实测 --disable-ipv6 无效，只有 MAP 规则生效）。
    """
    args = [edge]
    if mode == "headless":
        args += ["--headless=new",
                 "--user-agent=" + _UA,
                 "--window-size=1600,900",
                 "--force-device-scale-factor=1"]
    else:
        args += ["--window-size=1280,860", "--window-position=120,80"]
    args += [
        "--remote-debugging-port=%d" % port,
        "--user-data-dir=%s" % profile,
        "--no-first-run", "--no-default-browser-check",
        "--disable-extensions", "--disable-sync",
        "--disable-features=msEdgeSidebarV2,msHubApps",
        "--no-proxy-server",
    ]
    if map_ip:
        args.append("--host-resolver-rules=MAP pan.baidu.com " + str(map_ip))
    args.append("about:blank")
    return args


class _Session(object):
    """一个常驻 Edge 实例 + 一个已附着的页面 target。"""

    def __init__(self, mode):
        self.mode = mode
        self.proc = None
        self.port = 0
        self.cdp = None
        self.target_id = ""
        self.session_id = ""
        self.edge = ""
        self.started_at = 0.0
        self.page_url = ""
        self.map_ip = ""

    # ---- 启动 ----
    def start(self):
        edge = find_edge()
        if not edge:
            raise CDPError("没有找到 Edge（msedge.exe）")
        self.edge = edge
        profile = _profile_dir()
        # 优先 IPv4：先挑一个**能连上**的 A 记录，再用 MAP 规则把它钉死。
        # 拿不到就退回系统默认解析（有 v6 也照用，绝不让桥因为网络偏好起不来）。
        self.map_ip = ""
        try:
            if netpref.prefer_ipv4():
                self.map_ip = netpref.resolve_ipv4_first("pan.baidu.com", 443,
                                                         timeout=1.5) or ""
        except Exception:
            self.map_ip = ""
        last_err = None
        for _ in range(3):
            port = _free_port()
            args = _launch_args(edge, self.mode, port, profile, self.map_ip)
            try:
                self.proc = subprocess.Popen(
                    args,
                    stdout=subprocess.DEVNULL,
                    stderr=subprocess.DEVNULL,
                    creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
            except OSError as e:
                last_err = "启动 Edge 失败: %s" % e
                continue
            ws_url = None
            t0 = time.time()
            while time.time() - t0 < 12.0:
                if self.proc.poll() is not None:
                    break
                try:
                    v = _http_json("http://127.0.0.1:%d/json/version" % port)
                    ws_url = v.get("webSocketDebuggerUrl")
                    if ws_url:
                        break
                except Exception:
                    time.sleep(0.15)
            if not ws_url:
                last_err = "调试端口未就绪（端口 %d）" % port
                self._kill()
                continue
            try:
                self.port = port
                self.cdp = CDP(WebSocket.connect(ws_url, timeout=8.0))
                self._prepare_target()
                self.started_at = time.time()
                return self
            except CDPError as e:
                last_err = "CDP 附着失败: %s" % e
                self._kill()
                continue
        raise CDPError(last_err or "Edge 起不来")

    def _prepare_target(self):
        """建一个空白 target 并附着；headless 下顺手补齐指纹细节。"""
        t = self.cdp.call("Target.createTarget", {"url": "about:blank"})
        self.target_id = t["targetId"]
        s = self.cdp.call("Target.attachToTarget",
                          {"targetId": self.target_id, "flatten": True})
        self.session_id = s["sessionId"]
        self.cdp.call("Runtime.enable", session=self.session_id)
        self.cdp.call("Page.enable", session=self.session_id)
        self.cdp.call("Network.enable", session=self.session_id)
        if self.mode == "headless":
            # ① 屏幕尺寸（headless 默认 800x600，属明显特征）
            try:
                self.cdp.call("Emulation.setDeviceMetricsOverride", {
                    "width": 1536, "height": 864, "deviceScaleFactor": 1,
                    "mobile": False, "screenWidth": 1920,
                    "screenHeight": 1080}, session=self.session_id)
            except CDPError:
                pass
            # ② UA + Client Hints（命令行已改 UA，这里再兜一层）
            try:
                self.cdp.call("Network.setUserAgentOverride", {
                    "userAgent": _UA,
                    "acceptLanguage": "zh-CN,zh;q=0.9,en;q=0.8",
                    "platform": "Win32",
                    "userAgentMetadata": _UA_META}, session=self.session_id)
            except CDPError:
                pass
            # ③ navigator.userAgentData（headless 下完全缺失）
            try:
                self.cdp.call("Page.addScriptToEvaluateOnNewDocument",
                              {"source": _UA_DATA_SHIM}, session=self.session_id)
            except CDPError:
                pass

    # ---- 健康 ----
    def alive(self):
        if self.proc is None or self.proc.poll() is not None:
            return False
        return self.cdp is not None

    # ---- 导航 ----
    def navigate(self, url, timeout=LOAD_TIMEOUT):
        self.cdp.call("Page.navigate", {"url": url}, session=self.session_id,
                      timeout=timeout)
        t0 = time.time()
        while time.time() - t0 < timeout:
            time.sleep(0.2)
            try:
                r = self.cdp.call(
                    "Runtime.evaluate",
                    {"expression": "document.readyState", "returnByValue": True},
                    session=self.session_id, timeout=10.0)
                if (r.get("result") or {}).get("value") == "complete":
                    self.page_url = url
                    return True
            except CDPError:
                break
        raise CDPError("页面加载超时: %s" % url)

    # ---- 页面内执行 ----
    def eval_js(self, expr, await_promise=False, timeout=FETCH_TIMEOUT):
        r = self.cdp.call("Runtime.evaluate", {
            "expression": expr,
            "awaitPromise": bool(await_promise),
            "returnByValue": True,
        }, session=self.session_id, timeout=timeout)
        if r.get("exceptionDetails"):
            raise CDPError("页面脚本异常: %s" % (
                r["exceptionDetails"].get("text") or "unknown"))
        return (r.get("result") or {}).get("value")

    def page_fetch(self, path, method="GET", form=None, timeout=FETCH_TIMEOUT):
        """在页面里 fetch 同源接口，返回 (status:int, json|None, text)。"""
        headers = {"X-Requested-With": "XMLHttpRequest"}
        body = None
        if form is not None:
            body = urllib.parse.urlencode(form)
            headers["Content-Type"] = (
                "application/x-www-form-urlencoded; charset=UTF-8")
        opt = {"method": method, "credentials": "include", "headers": headers}
        if body is not None:
            opt["body"] = body
        expr = (
            "(async () => { try {"
            "  const r = await fetch(%s, %s);"
            "  const t = await r.text();"
            "  return JSON.stringify({status: r.status, body: t});"
            "} catch (e) { return JSON.stringify({status: -1, error: String(e)}); }"
            "})()" % (json.dumps(path), json.dumps(opt)))
        raw = self.eval_js(expr, await_promise=True, timeout=timeout)
        if not raw:
            raise CDPError("页面 fetch 无返回")
        d = json.loads(raw)
        status = int(d.get("status") or -1)
        text = d.get("body") or ""
        if status < 0:
            raise CDPError("页面 fetch 失败: %s" % (d.get("error") or "?"))
        try:
            return status, json.loads(text), text
        except Exception:
            return status, None, text

    def page_fetch_bytes(self, path, timeout=FETCH_TIMEOUT):
        """在页面里取二进制（验证码图片），返回 bytes。"""
        expr = (
            "(async () => { try {"
            "  const r = await fetch(%s, {credentials:'include'});"
            "  const b = new Uint8Array(await r.arrayBuffer());"
            "  let s = '';"
            "  for (let i = 0; i < b.length; i++) s += String.fromCharCode(b[i]);"
            "  return btoa(s);"
            "} catch (e) { return ''; }"
            "})()" % json.dumps(path))
        raw = self.eval_js(expr, await_promise=True, timeout=timeout)
        if not raw:
            return b""
        import base64
        try:
            return base64.b64decode(raw)
        except Exception:
            return b""

    def cookies(self):
        """页面当前所有 cookie（CDP Storage.getCookies）。"""
        try:
            r = self.cdp.call("Storage.getCookies", {}, timeout=10.0)
            out = {}
            for c in r.get("cookies") or []:
                if "baidu.com" in (c.get("domain") or ""):
                    out[c.get("name")] = c.get("value")
            return out
        except CDPError:
            return {}

    def close(self):
        try:
            if self.cdp is not None and self.target_id:
                self.cdp.call("Target.closeTarget",
                              {"targetId": self.target_id}, timeout=5.0)
        except Exception:
            pass
        self._kill()

    def _kill(self):
        try:
            if self.cdp is not None:
                self.cdp.close()
        except Exception:
            pass
        self.cdp = None
        p, self.proc = self.proc, None
        if p is not None:
            try:
                p.terminate()
                p.wait(timeout=6)
            except Exception:
                try:
                    p.kill()
                except Exception:
                    pass


# ---------------------------------------------------------------- 全局入口
def _ensure(mode):
    """拿到可用会话（必要时启动）；调用方已在 _lock 内。"""
    global _sess
    if _sess is not None and _sess.mode == mode and _sess.alive():
        return _sess
    if _sess is not None:
        _sess.close()
        _sess = None
    s = _Session(mode)
    s.start()
    _sess = s
    return s


def close():
    """关掉常驻浏览器（幂等；进程退出时也会调）。"""
    global _sess
    with _lock:
        if _sess is not None:
            try:
                _sess.close()
            except Exception:
                pass
            _sess = None


def _idle_watchdog():
    """后台线程：闲置过久就关掉，避免长期占内存。"""
    global _sess
    while True:
        time.sleep(15.0)
        try:
            with _lock:
                if _sess is not None and not _sess.alive():
                    _sess = None
                if _sess is not None and (time.time() - _last_used) > IDLE_CLOSE_SEC:
                    _log.info("浏览器桥闲置 %.0fs，关闭 Edge", time.time() - _last_used)
                    _sess.close()
                    _sess = None
        except Exception:
            pass


_watchdog_started = False


def _start_watchdog():
    global _watchdog_started
    if _watchdog_started:
        return
    _watchdog_started = True
    t = threading.Thread(target=_idle_watchdog, name="baidu-bridge-idle",
                         daemon=True)
    t.start()
    try:
        import atexit
        atexit.register(close)
    except Exception:
        pass


# ---------------------------------------------------------------- 对外 API
def share_download_prep(share_url, pwd, fids, paths, mode="headless",
                        timeout=60.0, vcode_solver=None, on_event=None,
                        max_vcode=3):
    """在真浏览器里跑完「拿 filelist 令牌」这一段。

    返回 dict，**永远不抛异常**：
      成功: {"ok": True, "list": "<令牌>", "sign","ts","jsToken","sekey",
             "uk","shareid","errno":0,"request_id":...}
      失败: {"ok": False, "reason": "中文原因", "errno": <可能为 None>}

    它做的事与页面完全一致：
      打开分享页 → yunData 取 uk/shareid → tplconfig 取 sign/timestamp/jsToken
      → 若有提取码则 verify 取 sekey（页面已有 BDCLND 时直接用 cookie）
      → sharedownload（encrypt=1 / product=share / ct=pcygj / cv=...）
    """
    global _last_used
    mode = str(mode or "").strip().lower()
    if mode not in _VALID_MODES or mode == "off":
        return {"ok": False, "reason": "浏览器桥未启用"}
    if not available():
        return {"ok": False, "reason": "没有找到 Edge，无法使用浏览器桥"}
    _start_watchdog()
    deadline = time.time() + float(timeout or 60.0)
    with _lock:
        _last_used = time.time()
        try:
            s = _ensure(mode)
            _last_used = time.time()
            if not s.navigate(share_url, timeout=min(LOAD_TIMEOUT, max(5.0, deadline - time.time()))):
                return {"ok": False, "reason": "分享页加载失败"}

            # 1) 页面自带的 yunData（uk / shareid / loginstate / bdstoken）
            yun = {}
            try:
                raw = s.eval_js("JSON.stringify(window.yunData || {})")
                yun = json.loads(raw) if raw else {}
            except Exception:
                yun = {}
            uk = yun.get("share_uk") or yun.get("uk") or ""
            shareid = yun.get("shareid") or ""

            # 2) tplconfig（sign / timestamp / jsToken）
            surl_full = _surl_full(share_url)
            q = (f"/share/tplconfig?surl={urllib.parse.quote(surl_full)}"
                 f"&fields=sign,timestamp,jsToken&view_mode=1"
                 f"&channel=chunlei&web=1&app_id=250528&clienttype=0&bdstoken=")
            st, tj, _t = s.page_fetch(q)
            if not isinstance(tj, dict):
                return {"ok": False, "reason": "tplconfig 返回异常（HTTP %s）" % st,
                        "errno": None}
            if tj.get("errno") not in (0, None):
                return {"ok": False,
                        "reason": "tplconfig 失败（errno=%s）" % tj.get("errno"),
                        "errno": tj.get("errno")}
            data = tj.get("data") or {}
            sign = data.get("sign")
            ts = data.get("timestamp")
            js_token = data.get("jsToken") or ""
            if not sign or not ts:
                return {"ok": False, "reason": "tplconfig 未返回 sign/timestamp",
                        "errno": None}

            # 3) sekey：页面 cookie 里有就直接用，否则 verify
            sekey = ""
            try:
                ck = s.cookies()
                cand = ck.get("BDCLND") or ""
                if cand:
                    sekey = urllib.parse.unquote(cand)
            except Exception:
                sekey = ""
            if not sekey:
                surl = surl_full[1:] if surl_full.startswith("1") else surl_full
                vq = (f"/share/verify?surl={urllib.parse.quote(surl)}"
                      f"&channel=chunlei&web=1&app_id=250528&clienttype=0"
                      f"&bdstoken=&t={int(time.time() * 1000)}&bioc=1")
                st, vj, vt = s.page_fetch(
                    vq, method="POST",
                    form={"pwd": pwd or "", "vcode": "", "vcode_str": ""})
                if not isinstance(vj, dict) or vj.get("errno") not in (0, None):
                    return {"ok": False,
                            "reason": "verify 失败（%s）" % (
                                (vj or {}).get("errno") if isinstance(vj, dict) else vt[:80]),
                            "errno": (vj or {}).get("errno") if isinstance(vj, dict) else None}
                sekey = vj.get("randsk") or ""
                if sekey:
                    sekey = urllib.parse.unquote(sekey)
            if not sekey:
                return {"ok": False, "reason": "未取到 sekey（randsk）"}

            # 4) sharedownload（body 与页面一致）
            cv = _client_version_hint()
            form = {
                "encrypt": "1",
                "extra": json.dumps({"sekey": sekey}, ensure_ascii=False),
                "product": "share",
                "ct": "pcygj",
                "cv": cv,
                "timestamp": str(ts),
                "uk": str(uk or yun.get("share_uk") or ""),
                "primaryid": str(shareid or ""),
                "fid_list": json.dumps(fids or []),
                "path_list": json.dumps(paths or [], ensure_ascii=False),
            }
            sq = (f"/api/sharedownload?channel=chunlei&web=1&app_id=250528"
                  f"&clienttype=0&bdstoken=&sign={urllib.parse.quote(str(sign))}"
                  f"&timestamp={ts}")
            if js_token:
                sq += "&jsToken=" + urllib.parse.quote(str(js_token))
            st, sj, stext = s.page_fetch(sq, method="POST", form=form)
            if not isinstance(sj, dict):
                return {"ok": False,
                        "reason": "sharedownload 返回非 JSON（HTTP %s）" % st,
                        "errno": None}

            # 5) 风控验证码（errno=-20）：**在同一个页面会话里**取图 + 提交，
            #    这样 vcode_str 与请求的 cookie/指纹天然配对。
            #    易混字符/低置信度由 solver 自己决定要不要放弃（换一张）。
            if sj.get("errno") == -20 and vcode_solver is not None:
                for attempt in range(1, int(max_vcode or 0) + 1):
                    if time.time() > deadline:
                        break
                    if attempt > 1:
                        time.sleep(random.uniform(0.8, 1.5))
                    try:
                        _gst, gv, _gt = s.page_fetch("/api/getvcode?prod=pan")
                        vcode_str = (gv or {}).get("vcode") if isinstance(gv, dict) else None
                        img_url = str((gv or {}).get("img") or "").replace("\\/", "/")
                        if not vcode_str or not img_url:
                            _emit(on_event, {"kind": "skip", "attempt": attempt,
                                             "reason": "fetch_fail"})
                            continue
                        img = s.page_fetch_bytes(img_url)
                        if not img:
                            _emit(on_event, {"kind": "skip", "attempt": attempt,
                                             "reason": "img_fail"})
                            continue
                        solved = vcode_solver(img)
                        if solved is None:
                            _emit(on_event, {"kind": "skip", "attempt": attempt,
                                             "reason": "no_ocr"})
                            continue
                        text, conf = _split_solution(solved)
                        if not text:
                            _emit(on_event, {"kind": "skip", "attempt": attempt,
                                             "reason": "empty"})
                            continue
                        form2 = dict(form)
                        form2["vcode_input"] = text
                        form2["vcode_str"] = vcode_str
                        st, sj, stext = s.page_fetch(sq, method="POST", form=form2)
                        if not isinstance(sj, dict):
                            break
                        token = sj.get("list")
                        ok = (sj.get("errno") == 0 and isinstance(token, str) and token)
                        _emit(on_event, {"kind": "try", "attempt": attempt,
                                         "text": text, "confidence": conf,
                                         "ok": bool(ok), "errno": sj.get("errno"),
                                         "show_msg": sj.get("show_msg")})
                        if ok or sj.get("errno") != -20:
                            break
                    except CDPError:
                        break

            token = sj.get("list")
            if isinstance(token, list):
                token = json.dumps(token)
            if sj.get("errno") == 0 and isinstance(token, str) and token:
                return {"ok": True, "list": token, "sign": sign, "ts": ts,
                        "jsToken": js_token, "sekey": sekey, "uk": uk,
                        "shareid": shareid, "errno": 0,
                        "request_id": sj.get("request_id")}
            return {"ok": False,
                    "reason": "sharedownload errno=%s %s" % (
                        sj.get("errno"), sj.get("show_msg") or ""),
                    "errno": sj.get("errno"),
                    "request_id": sj.get("request_id")}
        except CDPError as e:
            _log.info("浏览器桥失败: %s", e)
            return {"ok": False, "reason": "浏览器桥失败：%s" % e}
        except Exception as e:
            _log.info("浏览器桥异常: %r", e)
            return {"ok": False, "reason": "浏览器桥异常：%s" % e}
        finally:
            _last_used = time.time()


def _emit(on_event, payload):
    """把验证码事件交给上层（回调异常一律吞掉，绝不影响链路）。"""
    if on_event is None:
        return
    try:
        on_event(payload)
    except Exception:
        pass


def _split_solution(solved):
    """solver 允许返回 str / (str, 置信度) / dict{text,confidence}。"""
    if isinstance(solved, str):
        return solved.strip(), None
    if isinstance(solved, (tuple, list)):
        text = solved[0] if solved else ""
        conf = solved[1] if len(solved) > 1 else None
        return str(text or "").strip(), conf
    if isinstance(solved, dict):
        return str(solved.get("text") or "").strip(), solved.get("confidence")
    return "", None


def _surl_full(share_url):
    import re
    u = str(share_url or "")
    m = re.search(r"/s/([A-Za-z0-9_-]+)", u)
    if m:
        return m.group(1)
    m = re.search(r"[?&]surl=([A-Za-z0-9_-]+)", u)
    if m:
        return "1" + m.group(1)
    return u


def _client_version_hint():
    """cv 用实测到的网盘客户端版本，拿不到就回落抓包里的 7.14.1.6。"""
    try:
        from .db import client_version
        v = client_version()
        if v:
            return str(v)
    except Exception:
        pass
    return "7.14.1.6"
