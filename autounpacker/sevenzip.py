# -*- coding: utf-8 -*-
"""7-Zip 管理：版本发现/检测、隔离版与全局版的下载安装、卸载。

职责：- get_version()/check_version_ok() 读取 7z 版本并判断是否达 stdin 传密码门槛（18.00+），结果按路径缓存
- install_isolated() 优先走免提权路径（就地取用/下载本仓库的免安装 zip，标准库 zipfile 解到隔离目录），失败才回退官方安装器（弹一次 UAC）；install_global() 装到系统，始终需 UAC
- uninstall_isolated()/uninstall_system() 卸载（隔离版绝不被系统版卸载误伤）
关键入口：check_environment() / install_isolated() / install_global() / uninstall_isolated()
依赖：urllib.request、zipfile（解免安装包）、ctypes（UAC 提权）
注意：隔离版装在 %APPDATA%\\AutoUnpacker\\7z，不污染项目与全局；低于 MIN_VERSION 的 7-Zip 无法经 stdin 传密码，一律需升级
"""
import ctypes
import os
import re
import shutil
import subprocess
import sys
import tempfile
import time
import urllib.request
import uuid
import zipfile
from pathlib import Path

CREATE_NO_WINDOW = getattr(subprocess, "CREATE_NO_WINDOW", 0)

# 隔离版安装位置（不污染项目/全局）
_APPDATA = os.environ.get("APPDATA") or str(Path.home())
ISOLATED_DIR = Path(_APPDATA) / "AutoUnpacker" / "7z"
ISOLATED_BIN = ISOLATED_DIR / "bin" / "7z.exe"

# 免安装小包：由维护者把官方 7z.exe + 7z.dll（含 License/readme）打成一个普通 zip，
# 作为本仓库的 release 资产发布（tools/make_7zip_bundle.py 生成）。
# 普通 zip 用标准库 zipfile 就能解，所以隔离版安装全程不需要管理员权限；
# 而官方 -extra.7z 是 LZMA 压缩，Windows 自带 tar.exe 没有 LZMA codec，解不了。
BUNDLE_NAME = "AutoUnpacker-7zip.zip"
BUNDLE_URL = ("https://github.com/a2722/AutoUnpacker/releases/latest/download/"
              + BUNDLE_NAME)

# 免安装包内只允许这几个顶层文件落到隔离 bin 目录（其余一律忽略）
_BUNDLE_WHITELIST = frozenset({"7z.exe", "7z.dll", "License.txt", "readme.txt"})

# 支持 stdin 传密码的最低 7-Zip 版本（18.00 起）
MIN_VERSION = (18, 0, 0)

SEVEN_ZIP_HOME = "https://www.7-zip.org"
SEVEN_ZIP_DL = SEVEN_ZIP_HOME + "/download.html"

# 系统标准安装位置
SYSTEM_CANDIDATES = [
    Path(r"C:\Program Files\7-Zip\7z.exe"),
    Path(r"C:\Program Files (x86)\7-Zip\7z.exe"),
]

_7Z_RE = re.compile(
    r"7-Zip\s*(?:\([^)]*\)|\[[^\]]*\])?\s*([0-9]+(?:\.[0-9]+)*)", re.I)
_INSTALLER_RE = re.compile(r"(7z\d{4}-x64\.exe)", re.I)
_EXTRA_RE = re.compile(r"(7z\d{4}-extra\.7z)", re.I)
_USER_AGENT = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
               "AppleWebKit/537.36 (KHTML, like Gecko) AutoUnpacker/1.0")

# 版本检测缓存：{str(path): tuple|None}
_version_cache = {}


def _norm_version(v):
    """归一化为 (major, minor, patch) 三元组，便于比较。"""
    v = tuple(int(x) for x in (v or ()))
    while len(v) < 3:
        v += (0,)
    return v[:3]


def _parse_version_text(text):
    """从 7z 输出文本解析版本号三元组；失败返回 None。"""
    m = _7Z_RE.search(text or "")
    if not m:
        return None
    return _norm_version(m.group(1).split("."))


