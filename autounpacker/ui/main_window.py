# -*- coding: utf-8 -*-
"""主窗口 MainWindow：监听卡片管理、托盘、日志、暂停/恢复、全局快捷键、网址信任、拖放临时解压。

职责：- 组装主界面（监听路径卡片、日志区、进度条）并定时消费 Hub 队列
- 托盘图标与最小化到托盘、单实例事件响应、Esc/Ctrl+W 关闭行为
- 全局热键注册（RegisterHotKey + 原生事件过滤）、网址信任确认弹窗调度
- 拖入文件临时解压、首次启动 7-Zip 检测
关键入口：MainWindow / _first_run_7z_check()
依赖：PyQt5、hub、state、extract、dialogs、widgets、password_book、trust
注意：stdout 捕获与 Qt 插件路径由 app.main 统一处理，本模块不重复安装
"""
import html
import queue
import threading
import time
import types

from PyQt5.QtWidgets import (QApplication, QMainWindow, QWidget, QVBoxLayout, QHBoxLayout, QLabel, QPushButton, QScrollArea, QPlainTextEdit, QSystemTrayIcon, QMenu, QSplitter, QProgressBar, QShortcut, QMessageBox)
from PyQt5.QtCore import Qt, QTimer
from PyQt5.QtGui import QKeySequence

from .. import extract as smart_extract    # noqa: F401
from .. import trail as deletion_trail     # noqa: F401
from .. import sevenzip as sevenzip_manager  # noqa: F401
from .. import baidu_manifest as bm
from ..config import (parse_hotkey, HOTKEY_ID, HOTKEY_ID_SHARE,
                      HOTKEY_ID_SHARE_CODE, MOD_NOREPEAT)
from ..trust import add_trust_entry
from ..password_book import PasswordBookDialog
from .widgets import (WatchCard, RainbowBorderButton, make_tray_icon,
                      _HotkeyFilter)
from . import style as ui_style
from .style import PALETTE
from .dialogs import (SettingsDialog, DeleteTrailDialog, SevenZipSetupDialog,
                      CloseActionDialog, TrustAskDialog)

try:
    import win32api
    import win32con
    import win32gui
    import winerror
except ImportError:
    win32api = win32con = win32gui = winerror = None

# d3：挑选文件期间可能耗时很久，而分享会话的 sekey 寿命未知——提交前若已超过该
# 秒数（从 prepare 起算），先重新 prepare 并按 fs_id 重映射选择，再提交。
SHARE_PREP_STALE_SEC = 120
# 挑选窗最长等待秒数：超时未选择按「取消」处理，避免忙标志被永久占用。
SHARE_PICK_WAIT_SEC = 1800

