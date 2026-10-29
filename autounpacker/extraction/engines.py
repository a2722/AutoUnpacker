# -*- coding: utf-8 -*-
"""7-Zip / Python zipfile 双引擎与子进程原语（阶段6c 自 extract.py 纯搬移）。

职责：run_silent（密码经 stdin 传递，绝不进命令行）、退出码/错误文本 → 用户短句归类、
PythonZipEngine（zipfile 回退；_detect_7z_only_format 判定其能否处理该格式）、
SevenZipEngine（解压主循环与进度上报）、PauseController（NtSuspend/NtResume 跨线程暂停）。
本模块只依赖标准库：不导入 extraction 其他子模块（导入图汇点，避免环）。
"""
import ctypes
import os
import shutil
import subprocess
import sys
import tempfile
import threading
import time
import uuid
import zipfile
from pathlib import Path


CREATE_NO_WINDOW = getattr(subprocess, "CREATE_NO_WINDOW", 0)


def _decode_7z(data):
    """7-Zip 在中文 Windows 上输出 GBK；先按 GBK 解码避免中文乱码，
    失败再回退 UTF-8（影响错误匹配与文件名解析）。"""
    try:
        return data.decode("gbk")
    except UnicodeDecodeError:
        return data.decode("utf-8", "replace")


class _RunResult:
    """7z 子进程运行结果（兼容原 text 模式调用的 returncode/stdout 用法）。"""

    __slots__ = ("returncode", "stdout", "stderr")

    def __init__(self, returncode, stdout, stderr):
        self.returncode = returncode
        self.stdout = stdout
        self.stderr = stderr


def _pwd_stdin_bytes(pwd):
    """密码 → stdin 字节：优先系统 ANSI(GBK) 编码（与旧版 -p 参数行为一致，
    中文密码经命令行时 7z 按 ANSI 转换后做 KDF），失败回退 UTF-8。

    密码经 stdin 管道传递，绝不拼进命令行（防任务管理器/WMI 窥探）。"""
    if not pwd:
        return b"\n"
    for enc in ("gbk", "cp936", "utf-8"):
        try:
            return pwd.encode(enc) + b"\n"
        except Exception:
            continue
    return pwd.encode("utf-8", "replace") + b"\n"


# 短 7z 元数据调用（列表/探测）的墙钟超时上限。只用于 run_silent 这种
# 「秒级完成」的调用；长时解压循环（SevenZipEngine.extract）不受此限制。
RUN_SILENT_TIMEOUT = 90.0


def run_silent(args, password=None, timeout=RUN_SILENT_TIMEOUT):
    """运行 7-Zip；密码经 stdin 传递。

    重要：参数里绝不能带 -p（裸 -p 会触发 7z 走控制台 ReadConsole 读密码，
    stdin 管道读不到，导致密码永远不生效）。省略 -p 时，7z 遇到加密归档
    才会从 stdin 读一行密码——这样密码就不出现在进程命令行里，
    任务管理器/WMI 看不到。

    传密码时只用 `input=` 一个机制（它会自动把 stdin 设成 PIPE）；不传密码
    时 stdin 指向 DEVNULL（无密码提示时不会卡住）。绝不能同时给
    `input=` 和 `stdin=`，否则 CPython 直接抛
    ValueError: stdin and input arguments may not both be used.

    仅用于列表/探测这类短调用：超过 timeout 秒即杀掉 7z 并返回失败结果
    （returncode=-1），绝不抛异常、绝不无限阻塞 watcher 线程。"""
    kwargs = {
        "capture_output": True,
        "creationflags": CREATE_NO_WINDOW,
        "timeout": timeout,
    }
    if password is not None:
        kwargs["input"] = _pwd_stdin_bytes(password)   # 隐式 stdin=PIPE
    else:
        kwargs["stdin"] = subprocess.DEVNULL
    try:
        r = subprocess.run(args, **kwargs)
    except subprocess.TimeoutExpired:
        return _RunResult(-1, "", f"7-Zip 元数据调用超时（>{timeout:.0f}s）")
    except (OSError, ValueError, subprocess.SubprocessError) as e:
        return _RunResult(-1, "", str(e))
    return _RunResult(r.returncode, _decode_7z(r.stdout or b""),
                      _decode_7z(r.stderr or b""))


def concise_error(raw_error, code=None):
    """把 7-Zip 的原始多行输出压缩成一句「面向用户的中文原因」。

    旧行为把整段 7z 输出（版本横幅 + 帮助文本 + 错误行）直接塞进
    result["error"]，托盘通知与队列错误单元格因此显示成一大块不可读文本。
    这里只保留真正的原因；原始输出仍完整留在 result["logs"]（诊断不丢）。

    参数 code: 7z 退出码（0/1=警告，2=致命错误，7=命令行错误，8=内存不足，
    255=用户中断）。退出码是比在文本里猜更稳的分类依据。
    """
    raw = str(raw_error or "")
    low = raw.lower()
    # 退出码优先级：7z 的致命/命令行错误码本身就是权威信号
    if code == 7:
        return "不是压缩包或已损坏"
    if code == 8:
        return "7-Zip 内存不足"
    if code == 255:
        return "解压被中断"
    if "wrong password" in low or "密码错误" in raw:
        return "密码错误"
    if "missing volume" in low:
        return "分卷链不完整（缺少兄弟分卷）"
    if "unexpected end of archive" in low:
        return "不是压缩包或已损坏"
    if any(m.lower() in low for m in ZIP_OPEN_ERROR_MARKERS):
        return "不是压缩包或已损坏"
    if "crc failed" in low or "data error" in low or "crc 校验" in raw:
        return "数据校验失败（CRC 不符，文件可能已损坏）"
    if "is not supported" in low or "unsupported method" in low:
        return "不支持的压缩算法"
    # 输出文件被占用：必须保留 monitors 用来判定「稍后自动重试」的原始标记词
    # （monitors.py 用这些词决定是否重试，不能因为压缩成短句就丢掉）。
    lock_tokens = []
    if "cannot delete output file" in low:
        lock_tokens.append("Cannot delete output file")
    if "另一个程序正在使用此文件" in raw:
        lock_tokens.append("正在使用此文件")
    if "进程无法访问" in raw:
        lock_tokens.append("进程无法访问")
    if "another process is using" in low:
        lock_tokens.append("another process is using")
    if "being used by another process" in low:
        lock_tokens.append("being used by another process")
    if lock_tokens:
        return "输出文件被占用（" + "、".join(lock_tokens) + "，稍后自动重试）"
    if "cannot find" in low or "the system cannot find" in low:
        return "文件不存在或路径无法访问"
    # 认不出具体原因时，给一句短而诚实的兜底（绝不回填整段原始输出）
    if code is not None:
        return f"解压失败（7-Zip 退出码 {code}）"
    return "解压失败"


