# -*- coding: utf-8 -*-
"""百度网盘分享「拉起客户端下载」全链路（2.F：把分享链接交给网盘客户端下载）。

职责：
- 给定一条公开分享链接（含提取码），走完「分享页 → 校验提取码 → 列文件 →
  分享转存下载（拿 filelist 令牌）→ 唤起客户端 → 轮询校验」的完整链路，最终让
  **百度网盘客户端**自己去下载该分享里的文件（不下载、不登录、不碰网页，绕开 IDM）。

关键入口：invoke_download()

依赖：标准库（http.cookiejar / json / os / re / ssl / time / urllib.request /
      urllib.parse）。**不引入任何第三方依赖**。

注意：
- 本协议是抓包反推并**端到端验证通过**的，字段名 / 顺序 / 编码一律照搬，不要
  「优化」或改动（`filelist` 是不透明服务端令牌，必须原样回传客户端）；
- 第 9 步 `os.startfile("baiduyunguanjia://evoked-download/…")` 需要客户端已安装；
  本模块只负责把令牌投递过去，实际下载由客户端完成；
- 全程共用一个 CookieJar / 一个 OpenerDirector；Referer 用分享链接（invoker 用站点根）；
- 铁律：本模块不向调用方抛异常，任何失败一律 `(False, 原因)`。
"""
import http.cookiejar
import json
import os
import re
import ssl
import time
import urllib.parse
import urllib.request


# 公共查询串：chunlei Web 端固定参数，末尾的 `=` 不能省。
_Q = "channel=chunlei&web=1&app_id=250528&clienttype=0&bdstoken="
_S = "https://pan.baidu.com"
_UA = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
       "(KHTML, like Gecko) Chrome/120.0 Safari/537.36")
# 分享链接路径段（/s/<surl_full>）；提取码从查询串取。
_SURL_RE = re.compile(r"/s/([A-Za-z0-9_-]+)")


def _now_ms():
    """当前墙钟毫秒（各接口的 t 参数用）。"""
    return int(time.time() * 1000)


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


def _html_ids(html):
    """从分享页 HTML 取 (share_uk, shareid)（`window.yunData` / locals.get 都能命中）。"""
    t = html or ""
    uk = re.search(r'share_uk\s*[=:]\s*["\']?(\d+)', t)
    sid = re.search(r'shareid\s*[=:]\s*["\']?(\d+)', t)
    return (uk.group(1) if uk else None, sid.group(1) if sid else None)


def invoke_download(share_url, pwd=""):
    """把百度分享链接交给网盘客户端下载（2.F『拉起』全链路）。

    固定流程（已端到端验证）：
    1. 解析链接 → surl_full / surl / pwd；
    2. `/share/tplconfig` 取 sign / timestamp；
    3. `/share/verify` 校验提取码 → randsk（sekey）；
    4. `/share/list` 分页取**分享根目录下全部**条目的 fs_id / path（去重、保序）；
    5. `/api/sharedownload` 转存下载 → `list` 字段（字符串令牌 = filelist）；
    6. `/api/invoker/get` → browserId；
    7. `/api/invoker/online` 上报在线；
    8. `/api/invoker/send` 投递 downloadInfo → seq；
    9. `os.startfile("baiduyunguanjia://evoked-download/?…")` 唤起客户端；
    10. 轮询 `/api/invoker/check` 至 status==2 或 errno≠0。

    返回 (ok: bool, detail: str)：成功时 detail 为简述，失败时为原因。**绝不抛异常。**
    """
    try:
        surl_full, surl, pwd = _parse_share(share_url, pwd)
        share_url = str(share_url or "").strip()
        referer_share = share_url
        referer_api = _S + "/"
        op = _build_opener()

        # 0. 预热：先访问分享页（可能 404），吞掉异常只为拿 cookie。
        try:
            _get(op, f"{_S}/s/{surl_full}?pwd={pwd}", referer_share)
        except Exception:
            pass

        # 1. tplconfig：取 sign / timestamp。
        d = json.loads(_get(
            op,
            f"{_S}/share/tplconfig?surl={surl_full}&fields=sign,timestamp"
            f"&view_mode=1&{_Q}",
            referer_share))["data"]
        sign, ts = d["sign"], d["timestamp"]

        # 2. verify：校验提取码 → randsk（即 sekey），后面 extra 要用。
        randsk = json.loads(_post(
            op,
            f"{_S}/share/verify?surl={surl}&{_Q}&t={_now_ms()}&bioc=1",
            {"pwd": pwd, "vcode": "", "vcode_str": ""},
            referer_share))["randsk"]

        # 3. list：分页收齐分享根目录下的**全部**条目（整包 / 全选下载）。
        #    每页 50 条，按 fs_id 去重、保持服务端返回顺序；停止条件：
        #    无 list / errno≠0 / 本页不足 50 条 / 页数达到硬上限 20（防死循环）。
        fids, paths = [], []
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
                fids.append(fs)
                paths.append(path)
            if len(lst) < 50:
                break
        if not fids:
            return False, "分享列表为空"

        # 4. 分享页取 share_uk / shareid（不在 URL 里，只能从 HTML 解析）。
        try:
            page = _get(op, f"{_S}/s/{surl_full}?pwd={pwd}", referer_share)
        except Exception:
            page = ""
        share_uk, shareid = _html_ids(page)
        if not (share_uk and shareid):
            return False, "分享页未解析到 share_uk / shareid（链接可能失效或需提取码）"

        # 5. sharedownload：转存下载 → list 字段（字符串令牌 = filelist）。
        #    把**全部** fs_id / path 一次性提交（整包）：服务端只回一个不透明的
        #    `list` 令牌，该令牌已覆盖 fid_list 里的每一个条目，客户端据此全部下载。
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

        # 10. 轮询 check：status==2 即客户端已接单；errno≠0 视为失败；超时容忍。
        err = None
        for _ in range(8):
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
            return False, f"唤起后校验失败（errno={err}）"
        return True, f"已唤起客户端下载（browserId={bid}, seq={seq}）"
    except Exception as e:
        return False, str(e)
