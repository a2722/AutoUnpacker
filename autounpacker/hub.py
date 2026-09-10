# -*- coding: utf-8 -*-
"""Hub：后台线程 → GUI 的消息队列 + 日志落盘；StdoutCapture 把 print 转发到 Hub。"""
import io
import queue
import sys
import threading
import time

from . import paths

# 日志防爆保护：
# - LOG_MAX_BYTES：单日日志文件达到该字节数后停止追加（防止失控循环写爆磁盘）
# - LOG_MAX_LINE：单条日志最长字符数（防超长内容一次写爆）
LOG_MAX_BYTES = 200 * 1024 * 1024   # 200 MB / 天
LOG_MAX_LINE = 4096

class Hub:
    """后台线程 → GUI 的消息队列，同时把日志写入持久化文件（logs\YYYY-MM-DD.log）便于排查。

    通知按类型可控：notify_enabled 总开关 + 各类型单独开关（见 NOTIFY_KEYS）。
    """

    # 通知标题 -> 对应配置开关
    NOTIFY_KEYS = {
        "发现压缩包": "notify_archive",
        "智能解压完成": "notify_success",
        "智能解压失败": "notify_failure",
        "智能解压出错": "notify_error",
    }

    def __init__(self, state=None):
        self.q = queue.Queue()
        # 网址信任放行队列：主窗口用户决策「允许」后投递 (url, purpose)，
        # QRMonitor 循环读取并执行（与 self.q 分离，避免双读者竞态）
        self.url_grant_q = queue.Queue()
        self.state = state
        self._log_lock = threading.Lock()
        self._cleanup_old_logs()

    @staticmethod
    def _cleanup_old_logs():
        """删除 14 天前的日志文件，防止无限累积"""
        try:
            d = paths.LOGS_DIR
            if not d.exists():
                return
            cutoff = time.time() - 14 * 86400
            for p in d.glob("*.log"):
                try:
                    if p.stat().st_mtime < cutoff:
                        p.unlink()
                except OSError:
                    pass
        except Exception:
            pass

    def _write_file(self, line):
        try:
            d = paths.LOGS_DIR
            d.mkdir(exist_ok=True)
            fp = d / f"{time.strftime('%Y-%m-%d')}.log"
            # 单日日志上限：防止任何失控的 print/日志循环（如 stdout 捕获
            # 自我喂入、第三方库刷屏）无限写盘撑爆磁盘。达到上限后停止追加。
            try:
                if fp.exists() and fp.stat().st_size >= LOG_MAX_BYTES:
                    return
            except OSError:
                pass
            line = line[:LOG_MAX_LINE]   # 单行截断（防超长内容一次写爆）
            with self._log_lock:
                with open(fp, "a", encoding="utf-8") as f:
                    f.write(line + "\n")
        except Exception:
            pass

    def log(self, msg):
        self._write_file(f"[{time.strftime('%Y-%m-%d %H:%M:%S')}] {msg}")
        try:
            self.q.put({"type": "log", "msg": f"[{time.strftime('%H:%M:%S')}] {msg}"})
        except Exception:
            pass

    def notify(self, title, msg):
        # 日志始终记录（便于排查），弹窗通知按开关过滤
        self._write_file(f"[{time.strftime('%Y-%m-%d %H:%M:%S')}] [通知] {title}: {msg}")
        if self.state is not None:
            cfg = self.state.snapshot()
            if not cfg.get("notify_enabled", True):
                return
            key = self.NOTIFY_KEYS.get(title)
            if key and not cfg.get(key, True):
                return
        try:
            self.q.put({"type": "notify", "title": title, "msg": msg})
        except Exception:
            pass


class StdoutCapture:
    """把 print 转发到 Hub 的进程级 stdout 包装。

    背景：程序常用 pythonw.exe 启动（无控制台），extract.py 里大量面向
    用户的 print 需要被转发进 GUI 日志框，因此必须替换 sys.stdout。

    为避免"把全局状态当自家后院"，本类尽量表现得像一个正常的文本流
    （isatty / fileno / encoding / writable，且 write 返回写入长度），并给
    缓冲区加锁（多个线程 print 不会串错行）。安装 / 还原见
    ``install_stdout_capture`` / ``restore_stdout_capture``。
    """

    encoding = "utf-8"
    errors = "replace"

    def __init__(self, hub, original=None):
        self.hub = hub
        self._original = original
        self._buffer = ""
        self._lock = threading.Lock()
        # 每线程重入标志：日志路径里若又触发 print（自我喂入），
        # 第二次 write 直接丢弃，既不成环也不因锁（不可重入）死锁。
        self._in_hook = threading.local()

    # ---- 文本流协议：避免第三方库调用这些方法时抛 AttributeError ----
    def isatty(self):
        return False

    def writable(self):
        return True

    def readable(self):
        return False

    def fileno(self):
        # 需要真实 fd 的库退回原始 stdout；没有原始流则明确报不支持
        if self._original is not None:
            return self._original.fileno()
        raise io.UnsupportedOperation("fileno")

    @property
    def closed(self):
        return False

    def write(self, s):
        if not isinstance(s, str):
            s = str(s)
        # 防重入：本线程已在日志钩子内（写日志时又 print）→ 丢弃，避免死锁/成环
        if getattr(self._in_hook, "active", False):
            return len(s)
        self._in_hook.active = True
        try:
            with self._lock:
                self._buffer += s
                while "\n" in self._buffer:
                    line, self._buffer = self._buffer.split("\n", 1)
                    line = line.strip()
                    if line:
                        self.hub.log(line[:LOG_MAX_LINE])
        finally:
            self._in_hook.active = False
        return len(s)

    def flush(self):
        if getattr(self._in_hook, "active", False):
            return
        self._in_hook.active = True
        try:
            with self._lock:
                if self._buffer.strip():
                    self.hub.log(self._buffer.strip())
                    self._buffer = ""
        finally:
            self._in_hook.active = False


def install_stdout_capture(hub):
    """幂等地把 sys.stdout 换成 StdoutCapture，返回该实例。

    只应在进程入口（app.main）调用一次；若 stdout 已被包装则不二次包装，
    避免"越包越多"。原始 stdout 保存在实例里，可用 restore 还原。
    """
    if isinstance(sys.stdout, StdoutCapture):
        return sys.stdout
    cap = StdoutCapture(hub, sys.stdout)
    sys.stdout = cap
    return cap


def restore_stdout_capture():
    """还原到安装前的原始 stdout（若不是本模块安装的则不动）。"""
    if isinstance(sys.stdout, StdoutCapture):
        sys.stdout = sys.stdout._original


