# -*- coding: utf-8 -*-
"""剪贴板/二维码监控（Stage 6b 从 autounpacker.monitors 拆出）。

QRMonitor：剪贴板二维码识别 + 短文本临时密码捕获 + 网址信任门卫（黑白名单判定）；
轮询线程只负责「检测 + 入队」，单独的守护工作线程串行执行网络/子进程/浏览器等阻塞
I/O。本文件持有 QRMonitor 及其独占的探测状态/正则/过滤器；FolderWatcher 见
monitor/watcher.py；旧导入路径由 autounpacker.monitors 兼容 shim 保持可用。
"""
import os
import queue
import re
import sys
import threading
import time
import webbrowser
from collections import deque
from pathlib import Path

from .. import paths
from ..config import get_bool
from ..trust import (_host_of, decide_host, remember_auto_domain)
# 网址边界识别统一走 utils（正向字符集 + 尾部标点规则只有一份）：剪贴板文本
# 「网址 + 中文说明/提取码」的截断见 utils.split_urls 的文档。
from ..utils import (split_urls, is_url_like, is_baidu_pan_url)


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


# ==================== 剪贴板二维码识别 ====================
# 带这些扩展名的文本基本是文件名（如 MAKO202608.jpg / 画面.png），不是提取码
_FILE_EXT_RE = re.compile(
    r"\.(png|jpe?g|gif|bmp|webp|tiff?|svg|mp4|mkv|avi|mov|wmv|flv|"
    r"rar|zip|7z|tar|gz|txt|json|xml|lnk|exe|dll|msi|pdf|db|log|"
    r"vmd|pmx|pmd|fx|fxsub|dds)$", re.I)

# 一眼不是提取码的字符（用 \x22/\x27 写引号，避免转义地狱）：
# 中英文引号/括号。引号/括号包着的整段文本（如「"自动在浏览器中打开（二维码解出
# 的链接）"」）一律不是密码；CJK 表意文字与全角字符**不再**拦截——密码可以是
# 中文（「中文密码」「ＡＢＣ１２３」），有无中文/全角不证明它不是密码。
_NON_PASSWORD_CHARS_RE = re.compile(
    r"[\x22\x27()\[\]{}<>“”‘’（）【】〔〕《》「」『』]")
# 域名样文本（可带路径/查询）：pan.baidu / pan.baidu.com/s/1abc。
# 末段必须是字母 TLD（或 punycode）才像域名，否则 pass1.2 / v1.2.3 这类
# 「点号密码」会被误判成域名；另保留 IPv4 样文本（地址也不是密码）。
_DOMAIN_LIKE_RE = re.compile(
    r"^(?:[A-Za-z0-9_-]+(\.[A-Za-z0-9_-]+)*\.(?:[A-Za-z]{2,}|xn--[A-Za-z0-9-]+)"
    r"|\d{1,3}(?:\.\d{1,3}){3})([/?#].*)?$")
# 纯时间 / ISO-ish 日期：18:56:42 / 2026-09-18 / 2026/09/18
_TIME_LIKE_RE = re.compile(r"^\d{1,2}:\d{2}(:\d{2})?$")
_DATE_LIKE_RE = re.compile(r"^\d{4}[-/.]\d{1,2}[-/.]\d{1,2}$")
# 纯字母/下划线标识符形态（如 First_Project_）。
# 注意：该规则自 2026-09-19 起**不再**参与 _looks_like_non_password 判定
#（MyPassword 是合法密码）；常量保留只为兼容 monitors shim 的再导出面。
_IDENTIFIER_LIKE_RE = re.compile(r"^[A-Za-z_]+$")
# _extract_pwd_code 用：提取码在空白与句读标点处终止（「提取码：abcd。下一句」
# 只取 abcd）；首尾/内侧的中英文引号括号会被剥掉（「提取码："abcd"」→ abcd）。
_PWD_CODE_STOP = r"\s，。！？；：、…"
_PWD_CODE_WRAP = "\"'()[]{}<>“”‘’（）【】〔〕《》「」『』"
_PWD_CODE_RE = re.compile(
    r"(?:提取码|访问码|密\s*码|pwd|passcode|password|pass)"
    r"\s*[:：=]?\s*"
    r"([^" + _PWD_CODE_STOP + r"]{1,32})", re.I)
