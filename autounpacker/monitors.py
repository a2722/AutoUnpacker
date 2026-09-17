# -*- coding: utf-8 -*-
"""后台监控线程：目录轮询智能解压 + 剪贴板/二维码监控。

职责：- FolderWatcher：轮询监听目录表层，识别压缩包并触发智能解压（嵌套/密码/分卷/伪装/删除回溯）
- 分卷到齐判断、翻译 JSON 归位、百度清单模式（Tier-2 子目录处理，只增强不阻断）
- QRMonitor：剪贴板二维码识别 + 短文本临时密码捕获 + 网址信任门卫（黑白名单判定）；
  轮询线程只负责「检测 + 入队」，单独的守护工作线程串行执行网络/子进程/浏览器等阻塞 I/O
关键入口：FolderWatcher / QRMonitor
依赖：extract、trail、db、trust、baidu_task（实验性，惰性导入）
注意：剪贴板/二维码依赖（win32clipboard/PIL）为惰性探测，缺失时自动禁用相关功能
"""
import os
import queue
import re
import shutil
import types
import sys
import threading
import time
import webbrowser
from collections import deque
from pathlib import Path

from . import paths
from . import extract as smart_extract   # noqa: F401  保留原名引用
from . import trail as deletion_trail     # noqa: F401
from . import db                          # noqa: F401
from .trust import (_host_of, decide_host, remember_auto_domain)
from .utils import _can_open_append

# 剪贴板/二维码可用性：改为惰性探测（首次用到时才 import 并缓存结果）。
# 目的：win32clipboard/PIL 在启动路径上不再加载，缩短冷启动时间；
# 依赖缺失时首轮轮询探测一次即记入标志，后续不再重复尝试。
QR_AVAILABLE = True
CLIPBOARD_AVAILABLE = True
_QR_PROBED = False
_CLIP_PROBED = False
_clipboard_mod = None   # 探测成功后缓存的 win32clipboard 模块
_imagegrab_mod = None   # 探测成功后缓存的 PIL.ImageGrab 模块

# URL 允许的 ASCII 字符集（RFC 3986 组成字符），用于把「网址 + 中文说明/提取码」
# 这类剪贴板文本截断成真正的网址。若把空格/中文一并交给 urllib，会抛
# "URL can't contain control characters"（用户反馈的 drive.uc.cn 提取码场景）。
_URL_CHARS = r"A-Za-z0-9\-._~:/?#\[\]@!$&'()*+,;=%"
_URL_RE = re.compile(r"https?://[" + _URL_CHARS + r"]+", re.I)
_WWW_URL_RE = re.compile(r"www\.[" + _URL_CHARS + r"]+", re.I)
_URL_TRAILING_JUNK = ".,;:!?、。，；：！？）】》」"


def _ensure_clipboard():
    """首次调用时探测剪贴板依赖；结果缓存到模块级标志。返回 (win32clipboard, ImageGrab)。"""
    global CLIPBOARD_AVAILABLE, QR_AVAILABLE, _QR_PROBED, _CLIP_PROBED, _clipboard_mod, _imagegrab_mod
    if not _CLIP_PROBED:
        _CLIP_PROBED = True
        try:
            import win32clipboard
            _clipboard_mod = win32clipboard
        except ImportError:
            CLIPBOARD_AVAILABLE = False
            QR_AVAILABLE = False
    if not _QR_PROBED:
        _QR_PROBED = True
        try:
            from PIL import ImageGrab
            _imagegrab_mod = ImageGrab
        except ImportError:
            QR_AVAILABLE = False
    return _clipboard_mod, _imagegrab_mod


def _clipboard():
    """已探测到剪贴板依赖时返回 win32clipboard 模块，否则 None。"""
    if CLIPBOARD_AVAILABLE:
        return _clipboard_mod
    return None


