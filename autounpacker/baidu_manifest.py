# -*- coding: utf-8 -*-
"""百度网盘任务库：批次/分卷还原 + 任务跟踪事件（纯逻辑，无 IO 线程）。

职责：
- 把一次下载的条目按「分享根」归批（batch），识别分卷组（同 base、编号连续）；
- 跟踪活动任务的生命周期：新任务(started) / 完成(done) / 失联(gone) /
  重复(dups) / 出错(failed)，并给出「批次预登记清单」与「批次是否整体完成」；
- 提供判断口径给上层：expected_files()（期望文件清单）、volume_hint()（分卷是否到齐，
  只加速不阻断）、leftover_tasks()（启动时的未完成任务）、check_duplicate()（是否下过）；
- 粘性记忆读写（存 toolbox.db）：跨重启 / 客户端清历史后仍认得「我们的文件」。

关键入口：observe_tasks() / report_events() / expected_files() / volume_hint() /
          batch_state() / leftover_tasks() / check_duplicate() / is_enabled()

依赖：标准库 + .baidu_db（只读访问）+ .extract（分卷命名判定）。

注意：本模块全是纯函数/内存状态，**不做任何 DB 轮询、不启线程**（轮询在 baidu_watch）。
所有函数都不得抛异常给调用方（失败一律降级为 None/空）。
"""
import time
from pathlib import Path

from .extract import is_volume_name, _volume_base, _volume_number
from .baidu_db import (_select, _as_text, select_task_db, get_active_tasks,
                       read_tasks, find_task_db)


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


def format_active(tasks):
    if not tasks:
        return ["百度网盘：当前无活动下载任务"]
    lines = [f"百度网盘：检测到 {len(tasks)} 个活动下载任务"]
    for t in tasks[:30]:
        lines.append(f"  {t.get('local_path')}  ({t.get('file_size')} B)")
    return lines


# ---------- 任务跟踪：批次预登记(B) / 完成检测(A) / 重复检测(D) ----------
_TRACK = {
    "files": {},          # 归一化 local_path -> 任务信息（含 state / batch）
    "active_ids": set(),  # 上一拍的 active task_id 集合（用于检测「消失=完成」）
    "boot": None,         # 本进程启动墙钟（用于「遗留任务」提示）
}


def _norm_path(p):
    return str(p or "").replace("/", "\\").rstrip("\\").lower()


def is_enabled(state=None, cfg=None):
    """实验性开关是否打开（供上层判断是否启用「百度清单」监听模式）。"""
    try:
        c = cfg if isinstance(cfg, dict) else (
            state.snapshot() if state is not None else {})
    except Exception:
        c = {}
    return bool((c or {}).get("experimental_enabled", False))


def remember_sticky(path, kind="file", note=""):
    """把「属于网盘下载」的路径记入粘性存储（跨重启 / 客户端清历史后仍认得）。

    写入失败一律静默：粘性记忆只是增强，绝不能影响主流程。
    """
    try:
        from . import db as _db
        _db.sticky_remember(str(path), kind=kind, note=note)
    except Exception:
        pass


def sticky_known(path):
    """该路径是否被粘性记忆过。任何异常返回 False。"""
    try:
        from . import db as _db
        return _db.sticky_known(str(path))
    except Exception:
        return False


def _hist_match(hist_rows, server_path, size):
    """历史里是否存在同 server_path（且同 size）的记录。"""
    sp = _as_text(server_path)
    if not sp:
        return False
    try:
        sz = int(size) if size is not None else None
    except Exception:
        sz = None
    for r in hist_rows or []:
        if _as_text(r.get("server_path")) != sp:
            continue
        if sz is None:
            return True
        try:
            if int(r.get("size")) == sz:
                return True
        except Exception:
            continue
    return False


def check_duplicate(server_path, size, db_path=None):
    """D：该文件是否「之前已下载过」（历史里同 server_path + 同 size）。

    返回命中的历史记录列表；无命中/不可读返回 None。
    """
    try:
        db = Path(db_path) if db_path else select_task_db("")[0]
    except Exception:
        db = None
    if not db or not Path(db).is_file():
        return None
    hist = _select(db, "download_history_file",
                   ("server_path", "size", "local_path", "op_endtime"),
                   order_by="op_starttime", limit=2000)
    if not hist:
        return None
    sp = _as_text(server_path)
    if not sp:
        return None
    try:
        sz = int(size) if size is not None else None
    except Exception:
        sz = None
    hits = []
    for r in hist:
        if _as_text(r.get("server_path")) != sp:
            continue
        try:
            if sz is None or int(r.get("size")) == sz:
                hits.append(r)
        except Exception:
            continue
    return hits or None


