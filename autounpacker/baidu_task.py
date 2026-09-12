# -*- coding: utf-8 -*-
"""实验性功能：只读读取百度网盘客户端的下载任务库，还原「一次拉取的批次 /
目录结构 / 分卷」信息。

目的：给上层 watcher 提供一份「权威的期望文件清单」，用于
- 把一个分享/一次下载的所有条目归为一批（batch）；
- 精确识别分卷组（同 base、编号连续）；
- 还原/保留原始目录结构（server_path 的层级）。

! 安全铁律（务必遵守，避免干扰用户正在运行的百度网盘客户端）：
- 只用 mode=ro 只读打开，绝不写、绝不改 journal_mode、绝不 VACUUM；
- 短连接：打开 → 查询 → 立即 close，不保持常驻连接、不持有长事务；
- busy_timeout 放在我们这边（退避），遇 BUSY 重试而非抢锁；
- 全程 try/except，任何异常都返回空结果，绝不影响主流程；
- 只碰 BaiduYunGuanjia.db（journal_mode=delete，几百 KB）；
  绝不碰运行中的 BaiduYunCacheFileV0.db（WAL，上百 MB，且需自定义 collation）。

数据库结构（本机 7.14.1 实测）：
- download_file（活动任务）：task_id, server_path, local_path, status,
  file_size, isdir, error_code, add_time, status_changetime, download_url,
  download_type, ...
- download_history_file（历史）：id, server_path, local_path, isdir, size,
  op_starttime, op_endtime, download_type, ...
"""
import os
import re
import sqlite3
import sys
import threading
import time
from pathlib import Path
from urllib.parse import quote

from .extract import is_volume_name, _volume_base, _volume_number

DB_NAME = "BaiduYunGuanjia.db"


# ---------- 定位数据库 ----------
def _registry_install_dir():
    """从协议关联读取百度网盘安装目录（只读注册表）。"""
    try:
        import winreg
        with winreg.OpenKey(winreg.HKEY_CLASSES_ROOT,
                            r"baiduyunguanjia\shell\open\command") as k:
            cmd, _ = winreg.QueryValueEx(k, "")
        m = re.match(r'\s*"([^"]+)"', cmd or "")
        if m:
            return Path(m.group(1)).parent
    except Exception:
        pass
    return None


def find_task_db(explicit=""):
    """定位 BaiduYunGuanjia.db。explicit 优先；否则从注册表安装目录的 users\\* 里找。"""
    if explicit:
        p = Path(explicit)
        if p.is_dir():
            p = p / DB_NAME
        if p.is_file():
            return p
    roots = []
    install = _registry_install_dir()
    if install:
        roots.append(install / "users")
    import os
    for base in (os.environ.get("ProgramFiles"), os.environ.get("ProgramFiles(x86)"),
                 os.environ.get("LOCALAPPDATA")):
        if base:
            roots.append(Path(base) / "Baidu" / "BaiduNetdisk" / "users")
    for root in roots:
        try:
            if root.is_dir():
                for sub in root.iterdir():
                    db = sub / DB_NAME
                    if db.is_file():
                        return db
        except OSError:
            continue
    return None


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


def read_tasks(db_path, history_limit=500):
    """读取活动任务与历史记录（只读）。返回 {"active": [...], "history": [...]}。"""
    active = _query(
        db_path,
        "SELECT task_id, server_path, local_path, status, file_size, isdir, "
        "download_type, add_time FROM download_file")
    history = _query(
        db_path,
        "SELECT id, server_path, local_path, isdir, size, op_starttime, "
        "op_endtime, download_type FROM download_history_file "
        "ORDER BY op_starttime DESC LIMIT ?", (int(history_limit),))
    return {"active": active, "history": history}


# ---------- 批次 / 分卷 还原 ----------
def _share_root(server_path):
    """取 server_path 的首段作为批次 key（分享根，如 soul-xxxx等多个文件）。"""
    p = (server_path or "").replace("\\", "/").lstrip("/")
    seg = p.split("/", 1)[0].strip()
    return seg or "(root)"


def group_batches(items):
    """按分享根把条目归为批次，保持出现顺序。返回 {root: [item, ...]}。"""
    groups = {}
    for it in items:
        groups.setdefault(_share_root(it.get("server_path")), []).append(it)
    return groups


def pair_volumes(items):
    """识别分卷组：同 base、去编号后一致，且至少 2 卷。"""
    groups = {}
    for it in items:
        name = Path(it.get("local_path") or "").name
        if is_volume_name(name):
            base = _volume_base(name) or name
            groups.setdefault(base, []).append(it)
    out = []
    for base, its in groups.items():
        if len(its) < 2:
            continue
        nums = sorted(n for n in (
            _volume_number(Path(i.get("local_path") or "").name) for i in its)
            if n is not None)
        contiguous = bool(nums) and nums == list(range(1, len(nums) + 1))
        out.append({"base": base, "count": len(its), "numbers": nums,
                    "contiguous": contiguous, "items": its})
    out.sort(key=lambda v: (-v["count"], v["base"]))
    return out


def summarize(db_path=None):
    """只读汇总：返回结构化结果。任何异常都给出 ok=False。"""
    try:
        db = Path(db_path) if db_path else find_task_db()
    except Exception:
        db = None
    if not db or not Path(db).is_file():
        return {"ok": False, "reason": "未找到 BaiduYunGuanjia.db（可手动指定路径）"}
    try:
        tasks = read_tasks(db)
    except Exception as e:
        return {"ok": False, "reason": f"读取失败: {e}"}
    allitems = list(tasks["active"]) + list(tasks["history"])
    return {
        "ok": True, "db": str(db),
        "active": len(tasks["active"]), "history": len(tasks["history"]),
        "batches": group_batches(allitems),
        "volumes": pair_volumes(allitems),
    }


