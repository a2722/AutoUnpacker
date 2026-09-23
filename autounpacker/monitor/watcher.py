# -*- coding: utf-8 -*-
"""后台目录轮询监控（Stage 6b 从 autounpacker.monitors 拆出）。

FolderWatcher：轮询监听目录表层，识别压缩包并触发智能解压（嵌套/密码/分卷/伪装/
删除回溯）；分卷到齐判断、翻译 JSON 归位、百度清单模式（Tier-2 子目录处理，只增强
不阻断）。本文件持有 FolderWatcher 及其独占的模块级常量/助手；QRMonitor 见
monitor/clipboard.py；旧导入路径由 autounpacker.monitors 兼容 shim 保持可用。
"""
import os
import shutil
import threading
import time
import types
from collections import OrderedDict
from pathlib import Path

from .. import extract as smart_extract   # noqa: F401  保留原名引用
from .. import trail as deletion_trail     # noqa: F401
from ..deletion import quarantine as deletion_quarantine
from ..deletion import records as deletion_records
from .. import db                          # noqa: F401
from .. import volume_pair
from .. import baidu_manifest             # 实验性开关判定（子目录监听/分卷递归的唯一闸门）
from ..config import get_bool
from ..utils import (_norm_path_for_cfg, _can_open_append)

# 删除前意图记录（records.mark_deleting）属本任务新增函数：trail 兼容 shim 的显式
# 再导出清单早于它，这里把新函数绑定到 shim（shim 的 __setattr__ 会同步写 records
# 归属模块，读写同一份状态），保证 `deletion_trail.mark_deleting` 在旧路径下可用。
if not hasattr(deletion_trail, "mark_deleting"):
    try:
        deletion_trail.mark_deleting = deletion_records.mark_deleting
    except Exception:
        pass


def _mark_deleting(rid, targets):
    """删除动作前预写「正在删除…」意图记录（尽力而为，绝不打断删除主流程）。

    优先走 deletion_trail.mark_deleting；调用方的 trail 被替换成不含该函数的
    测试桩时直接退回 records 归属模块（同一 _lock/TRAIL_FILE）。任何异常都吞掉：
    意图记录只是崩溃兜底，绝不能反过来阻断真正的删除/解压流程。
    """
    try:
        fn = getattr(deletion_trail, "mark_deleting", None)
        if not callable(fn):
            fn = getattr(deletion_records, "mark_deleting", None)
        if callable(fn):
            fn(rid, targets)
    except Exception:
        pass


def _in_quarantine(path):
    """路径是否位于隔离区（_已删除）之内：任一路径段等于 QUARANTINE_DIRNAME 即命中。

    隔离区里的文件是「已删除源文件的可还原副本」，绝不能再被当成新压缩包扫描/重解，
    否则会被反复处理甚至再次删除。所有枚举监听/输出根的地方都必须先过这一关。
    """
    return deletion_quarantine.in_quarantine(path)


def _tasks_changed(hub):
    """任务生命周期事件（尽力而为）：Hub 实现了 tasks_changed 才投递。

    监听/拖放链路每次写 tasks 表后调用；测试桩 Hub 未实现该方法时静默跳过，
    绝不让「上报刷新」这一增强反过来打断既有的解压/失败处理流程。
    """
    try:
        cb = getattr(hub, "tasks_changed", None)
        if cb is not None:
            cb()
    except Exception:
        pass


# 分卷等待期「目录持续活动」的容忍上限（秒）：分卷目录里只要还有文件在下载/增长，
# 就不在此期间强解（洞1：改名上传的兄弟卷因此被看见）。但连续活动超过本上限后
# 不再以此压制兜底——否则一个无限增长的不相关文件能让等待永不结束。
PAIR_DIR_ACTIVE_MAX_SEC = 1800

# 截断分卷链的有界重试（与 _lock_retry 同一精神）：最多尝试 SPLIT_INCOMPLETE_MAX 次，
# 每次退避递增（SPLIT_INCOMPLETE_DELAYS：300→900→1800s），到顶后放弃自动重试并如实
# 告知一次；源文件与分卷一律原样保留（绝不回收/删除）。源文件身份（size/mtime）或
# 源目录内容变化 → 计数清零、重新武装（新到的分卷会立即重新触发重试）。不加配置键。
SPLIT_INCOMPLETE_MAX = 3
SPLIT_INCOMPLETE_DELAYS = (300, 900, 1800)


def _restored_exempt(fp):
    """该源文件是否处于「已还原 ⇒ 豁免」状态（防御性：取不到方法/异常一律按不豁免）。

    部分离线测试把 `deletion_trail` 换成只带少量方法的替身，取不到
    `is_restored_exempt` 时必须退回旧行为，绝不让监听线程因这条增强分支崩溃。
    """
    fn = getattr(deletion_trail, "is_restored_exempt", None)
    if not callable(fn):
        return False
    try:
        return bool(fn(fp))
    except Exception:
        return False


