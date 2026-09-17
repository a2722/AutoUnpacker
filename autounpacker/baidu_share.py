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
- 第 9 步 `os.startfile("baiduyunguanjia://evoked-download/…")` 需要客户端已安装；
  本模块只负责把令牌投递过去，实际下载由客户端完成；
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
import http.cookiejar
import json
import os
import re
import ssl
import subprocess
import time
import urllib.parse
import urllib.request

from .baidu_manifest import detect_dead_share_page


# 公共查询串：chunlei Web 端固定参数，末尾的 `=` 不能省。
_Q = "channel=chunlei&web=1&app_id=250528&clienttype=0&bdstoken="
_S = "https://pan.baidu.com"
_UA = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
       "(KHTML, like Gecko) Chrome/120.0 Safari/537.36")
# 分享链接路径段（/s/<surl_full>）；提取码从查询串取。
_SURL_RE = re.compile(r"/s/([A-Za-z0-9_-]+)")

# 新版分享的提取码是强制必填项：空码去提交必然失败。`prepare_share` 在死链判定之后、
# 任何 tplconfig/verify 请求之前就此短路，把原因交回上层去询问用户（供测试/上层引用）。
NEED_CODE_REASON = "该分享需要提取码"

# 唤起已发出、但后续 `/api/invoker/check` 复核返回 errno≠0 时的统一前缀。
# 上层据此把「唤醒已发出、客户端未确认接单」与普通拉起失败区分开，对已发出的
# 「已请求客户端下载」通知补一条纠正通知（时延整改：通知提前，复核降级为静默）。
CHECK_FAIL_PREFIX = "唤起后校验失败"

# 调 tasklist 时的无窗口标志（与 extract.py 一致；老版本 Python 无此常量时退化 0）。
_CREATE_NO_WINDOW = getattr(subprocess, "CREATE_NO_WINDOW", 0)

# 百度网盘客户端进程映像名（本机实测：客户端运行时三者都在）。
# BaiduNetdisk.exe 是主客户端进程（登录态的宿主）；YunDetectService.exe /
# BaiduNetdiskHost.exe 是随主进程拉起的辅助进程。命中其中任意一个即视为
# 「客户端已在运行」——对「是否可安全投递」而言，辅助进程在也说明主链路已起。
_CLIENT_PROC_NAMES = ("BaiduNetdisk.exe", "YunDetectService.exe",
                      "BaiduNetdiskHost.exe")

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


def _build_opener():
    """构造共用 OpenerDirector（CookieJar + 忽略证书校验的 HTTPS）。"""
    ctx = ssl.create_default_context()
    ctx.check_hostname = False
    ctx.verify_mode = ssl.CERT_NONE
    cj = http.cookiejar.CookieJar()
    op = urllib.request.build_opener(
        urllib.request.HTTPCookieProcessor(cj),
        urllib.request.HTTPSHandler(context=ctx))
    op.addheaders = [("User-Agent", _UA)]
    return op


def _get(op, url, referer):
    """GET 文本（utf-8, 容错）。"""
    rq = urllib.request.Request(url, headers={"Referer": referer})
    with op.open(rq, timeout=15) as r:
        return r.read().decode("utf-8", "replace")


def _post(op, url, data, referer):
    """POST 表单（urlencoded）并返回文本。data 用 dict（保持插入顺序）。"""
    body = urllib.parse.urlencode(data).encode()
    rq = urllib.request.Request(url, data=body, headers={
        "Referer": referer,
        "Content-Type": "application/x-www-form-urlencoded"})
    with op.open(rq, timeout=15) as r:
        return r.read().decode("utf-8", "replace")


