# -*- coding: utf-8 -*-
"""共享配置状态（AppState）：GUI 写、后台线程读；临时密码本机生命周期管理。"""
import json
import os
import threading
import time

from . import db, paths
from .config import save_config
from .utils import _boot_time, _boot_tick

class AppState:
    """共享配置（GUI 写，后台线程读）"""

    def __init__(self, cfg):
        self.cfg = cfg
        self.lock = threading.Lock()
        self.running = True
        self._temp_passwords = []
        self._temp_ts = {}          # 密码 -> 捕获时间戳（用于按有效期过期）
        self._load_temp_passwords()

    def _temp_cfg_int(self, key, default, lo, hi):
        try:
            v = int(self.cfg.get(key, default))
        except Exception:
            v = default
        return max(lo, min(hi, v))

    def _temp_max(self):
        """临时密码最多保留条数（默认 200，可在设置里改）。"""
        return self._temp_cfg_int("temp_password_max", 200, 1, 100000)

    def _temp_ttl_seconds(self):
        """临时密码有效期（秒，默认 24h，可在设置里改）。"""
        return self._temp_cfg_int("temp_password_ttl_hours", 24, 1, 24 * 365) * 3600

    @staticmethod
    def _parse_entries(data, now_boot, now):
        """把磁盘数据解析成 [(密码, 时间戳)]，兼容只存密码字符串的旧格式。

        旧格式没有逐条时间，用「本次开机时间点」作为保守年龄：正常每天重启的
        用户（本次开机 < 有效期）不受影响；而 Fast Startup 导致「本次开机」被
        拉到几十天的用户，陈旧条目会被有效期正常淘汰。"""
        entries = data.get("entries")
        if isinstance(entries, list):
            out = []
            for e in entries:
                if not isinstance(e, dict):
                    continue
                p = str(e.get("p") or "").strip()
                if not p:
                    continue
                try:
                    t = float(e.get("t") or 0)
                except Exception:
                    t = 0.0
                out.append((p, t if t > 0 else now))
            return out
        base = now_boot if now_boot > 0 else now
        return [(str(p), base) for p in (data.get("passwords") or []) if str(p).strip()]

    def _prune_temp(self):
        """按有效期与条数上限裁剪（调用方持锁）。"""
        now = time.time()
        ttl = self._temp_ttl_seconds()
        items = [(p, self._temp_ts.get(p, now)) for p in self._temp_passwords]
        items = [(p, t) for (p, t) in items if (now - t) <= ttl]
        items.sort(key=lambda x: x[1])
        items = items[-self._temp_max():]
        self._temp_passwords = [p for p, _ in items]
        self._temp_ts = dict(items)

    def _load_temp_passwords(self):
        """从磁盘加载临时密码：需「同一次开机」且未超有效期、不超条数上限。

        临时密码生命周期 = 本次系统启动（程序重启不丢）；再叠加有效期/上限，
        防止 Fast Startup（每天关机不重置 uptime）导致旧密码长期堆积。
        系统重启后按「开机时间点」判断自动丢弃。"""
        now = time.time()
        ttl = self._temp_ttl_seconds()
        try:
            if not paths.TEMP_PW_FILE.exists():
                return
            data = json.loads(paths.TEMP_PW_FILE.read_text(encoding="utf-8"))
            saved_boot = float(data.get("boot") or 0)
            now_boot = _boot_time()
            if saved_boot > 0 and now_boot > 0:
                same_boot = abs(now_boot - saved_boot) <= 5.0
            else:
                saved_tick = int(data.get("tick") or 0)
                now_tick = _boot_tick()
                same_boot = (saved_tick > 0 and now_tick > 0
                             and saved_tick <= now_tick)
            if not same_boot:
                return
            items = self._parse_entries(data, now_boot, now)
            items = [(p, t) for (p, t) in items if (now - t) <= ttl]
            items.sort(key=lambda x: x[1])
            items = items[-self._temp_max():]
            self._temp_passwords = [p for p, _ in items]
            self._temp_ts = dict(items)
            # 顺带把磁盘规整为新格式（旧格式被裁剪后的实际结果落盘，避免残留）
            self._save_temp_passwords()
        except Exception:
            self._temp_passwords = []
            self._temp_ts = {}

    def _save_temp_passwords(self):
        """把本次临时密码持久化到磁盘，并记录当前系统开机时间点。

        先写临时文件再原子替换（os.replace），避免程序在写入中途崩溃/
        被杀软扫描时留下半截损坏的 JSON，导致重启后整个临时密码表读不出来。"""
        try:
            data = json.dumps(
                {"boot": _boot_time(), "tick": _boot_tick(),
                 "entries": [{"p": p, "t": self._temp_ts.get(p, 0)}
                             for p in self._temp_passwords]},
                ensure_ascii=False)
            tmp = paths.TEMP_PW_FILE.with_suffix(".tmp")
            tmp.write_text(data, encoding="utf-8")
            os.replace(tmp, paths.TEMP_PW_FILE)
        except Exception:
            pass

    def snapshot(self):
        with self.lock:
            return json.loads(json.dumps(self.cfg))

    def set(self, key, value, save=True):
        with self.lock:
            self.cfg[key] = value
        if save:
            save_config(self.cfg)

    def update_path(self, idx, field, value):
        with self.lock:
            if 0 <= idx < len(self.cfg["watch_paths"]):
                self.cfg["watch_paths"][idx][field] = value
        save_config(self.cfg)

    def set_path(self, idx, entry):
        with self.lock:
            if 0 <= idx < len(self.cfg["watch_paths"]):
                self.cfg["watch_paths"][idx] = entry
        save_config(self.cfg)

    # ---------- 共享密码本（存于 toolbox.db） ----------
    def passwords(self):
        """长期密码本"""
        return db.get_passwords()

    def set_passwords(self, plist):
        """覆盖长期密码本"""
        db.set_passwords(plist)

    def add_long_password(self, p):
        """往长期密码本里追加一个密码"""
        db.add_password(p, source="manual")

    def auto_add(self):
        with self.lock:
            return bool(self.cfg.get("auto_add_clipboard_password", False))

    def set_auto_add(self, flag):
        self.set("auto_add_clipboard_password", bool(flag))

    def all_passwords(self):
        """长期密码本 + 本次运行的临时密码（去重、临时密码靠后）"""
        with self.lock:
            result = db.get_passwords()
            for p in self._temp_passwords:
                if p not in result:
                    result.append(p)
            return result

    def temp_passwords(self):
        """本次运行的临时密码列表（读取时顺带按有效期裁剪）"""
        with self.lock:
            self._prune_temp()
            return list(self._temp_passwords)

    def add_temp_password(self, p):
        """往临时密码表添加（本次开机内有效，程序重启不丢；叠加有效期/上限）。

        返回是否为「新」密码（重复复制同一内容只刷新其时间、不重复记录）。"""
        p = str(p).strip()
        if not p:
            return False
        with self.lock:
            first = p not in self._temp_passwords
            self._temp_ts[p] = time.time()
            if first:
                self._temp_passwords.append(p)
            self._prune_temp()
            self._save_temp_passwords()
            return first

    def clear_temp_passwords(self):
        with self.lock:
            self._temp_passwords = []
            self._temp_ts = {}
        try:
            paths.TEMP_PW_FILE.unlink(missing_ok=True)
        except Exception:
            pass