def _imagegrab():
    """已探测到 PIL 时返回 ImageGrab 模块，否则 None。"""
    if QR_AVAILABLE:
        return _imagegrab_mod
    return None

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
    TRANSLATION_MAX_SIZE = 10 * 1024 * 1024  # 翻译 json 最大 10MB
    TRANSLATION_WINDOW = 5 * 60              # 小文件夹先出现时的监控窗口（秒）
    VOL_MAX_WAIT = 300   # 首卷分卷"只有满卷"时最长等待(秒)：之后兜底按现状尝试解压
    SPLIT_MAX_WAIT = 1800        # 跨目录分卷等待兄弟卷的最长时间(秒)：超时放弃（仅保留不删）
    SPLIT_RECHECK_INTERVAL = 10  # 跨目录分卷复查间隔(秒)：节流「全监听根 rglob」
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
        # 新文件（重新下载/替换）不会被误当成已处理而跳过
        self.traced = {}
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
        # 百度清单模式（Tier-2）：已处理集合 / 稳定观察 / 已告警 / 粘性记忆缓存
        self._bt_seen = {}      # {watch_key: set(norm_abs_path)}
        self._bt_probe = {}     # {watch_key: {norm_abs_path: (size, 稳定起始时间)}}
        self._bt_warned = set()
        self._bt_sticky = None  # set(norm_abs_path)，来自 toolbox.db 的粘性记忆
        self._bt_consolidated = set()  # 已归拢（或已放弃）的跨目录分卷首卷：不再重复归拢
        self._out_warned = set()  # 已提示过「输出目录与监听根重叠」的路径
        # 跨目录分卷等待期 {norm_source_path: {anchor, ident, since, last_check, last_sig}}
        self._split_pending = {}

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

    @staticmethod
    def _norm_path(path):
        """规范化路径，用于去重（忽略大小写与尾部分隔符）"""
        try:
            p = os.path.normcase(os.path.abspath(path))
            while p.endswith(("\\", "/")) and len(p) > 3:
                p = p[:-1]
            return p
        except Exception:
            return str(path)

    def run(self):
        # stdout 捕获由进程入口（app.main）统一幂等安装，这里不再改进程级全局。
        while True:
            cfg = self.state.snapshot()
            interval = max(1, int(cfg.get("poll_interval", 2)))
            # 暂停 = 原「停止监听」：不再轮询、不再检测新文件，恢复后重新扫描
            if not self.state.running or (self.pauser is not None
                                          and self.pauser.is_paused()):
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
            for path, wc in enabled.items():
                try:
                    self._poll(Path(path), wc)
                except Exception as e:
                    self.hub.log(f"监听轮询出错 ({path}): {e}")
            time.sleep(interval)

    def _poll(self, watch, wc):
        # 只监听文件夹表面的一层文件，不递归子孙文件夹
        if not watch.is_dir():
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
        if str(wc.get("mode") or "") == "baidu":
            try:
                self._baidu_poll(watch, wc)
            except Exception as e:
                self.hub.log(f"百度清单模式处理出错 ({watch}): {e}")
        key = self._norm_path(watch)
        if key not in self.seen:
            try:
                current = set(n for n in os.listdir(watch) if (watch / n).is_file())
            except OSError:
                return
            self.hub.log(f"开始监听: {watch}")
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
            return
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
                from . import db as _db
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
        from . import baidu_task as bt
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
            dirs = [d for d in watch.iterdir() if d.is_dir()]
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
                if not d.is_dir() or d == exclude:
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
        abs_fp = str(fp)
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
            from . import baidu_task as _baidu_task
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
                state["last_sig"] = None
                state["last_change"] = now
                _wlog(("no-final", name, tuple(sorted(vols))),
                      f"首卷已出现，分卷未到齐（缺末卷 {final_name}）: {name}"
                      f"（已到编号分卷 {sorted(vols)}）")
                return False
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
            return False
        if now - state["last_change"] >= self.VOL_MAX_WAIT:
            # 长时间无变化 → 兜底强制放行（覆盖恰好整倍数/下载中断的罕见情况）
            self._vol_wait.pop(abs_fp, None)
            self.hub.log(f"分卷等待超时（{self.VOL_MAX_WAIT}s），按现有分卷尝试解压: {name}")
            return True
        return False

    def _handle(self, fp, wc, traced=False, initial_scan=False, out=None):
        """返回: "done"=已处理/已定型, "skip"=当前不是压缩包(可复查), "defer"=稍后重试"""
        name = fp.name
        # 用户暂停：延后所有解压（静默 defer，不刷日志），恢复后下轮自然继续。
        # 放在最前面，暂停期间连"下载未完成"等提示也不发。
        if self.pauser is not None and self.pauser.is_paused():
            return "defer"
        if smart_extract.is_incomplete_download(fp):
            self.hub.log(f"下载未完成，暂不解压（等待后缀消失）: {name}")
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
            return "defer"
        # 非首卷分卷（.part2.rar / .002 / .z02 / .r01 等）不是解压入口，
        # 单独交给 7-Zip 必然失败；等首卷出现时统一处理整个分卷。
        elif smart_extract.is_non_first_volume(name):
            self.hub.log(f"非首卷分卷，等待首卷处理整个分卷: {name}")
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
                return "defer"
        elif smart_extract.volume_download_pending(fp):
            self.hub.log(f"分卷未到齐（其他分卷仍在下载），暂不解压，等待下载完成: {name}")
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
            return "done"
        self.traced[abs_fp] = ident
        # 只给最初始源文件建立删除回溯记录（多层解压产生的次级中间文件不标记）
        record = None
        if not traced:
            record = deletion_trail.new_record(fp, wc.get("path"))
            deletion_trail.add_record(record)
        self.hub.notify("发现压缩包", f"{name}\n开始智能解压...")
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
                run_script=None, script_args=[],
                promote_to=promote_to,
                promote_merge=bool(self.state.snapshot().get("promote_merge", True)),
            )
            if record is not None:
                args.delete_hook = (
                    lambda recycled, failed, rid=record["id"]:
                    deletion_trail.mark_deleted(rid, recycled, failed))
            self.hub.q.put({"type": "progress_start"})
            try:
                result = smart_extract.extract_one(
                    engine, str(fp), out_dir, passwords, options, args,
                    progress_cb=self._progress_cb, pauser=self.pauser)
            finally:
                self.hub.q.put({"type": "progress_done"})
            if out is not None:
                out["result"] = result     # 供 _split_recheck 读锚点重试的结果
            if result and result["success"]:
                self._lock_retry.pop(abs_fp, None)
                msg = f"{name} 完成，穿透 {result['depth_reached']} 层，共 {len(result['extracted_files'])} 个文件"
                self.hub.log(msg)
                self.hub.notify("智能解压完成", msg)
                if record is not None and not args.delete_source and not promote_to:
                    deletion_trail.mark_kept(record["id"])
                # 只追溯本次解压产生的压缩包文件，不监听其他文件
                trace_targets = ([result["promoted_dir"]] if result.get("promoted_dir")
                                 else result["extracted_files"])
                self._trace_produced(trace_targets, wc)
            else:
                err = (result or {}).get("error") or "未知错误"
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
                    self._split_pending[abs_fp] = {
                        "anchor": str(anchor), "ident": ident,
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
                self.hub.log(f"{name} 解压失败: {err}")
                self.hub.notify("智能解压失败", f"{name}\n{err}")
                if record is not None:
                    deletion_trail.mark_failed(record["id"], err)
        except BaseException as e:
            self.hub.log(f"{name} 解压出错: {e}")
            self.hub.notify("智能解压出错", f"{name}\n{e}")
            if record is not None:
                deletion_trail.mark_failed(record["id"], str(e))
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
        for root in roots:
            if not root.exists():
                continue
            try:
                for cand in root.rglob("*"):
                    try:
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
            rec = deletion_trail.new_record(fp, wc.get("path"))
            deletion_trail.add_record(rec)
            recycled, failed = smart_extract._recycle_paths([str(fp)])
            deletion_trail.mark_deleted(rec["id"], recycled, failed)
            self.hub.log(f"跨目录分卷恢复成功，已回收源文件: {fp.name}")
        except Exception as e:
            self.hub.log(f"回收源文件出错（保留原文件）: {fp.name}: {e}")

    def _progress_cb(self, ratio, layer, name):
        """解压引擎进度回调 → GUI 队列（_drain 更新进度条）。ratio=None=忙碌。"""
        try:
            self.hub.q.put({"type": "progress", "ratio": ratio,
                            "layer": layer, "name": name})
        except Exception:
            pass

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
                    continue
                self.traced[abs_pf] = ident
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


# ==================== 剪贴板二维码识别 ====================
# 带这些扩展名的文本基本是文件名（如 MAKO202608.jpg / 画面.png），不是提取码
_FILE_EXT_RE = re.compile(
    r"\.(png|jpe?g|gif|bmp|webp|tiff?|svg|mp4|mkv|avi|mov|wmv|flv|"
    r"rar|zip|7z|tar|gz|txt|json|xml|lnk|exe|dll|msi|pdf|db|log|"
    r"vmd|pmx|pmd|fx|fxsub|dds)$", re.I)


def _looks_like_non_password(text):
    """严格过滤（对应「智能过滤」子项）：多行/路径/文件名/句子等明显不是提取码。

    不含网址判断——网址由更宽松的父项「网址排除」负责。
    另：含版本号样式 / ≥4 个空白分词 also 视为非密码（标题/版本串）。"""
    t = text.strip().strip('"').strip("'").strip()
    if not t:
        return True
    if "\n" in t or "\r" in t:
        return True                                       # 提取码都是单行
    if any(c in t for c in "，。！？；：、…"):
        return True                                       # 含句读标点 → 是句子不是提取码
    if (re.match(r"^[A-Za-z]:[\\/]", t) or t.startswith("\\\\")
            or t.startswith("//")):
        return True                                       # 盘符 / UNC / 网络路径
    if "\\" in t and re.search(r"[A-Za-z]", t):
        return True                                       # 含反斜杠的路径样文本
    if _FILE_EXT_RE.search(t):
        return True                                       # 带常见扩展名的文件名
    # 标题/版本样式（如「PIXEL CALL GIRLS -REI- 1.30」）：多词 + 版本号一眼不是提取码。
    # 只挡这两类窄形态，普通多词口令（「my pass」）与 4 位提取码不受影响；
    # 用户若真要捕获这类串，可在设置里关掉「智能过滤」子项。
    if re.search(r"\d+\.\d+", t):
        return True                                       # 含版本号样式 x.y
    if len([w for w in re.split(r"\s+", t) if w]) >= 4:
        return True                                       # 4 个及以上空白分词 → 标题/句子
    return False


def _should_capture_temp_password(text, cfg):
    """剪贴板文本是否记为临时密码（父子两级过滤）。

    - 父（宽松）url_exclude_temp_password：带 :// 的网址不记；关闭则照单全收。
    - 子（更严格）temp_password_filter：在父级基础上再排除多行/路径/文件名/
      句子，且只收 <60 字符；仅在父项开启时才有意义。
    """
    if not bool(cfg.get("url_exclude_temp_password", True)):
        return True                                   # 父关 → 照单全收
    if "://" in text.strip():
        return False                                  # 父级：网址不记
    if bool(cfg.get("temp_password_filter", True)):
        return len(text) < 60 and not _looks_like_non_password(text)
    return True


class QRMonitor(threading.Thread):
    """剪贴板监控：轮询线程只「检测 + 入队」，单独工作线程串行执行阻塞 I/O。

    两线程模型（D8 option C）：
    - 轮询线程（run/_poll_once，每 0.5s 一次）：快照配置、排空信任放行队列、
      捕获剪贴板文本、按 md5 去重检测剪贴板图片，全部只做轻量操作并转为工作项
      入队；不发起任何网络/子进程/浏览器调用。这样检测循环始终快速返回，
      _recent_texts 得以连续喂入（依赖它的「最近提取码回退」功能才不会被卡住）。
    - 工作线程（_worker_loop，单个守护线程）：从 task_q 串行取任务，执行
      _maybe_process_url/_decode_qr/_set_clipboard/_open_browser 等阻塞 I/O。
      工作项严格 FIFO，处理顺序与旧单线程实现一致。

    task_q 工作项格式：
      ("text", text)            -> _maybe_process_url(text)
      ("image", image)          -> _decode_qr(image) + _handle_decoded_texts(texts)
      ("grant", url, purpose)   -> purpose=="fetch" 时强制拉取，否则打开浏览器
    """
    def __init__(self, state, hub, pauser=None):
        super().__init__(daemon=True)
        self.state = state
        self.hub = hub
        self.pauser = pauser
        self.last_hash = None
        self.last_text = None
        self.last_url = None  # 最近尝试访问的网址（避免同一网址重复拉取）
        self.qr_worker_path = paths.WORKERS_DIR / "qr_worker.py"          # 解码子进程脚本
        self.clipboard_worker_path = paths.WORKERS_DIR / "clipboard_worker.py"  # 剪贴板写入子进程脚本
        # 最近捕获的非图片剪贴板内容（如用户复制二维码前复制的提取码）
        self._recent_texts = deque(maxlen=20)
        # 工作队列：轮询线程只负责「检测 + 入队」，工作线程串行执行阻塞 I/O。
        # maxsize=8 用于限制内存；满队列由 _enqueue 淘汰最旧一项处理，绝不阻塞/抛异常。
        self.task_q = queue.Queue(maxsize=8)
        self._hist_lock = threading.Lock()   # 保护 last_text/_recent_texts（轮询线程与工作线程共享）
        self._queue_full_logged = False      # 满队列日志节流标志（每段溢出只记一次）
        # 「静默复制」正在写入剪贴板的文本：写入成功前置位，防止自写内容被轮询
        # 线程当成用户输入（详见 _copy_silently）。None=当前无静默写入。
        self._silent_write = None

    def run(self):
        # 工作线程只启动一次：所有阻塞 I/O（网络/子进程/浏览器）都在它里面串行
        # 执行，保证本轮询循环每 0.5s 都能持续检测剪贴板变化。
        threading.Thread(target=self._worker_loop, daemon=True).start()
        while True:
            cfg = self.state.snapshot()
            self._poll_once(cfg)
            time.sleep(0.5)

    def _poll_once(self, cfg):
        """执行一轮轮询：只做「检测 + 入队」，绝不发起网络/子进程/浏览器 I/O。

        轮询线程职责：
        - 排空 url_grant_q，把用户的信任放行请求转为工作项入队（不受暂停影响）
        - 暂停门控下：捕获剪贴板文本、按 md5 去重检测剪贴板图片，命中即入队
        真正的耗时操作交给 _worker_loop 串行执行，因此本方法始终快速返回，
        _recent_texts 得以连续喂入（最近提取码回退功能依赖它）。"""
        # 处理用户信任确认后的放行请求（主窗口决策回调写入 url_grant_q）
        while True:
            try:
                gurl, gpurpose = self.hub.url_grant_q.get_nowait()
            except queue.Empty:
                break
            self._enqueue(("grant", gurl, gpurpose))
        # 排空「静默复制」请求（主窗口点击日志链接后写入 hub.clip_echo_q）。写法与
        # url_grant_q 同款；getattr 兼容没有该属性的测试桩 hub。
        clip_q = getattr(self.hub, "clip_echo_q", None)
        if clip_q is not None:
            while True:
                try:
                    curl = clip_q.get_nowait()
                except queue.Empty:
                    break
                self._enqueue(("clipcopy", curl))
        # 暂停 = 原「停止监听」：剪贴板/二维码监控一并停止
        if self.state.running and not (self.pauser is not None
                                       and self.pauser.is_paused()):
            try:
                new_text = self._capture_text_password()
                if cfg.get("qr_url_enabled") and QR_AVAILABLE and new_text:
                    self._enqueue(("text", new_text))
                if cfg.get("qr_enabled") and QR_AVAILABLE and CLIPBOARD_AVAILABLE:
                    wc, ig = _ensure_clipboard()
                    if wc is not None and ig is not None:
                        if wc.IsClipboardFormatAvailable(wc.CF_DIB):
                            image = ig.grabclipboard()
                            if image is not None:
                                self._process(image)
            except Exception as e:
                self.hub.log(f"剪贴板监控出错: {e}")

    def _worker_loop(self):
        """工作线程：串行消费 task_q，执行所有阻塞 I/O。

        与轮询线程分离的原因见类文档。broad try/except 保证任何单个任务失败都
        不会让工作线程退出（否则后续任务将永远无人处理）。工作项严格 FIFO。"""
        while True:
            item = self.task_q.get()
            try:
                kind = item[0]
                if kind == "text":
                    self._maybe_process_url(item[1])
                elif kind == "image":
                    texts = self._decode_qr(item[1])
                    self._handle_decoded_texts(texts)
                elif kind == "grant":
                    try:
                        if item[2] == "fetch":
                            self._maybe_process_url(item[1], force=True)
                        else:
                            self._open_browser(item[1])
                    except Exception as e:
                        self.hub.log(f"信任放行执行失败: {e}")
                elif kind == "clipcopy":
                    # 日志链接的「静默复制」：写剪贴板但绝不被本程序当成新输入
                    self._copy_silently(item[1])
            except Exception as e:
                self.hub.log(f"剪贴板监控出错: {e}")
            finally:
                self.task_q.task_done()

    def _enqueue(self, item):
        """把工作项放入 task_q；队列满时淘汰**最旧**的一项，绝不抛异常给调用者。

        队列满时必须在「丢最旧」与「丢最新」之间选。丢最新会掐死用户刚触发的
        操作（刚复制的分享链接、刚确认的信任放行）；而且 last_text/last_hash 是
        在入队之前就推进的，被丢掉之后「再复制一遍」也会被去重跳过、无法补救。
        因此这里淘汰最旧的一项，保证最新的用户操作一定被处理。

        满队列日志只在一次「溢出连续段」里记一条，恢复正常入队后复位标志。"""
        try:
            self.task_q.put_nowait(item)
            self._queue_full_logged = False
            return
        except queue.Full:
            pass
        evicted = False
        try:
            self.task_q.get_nowait()       # 淘汰最旧的一项
            self.task_q.task_done()        # 与被淘汰项配平 unfinished_tasks
            self.task_q.put_nowait(item)   # 单消费者只出不进，此处不会再 Full
            evicted = True
        except (queue.Empty, queue.Full):
            pass
        if not self._queue_full_logged:
            self._queue_full_logged = True
            self.hub.log("剪贴板处理队列已满，已淘汰最旧的一项" if evicted
                         else "剪贴板处理队列已满，本次任务已丢弃")

    def _capture_text_password(self):
        """监控剪贴板文本：短文本(<60)存入临时密码；同时记录最近的非图片内容
        到 _recent_texts（供二维码触发后恢复提取码到剪贴板用）。

        返回本次新出现的文本（供网址二维码识别用），无新文本返回 None。"""
        if not CLIPBOARD_AVAILABLE:
            return None
        wc, _ = _ensure_clipboard()
        if wc is None:
            return None
        try:
            if not wc.IsClipboardFormatAvailable(wc.CF_UNICODETEXT):
                return None
            # OpenClipboard 会因其他进程短暂占用剪贴板报"拒绝访问"(error 5)，
            # 重试几次通常能成功；全部失败才放弃本轮（下次轮询再试）。
            opened = False
            for _ in range(4):
                try:
                    wc.OpenClipboard()
                    opened = True
                    break
                except Exception:
                    time.sleep(0.15)
            if not opened:
                raise OSError("OpenClipboard 重试失败（剪贴板被其他进程占用）")
            try:
                text = wc.GetClipboardData(wc.CF_UNICODETEXT)
            finally:
                wc.CloseClipboard()
            if not text:
                return None
            text = text.strip()
            # last_text / _recent_texts 由轮询线程（此处）与工作线程
            # （_restore_last_text）共享，统一用 _hist_lock 保护；临界区内不做任何
            # 耗时操作，避免卡住另一线程。
            with self._hist_lock:
                # text == _silent_write：这是本程序刚静默写入剪贴板的内容，绝不当
                # 成用户输入（写剪贴板与置位 last_text 之间的瞬时窗口也由它兜住）。
                if (not text or text == self.last_text
                        or text == getattr(self, "_silent_write", None)):
                    return None
                self.last_text = text
                self._recent_texts.append((time.time(), text[:200]))
            cfg = self.state.snapshot()
            # 是否记录为临时密码：受「智能过滤」「网址排除」两个开关控制
            if _should_capture_temp_password(text, cfg):
                added = self.state.add_temp_password(text)
                if added:
                    self.hub.log(f"已捕获临时密码: {text}")
                    if self.state.auto_add():
                        self.state.add_long_password(text)
                        self.hub.log(f"已自动加入长期密码本: {text}")
            return text
        except Exception as e:
            self.hub.log(f"临时密码捕获出错: {e}")
            return None

    def _set_clipboard(self, text):
        """把文本写入剪贴板（替换当前内容）。失败返回 False。

        在独立子进程执行：win32clipboard.SetClipboardData 的原生层在
        并发/特殊输入下可能堆损坏（0xc0000374）导致整个程序闪退，
        隔离后崩溃只影响子进程，主程序不受影响。"""
        if not CLIPBOARD_AVAILABLE or not text:
            return False
        import subprocess
        try:
            proc = subprocess.run(
                [sys.executable, str(self.clipboard_worker_path)],
                timeout=8, input=text.encode("utf-8"),
                capture_output=True, creationflags=0x08000000,  # CREATE_NO_WINDOW
            )
            return proc.returncode == 0
        except Exception:
            return False

    def _copy_silently(self, text):
        """日志链接点击的「静默复制」：写剪贴板但**绝不**被本程序当作新输入处理。

        关键在本次自写期间 `_silent_write` 的置位（见下方失败语义）：轮询线程
        _capture_text_password 读到同一内容时，`text == self.last_text` 或
        `text == self._silent_write` 都会直接 return None —— 因此这次自写**不会**：
          ① 被记录为临时密码；② append 进 _recent_texts（最近提取码候选）；
          ③ 由 _poll_once 入队走 _maybe_process_url（网址信任询问/抓取/百度分享）。
        不要把它 append 进 _recent_texts，也不要加其它副作用。

        失败语义（本轮修复）：先写剪贴板，**成功才**推进 last_text/last_url；
        失败则不推进——否则用户之后真的手动复制同一个 URL 会被静默忽略，旧剪贴板
        内容也可能被重处理一次。自写内容不会被当成用户输入的保证：
          - 写之前先在 _hist_lock 内把 text 放进 `_silent_write`；轮询线程的
            `_capture_text_password` 在同一把锁内同时检查 last_text 与 `_silent_write`，
            命中任一即 return None；
          - 写成功后，在**同一次持锁**内先置 last_text/last_url、再把 `_silent_write`
            清空。轮询线程要么在清空前看到 `_silent_write`、要么在清空后看到
            last_text，两态都被同一把锁串起来，不存在「既未置位、又未标记」的窗口；
          - 写失败时剪贴板内容未变（仍是旧内容），`_silent_write` 清空、last_text 保持
            不变，旧内容仍由 last_text 去重抑制，且用户真的再复制该 URL 时不会被漏掉。
        """
        with self._hist_lock:
            self._silent_write = text
        ok = self._set_clipboard(text)
        with self._hist_lock:
            if ok:
                self.last_text = text
                self.last_url = text
            self._silent_write = None
        try:
            self.hub.q.put({"type": "clip_done", "url": text, "ok": bool(ok)})
        except Exception:
            pass

    def _restore_last_text(self):
        """把最近捕获的非图片剪贴板内容（如提取码）写回剪贴板，方便直接 Ctrl+V。

        _recent_texts / last_text 与轮询线程共享，用 _hist_lock 保护；锁只包住
        「取队尾 + 出队」这一步，_set_clipboard 的阻塞调用留在锁外，避免轮询
        线程被卡住。

        迭代次数以进入时的条数为上限：轮询线程在循环期间仍会持续 append，不设
        上限时（旧实现同线程读写，不会发生）写剪贴板持续失败 + 持续复制会让这里
        迟迟不退出，饿死队列里的其它任务。"""
        with self._hist_lock:
            pending = len(self._recent_texts)
        while pending > 0:
            pending -= 1
            with self._hist_lock:
                if not self._recent_texts:
                    break
                _, text = self._recent_texts[-1]
                self._recent_texts.pop()
                if text:
                    self.last_text = text  # 避免下一轮把它当新内容重复记录
            if not text:
                continue
            if self._set_clipboard(text):
                self.hub.log(f"已把最近的非图片复制内容写回剪贴板: {text[:40]}")
                return
        self.hub.log("没有可恢复的最近文本")

    def _history(self):
        """返回最近的非图片剪贴板文本快照 [(t, text), ...]（临时密码回退用，需持锁取副本）。"""
        with self._hist_lock:
            return list(self._recent_texts)

    def _process(self, image):
        """轮询线程侧：剪贴板图片按 md5 去重后入队，解码交给工作线程。

        去重必须留在轮询线程：否则未变化的剪贴板图片会每 0.5s 重复入队，很快
        淹没 task_q。仅在图片确实变化（哈希不同）时入队一次。"""
        import hashlib
        from io import BytesIO
        buf = BytesIO()
        image.save(buf, format="PNG")
        h = hashlib.md5(buf.getvalue()).hexdigest()
        if h == self.last_hash:
            return
        self.last_hash = h
        self._enqueue(("image", image))

    def _handle_decoded_texts(self, texts):
        """对解码出的文本做统一处理。

        三种情况：
        A. 无 URL（普通内容 / 单二维码多个链接无法唯一确定）：
           → 按设置写剪贴板：action=code 且内容含提取码时只抬升提取码，
             否则写回全部内容 + 记日志
        B. 纯链接（解码文本本身就是 URL）：
           → 信任判定 → 打开浏览器 → 按设置做剪贴板联动（含提取码抬升）
        C. 含链接但不是纯链接（链接夹杂其他内容）：
           → 截取 URL 打开（走黑白名单）+ 按设置写剪贴板：
             action=code 且文本内含提取码 → 抬升该提取码；否则写回全部内容

        信任判定（decide_host）在重定向之后执行：未信任的域名不自动打开
        浏览器，投递到主窗口询问；黑名单/内置敏感地址直接静默拒绝。"""
        try:
            cfg = self.state.snapshot()
            redirect = cfg.get("qr_url_redirect", True)
            rules = cfg.get("url_redirect_rules") or []
            action = cfg.get("qr_clipboard_action", "none")
            for text in texts:
                # d1/d2：二维码内容若解析为百度分享链接，只走「分享」链路
                # （记录 + 投递 share_link，由主窗口按 experimental_enabled 与
                # baidu_auto_invoke 决定是否拉起客户端），绝不打开浏览器；分享
                # 链接是给网盘客户端下载整包用的，不是给浏览器看的网页。
                if cfg.get("experimental_enabled") and self._handle_baidu_share(text):
                    continue
                url = self._extract_url(text)
                if not url:
                    # 情况A：无链接（普通内容 / 多链接）。内容里若含提取码，
                    # 同样按设置抬升，否则整段写回。
                    self.hub.log(f"识别到二维码（非 URL）: {text[:60]}")
                    self._write_qr_result(text, action)
                    continue
                is_pure_url = (text.strip() == url
                               or re.match(r"^https?://\S+$", text.strip(), re.I))
                if redirect:
                    new_url = self._redirect_url(url, rules)
                    if new_url != url:
                        self.hub.log(f"二维码链接域名重定向: {url} -> {new_url}")
                        url = new_url
                # 检查点B：打开浏览器前的信任判定
                host = _host_of(url)
                decision, cat = decide_host(cfg, host, "open")
                self._remember_auto_trust(cfg, host, "open")
                if decision == "deny":
                    self.hub.log(f"已阻止打开未信任的网址: {url[:80]}")
                    # 链接被阻止不代表内容无用：非纯链接仍把全部内容写剪贴板
                    if not is_pure_url:
                        self._copy_all_to_clipboard(text)
                    continue
                if decision == "ask":
                    self.hub.log(f"新网址等待确认，暂不打开: {url[:80]}")
                    self._queue_trust_ask(url, host, cat, "open")
                    if not is_pure_url:
                        self._copy_all_to_clipboard(text)
                    continue
                self._open_browser(url)
                if is_pure_url:
                    # 情况B：纯链接 → 按设置做剪贴板联动（含提取码抬升）
                    if action == "code":
                        self._restore_last_text()
                    elif action == "url":
                        if self._set_clipboard(text):
                            self.hub.log(f"已把二维码解码内容写回剪贴板: {text[:40]}")
                else:
                    # 情况C：含链接但不是纯链接。文本里常内嵌提取码
                    #（如「…链接 提取码：Zdjn」）：按设置优先把提取码单独抬升
                    # 到剪贴板（整段文本没法直接粘进网盘密码框），取不到再整段
                    # 写回。
                    self._write_qr_result(text, action)
                break
        except Exception as e:
            self.hub.log(f"二维码解码失败: {e}")

    def _copy_all_to_clipboard(self, text):
        """把二维码全部解码内容写回剪贴板 + 记日志（不做提取码抬升）。

        适用：无链接的普通内容、单二维码多个链接、含链接的混合内容。
        """
        if self._set_clipboard(text):
            self.hub.log(f"已把二维码内容写回剪贴板: {text[:60]}")
        else:
            self.hub.log(f"二维码内容写回剪贴板失败: {text[:60]}")

    def _write_qr_result(self, text, action):
        """按设置把解码文本写回剪贴板。

        action=code 且文本内含提取码 → 只写回提取码（抬升，便于直接粘进网盘
        密码框）；否则整段写回。"""
        code = self._extract_pwd_code(text)
        if action == "code" and code:
            if self._set_clipboard(code):
                self.hub.log(f"已提取并写回提取码: {code}")
                return
        self._copy_all_to_clipboard(text)

    @staticmethod
    def _extract_pwd_code(text):
        """从二维码解码文本里提取内嵌的提取码/密码（如「提取码：Zdjn」→ Zdjn）。

        取不到返回 None。只认关键字后紧跟的短代码，避免误抓整段文本。"""
        if not text:
            return None
        m = re.search(
            r"(?:提取码|访问码|密\s*码|pwd|passcode|password|pass)"
            r"\s*[:：=]?\s*([A-Za-z0-9]{2,16})",
            text, re.I)
        return m.group(1) if m else None

    def _queue_trust_ask(self, url, host, category, purpose):
        """把待用户确认的网址投递给主窗口（可见则弹窗，隐藏则挂起）。"""
        try:
            self.hub.q.put({"type": "url_trust_ask", "url": url,
                            "host": host, "category": category,
                            "purpose": purpose})
        except Exception:
            pass

    def _remember_auto_trust(self, cfg, host, purpose="open"):
        """auto_whitelist / auto_blacklist：把新公网域名写入**该用途**的名单并持久化。

        purpose="open" 写「自动打开」的白/黑名单；purpose="fetch" 写「下载识别」的。"""
        try:
            ut2 = remember_auto_domain(cfg, host, purpose)
        except Exception:
            return
        if ut2 is None:
            return
        try:
            self.state.set("url_trust", ut2)
            sub = ut2.get(purpose)
            mode = sub.get("new_domain_action") if isinstance(sub, dict) else None
            kind = "白名单" if mode == "auto_whitelist" else "黑名单"
            label = "自动打开" if purpose == "open" else "下载识别"
            self.hub.log(f"已自动把新域名加入[{label}]的{kind}: {host}")
        except Exception as e:
            self.hub.log(f"自动信任名单保存失败: {e}")

    def _open_browser(self, url):
        """在默认浏览器打开网址（供信任放行后执行）。"""
        try:
            self.hub.log(f"识别到二维码 URL: {url}，正在打开...")
            webbrowser.open(url, new=2, autoraise=True)
        except Exception as e:
            self.hub.log(f"打开网址失败: {e}")

    def _decode_qr(self, image):
        """把剪贴板图片暂存后交给子进程解码（原生库崩溃不影响主程序）。"""
        import tempfile
        tmp = None
        try:
            with tempfile.NamedTemporaryFile(
                    suffix=".png", delete=False, dir=tempfile.gettempdir()) as f:
                tmp = f.name
            image.save(tmp, format="PNG")
        except Exception as e:
            self.hub.log(f"二维码图片暂存失败: {e}")
            return []
        try:
            return self._decode_qr_file(tmp)
        finally:
            if tmp:
                try:
                    os.unlink(tmp)
                except OSError:
                    pass

    def _decode_qr_file(self, path):
        """把解码工作放到独立子进程执行：cv2/pyzbar/PIL 原生崩溃只杀子进程。"""
        import subprocess
        # 解码期计数只覆盖「子进程运行期」：这里是「图片/路径 → 文本」唯一的公共
        # 瓶颈，剪贴板图片链接与二维码分享两条路径都经此。放这一层而非调用方，是因为
        # 解码完成的信号就在紧随其后的 _handle_decoded_texts/_handle_baidu_share，
        # 把手势落空判定窗口与真正的解码窗口对齐，避免把「解码已结束、记录尚未入库」
        # 误判为「还在解码」。begin/end 成对，finally 保证异常/超时/早退都归还计数。
        self.hub.qr_begin_decode()
        try:
            try:
                proc = subprocess.run(
                    [sys.executable, str(self.qr_worker_path), path],
                    timeout=20, capture_output=True,
                    creationflags=0x08000000,  # CREATE_NO_WINDOW
                )
            except subprocess.TimeoutExpired:
                self.hub.log("二维码解码超时（已终止）")
                return []
            except Exception as e:
                self.hub.log(f"二维码解码子进程启动失败: {e}")
                return []
            if proc.returncode != 0:
                try:
                    err = proc.stderr.decode("utf-8", "replace").strip().splitlines()
                except Exception:
                    err = []
                detail = err[-1] if err else f"退出码 {proc.returncode}"
                kind = ("解码失败" if b"__ERROR__" in (proc.stderr or b"")
                        else "解码进程异常退出（已隔离）")
                self.hub.log(f"二维码{kind}: {detail[:80]}")
                return []
            try:
                out = proc.stdout.decode("utf-8", "replace")
            except Exception:
                out = ""
            return [ln.strip() for ln in out.splitlines() if ln.strip()]
        finally:
            self.hub.qr_end_decode()

    @staticmethod
    def _redirect_url(url, rules):
        """按配置把二维码链接里的域名重定向（如 drive.uc.cn -> fast.uc.cn）。

        只替换主机名，路径 / 查询 / 锚点（#/list/share）原样保留。"""
        if not rules:
            return url
        try:
            from urllib.parse import urlsplit, urlunsplit
            parts = urlsplit(url)
            host = (parts.hostname or "").lower()
            if not host:
                return url
            for rule in rules:
                frm = (rule.get("from") or "").strip().lower()
                to = (rule.get("to") or "").strip()
                if not frm or not to:
                    continue
                if host == frm or host.endswith("." + frm):
                    netloc = to
                    if parts.port:
                        netloc = f"{netloc}:{parts.port}"
                    return urlunsplit(
                        (parts.scheme, netloc, parts.path, parts.query, parts.fragment))
        except Exception:
            pass
        return url

    @staticmethod
    def _extract_url(text):
        """从任意文本里截出第一个真正可用的网址（沿 ASCII URL 字符集切分）。

        关键是**在首个非法字符处截断**：复制「链接 + 空格 + 码：XXXX」时，
        空格与中文都不属于 URL 字符集，因此不会再把「码：XXXX」吞进网址。
        只在此返回 None 表示没有网址。"""
        if not text:
            return None
        m = _URL_RE.search(text)
        if m:
            return m.group(0).rstrip(_URL_TRAILING_JUNK)
        m = _WWW_URL_RE.search(text)
        if m:
            return "http://" + m.group(0).rstrip(_URL_TRAILING_JUNK)
        return None

    # ---------- 网址形式的二维码图片识别 ----------
    def _handle_baidu_share(self, text):
        """2.F：百度分享链接的独立处理入口（**不受**「网址信任」限制）。

        分享链接的用途是「记录 + 把整包交给网盘客户端下载」，而不是「下载该网址
        识别是否二维码图片」，因此**先于**网址信任判定执行：命中即返回 True，
        由本函数负责抓公开分享页（无需登录，含 shareid/share_uk）、记录链接、
        并向主窗口投递 `share_link` 事件（主窗口据 `baidu_auto_invoke` 决定是否
        自动拉起）。非分享链接返回 False，交回原有二维码链路。
        """
        try:
            from . import baidu_task as _bt
            # 剪贴板/二维码里常见「链接 + 空格 + 提取码：XXXX」整段文本。必须**先把
            # 真 URL 截出来**再抓页与记录，原因有二：
            #   1) 直接拿整段去 fetch 会因空格/中文失败 → 拿不到 shareid/share_uk；
            #   2) 整段会被写进 rec["url"]，后续拉起时 prepare_share 把它当 Referer，
            #      请求头里带中文会直接炸。
            # 而提取码必须仍从**整段原文**里找（见下方 _extract_pwd_code(raw)）。
            raw = text or ""
            url = self._extract_url(raw) or raw
            if not _bt.parse_share_url(url):
                return False
            html = ""
            try:
                data = self._fetch_url(url)
                if data:
                    html = data.decode("utf-8", "replace")
            except Exception:
                html = ""      # 抓页失败也继续：拉起本身不需要 shareid/share_uk
            rec = _bt.remember_share_link(url, html)
            if not rec:
                return True    # 已确认是分享链接：按分享处理，不再走二维码
            # 2.F：抓页即判定「链接已失效」——最早的判定点，**零额外请求**。命中即
            # 按分享处理（return True，不再走二维码），但**不**投递 `share_link`
            # 事件：既然已判定失效，就不该再让自动路径去 prepare_share 白白请求一次。
            # 用户手势那条路由 main_window 的失效短路负责（弹 Windows 通知）。
            # 探测器缺失/异常一律按「未失效」处理，绝不打断主流程。
            try:
                dead = _bt.detect_dead_share_page(html)
            except Exception:
                dead = None
            if dead:
                try:
                    _bt.mark_share_dead(rec.get("surl") or url, dead)
                except Exception:
                    pass      # 标记失败只丢失缓存，绝不打断主流程
                self.hub.log(f"{dead}，已跳过：{url}")
                return True
            # d3（收紧）：URL 未带 ?pwd= 时按顺序自动回退解析提取码，命中即原地写
            # 回 rec["pwd"]（remember_share_link 存的正是同一个 dict，其它消费者立即
            # 可见）：1) 文本内嵌（如「…链接 提取码：Zdjn」）→ 2) 仅采用**最近 120 秒内**
            # 复制过的文本（严格时效；拿旧码去 verify 只会白烧唯一一次配额）。
            # **绝不**在此自动套用分享者的固定映射：使用固定码必须由用户显式手势
            # 触发（面板/托盘/热键）。本处只通过 has_map 告知 UI「该分享者配有固定
            # 码」，由 UI 去征询；fresh_code_from_history 缺失或异常时按无码处理。
            # 此处只做快速只读查询，绝不发起网络请求。
            code_source = "url" if rec.get("pwd") else ""
            if not rec.get("pwd"):
                code = self._extract_pwd_code(raw)
                if code:
                    rec["pwd"] = code
                    code_source = "text"
                    self.hub.log(f"分享缺提取码：已按文本内嵌提取码补上 -> {code}")
                else:
                    code = None
                    try:
                        code, _ = _bt.fresh_code_from_history(self._history())
                    except Exception:
                        code = None
                    if code:
                        rec["pwd"] = code
                        code_source = "recent"
                        self.hub.log(f"分享缺提取码：已按最近复制的文本补上 -> {code}")
                    else:
                        self.hub.log("分享缺提取码：未找到可用候选，按无码尝试")
            # 唯一取值 bug 修复（配合 main_window._effective_share_code）：把码的
            # 「来源」落到记录上——"url"/"text" 是权威来源，"recent" 是抓取时按
            # 120s 历史猜的。下游（手动拉起）据此判断是否需要在手势时重新取值，
            # 避免猜测码被黏死后覆盖后来更准的剪贴板码。
            rec["code_source"] = code_source
            self.hub.log(f"已记录分享链接: surl={rec.get('surl')} "
                         f"shareid={rec.get('shareid')} pwd={rec.get('pwd')}")
            try:
                has_map = bool(_bt.mapped_code(rec.get("share_uk")))
            except Exception:
                has_map = False
            try:
                self.hub.q.put({"type": "share_link", "url": rec.get("url"),
                                "surl": rec.get("surl"), "pwd": rec.get("pwd"),
                                "share_uk": rec.get("share_uk"),
                                "has_map": has_map,
                                "code_source": code_source})
            except Exception:
                pass
            sid = str(rec.get("shareid") or "")
            if sid:
                for info in (_bt._TRACK.get("files") or {}).values():
                    if str((info.get("share") or {}).get("shareid") or "") == sid:
                        self.hub.log(f"  ↳ 该分享对应正在下载: {info.get('local_path')}")
                        break
            return True
        except Exception:
            return False

    def _maybe_process_url(self, text, force=False):
        """复制的是 http(s) 网址时，尝试访问：若返回的是二维码图片则下载解码并打开
        （效果等同直接复制二维码图片）。同一网址只尝试一次（force=True 跳过
        去重，供用户信任确认后的放行重试）。

        检查点A：访问前先做信任判定——未信任的网址不发起任何请求，
        黑名单/内置敏感地址静默拒绝，公网新域名投递主窗口询问。"""
        # 剪贴板里复制的往往是「链接 + 空格 + 码：XXXX」整段文本，先截出真正
        # 的网址再访问，否则空格/中文会让 urllib 抛 "URL can't contain control
        # characters"（用户反馈的 drive.uc.cn 提取码场景）。
        if not (text or "").strip().lower().startswith(("http://", "https://")):
            return
        raw = text            # 整段原文（可能含「提取码：XXXX」，分享链路要用）
        url = self._extract_url(text)
        if not url:
            return
        text = url
        if not force and text == self.last_url:
            return
        self.last_url = text
        cfg = self.state.snapshot()
        # 2.F（实验性）：百度分享链接走**独立**链路，先于网址信任判定处理（不受其
        # 限制）。分享链接的用途是「记录并把整包交给网盘客户端下载」，不是「下载来
        # 识别是否二维码图片」，因此不该被 url_trust.fetch 的默认拒绝策略挡住。
        # 整条 2.F 都藏在「实验性功能」总开关之后：没开就完全不捕获分享链接。
        # 传**整段原文**（raw）而非截断后的 url：分享链路要从整段里提取
        # 「提取码：XXXX」，其内部会再自行截出干净 URL 用于抓页与记录
        # （见 _handle_baidu_share）。原先传 text=url 会把提取码整段丢掉。
        if cfg.get("experimental_enabled") and self._handle_baidu_share(raw):
            return
        host = _host_of(text)
        decision, cat = decide_host(cfg, host, "fetch")
        self._remember_auto_trust(cfg, host, "fetch")
        if decision == "deny":
            self.hub.log(f"已阻止访问未信任的网址: {text[:60]}")
            return
        if decision == "ask":
            self.hub.log(f"新网址等待确认，暂不访问: {text[:60]}")
            self._queue_trust_ask(text, host, cat, "fetch")
            return
        try:
            data = self._fetch_url(text)
        except Exception as e:
            self.hub.log(f"网址访问失败（跳过）: {text[:60]} ... {e}")
            return
        if not data:
            self.hub.log(f"网址内容过大或为空（跳过）: {text[:60]}")
            return
        if not self._is_image_bytes(data):
            self.hub.log(f"网址内容不是图片（跳过）: {text[:60]}")
            return
        import tempfile
        tmp = None
        try:
            with tempfile.NamedTemporaryFile(
                    suffix=".img", delete=False, dir=tempfile.gettempdir()) as f:
                f.write(data)
                tmp = f.name
            texts = self._decode_qr_file(tmp)
            if texts:
                self.hub.log(f"网址是二维码图片，解码: {texts[0][:60]}")
            self._handle_decoded_texts(texts)
        except Exception as e:
            self.hub.log(f"网址图片解码失败: {e}")
        finally:
            if tmp:
                try:
                    os.unlink(tmp)
                except OSError:
                    pass

    def _fetch_url(self, url, timeout=8, max_bytes=16 << 20):
        """拉取网址内容（限制大小，超时/超限返回 None）。

        安全措施：
        - HTTPS 证书默认验证（仅当设置开启 tls_skip_verify 才跳过校验）
        - 重定向逐跳校验目标 host：落入用户黑名单/内置敏感类别（内网、
          回环、元数据等）即中止跳转，防止 302 逃逸进内网/云元数据"""
        import ssl
        from urllib.request import (Request, HTTPRedirectHandler,
                                    build_opener, HTTPSHandler)
        from urllib.error import HTTPError
        cfg = self.state.snapshot()
        hub = self.hub

        class _RedirectGuard(HTTPRedirectHandler):
            def redirect_request(self, req, fp, code, msg, headers, newurl):
                new_host = _host_of(newurl)
                d, _ = decide_host(cfg, new_host, "fetch")
                try:
                    ut2 = remember_auto_domain(cfg, new_host, "fetch")
                    if ut2 is not None and hub.state is not None:
                        hub.state.set("url_trust", ut2)
                except Exception:
                    pass
                if d == "deny":
                    try:
                        hub.log(f"已拦截重定向到未信任地址: {newurl[:80]}")
                    except Exception:
                        pass
                    return None
                return super().redirect_request(
                    req, fp, code, msg, headers, newurl)

        req = Request(url, headers={
            "User-Agent": ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                           "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/126.0 Safari/537.36"),
            "Accept": "image/*,*/*;q=0.8",
        })
        if cfg.get("tls_skip_verify", False):
            ctx = ssl._create_unverified_context()
        else:
            ctx = ssl.create_default_context()
        opener = build_opener(_RedirectGuard(),
                              HTTPSHandler(context=ctx))
        try:
            with opener.open(req, timeout=timeout) as resp:
                data = resp.read(max_bytes + 1)
        except ssl.SSLError as e:
            self.hub.log(f"HTTPS 证书校验失败（如确需访问可在设置中允许不验证证书）: {e}")
            return None
        except HTTPError as e:
            # 3xx 被信任拦截（guard 已记录日志）；其余 HTTP 错误按访问失败跳过
            if not (300 <= e.code < 400):
                self.hub.log(f"网址访问失败（HTTP {e.code}）: {url[:60]}")
            return None
        if len(data) > max_bytes:
            return None
        return data

    @staticmethod
    def _is_image_bytes(data):
        """按魔数判断数据是否为常见图片格式（PNG/JPEG/GIF/WebP/BMP/ICO）。"""
        if len(data) < 12:
            return False
        if data[:8] == b"\x89PNG\r\n\x1a\n":
            return True
        if data[:3] == b"\xff\xd8\xff":
            return True
        if data[:4] == b"GIF8":
            return True
        if data[:4] == b"RIFF" and data[8:12] == b"WEBP":
            return True
        if data[:2] == b"BM":
            return True
        if data[:4] == b"\x00\x00\x01\x00":
            return True  # ICO
        return False