def invalidate_cache():
    """安装/卸载后调用，使版本缓存失效。"""
    _version_cache.clear()


def get_version(exe, use_cache=True):
    """读取 7z.exe 版本号（三元组）；失败返回 None。

    结果按路径缓存，避免每次解压都跑一次子进程（检查只在首次/手动时发生）。"""
    key = str(exe)
    if use_cache and key in _version_cache:
        return _version_cache[key]
    try:
        r = subprocess.run(
            [str(exe), "i"], capture_output=True, timeout=5,
            creationflags=CREATE_NO_WINDOW)
        text = (r.stdout or b"").decode("utf-8", "replace")
    except Exception:
        _version_cache[key] = None
        return None
    v = _parse_version_text(text)
    _version_cache[key] = v
    return v


def version_text(exe):
    """7z.exe 的人类可读版本号字符串。"""
    v = get_version(exe)
    if v is None:
        return "未知"
    if v[2] == 0:
        return f"{v[0]}.{v[1]:02d}"
    return f"{v[0]}.{v[1]:02d}.{v[2]}"


def check_version_ok(exe):
    """版本是否达到「可安全通过 stdin 传密码」的门槛。"""
    v = get_version(exe)
    return v is not None and v >= _norm_version(MIN_VERSION)


def find_system_sevenzip():
    """在标准位置 + PATH 里找系统版 7z。返回 Path 或 None。"""
    for c in SYSTEM_CANDIDATES:
        if c.exists():
            return c
    found = shutil.which("7z")
    return Path(found) if found else None


def check_environment():
    """探测当前 7-Zip 环境。

    优先级：隔离版（存在即用）> 系统版。
    返回 dict: {status, mode, path, version, version_str, min_ok}
    status: ok / low / none
    """
    if ISOLATED_BIN.exists():
        v = get_version(ISOLATED_BIN)
        ok = v is not None and v >= _norm_version(MIN_VERSION)
        return {
            "status": "ok" if ok else "low",
            "mode": "isolated",
            "path": str(ISOLATED_BIN),
            "version": v,
            "version_str": version_text(ISOLATED_BIN),
            "min_ok": bool(ok),
        }
    sys_path = find_system_sevenzip()
    if sys_path is not None:
        v = get_version(sys_path)
        ok = v is not None and v >= _norm_version(MIN_VERSION)
        return {
            "status": "ok" if ok else "low",
            "mode": "system",
            "path": str(sys_path),
            "version": v,
            "version_str": version_text(sys_path),
            "min_ok": bool(ok),
        }
    return {
        "status": "none", "mode": None, "path": None,
        "version": None, "version_str": None, "min_ok": False,
    }


