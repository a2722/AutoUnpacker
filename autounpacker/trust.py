# -*- coding: utf-8 -*-
"""网址信任机制：控制剪贴板/二维码 URL 的自动访问与自动打开浏览器。

职责：- classify_host() 对 IP 字面量/域名做内置敏感类别分类（私网/回环/链路本地/保留）
- decide_host() 核心判定，按 purpose 分两套独立名单：
  「open」= 二维码链接自动开浏览器 / 「fetch」= 复制网址下载识别二维码。
  优先级：本用途黑名单 > 本用途白名单(可覆盖内置) > 内置类别 > 本用途新域名默认行为
- remember_auto_domain() / add_trust_entry() 维护某用途下的用户黑白名单
关键入口：decide_host(cfg, host, purpose) / classify_host() / remember_auto_domain()
依赖：socket、ipaddress（getaddrinfo 为阻塞调用，仅后台线程用 resolve=True）
注意：内置敏感地址（含云元数据 169.254.169.254）默认拒绝，仅用户显式加白可覆盖
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


# 各用途独立：open=二维码链接自动开浏览器 / fetch=复制网址下载识别二维码
PURPOSES = ("open", "fetch")
_NA_MODES = ("none", "ask", "auto_whitelist", "auto_blacklist")


def _purpose_cfg(ut, purpose):
    """取某用途的信任子配置（new_domain_action/whitelist/blacklist）。

    兼容旧版扁平结构：若该用途子字典缺失，回退到顶层同名键
    （配置加载时已自动迁移；此处兜底以防外部改写或旧内存态）。"""
    sub = ut.get(purpose)
    if isinstance(sub, dict):
        return sub
    legacy = {}
    for k in ("new_domain_action", "whitelist", "blacklist"):
        if k in ut:
            legacy[k] = ut.get(k)
    return legacy


def decide_host(cfg, host, purpose="open"):
    """核心信任判定（按用途取名单）。返回 (decision, category)：
    - purpose: "open" 自动开浏览器 / "fetch" 下载识别二维码
    - decision: "allow" 静默放行 / "deny" 静默拒绝 / "ask" 需用户询问
    - category: classify_host 的类别（供弹窗风险标注）"""
    ut = cfg.get("url_trust") or {}
    if not isinstance(ut, dict):
        ut = {}
    sub = _purpose_cfg(ut, purpose)
    whitelist = sub.get("whitelist") or []
    blacklist = sub.get("blacklist") or []
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
    # 3) 内置类别黑名单（默认拒绝，仅用户显式加白可覆盖；两用途共享）
    cat = classify_host(host)
    if ut.get("builtin_blacklist", True) and cat and cat != "public":
        return "deny", cat
    # 4) 公网新域名：按该用途的默认策略处理（缺省/坏值一律 ask，安全默认不静默拒绝）
    mode = sub.get("new_domain_action", "ask")
    if mode == "auto_whitelist":
        return "allow", cat
    if mode == "auto_blacklist":
        return "deny", cat
    if mode == "none":
        return "deny", cat      # 无操作：不打开、不询问、不记录（静默跳过）
    return "ask", cat


def remember_auto_domain(cfg, host, purpose="open"):
    """auto_whitelist / auto_blacklist：把命中的「新公网域名」写入**该用途**的名单。

    返回更新后的 url_trust 字典（调用方据此持久化），无需变更时返回 None。
    仅当以下全部成立才写入：
    - 该用途的 new_domain_action 为 auto_whitelist / auto_blacklist；
    - host 非空，且不在该用途现有黑白名单中（已记录过则跳过）；
    - 不属于内置敏感类别（除非 builtin_blacklist 已关闭）。"""
    ut = cfg.get("url_trust") or {}
    if not isinstance(ut, dict):
        return None
    sub = _purpose_cfg(ut, purpose)
    mode = str(sub.get("new_domain_action", "ask"))
    if mode not in ("auto_whitelist", "auto_blacklist"):
        return None
    if not host:
        return None
    whitelist = [str(x).strip().lower() for x in (sub.get("whitelist") or [])]
    blacklist = [str(x).strip().lower() for x in (sub.get("blacklist") or [])]
    if any(_host_matches(e, host) for e in whitelist + blacklist):
        return None
    if ut.get("builtin_blacklist", True):
        cat = classify_host(host)
        if cat and cat != "public":
            return None         # 内置敏感地址：auto 信任也不能覆盖，不写白名单
    sub2 = dict(sub)
    if mode == "auto_whitelist":
        sub2["whitelist"] = whitelist + [host]
    else:
        sub2["blacklist"] = blacklist + [host]
    ut2 = dict(ut)
    ut2[purpose] = sub2
    return ut2


def add_trust_entry(cfg, host, kind, purpose="open"):
    """把 host 加入某用途的 whitelist / blacklist（用户手动「永久信任/拒绝」）。

    kind ∈ {"whitelist","blacklist"}。返回更新后的 url_trust 字典（调用方持久化）；
    host 为空或已在名单中则原样返回（无变化）。"""
    ut = cfg.get("url_trust") or {}
    if not isinstance(ut, dict):
        ut = {}
    h = str(host or "").strip().lower()
    if not h:
        return ut
    sub = dict(_purpose_cfg(ut, purpose))
    lst = [str(x).strip().lower() for x in (sub.get(kind) or [])]
    if not any(_host_matches(e, h) for e in lst):
        lst.append(h)
    sub[kind] = lst
    ut2 = dict(ut)
    ut2[purpose] = sub
    return ut2


def trust_entry_categories(host):
    """弹窗/设置页用：标注 host 属于哪些内置黑名单类别（供风险提示）。"""
    cat = classify_host(host)
    if cat and cat != "public":
        return cat
    return None


