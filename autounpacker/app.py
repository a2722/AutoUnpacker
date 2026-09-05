# -*- coding: utf-8 -*-
"""程序入口：单实例检测、依赖探测、后台线程启动、GUI 组装。"""
import os
import sys

from . import paths
from . import extract as smart_extract
from . import trail as deletion_trail
from . import db
from .config import load_config, save_config
from .utils import _install_crash_log
from .state import AppState
from .hub import Hub
from .monitors import FolderWatcher, QRMonitor, QR_AVAILABLE

# 把项目根目录加入 DLL 搜索路径（pyzbar 依赖 libzbar-64.dll / libiconv.dll，
# DLL 位于项目根目录）
if sys.platform == "win32" and hasattr(os, "add_dll_directory"):
    try:
        os.add_dll_directory(str(paths.PROJECT_ROOT))
    except OSError:
        pass


def _ensure_qt_platform_plugins():
    """在导入 PyQt5 前，把 Qt 平台插件目录注入环境变量。

    修复 venv 部署时 "no Qt platform plugin could be initialized"：
    Qt 在 venv 下可能把应用目录解析成基础 Python 目录，导致平台插件
    搜索路径指向不存在的位置。这里用 PyQt5 包自身定位 plugins 目录
    （site-packages\\PyQt5\\Qt5\\plugins），并显式注入
    QT_QPA_PLATFORM_PLUGIN_PATH，让 QFactoryLoader 一定能找到
    qwindows.dll。若环境变量已设置（用户/打包器指定）则不覆盖。"""
    if os.environ.get("QT_QPA_PLATFORM_PLUGIN_PATH"):
        return
    try:
        import PyQt5
        from pathlib import Path
        pkg = Path(PyQt5.__file__).resolve().parent
        candidates = [
            pkg / "Qt5" / "plugins",
            pkg / "plugins",
            pkg.parent / "PyQt5" / "Qt5" / "plugins",
        ]
        for cand in candidates:
            if (cand / "platforms" / "qwindows.dll").exists():
                os.environ["QT_QPA_PLATFORM_PLUGIN_PATH"] = str(cand)
                return
    except Exception:
        pass
    # 兜底：让 Qt 用 QLibraryInfo 自身路径（经一次无害的 QCoreApplication 探测）
    try:
        from pathlib import Path as _Path
        from PyQt5.QtCore import QCoreApplication, QLibraryInfo
        app = QCoreApplication([])
        try:
            plugins = QLibraryInfo.location(QLibraryInfo.PluginsPath)
        finally:
            app.quit()
        if plugins and (_Path(plugins) / "platforms" / "qwindows.dll").exists():
            os.environ["QT_QPA_PLATFORM_PLUGIN_PATH"] = str(plugins)
    except Exception:
        pass


def main():
    if sys.platform != "win32":
        print("此程序仅支持 Windows")
        return 1

    # Qt 平台插件路径必须在任何 PyQt5 导入（含 password_book/main_window
    # 等 UI 模块）之前注入，否则 venv 下窗口直接闪退。
    _ensure_qt_platform_plugins()

    _install_crash_log()
    # 原生崩溃（SIGSEGV/SIGABRT 等）也把 Python 调用栈打到 crash.log，便于定位
    try:
        import faulthandler
        faulthandler.enable(open(paths.CRASH_LOG, "a", encoding="utf-8"))
    except Exception:
        pass

    autostart = "--autostart" in sys.argv
    force = "--force" in sys.argv   # 强制新开：跳过单实例检测（清理僵尸实例用）

    # 单实例检测：命名事件。新实例发现事件已存在，说明程序已在运行，
    # 通过 SetEvent 让已运行实例把窗口调到前台后自行退出。
    # 僵尸实例（卡死/弹错误框但事件未释放）会拦截新实例，导致双击无反应；
    # 因此：写日志提示 + 支持 --force 强制新开。
    show_event = None
    if not force:
        try:
            import win32event
            import win32api
            import winerror
            show_event = win32event.CreateEvent(None, True, False, paths.SINGLE_INSTANCE_EVENT)
            if win32api.GetLastError() == winerror.ERROR_ALREADY_EXISTS:
                print("检测到已有实例在运行，本实例退出。"
                      "（如无响应请结束旧进程或使用 --force 强制新开）")
                try:
                    d = paths.LOGS_DIR
                    d.mkdir(exist_ok=True)
                    with open(d / f"{__import__('time').strftime('%Y-%m-%d')}.log",
                              "a", encoding="utf-8") as f:
                        f.write(f"[{__import__('time').strftime('%Y-%m-%d %H:%M:%S')}] "
                                "检测到已有实例在运行，新实例退出（加 --force 可强制新开）\n")
                except Exception:
                    pass
                if not autostart:
                    try:
                        win32event.SetEvent(show_event)
                    except Exception:
                        pass
                return 0
        except Exception:
            show_event = None
    else:
        # --force 模式：清除可能残留的旧事件，避免新实例也被旧事件拦截
        try:
            import win32event
            import win32api
            import winerror
            ev = win32event.CreateEvent(None, True, False, paths.SINGLE_INSTANCE_EVENT)
            win32event.ResetEvent(ev)
        except Exception:
            pass

    # 删除回溯窗口期 = 本次开机内：启动时清掉开机前产生的记录，防止累积
    try:
        deletion_trail.prune_records()
    except Exception:
        pass

    cfg = load_config()

    # 初始化 sqlite，并把旧 config 密码列表 / 旧字典 json 迁移进 toolbox.db
    try:
        db.init_db()
        if db.migrate_legacy(cfg, db.LEGACY_DICT_FILE):
            save_config(cfg)
    except Exception:
        pass

    state = AppState(cfg)
    hub = Hub(state)
    pauser = smart_extract.PauseController(hub)

    watcher = FolderWatcher(state, hub, pauser)
    qr = QRMonitor(state, hub, pauser)
    watcher.start()
    qr.start()

    # PyQt 依赖统一在入口加载：缺失时写 crash.log 并报错（保持原崩溃日志行为）
    try:
        from PyQt5.QtWidgets import QApplication
        from PyQt5.QtCore import QTimer
        from .ui.style import STYLE
        from .ui.main_window import MainWindow, _first_run_7z_check
    except Exception:
        try:
            import traceback
            with open(paths.CRASH_LOG, "a", encoding="utf-8") as f:
                f.write(f"[{__import__('time').strftime('%Y-%m-%d %H:%M:%S')}] UI 模块导入失败:\n")
                traceback.print_exc(file=f)
                f.write("\n")
        except Exception:
            pass
        raise

    app = QApplication(sys.argv)
    app.setQuitOnLastWindowClosed(False)
    app.setStyleSheet(STYLE)
    win = MainWindow(state, hub, show_event, pauser)
    if not autostart:
        win.show()

    # 首次启动：后台检测 7-Zip（仅首次或手动「立即检查」，其他时间不检查以免阻塞）
    if not cfg.get("sevenzip_check_done", False):
        state.set("sevenzip_check_done", True)
        QTimer.singleShot(1200, lambda: _first_run_7z_check(state, hub, win))

    if not QR_AVAILABLE:
        win.log_box.appendPlainText(
            "[信息] 二维码识别功能依赖缺失（已禁用二维码，临时密码捕获不受影响）")

    sys.exit(app.exec_())


if __name__ == "__main__":
    main()