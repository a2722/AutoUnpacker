# -*- coding: utf-8 -*-
"""格式/分卷/伪装探测与文件名分析（阶段6c 自 extract.py 纯搬移，函数体未做任何拆分）。

职责：魔数/尾部/前部/SFX 扫描识别真实格式与多段伪装、分卷识别与本套兄弟卷判定、
未完成下载判定、文件名密码提取、7z 清单解析与隐写探测、7-Zip 可执行文件查找。
依赖：engines.run_silent（仅 detect_steganography）；..sevenzip.ISOLATED_BIN
      （find_sevenzip_path 体内延迟导入）。
"""
import re
import shutil
import zipfile
from pathlib import Path

from .engines import run_silent


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

# 「按扩展名打发」的兜底：正常的图片/文档/文本不可能有这么大，而且这些文件是
# 程序运行期间被下载进监听目录的 —— 体积本身即强嫌疑。达到此体积的非压缩包
# 后缀文件也要过一次深层格式探测（尾部 EOCD / 前部扫描 / SFX，均有上限，廉价）。
SUSPECT_BIG_FILE_BYTES = 16 * 1024 * 1024


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


def _head_is_mz(path):
    """文件头两字节是否 MZ（PE/SFX 可执行包）。读取失败一律返回 False，绝不抛异常。"""
    try:
        with open(path, "rb") as f:
            return f.read(2) == b"MZ"
    except OSError:
        return False


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


def _verify_polyglot_7z_rar(path, fmt):
    """多段伪装扫描命中 7z/rar 后，用真实 7-Zip 复核签名不是巧合。

    _scan_tail_for_archive / _scan_sfx_for_archive / 全量扫描只做几字节签名
    匹配：安装包等二进制里恰好内嵌 7z\\xbc\\xaf\\x27\\x1c / Rar!\\x1a\\x07 时
    会被误判为「头部伪装 + 内嵌压缩包」，watcher 随后可能真的解压并把用户
    文件当结果隔离。zip 已由 _confirm_polyglot 用 zipfile 实检；这里补上
    7z/rar 的缺口：调 7-Zip 做一次廉价列表（`l -t#`，与 detect_steganography
    同款 parser 模式调用），退出码为 0 才接受，否则视为签名巧合。
    找不到 7-Zip（find_sevenzip_path 返回 None 或抛异常）时 fail-open，
    维持旧行为；仅在已扫到 7z/rar 签名时才会走到这里，且 run_silent 自带
    超时，绝不抛异常。"""
    if fmt not in ("7z", "rar"):
        return fmt
    try:
        sevenzip = find_sevenzip_path()
    except Exception:
        return fmt
    if not sevenzip:
        return fmt
    try:
        r = run_silent([str(sevenzip), "l", "-t#", str(path)])
    except Exception:
        return fmt
    return fmt if r.returncode == 0 else None


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


def head_archive_format(path):
    """只读头部魔数识别压缩包格式（8 字节读取 + tar 的 257 偏移 ustar 检查）。

    返回 "zip"/"rar"/"7z"/"gz"/"bz2"/"xz"/"tar"，识别不出或读取失败返回 None，
    绝不抛异常。廉价：普通文件只多读 8 字节。detect_archive_format 与
    is_archive_file 的改名/伪装识别共用这一份判定（旧内联块的 xz 判定写成
    header[:4] 与 5 字节字面量比较、永远不成立，这里一并修正为 header[:5]）。
    """
    try:
        with open(path, "rb") as f:
            header = f.read(8)
        if header[:4] == b"PK\x03\x04":
            return "zip"
        if header[:4] == b"Rar!":
            return "rar"
        if header[:4] == b"7z\xbc\xaf":
            return "7z"
        if header[:2] == b"\x1f\x8b":
            return "gz"
        if header[:3] == b"BZh":
            return "bz2"
        if header[:5] == b"\xfd7zXZ":  # 旧内联块写 header[:4] 永远匹配不上（笔误）
            return "xz"
        with open(path, "rb") as f:
            f.seek(257)
            if f.read(5) == b"ustar":
                return "tar"
    except OSError:
        pass
    return None


