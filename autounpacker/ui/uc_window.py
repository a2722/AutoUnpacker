# -*- coding: utf-8 -*-
"""UC 网盘内嵌浏览器窗口（UI 层；依赖可选组件 PyQtWebEngine）。

放在 ui/ 而不是 uc/ 的原因：分层守卫要求「非 ui 模块不得顶层导入 PyQt5」，
而本模块必须顶层导入 QtWebEngine（软导入失败也要能 import）。uc/ 包因此保持
纯逻辑（detect / capability / login / handoff），核心层零 Qt 依赖。

**软依赖契约**：本模块在 import 时先用 try 包住 QtWebEngine 导入：
- 可用 → `WEB_OK = True`，正常提供 `UcBrowserWindow`
- 不可用 → `WEB_OK = False`，`UcBrowserWindow` 退化成一个「一构造就抛
  RuntimeError」的占位类。调用方应先查 `capability.web_engine_available()`，
  这样即使误导入本模块也不会把主程序带崩。

实现要点（都是踩过坑的）：
1. 用 `QWebEngineProfile.defaultProfile()`，**不新建具名 Profile** —— 具名 Profile
   在进程退出析构时会导致 0xC0000409 崩溃；默认 Profile 的生命周期由 Qt 管。
2. UA 覆盖成普通 Chrome，避免站点识别到 QtWebEngine 后降级或拦截。
3. 下载由 `downloadRequested` 拦截：拿到 URL 后 `cancel()`，再把 URL 交出去
   （交给 IDM / 默认浏览器 / 剪贴板），不让浏览器自己落盘。
4. 滑块一律由用户手动拖；本模块只负责「填号、点获取验证码、填码、点登录、点全部下载」。
"""
from PyQt5.QtCore import Qt, QTimer, QUrl, pyqtSignal  # noqa: E402
from PyQt5.QtWidgets import (QComboBox, QDialog, QHBoxLayout, QLabel,  # noqa: E402
                             QLineEdit, QPushButton, QVBoxLayout, QWidget)

from ..uc import detect, login

_WEB_IMPORT_ERROR = None
try:  # 软依赖：没装 PyQtWebEngine 时这里失败，但不影响模块可导入
    from PyQt5.QtWebEngineWidgets import QWebEngineProfile, QWebEngineView

    WEB_OK = True
except Exception as _exc:  # pragma: no cover - 只在缺组件时走
    QWebEngineProfile = None
    QWebEngineView = None
    WEB_OK = False
    _WEB_IMPORT_ERROR = _exc

CHROME_UA = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
             "(KHTML, like Gecko) Chrome/131.0.0.0 Safari/537.36")

# 登录方式（后三个是占位符，见用户要求：先只打通手机号）
_LOGIN_METHODS = (
    ("phone", "手机号登录", True),
    ("wechat", "微信扫码（暂未支持）", False),
    ("uc_scan", "UC 扫码（暂未支持）", False),
    ("password", "密码登录（暂未支持）", False),
)


