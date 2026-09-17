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
import json
import re
import time
from pathlib import Path
from urllib.parse import urlsplit, parse_qs

from .extract import (is_volume_name, _volume_base, _volume_number,
                      is_volume_file, is_first_volume, is_incomplete_download)
from .baidu_db import (_select, _as_text, select_task_db, get_active_tasks,
                       read_tasks, find_task_db)


# ---------- 批次 / 分卷 还原 ----------
def _share_root(server_path):
    """取 server_path 的首段作为批次 key（分享根，如 soul-xxxx等多个文件）。"""
    p = (server_path or "").replace("\\", "/").lstrip("/")
    seg = p.split("/", 1)[0].strip()
    return seg or "(root)"


# 百度客户端给「分享下载」用的通用根目录名：**不同分享者、不同分享**都会落在这个
# 名字下面（真实样本见本次修复报告：/我的资源/<文件> 与 /我的资源/<分享目录>/...）。
# 因此它**不能**作为批次身份，否则不相干的分享会被并进同一「批次」。仅用于
# 「显示名」推导时跳过。
_GENERIC_SHARE_ROOTS = {"我的资源"}


def _share_key(share):
    """分享下载的稳定批次键（纯 ASCII、不含中文），优先 `<shareid>.<share_uk>`。

    没有分享信息（网页直链/普通下载）返回 None，由调用方退回 `_share_root`。
    绝不抛异常。
    """
    try:
        sid = str((share or {}).get("shareid") or "").strip()
        uk = str((share or {}).get("share_uk") or "").strip()
        if sid and uk:
            return f"{sid}.{uk}"
        return sid or uk or None
    except Exception:
        return None


def _share_display_name(server_path, share=None):
    """批次的人类可读显示名（日志/通知用）。绝不抛异常。

    规则（依据真实 `server_path` 样本推导，样本原文见修复报告）：
    - 根名**不是**通用根时直接用它（如 `某分享/1.7z.001` → `某分享`）；
    - 根名是通用根（如「我的资源」）时，跳过它再看：若其下还有「分享层目录」
      （至少两段），取紧邻的那一段。真实样本
      `我的资源/<分享目录>/<文件>` -> `<分享目录>`；
    - 分享层拿不到（文件直接落在通用根下，如 `我的资源/xxx.mp4`）时退化为
      `分享 <shareid>`；连 shareid 也没有才退回根名。
    """
    try:
        p = str(server_path or "").replace("\\", "/").strip("/")
        segs = [s for s in p.split("/") if s]
        root = segs[0] if segs else ""
        if root and root not in _GENERIC_SHARE_ROOTS:
            return root
        rest = segs[1:] if root in _GENERIC_SHARE_ROOTS else segs
        if len(rest) >= 2:
            return rest[0]
        sid = str((share or {}).get("shareid") or "").strip()
        if sid:
            return f"分享 {sid}"
        return root or "(root)"
    except Exception:
        return "(root)"


def _batch_identity(server_path, share=None):
    """返回 (batch_key, display_name)。

    分享下载按**分享**划分（键稳定且不含中文）；没有分享信息时才退回
    `_share_root(server_path)`。所有 batch 消费点必须统一用本函数产出的 key。
    """
    key = _share_key(share)
    if key:
        return key, _share_display_name(server_path, share)
    root = _share_root(server_path)
    return root, root


def _batch_label(info):
    """事件的显示名（缺 batch_name 时退回 batch 键，再退回 "(root)"）。"""
    return ((info or {}).get("batch_name") or (info or {}).get("batch")
            or "(root)")


def _batch_label_from(files, batch):
    """从已登记文件里取该批次的显示名（取不到退回 batch 键）。"""
    for v in files or []:
        if v.get("batch_name"):
            return v.get("batch_name")
    return batch or "(root)"


