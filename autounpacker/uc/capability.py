# -*- coding: utf-8 -*-
"""可选组件探测：内嵌浏览器（PyQtWebEngine）是否可用。

**只做 find_spec，不真正 import** —— 这样在主程序启动路径上调用也不会
因为加载 QtWebEngine 而拖慢启动 / 引入崩溃风险。

契约：本模块的任何函数都**绝不抛异常**，失败一律当作「不可用」。
"""
import importlib.util

# 可选组件：PyQt5 的 QtWebEngine（pip 包名 PyQtWebEngine）
_WEB_MODULES = ("PyQt5.QtWebEngineWidgets", "PyQt5.QtWebEngineCore")

# 供 UI 直接显示的一句话提示（不换行，方便塞进小标/气泡）
INSTALL_HINT = "未安装浏览器组件（PyQtWebEngine），UC 网盘通路不可用"
INSTALL_DETAIL = (
    "该功能需要可选组件 PyQtWebEngine。\n\n"
    "安装命令：\n"
    "    pip install PyQtWebEngine==5.15.7\n\n"
    "未安装不影响其它功能，只是 UC 网盘通路会保持不可用。"
)


def _spec_exists(name):
    try:
        return importlib.util.find_spec(name) is not None
    except Exception:
        return False


def web_engine_available():
    """PyQtWebEngine 是否可用（两个子模块都在才算可用）。"""
    return all(_spec_exists(m) for m in _WEB_MODULES)


def web_engine_missing_reason():
    """不可用时的简短原因（可用于日志）；可用时返回空串。"""
    try:
        missing = [m for m in _WEB_MODULES if not _spec_exists(m)]
        if not missing:
            return ""
        return "缺少模块：" + ", ".join(missing)
    except Exception:
        return "探测失败"
