# -*- coding: utf-8 -*-
"""统一 SQLite 小数据库（toolbox.db）：共享密码本、密码字典、百度粘性记忆、任务/日志索引、旧数据迁移。

职责：- 维护 passwords / password_dict / baidu_sticky / tasks / log_index 五张表（连接时自动建表，WAL 模式）
- 共享密码本与密码字典的增删查（原存 config.json 与 ~/.smart_extract_password_dict.json）
- 百度清单模式的粘性记忆（sticky_remember/sticky_known/sticky_list/sticky_prune）
- 任务表与日志索引（add_task/find_open_task/update_task_state/get_task/list_tasks/count_tasks/task_logs；add_log_index/query_logs/prune_logs/prune_tasks）
- migrate_legacy() 把旧 config 密码列表 / 旧字典 json 一次性迁入数据库
关键入口：init_db() / get_passwords() / set_passwords() / list_passwords() / add_password() /
          update_password() / delete_password() / load_password_dict() / add_task() / add_log_index() / migrate_legacy()
依赖：sqlite3、paths.DATA_DIR
注意：所有操作持模块级线程锁且 check_same_thread=False；log_index.source_dir 为 NULL 表示全局日志（绝不用空字符串）
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

# 最近一次「列表 / 查询」类读取失败的原始错误文本；None = 最近一次读取成功。
# 供 UI 在列表突然为空时区分「确实没有数据」与「数据库读取出错」，不改任何函数签名。
_LAST_ERROR = None

# 已完成「老库补列」迁移的库文件路径集合：每个库文件每进程只迁移一次。
# 所有调用 _connect() 的函数都持 _lock，因此该集合的读写受同一把锁保护。
_migrated = set()

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
CREATE TABLE IF NOT EXISTS tasks (
  id            INTEGER PRIMARY KEY AUTOINCREMENT,
  file_name     TEXT    NOT NULL,
  file_size     INTEGER,
  source_dir    TEXT,
  output_dir    TEXT,
  mode          TEXT,
  state         TEXT    NOT NULL,
  layer         INTEGER DEFAULT 1,
  password_src  TEXT,
  error         TEXT,
  created_at    INTEGER NOT NULL,
  started_at    INTEGER,
  finished_at   INTEGER
);
CREATE INDEX IF NOT EXISTS idx_tasks_state   ON tasks(state);
CREATE INDEX IF NOT EXISTS idx_tasks_source  ON tasks(source_dir);
CREATE INDEX IF NOT EXISTS idx_tasks_created ON tasks(created_at DESC);

CREATE TABLE IF NOT EXISTS log_index (
  id         INTEGER PRIMARY KEY AUTOINCREMENT,
  ts         INTEGER NOT NULL,
  level      TEXT,
  source_dir TEXT,
  task_id    INTEGER,
  text       TEXT,
  link       TEXT
);
CREATE INDEX IF NOT EXISTS idx_log_ts     ON log_index(ts DESC);
CREATE INDEX IF NOT EXISTS idx_log_task   ON log_index(task_id);
CREATE INDEX IF NOT EXISTS idx_log_source ON log_index(source_dir);
"""


def _connect():
    conn = sqlite3.connect(str(DB_FILE), check_same_thread=False)
    try:
        conn.execute("PRAGMA journal_mode=WAL")
        conn.execute("PRAGMA synchronous=NORMAL")
    except Exception:
        pass
    # 建表脚本（_SCHEMA 全部 CREATE TABLE/INDEX）与老库补列迁移（CREATE TABLE IF
    # NOT EXISTS 不会补列）每个库文件每进程只跑一次。调用方均持 _lock，故 _migrated
    # 的读写受现有锁保护；老库首次使用仍会补列，换一个 DB 路径仍会建表+迁移，
    # 迁移失败照旧吞掉、应用照常可用——只是不再在每次查询前重复执行整套 schema
    # 脚本与两条同样的 ALTER（旧实现每次都跑，是应用最热的 DB 开销，尤其每条日志
    # 追加都会 _connect 一次）。
    # 注意：PRAGMA journal_mode/synchronous 仍是逐连接设置（synchronous 本就是
    # 连接级、不随库持久化），故保留在 gated 块之外，语义不变。
    if str(DB_FILE) not in _migrated:
        conn.executescript(_SCHEMA)
        # 旧的分享者提取码映射功能已整条退场：老库里可能还留着 share_code_map
        # 表，这里幂等删除（DROP TABLE IF EXISTS），新库本就没有该表、执行即空操作。
        # 老库的 passwords 可能已存在但缺 note 列（行级 API 的备注是后加的），
        # 显式补一列且忽略「列已存在」：迁移幂等、只增列、不动旧行、绝不抛错。
        for stmt in (
            "DROP TABLE IF EXISTS share_code_map",
            "ALTER TABLE passwords ADD COLUMN note TEXT NOT NULL DEFAULT ''",
        ):
            try:
                conn.execute(stmt)
            except Exception:
                pass
        _migrated.add(str(DB_FILE))
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


