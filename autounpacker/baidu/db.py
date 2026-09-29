# -*- coding: utf-8 -*-
"""百度网盘任务库：只读访问层（定位/选择库 + 短连接只读查询 + 列容错）。

职责：
- 定位并选择 BaiduYunGuanjia.db（显式配置优先，否则在所有候选里取 mtime 最新）；
- 提供短连接、只读（mode=ro）的查询原语，并区分「读失败(None)」与「空表([])」；
- 按 `PRAGMA table_info` 的真实列名拼 SELECT，客户端升级加列/改列也不会整条失败；
- 读取活动任务/历史，并从下载历史推断「下载根目录」。

关键入口：select_task_db() / find_task_db() / _select() / _table_columns() /
          read_tasks() / get_active_tasks() / detect_download_root()

依赖：仅标准库（os / re / sqlite3 / pathlib / urllib）。

注意（安全铁律，务必遵守）：
- 只碰 BaiduYunGuanjia.db（journal_mode=delete，几百 KB）；
  **绝不碰运行中的 BaiduYunCacheFileV0.db**（WAL，上百 MB，且需自定义 collation）；
- 只用 mode=ro 打开、短连接（打开→查→立即 close）、busy_timeout 放在本端、
  绝不写库 / 改 journal_mode / VACUUM / 持有长事务；
- 全程 try/except，任何异常都返回空结果，绝不影响主流程。
"""
import os
import re
import sqlite3
from pathlib import Path
from urllib.parse import quote

DB_NAME = "BaiduYunGuanjia.db"


# ---------- 定位数据库 ----------
def _registry_client_exe():
    """从协议关联读取客户端可执行文件完整路径（只读注册表）；失败返回 None。"""
    try:
        import winreg
        with winreg.OpenKey(winreg.HKEY_CLASSES_ROOT,
                            r"baiduyunguanjia\shell\open\command") as k:
            cmd, _ = winreg.QueryValueEx(k, "")
        m = re.match(r'\s*"([^"]+)"', cmd or "")
        if m:
            return m.group(1)
    except Exception:
        pass
    return None


def _registry_install_dir():
    """从协议关联读取百度网盘安装目录（只读注册表）。"""
    exe = _registry_client_exe()
    if exe:
        try:
            return Path(exe).parent
        except Exception:
            pass
    return None


def _install_dirs():
    """列出百度网盘可能的安装目录（协议关联注册表 + 常见安装位置，去重保序）。

    顺序与历史 `_candidate_dbs` 的拼装口径一致：协议关联安装目录优先；其后是
    ProgramFiles / ProgramFiles(x86) / LOCALAPPDATA / APPDATA 下的
    Baidu\\BaiduNetdisk。实测（Win11 + 百度网盘 8.8.8.101）：新版客户端会装到
    %APPDATA%\\baidu\\BaiduNetdisk（Roaming），并不在 Program Files 下；平时靠
    协议关联的注册表兜住，但注册表缺失时这里必须也能命中。
    """
    out = []
    seen = set()

    def _add(p):
        try:
            if not p:
                return
            key = str(p).lower()
            if key not in seen:
                seen.add(key)
                out.append(Path(p))
        except Exception:
            pass

    _add(_registry_install_dir())
    for base in (os.environ.get("ProgramFiles"), os.environ.get("ProgramFiles(x86)"),
                 os.environ.get("LOCALAPPDATA"), os.environ.get("APPDATA")):
        if base:
            _add(Path(base) / "Baidu" / "BaiduNetdisk")
    return out


def _candidate_dbs():
    """列出所有候选 BaiduYunGuanjia.db（注册表安装目录 + 常见安装位置，去重）。"""
    roots = [install / "users" for install in _install_dirs()]
    out = []
    seen = set()
    for root in roots:
        try:
            if root.is_dir():
                for sub in sorted(root.iterdir()):
                    db = sub / DB_NAME
                    if db.is_file():
                        key = str(db).lower()
                        if key not in seen:
                            seen.add(key)
                            out.append(db)
        except OSError:
            continue
    return out


