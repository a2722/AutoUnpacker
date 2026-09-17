# -*- coding: utf-8 -*-
"""百度网盘任务库：后台轮询线程 + 一次性诊断 + 启动探测。

职责：
- start_active_watcher()：后台低频只读轮询 download_file，变化时写日志/通知；
- diagnose()：一次性只读诊断（路径/来源/两表行数/活动任务/错误/降级状态），
  供设置里的「立即读取网盘任务库」按钮使用；
- probe_and_log()：启动时探测一次（选中哪个库、批次汇总、未完成任务提示）；
- _baidu_running()：进程守卫（ctypes 读进程快照，判断网盘客户端是否在运行）。

关键入口：start_active_watcher() / diagnose() / probe_and_log() / _baidu_running()

依赖：标准库 + .baidu_db（只读访问）+ .baidu_manifest（跟踪/汇总）。

「别干扰」设计（三件套）：
- 进程守卫：百度网盘没运行就完全不读；
- 自适应间隔：有活动任务 3s，空闲 12s；
- 失败退避：连续读失败达阈值后指数降频（最高 60s），降级/恢复各只记一条日志。
另：单实例入口，**只在 app.py 启动时调用一次**（本模块自身不在 import 时启线程）。
"""
import sys
import threading
import time
from pathlib import Path

from .baidu_db import (_select, _as_text, select_task_db, detect_download_root)
from .baidu_manifest import (observe_tasks, report_events, leftover_tasks,
                             summarize, format_summary, format_summary_brief,
                             _TRACK)

# 轮询状态（模块级，供 diagnose() 展示；只读、无副作用）
_STATE = {
    "degraded": False,   # 是否处于「连续失败降级」中
    "interval": 12.0,    # 当前有效轮询间隔（秒）
    "error": None,       # 最近一次读失败原因
    "fail_count": 0,     # 连续失败次数
    "db_path": None,     # 最近解析到的任务库路径
    "db_source": "",     # 该路径的选择原因
}
_BACKOFF_AT = 5          # 连续失败达到该次数后开始指数退避
_INTERVAL_IDLE = 12.0    # 空闲（无活动任务）间隔
_INTERVAL_ACTIVE = 3.0   # 有活动任务时的间隔
_INTERVAL_MAX = 60.0     # 退避上限（秒）

# 「下载目录未加入监听」提示：按目录只提示一次
_HINTED_UNWATCHED = set()


def _norm_path(p):
    return str(p or "").replace("/", "\\").rstrip("\\").lower()


def _hint_if_download_dir_unwatched(cfg, db, log):
    """实验性已开、但识别出的百度下载目录没被任何「已启用」的监听路径覆盖时，
    多嘴提示一次：该目录**仅监控、不解压**。

    避免用户误以为「开了实验性 = 来料会被自动处理」。只提示，不改任何配置
    （监听范围始终是显式白名单）。任何失败静默。
    """
    try:
        if not db:
            return
        root = detect_download_root(db)
        if not root:
            return
        key = _norm_path(root)
        if key in _HINTED_UNWATCHED:
            return
        enabled = [_norm_path(w.get("path"))
                   for w in (cfg.get("watch_paths") or [])
                   if isinstance(w, dict) and w.get("enabled") and w.get("path")]
        covered = any(key == e or key.startswith(e + "\\") for e in enabled)
        if not covered:
            _HINTED_UNWATCHED.add(key)
            log(f"下载目录 {root} 未加入监听：仅监控、不解压")
            log("  如需自动解压，请在主界面点「网盘下载目录」，或启用对应的监听路径")
    except Exception:
        pass


def _baidu_running():
    """百度网盘客户端进程是否在运行（只读进程快照，无子进程、无第三方依赖）。

    用 ctypes 调 kernel32 的 Toolhelp32 快照直接拿进程名，比 tasklist 子进程轻得多。
    任何异常都返回 True（保守放行）——不能因为「检测失败」就停掉读取。
    """
    try:
        import ctypes
        from ctypes import wintypes

        TH32CS_SNAPPROCESS = 0x00000002
        INVALID_HANDLE_VALUE = ctypes.c_void_p(-1).value

        class PROCESSENTRY32(ctypes.Structure):
            _fields_ = [
                ("dwSize", wintypes.DWORD),
                ("cntUsage", wintypes.DWORD),
                ("th32ProcessID", wintypes.DWORD),
                ("th32DefaultHeapID", ctypes.POINTER(ctypes.c_ulong)),
                ("th32ModuleID", wintypes.DWORD),
                ("cntThreads", wintypes.DWORD),
                ("th32ParentProcessID", wintypes.DWORD),
                ("pcPriClassBase", ctypes.c_long),
                ("dwFlags", wintypes.DWORD),
                ("szExeFile", ctypes.c_char * 260),
            ]

        k32 = ctypes.windll.kernel32
        k32.CreateToolhelp32Snapshot.restype = ctypes.c_void_p
        k32.CreateToolhelp32Snapshot.argtypes = [wintypes.DWORD, wintypes.DWORD]
        k32.Process32First.argtypes = [ctypes.c_void_p,
                                       ctypes.POINTER(PROCESSENTRY32)]
        k32.Process32Next.argtypes = [ctypes.c_void_p,
                                      ctypes.POINTER(PROCESSENTRY32)]
        k32.CloseHandle.argtypes = [ctypes.c_void_p]

        snap = k32.CreateToolhelp32Snapshot(TH32CS_SNAPPROCESS, 0)
        if not snap or snap == INVALID_HANDLE_VALUE:
            return True
        try:
            pe = PROCESSENTRY32()
            pe.dwSize = ctypes.sizeof(PROCESSENTRY32)
            if not k32.Process32First(snap, ctypes.byref(pe)):
                return True
            while True:
                name = pe.szExeFile.decode("mbcs", "replace").lower()
                if name == "baidunetdisk.exe":
                    return True
                if not k32.Process32Next(snap, ctypes.byref(pe)):
                    return False
        finally:
            k32.CloseHandle(snap)
    except Exception:
        return True