def last_error():
    """返回最近一次「列表 / 查询」类读取失败的错误文本；成功读取后为 None。"""
    return _LAST_ERROR


# ---------- 共享密码本 ----------
def get_passwords():
    rows = _execute("SELECT password FROM passwords ORDER BY id", fetch=True)
    return [r[0] for r in rows]


def set_passwords(plist):
    """整体覆盖长期密码本：按列表顺序重写（去空白 / 去重 / 保序）。

    备注与来源不再随重写丢失：重写前先记下每个口令已有的 note / source，重写时
    按口令原样写回——「排序保存 / 旧弹窗保存」这类整表路径不再静默清空行级 API
    写入的备注（并被移出列表的口令照旧删除）。旧库没有 note 列时由 _connect()
    补列，缺省为空串。
    """
    clean = []
    seen = set()
    for p in plist or []:
        p = str(p).strip()
        if p and p not in seen:
            seen.add(p)
            clean.append(p)
    with _lock:
        conn = _connect()
        try:
            old = {}
            try:
                for pw, note, source in conn.execute(
                        "SELECT password, note, source FROM passwords"):
                    old[str(pw)] = (str(note or ""), str(source or "manual"))
            except Exception:
                old = {}                   # 极端老表缺列时按「无备注」处理，不阻断重写
            conn.execute("DELETE FROM passwords")
            now = int(time.time())
            for p in clean:
                note, source = old.get(p, ("", "manual"))
                conn.execute(
                    "INSERT OR IGNORE INTO passwords (password, created_at, source, note) "
                    "VALUES (?, ?, ?, ?)",
                    (p, now, source, note))
            conn.commit()
        finally:
            conn.close()


def list_passwords():
    """行级读取长期密码本，返回按 id 升序（= 解压尝试顺序，与 get_passwords() 同序）的行列表：

    [{"id", "password", "source", "created_at", "note"}, ...]

    每行的 source 是库里真实存储的来源值（老行缺省 'manual'），note 缺省 ''；任何异常返回 []，
    并把错误文本记入 db.last_error()（成功读取后复位为 None）。
    """
    global _LAST_ERROR
    try:
        rows = _execute(
            "SELECT id, password, source, created_at, note FROM passwords ORDER BY id",
            fetch=True)
        _LAST_ERROR = None
        return [{"id": r[0], "password": r[1], "source": r[2],
                 "created_at": r[3], "note": r[4] or ""} for r in (rows or [])]
    except Exception as e:
        _LAST_ERROR = str(e)
        return []


def add_password(p, source="manual", note=""):
    """新增一条长期口令，返回该行 id（供页面行级定位 / 编辑 / 删除）。

    重复口令沿用原有 INSERT OR IGNORE + UNIQUE 语义：绝不覆盖、绝不抛错；
    被忽略时 INSERT 的 lastrowid 会残留旧值，故改为按口令回查已有行 id 返回。
    空口令或任何异常返回 0。
    """
    p = str(p).strip()
    if not p:
        return 0
    try:
        with _lock:
            conn = _connect()
            try:
                cur = conn.execute(
                    "INSERT OR IGNORE INTO passwords (password, created_at, source, note) "
                    "VALUES (?, ?, ?, ?)",
                    (p, int(time.time()), str(source or "manual"), str(note or "")))
                conn.commit()
                if cur.rowcount:
                    return int(cur.lastrowid or 0)
                row = conn.execute(
                    "SELECT id FROM passwords WHERE password = ? LIMIT 1", (p,)).fetchone()
                return int(row[0]) if row else 0
            finally:
                conn.close()
    except Exception:
        return 0


