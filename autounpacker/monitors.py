# -*- coding: utf-8 -*-
"""后台监控线程：
- FolderWatcher：监听目录轮询，智能解压（嵌套/密码/分卷/伪装/隐写/删除回溯）
- QRMonitor：剪贴板监控（二维码识别 + 短文本临时密码捕获 + 网址信任门卫）
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
from .hub import StdoutCapture
from .trust import (_host_of, decide_host)
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
    TRANSLATION_MAX_SIZE = 10 * 1024 * 1024  # 翻译 json 最大 10MB
    TRANSLATION_WINDOW = 5 * 60              # 小文件夹先出现时的监控窗口（秒）
    VOL_MAX_WAIT = 300   # 首卷分卷"只有满卷"时最长等待(秒)：之后兜底按现状尝试解压

    def __init__(self, state, hub, pauser=None):
        super().__init__(daemon=True)
        self.state = state
        self.hub = hub
        self.pauser = pauser
        self.seen = {}
        # 已处理过的文件身份 {norm_path: (size, mtime)}：同名但内容不同的
        # 新文件（重新下载/替换）不会被误当成已处理而跳过
        self.traced = {}
        self.probing = {}  # {watch_key: {name: 剩余复查轮数}}
        # 翻译文件监控窗口 {小文件夹路径: (json_stem, 首次发现时间)}
        self._pending_trans = {}
        # 首卷分卷观察期 {abs_path: {seen, last_activity, stable_since}}
        self._vol_wait = {}
        # 输出文件被占用的重试计数 {abs_path: 次数}（上限 3 次，防死循环）
        self._lock_retry = {}

    @staticmethod
    def _file_identity(path):
        """文件身份 = (大小, 最后修改时间)。失败返回 None。"""
        try:
            st = Path(path).stat()
            return (st.st_size, st.st_mtime)
        except OSError:
            return None

    @staticmethod
    def _is_same_traced(old_ident, ident):
        return old_ident is not None and old_ident == ident

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
        sys.stdout = StdoutCapture(self.hub)
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
        # 翻译 JSON 归位检查（基于根目录下的文件夹，与文件监听相互独立）
        self._translation_check(watch)
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
            return
        try:
            current = set(n for n in os.listdir(watch) if (watch / n).is_file())
        except OSError:
            return
        new = current - self.seen[key]
        deferred = set()
        probe = self.probing.setdefault(key, {})
        # 先复查上一轮进入观察期的文件（这些已在 seen 中）：写入完成后会被
        # 重新识别为压缩包并处理
        for name in list(probe):
            if name not in current:
                probe.pop(name, None)
                continue
            res = self._handle(watch / name, wc)
            if res == "defer":
                probe.pop(name, None)
                deferred.add(name)
            elif res == "done":
                probe.pop(name, None)
            else:  # 仍不是压缩包
                probe[name] -= 1
                if probe[name] <= 0:
                    probe.pop(name, None)  # 观察期结束，放弃（留在 seen）
        # 再处理新出现的文件
        for name in sorted(new):
            res = self._handle(watch / name, wc)
            if res == "defer":
                deferred.add(name)
            elif res == "skip":
                # 暂未识别为压缩包：可能正被写入/复制（尾部还没写完），
                # 进入观察期复查几轮，避免一次性吸收后永不处理。
                probe[name] = self.PROBE_CYCLES
        # 分卷未到齐被推迟的文件不记入 seen，下轮会重新检查
        self.seen[key] = current - deferred

    # ---------- 翻译 JSON 归位 ----------
    def _translation_check(self, watch):
        """翻译 JSON 自动归位：单 json 小文件夹(<10MB) 的 json 文件名命中某大文件夹名
        （文件名是大文件夹名的子串）时，把 json 移入大文件夹。

        两者出现顺序不定：
        - 大文件夹先到：小文件夹一出现就归位；
        - 小文件夹先到：进入 5 分钟监控窗口，期间大文件夹出现即归位，超时放弃。"""
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
                continue
            if now - first_seen > self.TRANSLATION_WINDOW:
                self.hub.log(f"翻译文件监控超时（5 分钟内未等到目标文件夹），放弃: {p.name}")
                self._pending_trans.pop(path, None)
                continue
            target = self._find_translation_target(watch, stem, p)
            if target:
                self._pending_trans.pop(path, None)
                self._move_translation(p, stem, target)
        # 本轮出现的候选：能立刻找到目标就归位，否则进入监控窗口
        for d, (stem, _) in candidates.items():
            if not d.is_dir():
                continue
            target = self._find_translation_target(watch, stem, d)
            if target:
                self._move_translation(d, stem, target)
            elif str(d) not in self._pending_trans:
                self._pending_trans[str(d)] = (stem, time.time())
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

    def _handle(self, fp, wc, traced=False, initial_scan=False):
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
            promote_to = out_dir or (wc.get("path") or "").strip() or None
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
class QRMonitor(threading.Thread):
    """剪贴板监控线程：二维码识别（可选） + 短文本临时密码捕获"""

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

    def run(self):
        while True:
            cfg = self.state.snapshot()
            # 处理用户信任确认后的放行请求（主窗口决策回调写入 url_grant_q）
            while True:
                try:
                    gurl, gpurpose = self.hub.url_grant_q.get_nowait()
                except queue.Empty:
                    break
                try:
                    if gpurpose == "fetch":
                        self._maybe_process_url(gurl, force=True)
                    else:
                        self._open_browser(gurl)
                except Exception as e:
                    self.hub.log(f"信任放行执行失败: {e}")
            # 暂停 = 原「停止监听」：剪贴板/二维码监控一并停止
            if self.state.running and not (self.pauser is not None
                                           and self.pauser.is_paused()):
                try:
                    new_text = self._capture_text_password()
                    if cfg.get("qr_url_enabled") and QR_AVAILABLE and new_text:
                        self._maybe_process_url(new_text)
                    if cfg.get("qr_enabled") and QR_AVAILABLE and CLIPBOARD_AVAILABLE:
                        wc, ig = _ensure_clipboard()
                        if wc is not None and ig is not None:
                            if wc.IsClipboardFormatAvailable(wc.CF_DIB):
                                image = ig.grabclipboard()
                                if image is not None:
                                    self._process(image)
                except Exception as e:
                    self.hub.log(f"剪贴板监控出错: {e}")
            time.sleep(0.5)

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
            if not text or text == self.last_text:
                return None
            self.last_text = text
            self._recent_texts.append(text[:200])
            cfg = self.state.snapshot()
            if len(text) < 60:
                # 文件路径（盘符路径/UNC）不是提取码：复制文件时剪贴板会被
                # 塞进路径，不记录为临时密码（避免污染临时/长期密码本）。
                if not (re.match(r"^[A-Za-z]:[\\/]", text)
                        or text.startswith("\\\\")):
                    # 带 :// 的网址不是提取码，不记临时密码（可开关）。
                    # xxxx.com / xxxx.top 这类无协议头的域名形式照常记录。
                    if not (cfg.get("url_exclude_temp_password", True) and "://" in text):
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

    def _restore_last_text(self):
        """把最近捕获的非图片剪贴板内容（如提取码）写回剪贴板，方便直接 Ctrl+V。"""
        while self._recent_texts:
            text = self._recent_texts[-1]
            self._recent_texts.pop()
            if not text:
                continue
            self.last_text = text  # 避免下一轮把它当新内容重复记录
            if self._set_clipboard(text):
                self.hub.log(f"已把最近的非图片复制内容写回剪贴板: {text[:40]}")
                return
        self.hub.log("没有可恢复的最近文本")

    def _process(self, image):
        import hashlib
        from io import BytesIO
        buf = BytesIO()
        image.save(buf, format="PNG")
        h = hashlib.md5(buf.getvalue()).hexdigest()
        if h == self.last_hash:
            return
        self.last_hash = h
        texts = self._decode_qr(image)
        self._handle_decoded_texts(texts)

    def _handle_decoded_texts(self, texts):
        """对解码出的文本做统一处理：识别 URL → 重定向 → 信任判定 → 打开浏览器 → 剪贴板联动。

        信任判定（decide_host）在重定向之后执行：未信任的域名不自动打开
        浏览器，投递到主窗口询问；黑名单/内置敏感地址直接静默拒绝。"""
        try:
            cfg = self.state.snapshot()
            redirect = cfg.get("qr_url_redirect", True)
            rules = cfg.get("url_redirect_rules") or []
            action = cfg.get("qr_clipboard_action", "none")
            for text in texts:
                url = self._extract_url(text)
                if not url:
                    self.hub.log(f"识别到二维码（非 URL）: {text[:60]}")
                    continue
                if redirect:
                    new_url = self._redirect_url(url, rules)
                    if new_url != url:
                        self.hub.log(f"二维码链接域名重定向: {url} -> {new_url}")
                        url = new_url
                # 检查点B：打开浏览器前的信任判定
                host = _host_of(url)
                decision, cat = decide_host(cfg, host)
                if decision == "deny":
                    self.hub.log(f"已阻止打开未信任的网址: {url[:80]}")
                    continue
                if decision == "ask":
                    self.hub.log(f"新网址等待确认，暂不打开: {url[:80]}")
                    self._queue_trust_ask(url, host, cat, "open")
                    continue
                self._open_browser(url)
                # 剪贴板联动：按设置把「最近的非图片内容(提取码)」或
                # 「二维码解码内容」写回剪贴板，方便直接 Ctrl+V
                if action == "code":
                    self._restore_last_text()
                elif action == "url":
                    if self._set_clipboard(text):
                        self.hub.log(f"已把二维码解码内容写回剪贴板: {text[:40]}")
                break
        except Exception as e:
            self.hub.log(f"二维码解码失败: {e}")

    def _queue_trust_ask(self, url, host, category, purpose):
        """把待用户确认的网址投递给主窗口（可见则弹窗，隐藏则挂起）。"""
        try:
            self.hub.q.put({"type": "url_trust_ask", "url": url,
                            "host": host, "category": category,
                            "purpose": purpose})
        except Exception:
            pass

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
        import re
        if re.match(r"^https?://\S+$", text, re.I):
            return text
        m = re.search(r"(https?://\S+|www\.\S+\.\S+)", text, re.I)
        if m:
            url = m.group(1)
            return url if url.startswith(("http://", "https://")) else "http://" + url
        return None

    # ---------- 网址形式的二维码图片识别 ----------
    def _maybe_process_url(self, text, force=False):
        """复制的是 http(s) 网址时，尝试访问：若返回的是二维码图片则下载解码并打开
        （效果等同直接复制二维码图片）。同一网址只尝试一次（force=True 跳过
        去重，供用户信任确认后的放行重试）。

        检查点A：访问前先做信任判定——未信任的网址不发起任何请求，
        黑名单/内置敏感地址静默拒绝，公网新域名投递主窗口询问。"""
        if not text.startswith(("http://", "https://")):
            return
        if not force and text == self.last_url:
            return
        self.last_url = text
        cfg = self.state.snapshot()
        host = _host_of(text)
        decision, cat = decide_host(cfg, host)
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
                d, _ = decide_host(cfg, new_host)
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