# 「任务库选中」只打一次：启动期探测线程与轮询线程共用最近一次已打印的库路径。
# 用锁包住 check-and-set，避免两线程几乎同时完成选库时各打一行。
_DB_LOG_LOCK = threading.Lock()


def _should_log_db(db):
    """探测线程与轮询线程共用：同一库「任务库选中」只打印一次，避免启动期重复。

    读 _STATE["db_logged"]（最近一次已打印的库路径，字符串化比较）：
    与传入 db 不同 → 记录当前 db 并返回 True（库变化时仍各打一次）；相同 → False。
    绝不抛异常——任何异常一律返回 True（宁多打一行，不可漏打）。
    """
    try:
        key = str(db)
        with _DB_LOG_LOCK:
            if _STATE.get("db_logged") != key:
                _STATE["db_logged"] = key
                return True
            return False
    except Exception:
        return True


def start_active_watcher(state, hub, idle_interval=_INTERVAL_IDLE,
                         active_interval=_INTERVAL_ACTIVE):
    """实验性：后台轮询 download_file（活动任务），任务集合变化时写日志。

    只读、短连接；三重「别干扰」设计：
    - 进程守卫：百度网盘没运行就完全不读（真正零开销）；
    - 自适应间隔：有活动任务 3s，空闲 12s；
    - 失败退避：连续读失败达阈值后指数降频（最高 60s），降级/恢复各只记一条日志。
    整个 tick 包在 try/except 里，单次异常不会打死线程。返回线程对象或 None。
    """
    try:
        if not state.snapshot().get("experimental_enabled", False):
            return None
    except Exception:
        return None

    idle = max(1.0, float(idle_interval))
    active = max(1.0, float(active_interval))

    def _log(msg):
        try:
            hub.log(f"[实验性] {msg}")
        except Exception:
            pass

    def _worker():
        last_sig = ()
        last_db = None
        was_running = True
        while True:
            sleep_s = idle
            try:
                cfg = state.snapshot()
                if not cfg.get("experimental_enabled", False):
                    _STATE["interval"] = idle
                    time.sleep(idle)
                    continue

                # 进程守卫：网盘没开就不读
                if not _baidu_running():
                    if was_running:
                        was_running = False
                        _log("百度网盘未运行，暂停任务库轮询")
                    _STATE["interval"] = idle
                    time.sleep(idle)
                    continue
                if not was_running:
                    was_running = True
                    _log("百度网盘已运行，恢复任务库轮询")

                db, reason = select_task_db(cfg.get("baidu_task_db") or "")
                _STATE["db_path"] = str(db) if db else None
                _STATE["db_source"] = reason
                if db and str(db) != (last_db or ""):
                    last_db = str(db)
                    # 启动期探测线程可能已打过同一个库 → 这里只做去重打印；
                    # hint 维持原语义（自身按目录去重），不随后者一起吞掉。
                    if _should_log_db(db):
                        _log(f"任务库选中：{db}（{reason}）")
                    _hint_if_download_dir_unwatched(cfg, db, _log)

                rows = None
                hist = []
                if db:
                    rows = _select(db, "download_file",
                                   ("task_id", "server_path", "local_path",
                                    "file_size", "isdir", "error_code",
                                    "download_url", "param2"))
                    if rows is not None:
                        hist = _select(db, "download_history_file",
                                       ("server_path", "size"), limit=1000) or []

                if rows is None:
                    if not db:
                        # 没找到库：空闲，不算失败
                        _STATE["fail_count"] = 0
                        _STATE["degraded"] = False
                        _STATE["error"] = None
                        _STATE["interval"] = idle
                        sleep_s = idle
                    else:
                        _STATE["fail_count"] += 1
                        _STATE["error"] = "读取 download_file 失败（可能被客户端独占）"
                        if _STATE["fail_count"] >= _BACKOFF_AT:
                            extra = _STATE["fail_count"] - _BACKOFF_AT
                            iv = min(_INTERVAL_MAX, idle * (2 ** min(extra, 4)))
                            _STATE["interval"] = iv
                            if not _STATE["degraded"]:
                                _STATE["degraded"] = True
                                _log(f"百度网盘任务库持续不可读，已降频到 {iv:g}s")
                            sleep_s = iv
                        else:
                            _STATE["interval"] = idle
                            sleep_s = idle
                else:
                    if _STATE["degraded"]:
                        _log("百度网盘任务库已恢复")
                    _STATE["degraded"] = False
                    _STATE["fail_count"] = 0
                    _STATE["error"] = None
                    sig = tuple(sorted(
                        (_as_text(t.get("task_id")), _as_text(t.get("local_path")))
                        for t in rows))
                    if sig != last_sig:
                        last_sig = sig
                        _log(f"网盘活动任务 {len(rows)} 个")
                    # A/B/D：任务跟踪事件（完成检测 / 批次预登记 / 重复检测）
                    try:
                        report_events(observe_tasks(rows, hist), hub)
                    except Exception:
                        pass
                    _STATE["interval"] = active if rows else idle
                    sleep_s = _STATE["interval"]
            except Exception:
                pass
            try:
                time.sleep(max(1.0, sleep_s))
            except Exception:
                time.sleep(1.0)

    th = threading.Thread(target=_worker, daemon=True)
    th.start()
    return th