def observe_tasks(rows, hist_rows=None):
    """用一拍的 download_file（+历史）更新跟踪状态，返回本次事件。

    - started：本拍新出现的任务；
    - done  ：上一拍还在、本拍消失，且历史里能找到（=下载完成）；
    - gone  ：消失但历史里找不到（被取消/失败清除，无法确认完成）；
    - dups  ：新任务与历史中同 server_path + size 的记录重复（D）；
    - failed：error_code != 0。
    """
    events = {"started": [], "done": [], "gone": [], "dups": [], "failed": []}
    if rows is None:
        return events
    cur_ids = set()
    for r in rows:
        tid = _as_text(r.get("task_id"))
        cur_ids.add(tid)
        lp = _as_text(r.get("local_path"))
        key = _norm_path(lp)
        info = _TRACK["files"].get(key)
        if info is None:
            info = {
                "local_path": lp,
                "server_path": _as_text(r.get("server_path")),
                "size": r.get("file_size"),
                "isdir": r.get("isdir"),
                "batch": _share_root(_as_text(r.get("server_path"))),
                "task_id": tid,
                "add_time": r.get("add_time"),
                "state": "active",
            }
            _TRACK["files"][key] = info
            events["started"].append(dict(info))
            if _hist_match(hist_rows, info["server_path"], info["size"]):
                events["dups"].append(dict(info))
        else:
            # 同一路径被**重新下载**：刷新 task_id 等字段。否则本条目还挂着旧
            # task_id，新任务结束时就匹配不上，会被误判为「仍在下载中」，状态
            # 永远停在 active → _baidu_poll（只处理 done）→ 永不解压。
            reappeared = info.get("state") != "active"
            info["task_id"] = tid
            sp = _as_text(r.get("server_path"))
            if sp:
                info["server_path"] = sp
            if r.get("file_size") is not None:
                info["size"] = r.get("file_size")
            if r.get("isdir") is not None:
                info["isdir"] = r.get("isdir")
            info["state"] = "active"
            if reappeared:
                # 重新下载：当作一次新任务上报（便于日志/预登记）
                events["started"].append(dict(info))
        try:
            if int(r.get("error_code") or 0) != 0:
                events["failed"].append(dict(info))
        except Exception:
            pass
    _TRACK["active_ids"] = cur_ids
    # 按「是否仍在当前 downloading 集合」判定完成 / 失联 —— 而不是只看相邻两拍
    # 的差集。原因：同一路径被重新下载时任务 id 会变，用旧 task_id 去查差集必然
    # 落空，状态会永远停在 active（→ _baidu_poll 只处理 done，于是永不解压）。
    # 改为遍历所有 active 条目、只要其 task_id 不在当前活动集合里就判定完成/失联，
    # 与 task_id 是否刷新无关，更稳。
    for info in _TRACK["files"].values():
        if info.get("state") != "active":
            continue
        if _as_text(info.get("task_id")) in cur_ids:
            continue
        ok = _hist_match(hist_rows, info.get("server_path"), info.get("size"))
        info["state"] = "done" if ok else "gone"
        (events["done"] if ok else events["gone"]).append(dict(info))
    return events


def batch_files(batch):
    """某批次已登记的全部期望文件（含已完成），按 local_path 排序。"""
    return sorted((v for v in _TRACK["files"].values()
                   if v.get("batch") == batch),
                  key=lambda v: _norm_path(v.get("local_path")))


def batch_state(batch):
    """批次状态：(总数, 下载中, 已完成, 消失未确认)。"""
    fs = batch_files(batch)
    a = sum(1 for v in fs if v.get("state") == "active")
    d = sum(1 for v in fs if v.get("state") == "done")
    g = sum(1 for v in fs if v.get("state") == "gone")
    return len(fs), a, d, g