# ---------- 客户端 exe / 版本探测（只读、离线、绝不抛） ----------
# 百度网页端 `function-widget-1/pkg/download-all` 里的
# `GUANJIA_VERSION_COMPARE: "4.8.0"`：低于该版本的客户端可能无法正确处理新版
# 「唤起下载」深链。**仅用于软告警**（见 baidu/share.py 准备阶段的一条 warning），
# 绝不据此跳过 / 延迟 / 阻断任何唤起或启动。
MIN_WAKE_VERSION = (4, 8, 0)

# 版本探测成功后的模块级缓存（探测本身廉价，但唤起链路是热路径；只缓存成功
# 结果，探测不到时保持 None、允许下次重试）。小写命名：这是可变模块状态，
# 不是常量。
_client_version_cache = None

# 自动探测时在安装目录下预期的客户端主程序名（协议关联的注册表值实测可能指向
# YunDetectService.exe 等辅助进程，安装目录候选统一找主程序 BaiduNetdisk.exe）。
_CLIENT_EXE_NAME = "BaiduNetdisk.exe"


def parse_version(s):
    """把 '8.8.8.101' / '8.8' 这类点分版本解析成整型元组；解析不出返回 None。

    纯函数、绝不抛异常。口径从严：按 `.` 分段后**每段都必须是纯数字**
    （逐段 strip），任一段不是数字即整体判失败（不把 '8.8.8.101 beta' 之类
    的文本误当版本号）。
    """
    try:
        text = str(s or "").strip()
        if not text:
            return None
        nums = []
        for part in text.split("."):
            p = part.strip()
            if not p.isdigit():
                return None
            nums.append(int(p))
        return tuple(nums) if nums else None
    except Exception:
        return None


def version_below(a, b):
    """版本元组 a 是否**低于** b（长度不同时短的一方按 0 补齐）。绝不抛异常。

    例：version_below((4, 7, 9), (4, 8, 0)) is True；任一侧不可比较返回 False。
    """
    try:
        n = max(len(a), len(b))
        pa = tuple(a) + (0,) * (n - len(a))
        pb = tuple(b) + (0,) * (n - len(b))
        return bool(pa < pb)
    except Exception:
        return False


def _registry_display_version():
    """从卸载项注册表读客户端 DisplayVersion（HKLM/HKCU + WOW6432Node）；失败 None。"""
    try:
        import winreg
        paths = (
            (winreg.HKEY_LOCAL_MACHINE,
             r"SOFTWARE\Microsoft\Windows\CurrentVersion\Uninstall\BaiduNetdisk"),
            (winreg.HKEY_LOCAL_MACHINE,
             r"SOFTWARE\WOW6432Node\Microsoft\Windows\CurrentVersion\Uninstall"
             r"\BaiduNetdisk"),
            (winreg.HKEY_CURRENT_USER,
             r"SOFTWARE\Microsoft\Windows\CurrentVersion\Uninstall\BaiduNetdisk"),
        )
        for hive, sub in paths:
            try:
                with winreg.OpenKey(hive, sub) as k:
                    v, _ = winreg.QueryValueEx(k, "DisplayVersion")
                s = str(v or "").strip()
                if s:
                    return s
            except Exception:
                continue
    except Exception:
        pass
    return None