def group_batches(items):
    """按「批次身份」（分享优先，否则分享根）把条目归批，保持出现顺序。

    返回 {batch_key: [item, ...]}。历史行没有分享参数时自然退回 `_share_root`。
    """
    groups = {}
    for it in items:
        key, _name = _batch_identity(it.get("server_path"),
                                     parse_share_download(it))
        groups.setdefault(key, []).append(it)
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


def format_summary_brief(s):
    """启动期极简摘要：**恰好 1 行**（明细见手动「立即读取网盘任务库」诊断按钮）。

    与 format_summary 首行同款口径：不可用时给出原因；异常时降级为固定文案。
    供启动探测日志使用——不再逐行输出库路径/批次/分卷/本地路径明细。
    """
    try:
        if not s or not s.get("ok"):
            return [f"百度任务库：未启用或不可用（{(s or {}).get('reason', '')}）"]
        return [f"活动任务 {s['active']} 条，历史 {s['history']} 条；"
                f"批次 {len(s['batches'])} 个，分卷组 {len(s['volumes'])} 组"]
    except Exception:
        return ["百度任务库摘要不可用"]


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
    # 2.F 全链路：已记住的分享链接。surl -> 记录；shareid -> 同一条记录（用于关联）
    "shares": {},
    "shares_by_id": {},
    # 2.F：已判定「链接已失效」的分享。surl -> 原因（进程内记忆，供之后的手势短路）
    "dead": {},
}


# D7：会话级「本次进程内已拉起次数」计数。刻意**放在 _TRACK 之外**，免得干扰
# 现有对 _TRACK 的迭代；也刻意**只存内存不落盘**——去重窗口就是「一次进程生命周期」。
_LAUNCHES = {}      # surl -> 本次进程内已拉起次数（D7：会话内去重，重启即忘）


def share_launch_count(surl):
    """返回本进程内该 surl 已拉起的次数（未知/空串返回 0）。

    D7：只做内存计数、不持久化——去重窗口＝「一次进程生命周期」，重启即忘。
    绝不抛异常，开销极小。
    """
    try:
        key = str(surl or "").strip()
        if not key:
            return 0
        return int(_LAUNCHES.get(key) or 0)
    except Exception:
        return 0


def bump_share_launch(surl):
    """把该 surl 的拉起次数 +1 并返回新值（空 surl 忽略，返回 0）。

    D7：仅内存、不持久化。绝不抛异常。
    """
    try:
        key = str(surl or "").strip()
        if not key:
            return 0
        n = int(_LAUNCHES.get(key) or 0) + 1
        _LAUNCHES[key] = n
        return n
    except Exception:
        return 0


def _norm_path(p):
    return str(p or "").replace("/", "\\").rstrip("\\").lower()


# ---------- 2.F 全链路：分享「链接 ↔ 下载任务」的关联键（纯解析，无网络） ----------
_SHARE_URL_RE = re.compile(r"https?://pan\.baidu\.com/s/1([A-Za-z0-9_-]+)", re.I)
_SURL_RE = re.compile(r"[?&]surl=([A-Za-z0-9_-]+)")


def parse_share_url(url):
    """把剪贴板里的百度分享链接解析成 {"surl","pwd","url"}；不是分享链接返回 None。"""
    try:
        s = str(url or "")
        # D6：只认 pan.baidu.com 域名的分享链接。此前 `surl=` 兜底匹配不限域名，
        # 任何带 surl 参数的第三方站点都会被误当成百度分享；这里先做域名守卫。
        if "pan.baidu.com" not in s.lower():
            return None
        m = _SHARE_URL_RE.search(s)
        if m:
            surl = m.group(1)
        else:
            m2 = _SURL_RE.search(s)
            if not m2:
                return None
            surl = m2.group(1)
        pwd = ""
        try:
            pwd = (parse_qs(urlsplit(s).query).get("pwd") or [""])[0]
        except Exception:
            pass
        return {"surl": surl, "pwd": pwd, "url": s}
    except Exception:
        return None