def _parse_share(share_url, pwd):
    """把分享链接拆成 (surl_full, surl, pwd)。

    - surl_full = `/s/` 之后的完整段（`/share/tplconfig` 用）；
    - surl      = 去掉开头一个 `1`（`/share/verify`、`/share/list` 用）；
    - pwd       = 参数优先，其次 URL 查询串里的 `pwd`。
    解析不出路径段时抛 ValueError（由外层统一转成 (False, …)）。
    """
    u = str(share_url or "").strip()
    m = _SURL_RE.search(u)
    if not m:
        raise ValueError("不是有效的百度分享链接")
    surl_full = m.group(1)
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
      的目录项也保留（勾选目录 = 整目录下载）。另有两个下划线内部键：`_op`
      （共用 OpenerDirector）与 `_share_url`（原始分享链接），供 commit_download
      原样复用同一 CookieJar 与 Referer，UI 无需关心、也不应改动。
    - 失败：data 为 str（中文原因）。**绝不抛异常。**
    """
    try:
        surl_full, surl, pwd = _parse_share(share_url, pwd)
        share_url = str(share_url or "").strip()
        referer_share = share_url
        op = _build_opener()

        # 0. 预热：先访问分享页（可能 404），吞掉异常只为拿 cookie。
        #    （2.F）顺手留住页面 HTML 做「失效分享」探测：命中即在 verify 之前短路
        #    返回——绝不消耗本流程唯一一次、且间隔受限的 verify 配额。
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
        tpl = json.loads(_get(
            op,
            f"{_S}/share/tplconfig?surl={surl_full}&fields=sign,timestamp"
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

        # 2. verify：校验提取码 → randsk（即 sekey），后面 extra 要用。
        #    §4：本次调用最多 1 次 verify——失败即停，不换码、不重试；
        #    两次 verify 之间至少间隔 _VERIFY_MIN_GAP 秒。
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
        try:
            page = _get(op, f"{_S}/s/{surl_full}?pwd={pwd}", referer_share)
        except Exception:
            page = ""
        share_uk, shareid = _html_ids(page)
        if not (share_uk and shareid):
            return False, "分享页未解析到 share_uk / shareid（链接可能失效或需提取码）"

        return True, {
            "surl_full": surl_full,
            "surl": surl,
            "pwd": pwd,
            "sign": sign,
            "ts": ts,
            "randsk": randsk,
            "share_uk": share_uk,
            "shareid": shareid,
            "entries": entries,
            "_op": op,
            "_share_url": share_url,
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


def _client_running():
    """百度网盘客户端是否已在运行（只读进程表，无第三方依赖）。

    用 `tasklist` 过滤已知的客户端进程名；任何异常一律返回 False（保守：宁可不唤起）。
    `_CLIENT_PROC_NAMES` 里的 `BaiduNetdisk.exe` 是主客户端进程，另外两个是随它拉起
    的辅助进程；本机实测客户端运行时三者都在，命中任意一个即视为「客户端已在运行」。
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
    for name in _CLIENT_PROC_NAMES:
        if name.lower() in low:
            return True
    return False


def client_ready():
    """给 UI 用的只读检查：客户端是否可安全投递（现在是「客户端必须在运行」）。

    返回 bool，**绝不抛异常**；`_WAKE_GUARD_ENABLED = False`（门已关）时恒为 True。
    """
    try:
        if not _WAKE_GUARD_ENABLED:
            return True
        return bool(_client_running())
    except Exception:
        return False


def _wake_detail(bid, seq):
    """唤起客户端后的统一简述（`on_wake` 回调与成功返回值**共用同一措辞**）。

    抽成助手是为了让「唤醒即通知」的通知文案与链路最终 `ok` 的文案逐字一致，
    避免两处各写一遍字符串日后漂移。
    """
    return f"已唤起客户端下载（browserId={bid}, seq={seq}）"