def _file_version(path):
    """读 exe 的**文件版本资源**（如 "8.8.8.101"）；失败返回 None。

    只用标准库 ctypes 调 version.dll 的 GetFileVersionInfoSizeW /
    GetFileVersionInfoW / VerQueryValueW + VS_FIXEDFILEINFO。注意
    VerQueryValueW 的 `\\` 子块路径必须传**可写缓冲**
    （`ctypes.create_unicode_buffer("\\")`），普通 str 会被 ctypes 拒绝。
    """
    try:
        import ctypes
        from ctypes import wintypes

        class VS_FIXEDFILEINFO(ctypes.Structure):
            _fields_ = [
                ("dwSignature", wintypes.DWORD),
                ("dwStrucVersion", wintypes.DWORD),
                ("dwFileVersionMS", wintypes.DWORD),
                ("dwFileVersionLS", wintypes.DWORD),
                ("dwProductVersionMS", wintypes.DWORD),
                ("dwProductVersionLS", wintypes.DWORD),
                ("dwFileFlagsMask", wintypes.DWORD),
                ("dwFileFlags", wintypes.DWORD),
                ("dwFileOS", wintypes.DWORD),
                ("dwFileType", wintypes.DWORD),
                ("dwFileSubtype", wintypes.DWORD),
                ("dwFileDateMS", wintypes.DWORD),
                ("dwFileDateLS", wintypes.DWORD),
            ]

        p = str(path or "").strip()
        if not p or not os.path.isfile(p):
            return None
        vd = ctypes.WinDLL("version", use_last_error=True)
        vd.GetFileVersionInfoSizeW.argtypes = [wintypes.LPCWSTR,
                                               ctypes.POINTER(wintypes.DWORD)]
        vd.GetFileVersionInfoSizeW.restype = wintypes.DWORD
        vd.GetFileVersionInfoW.argtypes = [wintypes.LPCWSTR, wintypes.DWORD,
                                           wintypes.DWORD, ctypes.c_void_p]
        vd.GetFileVersionInfoW.restype = wintypes.BOOL
        vd.VerQueryValueW.argtypes = [ctypes.c_void_p, wintypes.LPCWSTR,
                                      ctypes.POINTER(ctypes.c_void_p),
                                      ctypes.POINTER(wintypes.UINT)]
        vd.VerQueryValueW.restype = wintypes.BOOL

        size = vd.GetFileVersionInfoSizeW(p, None)
        if not size:
            return None
        buf = ctypes.create_string_buffer(int(size))
        if not vd.GetFileVersionInfoW(p, 0, int(size), buf):
            return None
        ptr = ctypes.c_void_p()
        length = wintypes.UINT()
        sub = ctypes.create_unicode_buffer("\\")
        if not vd.VerQueryValueW(ctypes.cast(buf, ctypes.c_void_p), sub,
                                 ctypes.byref(ptr), ctypes.byref(length)):
            return None
        if not ptr or length.value < ctypes.sizeof(VS_FIXEDFILEINFO):
            return None
        info = ctypes.cast(ptr, ctypes.POINTER(VS_FIXEDFILEINFO)).contents
        ms, ls = int(info.dwFileVersionMS), int(info.dwFileVersionLS)
        return ".".join(str(v) for v in (
            (ms >> 16) & 0xFFFF, ms & 0xFFFF,
            (ls >> 16) & 0xFFFF, ls & 0xFFFF))
    except Exception:
        return None


def _client_exe_candidates():
    """自动探测时的客户端 exe 候选路径（协议关联优先，其次常见安装目录）。"""
    out = []
    seen = set()

    def _add(p):
        try:
            if p:
                key = str(p).lower()
                if key not in seen:
                    seen.add(key)
                    out.append(str(p))
        except Exception:
            pass

    _add(_registry_client_exe())
    for d in _install_dirs():
        _add(d / _CLIENT_EXE_NAME)
    return out


def client_version(exe_path=None):
    """探测百度网盘客户端版本号（如 "8.8.8.101"）；探测不到返回 None。

    只读、离线、极快、**绝不阻塞 / 不重试 / 不开 socket**，任何失败一律返回
    None（绝不对调用方抛异常）。策略：
    - `exe_path` 显式给定：只读该 exe 的文件版本资源；
    - `exe_path` 为 None：自动探测——协议关联 exe → 常见安装目录下的
      BaiduNetdisk.exe → 卸载项注册表 DisplayVersion（HKLM/HKCU + WOW6432Node）。
    成功结果缓存到 `_client_version_cache`（唤起链路是热路径）；失败不缓存。
    """
    global _client_version_cache
    if _client_version_cache is not None:
        return _client_version_cache
    try:
        if exe_path is None:
            for cand in _client_exe_candidates():
                v = _file_version(cand)
                if v:
                    _client_version_cache = v
                    return v
            v = _registry_display_version()
            if v:
                _client_version_cache = v
                return v
            return None
        v = _file_version(exe_path)
        if v:
            _client_version_cache = v
            return v
    except Exception:
        pass
    return None