def result_raw_error(result):
    """取结果用于「错误分类」的原始文本。

    新契约下 result["error"] 是简短中文原因，result["raw_error"] 才是原始
    7z 输出；旧的测试桩/旧引擎结果没有 raw_error 键时回退到 error，
    保证 is_zip_open_error / is_archive_open_error 等文本判定不回归。"""
    if not result:
        return ""
    return str(result.get("raw_error") or result.get("error") or "")


class PauseController:
    """解压暂停控制（跨线程共享）。

    - 暂停时用 NtSuspendProcess 挂起所有正在运行的 7z 子进程，恢复时
      NtResumeProcess 继续（不中断、无需重做）；
    - 新解压任务在启动前 wait_if_paused 阻塞等待（调用方也可在暂停时
      直接延后处理，见 FolderWatcher._handle）；
    - 暂停状态由用户手动复位，不随任务结束自动恢复。
    """

    def __init__(self, hub=None):
        self.hub = hub
        self._event = threading.Event()   # set = 已暂停
        self._procs = set()               # 正在运行的解压子进程
        self._aborted = set()             # 用户已中止的任务 id
        self._by_task = {}                # task_id -> 该任务正在运行的子进程集合
        self._lock = threading.Lock()

    def is_paused(self):
        return self._event.is_set()

    def wait_if_paused(self):
        """若已暂停则阻塞等待恢复（用于解压循环内部，恢复前不继续）。"""
        while self._event.is_set():
            time.sleep(0.3)

    def set_paused(self, flag):
        if flag:
            if self._event.is_set():
                return
            self._event.set()
            self._suspend_all()
        else:
            if not self._event.is_set():
                return
            self._event.clear()
            self._resume_all()

    @staticmethod
    def _base_key(task_id):
        """把多段解压的层 id `"<base>-layer-<n>"` 归一成 `"<base>"`。

        service 按层派发时把任务 id 改成 `f"{task_id}-layer-{depth}"` 登记，而 UI 的
        「忽略 / 标记完成」只有数据库里的任务 id。两者必须靠这层前缀关系对上，否则
        `abort_task(dbid)` 永远匹配不到层进程 —— 表现为「点了忽略，7z 还在跑，文件
        被咬住删不掉」。绝不抛异常。
        """
        try:
            s = str(task_id if task_id is not None else "")
        except Exception:
            return ""
        i = s.rfind("-layer-")
        return s[:i] if i > 0 else s

    def register(self, proc, task_id=None):
        """登记一个正在运行的解压子进程；若已暂停则立即挂起它。

        task_id 可选：旧调用 register(proc) 保持可用；带上 task_id 后
        abort_task(task_id) 才能定位并杀掉该任务卡住的子进程。"""
        if proc is None:
            return
        with self._lock:
            self._procs.add(proc)
            if task_id is not None:
                self._by_task.setdefault(task_id, set()).add(proc)
        if self._event.is_set():
            self._suspend(proc)

    def unregister(self, proc):
        with self._lock:
            self._procs.discard(proc)
            for procs in self._by_task.values():
                procs.discard(proc)

    def clear_abort(self, task_id):
        """清除某任务的中止标记（新一轮解压开始时调用）。

        层 id 每次解压都相同（`"<dbid>-layer-<n>"`），标记若不清会一直留着，
        让「中止过的任务」再次解压时被立刻秒杀。绝不抛异常。"""
        base = self._base_key(task_id)
        if not base:
            return
        with self._lock:
            self._aborted.discard(base)
            self._aborted.discard(str(task_id if task_id is not None else ""))

    def abort_task(self, task_id):
        """标记任务已中止，并杀掉该任务（含其各层）正在运行的子进程。

        只标记 + 杀进程：解压循环自己检测到中止后返回「已取消」，产物回退/保留
        由上层决定。可重复调用（幂等）。**前缀匹配**：`abort_task(dbid)` 会连带
        杀掉 `"<dbid>-layer-N"` 下标着的层进程，这样 UI 手动忽略才能真正打断
        咬住文件句柄的 7z / WinRAR。"""
        if task_id is None:
            return
        base = self._base_key(task_id)
        if not base:
            return
        with self._lock:
            self._aborted.add(base)
            self._aborted.add(str(task_id))
            procs = []
            for k, ps in self._by_task.items():
                if k == base or self._base_key(k) == base:
                    procs.extend(ps)
        for proc in procs:
            self._kill(proc)

    def is_aborted(self, task_id):
        if task_id is None:
            return False
        base = self._base_key(task_id)
        with self._lock:
            return base in self._aborted or str(task_id) in self._aborted

    def _kill(self, proc):
        try:
            proc.kill()
        except Exception:
            pass

    def _suspend(self, proc):
        try:
            h = getattr(proc, "_handle", None)
            if h:
                ctypes.windll.ntdll.NtSuspendProcess(h)
        except Exception:
            pass

    def _resume(self, proc):
        try:
            h = getattr(proc, "_handle", None)
            if h:
                ctypes.windll.ntdll.NtResumeProcess(h)
        except Exception:
            pass

    def _suspend_all(self):
        with self._lock:
            for p in list(self._procs):
                self._suspend(p)

    def _resume_all(self):
        with self._lock:
            for p in list(self._procs):
                self._resume(p)


def _dir_size(path):
    """目录下所有文件字节数总和（估算解压进度用）。失败返回 0。"""
    total = 0
    try:
        for root, _dirs, files in os.walk(path):
            for name in files:
                try:
                    total += os.path.getsize(os.path.join(root, name))
                except OSError:
                    pass
    except OSError:
        pass
    return total


