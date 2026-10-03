# -*- coding: utf-8 -*-
"""WinRAR 可执行文件定位（可选外部引擎，绝不作为硬依赖）。

为什么需要它：7-Zip 的 zip 解析器对某些**合法**的 Zip64 ZIP 无能为力
（真机案：一个伪装成 MP4 的 Zip64 加密 ZIP，内部是 7z 分卷；7z 三个版本
全部 `Cannot open`，而 WinRAR 用正确密码 60 秒就解出来）。这类文件在程序里
原有路径是「7z 失败 → 回退 Python zipfile」，而纯 Python 解 6GB 要 20+ 分钟
且占 GIL 冻结界面。WinRAR 可作为「7z 与 Python zipfile 之间的可选快路」。

设计口径：
- **可选、可关、可手动指定路径**：用户不一定装 WinRAR，也可能是绿色版放在任意
  位置；程序绝不捆绑 WinRAR，也不因缺它而失败（缺了照样回退 Python zipfile）。
- 只有 `WinRAR.exe` 能解 zip；`Rar.exe` / `UnRAR.exe` 仅处理 RAR，不用。

定位顺序（第一个可用的胜出）：
  1. 用户显式设置的路径（config `winrar_path`，支持绿色版任意位置）
  2. 注册表 App Paths：`HKLM\\SOFTWARE\\Microsoft\\Windows\\CurrentVersion\\App Paths\\WinRAR.exe`
     （含 WOW6432Node），值 `(default)` 或 `Path`
  3. 注册表 `HKLM/HKCU\\SOFTWARE\\WinRAR`（值 `exe64` / `exe`；含 WOW6432Node）
  4. 常见安装目录（Program Files / Program Files (x86)）
  5. PATH 上的 `WinRAR.exe`
绝不抛异常：任何失败都返回 None。
"""
import os
import shutil
from pathlib import Path

_WINRAR_EXE = "WinRAR.exe"


def _looks_like_winrar(exe):
    """粗校验：文件存在且名为 WinRAR.exe（大小写不敏感）。"""
    try:
        p = Path(exe)
        return p.is_file() and p.name.lower() == _WINRAR_EXE.lower()
    except Exception:
        return False


def _from_registry():
    """从注册表取 WinRAR.exe 路径（App Paths → WinRAR 键）。返回 Path 或 None。"""
    try:
        import winreg
    except ImportError:
        return None
    # App Paths：安装程序标准登记处
    app_paths_sub = (r"SOFTWARE\Microsoft\Windows\CurrentVersion"
                     r"\App Paths\WinRAR.exe")
    for hive in (winreg.HKEY_LOCAL_MACHINE, winreg.HKEY_CURRENT_USER):
        for sub in (app_paths_sub,
                    r"SOFTWARE\WOW6432Node\Microsoft\Windows\CurrentVersion"
                    r"\App Paths\WinRAR.exe"):
            try:
                with winreg.OpenKey(hive, sub) as key:
                    for val in ("", "Path"):
                        try:
                            data, _ = winreg.QueryValueEx(key, val)
                        except OSError:
                            continue
                        if not data:
                            continue
                        cand = Path(data)
                        if cand.is_dir():
                            cand = cand / _WINRAR_EXE
                        if _looks_like_winrar(cand):
                            return cand
            except OSError:
                continue
    # WinRAR 自己的键：exe64 / exe
    for hive in (winreg.HKEY_LOCAL_MACHINE, winreg.HKEY_CURRENT_USER):
        for sub in (r"SOFTWARE\WinRAR", r"SOFTWARE\WOW6432Node\WinRAR"):
            try:
                with winreg.OpenKey(hive, sub) as key:
                    for val in ("exe64", "exe"):
                        try:
                            data, _ = winreg.QueryValueEx(key, val)
                        except OSError:
                            continue
                        if data and _looks_like_winrar(Path(data)):
                            return Path(data)
            except OSError:
                continue
    return None


def _from_common_dirs():
    """常见安装目录。返回 Path 或 None。"""
    roots = []
    for env in ("ProgramFiles", "ProgramFiles(x86)", "ProgramW6432"):
        base = os.environ.get(env)
        if base:
            roots.append(Path(base) / "WinRAR" / _WINRAR_EXE)
    for cand in roots:
        if _looks_like_winrar(cand):
            return cand
    return None


def _from_path():
    """PATH 上的 WinRAR.exe。返回 Path 或 None。"""
    try:
        found = shutil.which(_WINRAR_EXE)
    except Exception:
        found = None
    if found and _looks_like_winrar(Path(found)):
        return Path(found)
    return None


def find_winrar(explicit_path=None):
    """定位 WinRAR.exe。返回 Path 或 None（绝不抛异常）。

    explicit_path：用户设置的路径（config `winrar_path`）——它排在最前，
    且**若用户显式给了路径却不合法，直接返回 None**（不悄悄回落到自动探测，
    避免用户以为「我指定了」实际用的是别的）。传 None / 空串则走自动探测。
    """
    try:
        if explicit_path:
            p = Path(str(explicit_path))
            if p.is_dir():
                p = p / _WINRAR_EXE
            return p if _looks_like_winrar(p) else None
        for finder in (_from_registry, _from_common_dirs, _from_path):
            try:
                got = finder()
            except Exception:
                got = None
            if got is not None:
                return got
    except Exception:
        return None
    return None


def is_available(explicit_path=None):
    """是否存在可用的 WinRAR.exe。"""
    return find_winrar(explicit_path) is not None