def select_task_db(explicit=""):
    """选择要读取的任务库，返回 (Path | None, 选择原因文本)。

    策略：显式配置优先（便于手动指定/排查）；否则在所有候选里取 mtime 最新者
    （换账号、旧残留时也能选到「正在用」的那个）。原因文本用于日志与诊断，
    避免以后排查时不知道到底读了哪个库、为什么。
    """
    if explicit:
        p = Path(explicit)
        if p.is_dir():
            p = p / DB_NAME
        if p.is_file():
            return p, f"配置指定：{p}"
        return None, f"配置指定的路径不可用：{explicit}"
    cands = _candidate_dbs()
    if not cands:
        return None, "自动探测：未发现 users/*/BaiduYunGuanjia.db"

    def _mtime(x):
        try:
            return x.stat().st_mtime
        except OSError:
            return 0.0

    best = max(cands, key=_mtime)
    try:
        ident = best.parent.name
    except Exception:
        ident = "?"
    return best, f"自动探测：users/{ident}（{len(cands)} 个候选中 mtime 最新）"


def find_task_db(explicit=""):
    """定位 BaiduYunGuanjia.db（兼容旧接口：只返回路径）。"""
    return select_task_db(explicit)[0]


# ---------- 谨慎只读访问 ----------
def _ro_uri(path):
    """构造只读 URI（百分号编码，兼容中文/特殊字符路径）。"""
    return "file:" + quote(Path(path).as_posix(), safe="/:") + "?mode=ro"


def _query(db_path, sql, params=()):
    """短连接只读查询：打开→查→立刻关闭；busy_timeout 放在本端。失败返回 []。"""
    con = None
    try:
        con = sqlite3.connect(_ro_uri(db_path), uri=True, timeout=1.5)
        con.execute("PRAGMA busy_timeout=1500")
        cur = con.execute(sql, params)
        cols = [d[0] for d in cur.description]
        return [dict(zip(cols, row)) for row in cur.fetchall()]
    except Exception:
        return []
    finally:
        if con is not None:
            try:
                con.close()
            except Exception:
                pass


def _query_or_none(db_path, sql, params=()):
    """同 _query，但出错返回 None（供轮询区分「空」与「读失败」）。"""
    con = None
    try:
        con = sqlite3.connect(_ro_uri(db_path), uri=True, timeout=1.5)
        con.execute("PRAGMA busy_timeout=1500")
        cur = con.execute(sql, params)
        cols = [d[0] for d in cur.description]
        return [dict(zip(cols, row)) for row in cur.fetchall()]
    except Exception:
        return None
    finally:
        if con is not None:
            try:
                con.close()
            except Exception:
                pass


def _as_text(v):
    """把 DB 里可能非 UTF-8 的文本字段安全转成 str（绝不因编码崩）。"""
    if v is None:
        return ""
    if isinstance(v, bytes):
        for enc in ("utf-8", "mbcs"):
            try:
                return v.decode(enc)
            except Exception:
                continue
        return v.decode("utf-8", "replace")
    return v if isinstance(v, str) else str(v)


# 列名缓存：{(db路径小写, 表名, mtime_ns): set(列名)}，避免每个 tick 都查 PRAGMA
_COL_CACHE = {}
_COL_TABLES = ("download_file", "download_history_file")


def _table_columns(db_path, table):
    """只读获取表的列名集合（带缓存）。失败返回空 set()。

    客户端升级可能加列/改列，写死 SELECT 会整条查询失败；先取真实列名再拼查询。
    """
    if table not in _COL_TABLES:      # 表名只来自本模块常量，仍做白名单校验
        return set()
    try:
        db = Path(db_path)
        key = (str(db).lower(), table, db.stat().st_mtime_ns)
    except Exception:
        key = None
    if key is not None and key in _COL_CACHE:
        return _COL_CACHE[key]
    con = None
    cols = set()
    try:
        con = sqlite3.connect(_ro_uri(db_path), uri=True, timeout=1.5)
        con.execute("PRAGMA busy_timeout=1500")
        cur = con.execute(f"PRAGMA table_info({table})")
        cols = {str(r[1]) for r in cur.fetchall()}
    except Exception:
        cols = set()
    finally:
        if con is not None:
            try:
                con.close()
            except Exception:
                pass
    if key is not None and cols:
        _COL_CACHE[key] = cols
    return cols