# ---------------- 下载与安装 ----------------
def latest_release():
    """从官网获取最新版本信息。

    返回 (version_tuple, installer_url, extra_url)；失败抛异常。
    installer_url 是官方安装器（requireAdministrator），仅在免提权免安装包路径
    失败时作为回退，届时会弹出一次 UAC。"""
    req = urllib.request.Request(SEVEN_ZIP_DL, headers={"User-Agent": _USER_AGENT})
    try:
        with urllib.request.urlopen(req, timeout=15) as resp:
            html = resp.read().decode("utf-8", "replace")
    except Exception as e:
        raise RuntimeError(f"无法访问官网下载页：{e}")
    mi = _INSTALLER_RE.search(html)
    me = _EXTRA_RE.search(html)
    if not mi or not me:
        raise RuntimeError("官网页面解析失败，未找到最新安装包链接")
    fname = mi.group(1)  # 如 7z2602-x64.exe
    mm = re.match(r"7z(\d{4})-x64\.exe", fname)
    if not mm:
        raise RuntimeError("安装包文件名解析失败")
    num = int(mm.group(1))
    version = _norm_version((num // 100, num % 100, 0))
    return (version,
            SEVEN_ZIP_HOME + "/a/" + mi.group(1),
            SEVEN_ZIP_HOME + "/a/" + me.group(1))


def _download(url, dest):
    """下载文件到 dest（先写 .part 再原子替换）。TLS 校验保持开启。"""
    dest = Path(dest)
    dest.parent.mkdir(parents=True, exist_ok=True)
    tmp = dest.with_name(dest.name + f".{uuid.uuid4().hex[:6]}.part")
    req = urllib.request.Request(url, headers={"User-Agent": _USER_AGENT})
    try:
        with urllib.request.urlopen(req, timeout=120) as resp, open(tmp, "wb") as out:
            shutil.copyfileobj(resp, out, 1024 * 256)
        if tmp.stat().st_size < 1_000_000:
            raise RuntimeError("下载文件异常偏小，可能下载到错误内容")
        tmp.replace(dest)
    finally:
        try:
            tmp.unlink(missing_ok=True)
        except OSError:
            pass
    return dest


def _shell_runas(exe, params):
    """以管理员权限启动（触发 UAC）。返回是否成功启动。"""
    try:
        res = ctypes.windll.shell32.ShellExecuteW(None, "runas", str(exe), params, None, 1)
        return int(res) > 32
    except Exception:
        return False


def _is_admin():
    try:
        return bool(ctypes.windll.shell32.IsUserAnAdmin())
    except Exception:
        return False


def _app_dir():
    """程序自身目录：冻结(PyInstaller)时是 exe 所在目录，否则是项目根目录。

    免安装包可以放在这里（便携/绿色用户的常见做法，或安装器随包分发），
    安装时优先就地取用，无需联网。"""
    if getattr(sys, "frozen", False):
        return Path(sys.executable).resolve().parent
    return Path(__file__).resolve().parent.parent


def _find_local_bundle():
    """本地免安装包：优先隔离目录，其次程序目录。不存在返回 None。"""
    for base in (ISOLATED_DIR, _app_dir()):
        cand = base / BUNDLE_NAME
        try:
            if cand.is_file():
                return cand
        except OSError:
            continue
    return None


def _extract_bundle(bundle, tmp_dir):
    """用标准库 zipfile 解免安装包，只把白名单文件按其 basename 放进隔离 bin。

    只解到私有临时目录、且只认归档成员的 basename（绝不使用归档内路径），
    借此杜绝 zip-slip：形如 `..\\evil.txt` / `sub/evil.txt` / `other.dll` 的
    成员一律被忽略。"""
    extract_dir = tmp_dir / "unpack"
    extract_dir.mkdir(parents=True, exist_ok=True)
    dest = ISOLATED_BIN.parent
    dest.mkdir(parents=True, exist_ok=True)
    with zipfile.ZipFile(bundle) as zf:
        for member in zf.namelist():
            name = Path(member).name          # 只取 basename，防 zip-slip
            if member.endswith("/") or name not in _BUNDLE_WHITELIST:
                continue
            target = extract_dir / name
            with zf.open(member) as src, open(target, "wb") as out:
                shutil.copyfileobj(src, out)
            shutil.copy2(target, dest / name)


def install_isolated(progress=None):
    """安装隔离版到 %APPDATA%\\AutoUnpacker\\7z\\bin，返回 7z.exe 路径。

    优先走免提权路径：就地取用（隔离目录/程序目录）或下载本仓库的免安装 zip
    （内含官方 7z.exe + 7z.dll），用标准库 zipfile 解到隔离目录，全程无需管理员
    授权、不弹 UAC。仅当该路径未产出 7z.exe 时，才回退到官方安装器
    （requireAdministrator，会弹出一次 UAC）。两种方式都只写入隔离目录，不污染
    项目目录，也不写入 Program Files。"""
    def _msg(s):
        if progress:
            progress(s)

    ISOLATED_DIR.mkdir(parents=True, exist_ok=True)

    # latest_release() 会联网，只有确实需要时才调用（本地包场景零网络）
    _rel = {}

    def _release():
        if not _rel:
            v, iu, _bu = latest_release()
            _rel["version"] = v
            _rel["installer_url"] = iu
        return _rel["version"], _rel["installer_url"]

    # ---- 路径一（首选，零 UAC）：本地/下载的免安装 zip + 标准库 zipfile ----
    local = _find_local_bundle()
    downloaded = None
    tmp_dir = None
    try:
        if local is not None:
            _msg(f"正在使用本地 7-Zip 免安装包（不需要管理员授权）：{local}")
            bundle = local
        else:
            version, _iu = _release()
            _msg(f"正在下载 7-Zip {version[0]}.{version[1]:02d} 免安装包"
                 f"（免管理员权限，HTTPS）…")
            downloaded = ISOLATED_DIR / f"7z-bundle-{uuid.uuid4().hex[:6]}.zip"
            bundle = _download(BUNDLE_URL, downloaded)
        tmp_dir = Path(tempfile.mkdtemp(prefix="7z-unpack-", dir=str(ISOLATED_DIR)))
        _msg("正在解压到隔离目录（不需要管理员授权）…")
        _extract_bundle(bundle, tmp_dir)
    except Exception:
        # 取包/下载/解压任一环节失败都视为首选路径不可用，交给安装器回退
        pass
    finally:
        # 无论成败都清理下载的 zip 与临时解压目录，不留残渣（本地包不删）
        if downloaded is not None:
            try:
                downloaded.unlink(missing_ok=True)
            except OSError:
                pass
        if tmp_dir is not None:
            shutil.rmtree(tmp_dir, ignore_errors=True)

    # ---- 路径二（回退）：官方安装器（requireAdministrator，会弹一次 UAC） ----
    if not ISOLATED_BIN.exists():
        _msg("免管理员安装包不可用，改用官方安装器（可能弹出一次 UAC，请点「是」）…")
        _version, installer_url = _release()
        installer = ISOLATED_DIR / f"7z-setup-{uuid.uuid4().hex[:6]}.exe"
        _download(installer_url, installer)
        dest = str(ISOLATED_BIN.parent)
        need_runas = False
        try:
            subprocess.run([str(installer), "/S", f"/D={dest}"],
                           timeout=300, creationflags=CREATE_NO_WINDOW)
        except OSError:
            need_runas = True  # 非提权环境：CreateProcess 返回 740
        except subprocess.TimeoutExpired:
            need_runas = True
        if need_runas:
            _shell_runas(installer, f"/S /D={dest}")
        for _ in range(180):  # 最多等 90 秒（提权安装是异步的）
            if ISOLATED_BIN.exists():
                break
            time.sleep(0.5)
        for _ in range(10):  # 清理安装器（提权进程可能短暂占用）
            try:
                installer.unlink(missing_ok=True)
                break
            except OSError:
                time.sleep(1)

    invalidate_cache()
    if not ISOLATED_BIN.exists():
        raise RuntimeError(
            "隔离版安装未完成：免安装包不可用，官方安装器也未生成 7z.exe"
            "（UAC 未确认或安装失败）")
    if not check_version_ok(ISOLATED_BIN):
        raise RuntimeError(f"安装成功但版本异常（{version_text(ISOLATED_BIN)}）")
    _msg(f"隔离版安装成功：{ISOLATED_BIN}")
    return ISOLATED_BIN


def install_global(progress=None):
    """下载并安装全局版（默认安装到 Program Files，会触发 UAC）。"""
    version, installer_url, _extra_url = latest_release()

    def _msg(s):
        if progress:
            progress(s)

    tmp = Path(tempfile.gettempdir()) / f"7z-{uuid.uuid4().hex[:6]}.exe"
    _msg(f"正在下载 7-Zip {version[0]}.{version[1]:02d}（官网，HTTPS）…")
    _download(installer_url, tmp)
    _msg("正在安装到系统（若弹出 UAC 请允许）…")
    ok = _shell_runas(tmp, "/S")
    try:
        tmp.unlink(missing_ok=True)
    except OSError:
        pass
    if not ok:
        raise RuntimeError("无法启动安装程序（可能需要管理员权限）")
    p = None
    for _ in range(120):  # 最多等 60 秒
        cand = find_system_sevenzip()
        if cand is not None and check_version_ok(cand):
            p = cand
            break
        time.sleep(0.5)
    invalidate_cache()
    if p is None:
        raise RuntimeError("安装未完成或版本过低，未检测到可用的系统版 7-Zip")
    _msg(f"全局版安装成功：{p}")
    return p


# ---------------- 卸载 ----------------
def uninstall_isolated(progress=None):
    """卸载隔离版：先跑官方 Uninstall.exe（清理注册表），再删除目录。返回 (ok, msg)。"""
    if not ISOLATED_DIR.exists():
        return False, "未安装隔离版（%APPDATA%\\AutoUnpacker\\7z）"
    un = ISOLATED_BIN.parent / "Uninstall.exe"
    if un.exists():
        try:
            if _is_admin():
                subprocess.run([str(un), "/S"], timeout=120,
                               creationflags=CREATE_NO_WINDOW)
            else:
                _shell_runas(un, "/S")
        except Exception:
            pass
        time.sleep(2)  # 等卸载器清理目录
    try:
        shutil.rmtree(ISOLATED_DIR, ignore_errors=True)
    except Exception:
        pass
    invalidate_cache()
    if ISOLATED_DIR.exists():
        return False, "隔离版目录删除失败（可能被占用）"
    return True, "已卸载隔离版（%APPDATA%\\AutoUnpacker\\7z）"


def _is_isolated_path(p):
    """路径是否为隔离版目录（或其子目录）。"""
    p = os.path.normcase(str(Path(p).resolve()))
    base = os.path.normcase(str(ISOLATED_DIR.resolve()))
    return p == base or p.startswith(base + os.sep)


def _registry_install_dir():
    """从注册表找 7-Zip 安装目录（排除隔离版目录）。返回 Path 或 None。"""
    try:
        import winreg
    except ImportError:
        return None
    for hive in (winreg.HKEY_LOCAL_MACHINE, winreg.HKEY_CURRENT_USER):
        try:
            key = winreg.OpenKey(hive, r"SOFTWARE\Microsoft\Windows\CurrentVersion\Uninstall\7-Zip")
        except OSError:
            continue
        try:
            loc, _ = winreg.QueryValueEx(key, "InstallLocation")
            if loc and Path(loc).exists() and not _is_isolated_path(loc):
                return Path(loc)
        except OSError:
            pass
        try:
            winreg.CloseKey(key)
        except OSError:
            pass
    return None


def uninstall_system(progress=None):
    """卸载系统版 7-Zip（运行官方 Uninstall.exe /S）。返回 (ok, msg)。

    隔离版（%APPDATA%\\AutoUnpacker\\7z）不在此列，绝不会被误卸——
    隔离版有自己的卸载入口（uninstall_isolated）。"""
    def _msg(s):
        if progress:
            progress(s)

    candidates = []
    loc = _registry_install_dir()
    if loc is not None:
        candidates.append(loc)
    p = find_system_sevenzip()
    if p is not None and not _is_isolated_path(p.parent):
        candidates.append(p.parent)
    seen = set()
    for d in candidates:
        d = Path(d)
        key = str(d).lower()
        if key in seen:
            continue
        seen.add(key)
        un = d / "Uninstall.exe"
        if un.exists():
            _msg(f"正在卸载：{un}")
            try:
                if _is_admin():
                    subprocess.run([str(un), "/S"], timeout=120,
                                   creationflags=CREATE_NO_WINDOW)
                else:
                    _shell_runas(un, "/S")
            except Exception:
                pass
            for _ in range(120):  # 最多等 60 秒
                if not (d / "7z.exe").exists():
                    break
                time.sleep(0.5)
            invalidate_cache()
            return True, f"已运行卸载程序：{un}"
    return False, "未找到系统版 7-Zip 的卸载程序"