# -*- coding: utf-8 -*-
"""网址信任机制：控制剪贴板 URL 自动访问 / 二维码 URL 自动打开浏览器。

判定优先级：用户黑名单 > 用户白名单(可覆盖内置) > 内置敏感类别(默认拒绝) > 公网新域名(按配置)。
"""
import socket
import ipaddress

# ==================== 网址信任机制 ====================
# 控制剪贴板 URL 自动访问 / 二维码 URL 自动打开浏览器时的信任判定。
# 内置黑名单类别：私网 / 回环 / 链路本地(含云元数据) / 保留 / 组播地址。
# 判定优先级：用户黑名单 > 用户白名单 > 内置类别(默认拒绝) > 公网新域名(按配置)。

# 域名解析缓存：host -> 类别 or None（None=解析失败或公网），避免重复 DNS 查询
_host_resolve_cache = {}


def _host_of(url):
    """提取 URL 的 hostname（小写、去端口），非法 URL 返回 None。"""
    try:
        from urllib.parse import urlsplit
        host = urlsplit(url).hostname
        if not host:
            return None
        return host.lower().rstrip(".")
    except Exception:
        return None


def _host_matches(entry, host):
    """信任条目匹配：entry 精确匹配 host，或 entry 是 host 的父域（含全部子域）。
    条目中的前导 *. 忽略（等价于父域匹配）。IP 条目要求字面相同。"""
    entry = str(entry or "").strip().lower().rstrip(".")
    if not entry or not host:
        return False
    if entry.startswith("*."):
        entry = entry[2:]
    if host == entry:
        return True
    return host.endswith("." + entry)


def _classify_ip(ip):
    """对 IP 对象分类：private / loopback / link_local / reserved / public。
    注意 ipaddress 中 0.0.0.0/8 的 is_private 为 True、169.254.0.0/16 与
    127.0.0.0/8 也标 private，故 unspecified/loopback/link_local 必须先判。"""
    try:
        if ip.is_unspecified:
            return "reserved"
        if ip.is_loopback:
            return "loopback"
        if ip.is_link_local:      # 169.254.0.0/16 与 fe80::/10（含云元数据 169.254.169.254）
            return "link_local"
        if ip.is_private:         # 10/8, 172.16/12, 192.168/16 与 fc00::/7
            return "private"
        if ip.is_multicast or ip.is_reserved:
            return "reserved"
        return "public"
    except Exception:
        return None


def _resolve_host(host):
    """解析域名得到类别（带缓存）。解析失败/超时返回 None（按公网候选处理）。
    getaddrinfo 为阻塞调用，仅在后台线程与 UI 弹窗中触发，频次受缓存限制。"""
    if host in _host_resolve_cache:
        return _host_resolve_cache[host]
    cat = None
    try:
        infos = socket.getaddrinfo(host, None, 0, socket.SOCK_STREAM)
        for family, _, _, _, sockaddr in infos:
            try:
                ip = ipaddress.ip_address(sockaddr[0])
            except ValueError:
                continue
            c = _classify_ip(ip)
            if c and c != "public":
                cat = c
                break
            if c == "public":
                cat = "public"
                break
    except Exception:
        cat = None
    _host_resolve_cache[host] = cat
    return cat


def classify_host(host, resolve=True):
    """判定 host 的内置类别：
    - IP 字面量：直接按地址分类（私网/回环/链路本地/保留 → 内置黑名单）
    - 域名：resolve=True 时解析后取首个内网/保留类地址（仅后台线程调用，
      getaddrinfo 可能阻塞）；resolve=False 时只查缓存/IP 字面量，绝不阻塞
      （供 UI 线程弹窗风险标注使用——检查点先于弹窗执行，缓存通常已就绪）
    返回类别字符串（public/private/loopback/link_local/reserved）或 None。"""
    if not host:
        return None
    try:
        ip = ipaddress.ip_address(host)
        return _classify_ip(ip)
    except ValueError:
        pass  # 不是 IP 字面量，按域名处理
    if resolve:
        return _resolve_host(host)
    return _host_resolve_cache.get(host)


def decide_host(cfg, host):
    """核心信任判定。返回 (decision, category)：
    - decision: "allow" 静默放行 / "deny" 静默拒绝 / "ask" 需用户询问
    - category: classify_host 的类别（供弹窗风险标注）"""
    ut = cfg.get("url_trust") or {}
    if not isinstance(ut, dict):
        ut = {}
    whitelist = ut.get("whitelist") or []
    blacklist = ut.get("blacklist") or []
    if not host:
        return "deny", None
    # 1) 用户黑名单（最高优先级，即使在内置白名单也拒绝）
    for entry in blacklist:
        if _host_matches(entry, host):
            return "deny", None
    # 2) 用户白名单（显式信任，可覆盖内置类别）
    for entry in whitelist:
        if _host_matches(entry, host):
            return "allow", None
    # 3) 内置类别黑名单（默认拒绝，仅用户显式加白可覆盖）
    cat = classify_host(host)
    if ut.get("builtin_blacklist", True) and cat and cat != "public":
        return "deny", cat
    # 4) 公网新域名：按默认策略处理
    mode = ut.get("new_domain_action", "ask")
    if mode == "auto_whitelist":
        return "allow", cat
    if mode == "auto_blacklist":
        return "deny", cat
    return "ask", cat


def trust_entry_categories(host):
    """弹窗/设置页用：标注 host 属于哪些内置黑名单类别（供风险提示）。"""
    cat = classify_host(host)
    if cat and cat != "public":
        return cat
    return None