def _detect_7z_only_format(path):
    """嗅探文件头部魔数：若格式只有 7-Zip 能解、内置 zipfile 引擎解不了，
    返回格式名（rar/7z/tar/gz/bz2/xz），否则返回 None（ZIP 或无法判断）。

    纯函数、零依赖、最多读 300 字节（tar 的 "ustar" 魔数在偏移 257）。文件
    缺失、为空、过短、不可读一律返回 None，绝不抛异常。ZIP 签名明确返回
    None，保证「损坏的 ZIP 仍报不是有效的 ZIP 文件」的旧行为不变。

    用途：7-Zip 缺失时程序会降级到 PythonZipEngine，此时对 rar/7z 等格式
    必须给出"需要 7-Zip"的准确提示，而不是让 zipfile 抛 BadZipFile 后误报
    "不是有效的 ZIP 文件"。"""
    try:
        with open(path, "rb") as f:
            data = f.read(300)
    except OSError:
        return None
    if not data:
        return None
    if data[:4] in (b"PK\x03\x04", b"PK\x05\x06", b"PK\x06\x06", b"PK\x07\x08"):
        return None
    if data[:7] == b"Rar!\x1a\x07\x00" or data[:8] == b"Rar!\x1a\x07\x01\x00":
        return "rar"  # rar4 / rar5
    if data[:6] == b"7z\xbc\xaf\x27\x1c":
        return "7z"
    if data[:2] == b"\x1f\x8b":
        return "gz"
    if data[:3] == b"BZh":
        return "bz2"
    if data[:6] == b"\xfd7zXZ\x00":
        return "xz"
    if data[257:262] == b"ustar":
        return "tar"
    return None


