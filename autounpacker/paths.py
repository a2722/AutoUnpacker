# -*- coding: utf-8 -*-
"""路径常量：数据文件放在「程序所在目录」，各模块经 `paths.X` 引用。

职责：- 定义 PROJECT_ROOT / DATA_DIR / CONFIG_FILE / TEMP_PW_FILE / CRASH_LOG / LOGS_DIR 等常量
- 冻结（PyInstaller）运行时改道：exe 同级目录可写就用它（便携优先），否则回落 %APPDATA%\\AutoUnpacker
- 提供 dll_dirs()：原生 DLL（libzbar-64.dll / libiconv.dll）的搜索目录（源码运行=项目根）
- 测试可重定向 DATA_DIR（或直接改 CONFIG_FILE 等），各模块统一引用保证全局生效
关键入口：dll_dirs()
依赖：仅标准库（os / sys / uuid / pathlib）
注意：各模块统一 `from . import paths` 后以 `paths.X` 引用，保证重定向全局生效；
      源码运行时 DATA_DIR 恒为 PROJECT_ROOT（与历史逐字节一致），改道只发生在 IS_FROZEN 分支
"""
import os
import sys
import uuid
from pathlib import Path

PACKAGE_DIR = Path(__file__).resolve().parent
PROJECT_ROOT = PACKAGE_DIR.parent

# 冻结（PyInstaller）运行时检测：源码运行恒为 False，行为与历史完全一致。
IS_FROZEN = bool(getattr(sys, "frozen", False))


def _exe_dir():
    """冻结运行时 sys.executable 所在目录（数据目录探测与 DLL 搜索共用）。"""
    try:
        return Path(sys.executable).resolve().parent
    except Exception:
        return PACKAGE_DIR


def _is_writable_dir(path):
    """探测 path 是否可写：在其中创建再删除一个唯一命名的临时文件。

    任何异常（目录不存在 / 无权限 / 只读）一律视为不可写；探测文件绝不残留。
    """
    probe = None
    try:
        probe = path / (".au_write_probe_" + uuid.uuid4().hex)
        with open(probe, "wb"):
            pass
        return True
    except Exception:
        return False
    finally:
        if probe is not None:
            try:
                probe.unlink()
            except OSError:
                pass


def _frozen_data_dir():
    """冻结运行时的数据目录：exe 同级（便携优先）→ 不可写则回落 %APPDATA%。

    与源码运行时「数据与程序放在一起」的直觉保持一致；exe 目录不可写
    （如装在 Program Files）时退回用户目录，保证始终有稳定可写的数据位置。
    """
    exe_dir = _exe_dir()
    if _is_writable_dir(exe_dir):
        return exe_dir
    base = os.environ.get("APPDATA") or str(Path.home())
    data_dir = Path(base) / "AutoUnpacker"
    data_dir.mkdir(parents=True, exist_ok=True)
    return data_dir


# 数据目录：源码运行恒为项目根（逐字节与历史一致）；冻结运行时在**导入期**
# 结算一次——db.py / deletion/records.py 都在 import 时绑定 DATA_DIR。
DATA_DIR = _frozen_data_dir() if IS_FROZEN else PROJECT_ROOT
CONFIG_FILE = DATA_DIR / "config.json"
TEMP_PW_FILE = DATA_DIR / "temp_passwords.json"
PENDING_TRUST_FILE = DATA_DIR / "pending_trust.json"   # 挂起的网址信任询问（重启恢复）
CRASH_LOG = DATA_DIR / "crash.log"
LOGS_DIR = DATA_DIR / "logs"
CACHE_DIR = DATA_DIR / "cache"        # 运行时生成的缓存（如主题图标），可随时重建
WORKERS_DIR = PACKAGE_DIR / "workers"
SINGLE_INSTANCE_EVENT = "Local\\AutoUnpacker_ShowEvent"


def dll_dirs():
    """原生 DLL（pyzbar 的 libzbar-64.dll / libiconv.dll）所在目录元组。

    源码运行：项目根目录（DLL 与源码同级）；
    冻结运行：PyInstaller 解包目录（sys._MEIPASS，若存在）与 exe 所在目录，
    去重且只保留真实存在的目录。app.py 启动时逐个加入 DLL 搜索路径。
    """
    if not IS_FROZEN:
        return (PROJECT_ROOT,)
    exe_dir = _exe_dir()
    meipass = getattr(sys, "_MEIPASS", None)
    out = []
    for cand in (Path(meipass) if meipass else None, exe_dir):
        if cand is None:
            continue
        try:
            if cand in out or not cand.is_dir():
                continue
        except Exception:
            continue
        out.append(cand)
    return tuple(out)