def diagnose(db_path=None):
    """一次性只读诊断：返回库路径/来源/两表行数/活动任务/最近错误/降级状态。

    供设置里的「立即读取网盘任务库」按钮使用；不受实验性开关限制（手动、显式）。
    绝不抛异常，任何失败都以字段形式返回，便于一键自查。
    返回键：db_path, db_source, found, download_file_rows, history_rows,
            active, error, degraded, interval
    """
    fallback = {
        "db_path": None, "db_source": "", "found": False,
        "download_file_rows": None, "history_rows": None, "active": [],
        "error": None, "degraded": bool(_STATE.get("degraded")),
        "interval": float(_STATE.get("interval") or _INTERVAL_IDLE),
    }
    try:
        db, reason = select_task_db(db_path or "")
        info = dict(fallback)
        info["db_source"] = reason
        info["db_path"] = str(db) if db else None
        info["found"] = bool(db and Path(db).is_file())
        if not db:
            return info
        active = _select(db, "download_file",
                         ("task_id", "server_path", "local_path", "status",
                          "file_size", "isdir", "download_type", "add_time"))
        history = _select(db, "download_history_file", ("id",),
                          order_by="op_starttime")
        if active is None:
            info["download_file_rows"] = None
            info["error"] = _STATE.get("error") or "读取 download_file 失败"
        else:
            info["download_file_rows"] = len(active)
            info["active"] = active
        info["history_rows"] = None if history is None else len(history)
        return info
    except Exception as e:
        out = dict(fallback)
        out["db_source"] = f"诊断异常：{e}"
        out["error"] = str(e)
        return out


def probe_and_log(state, hub):
    """启动时调用：实验性开着才在后台只读探测一次并写日志。绝不阻塞、绝不抛错。"""
    try:
        cfg = state.snapshot()
    except Exception:
        return
    if not cfg.get("experimental_enabled", False):
        return

    def _worker():
        # 明确记录「选中了哪个库、为什么」，便于事后排查多用户目录问题
        try:
            db, reason = select_task_db(cfg.get("baidu_task_db") or "")
            _STATE["db_path"] = str(db) if db else None
            _STATE["db_source"] = reason
            try:
                if _should_log_db(db):
                    hub.log(f"[实验性] 任务库选中：{db if db else '未找到'}（{reason}）")
            except Exception:
                pass
        except Exception:
            db = None
        # C：启动时提示「上次未完成的网盘任务」。
        # （若客户端重启会清空 download_file，则这里判断为空、不会误报。）
        _TRACK["boot"] = time.time()
        try:
            left = leftover_tasks(str(db) if db else None)
            if left:
                hub.log(f"[实验性] 检测到 {len(left)} 个未完成的网盘任务：")
                for t in left[:10]:
                    hub.log(f"[实验性]   {_as_text(t.get('local_path'))} "
                            f"({t.get('file_size')} B)")
                try:
                    hub.notify("网盘任务未完成",
                               f"有 {len(left)} 个网盘任务未完成（或仍在下载）")
                except Exception:
                    pass
        except Exception:
            pass
        try:
            s = summarize(str(db) if db else None)
            # 启动只留 1 行摘要；批次/分卷/条目明细见手动「立即读取网盘任务库」
            for line in format_summary_brief(s):
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