def expected_files(db_path=None):
    """B：批次预登记——本次运行内「已登记」的期望文件清单。

    返回 {batch: [{local_path, size, isdir, state}, ...]}。跟踪器为空时回退到
    当前活动任务（只读一次），便于独立调用。
    """
    files = list(_TRACK["files"].values())
    if not files:
        for r in get_active_tasks(db_path):
            files.append({
                "local_path": _as_text(r.get("local_path")),
                "size": r.get("file_size"),
                "isdir": r.get("isdir"),
                "batch": _share_root(_as_text(r.get("server_path"))),
                "state": "active",
            })
    out = {}
    for v in files:
        out.setdefault(v.get("batch") or "(root)", []).append(
            {"local_path": v.get("local_path"), "size": v.get("size"),
             "isdir": v.get("isdir"), "state": v.get("state")})
    for k in out:
        out[k].sort(key=lambda x: _norm_path(x.get("local_path")))
    return out


def leftover_tasks(db_path=None):
    """C：启动时读 download_file，返回未完成/进行中的任务（附 age_s 秒）。"""
    out = []
    now = time.time()
    for r in get_active_tasks(db_path):
        d = dict(r)
        try:
            add = float(r.get("add_time") or 0)
        except Exception:
            add = 0.0
        d["age_s"] = int(now - add) if add else None
        out.append(d)
    return out


def volume_hint(local_path):
    """A：供 monitors 判断「分卷是否到齐」——**只加速，绝不阻断**。

    返回 True  = 该分卷组（同批次同 base）已全部「下载完成」且文件都在磁盘上；
    返回 None  = 信息不足（未开启实验性 / 未被跟踪 / 无法判断）→ 调用方回退原启发式。
    绝不返回 False（避免因 DB 陈旧误判而卡住解压）。
    """
    try:
        info = _TRACK["files"].get(_norm_path(local_path))
        if not info:
            return None
        batch = info.get("batch")
        base = _volume_base(Path(str(local_path)).name)
        if not base:
            return None
        # 必须同一目录：跨目录的分卷无法直接交给 7-Zip（暂存到同一目录再说）。
        same_dir = _norm_path(Path(str(local_path)).parent)
        sibs = [v for v in _TRACK["files"].values()
                if v.get("batch") == batch
                and _volume_base(Path(str(v.get("local_path"))).name) == base
                and _norm_path(Path(str(v.get("local_path"))).parent) == same_dir]
        if len(sibs) < 2:
            return None
        for v in sibs:
            if v.get("state") != "done":
                return None
            try:
                if not Path(str(v.get("local_path"))).is_file():
                    return None
            except OSError:
                return None
        return True
    except Exception:
        return None


def report_events(events, hub, notify=True, max_lines=12):
    """把 observe_tasks 的事件写成日志（并按需通知）。返回已通知完成的批次列表。"""
    def _log(m):
        try:
            hub.log(f"[实验性] {m}")
        except Exception:
            pass

    for it in (events.get("started") or [])[:max_lines]:
        _log(f"网盘新任务：{it.get('local_path')}（{it.get('size')} B，"
             f"批次 {it.get('batch')}）")
    # B：新出现文件的批次，输出「预登记」清单（结构摘要，便于提前建目录/等分卷）
    for b in {it.get("batch") for it in (events.get("started") or [])}:
        fs = batch_files(b)
        ndir = sum(1 for v in fs if v.get("isdir"))
        _log(f"批次预登记 {b}：共 {len(fs)} 项（目录 {ndir}）")
        for v in fs[:8]:
            _log(f"    {v.get('local_path')}")
        if len(fs) > 8:
            _log(f"    …等共 {len(fs)} 项")
    for it in (events.get("dups") or []):
        _log(f"此前已下载过（同名同大小）：{it.get('local_path')}")
        if notify:
            try:
                hub.notify("网盘重复下载",
                           f"{Path(str(it.get('local_path'))).name} 之前已下载过")
            except Exception:
                pass
    for it in (events.get("failed") or []):
        _log(f"网盘任务出错（error_code≠0）：{it.get('local_path')}")
    for it in (events.get("gone") or []):
        _log(f"网盘任务从列表移除但历史未确认：{it.get('local_path')}")

    notified = []
    done = events.get("done") or []
    for it in done:
        _log(f"网盘任务完成：{it.get('local_path')}")
    for b in {it.get("batch") for it in done}:
        n, a, d, g = batch_state(b)
        if a == 0 and d > 0 and g == 0:
            _log(f"网盘下载批次完成：{b}（{d} 项全部完成）")
            if notify:
                try:
                    hub.notify("网盘下载完成", f"{b}：{d} 个文件已下载完成")
                except Exception:
                    pass
            notified.append(b)
    return notified