def format_summary(s, max_batches=8, max_vols=10):
    """把 summarize() 结果转成多行文本（供日志/CLI 显示）。"""
    if not s or not s.get("ok"):
        return [f"百度任务库：未启用或不可用（{(s or {}).get('reason', '')}）"]
    lines = [
        f"百度任务库: {s['db']}",
        f"活动任务 {s['active']} 条，历史 {s['history']} 条；"
        f"批次 {len(s['batches'])} 个，分卷组 {len(s['volumes'])} 组",
    ]
    for v in s["volumes"][:max_vols]:
        nums = ", ".join(f"{n:03d}" if n is not None else "?" for n in v["numbers"])
        lines.append(f"  分卷组 {v['base']}（{v['count']} 卷，编号连续={v['contiguous']}）: {nums}")
    for i, (root, items) in enumerate(list(s["batches"].items())[:max_batches]):
        dirs = sum(1 for it in items if it.get("isdir"))
        lines.append(f"  批次[{i + 1}] {root}: {len(items)} 项（目录 {dirs}）")
        for it in items[:2]:
            lines.append(f"      {it.get('local_path')}")
    return lines


# ---------- 下载根目录 / 活动任务 ----------
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


def detect_download_root(db_path=None, history_limit=300):
    """从下载历史推断百度网盘下载根目录（如 E:\\test）。失败返回 None。"""
    try:
        db = Path(db_path) if db_path else find_task_db()
    except Exception:
        db = None
    if not db or not Path(db).is_file():
        return None
    rows = _query(db,
                  "SELECT local_path FROM download_history_file "
                  "ORDER BY op_starttime DESC LIMIT ?", (int(history_limit),))
    paths = [str(r.get("local_path") or "") for r in rows if r.get("local_path")]
    paths = [p for p in paths if re.match(r"^[A-Za-z]:[\\/]", p)]
    if not paths:
        return None
    common = None
    try:
        common = Path(os.path.commonpath(paths))
    except Exception:
        common = None
    # 共同根若退化成盘符根（E:\），改用最高频的父目录
    if common is None or str(common).rstrip("\\/") == common.anchor.rstrip("\\/"):
        from collections import Counter
        top = Counter(os.path.dirname(p) for p in paths).most_common(1)
        if top:
            common = Path(top[0][0])
    try:
        if common is not None and common.is_dir():
            return common
    except OSError:
        pass
    return None


def get_active_tasks(db_path=None):
    """读取当前活动下载任务（download_file）。找不到库/失败返回 []。"""
    try:
        db = Path(db_path) if db_path else find_task_db()
    except Exception:
        return []
    if not db or not Path(db).is_file():
        return []
    return _query(db,
                  "SELECT task_id, server_path, local_path, status, file_size, "
                  "isdir, download_type, add_time FROM download_file") or []


def format_active(tasks):
    if not tasks:
        return ["百度网盘：当前无活动下载任务"]
    lines = [f"百度网盘：检测到 {len(tasks)} 个活动下载任务"]
    for t in tasks[:30]:
        lines.append(f"  {t.get('local_path')}  ({t.get('file_size')} B)")
    return lines


def start_active_watcher(state, hub, interval=5):
    """实验性：后台轮询 download_file（活动任务），任务集合变化时写日志。

    只读、短连接、低频；异常静默跳过。返回线程对象或 None。"""
    try:
        if not state.snapshot().get("experimental_enabled", False):
            return None
    except Exception:
        return None

    def _worker():
        last_sig = ()
        while True:
            try:
                cfg = state.snapshot()
                if cfg.get("experimental_enabled", False):
                    tasks = _query_or_none(
                        cfg.get("baidu_task_db") or find_task_db(),
                        "SELECT task_id, local_path, file_size FROM download_file")
                    if tasks is not None:
                        sig = tuple(sorted(
                            (t.get("task_id"), t.get("local_path")) for t in tasks))
                        if sig != last_sig:
                            last_sig = sig
                            if tasks:
                                hub.log(f"[实验性] 百度网盘活动任务 {len(tasks)} 个：")
                                for t in tasks[:30]:
                                    hub.log(f"[实验性]   {t.get('local_path')} "
                                            f"({t.get('file_size')} B)")
                            else:
                                hub.log("[实验性] 百度网盘活动任务已清空")
            except Exception:
                pass
            time.sleep(max(2, int(interval)))

    th = threading.Thread(target=_worker, daemon=True)
    th.start()
    return th


def probe_and_log(state, hub):
    """启动时调用：实验性开着才在后台只读探测一次并写日志。绝不阻塞、绝不抛错。"""
    try:
        cfg = state.snapshot()
    except Exception:
        return
    if not cfg.get("experimental_enabled", False):
        return

    def _worker():
        try:
            s = summarize(cfg.get("baidu_task_db") or None)
            for line in format_summary(s):
                hub.log(f"[实验性] {line}")
        except Exception as e:
            try:
                hub.log(f"[实验性] 百度任务库探测失败: {e}")
            except Exception:
                pass

    threading.Thread(target=_worker, daemon=True).start()


if __name__ == "__main__":
    _arg = sys.argv[1] if len(sys.argv) > 1 else None
    _s = summarize(_arg)
    for _line in format_summary(_s, max_batches=50, max_vols=50):
        print(_line)