class FolderWatcher(threading.Thread):
    """多路径监听线程（只监听目录表面，不递归子孙文件夹）"""

    PROBE_CYCLES = 4  # 新文件未被识别为压缩包时的复查轮数
    # seen 文件中「连续 skip + 大小变化」重走（含观察期重置）的硬上限：持续增长
    # 但根本不是压缩包的文件（监听目录里正在写的 .log/.db 等）达到本上限后不再
    # 逐轮重走 _handle（含 analyze_file）。默认 poll_interval=2s，60 次 ≈ 2 分钟：
    # 足够覆盖绝大多数正常下载（下载中每轮大小都在变，直到尾部落盘后变完整），
    # 又不会让非压缩包文件永久 churn。达到上限后仅当大小**稳定下来**（可能是
    # 下载已完成）时才给一次复查机会；仍非压缩包则继续保持上限。
    REWALK_MAX_STREAK = 60
    # traced 的硬上限（≈几千条）：traced 每个已处理文件一条，长会话/大量下载会
    # 单调增长吃内存。超过本上限按 LRU 淘汰最旧条目；被等待/重试状态引用的条目
    # 绝不被淘汰（见 _traced_protected）。原始下载文件另有 trail.already_handled
    # 的持久去重兜底，故淘汰只可能让极老、之后又变化过的中间产物被再处理一次。
    TRACED_MAX = 4096
    TRANSLATION_MAX_SIZE = 10 * 1024 * 1024  # 翻译 json 最大 10MB
    TRANSLATION_WINDOW = 5 * 60              # 小文件夹先出现时的监控窗口（秒）
    VOL_MAX_WAIT = 300   # 首卷分卷"只有满卷"时最长等待(秒)：之后兜底按现状尝试解压
    PAIR_WAIT_EXTEND_SEC = 600  # 已检测到改名链但未通过验证时的有界延长等待(秒)：绝不强解截断链
    PAIR_DIR_ACTIVE_MAX_SEC = PAIR_DIR_ACTIVE_MAX_SEC  # 兼容「类属性常量」访问（值同模块常量）
    SPLIT_INCOMPLETE_MAX = SPLIT_INCOMPLETE_MAX        # 截断分卷链最多重试次数（同模块常量）
    SPLIT_INCOMPLETE_DELAYS = SPLIT_INCOMPLETE_DELAYS  # 截断分卷链退避序列（秒）
    SPLIT_MAX_WAIT = 1800        # 跨目录分卷等待兄弟卷的最长时间(秒)：超时放弃（仅保留不删）
    SPLIT_RECHECK_INTERVAL = 10  # 跨目录分卷复查间隔(秒)：节流「全监听根 rglob」
    OFFLINE_NOTIFY_SEC = 300     # 监听目录连续离线超过 5 分钟告警一次（每段离线只发一次）
    BT_STABLE_SEC = 6    # 百度清单模式：文件大小需稳定这么久才处理（秒）
    BT_PROGRAM_EXT = {".exe", ".dll", ".bat", ".cmd", ".lnk", ".msi", ".sys",
                      ".scr", ".com", ".ocx"}
    # 百度清单模式：目录内程序文件至少这么多才判「疑似程序目录」（单个 .exe 不判，
    # 保守：宁可偶尔多解一次，也不把整个监听根禁掉）。见 _looks_like_program_dir。
    BT_PROGRAM_DIR_MIN = 2

    def __init__(self, state, hub, pauser=None):
        super().__init__(daemon=True)
        self.state = state
        self.hub = hub
        self.pauser = pauser
        self.seen = {}
        # seen 的身份镜像 {watch_key: {name: (size, mtime)}}：与 seen 一一对应。
        # seen 本身只记名字（=「已定型」），本结构让 seen 也「身份感知」：每轮对
        # seen 中的文件做一次 stat，身份变化的同名文件（下载完成/重新下载/替换）
        # 会被静默重走 _handle；身份不变者绝不重走（零额外 churn）。
        self._seen_ident = {}
        # 身份重走的 churn 上限状态：{(watch_key, name): {"streak": 连续「skip +
        # 大小变化」重走次数, "blocked": 是否已达上限, "pending": 达上限后是否出现过
        # 大小变化（等大小稳定后再给一次机会）}}。随「大小稳定 / 成功处理 / 文件
        # 消失」复位，避免长期占内存。
        self._rewalk = {}
        # 已处理过的文件身份 {norm_path: (size, mtime)}：同名但内容不同的
        # 新文件（重新下载/替换）不会被误当成已处理而跳过。有界 LRU：
        # 超 TRACED_MAX 淘汰最旧；仍被等待/重试引用的条目不淘汰（见 _traced_put）。
        self.traced = OrderedDict()
        self.probing = {}  # {watch_key: {name: 剩余复查轮数}}
        # 翻译文件监控窗口 {小文件夹路径: (json_stem, 首次发现时间)}
        self._pending_trans = {}
        # 超时放弃的翻译文件夹 {小文件夹路径 str: json 身份(size, mtime)}：
        # 身份不变则不再重开监控窗口（防「超时→删→立刻重登记」死循环）
        self._trans_given_up = {}
        # 首卷分卷观察期 {abs_path: {seen, last_activity, stable_since}}
        self._vol_wait = {}
        # 输出文件被占用的重试计数 {abs_path: 次数}（上限 3 次，防死循环）
        self._lock_retry = {}
        # 截断分卷链的有界重试 {abs_path: {ident, dir_sig, count, given_up}}：
        # 身份/目录变化自动重新武装；到上限后 given_up=True 不再放行（保留源与分卷）
        self._split_incomplete_retry = {}
        # 百度清单模式（Tier-2）：已处理集合 / 稳定观察 / 已告警 / 粘性记忆缓存
        self._bt_seen = {}      # {watch_key: set(norm_abs_path)}
        self._bt_probe = {}     # {watch_key: {norm_abs_path: (size, 稳定起始时间)}}
        self._bt_warned = set()
        self._bt_sticky = None  # set(norm_abs_path)，来自 toolbox.db 的粘性记忆
        self._bt_consolidated = set()  # 已归拢（或已放弃）的跨目录分卷首卷：不再重复归拢
        self._out_warned = set()  # 已提示过「输出目录与监听根重叠」的路径
        # P1-14（选项 D）磁盘空间闸门：已发过「空间不足」提醒的监听目录 norm_path。
        # 低空间期间同一目录只记一次日志 / 发一次通知；空间恢复后清除并记一次
        # 「恢复」，重新武装下一次提醒（见 _disk_space_ok）。
        self._disk_warned = set()
        # 跨目录分卷等待期 {norm_source_path: {anchor, ident, since, last_check, last_sig}}
        self._split_pending = {}
        # 当前离线的监听路径 {norm_path: 进入离线时刻}：目录不存在或无法枚举时进入，
        # 枚举成功才由 _mark_online 清除（每段离线只记一次日志、超 5 分钟通知一次）。
        self._offline = {}
        self._offline_notified = set()  # 已发过「离线超 5 分钟」通知的 norm_path
        # 每目录最近一次发布的 (state, progress, name)：新版 GUI 胶囊 / 状态灯用；
        # 只在值变化时投递，避免 2s 轮询把队列刷爆（见 _set_dir_state）。
        self._dir_state = {}
        # P1-9（选项 D）未完成下载可见性：{watch_key: set(name)} 各监听目录当前
        # 「下载中后缀」文件名单（只按文件名判断，无额外 stat）；总数与
        # _inc_total 不同才记一行日志；_inc_notified 按 norm_path 记已通知的
        # 文件（每文件只通知一次）。只做展示，不参与重走/复查。
        self._inc_dirs = {}
        self._inc_total = 0
        self._inc_notified = set()

    @staticmethod
    def _file_identity(path):
        """文件身份 = (大小, 最后修改时间)。失败返回 None。"""
        try:
            st = Path(path).stat()
            return (st.st_size, st.st_mtime)
        except OSError:
            return None

    def _identity_safe(self, fp):
        """_file_identity 的容错包装：stat 失败/异常一律记为 None（该轮不重走）。"""
        try:
            return self._file_identity(fp)
        except OSError:
            return None

    @staticmethod
    def _is_same_traced(old_ident, ident):
        return old_ident is not None and old_ident == ident

    @staticmethod
    def _size_changed(old_ident, ident):
        """身份是否「值得重走」：只看大小变化（mtime 仅作辅助记录）。

        `_file_identity` 是 (size, mtime)，索引器/杀软/`copy /b` 只碰 mtime 就会让
        整包重新解压。故身份重走一律以 size 为准；size 未变而 mtime 变了，只更新
        记录的 mtime、绝不重走。取不到身份（None）也不重走。"""
        if old_ident is None or ident is None:
            return False
        return old_ident[0] != ident[0]

    def _bump_rewalk(self, rk):
        """记一次「skip + 大小变化」重走；达到上限返回 False（并置 blocked）。

        `rk` = (watch_key, name)。未达上限返回 True，调用方可继续把该文件留在
        probe 观察期（重置为 PROBE_CYCLES）。"""
        st = self._rewalk.get(rk)
        if st is None:
            st = {"streak": 0, "blocked": False, "pending": False}
            self._rewalk[rk] = st
        st["streak"] += 1
        if st["streak"] >= self.REWALK_MAX_STREAK:
            st["blocked"] = True
            return False
        return True

    # ---------- traced 有界化（LRU，详见 TRACED_MAX 注释） ----------
    def _traced_protected(self):
        """仍被等待/重试/归拢状态引用的文件绝对路径集合。

        淘汰 traced 时必须保证：仍被某条 defer/retry 依赖的文件身份不被清掉。
        审计结论：这些等待状态各自持有身份副本（_split_pending["ident"] /
        _split_incomplete_retry["ident"] / _lock_retry 计数 / _vol_wait 签名），
        **不读取 self.traced**；且各 defer 分支在返回前都会主动
        self.traced.pop(abs_fp)。这里再把这些状态的键取并集作额外保险，
        淘汰时永不误伤仍在等待的文件。任何异常都退化为空集合（不影响淘汰本身）。
        """
        protected = set()
        for d in (self._split_pending, self._lock_retry,
                  self._split_incomplete_retry, self._vol_wait):
            try:
                protected.update(d.keys())
            except Exception:
                pass
        return protected

    def _traced_put(self, key, ident):
        """写入/刷新一个已处理文件身份；超出 TRACED_MAX 时 LRU 淘汰最旧。

        与旧 `self.traced[key] = ident` 等价，仅多了「有界 + 保护等待中条目」。
        任何异常都退回普通写入，绝不让内存治理反过来打断解压主流程。
        """
        try:
            tr = self.traced
            tr[key] = ident
            tr.move_to_end(key)
            if len(tr) > self.TRACED_MAX:
                protected = self._traced_protected()
                for k in list(tr):
                    if len(tr) <= self.TRACED_MAX:
                        break
                    if k in protected:
                        continue   # 等待/重试仍依赖：绝不淘汰
                    tr.pop(k, None)
        except Exception:
            try:
                self.traced[key] = ident
            except Exception:
                pass

    def _traced_touch(self, key):
        """命中时把条目移到末尾（近似 LRU：最近被读取的条目更难被淘汰）。"""
        try:
            self.traced.move_to_end(key)
        except Exception:
            pass

    def _purge_traced_under(self, watch_key):
        """监听路径被移除/禁用时，清掉其下所有 traced 条目（不再监听即无需去重记忆）。"""
        try:
            prefix = watch_key + os.sep
            for k in [k for k in self.traced
                      if k == watch_key or k.startswith(prefix)]:
                self.traced.pop(k, None)
        except Exception:
            pass

    @staticmethod
    def _norm_path(path):
        """规范化路径，用于去重（忽略大小写与尾部分隔符）

        薄委托：规范化口径统一由 utils._norm_path_for_cfg 提供，保持既有名字
        （内部大量 self._norm_path(...) 调用点不改）。
        """
        return _norm_path_for_cfg(path)

    def _set_dir_state(self, path, state, progress=None, name=None):
        """发布一条目录状态消息（dir_state，供新版 GUI 胶囊 / 状态灯消费）。

        - path 统一规范化为键，消息里的 dir 也用规范化路径；
        - 同一目录 (state, progress, name) 未变化则不重复发布（防 2s 轮询刷屏）；
        - 纯队列 put，绝不新增线程、绝不阻塞 GUI。
        """
        try:
            key = self._norm_path(path)
            sig = (state, progress, name)
            if self._dir_state.get(key) == sig:
                return
            self._dir_state[key] = sig
            self.hub.q.put({"type": "dir_state", "dir": key, "state": state,
                            "progress": progress, "name": name})
        except Exception:
            pass

    # ---------- 监听目录离线检测（不可达时不假装「监听中」） ----------
    def _mark_offline(self, watch):
        """目录不可用（不存在 / 枚举失败）：发布 missing，每段离线只记一次日志。

        与 _handle 的忙碌 waiting 区分：missing 专指「目录不存在/不可枚举」
        （文案「目录不存在」），不再冒充「等待中」。连续离线超过
        OFFLINE_NOTIFY_SEC 后补发唯一一条通知；恢复由 _mark_online（枚举成功时）
        负责复位。任何异常都吞掉：状态上报绝不打断监听主流程。
        """
        try:
            key = self._norm_path(watch)
            now = time.time()
            since = self._offline.get(key)
            if since is None:
                since = now
                self._offline[key] = since
                self.hub.log(f"监听目录不可用（等待恢复）: {watch}")
            self._set_dir_state(watch, "missing")
            if (key not in self._offline_notified
                    and now - since > self.OFFLINE_NOTIFY_SEC):
                self._offline_notified.add(key)
                self.hub.notify("监听目录不可用",
                                f"{watch} 已离线超过 5 分钟，请检查磁盘或网络")
        except Exception:
            pass

    def _mark_online(self, watch):
        """目录恢复可用（枚举成功）：清离线记忆与告警标记，并把离线 missing 复位
        为 listening（忙碌 waiting 由 _handle 自己维护，不在这里清除）。"""
        try:
            key = self._norm_path(watch)
            if key in self._offline:
                self._offline.pop(key, None)
                self._offline_notified.discard(key)
                self._set_dir_state(watch, "listening")
        except Exception:
            pass

    # ---------- 未完成下载可见性（P1-9 选项 D：只可见，不重扫/不重走） ----------

    def _inc_recount(self):
        """未完成下载总数与上次记录不同时记一行日志（绝不逐轮重复）。"""
        try:
            total = sum(len(s) for s in self._inc_dirs.values())
            if total == self._inc_total:
                return
            self._inc_total = total
            sample = sorted(n for s in self._inc_dirs.values() for n in s)[:3]
            more = " 等" if total > len(sample) else ""
            detail = ("：" + "、".join(sample) + more) if sample else ""
            self.hub.log(f"当前有 {total} 个未完成下载（下载完成后才会自动解压）"
                         f"{detail}")
        except Exception:
            pass

    def _note_incomplete_downloads(self, watch, key, names):
        """统计本目录的未完成下载文件：数量变化时记一行日志；新文件通知一次。

        P1-9 选项 D 的刻意取舍：只让「同名且大小不变 → 永不再查」的盲区可见，
        绝不重扫目录、绝不重走 seen、绝不改变 _handle 的返回值语义。只按文件名
        判断（is_incomplete_download 仅读 path.name，无额外 stat），异常一律吞掉。
        """
        try:
            inc = set()
            for name in names:
                try:
                    if smart_extract.is_incomplete_download(watch / name):
                        inc.add(name)
                except Exception:
                    continue
            self._inc_dirs[key] = inc
            # 每个文件首次出现时通知一次（按 norm_path 去重，绝不重复）
            for name in sorted(inc):
                np = self._norm_path(watch / name)
                if np in self._inc_notified:
                    continue
                self._inc_notified.add(np)
                try:
                    self.hub.notify(
                        "发现未完成下载",
                        f"{name}\n下载完成后（后缀消失）才会自动解压")
                except Exception:
                    pass
            self._inc_recount()
        except Exception:
            pass

    def run(self):
        # stdout 捕获由进程入口（app.main）统一幂等安装，这里不再改进程级全局。
        while True:
            cfg = self.state.snapshot()
            interval = max(1, int(cfg.get("poll_interval", 2)))
            # 暂停 = 原「停止监听」：不再轮询、不再检测新文件，恢复后重新扫描
            if not self.state.running or (self.pauser is not None
                                          and self.pauser.is_paused()):
                # 暂停期间所有（启用的）监听路径上报 paused；_set_dir_state 自带
                # 「值不变不重复发」，所以每 2s 空转也不会刷屏。
                for wc in cfg.get("watch_paths", []):
                    if wc.get("enabled") and wc.get("path"):
                        self._set_dir_state(wc["path"], "paused")
                time.sleep(interval)
                continue
            enabled = {}
            for wc in cfg.get("watch_paths", []):
                if wc.get("enabled") and wc.get("path"):
                    enabled[self._norm_path(wc["path"])] = wc
            for key in list(self.seen):
                if key not in enabled:
                    del self.seen[key]
                    self._seen_ident.pop(key, None)  # 身份镜像随 seen 一起清理
                    for rk in [k for k in self._rewalk if k[0] == key]:
                        self._rewalk.pop(rk, None)   # churn 计数随监听路径移除清理
                    self._purge_traced_under(key)    # 该路径不再监听：去重记忆一并清理
            for key in list(self._offline):
                if key not in enabled:               # 不再监听的路径：离线记忆一并清理
                    self._offline.pop(key, None)
                    self._offline_notified.discard(key)
            dropped_inc = False
            for key in list(self._inc_dirs):
                if key not in enabled:               # 不再监听的路径：未完成下载统计一并清理
                    self._inc_dirs.pop(key, None)
                    dropped_inc = True
            if dropped_inc:
                self._inc_recount()                  # 计数变化（含归零）时如实记一行
            for path, wc in enabled.items():
                key = self._norm_path(path)
                # 离线目录恢复：允许把离线期间发布的 missing 复位回 listening
                # （离线 missing = 目录不可用，不是 _handle 的忙碌等待；extracting
                # 永远不在此复位）。离线记忆/告警标记不在 run 里清——只有 _poll
                # 真正枚举成功（_mark_online）才算恢复，否则「目录存在但枚举失败」
                # 会在每轮重复刷离线日志。
                recovered = False
                if key in self._offline:
                    try:
                        recovered = Path(path).is_dir()
                    except OSError:
                        recovered = False
                cur = self._dir_state.get(key, (None, None, None))[0]
                # 空闲目录回到 listening（extracting/waiting/missing 视为忙碌，交给
                # _handle / _mark_online 自己收敛；error 会在本轮或下一轮被这里复位
                # 为 listening）。
                if (cur not in ("extracting", "waiting", "missing")
                        or (recovered and cur in ("waiting", "missing"))):
                    self._set_dir_state(path, "listening")
                try:
                    self._poll(Path(path), wc)
                except Exception as e:
                    self.hub.log(f"监听轮询出错 ({path}): {e}")
            time.sleep(interval)

    def _poll(self, watch, wc):
        # 只监听文件夹表面的一层文件，不递归子孙文件夹
        if not watch.is_dir():
            # 目录不存在/不可达：如实上报 missing（此前直接 return，状态永远停在
            # 蓝色「监听中」且无日志，离线盘看起来像在正常工作）。
            self._mark_offline(watch)
            return
        # 输出目录与监听根重叠（等于 / 在其内部）→ 可能「自己吃自己」，提示一次
        try:
            od = str(wc.get("output_dir") or "").strip()
            if od:
                okey = self._norm_path(od)
                wkey = self._norm_path(watch)
                if (okey == wkey or okey.startswith(wkey + os.sep)):
                    if wkey not in self._out_warned:
                        self._out_warned.add(wkey)
                        self.hub.log(
                            f"提示：监听路径与解压输出目录重叠，可能自我循环: "
                            f"{watch} ↔ {od}")
        except Exception:
            pass
        # 翻译 JSON 归位检查（基于根目录下的文件夹，与文件监听相互独立）
        self._translation_check(watch)
        # 百度清单模式（Tier-2）：额外处理下载到子目录里的压缩包/分卷。
        # 只增强、不阻断：清单不可用就什么都不做，等于退回表层模式。
        # 调用点也显式要求实验性开关（_baidu_poll 内部另有同名自检，双保险）：
        # 开关关掉时绝不进入会「摸子文件夹」的百度清单链路。
        if (str(wc.get("mode") or "") == "baidu"
                and baidu_manifest.is_enabled(self.state)):
            try:
                self._baidu_poll(watch, wc)
            except Exception as e:
                self.hub.log(f"百度清单模式处理出错 ({watch}): {e}")
        key = self._norm_path(watch)
        if key not in self.seen:
            try:
                current = set(n for n in os.listdir(watch) if (watch / n).is_file())
            except OSError:
                # 目录存在但枚举失败（权限/网络盘断开）同样视为离线：不得假装监听中
                self._mark_offline(watch)
                return
            self._mark_online(watch)
            self.hub.log(f"开始监听: {watch}")
            # P1-9 选项 D：只统计未完成下载（数量变化记一行、新文件通知一次），
            # 不重扫、不重走 seen，_handle 的返回值语义完全不变。
            self._note_incomplete_downloads(watch, key, current)
            # 初次扫描也处理已存在的压缩包：程序重启/监听路径重初始化时，
            # 已存在文件不能永远跳过（否则一直躺在目录里不处理）。
            # 已成功处理过的文件由 _handle 的 already_handled 跳过；
            # 分卷未到齐的保持不在 seen，下一轮再查。
            deferred = set()
            for name in sorted(current):
                if self._handle(watch / name, wc, initial_scan=True) == "defer":
                    deferred.add(name)
            self.seen[key] = current - deferred
            # 记录初次扫描后仍在 seen 中的文件身份，避免下一轮被误判为「身份变了」
            ident_map = self._seen_ident.setdefault(key, {})
            ident_map.clear()
            for name in self.seen[key]:
                ident = self._identity_safe(watch / name)
                if ident is not None:
                    ident_map[name] = ident
            return
        try:
            current = set(n for n in os.listdir(watch) if (watch / n).is_file())
        except OSError:
            # 同上：枚举失败即离线（missing + 单次日志），恢复后 run()/_mark_online 复位
            self._mark_offline(watch)
            return
        self._mark_online(watch)
        # P1-9 选项 D：同上，仅按文件名统计可见性，不改变任何处理语义。
        self._note_incomplete_downloads(watch, key, current)
        new = current - self.seen[key]
        deferred = set()
        probe = self.probing.setdefault(key, {})
        ident_map = self._seen_ident.setdefault(key, {})
        done_now = set()   # 本轮已由复查轮处理为 done 的文件：避免下面重走同轮重复调用
        # 先复查上一轮进入观察期的文件（这些已在 seen 中）：写入完成后会被
        # 重新识别为压缩包并处理
        for name in list(probe):
            rk = (key, name)
            if name not in current:
                probe.pop(name, None)
                self._rewalk.pop(rk, None)   # 文件消失：churn 计数复位
                continue
            fp = watch / name
            ident = self._identity_safe(fp)
            size_changed = self._size_changed(ident_map.get(name), ident)
            st = self._rewalk.get(rk)
            if st is not None and st.get("blocked"):
                # 已达 churn 硬上限：大小仍在变（如持续写入的 .log）→ 不再重走，
                # 让观察期自然过期；名额留在 _seen_ident 里继续观测大小变化。
                if size_changed:
                    st["pending"] = True
                    probe[name] -= 1
                    if probe[name] <= 0:
                        probe.pop(name, None)
                    if ident is not None:
                        ident_map[name] = ident
                    continue
                if not st.get("pending"):
                    # 上限后大小一直稳定：不复查，让观察期过期
                    probe[name] -= 1
                    if probe[name] <= 0:
                        probe.pop(name, None)
                    if ident is not None:
                        ident_map[name] = ident
                    continue
                # pending：上限后出现过大小变化、现已稳定（可能下载完成）→
                # 消耗一次「恢复机会」复查；若仍非压缩包则继续保持上限。
                st["pending"] = False
            res = self._handle(fp, wc)
            if res == "defer":
                probe.pop(name, None)
                self._rewalk.pop(rk, None)   # 交给下轮重查：churn 计数不复用
                deferred.add(name)
            elif res == "done":
                probe.pop(name, None)
                done_now.add(name)
                self._rewalk.pop(rk, None)   # 成功处理：churn 计数复位
            else:  # 仍不是压缩包
                # 身份相对上一轮发生变化 = 仍在写入/下载（尾部 zip 还没落盘）：
                # 重置观察期继续盯着它（而不是递减）。否则长下载会在固定 4 轮后
                # 被放弃、文件从此失联。身份不变才真正递减、按原语义过期。
                # 但受 churn 硬上限约束：连续「skip + 大小变化」达到
                # REWALK_MAX_STREAK 后不再重置，让非压缩包文件最终放弃。
                if size_changed:
                    if self._bump_rewalk(rk):
                        probe[name] = self.PROBE_CYCLES
                    else:
                        probe[name] -= 1
                        if probe[name] <= 0:
                            probe.pop(name, None)
                else:
                    probe[name] -= 1
                    if probe[name] <= 0:
                        probe.pop(name, None)  # 观察期结束，放弃（留在 seen）
            if ident is not None:
                ident_map[name] = ident
        # seen 中「身份变了」的文件重走 _handle：覆盖下载窗口早已关闭后文件才变
        # 完整（或同名文件被重新下载/替换）的情况。静默重走，不做额外日志；
        # 身份不变者绝不重走（避免逐轮 churn），身份取不到（stat 失败）也不重走
        # （避免死循环）。观察期内的文件已由上面的复查轮负责，这里跳过以免同轮
        # 重复调用。
        for name in list(self.seen[key]):
            if name in probe or name in done_now:
                continue
            fp = watch / name
            ident = self._identity_safe(fp)
            if ident is None:
                continue
            rk = (key, name)
            prev = ident_map.get(name)
            if prev is not None and not self._size_changed(prev, ident):
                # size 未变（可能仅 mtime 变了）→ 绝不重走；仅更新记录的 mtime。
                # churn 状态：出现过大小的文件若已稳定下来（可能下载完成），
                # 消耗一次「恢复机会」复查；否则大小稳定即复位计数。
                st = self._rewalk.get(rk)
                if st is not None and st.get("pending"):
                    st["pending"] = False
                    res = self._handle(fp, wc)
                    if res == "defer":
                        deferred.add(name)
                    elif res == "skip":
                        st["streak"] = self.REWALK_MAX_STREAK
                        st["blocked"] = True
                    else:
                        self._rewalk.pop(rk, None)   # 成功处理：计数复位
                else:
                    self._rewalk.pop(rk, None)       # 大小稳定 → churn 计数复位
                if prev != ident:
                    ident_map[name] = ident
                continue
            # size 变化（含身份首次可读）
            st = self._rewalk.get(rk)
            if st is not None and st.get("blocked"):
                # 已达 churn 硬上限：连续增长不再重走，仅记待稳定；等大小稳定后
                # 由上面的 stable 分支给一次恢复机会。
                st["pending"] = True
                ident_map[name] = ident
                continue
            res = self._handle(fp, wc)
            if res == "defer":
                deferred.add(name)
                self._rewalk.pop(rk, None)           # 交给下轮重查：计数不复用
            elif res == "skip":
                probe[name] = self.PROBE_CYCLES
                if not self._bump_rewalk(rk):
                    probe.pop(name, None)            # 达上限：移出观察期，避免重走
            else:
                self._rewalk.pop(rk, None)           # 成功处理：计数复位
            ident_map[name] = ident
        # 再处理新出现的文件
        for name in sorted(new):
            res = self._handle(watch / name, wc)
            if res == "defer":
                deferred.add(name)
            elif res == "skip":
                # 暂未识别为压缩包：可能正被写入/复制（尾部还没写完），
                # 进入观察期复查几轮，避免一次性吸收后永不处理。
                probe[name] = self.PROBE_CYCLES
                ident = self._identity_safe(watch / name)
                if ident is not None:
                    ident_map[name] = ident
        # 分卷未到齐被推迟的文件不记入 seen，下轮会重新检查
        self.seen[key] = current - deferred
        # 同步身份镜像：补齐 seen 中缺身份的、清掉已离开 seen 的
        for name in list(ident_map):
            if name not in self.seen[key]:
                ident_map.pop(name, None)
                self._rewalk.pop((key, name), None)   # 文件离开 seen：churn 计数复位
        for name in self.seen[key]:
            if name not in ident_map:
                ident = self._identity_safe(watch / name)
                if ident is not None:
                    ident_map[name] = ident

    # ---------- 百度清单模式（Tier-2，实验性；只增强不阻断）----------
    def _bt_sticky_set(self):
        """粘性记忆（toolbox.db），懒加载一次；存放归一化后的路径。"""
        if self._bt_sticky is None:
            s = set()
            try:
                from .. import db as _db
                for row in _db.sticky_list(5000):
                    path, _first, _last, kind, _note = row
                    if str(kind) != "program":   # 程序目录单独标记，不算「我们的文件」
                        s.add(self._norm_path(path))
            except Exception:
                s = set()
            self._bt_sticky = s
        return self._bt_sticky

    def _looks_like_program_dir(self, d, watch_roots=None):
        """目录是否为「疑似程序目录」（避免把程序自带的压缩包解开）。

        保守规则（此前把所有含单个 .exe 的目录都判成程序目录，导致监听根本身
        ——通常混着安装包/脚本——被整个禁掉，凡直接落在监听根的包都被跳过）：
        - **任何被监听目录自身永不算程序目录**：`d` 归一化后与 `watch_roots`
          （各监听根的 norm 路径）任一相等即返回 False（监听根不是「下载子目录」）；
        - 否则，目录内程序文件（`BT_PROGRAM_EXT`）数量达到 `BT_PROGRAM_DIR_MIN`
          个，**且目录内没有正在下载的压缩包**（`.baiduyun.p.downloading` 等，
          由 `is_incomplete_download` 判定）时，才判为程序目录；目录里还有正在下载
          的文件说明这是一个下载目录，放行（宁可偶尔多解一次）。
        绝不抛异常。
        """
        try:
            dn = self._norm_path(d)
        except Exception:
            return False
        if watch_roots:
            try:
                for wr in watch_roots:
                    if dn == wr:
                        return False
            except Exception:
                pass
        try:
            count = 0
            for e in d.iterdir():
                if not e.is_file():
                    continue
                if smart_extract.is_incomplete_download(e):
                    return False            # 有正在下载的压缩包 → 不是程序目录
                if e.suffix.lower() in self.BT_PROGRAM_EXT:
                    count += 1
            return count >= self.BT_PROGRAM_DIR_MIN
        except OSError:
            return False

    def _consolidate_cross_dir_volumes(self, first_fp, gathered):
        """把散落在不同目录的分卷兄弟**移动**到首卷所在目录，拼成一套。

        背景：7-Zip 只在同一目录里找兄弟卷；有的分享把同一套分卷放在不同子目录。
        归拢后交回既有 `_handle`，由 `_volume_ready`（目录局部）/`promote_extracted_
        content` 正常判定与回收——分卷源文件会连同首卷一起进回收站，删除回溯记录
        也准确。同盘时 `shutil.move` 即瞬间改名，几乎零成本。

        先预检全部目标（源存在、目标无同名冲突），再逐个移动；中途失败则**回滚**
        已移动的文件，避免半套。返回 True=已归拢（或无需归拢），False=放弃本套。
        """
        members = gathered.get("members") or []
        anchor_dir = first_fp.parent
        plan = []
        for m in members:
            src = Path(str(m.get("local_path") or ""))
            if not src.name or src.parent == anchor_dir:
                continue
            dest = anchor_dir / src.name
            if dest.exists():
                self.hub.log(f"跨目录分卷归拢放弃（目标已存在同名）: {dest}")
                return False
            if not src.is_file():
                self.hub.log(f"跨目录分卷归拢放弃（源不存在）: {src}")
                return False
            plan.append((src, dest))
        if not plan:
            return True
        done = []
        for src, dest in plan:
            try:
                shutil.move(str(src), str(dest))
                done.append((src, dest))
            except OSError as e:
                self.hub.log(f"跨目录分卷归拢失败: {src} -> {dest}: {e}")
                for s, d in reversed(done):   # 回滚，避免半套
                    try:
                        shutil.move(str(d), str(s))
                    except OSError:
                        pass
                return False
        self.hub.log(
            f"跨目录分卷已归拢到 {anchor_dir}（{len(done)} 个）: {first_fp.name}")
        for d in {s.parent for s, _ in done}:   # 清掉被搬空的来源目录（best-effort）
            try:
                d.rmdir()
            except OSError:
                pass
        return True

    def _baidu_poll(self, watch, wc):
        """按百度网盘任务清单，处理下载到子目录里的压缩包/分卷。

        只在监听路径 mode=='baidu' 时调用。安全铁律：
        - **清单外的路径一律不碰**（这是"只扫表层"之外的唯一扩展，白名单语义）；
        - 只处理「任务已完成(state=done)」或「我们之前确实处理过(粘性记忆)」的路径；
        - 大小需连续稳定 BT_STABLE_SEC 秒、且不是 `.baiduyun.p.downloading`；
        - 疑似程序目录（含 exe/dll/...）只记一条日志、不自动解压；
        - 任何异常都吞掉并回退到表层模式，绝不阻断。
        """
        from .. import baidu_task as bt
        if not bt.is_enabled(self.state):
            return
        try:
            manifest = bt.expected_files()
        except Exception:
            return
        if not manifest:
            return
        root = self._norm_path(watch)
        # 监听根集合（含所有配置的监听路径）：用于「任何被监听目录自身都不算程序
        # 目录」的豁免，避免根目录里的安装包/脚本把整个根禁掉。
        watch_roots = {root}
        try:
            for _wcp in (self.state.snapshot().get("watch_paths") or []):
                _wp = _wcp.get("path")
                if _wp:
                    watch_roots.add(self._norm_path(_wp))
        except Exception:
            pass
        seen = self._bt_seen.setdefault(root, set())
        probe = self._bt_probe.setdefault(root, {})
        sticky = self._bt_sticky_set()
        now = time.time()
        for _batch, items in manifest.items():
            for it in items:
                lp = str(it.get("local_path") or "")
                if not lp or it.get("isdir"):
                    continue
                key = self._norm_path(lp)
                if key in seen:
                    continue
                # 只处理本监听目录内的路径（清单可能含其它监听目录的批次）
                if not key.startswith(root + os.sep):
                    continue
                # 只认「已下载完成」，或「我们之前处理过」的（清历史/重启后的兜底）
                if it.get("state") != "done" and key not in sticky:
                    continue
                p = Path(lp)
                # 隔离区路径绝不在百度清单处理范围内（待还原副本，非下载产物）
                if _in_quarantine(p):
                    continue
                try:
                    if not p.is_file():
                        continue
                except OSError:
                    continue
                try:
                    if smart_extract.is_incomplete_download(p):
                        probe.pop(key, None)
                        continue
                except Exception:
                    pass
                # 大小连续稳定一段时间才动，避免半截文件
                try:
                    size = p.stat().st_size
                except OSError:
                    continue
                prev = probe.get(key)
                if prev is None or prev[0] != size:
                    probe[key] = (size, now)
                    continue
                if now - prev[1] < self.BT_STABLE_SEC:
                    continue
                probe.pop(key, None)
                # 程序目录保护：疑似程序 → 只提示，不自动解压。监听根/被监听目录
                # 自身由 _looks_like_program_dir 豁免；这里再加一道兜底，绝不对
                # 监听根路径写 kind="program" 粘性记忆（避免污染）。
                if self._looks_like_program_dir(p.parent, watch_roots):
                    if key not in self._bt_warned:
                        self._bt_warned.add(key)
                        self.hub.log(f"疑似程序目录，跳过自动解压（百度清单）: {p}")
                        if self._norm_path(p.parent) not in watch_roots:
                            bt.remember_sticky(p, kind="program")
                    seen.add(key)
                    continue
                # 跨目录分卷：同批次同系列的分卷散落在不同子目录时，7-Zip 在单个
                # 目录里找不到兄弟卷。先把它们**归拢**到首卷目录（同盘=瞬间改名），
                # 再交给下面的原目录局部管线；归拢后不再重复（_bt_consolidated）。
                if (key not in self._bt_consolidated
                        and smart_extract.is_volume_name(p.name)
                        and smart_extract.is_first_volume(p.name)):
                    gathered = bt.gather_volume_set(lp, root)
                    if gathered is not None:
                        if not gathered.get("ready"):
                            # 同套分卷还有没下完的：保持稳定态，下轮直查
                            probe[key] = (size, 0.0)
                            continue
                        if not self._consolidate_cross_dir_volumes(p, gathered):
                            # 归拢失败（目标同名冲突等）：放弃本套，避免死循环
                            self._bt_consolidated.add(key)
                            seen.add(key)
                            continue
                        self._bt_consolidated.add(key)
                # 交给原有处理管线（含「等分卷齐全」与「归档内部穿透」）
                res = self._handle(p, wc, initial_scan=True)
                if res == "defer":
                    # 分卷未到齐：不记 seen，但保持「已稳定」状态，
                    # 下一轮直接重试（否则会退化成隔轮才试一次）。
                    probe[key] = (size, 0.0)
                    continue
                seen.add(key)
                sticky.add(key)
                bt.remember_sticky(p)

    # ---------- 翻译 JSON 归位 ----------
    def _translation_check(self, watch):
        """翻译 JSON 自动归位：单 json 小文件夹(<10MB) 的 json 文件名命中某大文件夹名
        （文件名是大文件夹名的子串）时，把 json 移入大文件夹。

        两者出现顺序不定：
        - 大文件夹先到：小文件夹一出现就归位；
        - 小文件夹先到：进入 5 分钟监控窗口，期间大文件夹出现即归位，超时放弃；
          超时放弃后按 json 身份记住，文件变化前不再重试。"""
        cfg = self.state.snapshot()
        if not cfg.get("translation_move_enabled", True):
            return
        try:
            dirs = [d for d in watch.iterdir()
                    if d.is_dir() and not _in_quarantine(d)]
        except OSError:
            return
        candidates = {}
        for d in dirs:
            info = self._translation_candidate(d)
            if info:
                candidates[d] = info
        now = time.time()
        # 先复查监控窗口里的小文件夹（可能刚等到大文件夹）
        for path, (stem, first_seen) in list(self._pending_trans.items()):
            p = Path(path)
            if not p.is_dir():
                self._pending_trans.pop(path, None)
                self._trans_given_up.pop(path, None)
                continue
            if now - first_seen > self.TRANSLATION_WINDOW:
                self.hub.log(f"翻译文件监控超时（5 分钟内未等到目标文件夹），放弃（该文件变化前不再重试）: {p.name}")
                self._pending_trans.pop(path, None)
                info = self._translation_candidate(p)
                self._trans_given_up[path] = (
                    self._file_identity(info[1]) if info else None)
                continue
            target = self._find_translation_target(watch, stem, p)
            if target:
                self._pending_trans.pop(path, None)
                self._move_translation(p, stem, target)
        # 本轮出现的候选：能立刻找到目标就归位，否则进入监控窗口
        for d, (stem, json_file) in candidates.items():
            if not d.is_dir():
                continue
            key = str(d)
            if key in self._trans_given_up:
                old_ident = self._trans_given_up[key]
                if old_ident is not None and self._file_identity(json_file) == old_ident:
                    continue  # 已超时放弃且文件未变化：静默跳过，不重开窗口
                self._trans_given_up.pop(key, None)
            target = self._find_translation_target(watch, stem, d)
            if target:
                self._move_translation(d, stem, target)
            elif key not in self._pending_trans:
                self._pending_trans[key] = (stem, time.time())
                self.hub.log(f"发现翻译文件（等待目标文件夹，5 分钟窗口）: {d.name}\\{stem}.json")

    @staticmethod
    def _translation_candidate(d):
        """翻译文件候选：目录里只有一个 .json 文件且 <10MB。返回 (json_stem, json_path) 或 None。"""
        try:
            files = []
            for x in d.iterdir():
                if x.is_file():
                    files.append(x)
                    if len(files) > 1:
                        return None
            if len(files) != 1:
                return None
            f = files[0]
            if f.suffix.lower() != ".json":
                return None
            if f.stat().st_size > FolderWatcher.TRANSLATION_MAX_SIZE:
                return None
            return (f.stem, f)
        except OSError:
            return None

    @staticmethod
    def _find_translation_target(watch, stem, exclude):
        """在 watch 根下找名字包含 json stem 的文件夹（排除自身，取名字最长者，最具体）。"""
        if not stem:
            return None
        best = None
        try:
            for d in watch.iterdir():
                if not d.is_dir() or d == exclude or _in_quarantine(d):
                    continue
                if stem in d.name:
                    if best is None or len(d.name) > len(best.name):
                        best = d
        except OSError:
            pass
        return best

    def _move_translation(self, src_dir, stem, target_dir):
        """把 json 移入目标文件夹，移除空的小文件夹。"""
        try:
            jf = next(x for x in src_dir.iterdir()
                      if x.is_file() and x.suffix.lower() == ".json")
        except (OSError, StopIteration):
            self._pending_trans.pop(str(src_dir), None)
            self._trans_given_up.pop(str(src_dir), None)
            return
        try:
            dest = target_dir / jf.name
            if dest.exists():
                dest = target_dir / f"{jf.stem}_翻译{jf.suffix}"
            shutil.move(str(jf), str(dest))
            self.hub.log(f"翻译文件归位: {src_dir.name}\\{jf.name} -> {target_dir.name}\\{dest.name}")
        except OSError as e:
            self.hub.log(f"翻译文件归位失败: {e}")
            return
        try:
            src_dir.rmdir()  # json 移走后目录已空，直接删除
        except OSError:
            pass
        self._pending_trans.pop(str(src_dir), None)
        self._trans_given_up.pop(str(src_dir), None)

    # ---------- 首卷分卷观察期 ----------
    def _volume_ready(self, fp):
        """用「编号连续性 + 末卷存在性 + 大小」判断首卷分卷是否到齐。

        分卷命名分两类：
        - 带独立末卷（.zip.001/.7z.001/.z01 风格）：末卷是不带编号的
          `基础名.zip/.7z`。判定：编号连续 且 末卷已出现 → 到齐。
          末卷没出现（哪怕编号连续、大小不一致）→ 未到齐，继续等待。
        - 全部带编号（.partN.rar/.rNN 风格）：用大小判断尾卷——
          编号连续且存在更小尾卷 → 到齐；全是满卷 → 等尾卷。

        日志节流：等待状态下每次轮询都会调用本函数，只在「等待原因/分卷
        集合」发生变化时打一条日志，避免每 2 秒刷屏。
        返回 True=可解压，False=继续等待（由 _handle 返回 defer）。"""
        # 同一文件可能以两种拼写到达：_baidu_poll 用清单里的原始大小写路径，
        # _poll 用监听根的 normcase 路径。统一用 _norm_path 归一键，两条调用
        # 路径共享同一份等待状态（否则同一等待原因会被打两遍日志）。
        abs_fp = self._norm_path(fp)
        state = self._vol_wait.setdefault(
            abs_fp, {"last_sig": None, "last_change": time.time(), "last_log": None})
        now = time.time()
        if not fp.exists():
            self._vol_wait.pop(abs_fp, None)
            return True
        name = fp.name
        final_name = smart_extract._volume_final_name(name)
        try:
            vols = {}      # {卷号: 大小}（不含末卷）
            final_exists = False
            has_downloading = False
            for e in fp.parent.iterdir():
                if not e.is_file():
                    continue
                if e.name == name or smart_extract.is_volume_file(name, e.name, fp.stem):
                    if smart_extract.is_incomplete_download(e):
                        has_downloading = True
                        continue
                    if final_name and e.name == final_name:
                        final_exists = True
                        continue
                    num = smart_extract._volume_number(e.name)
                    if num is not None:
                        try:
                            vols[num] = e.stat().st_size
                        except OSError:
                            pass
        except OSError:
            return False

        def _wlog(key, msg):
            """状态变化才打日志（key=等待原因+分卷集合签名）"""
            if state.get("last_log") != key:
                state["last_log"] = key
                self.hub.log(msg)

        # 实验性：网盘任务库辅助判断——**只加速、不阻断**。当任务清单显示该分卷组
        # 已全部「下载完成」且文件都在磁盘上时，直接判定到齐，做到「下载完成即触发
        # 解压」；拿不到信息（未开启实验性/未被跟踪）就回退下面的磁盘启发式。
        try:
            from .. import baidu_task as _baidu_task
            if _baidu_task.volume_hint(str(fp)) is True:
                state["last_sig"] = None
                _wlog(("db-ready", name),
                      f"分卷已到齐（依据网盘任务清单）: {name}")
                return True
        except Exception:
            pass

        if has_downloading:
            # 仍有分卷在下载：未到齐
            state["last_sig"] = None
            state["last_change"] = now
            _wlog(("downloading", name),
                  f"首卷已出现，其他分卷仍在下载，等待到齐: {name}")
            return False
        if not vols:
            return False
        # 编号必须连续（从 1 到最大值无缺口）；有缺口说明中间卷未到齐
        max_num = max(vols)
        contiguous = (len(vols) == max_num
                      and set(vols) == set(range(1, max_num + 1)))
        if not contiguous:
            state["last_sig"] = None
            state["last_change"] = now
            _wlog(("gap", name, tuple(sorted(vols))),
                  f"分卷编号不连续（中间缺卷），等待补齐: {name}"
                  f"（已到 {sorted(vols)}）")
            return False
        if final_name is not None:
            # 带独立末卷的风格（.zip.001/.7z.001/.z01）。末卷判定：
            # 1) 不带编号的末卷（xxx.zip/xxx.7z）已出现 → 到齐；
            # 2) 最后一个编号分卷末尾含 zip EOCD → 它本身就是末卷（上传者
            #    可能把末卷也命名成 .002），→ 到齐。
            # 只有以上都不满足才继续等待。
            if not final_exists:
                last_part = None
                for e in fp.parent.iterdir():
                    if (e.is_file()
                            and not smart_extract.is_incomplete_download(e)
                            and smart_extract._volume_number(e.name) == max_num):
                        last_part = e
                        break
                if last_part is not None and smart_extract.has_zip_eocd(last_part):
                    self._vol_wait.pop(abs_fp, None)
                    self.hub.log(f"分卷已到齐（末卷为编号分卷，共 {len(vols)} 个分卷）: {name}")
                    return True
                # 既无独立末卷、也不是 zip EOCD：可能是 7-Zip 分卷
                # （xxx.7z.001+xxx.7z.002...，真正的末卷就是最高编号的分卷本身，
                # 不存在单独的 xxx.7z）。此时退化为「大小尾卷」判断：
                # 编号连续且存在更小的尾卷 → 已到齐。解压若仍缺卷（Missing
                # volume）会由 _handle 兜底回退重试，不会误判成成功。
                sig = tuple(sorted(vols.values()))
                if len(sig) >= 2 and len(set(sig)) > 1:
                    self._vol_wait.pop(abs_fp, None)
                    self.hub.log(f"分卷已到齐（含尾卷，共 {len(vols)} 个编号分卷）: {name}")
                    return True
                # 空闲制：只有分卷集合/大小真的变化时才重置计时。原实现每轮无条件
                # 重置 last_change，使下面的 300s 兜底永远不可达（洞2）。
                if state["last_sig"] != sig:
                    state["last_sig"] = sig
                    state["last_change"] = now
                    _wlog(("no-final", name, tuple(sorted(vols))),
                          f"首卷已出现，分卷未到齐（缺末卷 {final_name}）: {name}"
                          f"（已到编号分卷 {sorted(vols)}）")
                # 洞2：末卷缺失时同样进入「跨名链探测 + 有界兜底」共享逻辑，
                # 使改名上传的兄弟卷会被探测、300s 兜底也能到点触发。
                return self._pair_probe_or_timeout(fp, abs_fp, state, name, now)
            self._vol_wait.pop(abs_fp, None)
            self.hub.log(f"分卷已到齐（含末卷，共 {len(vols)} 个编号分卷）: {name}")
            return True
        # .partN.rar/.rNN 风格：全部带编号，用大小判断尾卷
        sig = tuple(sorted(vols.values()))
        if len(sig) >= 2 and len(set(sig)) > 1:
            # 编号连续 + 存在大小不一致（有更小尾卷）→ 已到齐
            self._vol_wait.pop(abs_fp, None)
            self.hub.log(f"分卷已到齐（含尾卷，共 {len(vols)} 个分卷）: {name}")
            return True
        # 编号连续但全是满卷 → 尾卷未到
        if state["last_sig"] != sig:
            state["last_sig"] = sig
            state["last_change"] = now
            _wlog(("all-full", name, sig),
                  f"首卷已出现，分卷未到齐（等待更小的尾卷）: {name}"
                  f"（已到 {len(vols)} 个满卷）")
        # 跨名分卷链配对 + 目录活跃度 + 有界兜底放行（与「缺独立末卷」分支共用）
        return self._pair_probe_or_timeout(fp, abs_fp, state, name, now)

    # ---------- 分卷等待共用出口：跨名探测 + 目录活跃 + 有界兜底 ----------
    def _pair_probe_or_timeout(self, fp, abs_fp, state, name, now):
        """「全满卷等待尾卷」与「缺独立末卷（xxx.7z.001 风格）」两个等待分支共用的收尾。

        顺序：跨名分卷链探测 → 目录活跃度重置空闲计时 → 有界兜底放行。
        返回 True=放行尝试解压，False=本轮继续等待。绝不重复实现兜底逻辑。
        """
        # 跨名分卷链配对：同目录里可能有被改名上传的兄弟分卷（可能不止一卷），
        # 属于基础解压逻辑的一部分、强制开启。唯一链 + 7z 验证通过即改名配对；
        # 失败/模糊一律只提示不动手，绝不改变既有等待语义。
        if self._pair_split_check(fp, state):
            return False
        # 目录仍在活动（有文件在下载，或与上一轮快照相比出现新文件/大小增长）→
        # 视为「还在到齐路上」，重置空闲计时，绝不在此期间强解。与文件名无关：
        # 改名上传的兄弟卷正是这样被看见的。连续活动超上限后不再压制兜底。
        if self._pair_dir_active(fp, state, now):
            state["last_change"] = now
        # 截断分卷链的有界重试：按已尝试次数提升本次退避阈值（300→900→1800s）；
        # 已放弃或达上限则不再放行（继续等新分卷，源文件与分卷一律保留）。
        # 源文件身份或源目录内容变化 → _split_retry_gate 清计数重新武装。
        _retry = self._split_retry_gate(fp, abs_fp)
        if _retry is not None:
            if (_retry.get("given_up")
                    or _retry.get("count", 0) >= self.SPLIT_INCOMPLETE_MAX):
                return False
            _wait = self.SPLIT_INCOMPLETE_DELAYS[
                min(_retry.get("count", 0),
                    len(self.SPLIT_INCOMPLETE_DELAYS) - 1)]
        else:
            _wait = self.VOL_MAX_WAIT
        # 已检测到改名链但验证未通过：300s 不能强解截断链，改为有界延长到
        # PAIR_WAIT_EXTEND_SEC，到点再按现有分卷尝试（只提示，绝不删除源文件）。
        if state.get("pair_chain_pending"):
            if now - state["last_change"] >= self.PAIR_WAIT_EXTEND_SEC:
                self._vol_wait.pop(abs_fp, None)
                self.hub.log(f"仍有缺失分卷，按现有分卷尝试（不会删除源文件）: {name}")
                return True
            return False
        if now - state["last_change"] >= _wait:
            # 长时间无变化 → 兜底强制放行（覆盖恰好整倍数/下载中断的罕见情况）；
            # 截断链重试时 _wait 按退避升级（300→900→1800s）。
            self._vol_wait.pop(abs_fp, None)
            self.hub.log(f"分卷等待超时（{_wait}s），按现有分卷尝试解压: {name}")
            return True
        return False

    def _pair_dir_active(self, fp, state, now):
        """同目录是否仍在活动（与文件名无关），供分卷等待重置空闲计时。

        活动 = 目录里有任何未完成下载文件，或与上一轮快照相比出现新文件/大小变化
        （包含改名上传的兄弟卷到达、任何文件增长）。快照存 state["dir_snapshot"]。
        连续活动超过 PAIR_DIR_ACTIVE_MAX_SEC 后返回 False（不再压制兜底），并只在
        该次连续活动首次达上限时如实打一条日志——无限增长的不相关文件不能永远延期。
        注意：这不替代既有的三个重置源（同组分卷下载中/编号缺口/大小签名变化），
        只是在其之外补上「与文件名无关」的一条。"""
        try:
            cur = {}
            incomplete = False
            for e in Path(fp).parent.iterdir():
                try:
                    if not e.is_file():
                        continue
                    cur[e.name] = e.stat().st_size
                    if smart_extract.is_incomplete_download(e):
                        incomplete = True
                except OSError:
                    continue
        except OSError:
            return False
        prev = state.get("dir_snapshot")
        # 只看「出现新文件 / 大小变化（增长）」：纯删除不算活动（下载器清理临时文件
        # 或分卷被改名归位时都会删除旧名，不该因此误判为「仍在到齐」）。
        changed = False
        if prev is not None:
            for ename, esize in cur.items():
                if prev.get(ename) != esize:
                    changed = True
                    break
        state["dir_snapshot"] = cur
        if not (incomplete or changed):
            state["dir_active_since"] = None
            return False
        since = state.get("dir_active_since")
        if since is None:
            since = now
            state["dir_active_since"] = since
        if now - since >= PAIR_DIR_ACTIVE_MAX_SEC:
            if state.get("dir_active_capped") != since:
                state["dir_active_capped"] = since
                self.hub.log(
                    f"目录持续变化已超 {PAIR_DIR_ACTIVE_MAX_SEC}s，"
                    f"不再延长分卷等待，按现有分卷尝试: {Path(fp).name}")
            return False
        return True

    # ---------- 截断分卷链的有界重试（同 _lock_retry 精神） ----------
    def _dir_sig(self, fp):
        """源文件所在目录的内容签名（名+大小），用于「新卷到达即重新武装重试」。

        只取顶层普通文件；单项 stat 失败跳过；目录不可读返回空元组（视为无变化）。"""
        try:
            out = []
            for e in Path(fp).parent.iterdir():
                try:
                    if e.is_file():
                        out.append((e.name, e.stat().st_size))
                except OSError:
                    continue
            return tuple(sorted(out))
        except OSError:
            return ()

    def _split_retry_gate(self, fp, abs_fp):
        """分卷链不完整的有界重试闸门。

        返回重试状态 dict（含 count/given_up），无记录返回 None。源文件身份
        （size/mtime）或源目录内容变化 → 清记录（重新武装，退避从头算起，新到的
        分卷因此立即重新触发重试）。无记录时只做一次字典查找，健康路径零开销。"""
        st = self._split_incomplete_retry.get(abs_fp)
        if st is None:
            return None
        ident = self._file_identity(fp)
        if (not self._is_same_traced(st.get("ident"), ident)
                or st.get("dir_sig") != self._dir_sig(fp)):
            self._split_incomplete_retry.pop(abs_fp, None)
            self.hub.log(
                f"源文件或所在目录有变化，重新武装分卷重试（计数清零）: {fp.name}")
            return None
        return st

    def _note_split_incomplete(self, fp, abs_fp, ident):
        """记录一次「分卷链不完整」尝试，返回累计次数（含本次）。

        身份或源目录内容变化 → 从 0 重新计数（重新武装）。只增计数，不做放行决策；
        放行/放弃由 _pair_probe_or_timeout 与 _handle 依据计数判定。"""
        sig = self._dir_sig(fp)
        st = self._split_incomplete_retry.get(abs_fp)
        if (st is None
                or not self._is_same_traced(st.get("ident"), ident)
                or st.get("dir_sig") != sig):
            st = {"ident": ident, "dir_sig": sig, "count": 0, "given_up": False}
        st["count"] += 1
        st["given_up"] = False
        self._split_incomplete_retry[abs_fp] = st
        return st["count"]

    # ---------- 跨名分卷配对（同目录·疑似改名上传的兄弟尾卷） ----------
    def _pair_split_check(self, fp, state):
        """跨名分卷配对检查的对外入口：任何异常都吞成 False，绝不影响等待逻辑。"""
        try:
            return self._pair_split_check_impl(fp, state)
        except Exception:
            return False

    def pair_split_for_drop(self, fp):
        """拖放场景的跨名分卷链配对入口（与监听路径共用 _pair_split_check 同一套实现）。

        拖放是一次性动作、没有「下一轮重扫」，故显式传一次性 state：既不读取也不写入
        监听路径对每个文件的等待/验证记忆，绝不污染监听状态。任何异常一律吞掉并返回
        False，绝不影响拖放解压流程。返回 True = 验证通过并已把改名兄弟卷改成首卷系列名。
        """
        try:
            return self._pair_split_check(fp, {})
        except Exception:
            return False

    def _pair_split_check_impl(self, fp, state):
        """「全满卷等待尾卷」时的跨名分卷链检查（实现）。

        基础解压逻辑的一部分、强制开启：唯一链 + 7z 验证 verified → 立即把整条链
        改名成首卷系列（无任何 auto 开关参与，只保留 pair_split_enabled 唯一总闸）。
        断号/模糊/证据不足/未验证一律只提示、零动作；任何异常都吞掉，绝不影响等待逻辑。
        返回 True = 已改名（调用方本轮 defer：下一轮重扫，现有管线自行接手）。
        """
        try:
            cfg = self.state.snapshot()
        except Exception:
            return False
        if not cfg.get("pair_split_enabled", True):
            return False
        try:
            info, reason = volume_pair.best_chain(fp)
        except Exception:
            return False
        first = Path(fp)
        if info is None:
            # 没有可用链：断号/模糊只提示一次；no_candidates 静默（普通等待）
            state["pair_chain_pending"] = False
            try:
                self._pair_split_notice(first, reason, state)
            except Exception:
                pass
            return False
        vols = info.get("volumes") or []
        if not vols:
            return False
        vol_paths = [Path(v.get("path")) for v in vols]
        chain_paths = [first] + vol_paths
        chaintxt = " + ".join(p.name for p in chain_paths)
        labels = []
        for key, label in (("number_adjacent", "编号相邻"),
                           ("first_has_signature", "首卷有包头"),
                           ("cand_lacks_signature", "续卷无包头"),
                           ("size_fits_split", "尺寸相符"),
                           ("same_dir", "同目录")):
            if any((v.get("evidence") or {}).get(key) for v in vols):
                labels.append(label)
        # 验证去重键覆盖整条链：路径 + 尺寸 + mtime + 口令数；只有身份变化才重验证
        passwords = self._pair_split_passwords()
        vkey = (tuple((self._norm_path(p), self._file_identity(p))
                      for p in chain_paths), len(passwords))
        if state.get("pair_verify_key") == vkey:
            vstate = state.get("pair_verify_state", "")
            detail = state.get("pair_verify_detail", "")
        else:
            state["pair_verify_key"] = vkey
            vstate, detail = self._pair_split_verify(first, vol_paths, passwords)
            state["pair_verify_state"] = vstate
            state["pair_verify_detail"] = detail
            self.hub.log(f"跨名分卷配对验证: {chaintxt} → {vstate}（{detail}）")
        info_txt = f"链已到 {len(vol_paths) + 1} 卷"
        if info.get("missing"):
            info_txt += f"，缺第 {info['missing']} 卷"
        verified_txt = vstate or "未验证"
        hint = (f"疑似同一压缩包被改名上传（{info_txt}）: {chaintxt}"
                f"（证据：{'/'.join(labels) if labels else '结构信号'}；"
                f"已验证={verified_txt}）")
        hint_key = (tuple(p.name for p in chain_paths), vstate)
        if state.get("pair_hint_key") != hint_key:
            state["pair_hint_key"] = hint_key
            self.hub.log(hint)
            self.hub.notify("疑似改名分卷", hint)
        if vstate != volume_pair.STATE_VERIFIED:
            state["pair_chain_pending"] = True
            return False
        # 验证通过 → 强制改名整条链：先查全部目标名，任一已存在则整链零动作
        targets = []
        for v, p in zip(vols, vol_paths):
            tname = volume_pair.sibling_target_name(first, v.get("number"))
            if not tname:
                self.hub.log(f"跨名分卷配对已通过验证，但无法确定改名目标名: {p.name}")
                state["pair_chain_pending"] = False
                return False
            targets.append(p.with_name(tname))
        for dest in targets:
            if dest.exists():
                self.hub.log(f"跨名分卷配对整链零动作（目标已存在）: {dest}")
                state["pair_chain_pending"] = False
                return False
        renamed, failed = [], []
        for p, dest in zip(vol_paths, targets):
            try:
                old = Path(p)
                old.rename(dest)
                renamed.append((old, dest))
            except OSError as e:
                failed.append(p)
                self.hub.log(f"跨名分卷配对改名失败（继续其余，下一轮可自愈）: "
                             f"{p.name} → {dest.name}: {e}")
        for old, dest in renamed:
            # 改名是唯一被允许的“动作”，且必须可发现/可逆：日志同时给旧、新绝对路径
            self.hub.log(f"跨名分卷配对已改名（可手动改回）: {old} → {dest}")
        if not renamed:
            # 全部失败：保持延长等待，留待下一轮重试（绝不放弃到强解）
            state["pair_chain_pending"] = True
            return False
        state["pair_chain_pending"] = False
        self._pair_split_forget(first, vol_paths, [d for _old, d in renamed])
        return True

    def _pair_split_notice(self, first, reason, state):
        """没有可用链时按原因给一条一次性提示（按原因去重）；no_candidates 静默。

        断号/模糊/证据不足/出错一律零动作，只写日志（绝不 notify、绝不改文件）。"""
        if not reason or reason == "no_candidates":
            return
        if reason.startswith("gap:"):
            miss = reason.split(":", 1)[1]
            msg = (f"疑似同一压缩包被改名上传（链已到 1 卷，缺第 {miss} 卷）: "
                   f"{first.name}（证据：结构信号；已验证=未验证）")
        elif reason.startswith("ambiguous:"):
            n = reason.split(":", 1)[1]
            msg = (f"疑似同一压缩包被改名上传（第 {n} 卷处有多个候选，无法决断）: "
                   f"{first.name}（原因：ambiguous:{n}）")
        elif reason == "weak_evidence":
            msg = (f"疑似同一压缩包被改名上传，但结构证据不足，暂不动作: {first.name}"
                   f"（原因：weak_evidence）")
        else:
            msg = f"跨名分卷链配对未决: {first.name}（原因：{reason}）"
        key = (first.name, reason)
        if state.get("pair_notice_key") != key:
            state["pair_notice_key"] = key
            self.hub.log(msg)

    @staticmethod
    def _pair_split_mtime(path):
        try:
            return Path(path).stat().st_mtime
        except OSError:
            return None

    def _pair_split_passwords(self):
        """候选口令：密码本 + 本次临时密码 + 字典口令（去重，封顶 64 次尝试）。"""
        out = []
        try:
            for p in self.state.all_passwords() or []:
                s = str(p)
                if s and s not in out:
                    out.append(s)
        except Exception:
            pass
        try:
            for p in smart_extract.get_dict_passwords() or []:
                s = str(p)
                if s and s not in out:
                    out.append(s)
        except Exception:
            pass
        return out[:volume_pair.MAX_PASSWORD_ATTEMPTS]

    def _pair_split_verify(self, first_fp, volume_paths, passwords):
        """硬链接装配 + 7z 验证整条链（独立方法，便于测试替换；绝不抛异常）。"""
        try:
            return volume_pair.verify(
                first_fp, list(volume_paths or []), passwords=passwords)
        except Exception as e:
            return volume_pair.STATE_INCONCLUSIVE, f"验证出错: {e}"

    @staticmethod
    def _pair_split_flatten(paths):
        """把 Path / Path 列表 统一摊平成 [Path...]（None 安全）。"""
        out = []
        for p in (paths if isinstance(paths, (list, tuple, set)) else [paths]):
            try:
                if p is not None:
                    out.append(Path(p))
            except Exception:
                continue
        return out

    def _pair_split_forget(self, first_fp, old_paths, new_paths):
        """改名成功后清掉整条链的监听痕迹：下一轮重扫即重新识别完整分卷组。"""
        try:
            paths = self._pair_split_flatten(first_fp)
            paths += self._pair_split_flatten(old_paths)
            paths += self._pair_split_flatten(new_paths)
            for p in paths:
                self.traced.pop(self._norm_path(p), None)
                self._vol_wait.pop(self._norm_path(p), None)
            names = {p.name for p in paths}
            parent_key = self._norm_path(Path(first_fp).parent)
            for key in list(self.seen):
                try:
                    if not (parent_key == key
                            or parent_key.startswith(key + os.sep)):
                        continue   # 只清包含该文件的监听目录，不误伤同名兄弟
                except Exception:
                    continue
                try:
                    self.seen[key].difference_update(names)
                except Exception:
                    pass
                ident_map = self._seen_ident.get(key)
                if isinstance(ident_map, dict):
                    for n in names:
                        ident_map.pop(n, None)
                probe = self.probing.get(key)
                if isinstance(probe, dict):
                    for n in names:
                        probe.pop(n, None)
                for n in names:
                    self._rewalk.pop((key, n), None)
        except Exception:
            pass

    @staticmethod
    def _free_space_gb(path):
        """目标路径所在卷的剩余空间（GB）；取不到（路径无效/卷不可读）返回 None。

        只读探测，绝不创建/改动任何文件；任何异常都返回 None，由调用方按
        「放行」处理——读不到空间绝不反过来卡住解压。
        """
        try:
            return shutil.disk_usage(str(path)).free / (1024 ** 3)
        except Exception:
            return None

    def _disk_space_ok(self, fp, wc):
        """P1-14（选项 D）磁盘空间闸门：目标卷剩余空间足够 → True（放行）。

        目标卷 = 监听配置的 output_dir（非空时）所在盘；未配置 output_dir 时，
        解压结果会抬升到**源文件所在目录**旁（见 _handle 的 promote_to），故用
        源文件所在盘。阈值 min_free_space_gb 每轮从实时配置读取（缺省 5.0，
        0 = 关闭闸门），改配置无需重启。空间不足时按监听目录只记一次日志 /
        发一次通知，目录状态置为既有的 waiting；空间恢复后记一次「恢复」并
        重新武装下一次提醒。任何异常一律放行：绝不因读不到空间而卡住解压。
        """
        try:
            cfg = self.state.snapshot() or {}
            try:
                limit_gb = float(cfg.get("min_free_space_gb", 5.0))
            except (TypeError, ValueError):
                limit_gb = 5.0
            if limit_gb <= 0:
                return True                     # 0（或负数）= 关闭闸门
            key = self._norm_path(wc.get("path"))
            target = str(wc.get("output_dir") or "").strip()
            if not target:
                target = str(Path(fp).parent)   # 无 output_dir：结果在源文件旁
            free_gb = self._free_space_gb(target)
            if free_gb is None:
                return True                     # 读不到空间：放行（fail open）
            if free_gb >= limit_gb:
                if key in self._disk_warned:
                    # 空间恢复：只记一次「恢复」，并重新武装下一次「空间不足」
                    self._disk_warned.discard(key)
                    self.hub.log(
                        f"磁盘空间已恢复（剩余 {free_gb:.1f}GB），恢复自动解压")
                    self._set_dir_state(wc.get("path"), "listening")
                return True
            if key not in self._disk_warned:
                self._disk_warned.add(key)
                self.hub.log(
                    f"磁盘空间不足（剩余 {free_gb:.1f}GB < 阈值 {limit_gb:.1f}GB），"
                    f"已暂停自动解压，等待空间释放: {wc.get('path')}")
                self.hub.notify(
                    "磁盘空间不足",
                    f"{wc.get('path')} 所在卷剩余空间 {free_gb:.1f}GB，低于阈值 "
                    f"{limit_gb:.1f}GB，已暂停自动解压；空间恢复后自动继续")
            self._set_dir_state(wc.get("path"), "waiting")
            return False
        except Exception:
            return True

    def _handle(self, fp, wc, traced=False, initial_scan=False, out=None):
        """返回: "done"=已处理/已定型, "skip"=当前不是压缩包(可复查), "defer"=稍后重试"""
        name = fp.name
        # 用户暂停：延后所有解压（静默 defer，不刷日志），恢复后下轮自然继续。
        # 放在最前面，暂停期间连"下载未完成"等提示也不发。
        if self.pauser is not None and self.pauser.is_paused():
            return "defer"
        # P1-14（选项 D）磁盘空间闸门：目标卷剩余空间低于配置阈值时暂停所有自动
        # 解压。与暂停同一 defer 语义（文件不进 seen，下一轮重试）；空间恢复后
        # 自动继续，不打断下方任何既有闸门。
        if not self._disk_space_ok(fp, wc):
            return "defer"
        if smart_extract.is_incomplete_download(fp):
            self.hub.log(f"下载未完成，暂不解压（等待后缀消失）: {name}")
            self._set_dir_state(wc.get("path"), "waiting", name=name)
            return "done"
        if smart_extract.is_do_not_extract(name):
            self.hub.log(f"移动安装包，保持原样不自动解压: {name}")
            return "done"
        if not self._is_archive(fp):
            return "skip"
        # 假分卷名的完整压缩包（改后缀迷惑，如 .z11 其实是完整 zip）：
        # 跳过分卷逻辑，按完整压缩包处理（extract_one 里会规范化后缀）
        if smart_extract.is_fake_volume_name(fp):
            self.hub.log(f"文件名像分卷但内容是完整压缩包（改后缀迷惑）: {name}")
        # 文件可能仍在被下载器写入：百度网盘/IDM 多线程下载合并碎片时
        # 直接独占写入目标文件（无 .downloading 后缀），此时 7-Zip 打不开
        #（报"另一个程序正在使用此文件"）。尝试以追加写模式打开，失败即
        # 视为仍在写入，defer 等下一轮。独立 if 返回，不打断下方 elif 链。
        if not _can_open_append(fp):
            self.hub.log(f"文件仍被占用（可能正在写入/合并碎片），暂不解压: {name}")
            self._set_dir_state(wc.get("path"), "waiting", name=name)
            return "defer"
        # 非首卷分卷（.part2.rar / .002 / .z02 / .r01 等）不是解压入口，
        # 单独交给 7-Zip 必然失败；等首卷出现时统一处理整个分卷。
        elif smart_extract.is_non_first_volume(name):
            self.hub.log(f"非首卷分卷，等待首卷处理整个分卷: {name}")
            return "done"
        # 还原豁免：该源文件是用户从删除回溯里**还原**回来的（登记了 路径+身份）。
        # 用户还原的意图就是完整保留这份源文件，所以即使它又出现在监听目录里也跳过
        # 解压——否则解压成功后又会被 delete_policy 删掉（「还原后立刻又被解压、再被
        # 删」的根因）。身份变化（重新下载/替换）后自动失效；**与 initial_scan 无关**：
        # 运行中还原走的是常规轮询，也必须生效。
        elif _restored_exempt(fp):
            self.hub.log(f"该源文件是从删除回溯还原的，豁免再次解压，跳过: {name}")
            return "done"
        # 监听（重）初始化扫描时，跳过已成功处理过的文件（避免重复解压）
        elif initial_scan and deletion_trail.already_handled(fp):
            self.hub.log(f"已处理过的文件，跳过: {name}")
            return "done"
        # 分卷下载分批到齐：.001 先下完、其他分卷还在下载时不能开始解压，
        # 否则 7-Zip 报 Unexpected end of archive。未到齐则跳过本轮，
        # 由 _poll 下一轮重新检查（保持文件不在 seen 中）。
        # 首卷分卷用「大小」判断到齐：下载器按固定大小切分，前面的分卷
        # (part1..N-1) 大小一致，最后一个尾卷通常更小。只要还没出现更小的
        # 尾卷，就继续等待。
        elif smart_extract.is_volume_name(name) and smart_extract.is_first_volume(name):
            if not self._volume_ready(fp):
                self._set_dir_state(wc.get("path"), "waiting", name=name)
                return "defer"
        elif smart_extract.volume_download_pending(fp):
            self.hub.log(f"分卷未到齐（其他分卷仍在下载），暂不解压，等待下载完成: {name}")
            self._set_dir_state(wc.get("path"), "waiting", name=name)
            return "defer"
        # 相同/嵌套监听路径下，防止同一个压缩包被重复处理；
        # 用「大小+时间」识别身份：同名新文件（内容不同）不会被跳过
        abs_fp = self._norm_path(fp)
        ident = self._file_identity(fp)
        # 跨目录分卷等待期：该源文件上一轮已解过（原因是缺兄弟卷），不重复解压，
        # 只复查兄弟卷是否已出现在监听范围内的其他目录（省去每次重解 GB 级伪装包）。
        st = self._split_pending.get(abs_fp)
        if st is not None:
            if self._is_same_traced(st.get("ident"), ident):
                return self._split_recheck(fp, wc)
            self._split_pending.pop(abs_fp, None)   # 文件变了（重新下载等）：走正常流程
        if self._is_same_traced(self.traced.get(abs_fp), ident):
            self._traced_touch(abs_fp)   # LRU：命中即刷新，避免活跃条目被淘汰
            return "done"
        self._traced_put(abs_fp, ident)
        # 只给最初始源文件建立删除回溯记录（多层解压产生的次级中间文件不标记）
        record = None
        if not traced:
            record = deletion_trail.new_record(fp, wc.get("path"))
            deletion_trail.add_record(record)
        self.hub.notify("发现压缩包", f"{name}\n开始智能解压...")
        # 提前置 0：建任务行之前若抛异常，终态更新按「无任务」跳过（避免 UnboundLocalError）
        tid = 0
        try:
            try:
                engine = smart_extract.create_engine("auto")
            except BaseException as e:
                try:
                    engine = smart_extract.create_engine("zip")
                except BaseException:
                    self.hub.log(f"{name} 无法初始化解压引擎: {e}")
                    self.hub.notify("智能解压失败", f"{name}\n7-Zip 不可用: {e}")
                    return "done"
            passwords = self.state.all_passwords()
            out_dir = (wc.get("output_dir") or "").strip() or None
            # 默认抬升到**源文件所在目录**（而不是监听根）。表层模式下源文件就在
            # 根，二者等价；百度清单模式会处理下载到**子目录**里的包，若一律抬升到
            # 监听根，就会破坏下载时的目录结构、并在原位留下空文件夹。
            promote_to = out_dir or str(fp.parent)
            options = {
                "enable_nested": True,
                "max_depth": 10,
                "max_size_ratio": 100.0,
                "use_dict": False,
                "default_password": None,
                "mode": "direct",
            }
            args = types.SimpleNamespace(
                move_to=None,
                delete_source=bool(wc.get("delete_source")),
                # 源文件删除策略（auto/permanent/keep/quarantine）：随监听目录条目下传，
                # 决定回收站不可用时是否永久删除源文件（见 extract.delete_source）。
                delete_policy=str(wc.get("delete_policy") or "auto"),
                # 仅 quarantine 策略启用隔离区；auto/permanent/keep 保持原语义。
                # 空 output_dir 由引擎按「源文件所在目录」补 _已删除。
                quarantine_root=(str(wc.get("output_dir") or "")
                                 if (str(wc.get("delete_policy") or "").strip().lower()
                                     == "quarantine")
                                 else None),
                run_script=None, script_args=[],
                promote_to=promote_to,
                promote_merge=get_bool(self.state.snapshot(), "promote_merge", True),
            )
            if record is not None:
                args.delete_hook = (
                    lambda recycled, failed, quarantine_map, rid=record["id"]:
                    deletion_trail.mark_deleted(rid, recycled, failed, quarantine_map))
                # 删除前预写「正在删除…」意图记录：删除动作先于结果落盘的崩溃窗口里，
                # 回溯记录不再是永远停在「已记录（处理中）」。
                args.pre_hook = (
                    lambda targets, rid=record["id"]:
                    _mark_deleting(rid, targets))
            self.hub.q.put({"type": "progress_start"})
            self._set_dir_state(wc.get("path"), "extracting", 0, name)
            # 任务行只在「所有 defer 闸门都过、真正要解压」时创建；重试路径先复用同目录
            # 同名的未完成任务行，避免每次重试都新建行、把队列堆满死行。
            tid = db.find_open_task(wc.get("path"), name) or db.add_task(
                file_name=name, file_size=(ident[0] if ident else None),
                source_dir=wc.get("path"), output_dir=out_dir,
                mode=str(wc.get("mode") or "surface"), state="queued")
            _tasks_changed(self.hub)
            db.update_task_state(tid, "extracting", started_at=int(time.time()))
            _tasks_changed(self.hub)
            # 线程级日志上下文：从解压一直保持到终态处理结束（见外层 finally 清除），
            # 这样「该任务日志」才包含完成 / 失败 / 差错等收尾行。
            self.hub.set_log_context(source_dir=wc.get("path"), task_id=tid)
            try:
                result = smart_extract.extract_one(
                    engine, str(fp), out_dir, passwords, options, args,
                    progress_cb=self._progress_cb, pauser=self.pauser)
            finally:
                self.hub.q.put({"type": "progress_done"})
            if out is not None:
                out["result"] = result     # 供 _split_recheck 读锚点重试的结果
            if (result and result["success"]
                    and not (result.get("incomplete") or result.get("failed_layers")
                             or result.get("split_incomplete"))):
                self._lock_retry.pop(abs_fp, None)
                self._split_incomplete_retry.pop(abs_fp, None)
                db.update_task_state(tid, "done", finished_at=int(time.time()),
                                     output_dir=(result.get("promoted_dir") or out_dir))
                _tasks_changed(self.hub)
                self._set_dir_state(wc.get("path"), "listening", name=name)
                msg = f"{name} 完成，穿透 {result['depth_reached']} 层，共 {len(result['extracted_files'])} 个文件"
                self.hub.log(msg)
                self.hub.notify("智能解压完成", msg)
                # 未删除源文件（delete_source=False）时标记 kept：promote 路径下
                # 只有删除开关为真才回收源码，故此分支现可正常到达。
                if record is not None and not args.delete_source:
                    deletion_trail.mark_kept(record["id"])
                # 只追溯本次解压产生的压缩包文件，不监听其他文件
                trace_targets = ([result["promoted_dir"]] if result.get("promoted_dir")
                                 else result["extracted_files"])
                self._trace_produced(trace_targets, wc)
            else:
                err = (result or {}).get("error") or "未知错误"
                # 分卷未到齐/链不完整（首卷拼出的载荷不是有效归档、缺兄弟分卷）：
                # 不是终态失败——源文件与分卷原样保留，撤销记录与追踪，任务回
                # 等待态，等分卷补齐后重试（保持既有 queued 重试语义）。
                if (result or {}).get("split_incomplete"):
                    # 截断分卷链的有界重试：最多 SPLIT_INCOMPLETE_MAX 次（退避
                    # 300→900→1800s，见 _pair_probe_or_timeout）。到顶放弃自动重试
                    # 并如实告知一次；源文件与分卷原样保留（绝不回收/删除）。
                    _cnt = self._note_split_incomplete(fp, abs_fp, ident)
                    if _cnt >= self.SPLIT_INCOMPLETE_MAX:
                        # 到顶放弃自动重试：任务行必须落**终态**（failed，可重试），
                        # 绝不留永远 queued 的僵尸行（见 db.py tasks 设计口径）。
                        # 源文件与分卷一律原样保留；如实告知一次（日志 + 通知各一条）。
                        # 返回 "defer" 不变：文件继续留在观察范围，新分卷到达时
                        # _split_retry_gate 会重新武装并再次尝试。
                        self._split_incomplete_retry[abs_fp]["given_up"] = True
                        self.hub.log(
                            f"{name} 分卷链不完整，已重试 {self.SPLIT_INCOMPLETE_MAX} 次"
                            f"仍未到齐，放弃自动重试（源文件与分卷保留，可补齐后手动处理）")
                        self.hub.notify(
                            "分卷等待放弃",
                            f"{name}\n分卷链不完整，已停止自动重试（源文件与分卷保留）")
                        db.update_task_state(
                            tid, "failed",
                            error="分卷链不完整且已放弃自动重试"
                                  "（保留源文件与分卷，可手动重试）",
                            finished_at=int(time.time()))
                    else:
                        self.hub.log(
                            f"{name} 分卷未到齐（分卷链不完整，缺少兄弟分卷），稍后重试: {err}")
                        db.update_task_state(tid, "queued", error="分卷未到齐，稍后重试")
                    _tasks_changed(self.hub)
                    self._set_dir_state(wc.get("path"), "waiting", name=name)
                    self.traced.pop(abs_fp, None)
                    if record is not None:
                        try:
                            deletion_trail.save_records(
                                [r for r in deletion_trail.load_records()
                                 if r.get("id") != record["id"]])
                        except Exception:
                            pass
                    return "defer"
                # 分卷可能仍未到齐（7-Zip 可能报 Missing volume，也可能因
                # 密码错误/损坏掩盖缺卷）。只要按大小判断尚未到齐，本轮失败
                # 就不算数：撤销记录与追踪，下轮重新检查，等尾卷到齐后再解压。
                # 注意：假分卷名的完整压缩包不在此列（它走正常解压，失败就是真失败）。
                if (not smart_extract.is_fake_volume_name(fp)
                        and smart_extract.is_volume_name(name)
                        and smart_extract.is_first_volume(name)
                        and not (result or {}).get("split_gap_archive")
                        and ("Unexpected end of archive" in err
                             or "Missing volume" in err
                             or not self._volume_ready(fp))):
                    self.hub.log(f"{name} 分卷可能未到齐，稍后重试: {err}")
                    db.update_task_state(tid, "queued", error="分卷可能未到齐，稍后重试")
                    _tasks_changed(self.hub)
                    self._set_dir_state(wc.get("path"), "waiting", name=name)
                    self.traced.pop(abs_fp, None)
                    if record is not None:
                        try:
                            deletion_trail.save_records(
                                [r for r in deletion_trail.load_records()
                                 if r.get("id") != record["id"]])
                        except Exception:
                            pass
                    return "defer"
                # 跨目录分卷：解压到一半缺兄弟卷（该兄弟卷来自**另一个源文件**、落在
                # 别的输出目录，如两个伪装 mp4 各装一半）。登记等待，由 _split_recheck
                # 在监听范围内寻找并归拢，凑齐后重试锚点；超时则放弃（分卷与源文件都
                # 保留，不删——源文件可重新下载，分卷留待手动处理）。
                if (result or {}).get("split_gap_archive"):
                    anchor = Path(result["split_gap_archive"])
                    self.hub.log(
                        f"{name} 分卷缺兄弟卷（{err[:80]}），等待跨目录归拢: {anchor.name}")
                    db.update_task_state(tid, "queued",
                                         error="分卷缺兄弟卷，等待跨目录归拢")
                    _tasks_changed(self.hub)
                    self._set_dir_state(wc.get("path"), "waiting", name=name)
                    self._split_pending[abs_fp] = {
                        "anchor": str(anchor), "ident": ident, "tid": tid,
                        "since": time.time(), "last_check": 0.0,
                        "last_sig": self._split_signature(anchor),
                    }
                    self.traced.pop(abs_fp, None)
                    if record is not None:
                        try:
                            deletion_trail.save_records(
                                [r for r in deletion_trail.load_records()
                                 if r.get("id") != record["id"]])
                        except Exception:
                            pass
                    return "defer"
                # 输出文件被占用（如用户打开了解出的文件/杀毒扫描/残留的 7z 子进程）：
                # 通常很快释放，最多重试 3 次，超限才判失败，避免死循环。
                if ("Cannot delete output file" in err
                        or "正在使用此文件" in err
                        or "进程无法访问" in err
                        or "another process is using" in err
                        or "being used by another process" in err):
                    cnt = self._lock_retry.get(abs_fp, 0) + 1
                    if cnt <= 3:
                        self._lock_retry[abs_fp] = cnt
                        self.hub.log(f"{name} 输出文件被占用（{cnt}/3），稍后自动重试: {err[:120]}")
                        db.update_task_state(tid, "queued", error="输出文件被占用，稍后重试")
                        _tasks_changed(self.hub)
                        self._set_dir_state(wc.get("path"), "waiting", name=name)
                        self.traced.pop(abs_fp, None)
                        if record is not None:
                            try:
                                deletion_trail.save_records(
                                    [r for r in deletion_trail.load_records()
                                     if r.get("id") != record["id"]])
                            except Exception:
                                pass
                        return "defer"
                    self._lock_retry.pop(abs_fp, None)
                if (result or {}).get("keep_output_dir"):
                    # 主层已解出真实内容、只是更深的嵌套层失败：不回退、不报「失败」，
                    # 如实说「部分完成」，并点明输出目录里保留着已解出的内容。
                    self.hub.log(f"{name} 部分完成: {err}（已保留已解出的内容）")
                    self.hub.notify("智能解压部分完成",
                                    f"{name}\n{err}\n已解出的内容已保留在输出目录")
                else:
                    self.hub.log(f"{name} 解压失败: {err}")
                    self.hub.notify("智能解压失败", f"{name}\n{err}")
                db.update_task_state(
                    tid, ("need_password" if "密码" in err else "failed"),
                    error=err, finished_at=int(time.time()))
                _tasks_changed(self.hub)
                self._set_dir_state(wc.get("path"), "error", name=name)
                if record is not None:
                    deletion_trail.mark_failed(record["id"], err)
        except BaseException as e:
            self.hub.log(f"{name} 解压出错: {e}")
            self.hub.notify("智能解压出错", f"{name}\n{e}")
            if tid:
                db.update_task_state(
                    tid, ("need_password" if "密码" in str(e) else "failed"),
                    error=str(e), finished_at=int(time.time()))
                _tasks_changed(self.hub)
            self._set_dir_state(wc.get("path"), "error", name=name)
            if record is not None:
                deletion_trail.mark_failed(record["id"], str(e))
        finally:
            # 终态处理完毕才清上下文：成功 / 失败 / 差错 / 各处 defer 的 return 全覆盖，
            # 绝不让陈旧上下文污染监听线程后续自己的日志行。
            self.hub.clear_log_context()
        return "done"

    def _split_signature(self, anchor):
        """锚点旁同系列分卷的（名, 大小）集合签名：出现新兄弟卷/大小变化 → 值得重试。"""
        try:
            return tuple(sorted(
                (e.name, e.stat().st_size)
                for e in anchor.parent.iterdir()
                if e.is_file()
                and smart_extract.is_volume_file(anchor.name, e.name, anchor.stem)))
        except OSError:
            return ()

    def _gather_and_consolidate(self, anchor, wc):
        """在监听根（与输出目录，若配置）范围内寻找锚点的跨目录分卷兄弟并移到锚点旁。

        同名冲突 / 被占用 / 移动失败者跳过（不回滚：凑齐多少算多少，下轮再找）。
        返回本轮移动的个数。只在监听白名单范围内搜索，绝不到监听根之外。"""
        anchor = Path(anchor)
        roots = []
        for p in (wc.get("path"), (wc.get("output_dir") or "").strip() or None):
            if p:
                roots.append(Path(p))
        moved = 0
        # 实验性关闭：只扫各根目录**表层**（与 _poll 能看到的范围一致），绝不往
        # 子文件夹里摸；实验性开启（百度网盘那套）才递归 rglob 找散落在子目录里
        # 的分卷兄弟。开关读取与其它实验性功能共用 baidu_manifest.is_enabled。
        recursive = baidu_manifest.is_enabled(self.state)
        for root in roots:
            if not root.exists():
                continue
            try:
                candidates = root.rglob("*") if recursive else root.iterdir()
                for cand in candidates:
                    try:
                        # 隔离区（_已删除）里的文件是待还原的副本，绝不参与分卷归拢
                        if _in_quarantine(cand):
                            continue
                        if (cand.is_file()
                                and cand.parent != anchor.parent
                                and not smart_extract.is_incomplete_download(cand)
                                and smart_extract.is_volume_file(
                                    anchor.name, cand.name, anchor.stem)
                                and _can_open_append(cand)):
                            dest = anchor.parent / cand.name
                            if dest.exists():
                                continue
                            shutil.move(str(cand), str(dest))
                            moved += 1
                            self.hub.log(f"跨目录分卷归拢: {cand} -> {dest}")
                            try:
                                cand.parent.rmdir()   # 搬空则清掉来源目录
                            except OSError:
                                pass
                    except OSError:
                        continue
            except OSError:
                continue
        return moved

    def _split_recheck(self, fp, wc):
        """跨目录分卷等待期复查（由 _handle 的等待捷径调用，每 SPLIT_RECHECK_INTERVAL
        秒一次）：找兄弟卷 → 归拢 → 重试锚点；无进展超 SPLIT_MAX_WAIT 则放弃。

        返回 "defer"=继续等待 / "done"=了结。"""
        abs_fp = self._norm_path(fp)
        st = self._split_pending.get(abs_fp)
        if not st:
            return "done"
        now = time.time()
        if now - st.get("last_check", 0.0) < self.SPLIT_RECHECK_INTERVAL:
            return "defer"
        st["last_check"] = now
        anchor = Path(st["anchor"])
        if not fp.exists() or not anchor.exists():
            self._split_pending.pop(abs_fp, None)
            self._set_dir_state(wc.get("path"), "listening")
            try:
                orig_tid = st.get("tid")
                if orig_tid:
                    db.update_task_state(
                        orig_tid, "failed",
                        error="跨目录分卷等待中源文件或分卷锚点已消失，已中止自动归拢",
                        finished_at=int(time.time()))
                    _tasks_changed(self.hub)
            except Exception:
                pass
            return "done"
        moved = self._gather_and_consolidate(anchor, wc)
        sig = self._split_signature(anchor)
        if moved or sig != st.get("last_sig") or st.pop("retry_pending", False):
            st["last_sig"] = sig
            st["since"] = now            # 有进展：重置超时计时
            if moved:
                self.hub.log(f"跨目录分卷已归拢 {moved} 个到 {anchor.parent}: {anchor.name}")
            self.hub.log(f"分卷兄弟有更新/归拢，重试: {anchor.name}")
            out = {}
            self._handle(anchor, wc, traced=True, out=out)
            result = out.get("result")
            if result is not None and result.get("success"):
                self._split_pending.pop(abs_fp, None)
                try:
                    orig_tid = st.get("tid")
                    if orig_tid:
                        db.update_task_state(
                            orig_tid, "done", finished_at=int(time.time()),
                            output_dir=(result.get("promoted_dir")
                                        or str(Path(st["anchor"]).parent)))
                        _tasks_changed(self.hub)
                except Exception:
                    pass
                self._recover_finish(fp, wc)
                return "done"
            if result is None:
                st["retry_pending"] = True   # 锚点被占用/暂停等：下轮再试
            return "defer"
        if now - st.get("since", now) >= self.SPLIT_MAX_WAIT:
            self.hub.log(f"跨目录分卷 {self.SPLIT_MAX_WAIT}s 内未到齐，放弃自动归拢"
                         f"（分卷已保留: {anchor}；源文件保留: {fp.name}）")
            self._split_pending.pop(abs_fp, None)
            try:
                orig_tid = st.get("tid")
                if orig_tid:
                    db.update_task_state(
                        orig_tid, "failed",
                        error="分卷兄弟卷未到齐（跨目录归拢超时）",
                        finished_at=int(time.time()))
                    _tasks_changed(self.hub)
            except Exception:
                pass
            self._set_dir_state(wc.get("path"), "error")
            try:
                rec = deletion_trail.new_record(fp, wc.get("path"))
                deletion_trail.add_record(rec)
                deletion_trail.mark_failed(rec["id"], "分卷兄弟卷未到齐（跨目录归拢超时）")
            except Exception:
                pass
            return "done"
        return "defer"

    def _recover_finish(self, fp, wc):
        """跨目录分卷恢复成功：源文件的载荷已由锚点解出，回收已消费的源文件并记档。"""
        try:
            # 与 _handle 一致地遵守本目录的删除策略：quarantine 时回收站不可用走隔离区
            delete_policy = str(wc.get("delete_policy") or "auto")
            qroot = (str(wc.get("output_dir") or "")
                     if delete_policy.strip().lower() == "quarantine" else None)
            rec = deletion_trail.new_record(fp, wc.get("path"))
            deletion_trail.add_record(rec)
            # 删除前预写「正在删除…」意图记录（与 _handle 的 pre_hook 同一语义）。
            _mark_deleting(rec["id"], [str(fp)])
            qmap = []
            recycled, failed = smart_extract._recycle_paths(
                [str(fp)],
                permanent_fallback=smart_extract.delete_policy_permanent_fallback(
                    delete_policy),
                quarantine_root=qroot, quarantine_out=qmap)
            deletion_trail.mark_deleted(rec["id"], recycled, failed, qmap or None)
            self.hub.log(f"跨目录分卷恢复成功，已回收源文件: {fp.name}")
        except Exception as e:
            self.hub.log(f"回收源文件出错（保留原文件）: {fp.name}: {e}")

    def _progress_cb(self, ratio, layer, name):
        """解压引擎进度回调 → GUI 队列（_drain 更新进度条）。ratio=None=忙碌。

        进度消息额外带上当前任务 id（取自线程日志上下文），供新版 GUI 归属进度；
        同时把所在目录上报为 extracting + 百分比。
        """
        try:
            _sd, tid = self.hub.current_log_context()
        except Exception:
            _sd, tid = None, None
        try:
            self.hub.q.put({"type": "progress", "ratio": ratio,
                            "layer": layer, "name": name, "task_id": tid})
        except Exception:
            pass
        if _sd:
            self._set_dir_state(_sd, "extracting",
                                int(ratio * 100) if ratio is not None else None,
                                name)

    def _trace_produced(self, extracted_files, wc, depth=0):
        """多层解压后，只专门追溯本次产生的压缩包文件（限制深度防死循环）"""
        if depth > 10:
            return
        for p in extracted_files or []:
            pf = Path(p)
            if not pf.exists():
                continue
            if self._is_archive(pf):
                abs_pf = self._norm_path(pf)
                ident = self._file_identity(pf)
                if self._is_same_traced(self.traced.get(abs_pf), ident):
                    self._traced_touch(abs_pf)
                    continue
                self._traced_put(abs_pf, ident)
                self.hub.log(f"追溯解压产生的压缩包: {pf.name}")
                self._handle(pf, wc, traced=True)

    @staticmethod
    def _is_archive(fp):
        try:
            info = smart_extract.analyze_file(fp)
            if info.get("is_incomplete"):
                return False
            if info["detected_format"]:
                return True
            return smart_extract.is_archive_file(Path(fp))
        except Exception:
            return False
