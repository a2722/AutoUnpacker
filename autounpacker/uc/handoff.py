# -*- coding: utf-8 -*-
"""把下载直链交给用户的下载器（纯标准库，无 Qt 依赖）。

调研结论（2026-11）决定的四种交接方式：

| 方式 | 覆盖 | 说明 |
|---|---|---|
| `idm`       | IDM | `IDMan.exe /n /d <url> [/p 目录] [/f 文件名]`，IDM 未运行会自动拉起 |
| `browser`   | 一切 | `os.startfile(url)` 交给默认浏览器；装了扩展的下载器（IDM/迅雷/FDM/XDM/Motrix）会自行拦截 |
| `clipboard` | 迅雷 / FDM / IDM | 只把链接写进剪贴板；这几家的「监视剪贴板」默认开启，会自动弹新建任务。XDM 默认关、Motrix 不监视 |
| `auto`      | 最多 | 装了 IDM 就直连 IDM，否则退回 `browser` |

契约：本模块所有函数**绝不抛异常**，失败一律返回 `(False, 原因)`。
"""
import os
import shutil
import subprocess

from . import capability  # noqa: F401  (保留：部分环境用其提示文案)

# ---- 注册表读取（延迟 import，非 Windows 环境自动降级） ----
try:
    import winreg
except Exception:  # pragma: no cover - 非 Windows
    winreg = None

_APP_PATHS = r"SOFTWARE\Microsoft\Windows\CurrentVersion\App Paths\%s"


def _app_path(exe_name):
    """查 App Paths；找不到返回空串。"""
    if winreg is None:
        return ""
    for hive in (winreg.HKEY_LOCAL_MACHINE, winreg.HKEY_CURRENT_USER):
        for view in (0, getattr(winreg, "KEY_WOW64_64KEY", 0),
                     getattr(winreg, "KEY_WOW64_32KEY", 0)):
            try:
                key = winreg.OpenKey(hive, _APP_PATHS % exe_name, 0,
                                     winreg.KEY_READ | view)
            except Exception:
                continue
            try:
                val = winreg.QueryValue(key, None)
            except Exception:
                val = ""
            finally:
                try:
                    winreg.CloseKey(key)
                except Exception:
                    pass
            if val and os.path.isfile(val):
                return val
    return ""


def _first_file(paths):
    for p in paths:
        try:
            if p and os.path.isfile(p):
                return p
        except Exception:
            continue
    return ""


def _env(*parts):
    try:
        return os.path.join(*parts)
    except Exception:
        return ""


def _idm_exe_from_registry():
    """IDM 自己的主键：HKCU/HKLM\\SOFTWARE\\DownloadManager 的 ExePath。

    绿色版 / 装到非标准目录的 IDM **不会**注册 App Paths，只写这个键——
    本机实测（IDM v6.43 绿色版，装在 E 盘）就只有这一处。缺了它检测会漏。
    """
    if winreg is None:
        return ""
    for hive in (winreg.HKEY_CURRENT_USER, winreg.HKEY_LOCAL_MACHINE):
        for view in (0, getattr(winreg, "KEY_WOW64_64KEY", 0),
                     getattr(winreg, "KEY_WOW64_32KEY", 0)):
            try:
                key = winreg.OpenKey(hive, r"SOFTWARE\DownloadManager", 0,
                                     winreg.KEY_READ | view)
            except Exception:
                continue
            try:
                try:
                    val = winreg.QueryValueEx(key, "ExePath")[0]
                except Exception:
                    val = ""
            finally:
                try:
                    winreg.CloseKey(key)
                except Exception:
                    pass
            val = str(val or "").strip()
            if val and os.path.isfile(val):
                return val
    return ""


def _resolve_lnk_target(lnk_path):
    """解析快捷方式目标（pywin32/WScript.Shell）；失败返回空串。"""
    try:
        import win32com.client  # pywin32（项目本就依赖）
        shell = win32com.client.Dispatch("WScript.Shell")
        target = str(shell.CreateShortCut(str(lnk_path)).TargetPath or "").strip()
        if target and os.path.isfile(target):
            return target
    except Exception:
        pass
    return ""


