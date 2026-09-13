# -*- coding: utf-8 -*-
"""配置中心：默认值、净化、读取、原子保存、全局快捷键解析。

职责：- 维护 DEFAULT_CONFIG 全部配置项（通知/二维码/信任/实验性等开关）
- _sanitize_cfg() 净化与迁移旧版配置（旧路径密码并入全局密码本、补默认值）
- load_config() 读取合并、损坏时备份回退默认；save_config() 原子写入
- parse_hotkey() 把 'Ctrl+Alt+W' 解析为 (mods, vk)，供全局快捷键注册
关键入口：load_config() / save_config() / parse_hotkey() / _sanitize_cfg()
依赖：paths（配置文件路径）、utils._norm_path_for_cfg
注意：保存必须走 save_config()（先写 .tmp 再 os.replace），半截 JSON 会导致下次启动整个配置被静默重置
"""
import json
import os
import time

from . import paths
from .utils import _norm_path_for_cfg

DEFAULT_CONFIG = {
    "qr_enabled": True,
    "notify_enabled": True,
    "notify_archive": True,
    "notify_success": True,
    "notify_failure": True,
    "notify_error": True,
    "notify_trayed": True,            # 托盘提示：已最小化到托盘
    "notify_already_running": True,   # 托盘提示：程序已在运行，已打开主界面
    "notify_trust_pending": True,     # 托盘提示：有新的网址等待确认
    "notify_baidu_done": True,        # 实验性：网盘下载批次完成
    "notify_baidu_leftover": True,    # 实验性：启动时有未完成的网盘任务
    "notify_baidu_dup": False,        # 实验性：新任务与历史下载重复（默认关，避免打扰）
    "qr_clipboard_action": "none",   # none=不处理 code=恢复最近非图片内容 url=写回二维码内容
    "qr_url_redirect": True,
    "promote_merge": True,           # 提升时同名文件夹无文件冲突则合并
    "qr_url_enabled": True,          # 复制 http(s) 网址时尝试访问并识别二维码图片
    "url_exclude_temp_password": True,  # 带 :// 的网址不记录为临时密码（xxxx.com 域名形式仍记录）
    "temp_password_filter": True,       # 临时密码智能过滤（排除路径/文件名/句子等；关闭则照单全收）
    "temp_password_max": 200,           # 临时密码最多保留条数（超出丢最旧）
    "temp_password_ttl_hours": 24,      # 临时密码有效期（小时），超时自动清理
    "translation_move_enabled": True,   # 翻译 JSON 自动归位（<10MB 单 json 移入同名大文件夹）
    "log_colors_enabled": True,         # 运行日志按事件类型着色
    "hotkey_enabled": True,             # 全局快捷键唤起主界面
    "hotkey": "Ctrl+Alt+W",             # 快捷键组合（空/无 表示禁用）
    "url_redirect_rules": [
        {"from": "drive.uc.cn", "to": "fast.uc.cn"},
    ],
    "sevenzip_check_done": False,   # 首次启动的 7-Zip 检测已完成（避免每次启动都检查/弹窗）
    "poll_interval": 2,
    "passwords": [],
    "auto_add_clipboard_password": False,
    "watch_paths": [
        {"path": "", "enabled": True, "output_dir": "",
         "delete_source": False},
    ],
    "close_action": "ask",   # 点右上角关闭时的行为：ask=每次询问 / tray=隐藏到托盘 / exit=关闭程序
    # 网址信任机制：控制二维码/剪贴板 URL 的自动访问与自动打开浏览器
    "url_trust": {
        "new_domain_action": "none",  # none=无操作(默认) / ask=弹窗询问 / auto_whitelist=自动信任并打开 / auto_blacklist=自动拒绝
        "whitelist": [],              # 信任域名（含全部子域），可覆盖内置黑名单类别
        "blacklist": [],              # 拒绝域名（含全部子域），最高优先级
        "builtin_blacklist": True,    # 内置类别黑名单（私网/回环/链路本地/元数据/保留地址）
    },
    "tls_skip_verify": False,   # 允许不验证 HTTPS 证书（默认关，开启有 MITM 风险）
    "experimental_enabled": False,  # 实验性功能总开关（默认关；开启后可只读探测百度任务库）
    "baidu_task_db": "",            # 实验性：BaiduYunGuanjia.db 路径（留空自动探测）
    "ui_theme": "auto",             # 界面主题：auto=跟随系统深浅色 / fluent=浅色 / devtool=深色
    "ui_theme_cached": "",          # 上次实际应用的主题（自动维护：启动时零检测先出首屏用）
}