def looks_like_share_code(text):
    """判断文本是否像百度网盘提取码：4 位字母数字（大小写均可）。"""
    try:
        s = str(text or "").strip()
        if not s:
            return False
        # 用 fullmatch 而非 search：长文本不应被误判为提取码。
        return bool(re.fullmatch(r"[A-Za-z0-9]{4}", s))
    except Exception:
        return False


def mapped_code(share_uk):
    """该分享者是否配置了固定提取码；返回 code 或 None（薄封装 db.find_share_code，**不做应用**）。

    d3 收紧后的口径：固定映射不再自动套用——monitor 用本函数只判断「有没有」，
    是否使用必须由用户显式手势决定。绝不抛异常。
    """
    try:
        if not share_uk:
            return None
        from . import db as _db
        code = _db.find_share_code(str(share_uk))
        if code:
            c = str(code).strip()
            if c:
                return c
    except Exception:
        pass
    return None


def recent_code_from_history(history):
    """只在「运行时剪贴板历史」里选一个候选提取码，返回 (code|None, source)。

    history 元素通常是 (t, text)（t 为墙钟 float），也容忍纯字符串（时间未知）
    与畸形条目：取 t 最大且像提取码的那条；若都没有可用时间戳但有条目像提取码，
    回退到「最后一个」像提取码的条目。source："recent" 命中，"" 无候选。
    绝不抛异常。
    """
    try:
        best_ts = None
        best_text = None
        fallback_text = None
        for ent in (history or []):
            if isinstance(ent, str):
                text = ent
                ts = None
            else:
                try:
                    text = ent[1]
                except Exception:
                    continue          # 畸形条目直接跳过
                try:
                    ts = float(ent[0])
                except Exception:
                    ts = None
            if not looks_like_share_code(text):
                continue
            fallback_text = text
            if ts is not None and (best_ts is None or ts > best_ts):
                best_ts = ts
                best_text = text
        if best_text is not None:
            return (best_text, "recent")
        if fallback_text is not None:
            return (fallback_text, "recent")
    except Exception:
        pass
    return (None, "")


# 手动拉起时「最近提取码」的有效期（秒），用户定的 120
CODE_CANDIDATE_TTL = 120.0


def fresh_code_from_history(history, ttl=CODE_CANDIDATE_TTL, now=None):
    """只在「时间戳可解析且仍在时效内」的历史条目里选一个候选提取码。

    与 recent_code_from_history 的差别：本函数**严格要求时间戳**——
    只有满足 `0 <= now - ts <= ttl` 的条目才参与，无时间戳的条目一律忽略
    （手动拉起是事后补码，必须严格按时效，不能拿陈年旧码充数）。

    返回 (code|None, source)：在时效内、时间戳最大且 looks_like_share_code 的
    那条命中时返回 (code, "recent")；否则 (None, "")。
    `now` 缺省 time.time()。history 可能是 None/数字/畸形条目，绝不抛异常。
    """
    try:
        if now is None:
            now = time.time()
        best_ts = None
        best_text = None
        for ent in (history or []):
            if isinstance(ent, str):
                continue              # 无时间戳：严格时效，忽略
            try:
                text = ent[1]
            except Exception:
                continue              # 畸形条目直接跳过
            try:
                ts = float(ent[0])
            except Exception:
                continue              # 时间戳不可解析：忽略
            age = now - ts
            if age < 0 or age > ttl:
                continue              # 未生效 / 已过期
            if not looks_like_share_code(text):
                continue
            if best_ts is None or ts > best_ts:
                best_ts = ts
                best_text = text
        if best_text is not None:
            return (best_text, "recent")
    except Exception:
        pass
    return (None, "")


