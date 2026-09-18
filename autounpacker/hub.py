# -*- coding: utf-8 -*-
"""Hub 消息中枢：后台线程 → GUI 的队列 + 日志落盘 + log_index 双写；StdoutCapture 把 print 转发进 GUI。

职责：- Hub.log()/notify() 投递日志与通知到队列、写 logs\\YYYY-MM-DD.log，并写 toolbox.db 的 log_index
- 线程级日志上下文（set_log_context/clear_log_context/current_log_context）：解压线程
  里 print 出的行自动带上 source_dir / task_id，无需逐处透传参数
- guess_level()：日志文本 → error/success/wait/info（与主窗口日志着色同一套关键词）
- LogRecord / emit_record()：带 task_id / source_dir 的结构化日志入口
- 单日日志防爆（200MB 上限 / 单行截断）+ 14 天旧日志清理
- notify 按类型开关过滤（NOTIFY_KEYS 映射配置项），同时把通知文本入 log_index
- StdoutCapture 幂等包装 sys.stdout，把 pythonw 下的 print 转发为 Hub 日志
关键入口：Hub / LogRecord / guess_level() / set_log_context() / install_stdout_capture()
依赖：paths.LOGS_DIR、db、queue/threading
注意：不引入 Qt；队列 + 定时 drain 的线程模型不变，_drain 仍是唯一 fan-out 点
"""
import io
import queue
import sys
import threading
import time
from dataclasses import dataclass

from . import db
from . import paths

# 日志防爆保护：
# - LOG_MAX_BYTES：单日日志文件达到该字节数后停止追加（防止失控循环写爆磁盘）
# - LOG_MAX_LINE：单条日志最长字符数（防超长内容一次写爆）
LOG_MAX_BYTES = 200 * 1024 * 1024   # 200 MB / 天
LOG_MAX_LINE = 4096


@dataclass
class LogRecord:
    """一条结构化日志（落盘 / 入队 / 写 log_index 三处共用同一份字段）。"""
    ts: int
    level: str                  # 'error'|'success'|'wait'|'info'|'link'
    text: str
    source_dir: str | None = None      # None = 全局日志
    task_id: int | None = None
    link: str | None = None


# 线程级日志上下文：FolderWatcher 在单个守护线程里同步解压，解压前后 set/clear，
# 期间该线程 print 出的每一行经 StdoutCapture → Hub.log() 都能归属到目录 / 任务。
_log_ctx = threading.local()


def set_log_context(source_dir=None, task_id=None):
    """设置当前线程的日志上下文（监听路径 / 任务 id），供 Hub.log 缺省取值。"""
    _log_ctx.source_dir = source_dir
    _log_ctx.task_id = task_id


def clear_log_context():
    """清除当前线程的日志上下文（任务结束即调，防止污染后续解压）。"""
    for attr in ("source_dir", "task_id"):
        try:
            delattr(_log_ctx, attr)
        except AttributeError:
            pass


def current_log_context():
    """返回当前线程的 (source_dir, task_id)；未设置时为 (None, None)。"""
    return (getattr(_log_ctx, "source_dir", None),
            getattr(_log_ctx, "task_id", None))