_PWD_CODE_WRAP_SPLIT_RE = re.compile("[" + re.escape(_PWD_CODE_WRAP) + "]")


def _looks_like_non_password(text):
    """宽松过滤（对应「智能过滤」子项）：只挡「明显不是密码」的文本。

    不含网址判断——网址由更宽松的父项「网址排除」负责。
    保留的判据（每一条都只拦「看着就不像密码」的形态）：
    - 空 / 多行（提取码都是单行）；
    - 含句读标点（，。！？；：、…）→ 是句子不是密码：中文句子必有标点，
      中文密码没有；
    - 含中英文引号/括号 → 引号/括号包着的整段文本一律不是密码；
    - 盘符 / UNC / // 网络路径、含反斜杠的路径样文本；
    - 带常见扩展名的文件名；
    - ≥8 个空白分词 → 标题/长句（真多词口令如「correct horse battery
      staple」只有 4 个词，必须存活）；
    - 去首尾空白后 >128 字符 → 超长文本（全模块唯一的长度上限）；
    - 纯时间（18:56:42）/ ISO-ish 日期（2026-09-18、2026/09/18）；
    - 域名样（pan.baidu、pan.baidu.com/s/1abc，含带路径）。

    刻意**不再**拦截（旧策略会误杀真密码）：CJK 表意文字与全角字符
    （「中文密码」「ＡＢＣ１２３」）、含点号样式（pass1.2）、纯字母下划线串
    （MyPassword）。

    本函数绝不抛异常：任何内部错误一律返回 True（保守地按「不是密码」处理，
    宁可漏记也不把垃圾写进临时密码/密码本）。"""
    try:
        t = "" if text is None else str(text)
        t = t.strip()
        # 引号/括号在「去首尾引号」之前判：带引号的整段文本
        #（如「"自动在浏览器中打开（二维码解出的链接）"」）一律不是密码。
        if _NON_PASSWORD_CHARS_RE.search(t):
            return True
        t = t.strip('"').strip("'").strip()
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
        if len([w for w in re.split(r"\s+", t) if w]) >= 8:
            return True                                       # ≥8 个空白分词 → 标题/长句
        if len(t) > 128:
            return True                                       # 超长文本不是密码（统一上限 128）
        if _TIME_LIKE_RE.match(t) or _DATE_LIKE_RE.match(t):
            return True                                       # 纯时间 / ISO 日期
        if _DOMAIN_LIKE_RE.match(t):
            return True                                       # 域名样（含带路径的链接文本）
        return False
    except Exception:
        return True