def update_password(pid, password=None, note=None, source=None):
    """按行 id 更新长期密码本，只写入显式提供（非 None）的字段，返回是否命中该行。

    - password 去首尾空白后为空视为非法，整次放弃（返回 False，不改任何列）；
    - 新口令与别的行重复会触发 UNIQUE 约束，同样返回 False 且不改动任何行；
    - 一个字段都没提供时返回 False；任何异常都不抛。
    """
    try:
        tid = int(pid)
    except Exception:
        return False
    if tid <= 0:
        return False
    sets = []
    params = []
    if password is not None:
        p = str(password).strip()
        if not p:
            return False
        sets.append("password = ?")
        params.append(p)
    if note is not None:
        sets.append("note = ?")
        params.append(str(note))
    if source is not None:
        sets.append("source = ?")
        params.append(str(source))
    if not sets:
        return False
    params.append(tid)
    try:
        with _lock:
            conn = _connect()
            try:
                cur = conn.execute(
                    "UPDATE passwords SET " + ", ".join(sets) + " WHERE id = ?", params)
                conn.commit()
                return cur.rowcount > 0
            finally:
                conn.close()
    except Exception:
        return False


def delete_password(pid):
    """按行 id 精确删除一行长期口令（同名口令也只删这一行），返回是否命中；任何异常都不抛。"""
    try:
        tid = int(pid)
    except Exception:
        return False
    if tid <= 0:
        return False
    try:
        with _lock:
            conn = _connect()
            try:
                cur = conn.execute("DELETE FROM passwords WHERE id = ?", (tid,))
                conn.commit()
                return cur.rowcount > 0
            finally:
                conn.close()
    except Exception:
        return False


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


def delete_dict_password(password):
    """从密码字典按口令精确删除一条记录（不影响密码本），返回是否命中；任何异常都不抛。"""
    pw = str(password or "")
    if not pw:
        return False
    try:
        with _lock:
            conn = _connect()
            try:
                cur = conn.execute(
                    "DELETE FROM password_dict WHERE password = ?", (pw,))
                conn.commit()
                return cur.rowcount > 0
            finally:
                conn.close()
    except Exception:
        return False


def clear_password_dict():
    """清空密码字典（命中次数 / 最近命中统计随之归零），返回删除条数；失败返回 -1，任何异常都不抛。"""
    try:
        with _lock:
            conn = _connect()
            try:
                cur = conn.execute("DELETE FROM password_dict")
                conn.commit()
                return max(0, int(cur.rowcount or 0))
            finally:
                conn.close()
    except Exception:
        return -1


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


# ---------- 任务表（tasks）与日志索引（log_index） ----------
# 设计口径（M1 数据层）：
# - tasks 一行 = 一次「真正开始」的解压尝试；defer/跳过路径绝不建行，保证不会留下
#   永远停在 queued/extracting 的僵尸行。
# - log_index.source_dir 为 NULL 表示全局日志（轮询/剪贴板/二维码/更新/启动），
#   绝不用空字符串表达「全局」；按路径筛选时 NULL 行永远通过。
_TASK_COLS = ("id", "file_name", "file_size", "source_dir", "output_dir", "mode",
              "state", "layer", "password_src", "error", "created_at",
              "started_at", "finished_at")
_LOG_COLS = ("id", "ts", "level", "source_dir", "task_id", "text", "link")


def _task_row(row):
    """把 tasks 行元组转成字段字典（列顺序与 _TASK_COLS 一致）。"""
    return dict(zip(_TASK_COLS, row))


def _log_row(row):
    """把 log_index 行元组转成字段字典（列顺序与 _LOG_COLS 一致）。"""
    return dict(zip(_LOG_COLS, row))


def add_task(file_name, file_size=None, source_dir=None, output_dir=None,
             mode=None, state="queued", layer=1):
    """新建一个任务行，返回 task_id（失败返回 0）。

    只在解压真正开始前调用；分卷未到齐 / 文件被占用等「稍后重试」路径不建行。
    """
    try:
        with _lock:
            conn = _connect()
            try:
                cur = conn.execute(
                    "INSERT INTO tasks (file_name, file_size, source_dir, output_dir, "
                    "mode, state, layer, created_at) VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
                    (str(file_name or ""), file_size, source_dir, output_dir, mode,
                     str(state or "queued"), int(layer or 1), int(time.time())))
                conn.commit()
                return int(cur.lastrowid or 0)
            finally:
                conn.close()
    except Exception:
        return 0


def _norm_path_key(p):
    """路径归一化：统一分隔符 / 小写 / 去尾部斜杠（仅用于比较，不改库中存储）。"""
    return str(p or "").replace("\\", "/").rstrip("/").lower()


def find_open_task(source_dir, file_name):
    """查该 (source_dir, file_name) 最新一条非终态任务（queued/extracting/need_password）。

    供监控线程「稍后重试」复用同一行：重试不再新建任务行，队列不会堆死行；
    没有匹配或异常时返回 0（调用方据此新建）。
    """
    try:
        name = str(file_name or "")
        if not name:
            return 0
        want = _norm_path_key(source_dir)
        with _lock:
            conn = _connect()
            try:
                cur = conn.execute(
                    "SELECT id, source_dir FROM tasks WHERE file_name = ? "
                    "AND state IN ('queued','extracting','need_password') "
                    "ORDER BY id DESC",
                    (name,))
                for tid, src in cur.fetchall():
                    if _norm_path_key(src) == want:
                        return int(tid)
                return 0
            finally:
                conn.close()
    except Exception:
        return 0