def detect_archive_format(path):
    """返回 (格式, 是否多段伪装)。

    多段伪装 = 文件头部是真实内容（如视频），真实压缩包被内嵌在文件末尾，
    直接把扩展名改成压缩包后缀即可解压。
    """
    head_fmt = head_archive_format(path)
    if head_fmt:
        return head_fmt, False
    ext_fmt = format_from_extension(path)
    if ext_fmt:
        # 扩展名是压缩包后缀但头部对不上（如 MP4 伪装顶着 .zip 后缀）：
        # 7-Zip 打不开，必须先按多段伪装剥离，不能直接当普通 zip 处理。
        if _header_matches_format(path, ext_fmt):
            return ext_fmt, False
    tail = _confirm_polyglot(path, _scan_tail_for_archive(path))
    if tail:
        # 7z/rar 仅为签名命中，再经真实 7-Zip 复核（zip 已在 _confirm_polyglot 实检）
        return _verify_polyglot_7z_rar(path, tail), True
    # SFX 自解压包：MZ 头 + 内嵌 7z/zip/rar（签名在文件中部，不在尾部）
    sfx = _scan_sfx_for_archive(path)
    if sfx:
        return _verify_polyglot_7z_rar(path, _confirm_polyglot(path, sfx)), True
    # 成本护栏：只对「伪装载体后缀」（含新增的文本族）或 MZ/SFX 头部做全量扫描。
    # 其余后缀（.iso/.bin/.dat/无后缀等）在 16–64MB 区间会被 _full_scan_for_archive
    # 整文件读一遍（最坏时同一轮 _handle 触发两次），却不增加识别力：尾部 EOCD
    # （后 64KB）与前部扫描已覆盖「尾部追加/前部内嵌」两类真实伪装。故此处只保留
    # 「前 32MB 扫描」（与原 >64MB 分支所用完全相同的廉价扫描），跳过中段全量读；
    # 载体后缀（图片/视频/文本等）与 SFX（MZ）头的既有全量扫描一字不改。
    ext = Path(path).suffix.lower().lstrip(".")
    if ext in DISGUISE_CARRIER_EXTS or _head_is_mz(path):
        full = _confirm_polyglot(path, _full_scan_for_archive(path))
    else:
        full = _confirm_polyglot(path, _scan_front_for_archive(path))
    if full:
        return _verify_polyglot_7z_rar(path, full), True
    if ext_fmt:
        return ext_fmt, False
    return None, False


def detect_format_by_magic(path):
    fmt, _ = detect_archive_format(path)
    return fmt


def format_from_extension(path):
    return EXT_FORMATS.get(path.suffix.lower().lstrip("."))


def infer_volume_name_from_siblings(path):
    """用同目录里「干净的标准分卷兄弟」反推本文件的正确分卷名；取不到返回 None。

    应对上传者用**任意字符**往分卷名里插垃圾（无法穷举）的情况：本文件名末尾的
    3 位卷号是真信号，而同目录里其它标准分卷（如 `xxx.7z.002`）给出了正确的
    系列基名 —— 两者拼起来即为正确名。例：
        `Huge is good.删除7z.删除001` + 兄弟 `Huge is good.7z.002`
        -> `Huge is good.7z.001`
    仅当「本文件不是标准分卷名、且同目录存在标准分卷兄弟」时才推断，绝不误伤
    普通文件名。绝不抛异常。
    """
    try:
        p = Path(path)
        if _is_volume_suffix(p.name):
            return None
        m = re.search(r"(\d{3})$", p.name)
        if not m:
            return None
        num = m.group(1)
        parent = p.parent
        if not parent.is_dir():
            return None
        from collections import Counter
        bases = Counter()
        try:
            for e in parent.iterdir():
                if e.is_file() and _is_volume_suffix(e.name):
                    b = _volume_base(e.name)
                    if b:
                        bases[b] += 1
        except OSError:
            return None
        if not bases:
            return None
        base = bases.most_common(1)[0][0]
        cand = f"{base}.{num}"
        return cand if (cand != p.name and _is_volume_suffix(cand)) else None
    except Exception:
        return None