def resolve_invoke_code(share_uk, entries, ttl=CODE_CANDIDATE_TTL):
    """手动拉起分享、记录本身无提取码时，决定要不要补码。返回 (code|None, source)。

    三态语义：
    - ("mapped")：该分享者配有**固定提取码映射** → 返回 (None, "mapped")，
      即**不**静默套用固定码（固定码是「固定提取码手势」/Alt+3 的显式手势，
      这里只提示用户改用那个手势）；
    - ("recent")：无固定映射，但在时效内找到最近捕获的提取码 → (code, "recent")；
    - ("")：无固定映射、时效内也无候选 → (None, "")。

    `entries` 形如 [(ts, text)]（见 AppState.temp_password_entries）。
    绝不抛异常。"""
    try:
        if mapped_code(share_uk):
            return (None, "mapped")
        return fresh_code_from_history(entries, ttl)
    except Exception:
        return (None, "")


def pick_share_code(share_uk, history):
    """按 d3/d4/d5 选一个候选提取码，返回 (code, source)。

    source 取值："map" = 特殊用户（share_uk）固定提取码映射命中；
                  "recent" = 运行时剪贴板历史里「绝对最近」且像提取码的那条；
                  "" = 没有候选。
    优先级：特殊用户映射 > 最近复制（d5 要求映射优先）。
    只返回一个候选（d4：默认只试最近 1 个）。绝不抛异常。
    实现＝ mapped_code + recent_code_from_history 的组合（行为与旧版逐字一致）。
    """
    # d3/d5：特殊用户固定映射优先于「最近复制」。
    code = mapped_code(share_uk)
    if code:
        return (code, "map")
    # d5：否则取剪贴板历史里「绝对最近」且像提取码的那条。
    c, src = recent_code_from_history(history)
    if c is not None:
        return (c, src)
    return (None, "")


def extract_share_ids_from_html(html):
    """从公开分享页 HTML 取 share_uk / shareid（**无需登录**）。

    真实页面里同一份数据有两种写法（取证：备份目录里的分享页快照）：
      ① JSON 片段：      …, "share_uk":"1102408115653", "shareid":21895586648, …
                        （**键带引号**：这是唯一能取到数字的地方）
      ② window.yunData： share_uk: data.share_uk, shareid: data.shareid
                        （值是对变量的引用，取不到数字）
    因此两种写法都要认：先按①在全文找（带引号键），取不到再退回②在 yunData 段内找。
    取不到 shareid 返回 None。"""
    try:
        t = html or ""
        m = re.search(r"window\.yunData\s*=\s*\{.*?\};", t, re.S)
        seg = m.group(0) if m else t
        sid = re.search(r'"shareid"\s*:\s*"?(\d+)', t) or \
            re.search(r'shareid\s*:\s*["\']?(\d+)', seg)
        if not sid:
            return None
        uk = re.search(r'"share_uk"\s*:\s*"?(\d+)', t) or \
            re.search(r'share_uk\s*:\s*["\']?(\d+)', seg)
        return {"shareid": sid.group(1), "share_uk": uk.group(1) if uk else None}
    except Exception:
        return None


# ---------- 2.F：失效分享页（分享被取消/过期/违规无法访问）的识别与进程内标记 ----------
# 死页由服务器直接渲染、不依赖 JS（取证：真实死页 vs 活页快照逐项对比）：
#   - 死页 <title> = 「百度网盘-链接不存在」，正文有 "share_page_type":"error"；
#   - 活页 <title> = 「百度网盘-分享文件」，无 "share_page_type":"error"
#     （活页该字段取值是 "multi" 等，故必须精确匹配 "error" 这一取值）。
# 判据只用服务器直出的 HTML 信号；**不**用 neglect / error-reason / errno 数字——
# neglect 活页也有；error-reason 在死页只是空模板残留（文案由 JS 拿到 errno 后回填）；
# errno 在多处 JSON 里出现、含义不唯一。
DEAD_SHARE_PREFIX = "链接已失效"

# <title> 里的失效文案族（取消/过期/侵权/违规共用同一套错误页骨架）。
_DEAD_TITLE_WORDS = ("链接不存在", "链接已失效", "分享已取消", "分享已过期")


