# -*- coding: utf-8 -*-
"""
统一 SQLite 小数据库（toolbox.db）
- 共享密码本（原存 config.json）
- 密码字典（原存 ~/.smart_extract_password_dict.json）
- 未来查表功能在此追加新表即可
"""
import json
import sqlite3
import threading
import time
from pathlib import Path

from .paths import DATA_DIR as APP_DIR
DB_FILE = APP_DIR / "toolbox.db"
LEGACY_DICT_FILE = Path.home() / ".smart_extract_password_dict.json"

_lock = threading.Lock()

_SCHEMA = """
CREATE TABLE IF NOT EXISTS passwords (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    password TEXT NOT NULL UNIQUE,
    created_at INTEGER NOT NULL,
    source TEXT NOT NULL DEFAULT 'manual'
);
CREATE TABLE IF NOT EXISTS password_dict (
    password TEXT PRIMARY KEY,
    used_count INTEGER NOT NULL DEFAULT 0,
    last_used_at INTEGER NOT NULL DEFAULT 0
);
"""


def _connect():
    conn = sqlite3.connect(str(DB_FILE), check_same_thread=False)
    try:
        conn.execute("PRAGMA journal_mode=WAL")
        conn.execute("PRAGMA synchronous=NORMAL")
    except Exception:
        pass
    conn.executescript(_SCHEMA)
    return conn


def init_db():
    with _lock:
        conn = _connect()
        try:
            conn.commit()
        finally:
            conn.close()


def _execute(sql, params=(), fetch=False):
    with _lock:
        conn = _connect()
        try:
            cur = conn.execute(sql, params)
            conn.commit()
            if fetch:
                return cur.fetchall()
            return cur.lastrowid
        finally:
            conn.close()


# ---------- 共享密码本 ----------
def get_passwords():
    rows = _execute("SELECT password FROM passwords ORDER BY id", fetch=True)
    return [r[0] for r in rows]


def set_passwords(plist):
    with _lock:
        conn = _connect()
        try:
            conn.execute("DELETE FROM passwords")
            now = int(time.time())
            for p in plist or []:
                p = str(p).strip()
                if p:
                    conn.execute(
                        "INSERT OR IGNORE INTO passwords (password, created_at) VALUES (?, ?)",
                        (p, now))
            conn.commit()
        finally:
            conn.close()


def add_password(p, source="manual"):
    p = str(p).strip()
    if not p:
        return
    with _lock:
        conn = _connect()
        try:
            conn.execute(
                "INSERT OR IGNORE INTO passwords (password, created_at, source) VALUES (?, ?, ?)",
                (p, int(time.time()), source))
            conn.commit()
        finally:
            conn.close()


# ---------- 密码字典 ----------
def load_password_dict():
    rows = _execute(
        "SELECT password, used_count, last_used_at FROM password_dict", fetch=True)
    return {r[0]: {"password": r[0], "used_count": r[1], "last_used_at": r[2]} for r in rows}


def save_password_dict(data):
    with _lock:
        conn = _connect()
        try:
            conn.execute("DELETE FROM password_dict")
            for pw, e in (data or {}).items():
                conn.execute(
                    "INSERT OR REPLACE INTO password_dict (password, used_count, last_used_at) "
                    "VALUES (?, ?, ?)",
                    (str(pw), int(e.get("used_count", 0) or 0),
                     int(e.get("last_used_at", 0) or 0)))
            conn.commit()
        finally:
            conn.close()


def get_dict_passwords():
    rows = _execute(
        "SELECT password FROM password_dict ORDER BY used_count DESC, password", fetch=True)
    return [r[0] for r in rows]


def add_dict_password(password):
    password = str(password or "").strip()
    if not password:
        return
    with _lock:
        conn = _connect()
        try:
            now = int(time.time())
            conn.execute(
                "INSERT INTO password_dict (password, used_count, last_used_at) VALUES (?, 1, ?) "
                "ON CONFLICT(password) DO UPDATE SET "
                "used_count = used_count + 1, last_used_at = ?",
                (password, now, now))
            conn.commit()
        finally:
            conn.close()


# ---------- 旧数据迁移 ----------
def migrate_legacy(config, legacy_dict_file=None):
    """把 config.json 里的密码列表和旧字典 json 迁移进数据库。

    返回 config 是否被修改（需要调用方保存 config）。
    """
    changed = False
    old = list((config or {}).get("passwords") or [])
    old = [str(p).strip() for p in old if str(p).strip()]
    if old:
        set_passwords(old)
        config["passwords"] = []
        changed = True

    if legacy_dict_file and Path(legacy_dict_file).exists():
        try:
            data = json.loads(Path(legacy_dict_file).read_text(encoding="utf-8"))
        except Exception:
            data = None
        if data:
            save_password_dict(data)
            try:
                Path(legacy_dict_file).rename(str(legacy_dict_file) + ".migrated")
            except Exception:
                pass
    return changed