def _idm_from_shortcuts():
    """兜底：桌面 / 开始菜单里的 IDM 快捷方式。

    覆盖面：用户把快捷方式放桌面、程序装在任意盘符（例如桌面下的
    `软件\Internet Download Manager.lnk` 这种分类目录里）。
    只扫一层目录 + 一层分类子目录，不做全盘遍历（避免启动卡顿）。
    """
    if os.name != "nt":
        return ""
    home = os.environ.get("USERPROFILE", "")
    appdata = os.environ.get("APPDATA", "")
    progdata = os.environ.get("PROGRAMDATA", "")
    public = os.environ.get("PUBLIC", r"C:\Users\Public")
    roots = [
        _env(home, "Desktop"),
        _env(home, "桌面"),
        _env(public, "Desktop"),
        _env(appdata, "Microsoft", "Windows", "Start Menu", "Programs"),
        _env(progdata, "Microsoft", "Windows", "Start Menu", "Programs"),
    ]
    # 桌面下常见的一层分类目录（本机是 Desktop\软件\）
    for base in list(roots):
        try:
            for name in os.listdir(base):
                sub = os.path.join(base, name)
                if os.path.isdir(sub):
                    roots.append(sub)
        except Exception:
            continue
    seen = set()
    for base in roots:
        if not base or base in seen:
            continue
        seen.add(base)
        try:
            for name in os.listdir(base):
                low = name.lower()
                if not low.endswith(".lnk"):
                    continue
                if "idm" not in low and "download manager" not in low:
                    continue
                target = _resolve_lnk_target(os.path.join(base, name))
                if target and os.path.basename(target).lower() == "idman.exe":
                    return target
        except Exception:
            continue
    return ""


def find_idm():
    """IDM 的 IDMan.exe 路径；未安装返回空串。

    按可靠性依次尝试（顺序就是踩坑顺序）：
    1. `HKCU\\SOFTWARE\\DownloadManager\\ExePath` —— 绿色版/非标准目录只写这里
    2. App Paths —— 标准安装
    3. 常见安装路径
    4. 桌面 / 开始菜单快捷方式（只扫一层 + 一层分类目录）
    """
    p = _idm_exe_from_registry()
    if p:
        return p
    p = _app_path("IDMan.exe")
    if p:
        return p
    local = _env(os.environ.get("LOCALAPPDATA", ""), "Programs", "Internet Download Manager")
    p = _first_file([
        r"C:\Program Files\Internet Download Manager\IDMan.exe",
        r"C:\Program Files (x86)\Internet Download Manager\IDMan.exe",
        _env(local, "IDMan.exe"),
    ])
    if p:
        return p
    return _idm_from_shortcuts()


def find_aria2():
    """aria2c.exe（PATH / scoop / 默认目录）。"""
    try:
        w = shutil.which("aria2c")
        if w:
            return w
    except Exception:
        pass
    return _first_file([
        r"C:\Program Files\aria2\aria2c.exe",
        r"C:\Program Files (x86)\aria2\aria2c.exe",
        _env(os.environ.get("USERPROFILE", ""), "scoop", "shims", "aria2c.exe"),
    ])


def find_xdm():
    p = _app_path("xdm-app.exe")
    if p:
        return p
    try:
        w = shutil.which("xdm-app") or shutil.which("xdman")
        if w:
            return w
    except Exception:
        pass
    return _first_file([
        r"C:\Program Files\XDM\xdm-app.exe",
        _env(os.environ.get("LOCALAPPDATA", ""), "Programs", "XDM", "xdm-app.exe"),
        r"C:\Program Files (x86)\XDM\xdman.exe",
    ])


def find_gopeed():
    p = _app_path("gopeed.exe")
    if p:
        return p
    return _first_file([
        r"C:\Program Files\Gopeed\gopeed.exe",
        _env(os.environ.get("LOCALAPPDATA", ""), "Programs", "Gopeed", "gopeed.exe"),
    ])


def find_thunder():
    p = _app_path("Thunder.exe")
    if p:
        return p
    return _first_file([
        r"C:\Program Files (x86)\Thunder Network\Thunder\Program\Thunder.exe",
        r"C:\Program Files\Thunder Network\Thunder\Program\Thunder.exe",
    ])


def find_fdm():
    p = _app_path("fdm.exe")
    if p:
        return p
    return _first_file([
        r"C:\Program Files\Softdeluxe\Free Download Manager\fdm.exe",
        r"C:\Program Files (x86)\Softdeluxe\Free Download Manager\fdm.exe",
    ])


def detect_downloaders():
    """返回已安装的下载器列表 [{"key","name","exe"}]，顺序即推荐优先级。"""
    out = []
    for key, name, fn in (
        ("idm", "Internet Download Manager", find_idm),
        ("aria2", "aria2", find_aria2),
        ("gopeed", "Gopeed", find_gopeed),
        ("xdm", "Xtreme Download Manager", find_xdm),
        ("thunder", "迅雷", find_thunder),
        ("fdm", "Free Download Manager", find_fdm),
    ):
        try:
            exe = fn()
        except Exception:
            exe = ""
        if exe:
            out.append({"key": key, "name": name, "exe": exe})
    return out