def _is_volume_suffix(name):
    """底层：按后缀字面判断是否分卷名（不做脏字符归一；供 normalize 复用，避免递归）。"""
    try:
        low = str(name or "").lower()
        ext = Path(low).suffix
        if len(ext) == 4 and ext[1:].isdigit():
            return True
        if len(ext) == 4 and ext[1].lower() == "z" and ext[2:].isdigit():
            return True
        if ".part" in low and low.endswith(".rar"):
            return True
        return False
    except Exception:
        return False


def normalize_volume_name(name):
    """把「被插了防和谐标记」的分卷名归一为标准形式。

    上传方常在文件名里插「删除」二字（或单字「删」/「除」），约定用户自行删掉，
    以规避直链检测。现场：`Huge is good.删除7z.删除001` —— 程序原有净化只去单字
    `删`，留下 `除`，名仍非标准分卷（`.001` 恰 4 字符）→ `is_volume_name` 判否
    → 不认为分卷 → 单文件交 7-Zip → `Cannot open`。

    依次尝试去掉 `删除` / `删` / `除`，以及「`NNN` 前的 1~4 个纯非 ASCII 字符」；
    任一候选成为**合法分卷名**即采用，绝不误伤普通文件名（非分卷名一律原样返回）。
    绝不抛异常。
    """
    try:
        s = str(name or "")
        if not s or _is_volume_suffix(s):
            return s
        cands = []
        if "删除" in s:
            cands.append(s.replace("删除", ""))
        if "删" in s:
            cands.append(s.replace("删", ""))
        if "除" in s:
            cands.append(s.replace("除", ""))
        m = re.match(r"^(.*)\.([^\x00-\x7f]{1,4})(\d{3})$", s)
        if m:
            cands.append(f"{m.group(1)}.{m.group(3)}")
        for c in cands:
            if c and _is_volume_suffix(c):
                return c
        return s
    except Exception:
        return str(name or "")


def sanitize_filename(name):
    return normalize_volume_name(name.replace("\u5220", ""))
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
    ".td", ".opdownload", ".uc!", ".aria2", ".!ut", ".partial",
)

# 运行期覆盖表：None = 用内置默认（由 set_incomplete_suffixes 写入）。
# 本模块被 CLI 直接使用（没有 config 对象），因此覆盖走模块级 setter，
# 绝不导入 config；未设置覆盖时行为与过去完全一致。
_incomplete_suffix_override = None


def set_incomplete_suffixes(suffixes):
    """覆盖未完成下载后缀表（None = 恢复内置默认）。

    带配置的运行实例在启动 / 设置变更时调用；CLI 不调用则始终用内置默认。
    逐项规整为「小写 + 前导点」并去重（保留顺序）；非可迭代输入（含字符串）
    一律视为无效，回退内置默认。"""
    global _incomplete_suffix_override
    if suffixes is None or isinstance(suffixes, (str, bytes)):
        _incomplete_suffix_override = None
        return
    try:
        cleaned = []
        for item in suffixes:
            s = str(item or "").strip().lower()
            if not s:
                continue
            if not s.startswith("."):
                s = "." + s
            if s not in cleaned:
                cleaned.append(s)
    except TypeError:
        _incomplete_suffix_override = None
        return
    _incomplete_suffix_override = tuple(cleaned)


def _incomplete_suffixes():
    """当前生效的未完成下载后缀表（默认 = 内置 INCOMPLETE_DOWNLOAD_SUFFIXES）。"""
    if _incomplete_suffix_override is not None:
        return _incomplete_suffix_override
    return INCOMPLETE_DOWNLOAD_SUFFIXES


def is_incomplete_download(path):
    """是否为未完成下载的文件（如百度网盘 .baiduyun.p.downloading）。

    这类文件后缀消失（下载完成自动改名）后才可能成为完整压缩包，
    在此之前不能尝试解压。
    """
    name = path.name.lower()
    if name.endswith(".baiduyun.p.downloading"):
        return True
    return any(name.endswith(s) for s in _incomplete_suffixes())


def _strip_download_suffix(name):
    """去掉下载中后缀，得到下载完成后的目标文件名（如
    xxx.7z.002.baiduyun.p.downloading -> xxx.7z.002）。"""
    low = name.lower()
    suffixes = [".baiduyun.p.downloading"] + list(_incomplete_suffixes())
    suffixes.sort(key=len, reverse=True)
    for s in suffixes:
        if low.endswith(s):
            return name[:len(name) - len(s)]
    return name


