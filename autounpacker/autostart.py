# -*- coding: utf-8 -*-
"""开机自启（登录后静默启动，不弹主界面）。

真源是注册表 `HKEY_CURRENT_USER\\Software\\Microsoft\\Windows\\CurrentVersion\\Run`
下的一个值（名字见 `config.AUTOSTART_VALUE_NAME`）——用 HKCU 而非 HKLM/计划任务：
无需管理员、免 UAC，且只对当前用户生效（符合「便携、不污染系统」的一贯口径）。
config 的 `autostart_enabled` 只作「期望状态」缓存与首屏勾选回填，**不驱动真实行为**；
`sync_from_registry()` / `is_enabled()` 一律以注册表为准，防止配置与系统实际不一致。

命令行：`<启动命令> --autostart`。启动命令按运行形态取值——
- 冻结（PyInstaller）运行：`"<exe>"`（exe 自身就是入口，空格路径自动加引号）；
- 源码运行：`"<pythonw.exe>" "<main.py>"`（优先 pythonw，避免留一个控制台黑窗）。

`--autostart` 分支在 app.main() 里已存在：发现单实例事件已存在时**不唤醒**已有实例、
自己静默退出；单实例时才创建窗口且**不调用 `start_interface()`**（即隐藏到托盘）。
本模块只负责「注册/注销/查询」这一件事，不碰 Qt、不做 IO 之外的副作用。

依赖：仅标准库（winreg / sys / pathlib）；非 win32 平台一律安全 no-op（返回 False）。
"""
import sys
from pathlib import Path

from . import config as _config

RUN_KEY = _config.AUTOSTART_RUN_KEY
VALUE_NAME = _config.AUTOSTART_VALUE_NAME


def _import_winreg():
    """惰性取 winreg（非 win32 返回 None，调用方据此安全 no-op）。"""
    try:
        import winreg  # noqa: WPS433（平台专属，必须惰性）
        return winreg
    except Exception:
        return None


def _entry_command():
    """开机自启要写入注册表的完整命令行（含 `--autostart`；空格路径已加引号）。

    冻结运行：`"<exe>" --autostart`（exe 自身即入口）。
    源码运行：`"<pythonw.exe>" "<main.py>" --autostart`；pythonw 缺失时回落
    当前解释器（可在 sys.executable 同目录找到 pythonw.exe 时优先用它）。
    """
    if getattr(sys, "frozen", False):
        exe = str(Path(sys.executable).resolve())
        return _quote(exe) + " --autostart"
    interp = _preferred_interpreter()
    root = Path(__file__).resolve().parent.parent      # 项目根（含 main.py）
    main_py = root / "main.py"
    return " ".join((_quote(interp), _quote(str(main_py)), "--autostart"))


def _quote(path):
    """命令行参数加引号（路径含空格必需）；已带引号则原样返回。"""
    s = str(path)
    if s.startswith('"') and s.endswith('"'):
        return s
    return '"%s"' % s


def _preferred_interpreter():
    """源码运行的启动解释器：优先同目录 pythonw.exe（无控制台黑窗），否则当前解释器。"""
    try:
        cur = Path(sys.executable)
        cand = cur.with_name("pythonw.exe")
        if cand.exists():
            return str(cand)
    except Exception:
        pass
    return str(getattr(sys, "executable", "python") or "python")


def is_enabled():
    """当前用户的开机自启是否已启用（读注册表；非 win32 / 任何异常一律 False）。"""
    winreg = _import_winreg()
    if winreg is None:
        return False
    try:
        with winreg.OpenKey(winreg.HKEY_CURRENT_USER, RUN_KEY) as k:
            val, _typ = winreg.QueryValueEx(k, VALUE_NAME)
            return bool(str(val or "").strip())
    except FileNotFoundError:
        return False
    except OSError:
        return False
    except Exception:
        return False


def enable():
    """写入开机自启项（幂等）。返回是否成功（非 win32 / 无权限一律 False）。"""
    winreg = _import_winreg()
    if winreg is None:
        return False
    try:
        with winreg.CreateKeyEx(winreg.HKEY_CURRENT_USER, RUN_KEY, 0,
                                winreg.KEY_SET_VALUE) as k:
            winreg.SetValueEx(k, VALUE_NAME, 0, winreg.REG_SZ, _entry_command())
        return True
    except Exception:
        return False


def disable():
    """删除开机自启项（幂等：本就不存在也算成功）。返回是否成功/已不存在。"""
    winreg = _import_winreg()
    if winreg is None:
        return False
    try:
        with winreg.CreateKeyEx(winreg.HKEY_CURRENT_USER, RUN_KEY, 0,
                                winreg.KEY_SET_VALUE) as k:
            try:
                winreg.DeleteValue(k, VALUE_NAME)
            except FileNotFoundError:
                pass
            except OSError:
                pass
        return True
    except Exception:
        return False


def apply(enabled):
    """按期望状态写注册表：enabled 真则 enable()、假则 disable()。

    返回 `(ok, actual)`：ok 为本次操作是否成功；actual 为操作后注册表的**实际**
    状态（读回校验，避免「写失败却回填成已启用」的撒谎）。"""
    ok = enable() if enabled else disable()
    return ok, is_enabled()


def sync_from_registry(cfg):
    """启动时用注册表实际状态校正 config 缓存（返回是否发生了变更）。

    以注册表为准：config 说开着但用户手工删了 Run 项 → 纠正为关；反之亦然。
    绝不让「配置与系统实际不一致」把设置页的勾选状态搞成谎报。任何异常吞掉。
    """
    try:
        actual = bool(is_enabled())
        if bool(cfg.get("autostart_enabled", False)) != actual:
            cfg["autostart_enabled"] = actual
            return True
    except Exception:
        pass
    return False
