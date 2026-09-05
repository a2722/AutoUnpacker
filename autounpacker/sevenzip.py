# -*- coding: utf-8 -*-
"""
7-Zip 管理模块：版本发现/检测、隔离版与全局版安装、卸载。

设计要点：
- 隔离版存放在 %APPDATA%\\AutoUnpacker\\7z，不污染项目目录，也不影响全局环境。
- 下载只发生在用户明确同意之后（首次启动检测弹窗 / 设置里的按钮），不捆绑任何二进制。
- 版本门槛：低于 MIN_VERSION 的 7-Zip 无法通过 stdin 传密码（密码只能拼在命令行，
  会被任务管理器/WMI 窥探），一律视为「低版本」，需升级后才能使用。
- 版本检测结果按 exe 路径缓存；安装/卸载后调用 invalidate_cache() 使缓存失效。
"""
import ctypes
import os
import re
import shutil
import subprocess
import tempfile
import time
import urllib.request
import uuid
from pathlib import Path

CREATE_NO_WINDOW = getattr(subprocess, "CREATE_NO_WINDOW", 0)

# 隔离版安装位置（不污染项目/全局）
_APPDATA = os.environ.get("APPDATA") or str(Path.home())
ISOLATED_DIR = Path(_APPDATA) / "AutoUnpacker" / "7z"
ISOLATED_BIN = ISOLATED_DIR / "bin" / "7z.exe"

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
    extra 包（7z.exe + 7z.dll 免安装控制台版）用于免提权的隔离安装。"""
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


def _extract_with_tar(extra, dest_dir):
    """用 Windows 自带 tar.exe（libarchive，支持 7z）解压 extra 包，免管理员权限。"""
    dest_dir.mkdir(parents=True, exist_ok=True)
    r = subprocess.run(["tar", "-xf", str(extra), "-C", str(dest_dir)],
                       timeout=180, creationflags=CREATE_NO_WINDOW)
    if r.returncode != 0:
        raise RuntimeError(f"tar 解压 extra 包失败（退出码 {r.returncode}）")


def install_isolated(progress=None):
    """下载并静默安装隔离版到 %APPDATA%\\AutoUnpacker\\7z\\bin。

    注意：7-Zip 官方安装器 manifest 为 requireAdministrator，即使安装到用户
    目录也会弹出一次 UAC 授权。确认后经 /S /D= 静默安装到隔离目录，
    不污染项目目录，也不会写入 Program Files。返回 7z.exe 路径。"""
    version, installer_url, _extra = latest_release()

    def _msg(s):
        if progress:
            progress(s)

    ISOLATED_DIR.mkdir(parents=True, exist_ok=True)
    installer = ISOLATED_DIR / f"7z-setup-{uuid.uuid4().hex[:6]}.exe"
    _msg(f"正在下载 7-Zip {version[0]}.{version[1]:02d}（官网，HTTPS）…")
    _download(installer_url, installer)
    _msg("正在安装到隔离目录（若弹出 UAC 请点「是」）…")
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
        raise RuntimeError("安装未完成，未生成 7z.exe（UAC 未确认或安装失败）")
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