def _should_capture_temp_password(text, cfg):
    """剪贴板文本是否记为临时密码（父子两级过滤）。

    - 父（宽松）url_exclude_temp_password：带 :// 的网址不记；关闭则照单全收。
    - 子（宽松）temp_password_filter：在父级基础上再按 _looks_like_non_password
      排除多行/句读/引号括号/路径/文件名/时间日期/域名/≥8 分词/超长等「明显不是
      密码」的文本；长度上限只有 _looks_like_non_password 里那一处（去首尾空白
      后 >128），不再另设 <60 的旧上限。
    子项默认关（配置默认 False）：默认照单全收，只有用户显式开启才过滤。
    本函数绝不抛异常：内部错误按「不记」处理（宁可不捕获，也不污染密码本）。
    """
    try:
        t = "" if text is None else str(text)
        if not get_bool(cfg, "url_exclude_temp_password", True):
            return True                                   # 父关 → 照单全收
        if "://" in t.strip():
            return False                                  # 父级：网址不记
        if get_bool(cfg, "temp_password_filter", False):
            return not _looks_like_non_password(t)        # 子开才过滤（长度上限在 looks 内）
        return True
    except Exception:
        return False


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
                # 分享输入在途计数：处理完（含分享记录写入）才归还；clipcopy 等
                # 不可能产出 share_link 的项不计，见 _share_input_counted。
                if self._share_input_counted(item):
                    self._share_input_end()
                self.task_q.task_done()

    def _enqueue(self, item):
        """把工作项放入 task_q；队列满时淘汰**最旧**的一项，绝不抛异常给调用者。

        队列满时必须在「丢最旧」与「丢最新」之间选。丢最新会掐死用户刚触发的
        操作（刚复制的分享链接、刚确认的信任放行）；而且 last_text/last_hash 是
        在入队之前就推进的，被丢掉之后「再复制一遍」也会被去重跳过、无法补救。
        因此这里淘汰最旧的一项，保证最新的用户操作一定被处理。

        满队列日志只在一次「溢出连续段」里记一条，恢复正常入队后复位标志。"""
        counted = self._share_input_counted(item)
        if counted:
            self._share_input_begin()
        try:
            self.task_q.put_nowait(item)
            self._queue_full_logged = False
            return
        except queue.Full:
            pass
        evicted = False
        try:
            old = self.task_q.get_nowait()   # 淘汰最旧的一项
            self.task_q.task_done()          # 与被淘汰项配平 unfinished_tasks
            if self._share_input_counted(old):
                self._share_input_end()      # 被淘汰项不会再经 worker → 就地归还计数
            self.task_q.put_nowait(item)     # 单消费者只出不进，此处不会再 Full
            evicted = True
        except (queue.Empty, queue.Full):
            if counted:
                self._share_input_end()      # 本项未能入队 → 归还计数（绝不悬空）
        if not self._queue_full_logged:
            self._queue_full_logged = True
            self.hub.log("剪贴板处理队列已满，已淘汰最旧的一项" if evicted
                         else "剪贴板处理队列已满，本次任务已丢弃")

    @staticmethod
    def _share_input_counted(item):
        """该工作项是否计入「分享输入在途」：text / image / 信任放行的抓取。

        clipcopy（日志链接静默复制）不可能产出 share_link，刻意不计——否则一次
        复制就会让手势误以为「有解析在途」而白白等待。绝不抛异常。"""
        try:
            kind = item[0]
            if kind in ("text", "image"):
                return True
            return kind == "grant" and len(item) > 2 and item[2] == "fetch"
        except Exception:
            return False

    def _share_input_begin(self):
        """把「分享输入在途」+1（hub 桩没有该能力时静默跳过）。绝不抛异常。"""
        try:
            fn = getattr(self.hub, "share_input_begin", None)
            if callable(fn):
                fn()
        except Exception:
            pass

    def _share_input_end(self):
        """把「分享输入在途」-1（hub 桩没有该能力时静默跳过）。绝不抛异常。"""
        try:
            fn = getattr(self.hub, "share_input_end", None)
            if callable(fn):
                fn()
        except Exception:
            pass

    def _capture_text_password(self):
        """监控剪贴板文本：按过滤策略存入临时密码；同时记录最近的非图片内容
        到 _recent_texts（供二维码触发后恢复提取码到剪贴板用）。

        长度策略：去首尾空白后 >128 字符一律不捕获（见 _looks_like_non_password）；
        「智能过滤」默认关，开启后按 _should_capture_temp_password 判定。

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

    def feed_image_file(self, path, force=False):
        """把磁盘上的图片文件按「剪贴板图片」同款链路送入二维码识别（公共接口）。

        供外部（主窗口的二维码图片拖放 / 文件投放）调用：只做「读配置 → PIL 打开
        → 复用 _process（md5 去重 + _enqueue(("image", image))）」，不触碰 UI，可在
        非 UI 线程安全调用。默认受 qr_enabled 总开关约束；force=True 时**豁免**该
        总开关（拖入二维码图片专用：用户主动拖进图片即视为明确意图，不受仅针对
        剪贴板/链接识别的开关限制）。路径缺失 / 不可读 / PIL 打不开时记一行日志并
        返回 False。返回值只表示「已受理并交给解码链路」，不代表一定解码出内容。
        任何异常都吞成一行日志，绝不抛出。
        """
        try:
            cfg = self.state.snapshot()
            if not force and not cfg.get("qr_enabled"):
                self.hub.log(
                    "二维码图片投放已忽略：二维码识别开关未开启（qr_enabled=False）")
                return False
        except Exception as e:
            self.hub.log(f"二维码图片投放失败：读取配置出错: {e}")
            return False
        try:
            p = Path(path)
            if not p.is_file():
                self.hub.log(f"二维码图片投放失败：文件不存在或不可读: {path}")
                return False
            from PIL import Image
            image = Image.open(str(p))
            image.load()          # 强制解码，非图片会在此抛出并被下面捕获
        except Exception as e:
            self.hub.log(f"二维码图片投放失败：无法打开图片 {path}: {e}")
            return False
        try:
            # 与剪贴板图片完全同一条链路：md5 去重 + _enqueue(("image", image))，
            # 解码由工作线程 _decode_qr + _handle_decoded_texts 统一完成。
            self._process(image)
        except Exception as e:
            self.hub.log(f"二维码图片投放失败：入队出错: {e}")
            return False
        return True

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
        浏览器，投递到主窗口询问；黑名单/内置敏感地址直接静默拒绝。

        UX-4（实验性）：开总开关后 pan.baidu（含 yun/eyun 子域）一律静默跳过
        （不进信任询问、不在浏览器打开），只记一行说明。"""
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
                # UX-4（实验性）：pan.baidu（含 yun/eyun 子域）一律走静默通道
                # （分享链路 / 客户端）——不进信任询问、不在浏览器打开。
                # 未开实验性或非百度网址时行为完全不变。
                if cfg.get("experimental_enabled") and is_baidu_pan_url(url):
                    self.hub.log(
                        "已开启实验性：pan.baidu 网址改走静默通道，不在浏览器打开")
                    continue
                is_pure_url = is_url_like(text)
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

        取不到返回 None。关键字后取一段连续文本：在空白与句读标点
        （，。！？；：、…）处终止，长度限 1~32；首尾及内侧的中英文引号/括号
        与尾部 ASCII 标点会被去掉（如「提取码："abcd"」「提取码：abcd（备注）」
        → abcd），绝不把标点或下一句吞进提取码。ASCII 提取码（Zdjn / 4a9u）
        与中文/全角提取码（中文密码 / ＡＢＣ１２３）都支持。"""
        if not text:
            return None
        m = _PWD_CODE_RE.search(text)
        if not m:
            return None
        code = m.group(1).strip(_PWD_CODE_WRAP)
        code = _PWD_CODE_WRAP_SPLIT_RE.split(code, maxsplit=1)[0]
        code = code.rstrip(".,;:!?").rstrip(_PWD_CODE_WRAP)
        return code or None

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

    def _baidu_open_blocked(self, url):
        """实验性模式下的「显式打开 pan.baidu」封禁判定（True=已拦截并记一行）。

        这是总开关授予的第二条：静默权限换显式打开封禁。与信任流程无关；
        配置/状态读不到或异常时一律按「不拦截」处理，绝不打断既有流程。"""
        try:
            if not is_baidu_pan_url(url):
                return False
            if not self.state.snapshot().get("experimental_enabled"):
                return False
        except Exception:
            return False
        self.hub.log("已开启实验性：pan.baidu 网址改走静默通道，不在浏览器打开")
        return True

    def _open_browser(self, url):
        """在默认浏览器打开网址（供信任放行后执行）。

        UX-4：实验性开启时 pan.baidu 显式打开被总开关封禁（静默通道），
        只记一行说明、绝不调用 webbrowser。"""
        try:
            if self._baidu_open_blocked(url):
                return
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
                stderr = proc.stderr or b""
                try:
                    err = stderr.decode("utf-8", "replace").strip().splitlines()
                except Exception:
                    err = []
                detail = err[-1] if err else f"退出码 {proc.returncode}"
                # qr_worker.py 实际输出 __OPEN_ERROR__（图片打开失败，退出码 3）与
                # __DECODE_ERROR__（解码失败，退出码 4）；二者都不含子串 __ERROR__，
                # 旧判定会把两种真实原因都误报成「进程异常退出」。这里按真实标记区分，
                # 并把标记前缀从用户可见文案里去掉（保留子进程的原始错误详情）。
                if b"__OPEN_ERROR__" in stderr:
                    kind, marker = "图片打开失败", "__OPEN_ERROR__"
                elif b"__DECODE_ERROR__" in stderr:
                    kind, marker = "解码失败", "__DECODE_ERROR__"
                else:
                    kind, marker = "解码进程异常退出（已隔离）", ""
                if marker:
                    detail = detail.replace(marker, "", 1).strip() or detail
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
        """从任意文本里截出第一个真正可用的网址（统一走 utils.split_urls 边界）。

        关键是**在首个非法字符处截断**：复制「链接 + 空格 + 码：XXXX」或
        「链接（中文说明）」时，空格与中文都不属于 URL 字符集，因此不会把
        说明文字吞进网址。语义冻结：优先 http(s) 链接；只有 www. 开头时
        补 "http://" 前缀后返回。只在此返回 None 表示没有网址。"""
        if not text:
            return None
        spans = split_urls(text)
        for _s, _e, url in spans:
            if url.lower().startswith("http"):
                return url
        for _s, _e, url in spans:
            if url.lower().startswith("www."):
                return "http://" + url
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
            from .. import baidu_task as _bt
            # 剪贴板/二维码里常见「链接 + 空格 + 提取码：XXXX」整段文本。必须**先把
            # 真 URL 截出来**再抓页与记录，原因有二：
            #   1) 直接拿整段去 fetch 会因空格/中文失败 → 拿不到 shareid/share_uk；
            #   2) 整段会被写进 rec["url"]，后续拉起时 prepare_share 把它当 Referer，
            #      请求头里带中文会直接炸。
            # 而提取码必须仍从**整段原文**里找（见下方 _extract_pwd_code(raw)）。
            raw = text or ""
            url = self._extract_url(raw) or raw
            p_share = _bt.parse_share_url(url)
            if not p_share:
                return False
            # 原子配对（D）：本链接入库**之前**先取「最近一条分享记录」的时间戳，
            # 作为本轮补码的下界——比它还早的提取码一律不再参与，避免把旧码
            # 套到新链接上（errno=-9 的根因之一）。排除**本 surl**（同一链接被
            # 用户重新复制/再次解析时，旧记录正是在被本次解析覆盖的那条，不能
            # 拿它当「上一条」——否则同一时刻抓到的码会被同刻时间戳误拒）。
            try:
                prev_share_ts = _bt.latest_share_ts(
                    exclude_surl=p_share.get("surl"))
            except Exception:
                prev_share_ts = 0.0
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
                        try:
                            # 收紧：只认「晚于上一条分享记录」的码（since_ts）。
                            code, _ = _bt.fresh_code_from_history(
                                self._history(), since_ts=prev_share_ts)
                        except TypeError:
                            # 兼容只接受旧签名 (history[, ttl, now]) 的替身：
                            # 退回旧调用，语义不变（since_ts 是可选增强）。
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
        黑名单/内置敏感地址静默拒绝，公网新域名投递主窗口询问。

        UX-4（实验性）：开总开关后 pan.baidu 一律静默（不询问信任、不抓页、
        不在浏览器打开），只记一行说明。"""
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
        # UX-4（实验性）：pan.baidu（含 yun/eyun 子域）一律静默——不进信任询问、
        # 不抓页、不在浏览器打开。未开实验性或非百度网址时行为完全不变。
        if cfg.get("experimental_enabled") and is_baidu_pan_url(text):
            self.hub.log("已开启实验性：pan.baidu 网址改走静默通道，不在浏览器打开")
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

    # 公共别名（契约冻结）：外部按 QRMonitor.is_image_bytes(bytes) 调用；旧私有名
    # _is_image_bytes 继续可用（内部与既有测试依赖），保持原实现不动。
    is_image_bytes = _is_image_bytes