def is_volume_name(name):
    """是否为分卷文件名：.001/.002、.z01、xxx.partN.rar 等（含脏字符归一后的）。"""
    return _is_volume_suffix(normalize_volume_name(name))


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
    if sanitized == original_name:
        # 名字式净化没改：再用同目录干净兄弟卷反推（应对任意脏字符）
        inferred = infer_volume_name_from_siblings(path)
        if inferred:
            sanitized = inferred
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
    if new_name == path.name:
        inferred = infer_volume_name_from_siblings(path)
        if inferred:
            new_name = inferred
    if new_name != path.name:
        new_path = path.with_name(new_name)
        path.rename(new_path)
        return new_path
    return path


def find_sevenzip_path():
    # 优先隔离版（%APPDATA%\AutoUnpacker\7z）：用户通过程序下载的版本
    # 一定是满足密码安全门槛的新版本，避免被系统里过旧的 7z 抢先。
    try:
        from ..sevenzip import ISOLATED_BIN
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


DISGUISE_CARRIER_EXTS = {
    "jpg", "jpeg", "png", "gif", "webp", "bmp", "ico", "tif", "tiff",
    "mp4", "mkv", "avi", "mov", "wmv", "flv", "webm", "mp3", "wav",
    "flac", "m4a", "aac", "pdf", "doc", "docx", "xls", "xlsx",
    # 文本族也常被用来伪装压缩包（改名 .txt 的 zip/7z）。普通文本只多读 8 字节
    # 头部 magic；真正的识别仍由 detect_archive_format 完成，不会误报。
    "txt", "csv", "log", "md",
}


def is_archive_file(path):
    """判断文件是否可能是压缩包（含改名伪装 / 分卷）。

    决定顺序（廉价优先，绝不抛异常）：
    1. 扩展名是压缩包后缀 → True；
    2. 名字像分卷（.001/.002/.z01/.r00/.partN.rar，含脏字符归一）→ True
       （首卷/非首卷由 _handle 既有分卷分支分辨处理）；
    3. 头部魔数是压缩包（改名伪装：.txt 的 zip、脏名 7z 等）→ True；
    4. 扩展名是常见伪装载体（图片/视频/音频/文档/文本），或体积 ≥
       SUSPECT_BIG_FILE_BYTES（正常图片/文档/文本不可能这么大，且是运行期间
       被下载进监听目录的 → 体积本身即强嫌疑）→ 用深层格式探测
       （detect_archive_format：尾部 EOCD + 前部扫描 + SFX，受 POLYGLOT 上限约束），
       识别「头部伪装 + 内嵌真实压缩包」的多段伪装文件；
    5. 其他 → False。
    """
    path = Path(path)
    if not path.is_file():
        return False  # 不存在/已被消费：不视为压缩包，避免下游对幽灵文件操作
    ext = path.suffix.lower().lstrip(".")
    if ext in ARCHIVE_EXTS:
        return True
    try:
        if is_volume_name(path.name):
            return True
    except Exception:
        pass
    try:
        if head_archive_format(path) is not None:
            return True
    except Exception:
        pass
    suspect = ext in DISGUISE_CARRIER_EXTS
    if not suspect:
        # 体积兜底：扩展名打发不掉的（.dat/.bin/无后缀等）也按体积放行深层探测
        try:
            suspect = path.stat().st_size >= SUSPECT_BIG_FILE_BYTES
        except OSError:
            suspect = False
    if suspect:
        try:
            fmt, _ = detect_archive_format(path)
            return fmt is not None
        except Exception:
            return False
    return False


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
    name = normalize_volume_name(name)
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
    name = normalize_volume_name(name)
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
    """剥离分卷编号，得到分卷基础名。如 2056.7z.002 -> 2056.7z、a.r00 -> a。

    先做脏字符归一（xxx.删除7z.删除001 -> xxx.7z.001）再取基础名：这样被插字/
    改名的卷能和它的干净兄弟卷归到同一基础名（volume_pair 的跨名配对、
    is_volume_file 的兄弟判定都依赖这个口径）。"""
    name = normalize_volume_name(name)
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