class PythonZipEngine:
    name = "Python zipfile"

    @staticmethod
    def _password_bytes(pwd):
        if not pwd:
            return [None]
        out = [pwd.encode("utf-8")]
        for codec in ("gbk", "cp936"):
            try:
                b = pwd.encode(codec)
            except Exception:
                continue
            if b not in out:
                out.append(b)
        return out

    @staticmethod
    def _opt_number(options, key, default):
        """读数值型安全选项：缺失/None/非法值一律回退默认值，绝不抛异常。

        与 service._opt_number 同口径，保证内置引擎的 zip bomb 预检与 7z 路径
        使用完全一致的阈值解析规则。"""
        try:
            value = options.get(key, default)
            return default if value is None else float(value)
        except (TypeError, ValueError):
            return default

    def _check_declared_bomb(self, archive, options):
        """只读打开 ZIP 清单，按「声明解压后大小 / 膨胀比」做 zip bomb 硬阈值预检。

        返回 (中文原因, 声明条目数)：命中硬阈值时原因为字符串，否则为 None。
        阈值与文案与 service._precheck（7z 路径）同口径：绝对上限
        bomb_hard_size_gb（默认 50 GB）、膨胀比 bomb_hard_ratio（默认 200×，
        需同时达到 bomb_hard_min_gb，默认 1 GB）。任一阈值 <= 0 = 关闭该条规则；
        bomb_hard_min_gb=0 = 比例规则无最小体积门槛。

        铁律：任何异常/未知（非 ZIP、打不开、解析不出）一律放行——加密或损坏的
        归档本就读不出真实清单，绝不能因为探测不到就拦住正常解压。"""
        options = options or {}
        if not options.get("bomb_guard_enabled", True):
            return None, 0
        try:
            with self._open(archive) as zf:
                infos = [i for i in zf.infolist() if not i.is_dir()]
                declared = sum(int(i.file_size) for i in infos)
                entries = len(infos)
            try:
                archive_size = Path(archive).stat().st_size
            except OSError:
                return None, entries
            hard_size_gb = self._opt_number(options, "bomb_hard_size_gb", 50.0)
            hard_ratio = self._opt_number(options, "bomb_hard_ratio", 200.0)
            hard_min_gb = self._opt_number(options, "bomb_hard_min_gb", 1.0)
            gib = 1024 ** 3
            declared_gb = declared / gib
            # 阈值 <= 0 = 关闭该条规则（与 service._precheck 完全同口径）。
            # bomb_hard_min_gb=0 = 比例规则无最小体积门槛。
            if hard_size_gb > 0 and declared > hard_size_gb * gib:
                return (f"声明解压后大小 {declared_gb:.1f} GB 超过硬上限 "
                        f"{hard_size_gb:g} GB（疑似 zip bomb），已停止解压"), entries
            ratio = declared / archive_size if archive_size > 0 else 0.0
            if hard_ratio > 0 and ratio >= hard_ratio and declared_gb >= hard_min_gb:
                return (f"声明膨胀比 {ratio:.0f}:1（解压后 {declared_gb:.2f} GB / "
                        f"压缩包 {archive_size / 1048576:.1f} MB）达到硬阈值 "
                        f"{hard_ratio:g}:1（疑似 zip bomb），已停止解压"), entries
            return None, entries
        except Exception:
            return None, 0

    def extract(self, task, options, layer):
        archive = Path(task["source_path"])
        fmt = _detect_7z_only_format(archive)
        if fmt:
            # 非 ZIP 格式（rar/7z/tar…）：内置引擎无能为力，必须明确提示需要
            # 7-Zip，而不是让 zipfile 抛 BadZipFile 后误报"不是有效的 ZIP 文件"。
            return {"success": False, "used_password": None, "encrypted": False,
                    "error": f"该格式需要 7-Zip（当前不可用）：{fmt} 无法用内置引擎解压",
                    "logs": [f"检测到 {fmt} 格式，内置 zipfile 引擎无法解压，需要 7-Zip。"
                             f"安装 7-Zip"]}
        blocked, entries = self._check_declared_bomb(archive, options)
        if blocked is not None:
            # 与 7z 路径同一失败契约：拒绝把炸弹写盘，也不创建输出目录。
            return {"success": False, "used_password": None, "encrypted": False,
                    "error": blocked, "raw_error": blocked,
                    "logs": [f"{self.name} 引擎安全预检拦截（声明 {entries} 个条目）："
                             f"{blocked}"]}
        out = Path(task["output_dir"])
        out.mkdir(parents=True, exist_ok=True)
        # 解压丢进独立子进程（zip_worker）：Python zipfile 的紧密解密循环会长时间
        # 持有 GIL，在主进程里跑会把 Qt 主线程饿死 → 界面整体冻结（真机案：大 zip
        # 回退本引擎后切任何页面都卡死）。子进程隔离 GIL 与原生崩溃，主进程照常。
        return self._extract_via_subprocess(task, archive, out, layer)

    def _extract_via_subprocess(self, task, archive, out, layer):
        """把 zipfile 解压交给 zip_worker 子进程，父进程只等 + 报进度/暂停/中止。

        结果契约与内联版逐字一致：成功返回 used_password/encrypted；失败按
        bad_zip / password / 其它分类回填 error。密码**绝不进命令行**——与 7z
        路径同一安全口径：写进临时请求文件（仅本进程与子进程可见），argv 只带
        请求文件路径，任务管理器/命令行参数里看不到任何密码。
        """
        from ..workers import worker_command
        import json
        import tempfile
        import uuid

        pauser = task.get("pauser")
        task_id = task.get("id")
        progress_cb = task.get("progress_cb")
        if progress_cb is not None:
            progress_cb(None, layer, archive.name)

        req_path = Path(tempfile.gettempdir()) / f"au_zipreq_{uuid.uuid4().hex}.json"
        req = {
            "archive": str(archive),
            "out": str(out),
            # 保留 None（表示「不带密码」这一候选轮次）；只把真正的字符串码原样
            # 传递。绝不能 str(None) 成 "None"——那会变成一个真密码去试，把
            # 「需要密码」误报成「密码错误」。
            "passwords": [None if p is None else str(p)
                          for p in (task.get("passwords") or [])],
        }
        try:
            req_path.write_text(json.dumps(req, ensure_ascii=False), encoding="utf-8")
        except OSError as e:
            return {"success": False, "used_password": None, "encrypted": False,
                    "error": f"准备解压请求失败: {e}", "logs": []}

        try:
            try:
                proc = subprocess.Popen(
                    worker_command("zip", str(req_path)),
                    stdin=subprocess.DEVNULL, stdout=subprocess.PIPE,
                    stderr=subprocess.STDOUT, creationflags=CREATE_NO_WINDOW)
            except Exception as e:
                return {"success": False, "used_password": None, "encrypted": False,
                        "error": f"启动解压子进程失败: {e}", "logs": []}
            if pauser is not None:
                pauser.register(proc, task_id)
            buf = []

            def _drain():
                try:
                    for raw in proc.stdout:
                        buf.append(raw.decode("utf-8", "replace"))
                except Exception:
                    pass

            th = threading.Thread(target=_drain, daemon=True)
            th.start()
            # 进度：子进程只输出一行结果，无中间进度；用输出目录体积估算（与 7z
            # 路径同一算法），让用户看到"还在动"。总量取自 task（service 已传入）。
            total = task.get("declared_total")
            last_ratio = -1.0
            last_progress_t = 0.0
            aborted = False
            try:
                while True:
                    if pauser is not None:
                        if task_id is not None and pauser.is_aborted(task_id):
                            proc.kill()
                            aborted = True
                            break
                        pauser.wait_if_paused()
                        # 暂停期间不推进进度，避免 pause 时长被算成解压耗时
                        last_progress_t = time.time()
                    if proc.poll() is not None:
                        break
                    now = time.time()
                    if total and now - last_progress_t >= 0.5:
                        last_progress_t = now
                        try:
                            ratio = min(0.999, _dir_size(out) / total)
                        except Exception:
                            ratio = last_ratio
                        if ratio != last_ratio:
                            last_ratio = ratio
                            if progress_cb is not None:
                                progress_cb(ratio, layer, archive.name)
                    time.sleep(0.1)
                rc = proc.wait()
            finally:
                if pauser is not None:
                    pauser.unregister(proc)
            th.join(timeout=2)
            if aborted:
                if progress_cb is not None:
                    progress_cb(None, layer, archive.name)
                return {"success": False, "used_password": None,
                        "encrypted": False,
                        "error": "已取消（用户中止）",
                        "raw_error": "用户中止解压",
                        "logs": [f"使用 {self.name} 引擎解压已取消（用户中止）"]}
            payload = self._parse_worker_result("".join(buf))
            if payload is None:
                if progress_cb is not None:
                    progress_cb(None, layer, archive.name)
                return {"success": False, "used_password": None, "encrypted": False,
                        "error": f"解压子进程异常退出（rc={rc}）", "logs": []}
            if payload.get("success"):
                if progress_cb is not None:
                    progress_cb(1.0, layer, archive.name)
                return {"success": True,
                        "used_password": payload.get("used_password"),
                        "encrypted": bool(payload.get("encrypted")),
                        "error": None,
                        "logs": [f"使用 {self.name} 引擎解压 ZIP"]}
            if progress_cb is not None:
                progress_cb(None, layer, archive.name)
            kind = payload.get("kind")
            err = str(payload.get("error") or "解压失败")
            if kind == "bad_zip":
                return {"success": False, "used_password": None, "encrypted": False,
                        "error": err, "logs": []}
            return {"success": False, "used_password": None, "encrypted": False,
                    "error": err, "logs": []}
        finally:
            try:
                req_path.unlink(missing_ok=True)
            except OSError:
                pass

    @staticmethod
    def _parse_worker_result(text):
        """从子进程 stdout 里取最后一行 `RESULT:{json}`；解析失败返回 None。"""
        import json
        payload = None
        for line in str(text or "").splitlines():
            line = line.strip()
            if line.startswith("RESULT:"):
                try:
                    payload = json.loads(line[len("RESULT:"):])
                except Exception:
                    payload = None
        return payload

    @staticmethod
    def _open(archive):
        """与 7-Zip 的文件名解码保持一致：未设 UTF-8 标志（bit 11）的条目按
        UTF-8 → GBK → cp437 依次尝试解码，避免同一文件被写成两份不同名字
        （如 7-Zip 解出"卡芙卡"，Python 默认 cp437 却写成"σìíΦèÖσìí"）。"""
        if sys.version_info >= (3, 11):
            for enc in ("utf-8", "gbk"):
                try:
                    return zipfile.ZipFile(archive, metadata_encoding=enc)
                except (UnicodeDecodeError, ValueError):
                    continue
        return zipfile.ZipFile(archive)

    @staticmethod
    def _test_password_any(zf, pwd_bytes_list):
        entries = [i for i in zf.infolist() if not i.is_dir()]
        encrypted = [i for i in entries if i.flag_bits & 0x1]
        targets = encrypted or entries
        if not targets:
            return pwd_bytes_list[0] if pwd_bytes_list else None
        for info in targets:
            for pb in pwd_bytes_list:
                try:
                    with zf.open(info, pwd=pb) as f:
                        f.read(1)
                    return pb
                except (RuntimeError, KeyError, zipfile.BadZipFile):
                    continue
            return None
        return None


