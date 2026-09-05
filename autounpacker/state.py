# -*- coding: utf-8 -*-
"""共享配置状态（AppState）：GUI 写、后台线程读；临时密码本机生命周期管理。"""
import json
import os
import threading

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
        self._load_temp_passwords()

    def _load_temp_passwords(self):
        """从磁盘加载临时密码：仅当是"本次系统启动"内保存的才恢复。

        临时密码生命周期=本次系统启动：程序重启（同一次开机）不丢，
        系统重启后按「开机时间点」判断自动丢弃。"""
        try:
            if not paths.TEMP_PW_FILE.exists():
                return
            data = json.loads(paths.TEMP_PW_FILE.read_text(encoding="utf-8"))
            pws = data.get("passwords") or []
            # 优先用「开机时间点」判断（同一次开机才恢复）：
            # 重启后开机时间点必然不同，可靠区分。兼容旧文件里只有 tick 的
            # 格式（重启后 tick 归零，用旧逻辑 best-effort 判断）。
            saved_boot = float(data.get("boot") or 0)
            now_boot = _boot_time()
            if saved_boot > 0 and now_boot > 0:
                same_boot = abs(now_boot - saved_boot) <= 5.0
            else:
                saved_tick = int(data.get("tick") or 0)
                now_tick = _boot_tick()
                same_boot = (saved_tick > 0 and now_tick > 0
                             and saved_tick <= now_tick)
            if same_boot:
                self._temp_passwords = [str(p) for p in pws if str(p).strip()]
        except Exception:
            self._temp_passwords = []

    def _save_temp_passwords(self):
        """把本次临时密码持久化到磁盘，并记录当前系统开机时间点。

        先写临时文件再原子替换（os.replace），避免程序在写入中途崩溃/
        被杀软扫描时留下半截损坏的 JSON，导致重启后整个临时密码表读不出来。"""
        try:
            data = json.dumps(
                {"boot": _boot_time(), "tick": _boot_tick(),
                 "passwords": list(self._temp_passwords)},
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
        """本次运行的临时密码列表"""
        with self.lock:
            return list(self._temp_passwords)

    def add_temp_password(self, p):
        """往临时密码表添加（只在本系统启动内有效，程序重启不丢）"""
        p = str(p).strip()
        if not p:
            return
        with self.lock:
            if p not in self._temp_passwords:
                self._temp_passwords.append(p)
                self._save_temp_passwords()
                return True
        return False

    def clear_temp_passwords(self):
        with self.lock:
            self._temp_passwords = []
        try:
            paths.TEMP_PW_FILE.unlink(missing_ok=True)
        except Exception:
            pass