def _sanitize_cfg(cfg):
    try:
        # 旧版本密码放在每个监听路径里，这里统一迁移到全局密码本
        global_passwords = list(cfg.get("passwords") or [])
        if not all(isinstance(p, str) for p in global_passwords):
            global_passwords = []
        paths = cfg.get("watch_paths")
        if not isinstance(paths, list):
            paths = []
        clean = []
        seen_paths = set()
        for p in paths:
            if not isinstance(p, dict):
                continue
            old_pw = str(p.get("passwords") or "").strip()
            if old_pw:
                for item in old_pw.replace("，", ",").split(","):
                    item = item.strip()
                    if item and item not in global_passwords:
                        global_passwords.append(item)
            path = str(p.get("path") or "").strip()
            norm = _norm_path_for_cfg(path)
            if not path or norm in seen_paths:
                continue
            seen_paths.add(norm)
            clean.append({
                "path": path,
                "enabled": bool(p.get("enabled", True)),
                "output_dir": str(p.get("output_dir") or ""),
                "delete_source": bool(p.get("delete_source", False)),
                # 监听模式：surface=只扫表层（原有，安全）
                #          baidu  =额外按百度网盘任务清单处理子目录里的压缩包/分卷
                "mode": ("baidu" if str(p.get("mode") or "").lower() == "baidu"
                         else "surface"),
            })
        cfg["watch_paths"] = clean
        cfg["passwords"] = global_passwords
        try:
            cfg["poll_interval"] = max(1, int(cfg.get("poll_interval", 2)))
        except Exception:
            cfg["poll_interval"] = 2
        cfg["qr_enabled"] = bool(cfg.get("qr_enabled", True))
        cfg["notify_enabled"] = bool(cfg.get("notify_enabled", True))
        cfg["notify_archive"] = bool(cfg.get("notify_archive", True))
        cfg["notify_success"] = bool(cfg.get("notify_success", True))
        cfg["notify_failure"] = bool(cfg.get("notify_failure", True))
        cfg["notify_error"] = bool(cfg.get("notify_error", True))
        cfg["notify_trayed"] = bool(cfg.get("notify_trayed", True))
        cfg["notify_already_running"] = bool(cfg.get("notify_already_running", True))
        cfg["notify_trust_pending"] = bool(cfg.get("notify_trust_pending", True))
        cfg["notify_baidu_done"] = bool(cfg.get("notify_baidu_done", True))
        cfg["notify_baidu_leftover"] = bool(cfg.get("notify_baidu_leftover", True))
        cfg["notify_baidu_dup"] = bool(cfg.get("notify_baidu_dup", False))
        action = str(cfg.get("qr_clipboard_action", "none"))
        cfg["qr_clipboard_action"] = action if action in ("code", "url", "none") else "none"
        cfg["qr_url_redirect"] = bool(cfg.get("qr_url_redirect", True))
        cfg["promote_merge"] = bool(cfg.get("promote_merge", True))
        cfg["qr_url_enabled"] = bool(cfg.get("qr_url_enabled", True))
        cfg["url_exclude_temp_password"] = bool(cfg.get("url_exclude_temp_password", True))
        cfg["temp_password_filter"] = bool(cfg.get("temp_password_filter", True))
        try:
            cfg["temp_password_max"] = max(1, min(100000, int(cfg.get("temp_password_max", 200))))
        except Exception:
            cfg["temp_password_max"] = 200
        try:
            cfg["temp_password_ttl_hours"] = max(
                1, min(24 * 365, int(cfg.get("temp_password_ttl_hours", 24))))
        except Exception:
            cfg["temp_password_ttl_hours"] = 24
        cfg["translation_move_enabled"] = bool(cfg.get("translation_move_enabled", True))
        cfg["log_colors_enabled"] = bool(cfg.get("log_colors_enabled", True))
        cfg["hotkey_enabled"] = bool(cfg.get("hotkey_enabled", True))
        cfg["hotkey"] = str(cfg.get("hotkey", "Ctrl+Alt+W")).strip()
        rules = []
        for r in cfg.get("url_redirect_rules") or []:
            if isinstance(r, dict) and r.get("from") and r.get("to"):
                rules.append({"from": str(r["from"]), "to": str(r["to"])})
        cfg["url_redirect_rules"] = rules
        cfg["auto_add_clipboard_password"] = bool(cfg.get("auto_add_clipboard_password", False))
        cfg["sevenzip_check_done"] = bool(cfg.get("sevenzip_check_done", False))
        close_action = str(cfg.get("close_action", "ask")).strip()
        cfg["close_action"] = close_action if close_action in ("ask", "tray", "exit") else "ask"
        # 网址信任机制
        ut = cfg.get("url_trust")
        if not isinstance(ut, dict):
            ut = {}
        na = str(ut.get("new_domain_action", "none"))
        ut["new_domain_action"] = na if na in ("none", "ask", "auto_whitelist", "auto_blacklist") else "none"
        wl = ut.get("whitelist")
        if not isinstance(wl, list):
            wl = []
        ut["whitelist"] = [str(x).strip().lower() for x in wl if str(x).strip()]
        bl = ut.get("blacklist")
        if not isinstance(bl, list):
            bl = []
        ut["blacklist"] = [str(x).strip().lower() for x in bl if str(x).strip()]
        ut["builtin_blacklist"] = bool(ut.get("builtin_blacklist", True))
        cfg["url_trust"] = ut
        cfg["tls_skip_verify"] = bool(cfg.get("tls_skip_verify", False))
        cfg["experimental_enabled"] = bool(cfg.get("experimental_enabled", False))
        cfg["baidu_task_db"] = str(cfg.get("baidu_task_db", "") or "").strip()
        _ut = str(cfg.get("ui_theme", "auto") or "auto").strip().lower()
        cfg["ui_theme"] = _ut if _ut in ("auto", "fluent", "devtool") else "auto"
        _utc = str(cfg.get("ui_theme_cached", "") or "").strip().lower()
        cfg["ui_theme_cached"] = _utc if _utc in ("fluent", "devtool") else ""
    except Exception:
        pass
    return cfg


