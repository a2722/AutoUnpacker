# -*- coding: utf-8 -*-
"""主窗口 MainWindow：监听卡片管理、托盘、日志、暂停、快捷键、网址信任。"""
import html
import queue
import threading
import types

from PyQt5.QtWidgets import (QApplication, QMainWindow, QWidget, QVBoxLayout, QHBoxLayout, QLabel, QPushButton, QScrollArea, QPlainTextEdit, QSystemTrayIcon, QMenu, QSplitter, QProgressBar, QShortcut)
from PyQt5.QtCore import Qt, QTimer
from PyQt5.QtGui import QKeySequence

from .. import extract as smart_extract    # noqa: F401
from .. import trail as deletion_trail     # noqa: F401
from .. import sevenzip as sevenzip_manager  # noqa: F401
from ..config import parse_hotkey, HOTKEY_ID, MOD_NOREPEAT
from ..trust import _host_matches
from ..password_book import PasswordBookDialog
from .widgets import (WatchCard, RainbowBorderButton, make_tray_icon,
                      _HotkeyFilter)
from .dialogs import (SettingsDialog, DeleteTrailDialog, SevenZipSetupDialog,
                      CloseActionDialog, TrustAskDialog)

try:
    import win32api
    import win32con
    import win32gui
    import winerror
except ImportError:
    win32api = win32con = win32gui = winerror = None

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
            self._hotkey_filter = _HotkeyFilter(self._show_window)
            app.installNativeEventFilter(self._hotkey_filter)
        self._register_hotkey()
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
        top.addWidget(self.add_btn)
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
                if hasattr(self, "tray") and self.state.snapshot().get("notify_enabled", True):
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
        if self.state.snapshot().get("notify_enabled", True):
            self.tray.showMessage(
                "AutoUnpacker", "已最小化到托盘，右键托盘图标可退出。",
                QSystemTrayIcon.Information, 2500)

    # ---------- 控制 ----------
    def _open_settings(self):
        dlg = SettingsDialog(self.state, self.hub, self,
                             on_hotkey_change=self._register_hotkey)
        dlg.exec_()

    def _add_path(self):
        cfg = self.state.snapshot()
        entry = {"path": "", "enabled": True, "output_dir": "",
                 "delete_source": False}
        cfg["watch_paths"].append(entry)
        self.state.set("watch_paths", cfg["watch_paths"])
        self.rebuild_cards()

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
            color = "#ff8080"      # 错误：红
        elif "完成" in m or "成功" in m or "开始监听" in m:
            color = "#8be28b"      # 成功：绿
        elif ("发现压缩包" in m or "开始智能解压" in m or "已捕获临时密码" in m
              or "识别到二维码" in m or "正在打开" in m or "归位" in m
              or "翻译" in m or "网址" in m):
            color = "#7fb6ff"      # 信息：蓝
        elif ("分卷" in m or "下载未完成" in m or "密码" in m
              or "超时" in m or "监控" in m or "等待" in m):
            color = "#f2c97d"      # 等待/提示：黄
        else:
            color = "#d8e0ea"      # 默认：灰白
        self.log_box.appendHtml(f'<span style="color:{color}">{html.escape(msg)}</span>')

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

    # ---------- 网址信任：挂起队列 / 非置顶询问弹窗 / 决策回写 ----------
    def _handle_trust_ask(self, req):
        """主窗口收到待确认网址：可见则弹非置顶询问窗，隐藏则挂起+托盘提示。"""
        if self.isVisible():
            if not self._show_trust_dialog(req):
                self._pending_trust.append(req)   # 已有弹窗打开，排队等下一个
        else:
            self._pending_trust.append(req)
            if self.state.snapshot().get("notify_enabled", True) and hasattr(self, "tray"):
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
        if decision in ("trust", "block") and host:
            try:
                ut = dict(self.state.snapshot().get("url_trust") or {})
                key = "whitelist" if decision == "trust" else "blacklist"
                lst = [str(x).strip().lower() for x in (ut.get(key) or [])]
                if not any(_host_matches(e, host) for e in lst):
                    lst.append(host)
                ut[key] = lst
                self.state.set("url_trust", ut)
                kind = "已永久信任" if decision == "trust" else "已永久拒绝"
                self.hub.log(f"{kind}域名: {host}")
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
    def _register_hotkey(self):
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
            self.hub.log(f"全局快捷键注册失败: {e}")

    def _unregister_hotkey(self):
        if win32gui is None:
            return
        try:
            hwnd = int(self.winId())
            win32gui.UnregisterHotKey(hwnd, HOTKEY_ID)
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