class SevenZipEngine:
    name = "7-Zip"

    def __init__(self, path):
        self.path = Path(path)

    def _listing_info(self, archive, password=None):
        """7z l -slt 列出当前压缩包的 (未压缩字节总和, 是否含加密条目)。

        分卷给首卷路径即可。失败/无法列出返回 (None, False)。
        注意 -slt 每条的 Folder = + 表示目录，目录不计入文件总量；
        Encrypted = + 表示该条目加密（ZipCrypto / WPAES(AES) / 7zAES 均会置位），
        这是判断「归档是否真的需要密码」的可靠信号。

        password: 头部加密的归档（RAR/7z 加密文件名）空密码列不出，需逐个
        用候选密码尝试；成功拿到总量后进度条才能从忙碌变为百分比进度。
        注意：绝不能带 -p 参数（裸 -p 会走控制台读密码，管道读不到），
        密码一律经 stdin 传递。

        这里刻意不套宽泛的 try/except：run_silent 已把所有子进程失败
        （超时/OSError/参数误用）转成 returncode=-1 的显式结果，若再吞异常
        会把「清单拿不到」伪装成「正常无加密」，正是旧 bug 被静默吞掉的根源。"""
        total = 0
        is_dir = False
        encrypted = False
        args = [str(self.path), "l", "-slt", str(archive)]
        r = run_silent(args, password=password)
        if r.returncode != 0:
            return None, False
        for line in r.stdout.splitlines():
            s = line.strip()
            if s.startswith("Path = "):
                is_dir = False
            elif s.startswith("Folder = "):
                is_dir = (s[9:].strip() == "+")
            elif s == "Encrypted = +":
                encrypted = True
            elif s.startswith("Size = ") and not is_dir:
                try:
                    total += int(s[7:].strip())
                except ValueError:
                    pass
        return (total or None), encrypted

    def _listing_total(self, archive, password=None):
        """兼容旧签名：只返回未压缩字节总和（进度条基准）。"""
        return self._listing_info(archive, password)[0]

    def _listing_ok_with_pwd(self, archive, pwd):
        """带密码列一次清单：返回 (是否成功, 原始输出文本)。

        专治**头部加密**（7z -mhe / RAR 加密文件名）：无密码连条目都列不出，
        选不了探针，此时用「带密码的清单」验证最便宜（不创建任何文件）。
        密码经 stdin 传递。异常一律 (False, "")。

        为什么要把原始文本带出去：失败原因不止「密码错」一种 —— 文件根本不是
        7-Zip 认识的归档时同样 rc!=0（真机：视频头部 + 内嵌 zip 的假 EOCD，7z 报
        `Cannot open the file as archive`）。上层据此决定是把错误原样透传（好让
        多段伪装剥离兜底接手），还是当作候选密码不匹配。绝不在这里臆造
        "Wrong password" 覆盖掉真实错误。"""
        try:
            sevenzip = getattr(self, "path", None)
            if not sevenzip:
                return False, ""
            r = run_silent([str(sevenzip), "l", "-slt", str(archive)], password=pwd)
            if r.returncode == 0:
                return True, str(getattr(r, "stdout", "") or "")
            return False, (str(getattr(r, "stdout", "") or "")
                           + str(getattr(r, "stderr", "") or ""))
        except Exception:
            return False, ""

    def _listing_is_unopenable(self, archive):
        """无密码清单失败时，判断失败原因是「7-Zip 根本打不开这个文件」而非「缺密码」。

        调用时机：仅在 `total is None`（无密码列不出清单）且候选密码多于 1 个时，
        用来决定要不要走「头部加密」分支。

        为什么需要：旧口径「列不出清单 ⇒ 头部加密」会把「文件不是归档」也归进去
        （真机 `doro31028.mp4`：视频头 + 内嵌 zip，尾部的 PK\\x05\\x06 是数据里
        的假 EOCD；Python zipfile 能读、7-Zip 报 `Cannot open the file as
        archive`）。误判后每个候选都先做一次带密码清单、全部失败，真实错误被
        "Wrong password" 覆盖，既报错「密码错误」，又让 service 里已有的
        `strip_embedded_zip` 兜底彻底失去触发条件。

        判据刻意只认 7-Zip 的这句固定文案（大小写不敏感），其余失败一律维持旧的
        「可能是头部加密」判断（保守：宁可按头部加密试密码，也不误伤真加密包）。
        异常一律 False（不改变既有行为）。"""
        try:
            sevenzip = getattr(self, "path", None)
            if not sevenzip:
                return False
            r = run_silent([str(sevenzip), "l", "-slt", str(archive)])
            if r.returncode == 0:
                return False
            text = (str(getattr(r, "stdout", "") or "")
                    + str(getattr(r, "stderr", "") or "")).lower()
            return "cannot open the file as archive" in text
        except Exception:
            return False

    # 多文件大包走「廉价密码预验」的阈值：条目数超过它、且候选密码多于 1 个时，
    # 才值得先探测（避免为小包引入多余一次子进程）。
    CHEAP_PROBE_MIN_ENTRIES = 200

    def _cheap_probe_entry(self, archive):
        """挑一条最小文件用于快速验密码；不适用（小包/列不出/没文件）返回 None。

        真机教训：1.83 GB / 9,970 文件的加密 RAR、密码候选 62 个，旧逻辑对每个
        候选都真解压一遍（错误密码会先把整套文件树创建出来），单次约 13 秒，
        合计约 14 分钟才轮到正确密码。改成只解最小一条即可把单次降到毫秒级。
        """
        try:
            sevenzip = getattr(self, "path", None)
            if not sevenzip:
                return None
            r = run_silent([str(sevenzip), "l", "-slt", str(archive)])
            if r.returncode != 0:
                return None
            name = None
            size = None
            is_dir = False
            count = 0
            best = None
            for line in (r.stdout or "").splitlines():
                s = line.strip()
                if s.startswith("Path = "):
                    name = s[7:]
                    size = None
                    is_dir = False
                elif s.startswith("Folder = "):
                    is_dir = (s[9:].strip() == "+")
                elif s.startswith("Size = "):
                    try:
                        size = int(s[7:].strip())
                    except ValueError:
                        size = None
                elif s.startswith("Attributes = "):
                    if name is not None and not is_dir and size is not None:
                        count += 1
                        if size > 0 and (best is None or size < best[1]):
                            best = (name, size)
                    name = None
            if count < self.CHEAP_PROBE_MIN_ENTRIES or best is None:
                return None
            return best[0]
        except Exception:
            return None

    def _password_ok_by_probe(self, archive, pwd, entry):
        """只解 entry 这一条到临时目录来验密码：rc==0 视为密码正确。

        密码经 stdin 传递（绝不拼进命令行）。任何异常一律返回 False（保守：当作
        不匹配，换下一个候选）；临时目录无论成败都清掉。"""
        tmp = None
        try:
            sevenzip = getattr(self, "path", None)
            if not sevenzip:
                return False
            tmp = tempfile.mkdtemp(prefix="au_pwprobe_")
            proc = subprocess.Popen(
                [str(sevenzip), "x", str(archive), str(entry), f"-o{tmp}", "-y"],
                stdin=subprocess.PIPE, stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL, creationflags=CREATE_NO_WINDOW)
            try:
                proc.communicate(_pwd_stdin_bytes(pwd), timeout=60)
            except subprocess.TimeoutExpired:
                try:
                    proc.kill()
                except Exception:
                    pass
                return False
            if proc.returncode != 0:
                return False
            # rc==0 还不够：条目名不存在时 7z 也返回 0（"No files to process"）。
            # 必须确认文件真的落在临时目录里，否则任何密码都会被误判为「正确」。
            try:
                rel = str(entry).replace("\\", "/").lstrip("/")
                return (Path(tmp) / rel).is_file()
            except Exception:
                return False
        except Exception:
            return False
        finally:
            if tmp:
                try:
                    shutil.rmtree(tmp, ignore_errors=True)
                except Exception:
                    pass

    def extract(self, task, options, layer):
        archive = Path(task["source_path"])
        out = Path(task["output_dir"])
        out.mkdir(parents=True, exist_ok=True)
        pauser = task.get("pauser")
        task_id = task.get("id")
        progress_cb = task.get("progress_cb")
        total, encrypted = self._listing_info(archive)
        if progress_cb is not None:
            progress_cb(None if total is None else 0.0, layer, archive.name)
        last_error = None
        last_rc = None
        last_raw = ""
        # 多候选 + 多文件大包：先「只解最小一条」把密码验掉，避免每个候选都整包
        # 解压一遍（错误密码会先把整套文件树创建出来 —— 真机 1.83GB / 9,970 文件
        # × 62 候选 ≈ 14 分钟）。单候选 / 小包 / 列不出清单时不开探测，旧行为不变。
        cand = list(task["passwords"])
        probe_entry = self._cheap_probe_entry(archive) if len(cand) > 1 else None
        # 无密码列不出清单 ⇒ 可能是头部加密：此时用「带密码的清单」验密码（最便宜）。
        # 但必须先排除「7-Zip 根本不认这个文件」：那种情况错误原样透传，交给 service
        # 里已有的 strip_embedded_zip 多段伪装兜底，绝不能被误当成「密码错误」。
        header_enc = bool(
            len(cand) > 1
            and total is None
            and not self._listing_is_unopenable(archive)
        )
        for pwd in cand:
            if pauser is not None:
                pauser.wait_if_paused()
            if header_enc and pwd:
                ok, err_text = self._listing_ok_with_pwd(archive, pwd)
                if not ok:
                    last_error = "密码错误"
                    last_rc = 2
                    # 保留真实错误文本：这里是「带密码清单失败」，既可能是密码错，
                    # 也可能是文件根本打不开；后者要让 is_zip_open_error 命中。
                    last_raw = err_text or "Wrong password"
                    continue
            if probe_entry is not None and pwd:
                if not self._password_ok_by_probe(archive, pwd, probe_entry):
                    # 预验不通过：直接换下一个候选，绝不落整套文件、绝不整包解压
                    last_error = "密码错误"
                    last_rc = 2
                    last_raw = "Wrong password"
                    continue
            # 头部加密（RAR/7z 加密文件名）空密码列不出总量：用候选密码逐个试，
            # 一旦列出即把进度条从忙碌切换为真实百分比；顺带确认加密标记。
            if total is None and pwd:
                t, enc = self._listing_info(archive, pwd)
                if t:
                    total = t
                    encrypted = encrypted or enc
                    if progress_cb is not None:
                        progress_cb(0.0, layer, archive.name)
            # 密码经 stdin 管道传递：7z 不带 -p（裸 -p 会走控制台读密码，
            # 管道读不到），省略 -p 时遇到加密归档才会从 stdin 读一行密码。
            # 任务管理器/WMI 只能看到进程命令行，看不到 stdin 内容。
            args = ["x", str(archive), f"-o{out}", "-y"]
            cmd = " ".join([str(self.path)] + args)
            try:
                proc = subprocess.Popen(
                    [str(self.path)] + args,
                    stdin=subprocess.PIPE, stdout=subprocess.PIPE,
                    stderr=subprocess.STDOUT,
                    creationflags=CREATE_NO_WINDOW)
            except Exception as e:
                last_error = str(e)
                last_raw = str(e)
                break
            try:
                proc.stdin.write(_pwd_stdin_bytes(pwd))
                proc.stdin.close()
            except Exception:
                try:
                    proc.stdin.close()
                except Exception:
                    pass
            if pauser is not None:
                pauser.register(proc, task_id)
            buf = []

            def _drain():
                try:
                    for raw in proc.stdout:
                        # 7-Zip 在中文 Windows 上输出 GBK 编码，按 UTF-8 解码会
                        # 变成乱码，导致 "正在使用此文件/进程无法访问" 等中文
                        # 错误串匹配失效（解压失败被误判为永久失败而非重试）。
                        # 先试 GBK（中文系统默认），再回退 UTF-8。
                        try:
                            buf.append(raw.decode("gbk"))
                        except (UnicodeDecodeError, UnicodeError):
                            buf.append(raw.decode("utf-8", "replace"))
                except Exception:
                    pass
            th = threading.Thread(target=_drain, daemon=True)
            th.start()
            last_ratio = -1.0
            last_progress_t = 0.0
            aborted = False
            try:
                while True:
                    if pauser is not None:
                        if task_id is not None and pauser.is_aborted(task_id):
                            # 用户中止：杀掉卡住的 7z 并跳出等待（先于暂停等待
                            # 判断，保证「暂停中再中止」也能立即结束）。rc =
                            # proc.wait() 紧接其后立即返回（该等待绝不带超时）。
                            proc.kill()
                            aborted = True
                            break
                        pauser.wait_if_paused()
                    if proc.poll() is not None:
                        break
                    now = time.time()
                    if total and now - last_progress_t >= 0.25:
                        last_progress_t = now
                        ratio = min(0.999, _dir_size(out) / total)
                        if ratio != last_ratio:
                            last_ratio = ratio
                            if progress_cb is not None:
                                progress_cb(ratio, layer, archive.name)
                    time.sleep(0.1)
                rc = proc.wait()
            finally:
                if pauser is not None:
                    pauser.unregister(proc)
            th.join(timeout=2)
            if aborted:
                if progress_cb is not None:
                    progress_cb(None, layer, archive.name)
                return {"success": False, "used_password": None,
                        "encrypted": bool(encrypted),
                        "error": "已取消（用户中止）",
                        "raw_error": "用户中止解压",
                        "logs": [f"使用 {self.name} 引擎解压已取消（用户中止）"]}
            err = "".join(buf)
            if rc == 0:
                if progress_cb is not None:
                    progress_cb(1.0, layer, archive.name)
                # 只有归档真的含加密条目、且此密码通过了 7-Zip 校验，才算「用到了密码」；
                # 未加密归档即使 stdin 喂了候选密码也是 rc==0，绝不能记为命中。
                return {"success": True,
                        "used_password": (pwd or None) if encrypted else None,
                        "encrypted": bool(encrypted),
                        "error": None,
                        "logs": [f"使用 {self.name} 引擎解压", f"命令: {cmd}"]}
            if "Wrong password" in err:
                last_error = "密码错误"
                last_rc = rc
                last_raw = err
                continue
            last_error = (err or f"7z 退出码 {rc}").strip()
            last_rc = rc
            last_raw = err
            break
        if progress_cb is not None:
            progress_cb(None if total is None else 0.0, layer, archive.name)
        # 面向用户的 error 只放一句简短中文原因（供托盘通知/队列错误单元格）；
        # 完整原始 7z 输出放 logs（诊断细节一律保留，绝不丢弃）。
        concise = concise_error(last_raw if last_raw else last_error, last_rc)
        raw_logs = [f"使用 {self.name} 引擎解压失败"]
        if last_raw:
            raw_lines = str(last_raw).splitlines()
            # 原始输出**完整**保留在日志里（事后排查全靠它，绝不截断/省略）；
            # 只在首尾各加一个显式标记，供日志界面把整块折叠成一行、点击再展开。
            # 折叠只发生在界面显示层，日志文件内容一个字节都不少。
            raw_logs.append("--- 7-Zip 原始输出 ---")
            raw_logs.extend(raw_lines)
            raw_logs.append(f"--- 7-Zip 原始输出结束（共 {len(raw_lines)} 行）---")
        return {"success": False, "used_password": None, "encrypted": bool(encrypted),
                "error": concise, "raw_error": last_raw or last_error,
                "logs": raw_logs}


