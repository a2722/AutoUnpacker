# -*- coding: utf-8 -*-
"""统一 SQLite 小数据库（toolbox.db）：共享密码本、密码字典、百度粘性记忆、旧数据迁移。

职责：- 维护 passwords / password_dict / baidu_sticky / share_code_map 四张表（连接时自动建表，WAL 模式）
- 共享密码本与密码字典的增删查（原存 config.json 与 ~/.smart_extract_password_dict.json）
- 百度清单模式的粘性记忆（sticky_remember/sticky_known/sticky_list/sticky_prune）
- 特殊用户固定提取码（find_share_code/find_share_entry/get_share_code_map/set_share_code_map/add_share_code）
- migrate_legacy() 把旧 config 密码列表 / 旧字典 json 一次性迁入数据库
关键入口：init_db() / get_passwords() / add_password() / load_password_dict() / sticky_remember() / find_share_code() / migrate_legacy()
依赖：sqlite3、paths.DATA_DIR
注意：所有操作持模块级线程锁且 check_same_thread=False；未来查表功能在此追加新表即可
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
CREATE TABLE IF NOT EXISTS baidu_sticky (
    path TEXT PRIMARY KEY,
    first_seen INTEGER NOT NULL,
    last_seen INTEGER NOT NULL,
    kind TEXT NOT NULL DEFAULT 'file',
    note TEXT NOT NULL DEFAULT ''
);
CREATE TABLE IF NOT EXISTS share_code_map (
    share_uk TEXT PRIMARY KEY,
    code TEXT NOT NULL,
    note TEXT NOT NULL DEFAULT '',
    updated_at INTEGER NOT NULL DEFAULT 0,
    pick INTEGER NOT NULL DEFAULT 0
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
    # 老库的 share_code_map 可能已存在但缺 pick 列（CREATE TABLE IF NOT EXISTS 不会补列），
    # 而「需要挑选」标记是后加的，所以这里显式补一列：列已存在时 SQLite 报
    # duplicate column name，直接忽略即可——迁移幂等、只增列、不改动任何旧数据、绝不抛错。
    try:
        conn.execute("ALTER TABLE share_code_map ADD COLUMN pick INTEGER NOT NULL DEFAULT 0")
    except Exception:
        pass
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


# ---------- 百度清单模式：粘性记忆 ----------
def sticky_remember(path, kind="file", note=""):
    """记住一个「属于本次网盘下载」的路径（跨重启 / 客户端清历史后仍认得）。"""
    p = str(path or "").strip()
    if not p:
        return
    now = int(time.time())
    with _lock:
        conn = _connect()
        try:
            conn.execute(
                "INSERT INTO baidu_sticky (path, first_seen, last_seen, kind, note) "
                "VALUES (?, ?, ?, ?, ?) "
                "ON CONFLICT(path) DO UPDATE SET last_seen = ?, kind = ?, note = ?",
                (p, now, now, str(kind), str(note), now, str(kind), str(note)))
            conn.commit()
        finally:
            conn.close()


def sticky_known(path):
    rows = _execute("SELECT 1 FROM baidu_sticky WHERE path = ? LIMIT 1",
                    (str(path or ""),), fetch=True)
    return bool(rows)


def sticky_list(limit=1000):
    return _execute(
        "SELECT path, first_seen, last_seen, kind, note FROM baidu_sticky "
        "ORDER BY last_seen DESC LIMIT ?", (int(limit),), fetch=True)


def sticky_prune(keep_days=7):
    """清理长期未再出现的记录（默认保留 7 天）。"""
    cutoff = int(time.time()) - int(keep_days) * 86400
    _execute("DELETE FROM baidu_sticky WHERE last_seen < ?", (cutoff,))


# ---------- 特殊用户固定提取码 ----------
def _pick_flag(value):
    """把「需要挑选」标记归一化为 0/1：兼容 bool、整数、"1"/"0" 及 int-ish 写法，绝不抛错。"""
    try:
        if isinstance(value, str):
            t = value.strip().lower()
            if t in ("1", "true", "yes", "on", "pick", "挑选"):
                return 1
            if t in ("", "0", "false", "no", "off", "none"):
                return 0
            return 1 if float(t) else 0
        return 1 if int(value) else 0
    except Exception:
        return 0


def find_share_entry(share_uk):
    """查某个分享者 uk 的完整映射：share_uk/code/note/pick/updated_at；没有记录或 uk 为空返回 None。

    pick 为 0/1（0=整包下载，1=需要挑选文件）；任何异常都不抛，按「无记录」处理。
    """
    uk = str(share_uk or "").strip()
    if not uk:
        return None
    try:
        rows = _execute(
            "SELECT share_uk, code, note, pick, updated_at FROM share_code_map "
            "WHERE share_uk = ? LIMIT 1",
            (uk,), fetch=True)
        if rows:
            r = rows[0]
            return {"share_uk": r[0], "code": r[1], "note": r[2],
                    "pick": _pick_flag(r[3]), "updated_at": r[4]}
    except Exception:
        pass
    return None


def find_share_code(share_uk):
    """查某个分享者 uk 的固定提取码（没有记录或 uk 为空时返回 None）。"""
    uk = str(share_uk or "").strip()
    if not uk:
        return None
    try:
        rows = _execute(
            "SELECT code FROM share_code_map WHERE share_uk = ? LIMIT 1",
            (uk,), fetch=True)
        if rows:
            return rows[0][0]
    except Exception:
        pass
    return None


def get_share_code_map():
    """返回全部固定提取码：share_uk/code/note/pick/updated_at 五字段列表（pick 为 0/1）。"""
    try:
        rows = _execute(
            "SELECT share_uk, code, note, pick, updated_at FROM share_code_map",
            fetch=True)
        return [{"share_uk": r[0], "code": r[1], "note": r[2],
                 "pick": _pick_flag(r[3]), "updated_at": r[4]}
                for r in (rows or [])]
    except Exception:
        return []


def set_share_code_map(items):
    """整体覆盖固定提取码表：先清空再写入；非法条目跳过，同 uk 后者覆盖前者。

    每个条目可带可选的 "pick"（缺省 0），值兼容 True/False、"1"/"0" 等 int-ish 写法。
    """
    try:
        clean = {}
        for it in items or []:
            if not isinstance(it, dict):
                continue
            uk = str(it.get("share_uk") or "").strip()
            code = str(it.get("code") or "").strip()
            if not uk or not code:
                continue
            clean[uk] = (code, str(it.get("note") or ""), _pick_flag(it.get("pick", 0)))
        with _lock:
            conn = _connect()
            try:
                conn.execute("DELETE FROM share_code_map")
                now = int(time.time())
                for uk, (code, note, pick) in clean.items():
                    conn.execute(
                        "INSERT INTO share_code_map (share_uk, code, note, pick, updated_at) "
                        "VALUES (?, ?, ?, ?, ?)",
                        (uk, code, note, pick, now))
                conn.commit()
            finally:
                conn.close()
        return True
    except Exception:
        return False


def add_share_code(share_uk, code, note="", pick=0):
    """新增/更新单个分享者的固定提取码（同 uk UPSERT，后者覆盖前者；pick=1 表示需要挑选）。"""
    uk = str(share_uk or "").strip()
    cd = str(code or "").strip()
    if not uk or not cd:
        return False
    try:
        now = int(time.time())
        pv = _pick_flag(pick)
        with _lock:
            conn = _connect()
            try:
                conn.execute(
                    "INSERT INTO share_code_map (share_uk, code, note, pick, updated_at) "
                    "VALUES (?, ?, ?, ?, ?) "
                    "ON CONFLICT(share_uk) DO UPDATE SET "
                    "code = ?, note = ?, pick = ?, updated_at = ?",
                    (uk, cd, str(note or ""), pv, now, cd, str(note or ""), pv, now))
                conn.commit()
            finally:
                conn.close()
        return True
    except Exception:
        return False


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