def copy_text_to_clipboard(text):
    """把文本写进 Windows 剪贴板（ctypes，无需 QApplication）。失败返回 False。"""
    try:
        import ctypes
        from ctypes import wintypes

        user32 = ctypes.windll.user32
        kernel32 = ctypes.windll.kernel32
        CF_UNICODETEXT = 13
        GMEM_MOVEABLE = 0x0002

        data = str(text or "")
        if not data:
            return False
        buf = ctypes.create_unicode_buffer(data)
        size = ctypes.sizeof(buf)

        kernel32.GlobalAlloc.restype = wintypes.HGLOBAL
        kernel32.GlobalAlloc.argtypes = [wintypes.UINT, ctypes.c_size_t]
        h = kernel32.GlobalAlloc(GMEM_MOVEABLE, size)
        if not h:
            return False
        kernel32.GlobalLock.restype = ctypes.c_void_p
        kernel32.GlobalLock.argtypes = [wintypes.HGLOBAL]
        ptr = kernel32.GlobalLock(h)
        if not ptr:
            kernel32.GlobalFree(h)
            return False
        try:
            ctypes.memmove(ptr, ctypes.addressof(buf), size)
        finally:
            kernel32.GlobalUnlock(h)

        user32.OpenClipboard.argtypes = [wintypes.HWND]
        user32.SetClipboardData.restype = wintypes.HANDLE
        user32.SetClipboardData.argtypes = [wintypes.UINT, wintypes.HANDLE]
        if not user32.OpenClipboard(None):
            kernel32.GlobalFree(h)
            return False
        try:
            user32.EmptyClipboard()
            return bool(user32.SetClipboardData(CF_UNICODETEXT, h))
        finally:
            user32.CloseClipboard()
    except Exception:
        return False


def send_to_idm(url, save_dir="", filename=""):
    """把 URL 加进 IDM 并立即开始下载。返回 (ok, detail)。"""
    exe = find_idm()
    if not exe:
        return False, "未检测到 IDM"
    args = [exe, "/n", "/d", str(url)]
    if save_dir:
        args += ["/p", str(save_dir)]
    if filename:
        args += ["/f", str(filename)]
    try:
        subprocess.Popen(args, close_fds=True)
    except Exception as e:
        return False, "调用 IDM 失败：%s" % e
    return True, "已提交给 IDM"


def open_in_browser(url):
    """交给系统默认浏览器（扩展型下载器会在浏览器层面拦截）。"""
    try:
        os.startfile(str(url))
    except Exception as e:
        return False, "打开默认浏览器失败：%s" % e
    return True, "已在默认浏览器打开"


def handoff(url, mode="auto", save_dir="", filename="", log=None):
    """统一入口：按 mode 把直链交出去。

    返回 (ok, detail, used)：
        ok     是否已交付成功
        detail 中文说明（可直接进日志）
        used   实际使用的方式（"idm" / "browser" / "clipboard" / "none"）

    mode 非法值按 "auto" 处理；任何异常都折成 (False, 原因, "none")。
    """
    try:
        u = str(url or "").strip()
        if not u:
            return False, "下载直链为空", "none"
        m = str(mode or "auto").strip().lower()
        if m not in ("auto", "idm", "browser", "clipboard"):
            m = "auto"

        if m == "auto":
            m = "idm" if find_idm() else "browser"

        if m == "idm":
            ok, detail = send_to_idm(u, save_dir, filename)
            if ok:
                return True, detail, "idm"
            # IDM 失败一律退回浏览器，绝不把用户卡住
            ok2, d2 = open_in_browser(u)
            return ok2, "%s；已改用默认浏览器" % d2, "browser" if ok2 else "none"

        if m == "clipboard":
            if copy_text_to_clipboard(u):
                return True, "下载链接已复制到剪贴板（迅雷/FDM/IDM 会自动接管）", "clipboard"
            ok2, d2 = open_in_browser(u)
            return ok2, "写剪贴板失败；%s" % d2, "browser" if ok2 else "none"

        ok, detail = open_in_browser(u)
        return ok, detail, "browser" if ok else "none"
    except Exception as e:
        return False, "交接下载链接失败：%s" % e, "none"