def _select(db_path, table, wanted, order_by=None, limit=None):
    """按「实际存在的列」拼 SELECT 并只读查询。

    返回 list[dict]（缺列时该键不存在，调用方用 .get()）；读失败返回 None
    （区别于「表为空」的空列表）。任何情况下都不抛异常。
    """
    cols = _table_columns(db_path, table)
    if not cols:
        return None
    use = [c for c in wanted if c in cols]
    if not use:
        return None
    sql = "SELECT " + ", ".join(use) + f" FROM {table}"
    params = ()
    if order_by and order_by in cols:
        sql += f" ORDER BY {order_by} DESC"
    if limit:
        sql += " LIMIT ?"
        params = (int(limit),)
    return _query_or_none(db_path, sql, params)


def read_tasks(db_path, history_limit=500):
    """读取活动任务与历史记录（只读）。返回 {"active": [...], "history": [...]}。

    列存在性容错：缺列时该键不出现，消费方一律用 .get()。读失败给出空列表
    （保持旧行为；需要区分失败请用 _select / diagnose）。
    """
    # download_url / param2 只在**活动表**存在（历史表没有），且只在下载进行中有值；
    # 分享下载时它们带 shareid / share_uk / fs_id / md5 / sekey，是「链接↔下载」关联键。
    active = _select(db_path, "download_file",
                     ("task_id", "server_path", "local_path", "status", "file_size",
                      "isdir", "download_type", "add_time", "error_code",
                      "status_changetime", "download_url", "param2"))
    history = _select(db_path, "download_history_file",
                      ("id", "server_path", "local_path", "isdir", "size",
                       "op_starttime", "op_endtime", "download_type"),
                      order_by="op_starttime", limit=int(history_limit))
    return {"active": active or [], "history": history or []}


def get_active_tasks(db_path=None):
    """读取当前活动下载任务（download_file）。找不到库/失败返回 []。"""
    try:
        db = Path(db_path) if db_path else select_task_db("")[0]
    except Exception:
        return []
    if not db or not Path(db).is_file():
        return []
    return _select(db, "download_file",
                   ("task_id", "server_path", "local_path", "status", "file_size",
                    "isdir", "download_type", "add_time", "error_code",
                    "status_changetime", "download_url", "param2")) or []


def detect_download_root(db_path=None, history_limit=300):
    """从下载历史推断百度网盘下载根目录（如 D:\\下载）。无法推断返回 None。

    注意：**不做 os.path.exists / is_dir 判死**——目标盘（如移动硬盘）此刻可能
    未插入，仍应返回推断出的路径，交给调用方决定是否加为监听路径。
    """
    try:
        db = Path(db_path) if db_path else select_task_db("")[0]
    except Exception:
        db = None
    if not db or not Path(db).is_file():
        return None
    rows = _select(db, "download_history_file", ("local_path",),
                   order_by="op_starttime", limit=int(history_limit))
    if not rows:
        return None
    paths = [_as_text(r.get("local_path")) for r in rows]
    paths = [p for p in paths if re.match(r"^[A-Za-z]:[\\/]", p)]
    if not paths:
        return None
    # 用「文件所在目录」求公共前缀：commonpath 对单条记录会直接返回该文件本身，
    # 于是「下载根」被推成一个文件路径（加监听/提示都会出错）。改用父目录，
    # 保证返回的一定是目录。
    dirs = [os.path.dirname(p) for p in paths]
    dirs = [d for d in dirs if d]
    if not dirs:
        return None
    common = None
    try:
        common = Path(os.path.commonpath(dirs))
    except Exception:
        common = None
    # 共同根若退化成盘符根（D:\），改用最高频的父目录
    if common is None or str(common).rstrip("\\/") == common.anchor.rstrip("\\/"):
        from collections import Counter
        top = Counter(dirs).most_common(1)
        if top:
            common = Path(top[0][0])
    return common
