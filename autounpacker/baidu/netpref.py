# -*- coding: utf-8 -*-
"""IPv4 优先：只影响「当前线程 + 当前上下文」里的域名解析。

## 为什么需要

百度对 **IPv6 出站**的分享下载几乎必弹图形验证码（社区长期反馈，本机实测：
直连 3/3 次走 `240e:...` v6；而用户 Edge 走 v4 `36.110.192.103` 就没有验证码）。
常见做法是改 hosts，但那需要管理员权限、且重装/换机就丢 —— 不适合分发。

这里改为在**进程内**做：进入 `ipv4_only()` 上下文后，`socket.getaddrinfo` 只返回
A 记录，于是 urllib / ssl / http.client 全都走 v4。

设计要点：
- **线程局部**：只有调用方线程生效，绝不影响程序里其它网络（更新检查、通知等）；
- **不改全局默认**：不在上下文里时行为与 Python 原生完全一致；
- 只对 `AF_UNSPEC`/`AF_INET` 的请求过滤，显式要 v6 的调用不受影响；
- 没有任何 A 记录时**原样返回**，退化成系统默认（绝不因为没有 v4 就失败）。
"""

import contextlib
import socket
import threading
import time

__all__ = ["ipv4_only", "ipv4_addresses", "prefer_ipv4", "resolve_ipv4_first"]

_TLS = threading.local()
_ORIG = socket.getaddrinfo
_installed = False
_PREF_CACHE = {"t": 0.0, "v": True}


def _patched(host, port, family=0, type=0, proto=0, flags=0):
    res = _ORIG(host, port, family, type, proto, flags)
    try:
        if getattr(_TLS, "v4", False) and family in (0, socket.AF_UNSPEC):
            v4 = [r for r in res if r[0] == socket.AF_INET]
            if v4:
                return v4
    except Exception:
        pass
    return res


def install():
    """把 getaddrinfo 换成可切换版本（幂等）。"""
    global _installed
    if _installed:
        return
    socket.getaddrinfo = _patched
    _installed = True


@contextlib.contextmanager
def ipv4_only(enabled=True):
    """进入后本线程的域名解析只用 IPv4；退出时恢复。"""
    if not enabled:
        yield
        return
    install()
    old = getattr(_TLS, "v4", False)
    _TLS.v4 = True
    try:
        yield
    finally:
        _TLS.v4 = old


def prefer_ipv4():
    """配置项 `baidu_force_ipv4`（默认开）；20 秒缓存，避免每个请求都读配置。"""
    now = time.time()
    if now - _PREF_CACHE["t"] < 20.0:
        return _PREF_CACHE["v"]
    v = True
    try:
        from ..config import load_config
        v = bool(load_config().get("baidu_force_ipv4", True))
    except Exception:
        v = True
    _PREF_CACHE["t"] = now
    _PREF_CACHE["v"] = v
    return v


def ipv4_addresses(host, port=443):
    """直接取 A 记录（**不受**当前线程的 v4 开关影响），去重保序。"""
    out = []
    try:
        for fam, _t, _p, _c, sa in _ORIG(host, port, socket.AF_INET, socket.SOCK_STREAM):
            if fam == socket.AF_INET and sa and sa[0] not in out:
                out.append(sa[0])
    except Exception:
        return out
    return out


def resolve_ipv4_first(host, port=443, timeout=2.0):
    """挑一个**能连上**的 A 记录（浏览器桥要用它钉死 v4）。连不上返回 None。"""
    for ip in ipv4_addresses(host, port):
        s = None
        try:
            s = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
            s.settimeout(timeout)
            s.connect((ip, port))
            return ip
        except OSError:
            continue
        finally:
            if s is not None:
                try:
                    s.close()
                except Exception:
                    pass
    return None