def load_config():
    try:
        if paths.CONFIG_FILE.exists():
            data = json.loads(paths.CONFIG_FILE.read_text(encoding="utf-8"))
            if isinstance(data, dict):
                merged = json.loads(json.dumps(DEFAULT_CONFIG))
                merged.update(data)
                return _sanitize_cfg(merged)
    except Exception:
        # 配置文件损坏（复制中断 / 写入中途被杀）：先把损坏文件备份下来，
        # 再回退默认配置，避免后续任何一次保存把"空配置"永久固化、
        # 用户原有配置无痕丢失。
        try:
            if paths.CONFIG_FILE.exists():
                bak = paths.CONFIG_FILE.with_name(
                    paths.CONFIG_FILE.name + ".corrupt" + time.strftime("%Y%m%d%H%M%S"))
                paths.CONFIG_FILE.replace(bak)
        except Exception:
            pass
    return _sanitize_cfg(json.loads(json.dumps(DEFAULT_CONFIG)))


# ---------- 全局快捷键（Win32 RegisterHotKey + WM_HOTKEY） ----------
HOTKEY_ID = 0x5354          # 自定义 id（WM_HOTKEY 的 wParam）
WM_HOTKEY = 0x0312
MOD_ALT, MOD_CONTROL, MOD_SHIFT, MOD_WIN = 0x1, 0x2, 0x4, 0x8
MOD_NOREPEAT = 0x4000

_HK_MOD_NAMES = {"ctrl": MOD_CONTROL, "control": MOD_CONTROL, "alt": MOD_ALT,
                 "shift": MOD_SHIFT, "win": MOD_WIN, "meta": MOD_WIN,
                 "windows": MOD_WIN}
_HK_VK_BY_NAME = {  # 特殊键显示名 -> 虚拟键码
    "空格": 0x20, "space": 0x20, "tab": 0x09, "回车": 0x0D, "enter": 0x0D,
    "esc": 0x1B, "home": 0x24, "end": 0x23, "pgup": 0x21, "pgdn": 0x22,
    "insert": 0x2D, "delete": 0x2E, "←": 0x25, "↑": 0x26, "→": 0x27, "↓": 0x28,
}
_HK_NAME_BY_VK = {v: k for k, v in _HK_VK_BY_NAME.items()}


def parse_hotkey(combo):
    """'Ctrl+Alt+W' -> (mods, vk)。无法解析 / 缺少 Ctrl/Alt/Win 之一返回 None。"""
    if not combo:
        return None
    parts = [p.strip() for p in combo.split("+") if p.strip()]
    if not parts:
        return None
    mods = 0
    key = None
    for p in parts:
        m = _HK_MOD_NAMES.get(p.lower())
        if m is not None:
            mods |= m
        else:
            key = p
    if key is None:
        return None
    vk = None
    if len(key) == 1 and key.isalpha():
        vk = ord(key.upper())
    elif len(key) == 1 and key.isdigit():
        vk = ord(key)
    elif len(key) >= 2 and key[0] in "fF" and key[1:].isdigit():
        n = int(key[1:])
        if 1 <= n <= 24:
            vk = 0x70 + n - 1
    else:
        vk = _HK_VK_BY_NAME.get(key.lower())
    if vk is None:
        return None
    if not (mods & (MOD_CONTROL | MOD_ALT | MOD_WIN)):
        return None
    return mods, vk

def save_config(cfg):
    """原子写配置：先写临时文件再 os.replace，避免程序中途崩溃/被杀软
    扫描时留下半截损坏的 JSON（半截 JSON 会在下次启动把整个配置静默重置）。"""
    try:
        data = json.dumps(cfg, ensure_ascii=False, indent=2)
        tmp = paths.CONFIG_FILE.with_name(paths.CONFIG_FILE.name + ".tmp")
        tmp.write_text(data, encoding="utf-8")
        os.replace(tmp, paths.CONFIG_FILE)
    except Exception:
        pass