def update_task_state(task_id, state, *, error=None, password_src=None,
                      output_dir=None, started_at=None, finished_at=None,
                      layer=None):
    """更新任务状态（可同时补写终态字段），返回是否命中行；任何异常都不抛。

    只有显式传入（非 None）的字段才会被写入，避免把已有值误覆盖成 NULL。
    """
    try:
        tid = int(task_id or 0)
    except Exception:
        return False
    if tid <= 0:
        return False
    sets = ["state = ?"]
    params = [str(state or "")]
    for col, val in (("error", error), ("password_src", password_src),
                     ("output_dir", output_dir), ("started_at", started_at),
                     ("finished_at", finished_at), ("layer", layer)):
        if val is not None:
            sets.append(col + " = ?")
            params.append(val)
    params.append(tid)
    try:
        with _lock:
            conn = _connect()
            try:
                cur = conn.execute(
                    "UPDATE tasks SET " + ", ".join(sets) + " WHERE id = ?", params)
                conn.commit()
                return cur.rowcount > 0
            finally:
                conn.close()
    except Exception:
        return False


def get_task(task_id):
    """按 id 取单个任务（dict）；不存在 / 非法 id / 异常一律返回 None。"""
    try:
        tid = int(task_id or 0)
    except Exception:
        return None
    if tid <= 0:
        return None
    try:
        rows = _execute(
            "SELECT " + ", ".join(_TASK_COLS) + " FROM tasks WHERE id = ? LIMIT 1",
            (tid,), fetch=True)
    except Exception:
        return None
    return _task_row(rows[0]) if rows else None


def list_tasks(scope="queue", result_filter=None, keyword=None, limit=500):
    """按「队列 / 历史」范围列出任务（created_at 倒序），支持失败过滤与关键词。

    scope="queue"   → state IN ('queued','extracting','need_password')
    scope="history" → state IN ('done','failed','canceled')
    result_filter="failed" 覆盖 scope，只返回 state='failed'。
    keyword：大小写不敏感子串，匹配 file_name 或 output_dir。
    """
    global _LAST_ERROR
    if result_filter == "failed":
        states = ("failed",)
    elif scope == "history":
        states = ("done", "failed", "canceled")
    else:
        states = ("queued", "extracting", "need_password")
    sql = ("SELECT " + ", ".join(_TASK_COLS) + " FROM tasks WHERE state IN ("
           + ", ".join("?" for _ in states) + ")")
    params = list(states)
    if keyword:
        sql += (" AND (instr(lower(COALESCE(file_name, '')), ?) > 0"
                " OR instr(lower(COALESCE(output_dir, '')), ?) > 0)")
        kw = str(keyword).lower()
        params.append(kw)
        params.append(kw)
    sql += " ORDER BY created_at DESC, id DESC LIMIT ?"
    params.append(int(limit or 500))
    try:
        rows = _execute(sql, tuple(params), fetch=True)
        _LAST_ERROR = None
        return [_task_row(r) for r in (rows or [])]
    except Exception as e:
        _LAST_ERROR = str(e)
        return []


def count_tasks():
    """统计各状态任务数，返回固定键字典（缺省 0），供底栏 / 分段控件直接使用。"""
    out = {"queue": 0, "queued": 0, "extracting": 0, "need_password": 0,
           "history": 0, "done": 0, "failed": 0, "canceled": 0}
    try:
        rows = _execute("SELECT state, COUNT(*) FROM tasks GROUP BY state", fetch=True)
    except Exception:
        return out
    for state, n in (rows or []):
        if state in out:
            out[state] = int(n or 0)
    out["queue"] = out["queued"] + out["extracting"] + out["need_password"]
    out["history"] = out["done"] + out["failed"] + out["canceled"]
    return out


def task_logs(task_id, limit=2000):
    """取某任务的日志索引行（ts 正序、同秒按 id 正序），供「该任务日志」视图。"""
    global _LAST_ERROR
    try:
        tid = int(task_id or 0)
    except Exception:
        return []
    if tid <= 0:
        return []
    try:
        rows = _execute(
            "SELECT " + ", ".join(_LOG_COLS) + " FROM log_index "
            "WHERE task_id = ? ORDER BY ts ASC, id ASC LIMIT ?",
            (tid, int(limit or 2000)), fetch=True)
        _LAST_ERROR = None
        return [_log_row(r) for r in (rows or [])]
    except Exception as e:
        _LAST_ERROR = str(e)
        return []


