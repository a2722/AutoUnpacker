#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""解压核心：格式探测（含伪装/分卷/未完成下载）、密码候选生成、7-Zip/Python 双引擎与多层解压服务。

职责：- detect_archive_format() 等：魔数/尾部/前部扫描识别真实压缩格式与「多段伪装」文件
- 分卷识别与到齐判断、文件名提取密码、按层生成候选密码列表
- 7-Zip 与 Python zipfile 两套解压引擎（密码经 stdin 传递，绝不进命令行）
- ExtractService 驱动多层嵌套解压（含 RAR 分卷规范化、伪装 ZIP 剥离兜底、暂停/进度回调）
关键入口：analyze_file() / create_engine() / PauseController / ExtractService.extract()
依赖：sevenzip、passwords.resolution（密码候选/字典，旧名经 re-export）、db（仅 main() 初始化）、标准库（subprocess/zipfile/ctypes）
注意：7z 命令绝不能带 -p（裸 -p 走控制台读密码，stdin 管道读不到，密码永远不生效）
"""
import argparse
import ctypes
import os
import re
import shutil
import struct
import subprocess
import sys
import tempfile
import threading
import time
import uuid
import zipfile
from collections import deque
from pathlib import Path

from .config import delete_policy_permanent_fallback
from .deletion import engine as deletion_engine
# 密码候选/字典实现已移至 passwords/resolution.py；旧名 re-export 保留在
# extract 命名空间，调用点与测试打桩（ex.get_dict_passwords）语义不变。
from .passwords.resolution import (  # noqa: F401
    get_password_for_layer,
    load_password_dict,
    save_password_dict,
    get_dict_passwords,
    add_dict_password,
)

CREATE_NO_WINDOW = getattr(subprocess, "CREATE_NO_WINDOW", 0)

ARCHIVE_EXTS = {
    "zip", "7z", "rar", "tar", "gz", "bz2", "xz",
    "tgz", "tbz", "tbz2", "txz",
}
EXT_FORMATS = {
    "zip": "zip", "rar": "rar", "7z": "7z", "tar": "tar",
    "gz": "gz", "gzip": "gz", "bz2": "bz2", "bzip2": "bz2", "xz": "xz",
}

DO_NOT_EXTRACT_SUFFIXES = (".apk", ".apks", ".xapk", ".aab", ".ipa", ".obb")


def is_do_not_extract(name):
    """移动安装包/扩展包等「成品交付物」后缀，自动解压会破坏其完整性，应保持原样"""
    n = name.lower()
    return any(n.endswith(s) for s in DO_NOT_EXTRACT_SUFFIXES)


CONTENT_STOP_MIN_FILES = 30
CONTENT_STOP_MIN_SMALL = 10
CONTENT_STOP_MIN_BUCKETS = 3


def looks_like_complete_content(paths):
    """启发式：解出的内容里存在大量大小不一的零碎文件时，通常已到达真实内容层，
    继续剥壳反而会破坏成品（如 apk 等），此时判定解压已完成。"""
    files = [p for p in paths if p.is_file()]
    if len(files) < CONTENT_STOP_MIN_FILES:
        return False
    sizes = []
    for p in files:
        try:
            sizes.append(p.stat().st_size)
        except OSError:
            continue
    small = sum(1 for s in sizes if 0 < s < 1024 * 1024)
    if small < CONTENT_STOP_MIN_SMALL:
        return False
    buckets = len({s // (1024 * 1024) for s in sizes})
    return buckets >= CONTENT_STOP_MIN_BUCKETS

PASSWORD_PATTERNS = [
    "解压密码", "密码", "pw:", "password:", "pass:", "pwd:",
    "_pw", "-pw", "【密码", "[密码", "（密码",
]

VOLUME_SKIP_PATTERNS = [
    re.compile(r"\.part\d{2,}\.rar$"),
    re.compile(r"\.z\d{2}$"),
    re.compile(r"\.r\d{2}$"),
    re.compile(r"\.(00[2-9]|0[1-9]\d|[1-9]\d{2})$"),
]

DICT_FILE = Path.home() / ".smart_extract_password_dict.json"

SEVENZIP_CANDIDATES = [
    Path("7z.exe"),
    Path(r"C:\Program Files\7-Zip\7z.exe"),
    Path(r"C:\Program Files (x86)\7-Zip\7z.exe"),
]


POLYGLOT_FULL_SCAN_LIMIT = 64 * 1024 * 1024
POLYGLOT_EOCD_RANGE = 65557


def _scan_chunk_for_archive(data):
    if b"PK\x05\x06" in data or b"PK\x06\x07" in data:
        return "zip"
    if b"Rar!\x1a\x07\x00" in data:
        return "rar"
    if b"7z\xbc\xaf\x27\x1c" in data:
        return "7z"
    return None


def _scan_tail_for_archive(path):
    try:
        size = path.stat().st_size
        with open(path, "rb") as f:
            f.seek(max(0, size - POLYGLOT_EOCD_RANGE))
            tail = f.read()
    except OSError:
        return None
    return _scan_chunk_for_archive(tail)


def _scan_sfx_for_archive(path):
    """PE 可执行文件（SFX 自解压包）内嵌压缩包检测。

    SFX 结构：MZ/PE 壳 + 紧跟的压缩数据（7z 签名在压缩数据开头）。
    7z 签名不在文件末尾，尾部扫描找不到；全量扫描又有大小上限（大文件
    如 38GB 会被跳过）。这里扫描壳后面的前 32MB 找归档签名。"""
    try:
        with open(path, "rb") as f:
            header = f.read(2)
            if header != b"MZ":
                return None
            f.seek(1024)
            data = f.read((32 << 20) - 1024)  # 壳之后的前 32MB
    except OSError:
        return None
    for marker, fmt in ((b"7z\xbc\xaf\x27\x1c", "7z"),
                        (b"PK\x03\x04", "zip"),
                        (b"PK\x05\x06", "zip"),
                        (b"Rar!\x1a\x07", "rar")):
        if marker in data:
            return fmt
    return None


def _full_scan_for_archive(path):
    try:
        size = path.stat().st_size
    except OSError:
        return None
    if size > POLYGLOT_FULL_SCAN_LIMIT:
        # 大文件：全量扫描会拖垮 IO。改为「前部扫描」——伪装格式的
        # 压缩包签名通常紧跟图片/视频头之后（如 JPEG 头 + 7z 数据，
        # 签名在文件 0.01% 处）。只扫前 32MB 就足够覆盖这类伪装，
        # 避免对整个 1GB+ 文件逐块读。
        return _scan_front_for_archive(path)
    found = None
    try:
        with open(path, "rb") as f:
            while True:
                chunk = f.read(1 << 20)
                if not chunk:
                    break
                found = _scan_chunk_for_archive(chunk)
                if found:
                    break
    except OSError:
        return None
    return found


# 大文件前部扫描窗口：图片/视频头 + 紧随其后的压缩包签名通常落在这段
POLYGLOT_FRONT_SCAN_LIMIT = 32 * 1024 * 1024


def _scan_front_for_archive(path):
    """扫描大文件开头一段（前 32MB）找压缩包签名。

    适用场景：JPEG/PNG/视频头（几 KB~几 MB）+ 紧跟的真实压缩包数据
    （如 JPEG Exif 头 + 7z 加密包，签名在文件前部约 0.01% 处）。
    这类伪装不在文件末尾，尾部扫描找不到；又因文件巨大全量扫描被跳过，
    前部扫描是唯一可行且廉价的检测方式。"""
    try:
        with open(path, "rb") as f:
            data = f.read(POLYGLOT_FRONT_SCAN_LIMIT)
    except OSError:
        return None
    return _scan_chunk_for_archive(data)


def _confirm_polyglot(path, fmt):
    if fmt == "zip":
        try:
            return "zip" if zipfile.is_zipfile(path) else None
        except OSError:
            return "zip"
        except zipfile.BadZipFile:
            # 多盘分卷 zip（base.zip + base.z01...zNN）：Python zipfile 不支持
            # （"zipfiles that span multiple disks are not supported"），
            # 但 7-Zip 能解。尾部已扫到 EOCD 签名，直接按 zip 处理。
            return "zip"
    return fmt


def _header_matches_format(path, fmt):
    """文件头部魔数是否确实匹配该格式（防止顶着 .zip 后缀的伪装文件被当普通 zip）"""
    try:
        with open(path, "rb") as f:
            header = f.read(8)
    except OSError:
        return False
    if fmt == "zip":
        return header[:4] in (b"PK\x03\x04", b"PK\x05\x06")
    if fmt == "7z":
        return header[:6] == b"7z\xbc\xaf\x27\x1c"
    if fmt == "rar":
        return header[:4] == b"Rar!"
    if fmt == "gz":
        return header[:2] == b"\x1f\x8b"
    if fmt == "bz2":
        return header[:3] == b"BZh"
    if fmt == "xz":
        return header[:6] == b"\xfd7zXZ\x00"
    if fmt == "tar":
        try:
            with open(path, "rb") as f:
                f.seek(257)
                return f.read(5) == b"ustar"
        except OSError:
            return False
    return False


def detect_archive_format(path):
    """返回 (格式, 是否多段伪装)。

    多段伪装 = 文件头部是真实内容（如视频），真实压缩包被内嵌在文件末尾，
    直接把扩展名改成压缩包后缀即可解压。
    """
    try:
        with open(path, "rb") as f:
            header = f.read(8)
        if header[:4] == b"PK\x03\x04":
            return "zip", False
        if header[:4] == b"Rar!":
            return "rar", False
        if header[:4] == b"7z\xbc\xaf":
            return "7z", False
        if header[:2] == b"\x1f\x8b":
            return "gz", False
        if header[:3] == b"BZh":
            return "bz2", False
        if header[:4] == b"\xfd7zXZ":
            return "xz", False
        with open(path, "rb") as f:
            f.seek(257)
            if f.read(5) == b"ustar":
                return "tar", False
    except OSError:
        pass
    ext_fmt = format_from_extension(path)
    if ext_fmt:
        # 扩展名是压缩包后缀但头部对不上（如 MP4 伪装顶着 .zip 后缀）：
        # 7-Zip 打不开，必须先按多段伪装剥离，不能直接当普通 zip 处理。
        if _header_matches_format(path, ext_fmt):
            return ext_fmt, False
    tail = _confirm_polyglot(path, _scan_tail_for_archive(path))
    if tail:
        return tail, True
    # SFX 自解压包：MZ 头 + 内嵌 7z/zip/rar（签名在文件中部，不在尾部）
    sfx = _scan_sfx_for_archive(path)
    if sfx:
        return _confirm_polyglot(path, sfx), True
    full = _confirm_polyglot(path, _full_scan_for_archive(path))
    if full:
        return full, True
    if ext_fmt:
        return ext_fmt, False
    return None, False


def detect_format_by_magic(path):
    fmt, _ = detect_archive_format(path)
    return fmt


def format_from_extension(path):
    return EXT_FORMATS.get(path.suffix.lower().lstrip("."))


def sanitize_filename(name):
    return name.replace("删", "")


def is_disguised(path, real_format):
    if not real_format:
        return False
    ext_format = EXT_FORMATS.get(path.suffix.lower().lstrip("."))
    return ext_format != real_format


def extract_password_from_filename(filename):
    lower = filename.lower()
    for pattern in PASSWORD_PATTERNS:
        pos = lower.find(pattern.lower())
        if pos >= 0:
            password = ""
            for ch in filename[pos + len(pattern):]:
                if ch in "_ -【[（.":
                    break
                password += ch
            if password:
                return password
    return None


def detect_volumes_quick(path):
    name = path.name
    lower = name.lower()
    parent = path.parent
    stem = path.stem
    if ".part" in lower and lower.endswith(".rar"):
        return True, path
    if name.endswith(".zip") and (parent / f"{stem}.z01").exists():
        return True, path
    ext = path.suffix
    if len(ext) == 4 and ext[1].lower() == "z" and ext[2:].isdigit():
        zip_file = parent / f"{stem}.zip"
        if zip_file.exists():
            return True, zip_file
        return True, None
    if len(ext) == 4 and ext[1:].isdigit():
        if ext == ".001":
            return True, path
        master = parent / f"{stem}.001"
        if master.exists():
            return True, master
        return True, None
    return False, None


INCOMPLETE_DOWNLOAD_SUFFIXES = (
    ".downloading", ".crdownload", ".download", ".part", ".tmp",
    ".td", ".opdownload", ".uc!",
)


def is_incomplete_download(path):
    """是否为未完成下载的文件（如百度网盘 .baiduyun.p.downloading）。

    这类文件后缀消失（下载完成自动改名）后才可能成为完整压缩包，
    在此之前不能尝试解压。
    """
    name = path.name.lower()
    if name.endswith(".baiduyun.p.downloading"):
        return True
    return any(name.endswith(s) for s in INCOMPLETE_DOWNLOAD_SUFFIXES)


def _strip_download_suffix(name):
    """去掉下载中后缀，得到下载完成后的目标文件名（如
    xxx.7z.002.baiduyun.p.downloading -> xxx.7z.002）。"""
    low = name.lower()
    suffixes = [".baiduyun.p.downloading"] + list(INCOMPLETE_DOWNLOAD_SUFFIXES)
    suffixes.sort(key=len, reverse=True)
    for s in suffixes:
        if low.endswith(s):
            return name[:len(name) - len(s)]
    return name


def is_volume_name(name):
    """是否为分卷文件名：.001/.002、.z01、xxx.partN.rar 等。"""
    low = name.lower()
    ext = Path(name).suffix
    if len(ext) == 4 and ext[1:].isdigit():
        return True
    if len(ext) == 4 and ext[1].lower() == "z" and ext[2:].isdigit():
        return True
    if ".part" in low and low.endswith(".rar"):
        return True
    return False


def is_split_gap_error(archive_name, err_text):
    """嵌套层解压失败是否属于「分卷缺兄弟卷」——应整体判失败并等待补齐/跨目录归拢，
    而不是当成「损坏但可跳过」从而误报成功（假成功）。

    - 分卷名（.001/.zNN/.partN.rar…）+ 三种典型缺卷报错 → 是；
    - 末卷 base.zip / base.rar 不带编号，不在 is_volume_name 内，但 7-Zip 报
      "Missing volume" 说明归档自身元数据已声明是多卷（缺 .z01/.r00 兄弟）→ 是；
    - 截断的独立 zip 只报 Unexpected end of archive，不算（避免误判成缺卷死等）。
    """
    err = err_text or ""
    gap_tokens = ("Unexpected end of archive" in err
                  or "Missing volume" in err
                  or "Cannot open the file as" in err)
    return gap_tokens and (is_volume_name(archive_name) or "Missing volume" in err)


def volume_download_pending(path):
    """分卷是否未到齐：是否还有正在下载中的分卷兄弟文件。

    下载器一般按 .001 → .002 → ... 顺序下载，先下完的 .001 若在其他
    分卷仍在下载时就开始解压，7-Zip 会报 Unexpected end of archive。
    返回 True 表示应等待（暂不开始解压）。
    """
    path = Path(path)
    parent = path.parent
    if not parent.exists():
        return False
    stem = path.stem
    for entry in parent.iterdir():
        if not entry.is_file() or not is_incomplete_download(entry):
            continue
        # 去掉下载中后缀，判断它下载完成后是否属于当前文件的分卷
        target = _strip_download_suffix(entry.name)
        if (target
                and target != path.name
                and is_volume_file(path.name, target, stem)):
            return True
    return False


def analyze_file(path, manual_format=None):
    path = Path(path)
    original_name = path.name
    if is_incomplete_download(path):
        return {
            "path": path,
            "original_name": original_name,
            "sanitized_name": None,
            "detected_format": None,
            "is_disguised": False,
            "is_polyglot": False,
            "is_incomplete": True,
            "extracted_password": None,
            "is_volume": False,
            "volume_master": None,
            "stego_content": None,
        }
    sanitized = sanitize_filename(original_name)
    sanitized_path = path.with_name(sanitized) if sanitized != original_name else None

    if manual_format:
        detected = manual_format.lower()
        is_polyglot = False
    else:
        detected, is_polyglot = detect_archive_format(path)

    disguised = is_disguised(path, detected)

    sevenzip = find_sevenzip_path()
    stego = detect_steganography(path, sevenzip) if disguised and sevenzip else None

    is_volume, volume_master = detect_volumes_quick(path)

    return {
        "path": path,
        "original_name": original_name,
        "sanitized_name": sanitized_path.name if sanitized_path else None,
        "detected_format": detected,
        "is_disguised": disguised,
        "is_polyglot": is_polyglot,
        "is_incomplete": False,
        "extracted_password": extract_password_from_filename(original_name),
        "is_volume": is_volume,
        "volume_master": volume_master,
        "stego_content": stego,
    }


def perform_sanitization(path):
    new_name = sanitize_filename(path.name)
    if new_name != path.name:
        new_path = path.with_name(new_name)
        path.rename(new_path)
        return new_path
    return path


def find_sevenzip_path():
    # 优先隔离版（%APPDATA%\AutoUnpacker\7z）：用户通过程序下载的版本
    # 一定是满足密码安全门槛的新版本，避免被系统里过旧的 7z 抢先。
    try:
        from .sevenzip import ISOLATED_BIN
        if ISOLATED_BIN.exists():
            return ISOLATED_BIN
    except Exception:
        pass
    for c in SEVENZIP_CANDIDATES:
        if c.exists():
            return c
    found = shutil.which("7z")
    if found:
        return Path(found)
    return None


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


def parse_sevenzip_listing(output):
    files = []
    in_listing = False
    for line in output.splitlines():
        t = line.strip()
        if t.startswith("-----"):
            in_listing = True
            continue
        if not in_listing or not t:
            continue
        if t.startswith(" "):
            continue
        parts = t.split()
        if parts and parts[-1] not in (".", ".."):
            files.append(parts[-1])
    return files


def detect_steganography(path, sevenzip):
    # 不带 -p：密码走 stdin（此处无需密码），避免裸 -p 走控制台读密码卡住
    r = run_silent([str(sevenzip), "l", "-t#", str(path)])
    if r.returncode != 0:
        return None
    archives = [f for f in parse_sevenzip_listing(r.stdout)
                if Path(f).suffix.lower().lstrip(".") in ARCHIVE_EXTS]
    return archives or None


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
            raw_logs.append("--- 7-Zip 原始输出 ---")
            raw_logs.extend(str(last_raw).splitlines())
        return {"success": False, "used_password": None, "encrypted": bool(encrypted),
                "error": concise, "raw_error": last_raw or last_error,
                "logs": raw_logs}


def create_engine(kind, custom_path=None):
    if kind == "zip":
        return PythonZipEngine()
    sevenzip = Path(custom_path) if custom_path else find_sevenzip_path()
    if sevenzip is None or not sevenzip.exists():
        raise SystemExit("未找到 7-Zip，请安装或使用 --engine zip 或指定 --7z-path")
    # 版本门槛：低于 18.00 无法经 stdin 传密码，密码只能拼命令行（会被
    # 任务管理器/WMI 窥探）。低版本一律拒绝使用，由调用方回退 zip 引擎。
    try:
        from .sevenzip import check_version_ok, version_text
        if not check_version_ok(sevenzip):
            raise SystemExit(
                f"7-Zip 版本过低（{version_text(sevenzip)}），低于 18.00 无法安全传密码。"
                f"请通过「设置 → 7-Zip 管理」下载隔离版/全局版，或使用 --engine zip")
    except ImportError:
        pass  # sevenzip_manager 不可用时维持旧行为（正常情况不会发生）
    return SevenZipEngine(sevenzip)


def should_skip_volume(name):
    """是否为「非首卷」分卷（应跳过，只让首卷进队列，避免整套分卷被拆成多个独立项）。

    兼容 .partN.rar / .partN(N).rar（下载批次括号标记）/ .z01 / .r00 / .001 等。
    首卷（.part1/.part01/.z01/.r00/.001）不跳过；否则整套因首卷缺失而无法解压，
    且嵌套文件留在临时目录会被清理丢失。

    注意：不能只看正则（旧的 \\.part\\d{2,}\\.rar$ 会把首卷 .part01 也当非首卷、
    也不认 .part01(1).rar 这类括号命名）。"""
    info = _part_info(name)
    if info:
        return info[1] > 1   # part01 → 首卷(不跳)，part02.. → 跳过
    return any(p.search(name) for p in VOLUME_SKIP_PATTERNS)


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


def strip_embedded_zip(path, dest_dir):
    """把多段伪装文件内嵌的 ZIP 部分剥离成独立 zip 文件（7-Zip 对超大/越界偏移的
    内嵌 ZIP64 打不开，剥离后可正常处理，含 AES 加密）。失败返回 None。"""
    try:
        path = Path(path)
        with zipfile.ZipFile(path) as zf:
            entries = [i for i in zf.infolist() if not i.is_dir()]
            if not entries:
                return None
            start = min(i.header_offset for i in entries)
        with open(path, "rb") as f:
            f.seek(0, 2)
            size = f.tell()
            if size - start <= 0:
                return None
            f.seek(max(0, size - 65557))
            tail = f.read()
        eocd = tail.rfind(b"PK\x05\x06")
        if eocd < 0:
            return None
        clen = struct.unpack_from("<H", tail, eocd + 20)[0]
        end = (size - (len(tail) - eocd)) + 22 + clen
        if end <= start:
            return None
        dest_dir = Path(dest_dir)
        dest_dir.mkdir(parents=True, exist_ok=True)
        dest = dest_dir / f"poly_{path.stem[:20]}_{uuid.uuid4().hex[:6]}.zip"
        with open(path, "rb") as src, open(dest, "wb") as out:
            src.seek(start)
            remaining = end - start
            while remaining > 0:
                chunk = src.read(min(8 * 1024 * 1024, remaining))
                if not chunk:
                    break
                out.write(chunk)
                remaining -= len(chunk)
        if remaining > 0:
            # 重新组装中断的临时 zip 是失败中间产物：移入回收站，绝不永久删除
            _recycle_paths([dest], permanent_fallback=False)
            return None
        return dest
    except Exception:
        return None


# 常见伪装载体扩展名：这些后缀不是压缩包，但内容可能是伪装压缩包
# （JPEG 头 + 7z 数据、MP4 + zip 等）。监听器对这类文件做深层格式探测，
# 其他无关后缀（.txt/.log/.py 等）不做 IO 扫描，避免无谓开销。
DISGUISE_CARRIER_EXTS = {
    "jpg", "jpeg", "png", "gif", "webp", "bmp", "ico", "tif", "tiff",
    "mp4", "mkv", "avi", "mov", "wmv", "flv", "webm", "mp3", "wav",
    "flac", "m4a", "aac", "pdf", "doc", "docx", "xls", "xlsx",
}


def is_archive_file(path):
    """判断文件是否可能是压缩包（含伪装格式）。

    - 扩展名是压缩包后缀 → True
    - 扩展名是常见伪装载体（图片/视频/音频/文档）→ 用深层格式探测
      （detect_archive_format 会扫头部 magic + 尾部 + 前部 + 全量小文件），
      识别「头部伪装 + 内嵌真实压缩包」的多段伪装文件
    -     其他扩展名 → False
    """
    path = Path(path)
    if not path.is_file():
        return False  # 不存在/已被消费：不视为压缩包，避免下游对幽灵文件操作
    ext = path.suffix.lower().lstrip(".")
    if ext in ARCHIVE_EXTS:
        return True
    if ext in DISGUISE_CARRIER_EXTS:
        try:
            fmt, _ = detect_archive_format(path)
            return fmt is not None
        except Exception:
            return False
    return False


def _stage_rar_volumes(source):
    """规范化 RAR 分卷命名：分卷组里只要有一个卷后缀非标准（如 .part1.除rar
    或 part3.删除rar 而其余是 .rar），7-Zip 就会按首卷命名去找兄弟卷而报
    Missing volume（例如找 MBM717.part3.rar 但实际是 .part3.删除rar）。

    把同基础名的所有分卷硬链接到源目录下的临时子目录，统一命名为
    .partN.rar 供 7-Zip 识别。返回 (规范后的首卷路径, 临时目录) 或 None。
    """
    source = Path(source)
    pinfo = _part_info(source.name)
    if not pinfo:
        return None
    base, num = pinfo
    if num != 1:
        return None  # 只从首卷进入
    vols = []
    try:
        for entry in source.parent.iterdir():
            if entry.is_file():
                pi = _part_info(entry.name)
                if pi and pi[0] == base:
                    vols.append((pi[1], entry))
    except OSError:
        return None
    if not vols:
        return None
    # 全套卷都是标准 .partN.rar 命名时无需处理（7-Zip 可直接识别为同套分卷）；
    # 任一卷非标准（如 .part1.除rar、part3.删除rar、feal.part01(2).rar 这类
    # 带括号/多余标记的命名）就整体规范化——否则 7-Zip 会按字面名去找
    # "feal.part01(3).rar" 这类不存在的兄弟卷而报 Missing volume。
    def _standard_name(n):
        return bool(PART_RE.match(n))
    if all(_standard_name(v.name) for _, v in vols):
        return None
    # 临时文件名用「去掉批次标记」的基础名（feal(1) → feal），交给 7-Zip
    # 的仍是干净的 .partN.rar 命名；集合标识 base 含标记仅用于分组。
    plain_base = re.sub(r"\.part\d+(?:\([^)]*\))?\.[^.]+$", "", source.name, flags=re.I)
    stage = source.parent / f".stage_{uuid.uuid4().hex[:6]}"
    try:
        stage.mkdir(exist_ok=True)
        for pn, entry in vols:
            dest = stage / f"{plain_base}.part{pn}.rar"
            try:
                os.link(str(entry), str(dest))
            except OSError:
                shutil.copy2(str(entry), str(dest))
        master = stage / f"{plain_base}.part1.rar"
        if not master.exists():
            raise OSError("staging master missing")
        return master, stage
    except Exception:
        # staging 构造失败：半成品临时目录移入回收站（绝不永久删除）
        _recycle_paths([stage], permanent_fallback=False)
        return None


def _stage_fake_volume(source, fmt):
    """假分卷名的完整压缩包：把文件硬链接/复制到临时子目录并改名为标准后缀。

    打包方把完整 zip 改名为 .z11/.111/.partN.rar 等分卷样式的后缀迷惑，
    7-Zip 会因后缀误判为 split 分卷而报 Missing volume。在临时目录把
    文件改名为对应格式的标准后缀（.zip/.rar/.7z）再交给 7-Zip 即可正常解压。
    返回 (规范后的路径, 临时目录) 或 None。"""
    source = Path(source)
    if not source.is_file():
        return None
    ext_map = {"zip": ".zip", "rar": ".rar", "7z": ".7z",
               "gz": ".gz", "bz2": ".bz2", "xz": ".xz", "tar": ".tar"}
    ext = ext_map.get((fmt or "").lower())
    if not ext:
        return None
    stage = source.parent / f".stage_{uuid.uuid4().hex[:6]}"
    try:
        stage.mkdir(exist_ok=True)
        dest = stage / (source.stem + ext)
        try:
            os.link(str(source), str(dest))  # 同卷硬链接，瞬时完成不占空间
        except OSError:
            shutil.copy2(str(source), str(dest))
        if not dest.exists():
            raise OSError("staging fake volume missing")
        return dest, stage
    except Exception:
        # staging 构造失败：半成品临时目录移入回收站（绝不永久删除）
        _recycle_paths([stage], permanent_fallback=False)
        return None


def unique_dest_path(dest):
    """返回不覆盖已有文件的落点路径：已存在时按 `name (1).ext` 递增找空位。

    与提升内容（promote_extracted_content）用的 name(N) 约定一致（文件带空格
    的形式，避免与目录提升的 name(N) 混淆）。已存在同名文件时绝不 shutil.move
    覆盖——用户放在输出目录里的同名文件必须原样保留。"""
    dest = Path(dest)
    if not dest.exists():
        return dest
    parent, stem, suffix = dest.parent, dest.stem, dest.suffix
    i = 1
    while True:
        cand = parent / f"{stem} ({i}){suffix}"
        if not cand.exists():
            return cand
        i += 1


class ExtractService:
    def __init__(self, engine, options):
        self.engine = engine
        self.options = options
        self.logs = []
        self.layer_records = []
        self.temp_dirs = set()
        self.temp_root = Path(tempfile.gettempdir())
        # 失败路径标记：置位后 _cleanup 把中间目录移入回收站（绝不永久删除）；
        # 成功路径保持原有 rmtree 行为不变。
        self.recycle_cleanup = False

    def emit(self, msg):
        self.logs.append(msg)
        print(msg)

    def _make_temp(self, task_id, depth):
        path = self.temp_root / f"extract_{task_id}_{depth}"
        if path.exists():
            shutil.rmtree(path, ignore_errors=True)
        path.mkdir(parents=True, exist_ok=True)
        self.temp_dirs.add(path)
        return path

    def _cleanup(self):
        for p in self.temp_dirs:
            self._release_temp(p)
        self.temp_dirs.clear()
        for p in getattr(self, "stage_dirs", []):
            self._release_temp(p)
        self.stage_dirs.clear()

    def _release_temp(self, path):
        """清掉一个中间目录：成功路径维持 rmtree；失败路径移入回收站
        （用户规则：解压失败的中间文件必须可恢复，绝不永久删除）。"""
        if self.recycle_cleanup:
            _recycle_paths([path], permanent_fallback=False)
        else:
            shutil.rmtree(path, ignore_errors=True)

    @staticmethod
    def _retry_unlink(path, max_attempts=5):
        """删除文件，短暂重试几次。刚解压出来的大文件可能被 7z 进程/
        杀软实时扫描瞬时占用，一次性 unlink 容易静默失败留下残留。"""
        path = Path(path)
        for attempt in range(max_attempts):
            try:
                path.unlink()
                return True
            except OSError:
                if attempt >= max_attempts - 1:
                    return False
                time.sleep(0.3)
        return False

    def extract(self, task):
        self.temp_root = self._temp_root_for(task.get("output_dir"))
        self.stage_dirs = []
        self.recycle_cleanup = False
        try:
            result = self._extract_inner(task)
        except BaseException:
            # 异常中断同样按失败处理：中间目录走回收站
            self.recycle_cleanup = True
            raise
        else:
            if not is_clean_success(result):
                self.recycle_cleanup = True
            return result
        finally:
            self._cleanup()

    @staticmethod
    def _temp_root_for(output_dir):
        """临时目录与输出目录同盘（同盘最终移动=瞬间改名，避免跨盘搬运与发热）；
        输出在系统盘时沿用系统临时目录，避免盘根建目录的权限问题"""
        try:
            anchor = Path(output_dir).anchor
            sys_anchor = Path(tempfile.gettempdir()).anchor
            if anchor and len(anchor) == 3 and anchor != sys_anchor:
                return Path(anchor) / "ExtractTemp"
        except Exception:
            pass
        return Path(tempfile.gettempdir())

    def _extract_inner(self, task):
        task_id = task["id"]
        output_dir = Path(task["output_dir"])
        user_passwords = task.get("passwords", [])
        original_size = Path(task["source_path"]).stat().st_size
        max_depth = self.options["max_depth"]

        source_name = Path(task["source_path"]).name
        # 分卷首卷（.001/.part1/.z01…）：首卷拼出的载荷打不开 ⇒ 缺兄弟分卷
        root_is_first_volume = (is_volume_name(source_name)
                                and is_first_volume(source_name))
        failed_layers = []        # 任何失败层都让整体结果不再是成功
        split_incomplete = False  # 分卷链不完整（缺兄弟分卷）

        queue = deque([{"archive": Path(task["source_path"]),
                        "depth": 1}])

        while queue:
            item = queue.popleft()
            depth = item["depth"]
            if depth > max_depth:
                self.emit(f"[第{depth}层] 超过最大深度限制 {max_depth}")
                break

            if self.options["mode"] == "direct" and depth == 1:
                extract_dir = output_dir
                is_direct = True
            else:
                extract_dir = self._make_temp(task_id, depth)
                is_direct = False

            info = analyze_file(item["archive"])
            self.emit(f"[第{depth}层] 开始解压: {item['archive'].name} (格式: {info['detected_format'] or '未知'})")

            # 嵌套层也可能是非标准命名分卷（如 feal.part01(1).rar 这类带括号
            # 批次标记、或 .part1.除rar 改后缀）：7-Zip 按字面名找兄弟卷会报
            # Missing volume。规范化成 .partN.rar（硬链接到临时子目录）再解压。
            extract_src = item["archive"]
            if info.get("detected_format") == "rar":
                staged_info = _stage_rar_volumes(extract_src)
                if staged_info:
                    extract_src, stage_dir = staged_info
                    self.stage_dirs.append(stage_dir)
                    self.emit(f"[第{depth}层] 分卷命名不规范，已规范化后交由引擎: {extract_src.name}")

            # 嵌套层密码优先沿用上一层成功密码（内外层常共用同一密码，
            # 机械按层序号取密码列表第 N 个容易取错，如外层用第 1 个、
            # 内层却是第 2 个）。再补上本层序号映射与其余候选。
            prev_pw = None
            for rec in reversed(self.layer_records):
                if rec.get("success") and rec.get("used_password"):
                    prev_pw = rec["used_password"]
                    break
            passwords = get_password_for_layer(
                depth, user_passwords,
                extracted=info["extracted_password"],
                default=self.options.get("default_password"),
                dict_passwords=get_dict_passwords() if self.options.get("use_dict") else [],
                prev_used=prev_pw,
            )

            layer_task = {
                "id": f"{task_id}-layer-{depth}",
                "source_path": extract_src,
                "output_dir": extract_dir,
                "passwords": passwords,
                "progress_cb": task.get("progress_cb"),
                "pauser": task.get("pauser"),
            }

            result = self.engine.extract(layer_task, self.options, depth)
            if (not result["success"]
                    and info["detected_format"] == "zip"
                    and isinstance(self.engine, SevenZipEngine)
                    and is_zip_open_error(result_raw_error(result))):
                fallback_attempts = []
                if info.get("is_polyglot"):
                    stripped = strip_embedded_zip(item["archive"], self.temp_root)
                    if stripped:
                        self.emit(f"[第{depth}层] 7-Zip 打不开多段伪装 ZIP（可能是 ZIP64/大文件偏移），剥离伪装头后重试")
                        retry_task = dict(layer_task)
                        retry_task["source_path"] = stripped
                        retry_out = SevenZipEngine(self.engine.path).extract(
                            retry_task, self.options, depth)
                        fallback_attempts.append(retry_out)
                        # 剥离出的临时 zip 是中间产物：重试成功维持原样删除，
                        # 失败移入回收站（绝不永久删除）
                        if retry_out.get("success"):
                            try:
                                stripped.unlink(missing_ok=True)
                            except OSError:
                                pass
                        else:
                            _recycle_paths([stripped], permanent_fallback=False)
                if any(a["success"] for a in fallback_attempts):
                    result = next(a for a in fallback_attempts if a["success"])
                elif self._all_entries_extracted(extract_dir, item["archive"]):
                    # 7-Zip 已经把全部文件都解出来了，只是返回了非零警告码（常见于
                    # 目录条目的 Unavailable data）。此时若再用 Python 全量重解，会因
                    # 文件名解码不同把同一批文件写成另一份不同名字（GBK vs cp437），
                    # 且耗时翻倍。判定已完成，直接按成功处理。
                    self.emit(f"[第{depth}层] 7-Zip 已解出全部文件但返回警告码，按成功处理")
                    best = fallback_attempts[0] if fallback_attempts else result
                    result = {"success": True,
                              "used_password": best.get("used_password"),
                              "encrypted": best.get("encrypted", False),
                              "error": None,
                              "logs": list(best.get("logs") or [])
                                       + ["[警告] 7-Zip 返回非零退出码，但校验文件数量与大小后确认全部解出"]}
                else:
                    self.emit(f"[第{depth}层] 改用 Python zipfile 重试")
                    fallback_attempts.append(PythonZipEngine().extract(layer_task, self.options, depth))
                    winner = next((a for a in fallback_attempts if a["success"]), None)
                    if winner is not None:
                        result = winner
                    else:
                        errs = [a["error"] for a in fallback_attempts if a.get("error")]
                        for a in fallback_attempts:
                            result["logs"].extend(a["logs"])
                        result["error"] = "；".join(dict.fromkeys(errs)) or result["error"]
            for log in result["logs"]:
                self.emit(f"[第{depth}层] {log}")

            if not result["success"]:
                if depth == 1:
                    self.emit(f"[第{depth}层] 解压失败: {result['error']}")
                    # 首卷打不开/被截断 ⇒ 分卷链不完整（缺兄弟分卷），不是终态失败
                    split_incomplete = (root_is_first_volume
                                        and is_archive_open_error(result_raw_error(result)))
                    if split_incomplete:
                        self.emit(f"[第{depth}层] 分卷链不完整（缺少兄弟分卷，"
                                  f"首卷拼出的载荷不是有效归档），保留源文件等待到齐")
                    # 第 1 层失败同样入 layer_records（失败层必须留痕，供审计）
                    failed_record = {
                        "layer": depth,
                        "archive_name": item["archive"].name,
                        "used_password": None,
                        "success": False,
                        "error": result["error"],
                    }
                    self.layer_records.append(failed_record)
                    failed_layers.append(failed_record)
                    self.recycle_cleanup = True   # 失败：中间文件走回收站
                    self._cleanup()
                    err_text = result["error"]
                    if split_incomplete:
                        err_text = f"分卷链不完整（缺少兄弟分卷），{err_text}"
                    failure = {
                        "task_id": task_id, "success": False,
                        "incomplete": True,
                        "failed_layers": failed_layers,
                        "depth_reached": len(self.layer_records),
                        "extracted_files": [], "used_password": None,
                        "layer_records": self.layer_records,
                        "logs": self.logs, "error": err_text,
                    }
                    if split_incomplete:
                        failure["split_incomplete"] = True
                    return failure
                # 嵌套层失败：失败文件先保留（移到输出目录），但任何失败层都会
                # 让整体判失败（见循环末尾 failed_layers 汇总）——不提升、不删源、
                # 不报「完成」；前面层已解出的内容由失败回退统一移入回收站。
                # 例外：嵌套层是分卷且报缺卷/打不开（Unexpected end / Missing
                # volume / Cannot open）时，说明分卷未到齐或兄弟卷未归拢，
                # 不应当作"已完成"吞掉失败——整体标记为失败，让监听层 defer
                # 重试（等分卷到齐 / 修复归拢后再解），避免把半成品当成品。
                err_text = result_raw_error(result)
                # 末卷 base.zip / base.rar 不带编号，不在 is_volume_name 内；但 7-Zip
                # 报 "Missing volume" 说明归档自身元数据已声明是多卷（缺 .z01/.r00
                # 兄弟卷）。因此把 "Missing volume" 作为独立的判定依据。截断的独立
                # zip 只报 Unexpected end of archive，不会误入此分支。
                is_split_gap = is_split_gap_error(item["archive"].name, err_text)
                if is_split_gap:
                    self.emit(f"[第{depth}层] 嵌套分卷缺卷（{err_text[:80]}），整体判失败待重试")
                    self.layer_records.append({
                        "layer": depth,
                        "archive_name": item["archive"].name,
                        "used_password": None,
                        "success": False,
                        "error": result["error"],
                    })
                    # 保留缺兄弟卷的锚点（该分卷），供监听层跨目录归拢后重试：锚点
                    # 若已在输出目录（直接模式）就原地不动；若在临时目录则搬到输出
                    # 目录，避免 _cleanup / 失败回退把它删掉。
                    anchor = item["archive"]
                    try:
                        if (anchor.exists()
                                and anchor.parent != output_dir
                                and output_dir not in anchor.parents):
                            dest = output_dir / anchor.name
                            if dest.exists():
                                dest = output_dir / f"{anchor.stem}_failed{anchor.suffix}"
                            dest.parent.mkdir(parents=True, exist_ok=True)
                            shutil.move(str(anchor), str(dest))
                            anchor = dest
                    except OSError as e:
                        self.emit(f"[第{depth}层] 保留缺卷锚点失败: {e}")
                    return {
                        "task_id": task_id, "success": False,
                        "depth_reached": len(self.layer_records),
                        "extracted_files": [], "used_password": None,
                        "layer_records": self.layer_records,
                        "logs": self.logs, "error": result["error"],
                        "split_gap_archive": str(anchor),   # 缺卷锚点：供跨目录归拢
                        "keep_output_dir": True,            # 失败回退时保留输出目录
                    }
                self.emit(f"[第{depth}层] 嵌套解压失败: {result['error']}（已跳过，保留原文件）")
                try:
                    failed_archive = item["archive"]
                    # 已在输出目录里的就地保留；只有仍留在临时目录的才搬过去。
                    # 否则 dest 恰好就是它自己 → dest.exists() 为真 → 会被误改成
                    # 「_failed」后缀，破坏分卷系列名（后续无法按系列名归拢）。
                    if failed_archive.exists() and failed_archive.parent != output_dir:
                        dest = output_dir / failed_archive.name
                        if dest.exists():
                            dest = output_dir / f"{failed_archive.stem}_failed{failed_archive.suffix}"
                        dest.parent.mkdir(parents=True, exist_ok=True)
                        shutil.move(str(failed_archive), str(dest))
                        self.emit(f"[第{depth}层] 已保留失败文件: {dest.name}")
                except OSError as e:
                    self.emit(f"[第{depth}层] 保留失败文件出错: {e}")
                failed_record = {
                    "layer": depth,
                    "archive_name": item["archive"].name,
                    "used_password": None,
                    "success": False,
                    "error": result["error"],
                }
                self.layer_records.append(failed_record)
                failed_layers.append(failed_record)
                # 首卷拼出的直接载荷（第 2 层）打不开 ⇒ 分卷链被截断（缺兄弟
                # 分卷），不能当「嵌套失败可跳过」吞掉：整体判失败等分卷补齐。
                if (depth == 2 and root_is_first_volume
                        and is_archive_open_error(result_raw_error(result))):
                    split_incomplete = True
                    self.emit(f"[第{depth}层] 分卷链不完整（缺少兄弟分卷，"
                              f"首卷拼出的载荷不是有效归档），整体判失败待重试")
                continue

            used_password = result["used_password"]
            # 加密信号与 used_password 同源：老测试桩/老引擎结果没有该键时，退回
            # 「used_password 非空即视为用到密码」，保持既有语义不回归。
            encrypted = bool(result.get("encrypted", used_password is not None))
            self.emit(f"[第{depth}层] 使用密码: {used_password or '无密码'}")
            self.layer_records.append({
                "layer": depth,
                "archive_name": item["archive"].name,
                "used_password": used_password,
                "encrypted": encrypted,
                "success": True,
            })

            # 本层源压缩包已被解压消费，先删掉（含其分卷兄弟），再移动解出内容。
            # 必须提前：若解出内容里有与源压缩包同名的条目（如层层同名文件夹
            # 2026年07月），晚删会让 move 撞上「已存在的源文件」报 WinError 183。
            if depth > 1 and item["archive"].exists():
                if not self._retry_unlink(item["archive"]):
                    self.emit(f"[第{depth}层] 警告: 删除主卷失败: {item['archive'].name}")
            if depth > 1 and is_volume_name(item["archive"].name):
                # 仅当被解压的本身就是分卷（如 xxx.7z.001）时才清理其分卷兄弟
                #（.002 等）。普通压缩包（如 xx.mp4）执行这步会误删内部嵌套
                # 分卷的 .002（stem 前缀匹配过于宽松），导致下层解压缺卷失败。
                arch_name = item["archive"].name
                arch_stem = item["archive"].stem
                parent = item["archive"].parent
                if parent.exists():
                    for entry in parent.iterdir():
                        if (entry.is_file()
                                and is_volume_file(arch_name, entry.name, arch_stem)):
                            if self._retry_unlink(entry):
                                self.emit(f"[第{depth}层] 已删除分卷文件: {entry.name}")
                            else:
                                self.emit(f"[第{depth}层] 警告: 删除分卷失败: {entry.name}")

            self._check_size(extract_dir, original_size)

            extracted_files = [p for p in extract_dir.rglob("*") if p.is_file()]

            nested_files = self._detect_nested(extracted_files) if self.options["enable_nested"] else []
            if nested_files:
                if looks_like_complete_content(extracted_files):
                    self.emit(f"[第{depth}层] 解压内容已是成品内容（大量大小不一的零碎文件），判定解压完成，停止继续剥壳")
                    if not is_direct:
                        self.move_to_output(extract_dir, output_dir)
                    self.emit(f"[第{depth}层] 完成")
                else:
                    self.emit(f"[第{depth}层] 检测到嵌套压缩包，继续解压下一层")
                    # 先处理「非嵌套」文件（混淆文件、零散小文件）：
                    # 分卷兄弟（如 .001 的 .002）必须留在主卷旁跟主卷一起进下一层，
                    # 不能提前移走；普通非嵌套文件移到输出目录。
                    for f in extracted_files:
                        if f not in nested_files:
                            # 嵌套分卷（如 .001）的分卷兄弟（.002）要留在原地
                            # 跟主卷一起进下一层，不能提前移走，否则下层缺卷解压失败
                            if not is_direct and not any(
                                    is_volume_file(nf.name, f.name, nf.stem)
                                    for nf in nested_files):
                                self.move_file(f, extract_dir, output_dir)
                    # 把散落在子目录里的分卷兄弟收集到首卷旁边：
                    # 多段伪装解出的分卷可能被 7-Zip 按内部目录结构展开
                    #（如 1\xxx.7z.002、2\xxx.7z.003），首卷 .001 在根目录时
                    # 7-Zip 在 .001 同目录找不到兄弟卷会报 Unexpected end of
                    # archive。这里把同系列分卷全部归拢到首卷所在目录。
                    for nf in list(nested_files):
                        if (is_volume_name(nf.name)
                                and _volume_number(nf.name) == 1
                                and not is_fake_volume_name(nf)):
                            series_dir = nf.parent
                            for cand in extract_dir.rglob("*"):
                                if (cand.is_file()
                                        and cand != nf
                                        and is_volume_file(nf.name, cand.name, nf.stem)
                                        and cand.parent != series_dir):
                                    try:
                                        dest = series_dir / cand.name
                                        if dest.exists():
                                            dest = series_dir / f"{cand.stem}_sibling{cand.suffix}"
                                        shutil.move(str(cand), str(dest))
                                        self.emit(f"[第{depth}层] 已归拢分卷兄弟: {cand.name} -> {dest.name}")
                                    except OSError as e:
                                        self.emit(f"[第{depth}层] 归拢分卷兄弟失败: {cand.name}: {e}")
                    queued_any = False
                    for f in nested_files:
                        # 假分卷名的完整压缩包（改后缀迷惑）不是分卷，应继续剥壳；
                        # 真分卷（如 .z01 的兄弟 .z02）才跳过，留给首卷一起处理
                        if (should_skip_volume(f.name)
                                and not is_fake_volume_name(f)):
                            self.emit(f"[第{depth}层] 跳过分卷文件: {f.name}")
                            continue
                        queue.append({"archive": f, "depth": depth + 1})
                        queued_any = True
                    if not queued_any and not is_direct:
                        # 所有嵌套文件都是非首卷（缺首卷）等异常：不能留在临时目录
                        # 被清理丢弃，移入输出目录保留，避免数据丢失。
                        for f in nested_files:
                            self.move_file(f, extract_dir, output_dir)
                        self.emit(f"[第{depth}层] 嵌套分卷缺少首卷，已保留到输出目录（未丢弃）")
            else:
                if not is_direct:
                    self.move_to_output(extract_dir, output_dir)
                self.emit(f"[第{depth}层] 完成")

        if failed_layers:
            self.recycle_cleanup = True   # 失败：中间文件走回收站
        self._cleanup()
        # 正常解压路径也会清理空目录（如内层压缩包所在文件夹在内容被
        # 消费后变空），避免残留空文件夹。
        if output_dir.exists():
            remove_empty_dirs(output_dir)
        if failed_layers:
            # 有层失败就绝不是成功：不提升、不回收源文件、不报「完成」。
            first = failed_layers[0]
            err = f"第{first['layer']}层解压失败: {first['error']}"
            if len(failed_layers) > 1:
                err += f"；共 {len(failed_layers)} 层未解出"
            if split_incomplete:
                err = f"分卷链不完整（缺少兄弟分卷），{err}"
            self.emit(f"[结果] 解压未完成（{err}），已保留源文件")
            return {
                "task_id": task_id, "success": False,
                "incomplete": True,
                "failed_layers": failed_layers,
                "split_incomplete": bool(split_incomplete),
                "depth_reached": len(self.layer_records),
                "extracted_files": [], "used_password": None,
                "layer_records": self.layer_records,
                "logs": self.logs, "error": err,
            }
        final_files = [p for p in output_dir.rglob("*") if p.is_file()] if output_dir.exists() else []
        _first = self.layer_records[0] if self.layer_records else {}
        return {
            "task_id": task_id, "success": True,
            "depth_reached": len(self.layer_records),
            "extracted_files": final_files,
            "used_password": _first.get("used_password"),
            # 顶层「是否真的用到密码」取自与 used_password 同一层（第 1 层），
            # 供 extract_one 据此只记录真实命中，杜绝未加密包误报。
            "encrypted": bool(_first.get("encrypted", False)) if self.layer_records else False,
            "layer_records": self.layer_records,
            "logs": self.logs, "error": None,
        }

    def _detect_nested(self, files):
        return [f for f in files
                if not is_do_not_extract(f.name)
                and (is_archive_file(f) or f.name.lower().endswith(".001") or detect_format_by_magic(f))]

    def move_file(self, src_file, src_dir, dst_dir):
        rel = src_file.relative_to(src_dir)
        dest = dst_dir / rel
        dest.parent.mkdir(parents=True, exist_ok=True)
        final = unique_dest_path(dest)
        if final != dest:
            self.emit(f"[输出] 目标同名已存在，另存为: {final.name}")
        shutil.move(str(src_file), str(final))

    def _check_size(self, dir_path, original_size):
        total = sum(p.stat().st_size for p in dir_path.rglob("*") if p.is_file())
        if original_size > 0:
            ratio = total / original_size
            if ratio > self.options["max_size_ratio"]:
                self.emit(f"[安全警告] 解压后大小膨胀 {ratio:.1f} 倍，可能存在 zip bomb")

    def _all_entries_extracted(self, extract_dir, archive):
        """7-Zip 返回非零退出码后，用 Python 核对输出目录是否已包含归档的全部文件。

        必须比较 (相对路径, 大小) 对而非仅大小多重集：只比大小的话，一个把
        a.bin(1024B) 解出、b.bin(1024B) 漏掉（或改名）的半成品会因大小多重集
        相同被误判为「全部解出」而报成功。路径按 as_posix().lower() 归一化
        （与 _dirs_conflict 的路径比较口径一致），兼容分隔符与 Windows 大小写。"""
        try:
            with zipfile.ZipFile(archive) as zf:
                expected = sorted(
                    (i.filename.replace("\\", "/").lower(), i.file_size)
                    for i in zf.infolist() if not i.is_dir())
        except Exception:
            return False
        if not expected:
            return False
        try:
            root = Path(extract_dir)
            actual = sorted(
                (p.relative_to(root).as_posix().lower(), p.stat().st_size)
                for p in root.rglob("*") if p.is_file())
        except OSError:
            return False
        return actual == expected

    def move_to_output(self, src_dir, dst_dir):
        dst_dir.mkdir(parents=True, exist_ok=True)
        for src in src_dir.rglob("*"):
            rel = src.relative_to(src_dir)
            dest = dst_dir / rel
            if src.is_dir():
                dest.mkdir(parents=True, exist_ok=True)
            elif src.is_file():
                dest.parent.mkdir(parents=True, exist_ok=True)
                final = unique_dest_path(dest)
                if final != dest:
                    self.emit(f"[输出] 目标同名已存在，另存为: {final.name}")
                shutil.move(str(src), str(final))


def default_output_dir(archive):
    try:
        out = archive.with_suffix("")
    except ValueError:
        out = archive
    if not out.name:
        out = archive.parent / f"{archive.name}_extracted"
    if out == archive or (out.exists() and not out.is_dir()):
        out = archive.parent / f"{archive.name}_extracted"
    return out


def parse_passwords(items):
    result = []
    for item in items or []:
        for p in item.split(","):
            if p:
                result.append(p)
    return result


def is_clean_success(result):
    """结果是否为「干净的整体成功」：success 且无失败层/不完整/分卷缺卷标记。

    后处理（提升内容、删除源文件）与「完成」报告的唯一闸门：任何一层失败
    （引擎失败、打不开、错误跳过、CRC/大小不符）或分卷未到齐都不算成功。"""
    if not result or not result.get("success"):
        return False
    return not (result.get("incomplete")
                or result.get("failed_layers")
                or result.get("split_incomplete"))


def build_post_actions(args):
    actions = []
    if args.move_to:
        actions.append({"action_type": "move_to_dir", "target_dir": args.move_to})
    promote_to = getattr(args, "promote_to", None)
    # 源文件删除策略：随动作下传，最终驱动回收站不可用时的回退（keep=保留源文件）。
    delete_policy = getattr(args, "delete_policy", None)
    # quarantine 策略的隔离区根（空串=按源文件所在目录补 _已删除）；其它策略为 None。
    quarantine_root = getattr(args, "quarantine_root", None)
    if promote_to:
        actions.append({"action_type": "promote_content",
                        "promote_to": promote_to,
                        "merge": bool(getattr(args, "promote_merge", False)),
                        "delete_source": bool(getattr(args, "delete_source", False)),
                        "delete_policy": delete_policy,
                        "quarantine_root": quarantine_root,
                        "delete_hook": getattr(args, "delete_hook", None)})
    elif args.delete_source:
        actions.append({"action_type": "delete_source",
                        "delete_policy": delete_policy,
                        "quarantine_root": quarantine_root,
                        "delete_hook": getattr(args, "delete_hook", None)})
    if args.run_script:
        actions.append({"action_type": "run_script",
                        "script_path": args.run_script,
                        "script_args": args.script_args or []})
    return actions


def apply_post_actions(result, source, output_dir, actions):
    if not is_clean_success(result):
        return
    for action in actions:
        try:
            if action["action_type"] == "move_to_dir":
                moved = move_result_dir(output_dir, action["target_dir"])
                result["extracted_files"] = moved
                result["logs"].append(f"已移动 {len(moved)} 个文件到 {action['target_dir']}")
            elif action["action_type"] == "delete_source":
                delete_source(source, action.get("delete_hook"),
                              permanent_fallback=delete_policy_permanent_fallback(
                                  action.get("delete_policy")),
                              quarantine_root=action.get("quarantine_root"))
            elif action["action_type"] == "promote_content":
                res = promote_extracted_content(
                    output_dir, action["promote_to"], source,
                    action.get("delete_hook"), merge=action.get("merge", False),
                    delete_src=action.get("delete_source", False),
                    permanent_fallback=delete_policy_permanent_fallback(
                        action.get("delete_policy")),
                    quarantine_root=action.get("quarantine_root"))
                result["logs"].append(f"[后处理] {res['note']}")
                if res["promoted"]:
                    result["promoted_dir"] = res["promoted"]
            elif action["action_type"] == "run_script":
                run_script(action["script_path"], action["script_args"],
                           source, output_dir, result)
        except Exception as e:
            result["logs"].append(f"[后处理] 失败: {e}")


def move_result_dir(output_dir, target_dir):
    target = Path(target_dir)
    target.mkdir(parents=True, exist_ok=True)
    moved = []
    for src in output_dir.rglob("*"):
        rel = src.relative_to(output_dir)
        dest = target / rel
        if src.is_dir():
            dest.mkdir(parents=True, exist_ok=True)
        elif src.is_file():
            dest.parent.mkdir(parents=True, exist_ok=True)
            final = unique_dest_path(dest)
            shutil.move(str(src), str(final))
            moved.append(final)
    remove_empty_dirs(output_dir)
    return moved


def remove_empty_dirs(directory):
    directory = Path(directory)
    if not directory.exists():
        return
    for child in directory.iterdir():
        if child.is_dir():
            remove_empty_dirs(child)
    try:
        directory.rmdir()
    except OSError:
        pass


def _set_created_time(path, ts):
    """把 Windows「创建时间」设为 ts；非 Windows 无此概念，直接视为成功。

    优先 pywin32（项目热键/托盘层已依赖，打包内已带）；不可用时退回 ctypes。
    打开目录句柄必须带 FILE_FLAG_BACKUP_SEMANTICS，否则 CreateFile 对目录失败。"""
    if os.name != "nt":
        return True
    ft = int((ts + 11644473600) * 10000000)   # 1601-01-01 起的 100ns 计数
    try:
        import pywintypes
        import win32con
        import win32file
    except ImportError:
        pywintypes = None
    if pywintypes is not None:
        handle = win32file.CreateFileW(
            str(path), win32con.GENERIC_WRITE,
            win32con.FILE_SHARE_READ | win32con.FILE_SHARE_WRITE
            | win32con.FILE_SHARE_DELETE,
            None, win32con.OPEN_EXISTING, win32con.FILE_FLAG_BACKUP_SEMANTICS, None)
        try:
            win32file.SetFileTime(handle, pywintypes.Time(ts), None, None)
        finally:
            win32file.CloseHandle(handle)
        return True
    from ctypes import wintypes
    kernel32 = ctypes.windll.kernel32
    kernel32.CreateFileW.restype = ctypes.c_void_p
    kernel32.CreateFileW.argtypes = [wintypes.LPCWSTR, wintypes.DWORD,
                                     wintypes.DWORD, ctypes.c_void_p,
                                     wintypes.DWORD, wintypes.DWORD,
                                     ctypes.c_void_p]
    kernel32.SetFileTime.argtypes = [ctypes.c_void_p,
                                     ctypes.POINTER(wintypes.FILETIME),
                                     ctypes.POINTER(wintypes.FILETIME),
                                     ctypes.POINTER(wintypes.FILETIME)]
    handle = kernel32.CreateFileW(str(path), 0x40000000, 0x1 | 0x2 | 0x4,
                                  None, 3, 0x02000000, None)
    if not handle or handle == ctypes.c_void_p(-1).value:
        return False
    try:
        created = wintypes.FILETIME(ft & 0xFFFFFFFF, (ft >> 32) & 0xFFFFFFFF)
        return bool(kernel32.SetFileTime(handle, ctypes.byref(created), None, None))
    finally:
        kernel32.CloseHandle(ctypes.c_void_p(handle))


def stamp_output_now(paths):
    """把产物顶层条目的 修改/访问/创建时间 校准为现在。

    7-Zip 解压目录时会还原归档里保存的目录时间戳（含创建时间），穿透/提升后的
    成品文件夹会带着压缩包里的旧日期，在大目录里按日期排序就沉到底部找不到。
    这里只校准传入的条目本身，不递归子文件；单个条目失败只记录问题、绝不抛出，
    不影响解压结果。返回 (已校准的路径列表, 失败描述列表)。"""
    now = time.time()
    stamped, problems = [], []
    for p in paths:
        p = Path(p)
        try:
            os.utime(p, (now, now))
        except OSError as e:
            problems.append(f"{p.name}: {e}")
            continue
        try:
            if not _set_created_time(p, now):
                problems.append(f"{p.name}: 创建时间未设置")
        except Exception as e:
            problems.append(f"{p.name}: {e}")
        stamped.append(p)
    return stamped, problems


def _final_output_targets(result, out_dir, pre_entries, args):
    """本次解压最终产物的顶层条目：提升成功=提升出的文件夹；--move-to 搬走内容时
    =目标目录里本次搬出的顶层条目；否则=输出目录里本次新建的顶层条目（用户
    预先放进输出目录的内容不动）。"""
    promoted = result.get("promoted_dir")
    if promoted:
        p = Path(promoted)
        return [p] if p.exists() else []
    moved_to = getattr(args, "move_to", None)
    if moved_to:
        target = Path(moved_to)
        seen, tops = set(), []
        for f in result.get("extracted_files") or []:
            try:
                rel = Path(f).relative_to(target)
            except (OSError, ValueError):
                continue
            if rel.parts and rel.parts[0] not in seen:
                seen.add(rel.parts[0])
                p = target / rel.parts[0]
                if p.exists():
                    tops.append(p)
        return tops
    try:
        return [p for p in Path(out_dir).iterdir() if p not in pre_entries]
    except OSError:
        return []


def _stamp_output_times(result, out_dir, pre_entries, args):
    """干净成功后按 output_time_now 开关校准最终产物顶层时间（失败只提示不抛错）。"""
    try:
        from . import config as app_config
        if not bool(app_config.load_config().get("output_time_now", True)):
            return
    except Exception:
        pass
    targets = _final_output_targets(result, out_dir, pre_entries, args)
    if not targets:
        return
    stamped, problems = stamp_output_now(targets)
    if stamped:
        names = [p.name for p in stamped]
        shown = "、".join(names[:3]) + (f" 等 {len(names)} 项" if len(names) > 3 else "")
        msg = f"已将输出时间戳校准到现在（避免在大目录里被旧日期淹没）: {shown}"
        print(msg)
        result["logs"].append(msg)
    if problems:
        warn = f"输出时间戳部分校准失败（不影响解压）: {'; '.join(problems[:2])}"
        print(warn)
        result["logs"].append(warn)


PART_RE = re.compile(r"^(?P<name>.+)\.part\d+\.rar$")


def _part_info(name):
    """解析 .partN.<后缀> 分卷名，返回 (集合标识, 序号) 或 None。

    集合标识包含 (N) 批次标记：不同批次的同名分卷组是彼此独立的包，
    不能互相视为分卷兄弟。例：
    - feal.part01(2).rar / feal.part02(2).rar → 集合 "feal(2)"
    - feal.part01(1).rar / feal.part02(1).rar → 集合 "feal(1)"
    - feal.part01.rar / feal.part02.rar       → 集合 "feal"
    三者基础名都是 feal，但不加标记就会把 feal.part02.rar 误当成
    feal.part01(1).rar 的兄弟卷，导致解压后把下一层的源卷误删。

    兼容：标准 .partN.rar、非标准后缀 .partN.除rar、带括号 .partN(2).rar。"""
    m = re.match(
        r"^(?P<base>.+)\.part(?P<num>\d+)(?:\((?P<mark>[^)]*)\))?\.(?P<ext>[^.]+)$",
        name, re.I)
    if m:
        base = m.group("base")
        mark = m.group("mark")
        if mark is not None:
            base = f"{base}({mark})"
        return (base, int(m.group("num")))
    return None


def is_non_first_rar_part(path):
    """是否为非首卷的 RAR 分卷（.part2.rar / .part2.除rar 等）。

    只有首卷是解压入口，非首卷单独交给 7-Zip 必然报 Missing volume。"""
    info = _part_info(Path(path).name)
    return bool(info) and info[1] > 1


def is_first_volume(name):
    """是否为分卷的第一卷（.part1.rar / .001 / .z01 / .r00）。

    首卷是解压入口；多分卷首卷出现时后续分卷可能尚未下载/创建，
    监听层需要据此进入观察期，避免后续分卷没到齐就提前解压。"""
    low = name.lower()
    info = _part_info(name)
    if info:
        return info[1] == 1
    m = re.search(r"\.([zr]?\d{2,3})$", low)
    if m:
        tail = m.group(1)  # 如 '001' / 'z01' / 'r00' / '002'
        return tail.startswith(("001", "z01", "r00"))
    return False


def _volume_number(name):
    """提取分卷编号（从 1 起）：partN.rar → N；xxx.001 → 1；xxx.z01 → 1；
    xxx.r00 → 1（r00 系列把 r00 视为第 1 卷）。

    用于判断分卷编号是否连续（缺中间卷时能检测出来）。无法识别返回 None。"""
    low = name.lower()
    info = _part_info(name)
    if info:
        return info[1]
    m = re.search(r"\.([zr]?)(\d{2,3})$", low)
    if m:
        prefix, digits = m.group(1), int(m.group(2))
        if prefix == "r":
            return digits + 1   # r00=第1卷, r01=第2卷 ...
        return digits           # z01=1, z02=2..., 001=1, 002=2...
    return None


def _volume_final_name(name):
    """分卷系列的末卷名（不带编号的最终部分），用于判断分卷是否到齐。

    - xxx.zip.001 / xxx.7z.001 风格 → 末卷 = 去掉 .NNN（如 xxx.zip）
    - xxx.z01 风格 → 末卷 = 基础名.zip
    - xxx.partN.rar / xxx.rNN 风格 → 全部带编号，无独立末卷（返回 None）
    无法判断返回 None。"""
    low = name.lower()
    m = re.search(r"\.\d{3}$", low)
    if m:
        base = name[:m.start()]
        if base.lower().endswith((".zip", ".rar", ".7z")):
            return base
        return None
    m = re.search(r"\.z\d{2}$", low)
    if m:
        return name[:m.start()] + ".zip"
    return None


def has_zip_eocd(path):
    """文件末尾是否含 zip 的 EOCD 标记（PK\\x05\\x06 / ZIP64 的 PK\\x06\\x06）。

    分卷 zip 的中央目录（含 EOCD）一定在最后一个分卷的末尾。因此某个
    分卷末尾含 EOCD ⇔ 它就是末卷 ⇔ 分卷已到齐。这同时兼容两种命名习惯：
    - 7-Zip 风格：末卷叫 xxx.zip（不带编号）
    - 上传者风格：末卷也带编号（如 .002），EOCD 在 .002 末尾
    失败/非 zip 返回 False。"""
    try:
        size = Path(path).stat().st_size
        if size <= 0:
            return False
        with open(path, "rb") as f:
            f.seek(max(0, size - 65557))
            tail = f.read()
        return b"PK\x05\x06" in tail or b"PK\x06\x06" in tail
    except Exception:
        return False


def is_non_first_volume(name):
    """是否为非首卷分卷（part2.rar / .002 / .z11 / .r01 等）。

    非首卷不是解压入口，单独交给 7-Zip 必然报 Missing volume/Unexpected end
    of archive，应跳过等待首卷出现后统一处理整个分卷。"""
    num = _volume_number(name)
    return num is not None and num > 1


def is_fake_volume_name(path):
    """文件名像分卷、但内容实际是完整自包含压缩包（打包方改后缀迷惑）。

    例如"二重解压改后缀.z11"其实是一个完整 zip（头部 PK 头 + 尾部 EOCD），
    7-Zip 因 .z11 后缀误判为 split 分卷而报 Missing volume。判断规则：
    - 文件名像分卷（is_volume）且没有主卷兄弟（volume_master 为空）
    - 卷号 > 1（真实首卷 .z01/.001/.part1 可能是等后续卷，保留分卷逻辑）
    - 内容头部确实是归档魔数（自包含完整压缩包，非伪装）
    命中则应按完整压缩包处理，不能走分卷逻辑。"""
    try:
        info = analyze_file(path)
    except Exception:
        return False
    if not (info.get("is_volume") and not info.get("volume_master")):
        return False
    num = _volume_number(Path(path).name)
    if num is None or num <= 1:
        return False
    fmt = info.get("detected_format")
    if not fmt or info.get("is_polyglot"):
        return False
    return _header_matches_format(path, fmt)


def _volume_base(name):
    """剥离分卷编号，得到分卷基础名。如 2056.7z.002 -> 2056.7z、a.r00 -> a。"""
    m = re.search(r"\.([zr]?\d{2,3})$", name, re.I)
    return name[:m.start()] if m else None


def _series_base(name):
    """zip/rar 系列分卷的基础名：base.z01...base.zip 或 base.rar+base.rNN。

    返回 (系列, 基础名) 或 None。最后一个 zip 分卷就叫 base.zip，
    第一个 rar 分卷就叫 base.rar，都不带编号。"""
    low = name.lower()
    if low.endswith(".zip"):
        return "z", name[:-4]
    if low.endswith(".rar"):
        return "r", name[:-4]
    m = re.match(r"^(.*)\.(z\d{2}|r\d{2})$", name, re.I)
    if m:
        return m.group(2)[0].lower(), m.group(1)
    return None


def is_volume_file(source_name, candidate, stem):
    """candidate 是否为 source_name 的分卷兄弟（如 xx.7z.002 之于 xx.7z.001）。

    严格按「去掉末尾编号后基础名一致」判断，避免 xx.7z.002 被误认成
    xx.mp4（stem 都是 "xx"）的分卷而误删。
    """
    if candidate == source_name:
        return False
    m_src = PART_RE.match(source_name)
    m_can = PART_RE.match(candidate)
    if m_src and m_can:
        return m_src.group("name") == m_can.group("name")
    # 通用 partN 系列（含 .part1.除rar 这类非标准后缀，如
    # VW815.part1.除rar ↔ VW815.part2.rar）
    sp = _part_info(source_name)
    if sp:
        cp = _part_info(candidate)
        if cp and cp[0] == sp[0] and cp[1] != sp[1]:
            return True
    # 常规分卷：剥掉末尾编号后基础名一致（2056.7z.001 vs 2056.7z.002）
    src_base = _volume_base(source_name)
    can_base = _volume_base(candidate)
    if src_base and src_base == can_base:
        return True
    # zip/rar 系列分卷：base.z01...base.zip、base.rar + base.rNN
    s_ser = _series_base(source_name)
    if s_ser:
        c_ser = _series_base(candidate)
        if c_ser and c_ser[0] == s_ser[0] and c_ser[1] == s_ser[1]:
            return True
    # 末卷：不带编号的最终部分（xxx.zip 之于 xxx.zip.001/.002，
    # xxx.7z 之于 xxx.7z.001；base.zip 之于 base.z01）
    final_part = _volume_final_name(source_name)
    if final_part and candidate == final_part:
        return True
    return False


def _recycle_paths(paths, permanent_fallback=True, quarantine_root=None, quarantine_out=None):
    """把存在的路径移入回收站。

    permanent_fallback=True（默认，成功路径沿用）：回收站不可用时回退永久删除。
    permanent_fallback=False（解压失败路径专用）：回收站不可用时**保留原样**，
    绝不永久删除——满足「涉及解压失败的都不能走永久删除」。

    quarantine_root 非 None 时进入隔离模式：回收站不可用时把失败路径移入隔离区
    （trail.move_to_quarantine），移入项追加到 quarantine_out（调用方传入的 list），
    仍未能移走的路径作为 failed 返回且原样留在原位；**绝不永久删除**。空字符串表示
    按每个文件自身所在目录补 _已删除（无 output_dir 的兜底）。

    返回 (recycled, failed)：recycled=已移入回收站的路径；failed=回收站不可用时
    已永久删除的路径（True）或未能回收、保留在原地的路径（False）。不存在的忽略。"""
    return deletion_engine._recycle_paths(
        paths, permanent_fallback=permanent_fallback,
        quarantine_root=quarantine_root, quarantine_out=quarantine_out)


def delete_source(source, hook=None, permanent_fallback=True, quarantine_root=None):
    """删除源文件（含分卷）。

    优先移入回收站（可撤销）；回收站不可用时按 permanent_fallback 决策：
    True（默认，auto / permanent 策略）退化为永久删除；False（keep 策略）**保留
    原文件**，绝不永久删除；quarantine_root 非 None（quarantine 策略）时移入隔离区，
    同样绝不永久删除。
    hook(recycled, failed, quarantine_map) 在删除后回调：recycled=已移入回收站，
    failed=回收站不可用时的残留（是否已永久删除取决于 permanent_fallback），
    quarantine_map=已移入隔离区、可还原的文件地图（无则 None）。
    """
    source = Path(source)
    targets = []
    if source.exists():
        targets.append(source)
    parent = source.parent
    if parent.exists():
        for entry in parent.iterdir():
            if entry.is_file() and is_volume_file(source.name, entry.name, source.stem):
                targets.append(entry)
    if not targets:
        if hook:
            hook([], [], None)
        print("没有需要删除的源文件")
        return

    qmap = []
    recycled, failed = _recycle_paths(
        targets, permanent_fallback=permanent_fallback,
        quarantine_root=quarantine_root, quarantine_out=qmap)
    if hook:
        hook(recycled, failed, qmap or None)
    print(f"已删除源文件及分卷，共 {len(recycled) + len(failed)} 个（移入回收站）")


def _dirs_conflict(src_dir, dest):
    """合并前检查两目录是否有冲突：相同相对路径的文件对（哪怕一个），
    或文件/目录同名混排。目录对目录不算冲突（可递归合并）。"""
    def _entries(d):
        out = {}
        for p in Path(d).rglob("*"):
            out[p.relative_to(d).as_posix().lower()] = p.is_dir()
        return out
    s, d_ = _entries(src_dir), _entries(dest)
    for rel, is_dir in s.items():
        if rel in d_:
            if is_dir and d_[rel]:
                continue  # 目录对目录，可递归合并
            return True    # 文件冲突 / 类型冲突
    return False


def _merge_dir(src, dst):
    """把 src 目录的内容并入 dst（目录递归合并，文件移动），src 会被清空。"""
    for entry in src.iterdir():
        target = dst / entry.name
        if entry.is_dir():
            target.mkdir(parents=True, exist_ok=True)
            _merge_dir(entry, target)
        else:
            shutil.move(str(entry), str(target))


def promote_extracted_content(output_dir, promote_to, source, hook=None, merge=False,
                              delete_src=False, permanent_fallback=True,
                              quarantine_root=None):
    """解压后处理：输出目录顶层只有 1 个文件夹时，把该文件夹提升到指定地区。

    delete_src=True 时，随后把源文件（含分卷）与输出目录移入回收站并回调 hook 标记
    删除回溯；回收站不可用时按 permanent_fallback 决策（True=永久删除，
    False=保留源文件，见 delete_source）；quarantine_root 非 None（quarantine 策略）
    时移入隔离区，绝不永久删除。delete_src=False（默认，数据安全优先）时
    只做提升，绝不回收源文件/分卷，仅原地清理提升后空掉的输出目录。
    条件不满足（0 个或多于 1 个顶层文件夹）时：delete_src=True 退化为仅删除源文件，
    delete_src=False 则原样保留源文件。
    merge=True 且目标同名文件夹无文件冲突时，直接并入（不建 (N) 文件夹）；
    有同名文件冲突时仍按原逻辑重命名为 name(N)。
    """
    output_dir = Path(output_dir)
    promote_to = Path(promote_to)
    try:
        entries = list(output_dir.iterdir())
    except OSError as e:
        return {"promoted": None, "recycled": [], "hook_called": False,
                "note": f"提升失败: {e}"}
    top_dirs = [p for p in entries if p.is_dir()]
    top_files = [p for p in entries if p.is_file()]

    # 只有顶层「恰好 1 个文件夹、且没有顶层文件」时才适合提升该文件夹；
    # 否则（顶层文件 + 空文件夹并存，如压缩包里有个空目录）提升会搬错内容，
    # 把真实文件随输出目录一起回收。
    if len(top_dirs) != 1 or top_files:
        if not delete_src:
            return {"promoted": None, "recycled": [], "hook_called": False,
                    "note": "顶层文件夹数≠1 或存在顶层文件，未提升，保留源文件"}
        delete_source(source, hook, permanent_fallback=permanent_fallback,
                      quarantine_root=quarantine_root)
        return {"promoted": None, "recycled": [], "hook_called": True,
                "note": "顶层文件夹数≠1 或存在顶层文件，未提升，仅删除源文件"}

    src_dir = top_dirs[0]
    try:
        # 提升目标若与输出目录是同一位置（如 xx.mp4 内层文件夹也叫 xx，
        # output_dir 与 dest 指向同一目录），说明内容已到位，
        # 视为 same_place：不移动、不回收 output_dir（否则连内容一起回收）。
        same_place = (promote_to.resolve() == output_dir.resolve()
                      or (promote_to / src_dir.name).resolve() == output_dir.resolve())
    except OSError:
        same_place = False

    promoted = None
    pre_recycled, pre_failed = [], []
    qmap = []            # 本函数累计的隔离区地图（pre + 主回收两段）
    occupies_source = False
    if not same_place:
        promote_to.mkdir(parents=True, exist_ok=True)
        dest = promote_to / src_dir.name
        # 目标名被「本次即将回收的源文件/其分卷」占用：典型为无扩展名压缩包与其
        # 内层同名文件夹撞名（源文件 E:\test\2022 与内层文件夹 2022）。此时既不能
        # 合并进去、也不能直接 move 覆盖；先回收占位文件让出位置，提升后才能用回
        # 原名（否则会退化成 2022(1)）。
        if dest.is_file() and source.exists():
            try:
                occupies_source = os.path.samefile(dest, source)
            except OSError:
                occupies_source = False
            if not occupies_source and is_volume_name(source.name):
                occupies_source = is_volume_file(source.name, dest.name, source.stem)
        if occupies_source and delete_src:
            pre_recycled, pre_failed = _recycle_paths(
                [dest], permanent_fallback=permanent_fallback,
                quarantine_root=quarantine_root, quarantine_out=qmap)

        if dest.is_dir() and merge and not _dirs_conflict(src_dir, dest):
            # 目标同名文件夹存在但无文件冲突：直接并入，不建 (N)
            _merge_dir(src_dir, dest)
            try:
                src_dir.rmdir()
            except OSError:
                pass
            promoted = str(dest)
        else:
            if dest.exists():
                # 有同名文件冲突（哪怕一个）或未开启合并：重命名为 name(N)
                i = 1
                while (promote_to / f"{src_dir.name}({i})").exists():
                    i += 1
                dest = promote_to / f"{src_dir.name}({i})"
            try:
                shutil.move(str(src_dir), str(dest))
                promoted = str(dest)
            except OSError as e:
                if hook:
                    hook(pre_recycled, pre_failed, qmap or None)
                return {"promoted": None, "recycled": pre_recycled,
                        "hook_called": bool(pre_recycled or pre_failed),
                        "note": f"提升失败: {e}"}
    else:
        promoted = str(src_dir)

    # delete_source=False：只完成内容提升，绝不回收源文件与分卷；提升后已空掉的
    # 输出目录用原地清理（空目录）而非回收，确保源数据一定留在原位。
    if not delete_src:
        if not same_place:
            remove_empty_dirs(output_dir)
        return {"promoted": promoted, "recycled": list(pre_recycled),
                "hook_called": False,
                "note": f"已提升 {Path(promoted).name}，保留源文件与分卷"}

    # 源文件路径已被提升后的文件夹复用（occupies_source）时，该路径此刻是成品
    # 目录，不能再当源文件回收（否则会把刚提升出来的内容一起删掉）。
    reused = {str(source)} if occupies_source else set()
    targets = []
    if source.exists() and str(source) not in reused:
        targets.append(str(source))
    # 分卷源文件（如 xxx.7z.001）连同其他分卷一起回收，否则只删主卷和
    # 输出目录，分卷兄弟（xxx.7z.002...）会残留（promote 成功时才走到这里）。
    src_parent = source.parent
    if src_parent.exists():
        for entry in src_parent.iterdir():
            if (entry.is_file()
                    and is_volume_file(source.name, entry.name, source.stem)
                    and str(entry) not in targets
                    and str(entry) not in reused):
                targets.append(str(entry))
    if same_place:
        targets.extend(str(f) for f in top_files)
    elif output_dir.exists():
        targets.append(str(output_dir))

    recycled2, failed2 = _recycle_paths(
        targets, permanent_fallback=permanent_fallback,
        quarantine_root=quarantine_root, quarantine_out=qmap)
    recycled = pre_recycled + recycled2
    failed = pre_failed + failed2

    if hook:
        hook(recycled, failed, qmap or None)

    note = f"已提升 {Path(promoted).name}，回收源文件与中间文件共 {len(recycled) + len(failed)} 个"
    return {"promoted": promoted, "recycled": recycled, "hook_called": True, "note": note}


def run_script(script_path, args, source, output_dir, result):
    script = Path(script_path)
    if not script.exists():
        raise FileNotFoundError(f"脚本不存在: {script_path}")
    env = dict(os.environ)
    env["EXTRACT_SOURCE_PATH"] = str(source)
    env["EXTRACT_OUTPUT_DIR"] = str(output_dir)
    env["EXTRACT_SUCCESS"] = "true" if result["success"] else "false"
    env["EXTRACT_DEPTH"] = str(result["depth_reached"])
    cmd = [str(script)] + (args or [])
    r = subprocess.run(cmd, capture_output=True, text=True, env=env,
                       creationflags=CREATE_NO_WINDOW,
                       encoding="utf-8", errors="replace")
    if r.returncode != 0:
        raise RuntimeError(f"脚本返回错误: {(r.stderr or '').strip()}")
    result["logs"].append("脚本执行成功")


def extract_one(engine, source, out_arg, user_passwords, options, args,
                progress_cb=None, pauser=None):
    source = Path(source)
    if not source.exists():
        print(f"文件不存在: {source}")
        return None

    if pauser is not None:
        pauser.wait_if_paused()
    task_id = uuid.uuid4().hex[:8]
    print(f"=== 处理: {source.name} ===")
    if is_do_not_extract(source.name):
        print(f"移动安装包/交付物，保持原样不自动解压（需手动处理）: {source.name}")
        return None
    if is_non_first_rar_part(source):
        print(f"非首卷 RAR 分卷，跳过（等待 .part1 处理整个分卷）: {source.name}")
        return None
    info = analyze_file(source)
    if info.get("is_incomplete"):
        print(f"下载未完成，暂不解压（等待后缀消失）: {source.name}")
        return None
    if volume_download_pending(source):
        print(f"分卷未到齐（还有分卷在下载），暂不解压，等待全部下载完成: {source.name}")
        return None
    print(f"真实格式: {info['detected_format'] or '未知'}  伪装: {'是' if info['is_disguised'] else '否'}"
          f"  分卷: {'是' if info['is_volume'] else '否'}")
    if info["sanitized_name"]:
        source = perform_sanitization(source)
        print(f"已清理文件名中的「删」字: {source.name}")

    extract_src = source
    staged = None
    if info.get("detected_format") == "rar":
        # 首卷后缀非标准（如 .part1.除rar）时，7-Zip 找不到实际存在的 .rar 兄弟卷。
        # 规范化命名（硬链接到临时子目录改名为 .partN.rar）后再解压。
        staged_info = _stage_rar_volumes(extract_src)
        if staged_info:
            extract_src, staged = staged_info
            print(f"分卷命名不规范（含 .删除rar/.除rar 等），已规范化命名后交给引擎: {extract_src.name}")
    elif is_fake_volume_name(extract_src):
        # 文件名像分卷（.z11/.111 等）但内容是完整自包含压缩包（改后缀迷惑）：
        # 复制到临时目录改名为标准后缀，7-Zip 才不会误判为 split 缺卷。
        staged_info = _stage_fake_volume(extract_src, info["detected_format"])
        if staged_info:
            extract_src, staged = staged_info
            print(f"文件名像分卷但内容是完整压缩包（改后缀迷惑），已规范后缀后交给引擎: {extract_src.name}")
    if info.get("is_polyglot"):
        print("多段伪装文件，直接按内嵌压缩包格式处理（引擎按内容识别）")
    elif info["is_disguised"]:
        print(f"伪装文件（真实格式 {info['detected_format']}），直接按内容交给引擎处理（无需改名复制）")

    out_dir = Path(out_arg) if out_arg else default_output_dir(source)
    out_dir.mkdir(parents=True, exist_ok=True)
    # 记录解压前输出目录是否为空：失败回退时只清理"本次产生"的半成品，
    # 不误删用户预先放进自定义输出目录的内容
    try:
        pre_entries = set(out_dir.iterdir())
    except OSError:
        pre_entries = set()
    was_empty = not pre_entries
    print(f"输出目录: {out_dir}")

    svc = ExtractService(engine, options)
    result = None
    try:
        result = svc.extract({
            "id": task_id,
            "source_path": extract_src,
            "output_dir": out_dir,
            "passwords": user_passwords,
            "progress_cb": progress_cb,
            "pauser": pauser,
        })
    finally:
        if staged:
            # 规范化命名的临时 staging 目录也是中间产物：成功维持原样删除，
            # 失败移入回收站（里面只是源文件的硬链接/副本，源文件不受影响）。
            if result is not None and not is_clean_success(result):
                _recycle_paths([staged], permanent_fallback=False)
            else:
                shutil.rmtree(staged, ignore_errors=True)

    # 命中统计与「是否用字典解压」解耦：成功用到的口令一律计入密码字典，
    # 这样 GUI（use_dict=False，字典口令从不被尝试）也能按真实命中次数排序。
    # 写入只影响 password_dict 的 used_count/last_used_at；字典内容仍只在
    # options["use_dict"] 打开时才作为候选（见 get_password_for_layer 调用点），
    # 因此关字典的运行行为完全不变。
    # 关键：只有归档「真的有加密内容」、且该口令确实把它解开了（engine 结果里的
    # encrypted=True、used_password 非空），才记一次命中。未加密归档即使把候选
    # 密码喂进引擎也会成功，绝不能把第一个候选误记为命中（否则 62 条密码本的
    # 每次都让每个未加密包虚增一次）。每次成功只记一次；空/空白口令不记。
    if result["success"] and result.get("encrypted") and result["used_password"]:
        add_dict_password(result["used_password"])

    # 只有「干净的整体成功」才允许后处理（提升内容 / 删除源文件）。
    # 任何层失败都不是成功：源文件与分卷原样保留。
    if is_clean_success(result):
        apply_post_actions(result, source, out_dir, build_post_actions(args))

    if is_clean_success(result):
        print(f"=== 完成，穿透 {result['depth_reached']} 层，共 {len(result['extracted_files'])} 个文件 ===")
        _stamp_output_times(result, out_dir, pre_entries, args)
    else:
        print(f"=== 解压未完成（已保留源文件）: {result['error']} ===")
        # 回退：解压失败时中间文件移入回收站（可恢复），绝不永久删除。
        # 源文件(含分卷)未删除，之后可重试。仅当输出目录"解压前为空"
        # (即本次新建)才整体回收，避免误删用户预先放入自定义输出目录的内容。
        try:
            if out_dir.exists() and was_empty and not result.get("keep_output_dir"):
                recycled, _failed = _recycle_paths([out_dir], permanent_fallback=False)
                if recycled:
                    print(f"已回退，中间文件已移入回收站: {out_dir}")
                else:
                    print(f"已回退（中间文件移入回收站失败，保留原样）: {out_dir}")
        except Exception:
            pass
    return result


def print_analysis(path):
    info = analyze_file(path)
    print(f"文件: {info['original_name']}")
    print(f"真实格式: {info['detected_format'] or '未知'}")
    print(f"伪装: {'是' if info['is_disguised'] else '否'}")
    if info.get("is_polyglot"):
        print("多段伪装: 是（压缩包内嵌在文件末尾）")
    print(f"文件名清理: {info['sanitized_name'] or '无需清理'}")
    print(f"文件名中的密码: {info['extracted_password'] or '无'}")
    print(f"分卷: {'是' if info['is_volume'] else '否'}"
          + (f" (主卷: {info['volume_master']})" if info['volume_master'] else ""))
    if info["stego_content"]:
        print(f"隐写内容中的压缩文件: {', '.join(info['stego_content'])}")
    else:
        print("隐写内容: 无")


def main():
    try:
        from . import db
        db.init_db()
        db.migrate_legacy({}, db.LEGACY_DICT_FILE)
    except Exception:
        pass

    parser = argparse.ArgumentParser(
        prog="smart_extract",
        description="智能多重解压工具：嵌套穿透、自动识别格式、密码自动尝试、伪装/分卷处理",
    )
    parser.add_argument("archives", nargs="*", help="要解压的压缩包路径")
    parser.add_argument("-o", "--output", help="输出目录（默认：压缩包同名文件夹）")
    parser.add_argument("-p", "--password", action="append", default=[],
                        help="密码（可多次，或用逗号分隔；第N层优先用第N个）")
    parser.add_argument("--default-password", help="兜底默认密码")
    parser.add_argument("--no-nested", action="store_true", help="禁用嵌套穿透")
    parser.add_argument("--max-depth", type=int, default=10, help="最大穿透层数（默认10）")
    parser.add_argument("--max-size-ratio", type=float, default=100.0,
                        help="解压大小膨胀比例上限，防 zip bomb（默认100倍）")
    parser.add_argument("--use-password-dict", action="store_true",
                        help="自动使用/保存密码字典")
    parser.add_argument("--engine", choices=["auto", "7z", "zip"], default="auto",
                        help="解压引擎（默认 auto：有7-Zip用7-Zip，否则Python zipfile）")
    parser.add_argument("--7z-path", help="7z.exe 自定义路径")
    parser.add_argument("--mode", choices=["temp", "direct"], default="direct",
                        help="direct=直接解压到输出目录（最快，失败会留半成品）；temp=临时目录模式（出错可回滚，临时目录与输出同盘）")
    parser.add_argument("--move-to", help="解压成功后移动到指定文件夹")
    parser.add_argument("--promote-to", help="解压成功后：若输出目录顶层只有 1 个文件夹则提升到该地区，"
                        "并回收源文件与中间文件")
    parser.add_argument("--delete-source", action="store_true", help="解压成功后删除源文件（含分卷）")
    parser.add_argument("--run-script", help="解压成功后执行脚本")
    parser.add_argument("--script-args", nargs="*", default=[],
                        help="传给脚本的参数")
    parser.add_argument("--analyze", action="store_true", help="只分析文件，不解压")
    parser.add_argument("--list-dict", action="store_true", help="列出密码字典")
    parser.add_argument("--dict-add", help="向密码字典添加一个密码")
    args = parser.parse_args()

    if args.list_dict:
        data = load_password_dict()
        if not data:
            print("字典为空")
            return
        for e in sorted(data.values(), key=lambda x: -x.get("used_count", 0)):
            print(f"{e['password']}  使用 {e.get('used_count', 0)} 次  "
                  f"最近 {time.strftime('%Y-%m-%d %H:%M', time.localtime(e.get('last_used_at', 0)))}")
        return

    if args.dict_add:
        add_dict_password(args.dict_add)
        print(f"已添加: {args.dict_add}")
        return

    if args.analyze:
        for a in args.archives:
            print_analysis(a)
        return

    if not args.archives:
        parser.error("需要提供至少一个压缩包路径")

    engine = create_engine(args.engine, getattr(args, "7z_path"))
    print(f"解压引擎: {engine.name}")
    options = {
        "enable_nested": not args.no_nested,
        "max_depth": args.max_depth,
        "max_size_ratio": args.max_size_ratio,
        "use_dict": args.use_password_dict,
        "default_password": args.default_password,
        "mode": args.mode,
    }
    passwords = parse_passwords(args.password)
    for a in args.archives:
        try:
            extract_one(engine, a, args.output, passwords, options, args)
        except KeyboardInterrupt:
            print("\n已中断")
            sys.exit(130)


if __name__ == "__main__":
    main()
