# -*- coding: utf-8 -*-
"""7-Zip / Python zipfile 双引擎与子进程原语（阶段6c 自 extract.py 纯搬移）。

职责：run_silent（密码经 stdin 传递，绝不进命令行）、退出码/错误文本 → 用户短句归类、
PythonZipEngine（zipfile 回退；_detect_7z_only_format 判定其能否处理该格式）、
SevenZipEngine（解压主循环与进度上报）、PauseController（NtSuspend/NtResume 跨线程暂停）。
本模块只依赖标准库：不导入 extraction 其他子模块（导入图汇点，避免环）。
"""
import ctypes
import os
import subprocess
import sys
import threading
import time
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

    def register(self, proc):
        """登记一个正在运行的解压子进程；若已暂停则立即挂起它。"""
        if proc is None:
            return
        with self._lock:
            self._procs.add(proc)
        if self._event.is_set():
            self._suspend(proc)

    def unregister(self, proc):
        with self._lock:
            self._procs.discard(proc)

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

    def extract(self, task, options, layer):
        archive = Path(task["source_path"])
        fmt = _detect_7z_only_format(archive)
        if fmt:
            # 非 ZIP 格式（rar/7z/tar…）：内置引擎无能为力，必须明确提示需要
            # 7-Zip，而不是让 zipfile 抛 BadZipFile 后误报"不是有效的 ZIP 文件"。
            return {"success": False, "used_password": None, "encrypted": False,
                    "error": f"该格式需要 7-Zip（当前不可用）：{fmt} 无法用内置引擎解压",
                    "logs": [f"检测到 {fmt} 格式，内置 zipfile 引擎无法解压，需要 7-Zip"]}
        out = Path(task["output_dir"])
        out.mkdir(parents=True, exist_ok=True)
        pauser = task.get("pauser")
        last_error = None
        for pwd in task["passwords"]:
            if pauser is not None:
                pauser.wait_if_paused()
            try:
                with self._open(archive) as zf:
                    # 「真的需要密码」的判据：归档里存在非目录的加密条目（bit 0）。
                    # 未加密归档即使传了候选密码，也绝不能让该候选被记为「命中」，
                    # 否则 GUI 把整本密码本当候选时，每个未加密包都会误报命中。
                    encrypted = any(i.flag_bits & 0x1 for i in zf.infolist() if not i.is_dir())
                    if pwd:
                        pb = self._test_password_any(zf, self._password_bytes(pwd))
                        if pb is None:
                            raise RuntimeError("密码错误")
                        zf.extractall(out, pwd=pb)
                    else:
                        if encrypted:
                            raise RuntimeError("需要密码")
                        zf.extractall(out)
                return {"success": True,
                        "used_password": (pwd or None) if encrypted else None,
                        "encrypted": bool(encrypted),
                        "error": None, "logs": [f"使用 {self.name} 引擎解压 ZIP"]}
            except zipfile.BadZipFile as e:
                return {"success": False, "used_password": None, "encrypted": False,
                        "error": f"不是有效的 ZIP 文件: {e}", "logs": []}
            except (RuntimeError, OSError, ValueError) as e:
                last_error = str(e)
        return {"success": False, "used_password": None, "encrypted": False,
                "error": last_error or "解压失败", "logs": []}

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

    def extract(self, task, options, layer):
        archive = Path(task["source_path"])
        out = Path(task["output_dir"])
        out.mkdir(parents=True, exist_ok=True)
        pauser = task.get("pauser")
        progress_cb = task.get("progress_cb")
        total, encrypted = self._listing_info(archive)
        if progress_cb is not None:
            progress_cb(None if total is None else 0.0, layer, archive.name)
        last_error = None
        last_rc = None
        last_raw = ""
        for pwd in task["passwords"]:
            if pauser is not None:
                pauser.wait_if_paused()
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
                pauser.register(proc)
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
            try:
                while True:
                    if pauser is not None:
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