def add_log_index(ts, level, text, source_dir=None, task_id=None, link=None):
    """写一行日志索引，返回 rowid（失败返回 0）；source_dir 为空一律存 NULL（全局）。"""
    try:
        with _lock:
            conn = _connect()
            try:
                cur = conn.execute(
                    "INSERT INTO log_index (ts, level, source_dir, task_id, text, link) "
                    "VALUES (?, ?, ?, ?, ?, ?)",
                    (int(ts), str(level or ""), source_dir or None, task_id,
                     str(text or ""), link))
                conn.commit()
                return int(cur.lastrowid or 0)
            finally:
                conn.close()
    except Exception:
        return 0


def query_logs(levels=None, sources=None, keyword=None, limit=1000):
    """查询日志索引（ts 倒序、同秒按 id 倒序），级别 / 路径 / 关键词三滤镜可叠加。

    - levels：级别集合；None/空 = 不按级别过滤
    - sources：路径集合；source_dir 为 NULL 的全局日志**永远通过**
    - keyword：大小写不敏感子串（在 text 内匹配）

    SQL 与「先取全量再按 Python 过滤」等价：级别用 IN，路径用
    `source_dir IS NULL OR source_dir IN (...)`，关键词用 instr(lower(text))。
    """
    global _LAST_ERROR
    try:
        lv = list(levels) if levels else []
        src = list(sources) if sources else []
        sql = "SELECT " + ", ".join(_LOG_COLS) + " FROM log_index"
        where = []
        params = []
        if lv:
            where.append("level IN (" + ", ".join("?" for _ in lv) + ")")
            params.extend(lv)
        if src:
            where.append("(source_dir IS NULL OR source_dir IN ("
                         + ", ".join("?" for _ in src) + "))")
            params.extend(src)
        if keyword:
            where.append("instr(lower(COALESCE(text, '')), ?) > 0")
            params.append(str(keyword).lower())
        if where:
            sql += " WHERE " + " AND ".join(where)
        sql += " ORDER BY ts DESC, id DESC LIMIT ?"
        params.append(int(limit or 1000))
        rows = _execute(sql, tuple(params), fetch=True)
        _LAST_ERROR = None
        return [_log_row(r) for r in (rows or [])]
    except Exception as e:
        _LAST_ERROR = str(e)
        return []


def prune_logs(before_ts):
    """删除 ts < before_ts 的日志索引行，返回删除行数（失败返回 0）。"""
    try:
        with _lock:
            conn = _connect()
            try:
                cur = conn.execute("DELETE FROM log_index WHERE ts < ?",
                                   (int(before_ts),))
                conn.commit()
                return max(0, int(cur.rowcount or 0))
            finally:
                conn.close()
    except Exception:
        return 0


def delete_task(task_id):
    """彻底删除一个任务：tasks 行 + 该任务的日志索引行（同一事务）。

    这是「删除记录」的硬删除路径（与「从队列移除」的软取消不同）：删除后该任务
    连同它的日志永久消失，绝不会再回到队列。返回是否命中 tasks 行；任何异常都不抛。
    """
    try:
        tid = int(task_id or 0)
    except Exception:
        return False
    if tid <= 0:
        return False
    try:
        with _lock:
            conn = _connect()
            try:
                conn.execute("DELETE FROM log_index WHERE task_id = ?", (tid,))
                cur = conn.execute("DELETE FROM tasks WHERE id = ?", (tid,))
                conn.commit()
                return cur.rowcount > 0
            finally:
                conn.close()
    except Exception:
        return False


def prune_tasks(limit=500):
    """只保留最新 limit 条终态任务，返回删除行数；非终态行永不删除。

    终态 = done/failed/canceled；「最新」按 created_at DESC, id DESC 判定。
    """
    try:
        keep = int(limit) if limit is not None else 500
    except Exception:
        keep = 500
    if keep < 0:
        keep = 0
    try:
        with _lock:
            conn = _connect()
            try:
                cur = conn.execute(
                    "DELETE FROM tasks WHERE state IN ('done','failed','canceled') "
                    "AND id NOT IN (SELECT id FROM tasks WHERE state IN "
                    "('done','failed','canceled') ORDER BY created_at DESC, id DESC "
                    "LIMIT ?)", (keep,))
                conn.commit()
                return max(0, int(cur.rowcount or 0))
            finally:
                conn.close()
    except Exception:
        return 0