if WEB_OK:

    class UcBrowserWindow(QDialog):
        """内嵌浏览器 + 登录助手 + 下载拦截。"""

        # URL, 建议文件名
        download_ready = pyqtSignal(str, str)
        # 一行状态（给主窗口写日志/状态栏）
        status = pyqtSignal(str)
        # 手机号变更（用于记住手机号）
        phone_changed = pyqtSignal(str)

        def __init__(self, parent=None, phone=""):
            super().__init__(parent)
            self.setWindowTitle("UC 网盘（实验性）")
            self.setMinimumSize(1000, 760)
            self.setWindowFlag(Qt.WindowContextHelpButtonHint, False)

            self._logged_in = False
            self._last_code = ""
            self._pending_downloads = 0

            prof = QWebEngineProfile.defaultProfile()
            try:
                prof.setHttpUserAgent(CHROME_UA)
            except Exception:
                pass

            self.view = QWebEngineView(self)
            self.view.loadFinished.connect(self._on_load_finished)
            if getattr(self.view, "page", None):
                try:
                    self.view.page().profile().downloadRequested.connect(
                        self._on_download_requested)
                except Exception:
                    pass

            self._build_ui(phone)
            self._timer = QTimer(self)
            self._timer.setInterval(2000)
            self._timer.timeout.connect(self.refresh_state)

        # ---------------- UI ----------------
        def _build_ui(self, phone):
            root = QVBoxLayout(self)
            root.setContentsMargins(10, 10, 10, 10)
            root.setSpacing(8)

            # 第一行：登录方式 + 手机号 + 验证码
            row1 = QHBoxLayout()
            row1.addWidget(QLabel("登录方式"))
            self.method_box = QComboBox()
            model = self.method_box.model()
            for i, (_key, label, enabled) in enumerate(_LOGIN_METHODS):
                self.method_box.addItem(label)
                if not enabled:
                    item = model.item(i)
                    if item is not None:
                        item.setEnabled(False)
            self.method_box.setMinimumWidth(190)
            row1.addWidget(self.method_box)

            self.phone_edit = QLineEdit(str(phone or ""))
            self.phone_edit.setPlaceholderText("手机号")
            self.phone_edit.setMaximumWidth(160)
            self.phone_edit.setMaxLength(11)
            row1.addWidget(self.phone_edit)

            self.btn_send = QPushButton("填入并获取验证码")
            self.btn_send.clicked.connect(self.do_send_code)
            row1.addWidget(self.btn_send)

            self.code_edit = QLineEdit()
            self.code_edit.setPlaceholderText("短信验证码")
            self.code_edit.setMaximumWidth(100)
            self.code_edit.setMaxLength(8)
            row1.addWidget(self.code_edit)

            self.btn_login = QPushButton("登录")
            self.btn_login.clicked.connect(self.do_login)
            row1.addWidget(self.btn_login)
            row1.addStretch(1)
            root.addLayout(row1)

            # 第二行：页面操作
            row2 = QHBoxLayout()
            self.btn_open_login = QPushButton("① 打开登录面板")
            self.btn_open_login.clicked.connect(self.do_open_login)
            row2.addWidget(self.btn_open_login)

            self.btn_download = QPushButton("② 全部下载")
            self.btn_download.clicked.connect(self.do_download_all)
            self.btn_download.setEnabled(False)
            row2.addWidget(self.btn_download)

            self.btn_reload = QPushButton("刷新页面")
            self.btn_reload.clicked.connect(self.reload)
            row2.addWidget(self.btn_reload)

            self.btn_manual = QPushButton("我来手动操作")
            self.btn_manual.setToolTip("隐藏/展开上方登录助手，直接用页面里的表单登录")
            self.btn_manual.clicked.connect(self._toggle_helper)
            row2.addWidget(self.btn_manual)

            row2.addStretch(1)
            root.addLayout(row2)

            self._helper_row = row1
            self._helper_widgets = [
                self.method_box, self.phone_edit, self.btn_send,
                self.code_edit, self.btn_login,
            ]

            root.addWidget(self.view, 1)

            self.status_label = QLabel("准备就绪")
            self.status_label.setWordWrap(True)
            root.addWidget(self.status_label)

        # ---------------- 状态 ----------------
        def _set_status(self, text):
            self.status_label.setText(text)
            try:
                self.status.emit(text)
            except Exception:
                pass

        def _run(self, js, cb):
            try:
                self.view.page().runJavaScript(js, cb)
            except Exception as e:
                self._set_status("脚本执行失败：%s" % e)

        @staticmethod
        def _parse(res):
            import json
            try:
                return json.loads(res) if isinstance(res, str) else {}
            except Exception:
                return {}

        def _on_load_finished(self, ok):
            self._set_status("页面已加载" if ok else "页面加载失败（检查网络）")
            QTimer.singleShot(1500, self.refresh_state)

        def _on_download_requested(self, item):
            """拦截浏览器下载：拿到直链后取消，交给外部下载器。"""
            try:
                url = item.url().toString()
                name = item.suggestedFileName() or ""
            except Exception:
                url, name = "", ""
            try:
                item.cancel()
            except Exception:
                pass
            if url:
                self._pending_downloads += 1
                self._set_status("已捕获下载直链（%d 个），正在交接…" % self._pending_downloads)
                try:
                    self.download_ready.emit(url, name)
                except Exception:
                    pass

        def refresh_state(self):
            def cb(res):
                st = self._parse(res)
                if not st.get("ok"):
                    self._set_status(st.get("msg") or "无法读取页面状态")
                    return
                self._logged_in = bool(
                    st.get("download_btn") and not st.get("nav_login"))
                self.btn_download.setEnabled(bool(st.get("download_btn")))
                if st.get("download_btn"):
                    self._set_status("已就绪：可以点「② 全部下载」")
                elif st.get("nav_login"):
                    self._set_status("未登录：点「① 打开登录面板」，或用上方助手登录")
                else:
                    self._set_status("页面还在加载…")
            self._run(login.js_state(), cb)

        # ---------------- 动作 ----------------
        def load_share(self, url):
            """加载分享页（一律改写成 fast.uc.cn 网页端）。

            drive.uc.cn 是「UC 客户端」引导页：它的登录面板与网页端不同，
            没有「手机登录」标签，助手点「填入并获取验证码」必然失败。
            这里统一改写成 canonical（fast 域），任何来源的链接都不会漏。
            """
            target = str(url or "").strip()
            try:
                rec = detect.parse_uc_share_url(target)
                if rec and rec.get("canonical"):
                    target = rec["canonical"]
            except Exception:
                pass
            self.view.load(QUrl(target))
            self._timer.start()

        def reload(self):
            self.view.reload()

        def do_open_login(self):
            self._run(login.js_open_login(), lambda r: self._after(self._parse(r)))

        def do_send_code(self):
            phone = self.phone_edit.text().strip()
            if not phone:
                self._set_status("请先填手机号")
                return
            try:
                self.phone_changed.emit(phone)
            except Exception:
                pass

            def step_pick(res):
                st = self._parse(res)
                if not st.get("ok"):
                    self._set_status("切换手机登录失败：%s" % st.get("msg"))
                    return

                def step_fill(r2):
                    st2 = self._parse(r2)
                    if not st2.get("ok"):
                        self._set_status("填手机号失败：%s" % st2.get("msg"))
                        return
                    self._run(login.js_send_sms(),
                              lambda r3: self._after(self._parse(r3),
                                                     extra="（若没收到短信，请手动拖动滑块后再点一次）"))
                self._run(login.js_fill_phone(phone), step_fill)

            self._run(login.js_pick_phone_tab(), step_pick)

        def do_login(self):
            code = self.code_edit.text().strip()
            if not code:
                self._set_status("请先填短信验证码")
                return

            def step_fill(res):
                st = self._parse(res)
                if not st.get("ok"):
                    self._set_status("填验证码失败：%s" % st.get("msg"))
                    return
                self._run(login.js_submit(), lambda r2: self._after(self._parse(r2)))
            self._run(login.js_fill_code(code), step_fill)

        def do_download_all(self):
            self._pending_downloads = 0
            self._run(login.js_download_all(), lambda r: self._after(self._parse(r)))
            # 「点了没反应」最常见的两种原因：① 登录已过期（页面弹回登录框）；
            # ② 按钮被禁用/页面没加载完。这里 6 秒内没捕获到下载就明确提示，
            # 免得用户以为是程序坏了。
            QTimer.singleShot(6000, self._warn_if_no_download)

        def _warn_if_no_download(self):
            if self._pending_downloads:
                return
            self._set_status(
                "没有捕获到下载请求：若页面弹出了登录框，说明登录已过期——"
                "请用上方助手重新登录后再点「② 全部下载」。")

        def _toggle_helper(self):
            visible = self.method_box.isVisible()
            for w in self._helper_widgets:
                w.setVisible(not visible)

        def _after(self, st, extra=""):
            msg = st.get("msg") or ("完成" if st.get("ok") else "操作失败")
            self._set_status(msg + (extra or ""))
            QTimer.singleShot(700, self.refresh_state)

        def closeEvent(self, ev):
            try:
                self._timer.stop()
            except Exception:
                pass
            super().closeEvent(ev)

else:  # pragma: no cover - 缺组件时的占位

    class UcBrowserWindow(QDialog):  # type: ignore[no-redef]
        """缺 PyQtWebEngine 时的占位：构造即报错，提示去装可选组件。"""

        def __init__(self, *a, **k):
            raise RuntimeError(
                "未安装可选组件 PyQtWebEngine，UC 网盘窗口不可用（%s）"
                % (_WEB_IMPORT_ERROR,))