def detect_dead_share_page(html):
    """识别「分享已失效」页：命中返回以 DEAD_SHARE_PREFIX 开头的中文原因，否则 None。

    绝不抛异常（None/空/数字/畸形一律 None）。
    判据（服务器直接渲染，不依赖 JS）：
      A) <title> 命中失效文案族：链接不存在 / 链接已失效 / 分享已取消 / 分享已过期；
      B) 正文出现 "share_page_type":"error"（允许冒号两侧空白）。
    命中 A 时原因带命中文字，如 "链接已失效（链接不存在）"；
    仅命中 B 时用 "链接已失效（分享页异常，可能已被取消或过期）"。
    禁止使用 neglect / error-reason / errno 数字作为判据（neglect 活页也有；error-reason
    在死页是空模板残留；errno 在多处 JSON 里出现、含义不唯一）。
    """
    try:
        t = html if isinstance(html, str) else ""
        if not t:
            return None
        m = re.search(r"<title[^>]*>(.*?)</title>", t, re.I | re.S)
        title = m.group(1) if m else ""
        for w in _DEAD_TITLE_WORDS:
            if w in title:
                return f"{DEAD_SHARE_PREFIX}（{w}）"
        if re.search(r'"share_page_type"\s*:\s*"error"', t):
            return f"{DEAD_SHARE_PREFIX}（分享页异常，可能已被取消或过期）"
        return None
    except Exception:
        return None


def is_dead_share_reason(reason):
    """失败原因是否属于「链接已失效」：str(reason or "").startswith(DEAD_SHARE_PREFIX)。"""
    try:
        return str(reason or "").startswith(DEAD_SHARE_PREFIX)
    except Exception:
        return False


def mark_share_dead(surl, reason=""):
    """把某分享标记为已失效（进程内记忆，供之后的手势短路）。绝不抛异常。

    - 记入 _TRACK 的新键 "dead"（surl -> 原因）。
    - 若 _TRACK["shares"] 里已有该 surl 的记录，就地把同一 dict 打上
      rec["dead"]=原因、rec["dead_reason"]=原因（该 dict 是共享对象，见
      remember_share_link）。
    """
    try:
        key = str(surl or "").strip()
        if not key:
            return
        r = str(reason or "")
        _TRACK["dead"][key] = r
        rec = _TRACK["shares"].get(key)
        if isinstance(rec, dict):
            rec["dead"] = r
            rec["dead_reason"] = r
    except Exception:
        pass


def share_dead(surl):
    """返回失效原因字符串；未标记/未知/异常一律返回 ""（空串）。绝不抛异常。"""
    try:
        key = str(surl or "").strip()
        if not key:
            return ""
        v = _TRACK["dead"].get(key)
        return v if isinstance(v, str) else ""
    except Exception:
        return ""


def parse_share_download(info):
    """从活动任务的 download_url / param2 解析「分享下载」的关联键（纯函数）。

    download_url（客户端分享下载）形如::
        https://d.pcs.baidu.com/file/<md5>?fid=<share_uk>-250528-<fs_id>&rt=sh
            &shareid=<shareid>&vuk=<下载者uk>&…
    param2 形如::
        uk=<share_uk>&primaryid=<shareid>&fid_list=[<fs_id>]&product=share
            &extra={"sekey":"…"}&token=<token>

    返回 {shareid, share_uk, fs_id, md5, sekey, token, vuk, dlink}；
    无法确认为「分享下载」时返回 None。本函数不抛异常。
    """
    try:
        info = info or {}
        url = str(info.get("download_url") or "").strip()
        param2 = str(info.get("param2") or "").strip()
        if not url and not param2:
            return None
        out = {}
        if url:
            parts = urlsplit(url)
            q = parse_qs(parts.query)
            m = re.match(r"^(\d+)-250528-(\d+)$", (q.get("fid") or [""])[0])
            if m:
                out["share_uk"] = m.group(1)
                out["fs_id"] = m.group(2)
            for src, dst in (("shareid", "shareid"), ("vuk", "vuk")):
                v = (q.get(src) or [""])[0]
                if v:
                    out[dst] = v
            seg = [x for x in parts.path.split("/") if x]
            if seg:
                out["md5"] = seg[-1]
            out["dlink"] = url
        if param2:
            q2 = parse_qs(param2)
            for src, dst in (("uk", "share_uk"), ("primaryid", "shareid"),
                             ("token", "token")):
                v = (q2.get(src) or [""])[0]
                if v:
                    out.setdefault(dst, v)
            fm = re.search(r"(\d{6,})", (q2.get("fid_list") or [""])[0])
            if fm:
                out.setdefault("fs_id", fm.group(1))
            try:
                sekey = (json.loads((q2.get("extra") or [""])[0]) or {}).get("sekey")
                if sekey:
                    out["sekey"] = sekey
            except Exception:
                pass
        if not (out.get("shareid") or out.get("share_uk")):
            return None
        return out
    except Exception:
        return None