def guess_level(text):
    """按关键词把日志文本分级：error / success / wait / info（默认 info）。

    与 MainWindow._log_color_for 的判定顺序完全一致（错误 > 成功 > 信息 > 等待），
    只是把颜色换成级别名，供 log_index 与新版日志页过滤使用。
    """
    m = text or ""
    if "失败" in m or "出错" in m or "错误" in m:
        return "error"
    if "完成" in m or "成功" in m or "开始监听" in m:
        return "success"
    if ("发现压缩包" in m or "开始智能解压" in m or "已捕获临时密码" in m
            or "识别到二维码" in m or "正在打开" in m or "归位" in m
            or "翻译" in m or "网址" in m):
        return "info"
    if ("分卷" in m or "下载未完成" in m or "密码" in m
            or "超时" in m or "监控" in m or "等待" in m):
        return "wait"
    return "info"


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
        # 实验性：百度网盘任务库（A/B/C/D）
        "网盘下载完成": "notify_baidu_done",
        "网盘任务未完成": "notify_baidu_leftover",
        "网盘重复下载": "notify_baidu_dup",
    }

    def __init__(self, state=None):
        self.q = queue.Queue()
        # 网址信任放行队列：主窗口用户决策「允许」后投递 (url, purpose)，
        # QRMonitor 循环读取并执行（与 self.q 分离，避免双读者竞态）
        self.url_grant_q = queue.Queue()
        # 静默复制队列：主窗口点击日志里的链接后投递 url，QRMonitor 循环读取并
        # 「先置位 last_text 再写剪贴板」，使这次自写不被本程序当成新输入处理
        # （仿 url_grant_q：与 self.q 分离，避免双读者竞态）
        self.clip_echo_q = queue.Queue()
        # 二维码解码期计数：QRMonitor（工作线程）在解码子进程运行期间加一，
        # GUI 手势可据此判断「当前确实正在解码」→ 登记一次性预定任务；
        # 两侧只持有同一个 hub，跨线程共享状态挂在这里（不新增消息类型）。
        self._qr_lock = threading.Lock()
        self._qr_decoding = 0
        # 分享输入「在途」计数：QRMonitor 把 text/image/放行抓取工作项入队时 +1，
        # 工作线程处理完该项（含分享记录写入）后 -1。手势据此判断「当前有输入正在
        # 解析」→ 登记一次性意图，避免按压时直接拉起上一条分享（详见 pending 方法）。
        # 与 _qr_decoding 相互独立：解码期只是整条链路的中间一小段。
        self._share_lock = threading.Lock()
        self._share_inputs = 0
        self.state = state
        self._log_lock = threading.Lock()
        self._cleanup_old_logs()

    def set_log_context(self, source_dir=None, task_id=None):
        """透传模块级线程日志上下文（见 set_log_context()）。"""
        set_log_context(source_dir, task_id)

    def clear_log_context(self):
        """清除当前线程的日志上下文（解压结束 / 监控线程退出前调用）。"""
        clear_log_context()

    def current_log_context(self):
        """返回当前线程的 (source_dir, task_id)；未设置时为 (None, None)。"""
        return current_log_context()

    def qr_begin_decode(self):
        """进入二维码解码期（子进程运行期）+1。"""
        with self._qr_lock:
            self._qr_decoding += 1

    def qr_end_decode(self):
        """退出二维码解码期 -1（最小钳到 0，防止异常路径把计数拉成负数）。"""
        with self._qr_lock:
            self._qr_decoding = max(0, self._qr_decoding - 1)

    def qr_decoding_active(self):
        """当前是否处于二维码解码期（有解码子进程正在运行）。"""
        with self._qr_lock:
            return self._qr_decoding > 0

    def share_input_begin(self):
        """进入「分享输入在途」期（工作项入队）+1。绝不抛异常。"""
        try:
            with self._share_lock:
                self._share_inputs += 1
        except Exception:
            pass

    def share_input_end(self):
        """退出「分享输入在途」期 -1（最小钳到 0，防止异常路径把计数拉成负数）。"""
        try:
            with self._share_lock:
                self._share_inputs = max(0, self._share_inputs - 1)
        except Exception:
            pass

    def share_input_pending(self):
        """当前是否有分享输入（文本/图片/放行抓取）尚未处理完。绝不抛异常。"""
        try:
            with self._share_lock:
                return self._share_inputs > 0
        except Exception:
            return False

    @staticmethod
    def _cleanup_old_logs():
        """删除 14 天前的日志文件，并同步清理 log_index 旧行，防止无限累积"""
        cutoff = time.time() - 14 * 86400
        # log_index 跟随日志文件的 14 天策略同步删除；DB 打嗝绝不影响 Hub 构造。
        try:
            db.prune_logs(int(cutoff))
        except Exception:
            pass
        try:
            d = paths.LOGS_DIR
            if not d.exists():
                return
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

    def log(self, msg, source_dir=None, task_id=None, level=None, link=None):
        """写一行日志：落盘 + 入 GUI 队列 + 写 log_index（source_dir 为 NULL=全局）。

        未显式给 source_dir/task_id 时，从当前线程的日志上下文取（解压线程由
        monitors 在解压前后设置 / 清除），使 extract 里 print 出的行自动归属任务。
        """
        if source_dir is None or task_id is None:
            csd, ctid = current_log_context()
            if source_dir is None:
                source_dir = csd
            if task_id is None:
                task_id = ctid
        lv = level or guess_level(msg)
        now = int(time.time())
        self._write_file(f"[{time.strftime('%Y-%m-%d %H:%M:%S')}] {msg}")
        try:
            self.q.put({"type": "log", "msg": f"[{time.strftime('%H:%M:%S')}] {msg}",
                        "text": msg, "ts": now, "level": lv,
                        "source_dir": source_dir, "task_id": task_id, "link": link})
        except Exception:
            pass
        try:
            db.add_log_index(now, lv, msg, source_dir, task_id, link)
        except Exception:
            pass

    def emit_record(self, rec):
        """按调用方给定的 LogRecord 落盘 + 入队 + 写 log_index（原样使用 rec.ts）。"""
        try:
            self._write_file(
                f"[{time.strftime('%Y-%m-%d %H:%M:%S')}] {rec.text}")
        except Exception:
            pass
        try:
            self.q.put({"type": "log",
                        "msg": f"[{time.strftime('%H:%M:%S')}] {rec.text}",
                        "text": rec.text, "ts": rec.ts, "level": rec.level,
                        "source_dir": rec.source_dir, "task_id": rec.task_id,
                        "link": rec.link})
        except Exception:
            pass
        try:
            db.add_log_index(rec.ts, rec.level, rec.text, rec.source_dir,
                             rec.task_id, rec.link)
        except Exception:
            pass

    def notify(self, title, msg):
        # 日志始终记录（便于排查），弹窗通知按开关过滤
        line = f"[通知] {title}: {msg}"
        self._write_file(f"[{time.strftime('%Y-%m-%d %H:%M:%S')}] {line}")
        # 通知文本也进 log_index（级别固定 info），让新版日志页能过滤到
        try:
            db.add_log_index(int(time.time()), "info", line)
        except Exception:
            pass
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