class WinRarEngine:
    """WinRAR 引擎（可选）：解某些 7-Zip 打不开的合法 Zip64/大偏移加密 ZIP。

    为什么存在：7-Zip 的 zip 解析器对「本地头在超大偏移 + Zip64 数据描述符 +
    加密」的合法 ZIP 无能为力（真机案：伪装 MP4 的加密 Zip64，内含 7z 分卷，
    7z 三个版本全 `Cannot open`），而 WinRAR 用正确密码 60s 解出 6GB。原先这类
    文件只能回退 Python zipfile（纯 Python 20+ 分钟且占 GIL 冻结界面）。

    调用契约（已实测，WinRAR 6.24）：
        WinRAR.exe x -ibck -y -inul [-ilog<tmp>] -p<pwd> <archive> <outdir>
      - `-ibck`：后台，不占前台；**实测不弹窗**（错误时配合 -inul 也不弹）。
      - `-inul`：抑制错误弹窗（否则错误会弹诊断窗 → 无人值守挂起）。
      - `-y`：全部查询默认 Yes（覆盖已存在文件等）。
      - `-ilog<file>`：把错误写日志文件（-inul 后屏幕无输出，靠它拿文本）。
      - `-p<pwd>`：密码**必须给**，否则加密归档会弹密码框并挂起；无候选时给
        `-p-`（实测不弹、立即失败）。
      绝不使用 `-ioff`（那是「解压完关机」）。
      退出码（实测 6.24）：0=成功；10=失败（密码错 / 打不开 / 无匹配）；
      其余非零按失败处理。

    安全：密码经 `-p` 落在命令行 → 任务管理器/命令行可见（WinRAR 无 stdin
    传密码能力，这是它的固有限制）。因此本引擎**只作末位可选兜底**（7z 与
    Python zipfile 都搞不定时才用），且调用方**绝不把 argv 写进日志**。
    """

    name = "WinRAR"

    def __init__(self, path):
        self.path = Path(path)

    def _total_bytes(self, archive):
        """从 zipfile 读未压缩总量作进度分母（WinRAR CLI 无机器可读进度）。

        只读中心目录（不解压），失败返回 None（进度退化为忙碌态）。"""
        try:
            with zipfile.ZipFile(archive) as zf:
                total = sum(int(i.file_size) for i in zf.infolist()
                            if not i.is_dir())
            return total or None
        except Exception:
            return None

    # WinRAR 按扩展名认归档：伪装文件（如 xxx.mp4 实为 ZIP）直接被拒（实测 rc=1）。
    # 故喂给 WinRAR 前，若源扩展名不是压缩包后缀，用**同盘硬链接**造一个带
    # 正确后缀、零拷贝的路径（实测 6GB 瞬间建好，WinRAR 正常解）。跨盘/硬链接
    # 失败时回退临时复制（有 I/O 成本，仅在不得以时用）。
    _ARCHIVE_EXTS = (".zip", ".rar", ".7z", ".tar", ".gz", ".bz2", ".xz")

    def _winrar_friendly_path(self, archive):
        """返回一个 WinRAR 认可扩展名的等效路径（可能等于原路径）。

        返回 (path, temp_link_or_None)。调用方必须在用完后清理 temp_link。
        原路径已是压缩包后缀 → 原样返回。否则尝试同盘硬链接；失败则复制到
        临时目录（同盘优先，避免跨盘大文件复制）。任何异常都退回原路径
        （宁可按原样试一次，也绝不因造链失败而放弃）。"""
        try:
            if archive.suffix.lower() in self._ARCHIVE_EXTS:
                return archive, None
        except Exception:
            return archive, None
        link = None
        try:
            # 同盘硬链接：放在源文件同目录（保证同卷），名字加唯一后缀避免冲突
            link = archive.with_name(
                archive.name + ".au_winrar_" + uuid.uuid4().hex[:8] + ".zip")
            os.link(str(archive), str(link))
            return link, link
        except OSError:
            # 跨盘 / 不支持硬链接：退回临时复制（放临时目录即可）
            try:
                if link is not None and link.exists():
                    link.unlink()
            except OSError:
                pass
            try:
                tmp = Path(tempfile.gettempdir()) / (
                    "au_winrar_" + uuid.uuid4().hex + ".zip")
                shutil.copyfile(str(archive), str(tmp))
                return tmp, tmp
            except OSError:
                return archive, None

    def extract(self, task, options, layer):
        archive = Path(task["source_path"])
        out = Path(task["output_dir"])
        out.mkdir(parents=True, exist_ok=True)
        pauser = task.get("pauser")
        task_id = task.get("id")
        progress_cb = task.get("progress_cb")
        total = self._total_bytes(archive)
        if progress_cb is not None:
            progress_cb(None if total is None else 0.0, layer, archive.name)

        # 伪装文件（如 xxx.mp4 实为 ZIP）：WinRAR 按扩展名拒绝，用同盘硬链接造
        # 一个 .zip 等效路径（零拷贝）再喂给它；用完清理。
        wr_archive, temp_link = self._winrar_friendly_path(archive)

        passwords = list(task.get("passwords") or [])
        if not passwords:
            passwords = [None]

        last_rc = None
        last_log = ""
        try:
            for pwd in passwords:
                if pauser is not None:
                    pauser.wait_if_paused()
                # 密码必给：空候选用 -p-（实测不弹密码框、立即失败），防无人值守挂起
                pwd_arg = ("-p" + str(pwd)) if pwd else "-p-"
                # -ilog 临时文件：-inul 后屏幕无输出，靠它拿错误文本（诊断用）
                log_path = Path(tempfile.gettempdir()) / (
                    "au_winrar_" + uuid.uuid4().hex + ".log")
                args = [str(self.path), "x", "-ibck", "-y", "-inul",
                        "-ilog" + str(log_path), pwd_arg,
                        str(wr_archive), str(out)]
                try:
                    proc = subprocess.Popen(
                        args, stdin=subprocess.DEVNULL,
                        stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                        creationflags=CREATE_NO_WINDOW)
                except Exception as e:
                    self._unlink(log_path)
                    self._unlink(temp_link)
                    return self._fail(f"启动 WinRAR 失败: {e}",
                                      raw=str(e),
                                      logs=[f"使用 {self.name} 引擎失败"])
                if pauser is not None:
                    pauser.register(proc, task_id)
                last_ratio = -1.0
                last_progress_t = 0.0
                aborted = False
                try:
                    while True:
                        if pauser is not None:
                            if task_id is not None and pauser.is_aborted(task_id):
                                proc.kill()
                                aborted = True
                                break
                            pauser.wait_if_paused()
                            last_progress_t = time.time()
                        if proc.poll() is not None:
                            break
                        now = time.time()
                        if total and now - last_progress_t >= 0.5:
                            last_progress_t = now
                            ratio = min(0.999, _dir_size(out) / total)
                            if ratio != last_ratio:
                                last_ratio = ratio
                                if progress_cb is not None:
                                    progress_cb(ratio, layer, archive.name)
                        time.sleep(0.1)
                    rc = proc.wait()
                finally:
                    if pauser is not None:
                        pauser.unregister(proc)
                last_rc = rc
                last_log = self._read_log(log_path)
                self._unlink(log_path)
                if aborted:
                    if progress_cb is not None:
                        progress_cb(None, layer, archive.name)
                    return {"success": False, "used_password": None,
                            "encrypted": False,
                            "error": "已取消（用户中止）",
                            "raw_error": "用户中止解压",
                            "logs": [f"使用 {self.name} 引擎解压已取消（用户中止）"]}
                if rc == 0:
                    if progress_cb is not None:
                        progress_cb(1.0, layer, archive.name)
                    return {"success": True,
                            "used_password": pwd or None,
                            "encrypted": bool(pwd),
                            "error": None,
                            "logs": [f"使用 {self.name} 引擎解压"]}
                # 非零：若是「密码错」则继续试下一个候选；否则（打不开/损坏）
                # 也继续试——候选耗尽后由下方统一报错。last_log 保留最后一条。
        finally:
            self._unlink(temp_link)
        if progress_cb is not None:
            progress_cb(None, layer, archive.name)
        # 失败文案：优先从 -ilog 里认「密码错」，否则给通用失败。绝不含密码/argv。
        msg = "WinRAR 解压失败"
        if self._looks_wrong_password(last_log):
            msg = "密码错误"
        elif last_rc is not None:
            msg = f"WinRAR 解压失败（退出码 {last_rc}）"
        logs = [f"使用 {self.name} 引擎解压失败"]
        if last_log:
            logs.append("--- WinRAR 诊断 ---")
            logs.extend(str(last_log).splitlines()[:20])
        return {"success": False, "used_password": None, "encrypted": False,
                "error": msg, "raw_error": msg, "logs": logs}

    @staticmethod
    def _fail(msg, raw="", logs=None):
        return {"success": False, "used_password": None, "encrypted": False,
                "error": msg, "raw_error": raw or msg,
                "logs": logs or [msg]}

    @staticmethod
    def _looks_wrong_password(text):
        t = str(text or "").lower()
        return ("wrong password" in t or "密码错误" in t
                or "incorrect password" in t or "bad password" in t)

    @staticmethod
    def _read_log(path):
        """读 -ilog 文本（WinRAR 写 UTF-16LE，含 BOM；容错多种编码）。"""
        try:
            data = Path(path).read_bytes()
        except OSError:
            return ""
        if not data:
            return ""
        for enc in ("utf-16", "utf-8", "gbk"):
            try:
                return data.decode(enc)
            except (UnicodeDecodeError, UnicodeError):
                continue
        return data.decode("utf-8", "replace")

    @staticmethod
    def _unlink(path):
        if path is None:
            return
        try:
            Path(path).unlink(missing_ok=True)
        except OSError:
            pass


ZIP_OPEN_ERROR_MARKERS = (
    "Cannot open the file as archive",
    "Open ERROR",
    "Can't open as archive",
    "Is not archive",
    "not a valid zip",
    "不是有效的 ZIP",
)


def is_zip_open_error(error):
    if not error:
        return False
    err = str(error)
    return any(m in err for m in ZIP_OPEN_ERROR_MARKERS)


def is_archive_open_error(error):
    """错误是否属于「归档打不开/被截断」——分卷链不完整的典型签名。

    与 is_split_gap_error 的区别：不要求报错归档的文件名是分卷名。分卷首卷
    拼出的载荷文件名通常不带编号（如 xxx.7z），7-Zip 对它报 Cannot open /
    Unexpected end 即说明拼出来的不是完整归档（缺兄弟分卷）。"""
    err = str(error or "")
    return (is_zip_open_error(err)
            or "Unexpected end of archive" in err
            or "Missing volume" in err)