def remember_share_link(url, html=None):
    """记住一条分享链接（剪贴板捕获时调用），并按 shareid 建索引。

    html 给出时顺带解析 shareid/share_uk（公开分享页无需登录即可解析）。
    返回记录 dict 或 None；写入 _TRACK["shares"] / _TRACK["shares_by_id"]。"""
    try:
        p = parse_share_url(url)
        if not p:
            return None
        rec = {"surl": p["surl"], "pwd": p["pwd"], "url": p["url"],
               "ts": time.time()}
        ids = extract_share_ids_from_html(html) if html else None
        if ids:
            rec.update(ids)
        _TRACK["shares"][p["surl"]] = rec
        if rec.get("shareid"):
            _TRACK["shares_by_id"][str(rec["shareid"])] = rec
        return rec
    except Exception:
        return None


def share_link_for(share):
    """给定 parse_share_download 的结果，找回已记住的分享链接（没有返回 None）。"""
    try:
        sid = str((share or {}).get("shareid") or "")
        rec = _TRACK["shares_by_id"].get(sid) if sid else None
        if rec:
            return rec.get("url")
    except Exception:
        pass
    return None


def last_share():
    """最近记住的一条分享链接记录（按时间），没有返回 None。"""
    try:
        recs = list(_TRACK["shares"].values())
        return max(recs, key=lambda r: r.get("ts") or 0) if recs else None
    except Exception:
        return None


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
            sp = _as_text(r.get("server_path"))
            sh = parse_share_download(r)            # 分享下载的关联键（2.F）
            bkey, bname = _batch_identity(sp, sh)
            info = {
                "local_path": lp,
                "server_path": sp,
                "size": r.get("file_size"),
                "isdir": r.get("isdir"),
                "batch": bkey,
                "batch_name": bname,
                "task_id": tid,
                "add_time": r.get("add_time"),
                "state": "active",
                "share": sh,
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
            sh = parse_share_download(r)
            if sh:
                info["share"] = sh
            # 批次身份随 server_path / 分享信息刷新，保证同一路径被重新下载（可能
            # 来自另一个分享）时也归到正确的批次，不残留旧键。
            bkey, bname = _batch_identity(info.get("server_path"),
                                          info.get("share"))
            info["batch"] = bkey
            info["batch_name"] = bname
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
            sp = _as_text(r.get("server_path"))
            sh = parse_share_download(r)
            bkey, bname = _batch_identity(sp, sh)
            files.append({
                "local_path": _as_text(r.get("local_path")),
                "size": r.get("file_size"),
                "isdir": r.get("isdir"),
                "batch": bkey,
                "batch_name": bname,
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


def gather_volume_set(local_path, root=None):
    """跨目录分卷集合：同一批次 + 与首卷同系列（`is_volume_file` 口径）的全部成员。

    用途：有些分享把同一套分卷分散在不同子目录（同一次下载批次），而 7-Zip 只会
    在同一目录里找兄弟卷。发现这种情况时，上层可把成员**归拢**到首卷目录后再走
    既有的目录局部管线（`monitors._consolidate_cross_dir_volumes`）。

    返回：
    - None —— 不适用：未跟踪 / 非首卷 / 同目录（交由原逻辑）/ 成员不足 2 /
      卷号重复（歧义）/ 有成员不在 root 下 / 任何异常；
    - dict —— {"members": [info, ...]（含首卷自身）,
               "ready": bool（全部 state=="done"、在盘、非未完成下载、编号连续无重复）}。

    注意：完整性只判「清单层面」（客户端是否下完）；压缩包层面的末卷/EOCD/尾卷
    判定交给归拢后的 `_volume_ready`，两侧各用各自权威的信号，不重复实现。
    本函数不抛异常（模块铁律）。
    """
    try:
        info = _TRACK["files"].get(_norm_path(local_path))
        if not info or info.get("isdir"):
            return None
        name = Path(str(local_path)).name
        if not is_volume_name(name) or not is_first_volume(name):
            return None
        batch = info.get("batch")
        stem = Path(name).stem
        # 成员判定一律走 is_volume_file：它覆盖 .NNN / .zNN / .rNN / .partN.rar，
        # 以及「不带编号的末卷」（test.zip.001 系列的 test.zip）。
        members = [info]
        for v in _TRACK["files"].values():
            if v is info or v.get("isdir") or v.get("batch") != batch:
                continue
            vname = Path(str(v.get("local_path") or "")).name
            if vname and is_volume_file(name, vname, stem):
                members.append(v)
        if len(members) < 2:
            return None
        dirs = {_norm_path(Path(str(m.get("local_path"))).parent)
                for m in members}
        if len(dirs) < 2:
            return None                    # 同目录：交还原目录局部逻辑
        if root is not None:
            r = _norm_path(root)
            for m in members:
                if not _norm_path(str(m.get("local_path") or "")).startswith(r + "\\"):
                    return None            # 跨监听根（可能跨盘）：不动
        nums = [n for n in (_volume_number(Path(str(m.get("local_path"))).name)
                            for m in members) if n is not None]
        if len(nums) != len(set(nums)):
            return None                    # 卷号重复 → 两套同名分卷，无法区分，放弃
        ready = (sorted(nums) == list(range(1, len(nums) + 1))
                 and all(m.get("state") == "done"
                         and Path(str(m.get("local_path"))).is_file()
                         and not is_incomplete_download(
                             Path(str(m.get("local_path"))))
                         for m in members))
        return {"members": members, "ready": ready}
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
        extra = ""
        sh = it.get("share") or {}
        if sh:
            extra = (f"，分享 shareid={sh.get('shareid')} uk={sh.get('share_uk')}"
                     f" fs_id={sh.get('fs_id')}")
            link = share_link_for(sh)
            if link:
                extra += f"，链接 {link}"
            elif sh.get("sekey"):
                extra += "，已含提取码校验(sekey)"
        _log(f"网盘新任务：{it.get('local_path')}（{it.get('size')} B，"
             f"批次 {_batch_label(it)}{extra}）")
    # B：新出现文件的批次，输出「预登记」清单（结构摘要，便于提前建目录/等分卷）
    for b in {it.get("batch") for it in (events.get("started") or [])}:
        fs = batch_files(b)
        ndir = sum(1 for v in fs if v.get("isdir"))
        _log(f"批次预登记 {_batch_label_from(fs, b)}：共 {len(fs)} 项（目录 {ndir}）")
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
        fs = batch_files(b)
        n, a, d, g = batch_state(b)
        if a == 0 and d > 0 and g == 0:
            # 显示名按「分享层目录」推导；绝不用通用根（如「我的资源」）冒充批次名，
            # 通知口径明确为「本批次」（只统计该分享键下的已登记项）。
            name = _batch_label_from(fs, b)
            _log(f"网盘下载批次完成：{name}（本批次 {d} 项全部完成）")
            if notify:
                try:
                    hub.notify("网盘下载完成", f"{name}：{d} 个文件已下载完成")
                except Exception:
                    pass
            notified.append(b)
    return notified
