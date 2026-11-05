# -*- coding: utf-8 -*-
"""UC 分享链接识别与解析（纯标准库，绝无 Qt 依赖）。

只处理「分享页」形态：`/s/<id>`。
- https://fast.uc.cn/s/67c130c3452d4?entry=webother
- https://drive.uc.cn/s/abcdef123456
- https://fast.uc.cn/s/abc?pwd=1234

注意与 `autounpacker.utils.is_uc_pan_url()` 的分工：
- `utils.is_uc_pan_url()` 只判主机（用于「是不是 UC 的链接」这种粗筛）
- 本模块再判路径与参数（用于「是不是一条可用的分享链接」）

依赖：仅标准库（re / urllib.parse）。
"""
import re
from urllib.parse import parse_qs, urlsplit

# 分享页只在这两个主机上（含子域）
UC_SHARE_HOSTS = ("fast.uc.cn", "drive.uc.cn")

# /s/<id>，id 由字母数字与 -_ 组成（长度 4~64，避免误吃路径片段）
_SHARE_PATH_RE = re.compile(r"^/s/([A-Za-z0-9_-]{4,64})/?$")

# 提取码可能出现的键名（不同时期叫法不同）
_PWD_KEYS = ("pwd", "password", "passcode", "code")


def _host_of(url):
    """取主机名（小写、去尾点）；失败返回空串。绝不抛。"""
    try:
        s = str(url or "").strip()
        if not s:
            return ""
        parts = urlsplit(s if "://" in s else "//" + s)
        return (parts.hostname or "").lower().rstrip(".")
    except Exception:
        return ""


def _is_uc_host(host):
    for base in UC_SHARE_HOSTS:
        if host == base or host.endswith("." + base):
            return True
    return False


def parse_uc_share_url(url):
    """把 UC 分享链接解析成结构化信息；不是分享页则返回 None。

    返回 dict：
        {
          "host":     "fast.uc.cn",
          "share_id": "67c130c3452d4",
          "pwd":      "1234" 或 "",
          "url":      原始输入（strip 后）,
          "canonical": "https://fast.uc.cn/s/<share_id>"（不含提取码, 便于去重）,
        }
    解析失败/非分享页一律返回 None，绝不抛异常。
    """
    try:
        raw = str(url or "").strip()
        if not raw:
            return None
        parts = urlsplit(raw if "://" in raw else "//" + raw)
        host = (parts.hostname or "").lower().rstrip(".")
        if not host or not _is_uc_host(host):
            return None
        path = parts.path or ""
        m = _SHARE_PATH_RE.match(path)
        if not m:
            return None
        share_id = m.group(1)
        pwd = ""
        try:
            qs = parse_qs(parts.query or "")
            for k in _PWD_KEYS:
                vals = qs.get(k) or []
                if vals and vals[0].strip():
                    pwd = vals[0].strip()
                    break
        except Exception:
            pwd = ""
        return {
            "host": host,
            "share_id": share_id,
            "pwd": pwd,
            "url": raw,
            "canonical": "https://%s/s/%s" % (host, share_id),
        }
    except Exception:
        return None


def is_uc_share_url(url):
    """是不是一条可用的 UC 分享链接（`/s/<id>`）。"""
    return parse_uc_share_url(url) is not None
