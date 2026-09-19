# -*- coding: utf-8 -*-
"""程序入口：单实例检测、Qt 插件路径注入、后台线程启动、GUI 组装。

职责：- 在导入 PyQt5 前注入 Qt 平台插件路径（修复 venv 下 no Qt platform plugin）
- 在构造 QApplication 前开启 Qt 高 DPI 缩放（分辨率 + 系统「文本大小」百分比自适应）
- 单实例检测（命名事件 + --force 强制新开）、crash.log 与 faulthandler 安装
- 初始化日志/数据库/配置，启动 FolderWatcher 与 QRMonitor 后台线程
- 组装 QApplication 与 MainWindow，处理首次启动的 7-Zip 检测
关键入口：main()
依赖：paths / extract / trail / db / config / state / hub / monitors / ui（PyQt5）
注意：仅支持 Windows；PyQt5 必须在 Qt 插件路径注入之后才可导入
"""
import os
import sys

from . import paths
from . import extract as smart_extract
from . import trail as deletion_trail
from . import db
from .config import get_int, load_config, save_config
from .utils import _install_crash_log
from .state import AppState
from .hub import Hub, install_stdout_capture
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


def _setup_high_dpi():
    """在构造 QApplication 前开启 Qt 高 DPI 缩放（适配分辨率与系统文本缩放百分比）。

    必须在**任何 Qt 对象（QApplication/QCoreApplication）创建之前**调用才生效：
    属性与取整策略都在 QGuiApplication 构造时被读取一次。分两层：
    1. 环境变量兜底（冻结/无 manifest 的 pythonw 场景，用户已设置则不覆盖）：
       只设 QT_ENABLE_HIGHDPI_SCALING=1。**不设** QT_AUTO_SCREEN_SCALE_FACTOR=0
       ——实测 Qt 5.15.2 下它反而会关闭系统缩放（125% 机器上 dpr 掉回 1.0），
       与目标相反。
    2. Qt 属性 + 取整策略，逐项 getattr 探测，任何一个不存在都跳过（兼容旧版）：
       AA_EnableHighDpiScaling / AA_UseHighDpiPixmaps（Qt ≥ 5.6）；
       HighDpiScaleFactorRoundingPolicy.PassThrough（Qt ≥ 5.14）——否则
       125%/150%/175% 会被取整成 100%/200%，窗口与字号全部错位。
    异常全部吞掉：宁可退回系统位图拉伸，也绝不因缩放设置挡启动。
    """
    os.environ.setdefault("QT_ENABLE_HIGHDPI_SCALING", "1")
    try:
        from PyQt5.QtCore import Qt, QCoreApplication
    except Exception:
        return
    for name in ("AA_EnableHighDpiScaling", "AA_UseHighDpiPixmaps"):
        try:
            attr = getattr(Qt, name, None)
            if attr is not None:
                QCoreApplication.setAttribute(attr, True)
        except Exception:
            pass
    try:
        from PyQt5.QtGui import QGuiApplication
        policy = getattr(Qt, "HighDpiScaleFactorRoundingPolicy", None)
        setter = getattr(QGuiApplication, "setHighDpiScaleFactorRoundingPolicy", None)
        if policy is not None and setter is not None:
            setter(policy.PassThrough)
    except Exception:
        pass


def main():
    if sys.platform != "win32":
        print("此程序仅支持 Windows")
        return 1

    # HiDPI 属性/策略必须最先设置：QApplication 构造时读取一次，之后设置无效。
    # （_ensure_qt_platform_plugins 的兜底分支可能构造 QCoreApplication，
    # 所以本调用必须排在它前面。）
    _setup_high_dpi()

    # Qt 平台插件路径必须在任何 PyQt5 导入（含 ui.password_book/main_window
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

    # 任务历史按配置收敛（只删终态、保留最新 N 条；失败绝不影响启动）。
    # cfg 为 load_config() 产物：task_history_limit 已被 _sanitize_cfg 钳为
    # [1, 100000] 的 int，这里用同一口径读取（等价于旧 int(... or 500)）。
    try:
        db.prune_tasks(get_int(cfg, "task_history_limit", 500, 1, 100000))
    except Exception:
        pass

    state = AppState(cfg)
    hub = Hub(state)
    pauser = smart_extract.PauseController(hub)

    # pythonw（无控制台）下，让 extract.py 的用户可见 print 进入 GUI 日志框。
    # 在进程入口幂等安装一次，而不是在工作线程里改进程级全局 stdout。
    install_stdout_capture(hub)

    watcher = FolderWatcher(state, hub, pauser)
    qr = QRMonitor(state, hub, pauser)
    # 暴露给 UI：拖入二维码图片的入口需要实例调用 feed_image_file（见
    # MainWindow._handle_drop_file）。缺失时 UI 侧 getattr 回落为「跳过并记日志」。
    hub.qr_monitor = qr
    watcher.start()
    qr.start()

    # 实验性功能（默认关）：后台只读探测百度网盘本地任务库，写日志供验证；
    # 并轮询活动下载任务（仅在开关打开时）。只读、短连接、异常全部吞掉。
    try:
        from .baidu_task import probe_and_log, start_active_watcher
        probe_and_log(state, hub)
        start_active_watcher(state, hub)
    except Exception:
        pass

    # PyQt 依赖统一在入口加载：缺失时写 crash.log 并报错（保持原崩溃日志行为）
    try:
        from PyQt5.QtWidgets import QApplication
        from PyQt5.QtCore import QTimer
        from .ui import style as ui_style
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

    # 主题：启动**不做任何系统检测**（零启动开销），先用「上次记住的主题」出首屏；
    # 窗口显示后再检测纠正一次；之后靠 WM_SETTINGCHANGE 跟随系统切换（零轮询）。
    _pref = str(cfg.get("ui_theme", "auto") or "auto").lower()
    _cached = str(cfg.get("ui_theme_cached") or "").lower()
    if _pref in ui_style.THEMES:
        _theme0 = _pref
    else:
        _theme0 = _cached if _cached in ui_style.THEMES else ui_style.DEFAULT_THEME
    ui_style.apply_theme(app, _theme0)

    win = MainWindow(state, hub, show_event, pauser)
    if not autostart:
        win.show()

    def _sync_theme_after_show():
        """显示后再纠正主题：仅当解析结果与当前不同才切；结果记进配置供下次零检测启动。"""
        try:
            pref = str(state.snapshot().get("ui_theme", "auto") or "auto").lower()
            want = ui_style.resolve_theme(pref)
            if want != ui_style.current_theme():
                ui_style.apply_theme(app, want)
                win.on_theme_changed(want)
            if (state.snapshot().get("ui_theme_cached") or "") != want:
                state.set("ui_theme_cached", want)
        except Exception:
            pass

    QTimer.singleShot(900, _sync_theme_after_show)

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