class MainWindow(QMainWindow):
    def __init__(self, state, hub, show_event=None, pauser=None):
        super().__init__()
        self.state = state
        self.hub = hub
        self.show_event = show_event
        self.pauser = pauser
        self.setWindowTitle("AutoUnpacker")
        self.resize(760, 640)
        self.setWindowIcon(make_tray_icon())
        self.setAcceptDrops(True)   # 支持拖入文件临时解压
        self._build_ui()
        self._drain_timer = QTimer(self)
        self._drain_timer.timeout.connect(self._drain)
        self._drain_timer.start(200)
        self.rebuild_cards()
        self._setup_tray()
        if self.show_event is not None:
            self._show_check = QTimer(self)
            self._show_check.timeout.connect(self._check_show_request)
            self._show_check.start(400)
        app = QApplication.instance()
        if app is not None:
            self._hotkey_filter = _HotkeyFilter(
                self._show_window, self._on_system_theme_changed,
                self._on_share_hotkey,
                on_hotkey_share_code=self._on_share_code_hotkey)
            app.installNativeEventFilter(self._hotkey_filter)
        # 全局快捷键：**等窗口显示后再注册**。在 __init__ 里立刻注册时，winId()
        # 拿到的原生窗口句柄可能尚未“坐实”，偶发 RegisterHotKey 失败(1400 无效句柄)；
        # 延后注册 + 失败重试可彻底消除这个启动偶发。
        QTimer.singleShot(600, self._register_hotkey)
        # 主界面快捷键：Esc / Ctrl+W 触发关闭（走 close_action 逻辑：
        # 询问弹窗 / 隐藏到托盘 / 关闭程序）。仅主界面激活时生效，
        # 模态对话框（设置/密码本等）打开时不干扰。
        self._esc_sc = QShortcut(QKeySequence("Esc"), self)
        self._esc_sc.activated.connect(self.close)
        self._cw_sc = QShortcut(QKeySequence("Ctrl+W"), self)
        self._cw_sc.activated.connect(self.close)
        # 网址信任：挂起的询问请求 + 当前打开的确认弹窗（防叠加）
        self._pending_trust = []
        self._trust_dlg = None
        # 分享「拉起」：同一时刻只允许一个后台拉起任务（防重复），
        # 自动/手动两条路径共用该忙标志
        self._share_invoke_busy = False
        # 无登录态实验链路：本进程首次真正自动拉起时提醒一次（手动路径绝不提醒）
        self._share_nologin_warned = False
        # d7：同一分享本次运行重复拉起需征得用户同意。进程内兜底集合：
        # baidu_manifest 计数器不可用（或测试桩）时保证「只在首见自动拉起」。
        self._share_launched_surls = set()
        # d3：当前打开的「提取码询问」/「文件挑选」面板（防叠加）。后台 worker 只
        # 通过 hub 队列请求，控件一律在 Qt 线程构造。
        self._share_ask_dlg = None
        self._share_pick_dlg = None

    def _build_ui(self):
        central = QWidget()
        self.setCentralWidget(central)
        root = QVBoxLayout(central)
        root.setContentsMargins(16, 14, 16, 14)
        root.setSpacing(12)

        title = QLabel("AutoUnpacker")
        title.setObjectName("appTitle")
        root.addWidget(title)

        top = QHBoxLayout()
        self.add_btn = RainbowBorderButton("＋ 添加监听路径")
        self.add_btn.clicked.connect(self._add_path)
        pw_btn = QPushButton("密码本")
        pw_btn.setObjectName("primary")
        pw_btn.clicked.connect(self._open_password_book)
        trail_btn = QPushButton("删除回溯")
        trail_btn.clicked.connect(self._open_delete_trail)
        settings_btn = QPushButton("设置")
        settings_btn.setObjectName("primary")
        settings_btn.clicked.connect(self._open_settings)
        baidu_btn = QPushButton("网盘下载目录")
        baidu_btn.setToolTip(
            "从百度网盘本地任务库识别下载目录并加入监听（需先在设置开启实验性功能）")
        baidu_btn.clicked.connect(self._add_baidu_download_dir)
        top.addWidget(self.add_btn)
        top.addWidget(baidu_btn)
        top.addWidget(pw_btn)
        top.addWidget(trail_btn)
        top.addWidget(settings_btn)
        top.addStretch(1)
        root.addLayout(top)

        sec = QLabel("监听路径")
        sec.setObjectName("sectionTitle")
        root.addWidget(sec)

        self.scroll = QScrollArea()
        self.scroll.setWidgetResizable(True)
        self.card_host = QWidget()
        self.card_lay = QVBoxLayout(self.card_host)
        self.card_lay.setContentsMargins(2, 2, 6, 2)
        self.card_lay.setSpacing(10)
        self.card_lay.addStretch(1)
        self.scroll.setWidget(self.card_host)

        # 监听路径区与日志区之间用可拖拽分隔条连接：
        # 监听路径区有最小/最大高度，超出最大值后窗口增高只会让日志区变高
        self.scroll.setMinimumHeight(120)
        self.scroll.setMaximumHeight(420)
        self.splitter = QSplitter(Qt.Vertical)
        self.splitter.addWidget(self.scroll)

        log_wrap = QWidget()
        log_lay = QVBoxLayout(log_wrap)
        log_lay.setContentsMargins(0, 0, 0, 0)
        log_lay.setSpacing(4)
        log_lbl = QLabel("运行日志")
        log_lbl.setObjectName("sectionTitle")
        log_head = QHBoxLayout()
        log_head.setSpacing(8)
        log_head.addWidget(log_lbl)
        log_head.addStretch(1)
        # 解压进度条：待机时隐藏，有解压任务时显示在「运行日志」右侧空白处
        self.progress = QProgressBar()
        self.progress.setRange(0, 100)
        self.progress.setValue(0)
        self.progress.setFixedWidth(200)
        self.progress.setFixedHeight(8)
        self.progress.hide()
        log_head.addWidget(self.progress)
        # 暂停键：挂起正在解压的任务 + 暂停后续解压任务
        self.pause_btn = QPushButton("暂停")
        self.pause_btn.setObjectName("pause")
        self.pause_btn.setFixedWidth(52)
        self.pause_btn.clicked.connect(self._toggle_pause)
        log_head.addWidget(self.pause_btn)
        log_lay.addLayout(log_head)
        self.log_box = QPlainTextEdit()
        self.log_box.setReadOnly(True)
        self.log_box.setMinimumHeight(60)
        log_lay.addWidget(self.log_box, 1)
        self.splitter.addWidget(log_wrap)
        self.splitter.setSizes([300, 200])
        self.splitter.setStretchFactor(0, 0)
        self.splitter.setStretchFactor(1, 1)
        root.addWidget(self.splitter, 1)

    # ---------- 拖放临时解压 ----------
    def dragEnterEvent(self, e):
        """只接受拖入的文件（含多个），目录或链接不接受。"""
        if e.mimeData().hasUrls():
            urls = e.mimeData().urls()
            if urls and any(u.isLocalFile() for u in urls):
                e.acceptProposedAction()
                return
        e.ignore()

    def dropEvent(self, e):
        """拖入一个或多个文件：每个文件在后台线程做智能解压。

        - 只处理拖入的文件本身；同目录其他文件不处理（除非是它自己的分卷兄弟）
        - 输出到文件所在目录（default_output_dir 自动建同名目录）
        - 分卷：拖入首卷（.001）正常处理；拖入非首卷（.002）提示跳过，
          等待首卷；伪装分卷名的完整包正常处理
        """
        paths = []
        for u in e.mimeData().urls():
            if u.isLocalFile():
                from pathlib import Path
                p = Path(u.toLocalFile())
                if p.is_file():
                    paths.append(p)
        if not paths:
            e.ignore()
            return
        e.acceptProposedAction()
        for p in paths:
            self._handle_drop_file(p)

    def _handle_drop_file(self, path):
        """后台线程处理单个拖入文件（不阻塞界面）。"""
        import os as _os
        from pathlib import Path
        path = Path(path)

        # 目录：不支持，跳过
        if path.is_dir():
            self.hub.log(f"拖放: 目录不可解压，跳过: {path.name}")
            return

        # 下载未完成：跳过
        if smart_extract.is_incomplete_download(path):
            self.hub.log(f"拖放: 文件未下载完成，暂不解压: {path.name}")
            return

        # 非首卷分卷：等待首卷，不单独解压
        if smart_extract.is_non_first_volume(path.name):
            self.hub.log(f"拖放: 这是非首卷分卷，请拖入首卷（如 .001）统一处理: {path.name}")
            return

        # 移动安装包等不自动解压
        if smart_extract.is_do_not_extract(path.name):
            self.hub.log(f"拖放: 移动安装包/交付物，保持原样: {path.name}")
            return

        # 非压缩包且不是分卷：跳过
        if not smart_extract.is_archive_file(path) and not smart_extract.is_volume_name(path.name):
            self.hub.log(f"拖放: 不是压缩包，跳过: {path.name}")
            return

        self.hub.notify("发现压缩包", f"拖放解压: {path.name}")

        def _run():
            try:
                try:
                    engine = smart_extract.create_engine("auto")
                except BaseException as e:
                    try:
                        engine = smart_extract.create_engine("zip")
                    except BaseException:
                        self.hub.log(f"拖放: {path.name} 无法初始化解压引擎: {e}")
                        self.hub.notify("智能解压失败", f"{path.name}\n7-Zip 不可用")
                        return
                passwords = self.state.all_passwords()
                options = {
                    "enable_nested": True,
                    "max_depth": 10,
                    "max_size_ratio": 100.0,
                    "use_dict": False,
                    "default_password": None,
                    "mode": "direct",
                }
                args = types.SimpleNamespace(
                    move_to=None,
                    delete_source=False,   # 拖放不删除源文件
                    run_script=None, script_args=[],
                    promote_to=None,
                    promote_merge=bool(self.state.snapshot().get("promote_merge", True)),
                )
                self.hub.q.put({"type": "progress_start"})
                try:
                    result = smart_extract.extract_one(
                        engine, str(path), None, passwords, options, args,
                        progress_cb=self._progress_cb, pauser=self.pauser)
                finally:
                    self.hub.q.put({"type": "progress_done"})
                if result and result["success"]:
                    msg = (f"拖放解压完成: {path.name} 穿透 "
                           f"{result['depth_reached']} 层，共 "
                           f"{len(result['extracted_files'])} 个文件")
                    self.hub.log(msg)
                    self.hub.notify("智能解压完成", msg)
                else:
                    err = (result or {}).get("error") or "未知错误"
                    self.hub.log(f"拖放解压失败: {path.name} ({err})")
                    self.hub.notify("智能解压失败", f"{path.name}\n{err}")
            except Exception as ex:
                self.hub.log(f"拖放处理出错: {path.name}: {ex}")
                self.hub.notify("智能解压出错", f"{path.name}\n{ex}")

        threading.Thread(target=_run, daemon=True).start()

    def _progress_cb(self, ratio, layer, name):
        """解压引擎进度回调 → GUI 队列（_drain 更新进度条）。ratio=None=忙碌。"""
        try:
            self.hub.q.put({"type": "progress", "ratio": ratio,
                            "layer": layer, "name": name})
        except Exception:
            pass

    def _setup_tray(self):
        self.tray = QSystemTrayIcon(make_tray_icon(), self)
        self.tray.setToolTip("AutoUnpacker")
        menu = QMenu()
        show = menu.addAction("显示主界面")
        show.triggered.connect(self._show_window)
        hide = menu.addAction("隐藏到托盘")
        hide.triggered.connect(self._hide_window)
        # 2.F「用客户端下载最近分享」：整条链路属实验性功能，未开启时整项隐藏。
        self._open_share_action = menu.addAction("用客户端打开最近分享")
        self._open_share_action.triggered.connect(self._open_recent_share)
        # d3：用分享者的「固定提取码」下载最近分享（同为实验性，未开启时整项隐藏）。
        self._open_share_code_action = menu.addAction("用固定提取码下载最近分享")
        self._open_share_code_action.triggered.connect(self._open_recent_share_with_code)
        # 菜单每次展开前刷新一次可见性（配置可能刚被改过，无需额外的变更通知）
        menu.aboutToShow.connect(self._refresh_share_menu)
        menu.addSeparator()
        quit_ = menu.addAction("退出")
        quit_.triggered.connect(self._quit)
        self.tray.setContextMenu(menu)
        self.tray.activated.connect(self._on_tray_activated)
        self.tray.show()

    def _on_tray_activated(self, reason):
        if reason == QSystemTrayIcon.DoubleClick:
            self._toggle_window()

    def _toggle_window(self):
        if self.isVisible():
            self._hide_window()
        else:
            self._show_window()

    def _show_window(self):
        self.showNormal()
        self.raise_()
        self.activateWindow()
        self._process_pending_trust()

    def _hide_window(self):
        self.hide()

    def _quit(self):
        self.tray.hide()
        QApplication.instance().quit()

    def _check_show_request(self):
        if self.show_event is None:
            return
        try:
            import win32event
            if win32event.WaitForSingleObject(self.show_event, 0) == win32event.WAIT_OBJECT_0:
                win32event.ResetEvent(self.show_event)
                self._show_window()
                snap = self.state.snapshot()
                if (hasattr(self, "tray") and snap.get("notify_enabled", True)
                        and snap.get("notify_already_running", True)):
                    self.tray.showMessage(
                        "AutoUnpacker", "程序已在运行，已打开主界面。",
                        QSystemTrayIcon.Information, 2000)
        except Exception:
            pass

    def closeEvent(self, event):
        action = self.state.snapshot().get("close_action", "ask")
        if action == "tray":
            event.ignore()
            self._hide_window()
            self._notify_trayed()
            return
        if action == "exit":
            event.accept()
            self._quit()
            return
        # 每次询问：弹二选一（关闭程序 / 隐藏到托盘）+「不再提示」勾选
        action, remember = self._ask_close_action()
        if action is None:
            # 用户取消（按弹窗 X / Esc）：中止关闭，主界面保持原样
            event.ignore()
            return
        if remember:
            self.state.set("close_action", action)
        if action == "exit":
            event.accept()
            self._quit()
        else:
            event.ignore()
            self._hide_window()
            self._notify_trayed()

    def _ask_close_action(self):
        """关闭主界面时弹窗：二选一 +「不再提示」勾选（可取消）。

        返回 (action, remember)：
        - action: "exit" 关闭程序 / "tray" 隐藏到托盘（本次立即执行）；
                  None = 用户按标题栏 X / Esc 取消，调用方应中止关闭；
        - remember: 是否勾选「不再提示」（勾选则把 action 同步进设置，
          以后关闭默认照此执行；不勾选则本次执行后下次仍询问）。"""
        return CloseActionDialog.ask(self)

    def _notify_trayed(self):
        cfg = self.state.snapshot()
        if cfg.get("notify_enabled", True) and cfg.get("notify_trayed", True):
            self.tray.showMessage(
                "AutoUnpacker", "已最小化到托盘，右键托盘图标可退出。",
                QSystemTrayIcon.Information, 2500)

    # ---------- 控制 ----------
    def _open_settings(self):
        dlg = SettingsDialog(self.state, self.hub, self,
                             on_hotkey_change=self._register_hotkey,
                             on_theme_change=self.on_theme_changed)
        dlg.exec_()

    def _add_path(self):
        cfg = self.state.snapshot()
        entry = {"path": "", "enabled": True, "output_dir": "",
                 "delete_source": False, "mode": "surface"}
        cfg["watch_paths"].append(entry)
        self.state.set("watch_paths", cfg["watch_paths"])
        self.rebuild_cards()

    def _add_baidu_download_dir(self):
        """从百度网盘本地任务库识别下载目录并加入监听（实验性功能）。"""
        if not self.state.snapshot().get("experimental_enabled", False):
            QMessageBox.information(
                self, "百度网盘下载目录",
                "该功能依赖实验性功能，请先在「设置 → 常规」开启「实验性功能」。")
            return
        root = None
        try:
            from ..baidu_task import detect_download_root
            root = detect_download_root(
                self.state.snapshot().get("baidu_task_db") or None)
        except Exception as e:
            self.hub.log(f"识别百度网盘下载目录失败: {e}")
        if root is None:
            QMessageBox.information(
                self, "百度网盘下载目录",
                "未能从百度网盘任务库识别到下载目录。\n"
                "（确认网盘客户端有下载历史，或该库路径未被改动）")
            return
        try:
            from ..utils import _norm_path_for_cfg
            norm = _norm_path_for_cfg(str(root))
            entries = list(self.state.snapshot().get("watch_paths") or [])
            if any(_norm_path_for_cfg(str(e.get("path", ""))) == norm for e in entries):
                QMessageBox.information(self, "百度网盘下载目录",
                                        f"该目录已在监听中：\n{root}")
                return
            entries.append({"path": str(root), "enabled": True,
                            "output_dir": "", "delete_source": False,
                            "mode": "baidu"})
            self.state.set("watch_paths", entries)
            self.rebuild_cards()
            self.hub.log(f"已把百度网盘下载目录加入监听: {root}")
            QMessageBox.information(self, "百度网盘下载目录",
                                    f"已添加监听路径：\n{root}")
        except Exception as e:
            self.hub.log(f"添加百度网盘下载目录失败: {e}")
            QMessageBox.warning(self, "百度网盘下载目录", f"添加失败：{e}")

    def _open_password_book(self):
        dlg = PasswordBookDialog(self.state, self)
        dlg.exec_()

    def _open_delete_trail(self):
        dlg = DeleteTrailDialog(self)
        dlg.exec_()

    def _remove_path(self, idx):
        cfg = self.state.snapshot()
        if 0 <= idx < len(cfg["watch_paths"]):
            cfg["watch_paths"].pop(idx)
        self.state.set("watch_paths", cfg["watch_paths"])
        self.rebuild_cards()

    def _update_rainbow(self):
        """彩虹引导状态机：
        - 没有任何监听路径条目时：「＋ 添加监听路径」按钮流动彩虹；
        - 有条目但该条目的监听路径尚未填写时：该卡片路径行右侧的「浏览」
          按钮流动彩虹（引导用户选目录）；
        - 各自负责的区域非空即熄灭，全部填好则所有彩虹消失。"""
        cfg = self.state.snapshot()
        entries = [w for w in cfg.get("watch_paths", []) if isinstance(w, dict)]
        self.add_btn.set_rainbow(not entries)
        for i in range(self.card_lay.count()):
            it = self.card_lay.itemAt(i)
            w = it.widget() if it else None
            if isinstance(w, WatchCard):
                w.browse_path_btn.set_rainbow(not w.path_edit.text().strip())

    def rebuild_cards(self):
        while self.card_lay.count() > 0:
            item = self.card_lay.takeAt(0)
            w = item.widget()
            if w is not None:
                w.deleteLater()
        cfg = self.state.snapshot()
        for i, entry in enumerate(cfg.get("watch_paths", [])):
            card = WatchCard(self.state, i, entry, self._remove_path,
                             on_path_change=self._update_rainbow)
            self.card_lay.insertWidget(self.card_lay.count() - 1, card)
        self._update_rainbow()

    def _append_log(self, msg):
        """按事件类型着色追加日志（可开关）。"""
        if not self.state.snapshot().get("log_colors_enabled", True):
            self.log_box.appendPlainText(msg)
            return
        m = msg
        if "失败" in m or "出错" in m or "错误" in m:
            color = PALETTE["log_error"]      # 错误：红
        elif "完成" in m or "成功" in m or "开始监听" in m:
            color = PALETTE["log_success"]    # 成功：绿
        elif ("发现压缩包" in m or "开始智能解压" in m or "已捕获临时密码" in m
              or "识别到二维码" in m or "正在打开" in m or "归位" in m
              or "翻译" in m or "网址" in m):
            color = PALETTE["log_info"]       # 信息：蓝
        elif ("分卷" in m or "下载未完成" in m or "密码" in m
              or "超时" in m or "监控" in m or "等待" in m):
            color = PALETTE["log_wait"]       # 等待/提示：黄
        else:
            color = PALETTE["log_default"]    # 默认
        self.log_box.appendHtml(f'<span style="color:{color}">{html.escape(msg)}</span>')

    # ---------- 主题（深浅色）----------
    def _on_system_theme_changed(self):
        """系统深浅色切换（WM_SETTINGCHANGE）→ 仅当偏好为 auto 时跟随。"""
        try:
            pref = str(self.state.snapshot().get("ui_theme", "auto")).lower()
            if pref != "auto":
                return
            want = ui_style.detect_system_theme()
            if want == ui_style.current_theme():
                return
            ui_style.apply_theme(QApplication.instance(), want)
            self.on_theme_changed(want)
            self.state.set("ui_theme_cached", want)
        except Exception:
            pass

    def on_theme_changed(self, theme):
        """主题已切换：重建卡片 + 重设标题栏 + 记一条日志（内联色取自 PALETTE）。"""
        try:
            self.rebuild_cards()
        except Exception:
            pass
        try:
            QTimer.singleShot(0, self._apply_titlebar)
        except Exception:
            pass
        try:
            self.hub.log(f"界面主题已切换: {theme}")
        except Exception:
            pass

    def _apply_titlebar(self):
        """深色主题时把 Windows 原生标题栏也变深。

        DWMWA_USE_IMMERSIVE_DARK_MODE：新系统属性号 20，旧版 Win10 用 19（两个都试）。
        """
        try:
            import ctypes
            from ctypes import wintypes
            dark = 1 if ui_style.current_theme() == "devtool" else 0
            hwnd = wintypes.HWND(int(self.winId()))
            v = ctypes.c_int(dark)
            dwm = ctypes.windll.dwmapi
            for attr in (20, 19):
                try:
                    if dwm.DwmSetWindowAttribute(hwnd, attr, ctypes.byref(v),
                                                 ctypes.sizeof(v)) == 0:
                        break
                except Exception:
                    continue
        except Exception:
            pass

    def showEvent(self, event):
        super().showEvent(event)
        if not getattr(self, "_titlebar_done", False):
            self._titlebar_done = True
            QTimer.singleShot(0, self._apply_titlebar)

    def _drain(self):
        while True:
            try:
                item = self.hub.q.get_nowait()
            except queue.Empty:
                break
            if item["type"] == "log":
                self._append_log(item["msg"])
            elif item["type"] == "notify":
                if hasattr(self, "tray"):
                    self.tray.showMessage(
                        item["title"], item["msg"], QSystemTrayIcon.Information, 4000)
            elif item["type"] == "progress_start":
                self.progress.setRange(0, 0)   # 忙碌模式
                self.progress.setValue(0)
                self.progress.show()
            elif item["type"] == "progress":
                if item.get("ratio") is None:
                    self.progress.setRange(0, 0)
                else:
                    self.progress.setRange(0, 100)
                    self.progress.setValue(
                        max(0, min(100, int(round(item["ratio"] * 100)))))
                self.progress.show()
                if item.get("name"):
                    self.progress.setToolTip(str(item["name"]))
            elif item["type"] == "progress_done":
                self.progress.setRange(0, 100)
                self.progress.setValue(0)
                self.progress.hide()
            elif item["type"] == "url_trust_ask":
                self._handle_trust_ask(item)
            elif item["type"] == "share_link":
                self._append_log(
                    f"[分享] 已记录: {item.get('url')}"
                    f"（托盘菜单「用客户端打开最近分享」可拉起客户端）")
                # 实验性：开启「自动拉起」时才处理（默认关）。
                # 链路含网络 IO，禁止阻塞 Qt 事件循环 → 交给后台线程。
                if (self.state.snapshot().get("experimental_enabled")
                        and self.state.snapshot().get("baidu_auto_invoke")):
                    url = item.get("url")
                    pwd = item.get("pwd") or ""
                    surl = item.get("surl") or url   # d7 去重键：surl（缺失时退回 url）
                    if not pwd:
                        # d3：空提取码绝不自动拉起。先看该分享者有无固定映射：
                        #   有 → 直接弹「询问」（不浪费一次探针）；
                        #   无 → 起后台线程先 prepare_share(url, "") 探测是否根本不需要
                        #        提取码，确需提取码才把「询问」请求投回 Qt 线程。
                        mapped = None
                        try:
                            mapped = bm.mapped_code(item.get("share_uk"))
                        except Exception:
                            mapped = None
                        if mapped:
                            self._share_ask_code(item, url, surl)
                        else:
                            self._start_share_pick(url, surl, "", manual=False,
                                                   item=item)
                    elif self._share_needs_consent(surl):
                        # d7：同一分享本次运行已拉起过 → 未经用户同意不再自动拉起
                        self._confirm_share_reinvoke(surl, url, pwd)
                    else:
                        self._start_share_pick(url, surl, pwd, manual=False)
            elif item["type"] == "share_pick":
                # 后台 worker 请求挑选文件：在 Qt 线程构造挑选窗（worker 绝不碰控件）
                self._show_share_pick(item)
            elif item["type"] == "share_ask":
                # 后台探测发现该分享需要提取码：在 Qt 线程弹询问面板（worker 绝不碰控件）
                self._share_ask_code(item.get("item") or {}, item.get("url"),
                                     item.get("surl"))

    def _refresh_share_menu(self):
        """按「实验性功能」总开关刷新分享菜单项可见性（整条 2.F 属实验性）。"""
        try:
            visible = bool(self.state.snapshot().get("experimental_enabled"))
            if hasattr(self, "_open_share_action"):
                self._open_share_action.setVisible(visible)
            if hasattr(self, "_open_share_code_action"):
                self._open_share_code_action.setVisible(visible)
        except Exception:
            pass

    def _open_recent_share(self):
        """托盘动作：把最近捕获的分享链接交给网盘客户端下载（2.F『拉起』全链路）。

        走完整分享下载令牌链路后，用 `baiduyunguanjia://evoked-download/…` 唤起
        客户端，由客户端自己完成下载（不下载、不登录、不开网页）。
        链路约 20s（含轮询），必须后台执行，否则会冻住整个界面。"""
        try:
            # 整条 2.F 属实验性功能：全局热键也可能被按下，这里必须再校验一次。
            if not self.state.snapshot().get("experimental_enabled"):
                self._append_log("实验性功能未开启，「用客户端下载分享」不可用")
                return
            from .. import baidu_task as bt
            rec = bt.last_share()
            if not rec:
                self._append_log("还没有记录到百度分享链接（复制一下分享链接即可）")
                return
            # 手动路径即用户明确同意：直接拉起，同时计数（与自动路径共用 d7 计数）
            self._bump_share_launch(rec.get("surl") or rec.get("url"))
            self._start_share_invoke(rec.get("url"), rec.get("pwd") or "", manual=True)
        except Exception as e:
            self._append_log(f"拉起客户端出错: {e}")

    def _on_share_hotkey(self):
        """全局热键：用客户端下载最近分享（等价托盘菜单那项）。"""
        self._open_recent_share()

    def _open_recent_share_with_code(self):
        """托盘动作/热键：用分享者的「固定提取码」下载最近分享（d3 显式手势）。

        与「用客户端打开最近分享」并列，但走新的「准备 →（可选）挑选 → 提交」管线；
        记录里 share_uk 没有固定映射时只记一行日志、不发起任何下载。"""
        try:
            # 整条 2.F 属实验性功能：全局热键也可能被按下，这里必须再校验一次。
            if not self.state.snapshot().get("experimental_enabled"):
                self._append_log("实验性功能未开启，「用固定提取码下载分享」不可用")
                return
            from .. import baidu_task as bt
            rec = bt.last_share()
            if not rec:
                self._append_log("还没有记录到百度分享链接（复制一下分享链接即可）")
                return
            code = None
            try:
                code = bm.mapped_code(rec.get("share_uk"))
            except Exception:
                code = None
            if not code:
                self._append_log("该分享者未配置固定提取码，无法按固定码下载")
                return
            # 计数已移入 _start_share_pick：手动路径同样计入（明确同意）
            self._start_share_pick(rec.get("url"), rec.get("surl"), code, manual=True)
        except Exception as e:
            self._append_log(f"按固定提取码下载出错: {e}")

    def _on_share_code_hotkey(self):
        """全局热键：用固定提取码下载最近分享（等价托盘菜单那项）。"""
        self._open_recent_share_with_code()

    def _start_share_invoke(self, url, pwd, manual=False):
        """统一的分享拉起入口：忙则跳过，否则起后台 daemon 线程（不阻塞界面）。

        `invoke_download` 含约 20s 轮询，**绝不能**在 UI 线程调用。"""
        if self._share_invoke_busy:
            self._append_log("[分享] 上一个拉起尚未结束，已跳过"
                             if not manual else "[分享] 上一个拉起尚未结束，请稍候")
            return
        self._share_invoke_busy = True
        threading.Thread(target=self._invoke_share_worker,
                         args=(url, pwd, manual), daemon=True).start()

    def _invoke_share_worker(self, url, pwd, manual):
        """后台线程：跑完整拉起链路（约 20s 轮询）。

        线程内**不碰 UI**：日志走线程安全的 `self.hub.log`，托盘提示走 `hub.q`
        队列（由 `_drain()` 在主线程消费）。异常一律吞掉，忙标志在 finally 复位。"""
        try:
            self.hub.log(f"[分享] {'手动' if manual else '自动'}拉起客户端下载…: {url}")
            from .. import baidu_task as bt
            ok, detail = bt.invoke_download(url, pwd=pwd)
            if ok:
                self.hub.log(f"[分享] 拉起成功: 已请求客户端下载（{detail}）")
                try:
                    self.hub.q.put({"type": "notify", "title": "用客户端下载分享",
                                    "msg": str(url)})
                except Exception:
                    pass
            else:
                self.hub.log(f"[分享] 拉起失败: {detail}")
        except Exception as e:
            self.hub.log(f"[分享] 拉取出错: {e}")
        finally:
            self._share_invoke_busy = False

    # ---------- d3：分享「准备 →（可选）挑选 → 提交」管线 ----------
    def _start_share_pick(self, url, surl, pwd, manual=False, item=None):
        """统一的分享管线入口：忙则跳过，否则起后台 daemon 线程（不阻塞界面）。

        `prepare_share` / `commit_download` / `list_share_dir` 均含网络 IO，**绝不能**
        在 UI 线程调用。与旧的 `_start_share_invoke` 共用同一个忙标志：同一时刻只允许
        一个分享管线。`item` 仅用于空提取码探测：确认需要提取码时把它回传给 Qt 线程弹
        询问面板（不传则询问面板按未知分享者处理）。

        d7 计数在**忙检查通过之后**才计入（被忙标志跳过的启动不计），询问路径最终也
        汇入本入口，故同样计入一次。"""
        if self._share_invoke_busy:
            self._append_log("[分享] 上一个拉起尚未结束，已跳过"
                             if not manual else "[分享] 上一个拉起尚未结束，请稍候")
            return
        # 一次性提醒（本进程内只发一次；手动路径绝不提醒）：自动链路不携带浏览器
        # 登录态，客户端未运行时被唤起会进入未登录状态、可能被迫重新登录。
        if not manual and not self._share_nologin_warned:
            self._share_nologin_warned = True
            self.hub.log(
                "[分享] 实验性自动拉起不携带登录态：若客户端未运行可能需重新登录")
            try:
                self.hub.q.put({
                    "type": "notify",
                    "title": "实验性自动拉起",
                    "msg": "实验性自动拉起不携带登录态：若客户端未运行可能需重新登录"})
            except Exception:
                pass
        self._share_invoke_busy = True
        self._bump_share_launch(surl)
        threading.Thread(target=self._pick_share_worker,
                         args=(url, surl, pwd, manual, item), daemon=True).start()

    def _pick_share_worker(self, url, surl, pwd, manual, item=None):
        """后台线程：跑「准备 →（可选）挑选 → 提交」管线（网络 IO，绝不阻塞界面）。

        线程内**不碰 UI**：日志走线程安全的 `self.hub.log`，托盘提示与挑选窗/询问窗
        请求都走 `hub.q`（由 `_drain()` 在 Qt 线程消费/构造）。需要挑选时本线程阻塞
        等待用户在 Qt 线程做出的选择。空提取码时先探测该分享是否根本不需要提取码，
        确需提取码才把「询问」请求投回 Qt 线程。异常一律吞掉，忙标志在 finally 复位。"""
        try:
            from .. import baidu_share as bs
            if not pwd:
                # 空提取码：先在后台线程探测是否根本不需要提取码（网络 IO）。
                self.hub.log(f"[分享] 自动探测分享是否需要提取码…: {url}")
                ok, prep = bs.prepare_share(url, "")
                if not ok:
                    self.hub.log("[分享] 该分享需要提取码，改为询问用户")
                    try:
                        self.hub.q.put({"type": "share_ask", "item": item or {},
                                        "url": url, "surl": surl})
                    except Exception as e:
                        self.hub.log(f"[分享] 无法请求提取码询问: {e}")
                    return
                self.hub.log("[分享] 该分享无需提取码，继续下载")
            else:
                self.hub.log(f"[分享] {'手动' if manual else '自动'}准备分享下载…: {url}")
                ok, prep = bs.prepare_share(url, pwd)
                if not ok:
                    self.hub.log(f"[分享] 准备失败: {prep}")
                    self._share_notify("分享下载失败", f"{url}\n{prep}")
                    return
            prep_ts = time.time()
            if self._share_pick_wanted(prep.get("share_uk")):
                # 需要挑选：把 prep 交给 Qt 线程弹挑选窗，阻塞等待用户选择。
                req = {"type": "share_pick", "prep": prep, "url": url,
                       "pwd": pwd, "surl": surl, "ts": prep_ts,
                       "event": threading.Event(), "pairs": None,
                       "cancelled": True, "answered": False}
                self.hub.log("[分享] 该分享者需要挑选文件，等待用户选择…")
                try:
                    self.hub.q.put(req)
                except Exception as e:
                    self.hub.log(f"[分享] 无法请求挑选窗: {e}")
                    return
                req["event"].wait(SHARE_PICK_WAIT_SEC)
                if req.get("cancelled"):
                    self.hub.log("[分享] 未选择任何文件，已取消本次下载")
                    return
                pairs = req.get("pairs") or []
                # 陈旧检测：挑选可能耗时很久，sekey 寿命未知 → 提交前重新 prepare，
                # 并按 fs_id 把选择重映射到新 prep 的条目上。
                try:
                    if time.time() - prep_ts > SHARE_PREP_STALE_SEC:
                        self.hub.log("[分享] 挑选耗时较长，提交前重新准备分享…")
                        ok2, prep = bs.prepare_share(url, pwd)
                        if not ok2:
                            self.hub.log(f"[分享] 重新准备失败: {prep}")
                            self._share_notify("分享下载失败", f"{url}\n{prep}")
                            return
                        pairs = self._remap_share_pairs(pairs, prep)
                        if not pairs:
                            self.hub.log(
                                "[分享] 重新准备后已选文件全部失效，已取消本次下载")
                            self._share_notify(
                                "分享下载失败",
                                f"{url}\n重新准备后已选文件全部失效，已取消")
                            return
                except Exception as e:
                    self.hub.log(f"[分享] 重新准备出错: {e}")
                    return
                ok3, detail = bs.commit_download(prep, pairs=pairs)
            else:
                ok3, detail = bs.commit_download(prep, None)
            if ok3:
                self.hub.log(f"[分享] 提交成功: 已请求客户端下载（{detail}）")
                self._share_notify("分享下载", str(url))
            else:
                self.hub.log(f"[分享] 提交失败: {detail}")
                self._share_notify("分享下载失败", f"{url}\n{detail}")
        except Exception as e:
            self.hub.log(f"[分享] 准备出错: {e}")
        finally:
            self._share_invoke_busy = False

    def _share_pick_flag(self, share_uk):
        """读该分享者的「需要挑选」标记：AppState 优先，其次 db；不可用按 0（整包）。

        `find_share_entry` 是并行新增接口，缺失（未落地 / 旧实现 / 查询失败）时一律
        按「不需要挑选」处理，绝不因此中断下载。"""
        try:
            ent = None
            state = getattr(self, "state", None)
            if state is not None and hasattr(state, "find_share_entry"):
                ent = state.find_share_entry(share_uk)
            if ent is None:
                from .. import db as _db
                ent = _db.find_share_entry(share_uk)
            if isinstance(ent, dict):
                return ent.get("pick")
        except Exception:
            pass
        return 0

    def _share_pick_wanted(self, share_uk):
        """是否需要弹文件挑选窗：分享者标记 pick，或全局「分享前总是挑选」开关。

        全局开关 `baidu_pick_before_download`（默认关，实验性）读取失败/缺失一律按
        关闭处理，绝不因此中断下载。返回布尔值。"""
        try:
            if self.state.snapshot().get("baidu_pick_before_download", False):
                return True
        except Exception:
            pass
        return bool(self._share_pick_flag(share_uk))

    def _share_notify(self, title, msg):
        """托盘气泡（线程安全）：只向 hub 队列投递，由 `_drain()` 在 Qt 线程消费。"""
        try:
            self.hub.q.put({"type": "notify", "title": title, "msg": msg})
        except Exception:
            pass

    def _show_share_pick(self, req):
        """Qt 线程：按 worker 准备好的 prep 弹出文件挑选窗（惰性导入）。

        `on_expand` 交给挑选窗按需调用（由挑选窗自行后台执行），`on_commit` 回传用户
        选择并唤醒等待中的 worker；真正的提交仍在 worker 线程完成。隐藏到托盘时绝不
        弹窗（非置顶窗弹在托盘里没有意义，且会把 worker 卡满等待），直接按取消唤醒并
        投递一条托盘提示。挑选组件缺失时退化为整包提交，不让用户干等、也不改变既有
        行为。`finished`（接受/取消/×/Esc 都会触发）兜底唤醒，避免用户关闭挑选窗后
        worker 一直阻塞。"""
        if not self.isVisible():
            self._abort_share_pick(req, "需要你选择文件：请打开主界面后重试")
            self._share_notify("分享需要选择文件",
                               "该分享需要你选择文件，请打开主界面后重试。\n"
                               + str(req.get("url") or ""))
            return
        try:
            from .. import baidu_share as bs
            from .share_files import ShareFilesDialog
        except Exception:
            self.hub.log("[分享] 缺少文件挑选组件，改为整包提交")
            req["pairs"] = None
            req["cancelled"] = False
            self._wake_share_pick(req)
            return
        prep = req.get("prep") or {}
        entries = prep.get("entries") or []
        try:
            dlg = ShareFilesDialog(
                self, entries,
                (lambda path: bs.list_share_dir(prep, path)),
                (lambda pairs: self._on_share_pick_commit(req, pairs)),
                title="选择要下载的文件",
                subtitle=req.get("url") or "")
        except TypeError:
            # 兼容旧签名（无 title/subtitle 关键字）
            try:
                dlg = ShareFilesDialog(
                    self, entries,
                    (lambda path: bs.list_share_dir(prep, path)),
                    (lambda pairs: self._on_share_pick_commit(req, pairs)))
            except Exception as e:
                self._abort_share_pick(req, f"挑选窗创建失败: {e}")
                return
        except Exception as e:
            self._abort_share_pick(req, f"挑选窗创建失败: {e}")
            return
        self._share_pick_dlg = dlg
        try:
            # QDialog.finished 覆盖「下载选中 / 取消 / × / Esc」所有关闭方式；
            # 未提交就关闭时按取消唤醒 worker，已提交时只做收尾、不覆盖选择。
            dlg.finished.connect(lambda _code, r=req: self._on_share_pick_closed(r))
        except Exception:
            pass
        try:
            dlg.show()
        except Exception:
            self._share_pick_dlg = None
            self._abort_share_pick(req, "挑选窗无法显示")

    def _on_share_pick_commit(self, req, pairs):
        """挑选窗提交回调（Qt 线程）：记录选择并唤醒 worker（提交仍在 worker 线程）。"""
        self._share_pick_dlg = None
        try:
            req["answered"] = True
            req["pairs"] = list(pairs or [])
            req["cancelled"] = not req["pairs"]
        except Exception:
            req["answered"] = True
            req["pairs"] = None
            req["cancelled"] = True
        self._wake_share_pick(req)

    def _abort_share_pick(self, req, reason):
        """挑选窗不可用：记一行日志并唤醒 worker 按「取消」处理。"""
        try:
            self.hub.log(f"[分享] {reason}")
        except Exception:
            pass
        try:
            req["answered"] = True
        except Exception:
            pass
        req["pairs"] = None
        req["cancelled"] = True
        self._wake_share_pick(req)

    def _wake_share_pick(self, req):
        """唤醒等待挑选结果的 worker（只对 threading.Event 置位，异常一律吞掉）。"""
        ev = req.get("event")
        if ev is not None:
            try:
                ev.set()
            except Exception:
                pass

    def _on_share_pick_closed(self, req):
        """挑选窗关闭（QDialog.finished）：未提交时按取消唤醒 worker。

        finished 覆盖「下载选中 / 取消 / × / Esc」全部关闭方式。用户未点「下载选中」
        就关闭时 worker 仍在等待，必须按取消唤醒，否则会把忙标志占满整个等待窗口；
        已提交（req["answered"]，由提交回调置位并已清空面板指针）时只返回，绝不覆盖
        已提交的选择、也不在真实提交之后再动面板指针。"""
        try:
            if req.get("answered"):
                return
            self._share_pick_dlg = None
            self._abort_share_pick(req, "挑选窗已关闭")
        except Exception:
            pass

    def _remap_share_pairs(self, pairs, prep):
        """把挑选结果 (fs_id, path) 按 fs_id 重映射到新 prep 的根清单条目上。

        重新 prepare 后根层条目的 path 可能变化；根清单里查不到 fs_id 的选择
        （含无法核实是否仍存在的嵌套条目）一律**丢弃**并计数，避免提交已消失的
        条目。全部被丢弃时返回空列表，由调用方中止本次下载。记一行日志说明丢弃
        了多少个。"""
        try:
            index = {}
            for e in (prep.get("entries") or []):
                if isinstance(e, dict) and e.get("fs_id") is not None:
                    index[str(e.get("fs_id"))] = e.get("path")
            out = []
            dropped = 0
            for it in (pairs or []):
                try:
                    fid, pth = it[0], it[1]
                except Exception:
                    dropped += 1
                    continue
                new_path = index.get(str(fid))
                if new_path:
                    out.append((fid, new_path))
                else:
                    dropped += 1
            if dropped:
                self.hub.log(f"[分享] 重新准备后有 {dropped} 个已选条目未在根清单中，"
                             f"已丢弃")
            return out
        except Exception:
            return pairs

    # ---------- d3：空提取码处理（绝不自动拉起） ----------
    def _share_ask_code(self, item, url, surl):
        """空提取码处理（Qt 线程）：可见则弹询问面板；隐藏则只提示，绝不自动拉起。

        未知分享者（has_map=False）额外用浏览器打开分享页做「探针」，方便用户查看；
        有固定映射（has_map=True）只弹询问、不开浏览器。任何异常都不向外抛。"""
        try:
            share_uk = item.get("share_uk")
            has_map = bool(item.get("has_map"))
            if not self.isVisible():
                # 托盘应用常驻后台：隐藏时不弹任何窗口，只记日志 + 托盘气泡提醒，
                # 等用户主动打开主界面或用托盘菜单/热键走「固定提取码」入口。
                self._append_log("[分享] 该分享缺少提取码，暂不自动下载")
                try:
                    self.hub.q.put({
                        "type": "notify", "title": "分享缺少提取码",
                        "msg": "该分享缺少提取码，可打开主界面用托盘菜单或"
                               "「用固定提取码下载最近分享」处理。\n" + str(url)})
                except Exception:
                    pass
                return
            if getattr(self, "_share_ask_dlg", None) is not None:
                self._append_log("[分享] 提取码询问已打开，本次分享不再重复弹出")
                return
            if not has_map:
                # 未知分享者没有可预填的固定码：顺手用浏览器打开分享页，便于查看
                try:
                    import webbrowser
                    webbrowser.open(str(url), new=2)
                    self._append_log(f"[分享] 未知分享者，已在浏览器打开分享页: {url}")
                except Exception as e:
                    self._append_log(f"[分享] 打开分享页失败: {e}")
            try:
                from .dialogs import ShareCodeAskDialog
            except Exception:
                self._append_log("[分享] 缺少提取码询问组件，已跳过")
                return
            mapped = None
            try:
                mapped = bm.mapped_code(share_uk)
            except Exception:
                mapped = None
            try:
                dlg = ShareCodeAskDialog(parent=self, surl=surl, url=url,
                                         share_uk=share_uk, mapped_code=mapped)
            except Exception as e:
                self._append_log(f"[分享] 打开提取码询问失败: {e}")
                return
            dlg.on_decision = lambda kind, code: self._on_share_code_decision(
                kind, code, url, surl, share_uk)
            self._share_ask_dlg = dlg
            try:
                dlg.show()
            except Exception:
                self._share_ask_dlg = None
        except Exception as e:
            self._append_log(f"[分享] 处理缺少提取码的分享出错: {e}")

    def _on_share_code_decision(self, kind, code, url, surl, share_uk):
        """提取码询问回调（Qt 线程）：mapped=先落库再拉起；once=只本次；ignore=忽略。"""
        try:
            self._share_ask_dlg = None
            if kind == "ignore" or not code:
                self._append_log(f"[分享] 已忽略缺少提取码的分享: {surl}")
                return
            if kind == "mapped":
                self._persist_share_code(share_uk, code)
            self._start_share_pick(url, surl, code, manual=False)
        except Exception as e:
            self._append_log(f"[分享] 处理提取码选择出错: {e}")

    def _persist_share_code(self, share_uk, code):
        """把用户选择的固定提取码写入映射（pick=0）；旧签名不支持 pick 时退回 3 参。

        持久化可能因未识别到分享者（share_uk 为空）等原因返回假值；此时明确告知
        用户未能保存，绝不谎报「已保存」。"""
        note = "自动加入(d3面板)"
        try:
            saved = self.state.add_share_code(share_uk, code, note, 0)
        except TypeError:
            try:
                saved = self.state.add_share_code(share_uk, code, note)
            except Exception as e:
                self._append_log(f"[分享] 保存固定提取码失败: {e}")
                return
        except Exception as e:
            self._append_log(f"[分享] 保存固定提取码失败: {e}")
            return
        if not saved:
            self._append_log("[分享] 未识别到分享者，固定提取码未能保存，仅本次生效")

    # ---------- 分享重复拉起（d7）：同一 surl 本次运行再次拉起需用户同意 ----------
    def _share_needs_consent(self, surl):
        """该分享本次运行是否已拉起过？是 → 再次自动拉起前必须征得用户同意。

        计数优先读 baidu_manifest 的进程内计数器（跨模块可见、重启即忘）；
        计数器不可用时退回本对象的进程内集合（口径一致：只在首见自动）。"""
        key = str(surl or "")
        try:
            return int(bm.share_launch_count(key)) > 0
        except Exception:
            return key in getattr(self, "_share_launched_surls", ())

    def _bump_share_launch(self, surl):
        """记录「该分享已被拉起一次」。仅在拉起真正开始时调用（自动/同意/手动）。

        优先写 baidu_manifest 计数器；写失败不影响拉起本身，进程内集合
        始终同步更新，作为计数器不可用时的兜底。"""
        key = str(surl or "")
        try:
            bm.bump_share_launch(key)
        except Exception:
            pass
        try:
            self._share_launched_surls.add(key)
        except Exception:
            pass

    def _confirm_share_reinvoke(self, surl, url, pwd):
        """同一分享本次运行重复出现：先征得同意，用户点「是」才再次拉起（d7）。

        主界面可见：弹模态确认（默认「否」）；隐藏到托盘时不弹任何窗口
        （模态框不能凭空出现在托盘里），改为记日志 + 托盘气泡走队列提醒，
        等用户主动打开主界面后自行走托盘菜单/热键。两条路径都不自动拉起。"""
        if self.isVisible():
            ret = QMessageBox.question(
                self, "重复的分享链接",
                f"本次运行已拉起过该分享（{surl}），是否再次用客户端下载？",
                QMessageBox.Yes | QMessageBox.No, QMessageBox.No)
            if ret == QMessageBox.Yes:
                # 计数已移入 _start_share_pick：同意后同样计入
                self._start_share_pick(url, surl, pwd, manual=False)
            else:
                self._append_log(f"[分享] 用户取消了重复拉起: {surl}")
            return
        self._append_log(f"[分享] 该分享本次运行已拉起过，需确认后才会再次拉起: {surl}")
        try:
            self.hub.q.put({"type": "notify", "title": "重复的分享链接",
                            "msg": f"本次运行已拉起过该分享（{surl}），"
                                   f"如需再次下载请打开主界面确认。"})
        except Exception:
            pass

    # ---------- 网址信任：挂起队列 / 非置顶询问弹窗 / 决策回写 ----------
    def _handle_trust_ask(self, req):
        """主窗口收到待确认网址：可见则弹非置顶询问窗，隐藏则挂起+托盘提示。"""
        if self.isVisible():
            if not self._show_trust_dialog(req):
                self._pending_trust.append(req)   # 已有弹窗打开，排队等下一个
        else:
            self._pending_trust.append(req)
            snap = self.state.snapshot()
            if (snap.get("notify_enabled", True) and hasattr(self, "tray")
                    and snap.get("notify_trust_pending", True)):
                try:
                    self.tray.showMessage(
                        "网址信任确认", "有新的网址等待确认，打开主界面后处理。",
                        QSystemTrayIcon.Information, 3000)
                except Exception:
                    pass

    def _show_trust_dialog(self, req):
        """弹非置顶询问窗（不 raise/activate，不打断用户当前操作）。
        已有弹窗打开时返回 False（请求留在挂起队列）。"""
        if getattr(self, "_trust_dlg", None) is not None:
            return False
        dlg = TrustAskDialog(self, req.get("url", ""), req.get("host"),
                             req.get("category"), req.get("purpose", "open"))
        dlg.on_decision = lambda dec, r=req: self._on_trust_decision(r, dec)
        self._trust_dlg = dlg
        dlg.show()
        return True

    def _process_pending_trust(self):
        """主界面变为可见时处理挂起的信任询问（无限挂起，不丢请求）。"""
        while self._pending_trust and getattr(self, "_trust_dlg", None) is None:
            req = self._pending_trust.pop(0)
            if not self._show_trust_dialog(req):
                self._pending_trust.insert(0, req)
                break

    def _on_trust_decision(self, req, decision):
        """用户对信任询问做出选择：持久化黑白名单 + 放行或跳过。"""
        self._trust_dlg = None
        host = req.get("host") or ""
        purpose = req.get("purpose", "open") or "open"
        if decision in ("trust", "block") and host:
            try:
                key = "whitelist" if decision == "trust" else "blacklist"
                ut = add_trust_entry(self.state.snapshot(), host, key, purpose)
                self.state.set("url_trust", ut)
                kind = "已永久信任" if decision == "trust" else "已永久拒绝"
                label = "自动打开" if purpose == "open" else "下载识别"
                self.hub.log(f"{kind}[{label}]域名: {host}")
            except Exception as e:
                self.hub.log(f"信任名单保存失败: {e}")
        if decision in ("open_once", "trust"):
            # 放行：投递给 QRMonitor 执行（避免在 UI 线程做网络操作）
            try:
                self.hub.url_grant_q.put((req.get("url", ""), req.get("purpose", "open")))
            except Exception:
                pass
        self._process_pending_trust()

    # ---------- 解压暂停（唯一的总开关，涵盖原「停止监听」） ----------
    def _toggle_pause(self):
        if self.pauser is None:
            return
        pausing = not self.pauser.is_paused()
        self.pauser.set_paused(pausing)
        if pausing:
            self.pause_btn.setText("继续")
            self.pause_btn.setProperty("paused", True)
            self.hub.log("已暂停：停止监听与剪贴板监控，正在解压的任务已挂起")
        else:
            self.pause_btn.setText("暂停")
            self.pause_btn.setProperty("paused", False)
            self.hub.log("已恢复：继续监听与解压")
        self.pause_btn.style().unpolish(self.pause_btn)
        self.pause_btn.style().polish(self.pause_btn)

    # ---------- 全局快捷键 ----------
    def _register_hotkey(self, _retry=0):
        if not isinstance(_retry, int):
            _retry = 0
        self._unregister_hotkey()
        if win32gui is None:
            return
        try:
            if not self.state.snapshot().get("hotkey_enabled", True):
                return
            combo = str(self.state.snapshot().get("hotkey", "")).strip()
            if not combo or combo.lower() in ("无", "none", "null"):
                return
            parsed = parse_hotkey(combo)
            if parsed is None:
                self.hub.log(f"快捷键配置无效，未注册: {combo}")
                return
            mods, vk = parsed
            hwnd = int(self.winId())
            if not hwnd:
                return
            # pywin32 的 RegisterHotKey 成功时返回 None（不是 True），
            # 所以不依赖返回值：没有抛异常即注册成功。
            win32gui.RegisterHotKey(hwnd, HOTKEY_ID, mods | MOD_NOREPEAT, vk)
            self.hub.log(f"全局快捷键已注册: {combo}")
        except Exception as e:
            # 启动瞬间偶发失败（例如窗口句柄尚未就绪，error 1400 "无效的窗口句柄"）。
            # 稍后重试：最多 3 次，避免「偶发注册不上」让热键长期失效。
            if _retry < 3:
                self.hub.log(f"全局快捷键注册失败，稍后重试({_retry + 1}/3): {e}")
                QTimer.singleShot(1200, lambda: self._register_hotkey(_retry + 1))
            else:
                self.hub.log(f"全局快捷键注册失败: {e}")
        finally:
            # 主热键无论走哪条分支（含上面的提前 return），都顺带注册两个分享热键
            self._register_share_hotkey()
            self._register_share_code_hotkey()

    def _register_share_hotkey(self):
        """注册「用客户端下载最近分享」的全局热键（可选，默认不设置）。

        与主热键不同：分享热键是可选功能，注册失败不重试、不打扰；未启用
        全局热键、未配置（空 / 无 / none / null）时静默跳过。异常一律吞掉。"""
        if win32gui is None:
            return
        try:
            if not self.state.snapshot().get("hotkey_enabled", True):
                return
            combo = str(self.state.snapshot().get("hotkey_share", "")).strip()
            if not combo or combo.lower() in ("无", "none", "null"):
                return
            parsed = parse_hotkey(combo)
            if parsed is None:
                self.hub.log(f"分享快捷键配置无效，未注册: {combo}")
                return
            mods, vk = parsed
            hwnd = int(self.winId())
            if not hwnd:
                return
            win32gui.RegisterHotKey(hwnd, HOTKEY_ID_SHARE, mods | MOD_NOREPEAT, vk)
            self.hub.log(f"分享快捷键已注册: {combo}")
        except Exception as e:
            self.hub.log(f"分享快捷键注册失败: {e}")

    def _register_share_code_hotkey(self):
        """注册「用固定提取码下载最近分享」的全局热键（可选，默认不设置）。

        与分享热键同口径：可选功能，注册失败不重试、不打扰；未启用全局热键、
        未配置（空 / 无 / none / null）时静默跳过。异常一律吞掉。"""
        if win32gui is None:
            return
        try:
            if not self.state.snapshot().get("hotkey_enabled", True):
                return
            combo = str(self.state.snapshot().get("hotkey_share_code", "")).strip()
            if not combo or combo.lower() in ("无", "none", "null"):
                return
            parsed = parse_hotkey(combo)
            if parsed is None:
                self.hub.log(f"固定提取码快捷键配置无效，未注册: {combo}")
                return
            mods, vk = parsed
            hwnd = int(self.winId())
            if not hwnd:
                return
            win32gui.RegisterHotKey(hwnd, HOTKEY_ID_SHARE_CODE, mods | MOD_NOREPEAT, vk)
            self.hub.log(f"固定提取码快捷键已注册: {combo}")
        except Exception as e:
            self.hub.log(f"固定提取码快捷键注册失败: {e}")

    def _unregister_hotkey(self):
        if win32gui is None:
            return
        try:
            hwnd = int(self.winId())
            win32gui.UnregisterHotKey(hwnd, HOTKEY_ID)
        except Exception:
            pass
        try:
            hwnd = int(self.winId())
            win32gui.UnregisterHotKey(hwnd, HOTKEY_ID_SHARE)
        except Exception:
            pass
        try:
            hwnd = int(self.winId())
            win32gui.UnregisterHotKey(hwnd, HOTKEY_ID_SHARE_CODE)
        except Exception:
            pass


def _first_run_7z_check(state, hub, parent):
    """首次启动的 7-Zip 检查：缺失/过低时弹窗询问安装方式。

    在后台线程检测（7z 未装时纯文件系统判断，装了时一次 7z i），
    不阻塞启动；结果只在需要处理时才在主线程弹窗。"""
    def worker():
        try:
            info = sevenzip_manager.check_environment()
        except Exception as e:
            hub.log(f"首次 7-Zip 检查失败: {e}")
            return
        if info["status"] == "ok":
            return
        hub.log(f"首次启动检测: 7-Zip {info['status']}"
                + (f"（{info['version_str']}）" if info["version_str"] else ""))
        def show():
            dlg = SevenZipSetupDialog(state, hub, info, parent)
            dlg.exec_()
        QTimer.singleShot(0, show)
    threading.Thread(target=worker, daemon=True).start()