def commit_download(prep, fs_ids=None, pairs=None, on_wake=None):
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
    9. `os.startfile("baiduyunguanjia://evoked-download/?…")` 唤起客户端，随后
       **立即回调 `on_wake(detail)`**（在复核轮询之前，供上层「唤醒即通知」）；
    10. 复核轮询 `/api/invoker/check`：最多 3 次、每次间隔 1.5s（上限 4.5s），
        status==2 提前跳出；errno≠0 才算失败；超时容忍（视为已唤起）。

    参数：
    - `on_wake`：可选回调。唤起成功后、复核轮询之前调用一次，收到
      `_wake_detail(bid, seq)` 字符串；回调自身异常一律吞掉，绝不影响链路。

    返回 (ok: bool, detail: str)：成功时 detail 为简述，失败时为原因。**绝不抛异常。**
    """
    try:
        # 登录态保护门：在发出任何百度请求（第 5 步 sharedownload 起）之前先检查
        # 客户端是否在运行。未运行就直接返回——此刻投递唤醒会冷启动一个无登录态
        # 的客户端，导致用户被迫重新登录；既然不可能成功，就一个请求都不花。
        if _WAKE_GUARD_ENABLED and not _client_running():
            return False, ("网盘客户端未在运行：已跳过拉起（避免用无登录态的方式冷启动客户端，"
                           "那会导致你被迫重新登录）。请先手动打开百度网盘客户端后重试。")

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
        sd = json.loads(_post(
            op,
            f"{_S}/api/sharedownload?{_Q}"
            f"&sign={urllib.parse.quote(sign)}&timestamp={ts}",
            {"encrypt": "1",
             "extra": json.dumps({"sekey": urllib.parse.unquote(randsk)},
                                 ensure_ascii=False),
             "product": "share",
             "timestamp": str(ts),
             "uk": share_uk,
             "primaryid": shareid,
             "fid_list": json.dumps(fids),
             "path_list": json.dumps(paths)},
            referer_share))
        blob = sd.get("list")
        if not isinstance(blob, str):
            # 兜底：极少数返回结构为 list/dict，退化为取首个元素的 dlink。
            try:
                first = blob[0] if isinstance(blob, list) else blob
                blob = first.get("dlink") if isinstance(first, dict) else None
            except Exception:
                blob = None
        if not blob:
            return False, f"sharedownload 未取到 filelist 令牌（errno={sd.get('errno')}）"

        # 6. invoker/get：拿 browserId。
        bid = json.loads(_get(
            op, f"{_S}/api/invoker/get?{_Q}&t={_now_ms()}", referer_api))["browserId"]
        if not bid:
            return False, "invoker/get 未取到 browserId"

        # 7. invoker/online：上报客户端在线。
        _get(op, f"{_S}/api/invoker/online?browserId={bid}&{_Q}&t={_now_ms()}",
             referer_api)

        # 8. invoker/send：把下载令牌投递给客户端 → seq。
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
        wake = (f"baiduyunguanjia://evoked-download/?browserId={bid}&seq={seq}"
                f"&src_from=wp-download_web_share&src_type=web_sharelink_page")
        os.startfile(wake)

        # 9.5 唤醒已发出：先回调 on_wake（在复核轮询之前），让上层立即通知用户
        #     「已请求客户端下载」。复核只作为静默兜底，不再阻塞这条告知。
        if on_wake is not None:
            try:
                on_wake(_wake_detail(bid, seq))
            except Exception:
                pass

        # 10. 复核 check：status==2 即客户端已接单；errno≠0 视为失败；超时容忍。
        #     最多 3 次（上限 4.5s）：通知已在唤起那一刻发出，这里只是静默复核，
        #     没必要再白等满 12s。
        err = None
        for _ in range(3):
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
        return True, _wake_detail(bid, seq)
    except Exception as e:
        return False, str(e)


def invoke_download(share_url, pwd="", on_wake=None):
    """兼容保留：prepare_share + commit_download(prep, None) 的薄封装。

    签名与 (ok, detail) 返回值、以及所有现有调用方都不受影响——行为等价于旧的
    「下载该分享的全部条目」。需要让用户挑选文件时，请改用 `prepare_share()` 拿
    `entries`，再用 `commit_download(prep, 选中的 fs_id 列表)` 提交子集。
    `on_wake` 原样透传给 `commit_download`（唤醒成功后、复核轮询之前调用一次）。
    固定流程编号见 prepare_share（0~4）与 commit_download（5~10）。
    **绝不抛异常。**
    """
    ok, prep = prepare_share(share_url, pwd)
    if not ok:
        return False, prep
    return commit_download(prep, None, on_wake=on_wake)
