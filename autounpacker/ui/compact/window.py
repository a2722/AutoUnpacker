# -*- coding: utf-8 -*-
"""CompactWindow：精简模式（方案 E）的独立顶层小窗——所有事都在这个窗内办完。

窗口（规格 §3）：`QWidget` + `Qt.Window | FramelessWindowHint`，**保留任务栏按钮**
（绝不用 `Qt.Tool`）；默认 360×360、最小 320×300、最大 460×600；32px 自绘标题栏
（左=图标+`AutoUnpacker`；中=状态 `● 运行中` / `队列 N`；右=`‹` `›` `⤢` `—` `✕`）。

导航（规格 §4.2）：QStackedWidget 三页 + 浏览器式历史；`‹`/`›` 与 `Alt+←`/`Alt+→`；
`Esc` 按页处理（CODE=忽略回 HOME、PW=取消回 HOME、HOME=与主界面一致：走 close_action）。

红线：不弹任何子窗（CODE / PW 都在本窗换页，规格 §0 D2）；不做「同时存入永久口令本」
（D4）；不加日志/设置/目录入口，只留一个 `⤢` 返回完整界面（D5）。

宿主冻结契约（不得改）：
    host.state / host.hub /
    host._handle_drop_file(path) /
    host._on_share_code_decision(kind, code, url, surl, share_uk) /
    host._toggle_compact(False)
"""
import os
import time
import webbrowser
from pathlib import Path

from PyQt5.QtCore import (QByteArray, QEvent, QRect, Qt, QTimer)
from PyQt5.QtGui import QKeySequence
from PyQt5.QtWidgets import (QApplication, QFileDialog, QHBoxLayout, QLabel,
                             QMenu, QPushButton, QShortcut, QStackedWidget,
                             QVBoxLayout, QWidget)

from ... import db
from .. import style as ui_style
from ..style import PALETTE
from ..widgets import make_tray_icon
from ..widgets.inputs import Glyph, _ElideLabel
from .nav import NavHistory
from .pages_code import CodePage
from .pages_home import HomePage
from .pages_pw import PwPage

_WIN_W, _WIN_H = 360, 360          # 默认尺寸（规格 §3）
_MIN_W, _MIN_H = 320, 300          # 最小尺寸
_MAX_W, _MAX_H = 460, 600          # 最大尺寸
_EDGE = 5                          # 无边框窗的边缘缩放热区（px）
_TITLE_H = 32                      # 自绘标题栏高
_DEFAULT_MARGIN = 24               # 首次 / 越界回落：主屏右下角离边 24px
_PAGE_KEYS = ("HOME", "CODE", "PW", "PICK")
_TASKS_IDLE_TTL = 2.0              # 无宿主事件钩子：空闲最短重查间隔（秒）
_TASKS_IDLE_TTL_HOOKED = 10.0      # 已接宿主事件：仅作安全网（事件本就会立即刷新）


class _TitleBar(QWidget):
    """32px 自绘标题栏：按住拖动整窗；右键弹「窗口置顶」勾选（规格 §3）。"""

    def __init__(self, window):
        super().__init__(window)
        self._win = window
        self.setObjectName("compactTitleBar")
        self.setAttribute(Qt.WA_StyledBackground, True)
        self.setFixedHeight(_TITLE_H)
        self._drag = None

    def mousePressEvent(self, event):
        if event.button() == Qt.LeftButton:
            self._drag = event.globalPos() - self._win.frameGeometry().topLeft()
            event.accept()
            return
        super().mousePressEvent(event)

    def mouseMoveEvent(self, event):
        if self._drag is not None and (event.buttons() & Qt.LeftButton):
            try:
                self._win.move(event.globalPos() - self._drag)
            except Exception:
                pass
            event.accept()
            return
        super().mouseMoveEvent(event)

    def mouseReleaseEvent(self, event):
        if self._drag is not None:
            self._drag = None
            event.accept()
            return
        super().mouseReleaseEvent(event)

    def contextMenuEvent(self, event):
        """右键标题栏：勾选「窗口置顶」（勾选状态持久化到 compact_on_top）。"""
        try:
            menu = QMenu(self)
            act = menu.addAction("窗口置顶")
            act.setCheckable(True)
            act.setChecked(self._win.on_top())
            act.toggled.connect(self._win.set_on_top)
            menu.exec_(event.globalPos())
        except Exception:
            pass


class CompactWindow(QWidget):
    """精简界面唯一入口：三页 + 历史导航 + 拖放 + 置顶 / 几何记忆。"""

    def __init__(self, host, parent=None):
        super().__init__(parent)
        self._host = host
        self._nav = NavHistory("HOME")
        self._on_top = False
        self._resize_ctx = None
        self._geo_ready = False
        self._qss_cache = ""

        # ---- 窗口形态：顶层 + 无边框 + 保留任务栏按钮（绝不用 Qt.Tool）----
        self.setObjectName("compactWindow")
        self.setWindowFlags(Qt.Window | Qt.FramelessWindowHint)
        self.setAttribute(Qt.WA_StyledBackground, True)
        self.setWindowTitle("AutoUnpacker")
        try:
            self.setWindowIcon(make_tray_icon())
        except Exception:
            pass
        self.setMinimumSize(_MIN_W, _MIN_H)
        self.setMaximumSize(_MAX_W, _MAX_H)
        self.resize(_WIN_W, _WIN_H)
        self.setAcceptDrops(True)          # 整窗接收拖放（规格 §14.3 默认）

        # ---- 版面：标题栏 + 页面栈 ----
        root = QVBoxLayout(self)
        root.setContentsMargins(0, 0, 0, 0)
        root.setSpacing(0)
        root.addWidget(self._build_titlebar())

        self.stack = QStackedWidget(self)
        self.home_page = HomePage(host, self.stack)
        self.code_page = CodePage(host, self.stack)
        self.pw_page = PwPage(host, self.stack)
        for page in (self.home_page, self.code_page, self.pw_page):
            self.stack.addWidget(page)
        root.addWidget(self.stack, 1)

        # 页内轻提示（窗内 QLabel，绝不弹顶层气泡窗）
        self._toast_label = QLabel("", self)
        self._toast_label.setObjectName("toastBubble")
        self._toast_label.setAttribute(Qt.WA_TransparentForMouseEvents, True)
        self._toast_label.hide()
        self._toast_timer = QTimer(self)
        self._toast_timer.setSingleShot(True)
        self._toast_timer.timeout.connect(self._toast_label.hide)

        # ---- 接线（页 -> 窗口）----
        self.home_page.addFilesRequested.connect(self._pick_files)
        self.home_page.pwRequested.connect(self._open_pw)
        self.home_page.taskActivated.connect(self._open_task_dir)
        self.code_page.finished.connect(self.leave_code)
        self.pw_page.finished.connect(self._on_pw_finished)

        # ---- 快捷键：Alt+←/→ 后退/前进；Esc 按页语义（规格 §9）----
        self._sc_back = QShortcut(QKeySequence("Alt+Left"), self)
        self._sc_back.activated.connect(lambda: self.back())
        self._sc_fwd = QShortcut(QKeySequence("Alt+Right"), self)
        self._sc_fwd.activated.connect(lambda: self.forward())
        self._sc_esc = QShortcut(QKeySequence("Esc"), self)
        self._sc_esc.activated.connect(self._on_esc)

        # ---- 状态 / 任务刷新（宿主 hub 队列由主窗消费，这里用 1s 轮询兜底；
        #      宿主也可主动调用 refresh_tasks()）----
        self._refresh_timer = QTimer(self)
        self._refresh_timer.setInterval(1000)
        self._refresh_timer.timeout.connect(self._on_tick)

        # ---- 任务数据节流：宿主任务变化经事件（主窗合并事件的防抖定时器到点）立即
        #      刷新；1s tick 只在数据过期时兜底重查，空闲时不再每秒盲查整表 ----
        self._tasks_refresh_at = 0.0
        self._tasks_hooked = False
        self._counts_cache = None      # 最近一次 db.count_tasks()（状态标签直接复用）
        try:
            host_timer = getattr(host, "_tasks_refresh_timer", None)
            if host_timer is not None:
                host_timer.timeout.connect(self._on_host_tasks_refresh)
                self._tasks_hooked = True
        except Exception:
            self._tasks_hooked = False
        self._tasks_idle_ttl = (_TASKS_IDLE_TTL_HOOKED if self._tasks_hooked
                                else _TASKS_IDLE_TTL)

        # ---- 几何记忆（saveGeometry/restoreGeometry 自带越界钳制）----
        self._geo_timer = QTimer(self)
        self._geo_timer.setSingleShot(True)
        self._geo_timer.setInterval(400)
        self._geo_timer.timeout.connect(self._save_geometry)
        self._restore_geometry()
        self._geo_ready = True

        # ---- 置顶（默认关，D6；持久化 compact_on_top）----
        try:
            self._on_top = bool(host.state.get("compact_on_top", False))
        except Exception:
            self._on_top = False
        if self._on_top:
            self.setWindowFlag(Qt.WindowStaysOnTopHint, True)

        self._apply_compact_style()
        self._switch_page("HOME")
        self._refresh_status()

    # ================= 冻结接口：显隐 / 置顶 =================
    def show_home(self, raise_=True):
        """显示窗口并切到 HOME（**不重置历史**；区别于 `leave_code`）。"""
        self._ensure_visible(bool(raise_))
        try:
            self._nav.goto("HOME")
        except Exception:
            pass
        self._switch_page("HOME")
        self.refresh_tasks()

    def set_on_top(self, flag):
        """置顶开关 + 持久化 `compact_on_top`；可见时改 flags 后需重新 show。"""
        flag = bool(flag)
        self._on_top = flag
        visible = False
        try:
            visible = bool(self.isVisible())
        except Exception:
            visible = False
        try:
            self.setWindowFlag(Qt.WindowStaysOnTopHint, flag)
        except Exception:
            pass
        if visible:
            try:
                self.show()
            except Exception:
                pass
        try:
            self._host.state.set("compact_on_top", flag)
        except Exception:
            pass

    def on_top(self):
        return bool(self._on_top)

    # ================= 冻结接口：CODE 页（分享流程唯一入口） =================
    def enter_code(self, surl, url, share_uk, force_pick=False, open_browser=False):
        """进入 / 复用 CODE 页。

        单例：CODE 页当前就是这个 (surl 或 share_uk) -> 只更新字段 + 重置 120s
        倒计时，返回 False；否则 push CODE 页（记住 force_pick）并启动倒计时，
        返回 True（调用方可据此决定是否做「未知分享者探针」之类的首次动作）。
        """
        surl = str(surl or "").strip()
        url = str(url or "").strip()
        uk = str(share_uk or "").strip()
        same = self._code_target_matches(surl, uk)
        self._ensure_visible(True)
        self.code_page.load(surl, url, uk, bool(force_pick),
                            reset_input=not same)
        if same:
            if self.current_page_key() != "CODE":
                self.push_page("CODE")
            self.code_page.start()
            return False
        self.push_page("CODE")
        self.code_page.start()
        if open_browser:
            self._probe_share_url(url)
        return True

    def leave_code(self):
        """回到 HOME，并把历史重置为 [HOME]（CODE 页的特殊退出语义，规格 §4.2）。"""
        try:
            self.code_page.abandon()
        except Exception:
            pass
        self.reset_history("HOME")

    def code_open_for(self):
        """当前活动的 CODE 请求：(surl, share_uk)；没有活动请求返回 ("", "")。"""
        try:
            if self.code_page.is_active():
                return (self.code_page.target_surl(), self.code_page.target_uk())
        except Exception:
            pass
        return ("", "")

    # ================= 冻结接口：挑选要下载的文件（Alt+3，本窗内完成） =================
    def _ensure_pick_page(self):
        """懒建 PICK 页。

        延迟 import + 延迟 addWidget：该页文件缺失或损坏时**绝不能**让小窗整个起不来，
        也绝不能让 worker 静默挂死（拿不到页面就按取消唤醒它，见 enter_pick）。
        """
        page = getattr(self, "_pick_page", None)
        if page is not None:
            return page
        try:
            from .pages_pick import PickPage
        except Exception as ex:
            self._log("挑选页组件加载失败: %s" % ex)
            return None
        try:
            page = PickPage(self._host, self.stack)
            page.commitRequested.connect(self._on_pick_commit)
            page.cancelRequested.connect(self._on_pick_cancel)
            self.stack.addWidget(page)
        except Exception as ex:
            self._log("挑选页创建失败: %s" % ex)
            return None
        self._pick_page = page
        return page

    def enter_pick(self, req):
        """把 worker 的挑选请求切到本窗 PICK 页（替代独立的 ShareFilesDialog）。

        `req` 是 `_pick_share_worker` 推来的握手字典（prep/url/surl/event/pairs/
        cancelled/answered）。数据层完全复用现有 `baidu_share.list_share_dir`，提交仍走
        宿主既有的 `_on_share_pick_commit`（**提交只在 worker 线程发生**，本页不碰下载管线）。
        取不到页面就按取消唤醒 worker，绝不静默挂死。返回是否已接管。
        """
        page = self._ensure_pick_page()
        if page is None:
            try:
                self._host._abort_share_pick(req, "精简窗缺少挑选组件，已按取消处理")
            except Exception:
                pass
            return False
        prep = req.get("prep") or {}
        entries = prep.get("entries") or []
        try:
            from ... import baidu_share as bs
        except Exception:
            bs = None

        def _on_expand(path, _prep=prep):
            if bs is None:
                return (False, "缺少网盘组件")
            return bs.list_share_dir(_prep, path)

        try:
            page.reset()
            page.load(entries, _on_expand, subtitle=str(req.get("url") or ""))
        except Exception as ex:
            self._log("挑选页载入失败: %s" % ex)
            try:
                self._host._abort_share_pick(req, "精简窗挑选页载入失败")
            except Exception:
                pass
            return False
        self._pick_req = req
        self._ensure_visible(True)
        self.push_page("PICK")
        return True

    def _on_pick_commit(self, pairs):
        """PICK 页确认：交回宿主既有提交回调（提交仍在 worker 线程完成）。"""
        req = getattr(self, "_pick_req", None)
        self._pick_req = None
        if req is not None:
            try:
                self._host._on_share_pick_commit(req, list(pairs or []))
            except Exception as ex:
                self._log("挑选提交失败: %s" % ex)
                try:
                    self._host._abort_share_pick(req, "精简窗挑选提交失败")
                except Exception:
                    pass
        self.leave_pick()

    def _on_pick_cancel(self):
        """PICK 页取消 / Esc：按取消唤醒 worker，绝不让它空等到超时。"""
        req = getattr(self, "_pick_req", None)
        self._pick_req = None
        if req is not None:
            try:
                self._host._abort_share_pick(req, "已在精简窗取消挑选")
            except Exception:
                pass
        self.leave_pick()

    def leave_pick(self):
        """离页：清空挑选页并回 HOME（与 CODE 页同款退出语义，历史重置为 [HOME]）。"""
        page = getattr(self, "_pick_page", None)
        if page is not None:
            try:
                page.reset()
            except Exception:
                pass
        self.reset_history("HOME")

    # ================= 冻结接口：导航（测试直接使用） =================
    def push_page(self, key):
        """进入新页（截断游标之后的记录，标准浏览器语义）。"""
        key = str(key or "").upper()
        if key not in _PAGE_KEYS:
            return
        self._nav.push(key)
        self._switch_page(key)

    def back(self):
        """后退；有历史可退返回 True。"""
        if not self._nav.back():
            return False
        self._switch_page(self._nav.current())
        return True

    def forward(self):
        """前进；有前进记录返回 True。"""
        if not self._nav.forward():
            return False
        self._switch_page(self._nav.current())
        return True

    def reset_history(self, key="HOME"):
        """历史重置为单页（默认 HOME）。"""
        key = str(key or "HOME").upper()
        if key not in _PAGE_KEYS:
            key = "HOME"
        self._nav.reset(key)
        self._switch_page(key)

    def current_page_key(self):
        return self._nav.current()

    # ================= 状态刷新（供宿主可选接线） =================
    def refresh_tasks(self):
        """立即重查任务数据（宿主事件 / 显式动作调用）。

        1s tick 只是兜底：宿主有 `_tasks_refresh_timer`（MainWindow 合并任务事件的
        防抖定时器）时，其到点即接事件强制刷新；没有钩子时按 TTL 节流，空闲时不再
        每秒盲查整表。列表刷新顺带返回计数，状态区直接复用（同一轮不重复查库）。
        """
        self._tasks_refresh_at = 0.0
        counts = None
        try:
            counts = self.home_page.refresh_tasks()
        except Exception:
            counts = None
        self._refresh_status(counts)
        self._tasks_refresh_at = time.monotonic()

    # ================= 拖放（整窗接收，规格 §6.3 / §14.3） =================
    @staticmethod
    def _drop_http_urls(mime):
        """拖入内容里的 http(s) 链接（与 MainWindow._drop_http_urls 同判定）。"""
        out = []
        try:
            for u in mime.urls():
                s = str(u.toString() or "")
                low = s.lower()
                if low.startswith("http://") or low.startswith("https://"):
                    out.append(s)
        except Exception:
            pass
        return out

    def _drag_acceptable(self, mime):
        """与 main_window.dragEnterEvent 同一判定：本地文件 / 图片 / http(s) 链接。"""
        try:
            if mime.hasUrls() and any(u.isLocalFile() for u in mime.urls()):
                return True
            if mime.hasImage() or self._drop_http_urls(mime):
                return True
        except Exception:
            pass
        return False

    def dragEnterEvent(self, event):
        try:
            if self._drag_acceptable(event.mimeData()):
                event.acceptProposedAction()
                return
        except Exception:
            pass
        event.ignore()

    def dragMoveEvent(self, event):
        """进入 / 移动时给落区加高亮（强调色虚线 + 浅强调底）——现仓新增的第一处。"""
        try:
            ok = self._drag_acceptable(event.mimeData())
        except Exception:
            ok = False
        try:
            self.home_page.set_drag_active(ok)
        except Exception:
            pass
        if ok:
            event.acceptProposedAction()
        else:
            event.ignore()

    def dragLeaveEvent(self, event):
        try:
            self.home_page.set_drag_active(False)
        except Exception:
            pass
        event.accept()

    def dropEvent(self, event):
        """落地：本地文件逐路径调**现有** `host._handle_drop_file`（绝不另写入队管线）。"""
        try:
            self.home_page.set_drag_active(False)
        except Exception:
            pass
        mime = event.mimeData()
        paths = []
        try:
            for u in mime.urls():
                if u.isLocalFile():
                    p = Path(u.toLocalFile())
                    if p.is_file():
                        paths.append(p)
        except Exception:
            pass
        urls = self._drop_http_urls(mime)
        image = None
        try:
            if mime.hasImage():
                image = mime.imageData()
        except Exception:
            image = None
        if not paths and not urls and image is None:
            event.ignore()
            return
        event.acceptProposedAction()
        for path in paths:
            self._handle_drop_file(path)
        if image is not None:
            fn = getattr(self._host, "_handle_drop_image", None)
            if callable(fn):
                try:
                    fn(image)
                except Exception as ex:
                    self._log("拖放: 图片处理出错: %s" % ex)
        elif urls:
            fn = getattr(self._host, "_handle_drop_url", None)
            if callable(fn):
                for url in urls:
                    try:
                        fn(url)
                    except Exception as ex:
                        self._log("拖放: 链接处理出错: %s" % ex)
        self.refresh_tasks()

    # ================= 内部：页面切换 / 历史按钮 =================
    def _switch_page(self, key):
        page = {"HOME": self.home_page, "CODE": self.code_page,
                "PW": self.pw_page,
                "PICK": getattr(self, "_pick_page", None)}.get(str(key or ""))
        if page is not None:
            try:
                self.stack.setCurrentWidget(page)
            except Exception:
                pass
        self._sync_nav_buttons()

    def _sync_nav_buttons(self):
        """`‹`/`›`：有历史才显示并可用（规格 §4.2）。"""
        try:
            back_ok = self._nav.can_back()
            fwd_ok = self._nav.can_forward()
            self.back_btn.setVisible(back_ok)
            self.back_btn.setEnabled(back_ok)
            self.fwd_btn.setVisible(fwd_ok)
            self.fwd_btn.setEnabled(fwd_ok)
        except Exception:
            pass

    def _code_target_matches(self, surl, uk):
        """单例判定：CODE 页仍有活动请求，且 (surl 或 share_uk) 与之相同。"""
        try:
            if not self.code_page.is_active():
                return False
            cur_s = self.code_page.target_surl()
            cur_u = self.code_page.target_uk()
            if surl or uk:
                return bool((surl and cur_s and surl == cur_s)
                            or (uk and cur_u and uk == cur_u))
            # 手动入口（空目标）：已在 CODE 页的同一空表单直接复用
            return not (cur_s or cur_u)
        except Exception:
            return False

    # ================= 内部：HOME 按钮动作 =================
    def _open_pw(self):
        try:
            self.pw_page.reset()
        except Exception:
            pass
        self.push_page("PW")

    def _pick_files(self):
        """`＋ 添加文件`：QFileDialog -> 与拖拽同一条入队路径。"""
        try:
            files, _selected = QFileDialog.getOpenFileNames(
                self, "添加文件", "",
                "压缩包 (*.zip *.rar *.7z *.tar *.gz *.bz2 *.xz *.001);;所有文件 (*.*)")
        except Exception:
            files = []
        for name in files or []:
            self._handle_drop_file(Path(str(name)))
        if files:
            self.refresh_tasks()

    def _handle_drop_file(self, path):
        fn = getattr(self._host, "_handle_drop_file", None)
        if not callable(fn):
            return
        try:
            fn(Path(path))
        except Exception as ex:
            self._log("拖放处理出错: %s" % ex)

    def _open_task_dir(self, task_id):
        """双击任务行 = 打开输出目录（复用宿主既有入口；桩环境走等价回落）。"""
        fn = getattr(self._host, "_open_task_dir", None)
        if callable(fn):
            try:
                fn(task_id)
                return
            except Exception:
                pass
        task = {}
        try:
            task = db.get_task(task_id) or {}
        except Exception:
            task = {}
        for key, label in (("output_dir", "输出目录"), ("source_dir", "源目录")):
            path = str(task.get(key) or "").strip()
            if not path:
                continue
            try:
                if os.path.isdir(path):
                    os.startfile(path)      # 仅 Windows；本程序只支持 Windows
                    return
            except Exception as ex:
                self._log("打开%s失败: %s" % (label, ex))
                return
        self._log("该任务还没有可打开的目录（输出目录未生成）")

    # ================= 内部：PW 结果 / 轻提示 =================
    def _on_pw_finished(self, saved):
        if saved:
            self._toast("已新增口令")
            self.reset_history("HOME")
            return
        # 取消 / Esc：遵循普通历史（能退就退，否则回 HOME）
        if not self.back():
            self.reset_history("HOME")

    def _toast(self, text):
        """窗内轻提示（底部居中，约 1.8s 自动消失；不新建任何顶层窗）。"""
        try:
            self._toast_label.setText(str(text))
            self._toast_label.adjustSize()
            area = self.stack.geometry()
            x = area.x() + max(0, (area.width() - self._toast_label.width()) // 2)
            y = area.y() + max(0, area.height() - self._toast_label.height() - 12)
            self._toast_label.move(x, y)
            self._toast_label.show()
            self._toast_label.raise_()
            self._toast_timer.start(1800)
        except Exception:
            pass

    # ================= 内部：Esc / 关闭 / ⤢ =================
    def _on_esc(self):
        """Esc：CODE=忽略回 HOME；PW=取消回 HOME；HOME=与主界面一致（走 close_action）。

        HOME 这一支必须调 `_on_close_clicked`——它已忠实实现 `close_action`
        （tray=隐藏到托盘 / exit=关掉整个程序 / ask=弹询问并记住选择），**不能**用
        `self.close()`：小窗的 closeEvent 只 accept、不读 close_action，那样会把
        「用户把 Esc 设成关闭程序」变成「只是关掉小窗」。用户要求小窗是大窗的清爽化，
        该有的一致性都得有。
        """
        key = self.current_page_key()
        if key == "CODE":
            self.code_page.request_ignore()
        elif key == "PW":
            self.pw_page.request_cancel()
        elif key == "PICK":
            page = getattr(self, "_pick_page", None)
            if page is not None:
                page.request_cancel()
        else:
            self._on_close_clicked()

    def _on_full_clicked(self):
        """`⤢` 返回完整界面：优先走宿主互斥切换（唯一入口，D5）。"""
        fn = getattr(self._host, "_toggle_compact", None)
        if callable(fn):
            try:
                fn(False)
                return
            except Exception:
                pass
        # 回落：隐藏小窗 + 显示主窗（与互斥显示同语义）
        self.hide()
        host = self._host
        try:
            host.showNormal()
            host.raise_()
            host.activateWindow()
        except Exception:
            pass

    def _on_close_clicked(self):
        """`✕` 沿用宿主 `close_action` 语义（ask / tray / exit），不新造行为。

        与 `MainWindow.closeEvent` 的等价分支：
        - exit  -> 宿主既有退出入口（_quit / close）；
        - tray  -> 隐藏小窗 + 宿主 _hide_window（+ 托盘气泡）；
        - ask   -> 走宿主自己的询问方法（_ask_close_action），按选择结果执行，
                   取消则小窗保持原样——绝不复制一套自造询问。
        """
        action = "ask"
        try:
            action = str(self._host.state.snapshot().get("close_action", "ask")
                         or "ask").strip().lower()
        except Exception:
            action = "ask"
        if action == "tray":
            self._close_to_tray()
            return
        if action == "exit":
            self._quit_via_host()
            return
        ask = getattr(self._host, "_ask_close_action", None)
        if not callable(ask):
            # 宿主没有询问方法：交给宿主 closeEvent 既有语义
            try:
                self._host.close()
            except Exception:
                self._quit_via_host()
            return
        try:
            result = ask()
        except Exception:
            result = None
        if not result:
            return                       # 宿主没给出结果（异常等）：小窗保持原样
        try:
            choice, remember = result
        except Exception:
            return
        # ⚠️ 必须单独判 None：宿主 `_ask_close_action()` 返回的是 **(结果, 是否记住)**
        # 二元组，用户取消时是 `(None, False)`——元组本身是「真值」，上面的
        # `if not result` 拦不住它。漏了这条，用户点「取消」会掉进下面的 else 被当成
        # tray（隐藏到托盘），与主界面「取消=中止关闭」正好相反。
        if choice is None:
            return                       # 用户在询问里取消：小窗保持原样
        if remember:
            try:
                self._host.state.set("close_action", choice)
            except Exception:
                pass
        if str(choice) == "exit":
            self._quit_via_host()
        else:
            self._close_to_tray()

    def _close_to_tray(self):
        """tray 分支：隐藏小窗 + 宿主隐藏到托盘（+ 既有托盘提示）。"""
        self.hide()
        host = self._host
        hide = getattr(host, "_hide_window", None)
        if callable(hide):
            try:
                hide()
            except Exception:
                pass
        else:
            try:
                host.hide()
            except Exception:
                pass
        notify = getattr(host, "_notify_trayed", None)
        if callable(notify):
            try:
                notify()
            except Exception:
                pass

    def _quit_via_host(self):
        """exit 分支：走宿主既有退出逻辑（托盘隐藏 + 事件循环退出）。"""
        fn = getattr(self._host, "_quit", None)
        if callable(fn):
            try:
                fn()
                return
            except Exception:
                pass
        app = QApplication.instance()
        if app is not None:
            try:
                app.quit()
            except Exception:
                pass

    # ================= 内部：状态区 / 轮询 =================
    def _on_tick(self):
        """1s tick：暂停态每拍跟手；任务数据只在过期时兜底重查（事件即时刷新）。"""
        if self._tasks_cache_stale():
            self.refresh_tasks()
        else:
            self._refresh_status()

    def _tasks_cache_stale(self):
        """距上次真实查询是否已过 TTL（取不到时间戳按过期处理，绝不漏刷）。"""
        try:
            return ((time.monotonic() - self._tasks_refresh_at)
                    >= self._tasks_idle_ttl)
        except Exception:
            return True

    def _on_host_tasks_refresh(self):
        """宿主任务变化通知：可见就立即重查；不可见只作废缓存（show 时再刷）。"""
        self._tasks_refresh_at = 0.0
        try:
            if self.isVisible():
                self.refresh_tasks()
        except Exception:
            pass

    def _refresh_status(self, counts=None):
        """标题栏状态：`● 运行中` / `● 已暂停`（队列 N 有余量时追加）。

        counts：调用方刚查到的 `db.count_tasks()`；不传则复用上次结果（仅首次
        无缓存时才自查）。无参调用只重渲染标签（暂停态每 tick 跟手），不碰数据库。
        """
        if counts is not None:
            self._counts_cache = counts
        elif self._counts_cache is None:
            try:
                self._counts_cache = db.count_tasks() or {}
            except Exception:
                self._counts_cache = {}
        counts = self._counts_cache or {}
        queue_n = 0
        try:
            queue_n = int(counts.get("queue", 0) or 0)
        except Exception:
            queue_n = 0
        paused = False
        pauser = getattr(self._host, "pauser", None)
        try:
            paused = bool(pauser is not None and pauser.is_paused())
        except Exception:
            paused = False
        text = "● 已暂停" if paused else "● 运行中"
        if queue_n > 0:
            text += " · 队列 %d" % queue_n
        try:
            color = PALETTE["muted"] if paused else PALETTE["success"]
            self.status_label.set_full_text(text)   # 可省略标签：全文进 tooltip
            self.status_label.setStyleSheet("color: %s; font-size: 11px;" % color)
        except Exception:
            pass
        self._sync_nav_buttons()

    # ================= 内部：窗口生命周期 =================
    def showEvent(self, event):
        super().showEvent(event)
        self._clamp_to_screen()
        try:
            self._refresh_timer.start()
        except Exception:
            pass
        self.refresh_tasks()

    def hideEvent(self, event):
        try:
            self._refresh_timer.stop()
        except Exception:
            pass
        self._save_geometry()
        super().hideEvent(event)

    def closeEvent(self, event):
        try:
            self._refresh_timer.stop()
        except Exception:
            pass
        try:
            self.code_page.stop()
        except Exception:
            pass
        self._save_geometry()
        event.accept()
        super().closeEvent(event)

    def changeEvent(self, event):
        try:
            super().changeEvent(event)
        except Exception:
            pass
        try:
            et = event.type()
            theme_ev = getattr(QEvent, "ThemeChange", None)
            if et in (QEvent.StyleChange, QEvent.PaletteChange) or \
                    (theme_ev is not None and et == theme_ev):
                self._apply_compact_style()
        except Exception:
            pass

    def moveEvent(self, event):
        super().moveEvent(event)
        self._queue_geometry_save()

    def resizeEvent(self, event):
        super().resizeEvent(event)
        self._queue_geometry_save()

    # ================= 内部：几何记忆 / 钳制 =================
    def _queue_geometry_save(self):
        if not self._geo_ready:
            return
        try:
            self._geo_timer.start()
        except Exception:
            pass

    def _restore_geometry(self):
        """恢复 saveGeometry 的 base64；失败 / 越界 -> 主屏右下角离边 24px。"""
        ok = False
        raw = None
        try:
            raw = self._host.state.get("compact_geometry", "")
        except Exception:
            raw = None
        if raw:
            try:
                ba = QByteArray.fromBase64(str(raw).encode("ascii"))
                if not ba.isEmpty():
                    ok = bool(self.restoreGeometry(ba))
            except Exception:
                ok = False
        if not ok:
            try:
                self.resize(_WIN_W, _WIN_H)
            except Exception:
                pass
            self._move_default()
        else:
            self._clamp_to_screen()

    def _save_geometry(self):
        """持久化窗口几何（base64 字符串；Qt 自带越界钳制）。"""
        try:
            data = bytes(self.saveGeometry().toBase64()).decode("ascii")
            if data:
                self._host.state.set("compact_geometry", data)
        except Exception:
            pass

    def _move_default(self):
        """首次 / 越界回落：主屏可用区右下角、离边 24px。"""
        try:
            scr = QApplication.primaryScreen()
            if scr is None:
                return
            geo = scr.availableGeometry()
            self.move(geo.x() + geo.width() - self.width() - _DEFAULT_MARGIN,
                      geo.y() + geo.height() - self.height() - _DEFAULT_MARGIN)
        except Exception:
            pass

    def _clamp_to_screen(self):
        """与任何屏幕可用区都无交集时收回到主屏右下角。"""
        try:
            fg = self.frameGeometry()
            screens = QApplication.screens()
            if not any(s.availableGeometry().intersects(fg) for s in screens):
                self._move_default()
        except Exception:
            pass

    # ================= 内部：无边框边缘缩放 =================
    def _edge_hit(self, pos):
        r = self.rect()
        x, y = int(pos.x()), int(pos.y())
        return (x <= _EDGE, y <= _EDGE,
                x >= r.width() - _EDGE, y >= r.height() - _EDGE)

    def mousePressEvent(self, event):
        try:
            if event.button() == Qt.LeftButton:
                edge = self._edge_hit(event.pos())
                if any(edge):
                    self._resize_ctx = {"edge": edge, "geo": QRect(self.geometry()),
                                        "gp": event.globalPos()}
                    event.accept()
                    return
        except Exception:
            pass
        super().mousePressEvent(event)

    def mouseMoveEvent(self, event):
        try:
            ctx = self._resize_ctx
            if ctx is not None and (event.buttons() & Qt.LeftButton):
                self._apply_resize(ctx, event.globalPos())
                event.accept()
                return
        except Exception:
            pass
        super().mouseMoveEvent(event)

    def mouseReleaseEvent(self, event):
        if self._resize_ctx is not None:
            self._resize_ctx = None
            event.accept()
            return
        super().mouseReleaseEvent(event)

    def _apply_resize(self, ctx, global_pos):
        """按边缘拖动缩放；越界由 minimumSize/maximumSize 自动钳制。"""
        geo = QRect(ctx["geo"])
        dx = int(global_pos.x() - ctx["gp"].x())
        dy = int(global_pos.y() - ctx["gp"].y())
        left, top, right, bottom = ctx["edge"]
        min_w, min_h = self.minimumWidth(), self.minimumHeight()
        if left:
            geo.setLeft(min(geo.left() + dx, geo.right() - min_w + 1))
        if right:
            geo.setRight(max(geo.right() + dx, geo.left() + min_w - 1))
        if top:
            geo.setTop(min(geo.top() + dy, geo.bottom() - min_h + 1))
        if bottom:
            geo.setBottom(max(geo.bottom() + dy, geo.top() + min_h - 1))
        self.setGeometry(geo)

    # ================= 内部：标题栏 =================
    def _build_titlebar(self):
        bar = _TitleBar(self)
        lay = QHBoxLayout(bar)
        lay.setContentsMargins(10, 0, 6, 0)
        lay.setSpacing(6)

        self._icon_label = QLabel(bar)
        try:
            self._icon_label.setPixmap(make_tray_icon().pixmap(16, 16))
        except Exception:
            pass
        lay.addWidget(self._icon_label, 0, Qt.AlignVCenter)

        title = QLabel("AutoUnpacker", bar)
        title.setObjectName("compactTitle")
        title.setMinimumWidth(0)          # 320px 最小宽时标题栏不顶宽（布局压力）
        lay.addWidget(title, 0, Qt.AlignVCenter)

        # 状态用可省略标签：窄窗下优先收缩它，绝不把整窗最小宽度顶大
        self.status_label = _ElideLabel("● 运行中", bar)
        self.status_label.setObjectName("compactStatus")
        self.status_label.setMaximumWidth(130)
        lay.addWidget(self.status_label, 0, Qt.AlignVCenter)
        lay.addStretch(1)

        self.back_btn = self._title_button(bar, "‹", "后退（Alt+←）", "compactNav")
        self.back_btn.clicked.connect(lambda *_: self.back())
        self.fwd_btn = self._title_button(bar, "›", "前进（Alt+→）", "compactNav")
        self.fwd_btn.clicked.connect(lambda *_: self.forward())
        # 图标用 QPainter 自绘的 `Glyph`，**不用字符 "⤢"(U+2922)**：该码位不在默认
        # UI 字体（Segoe UI）里，Qt 会回退到符号字体，其 ascent/descent 与相邻的 —/✕
        # 不同 → 视觉上不与它俩同一条水平线（用户实测）。自绘图标与字体度量无关，
        # 且 `external` 正是「打开到外部 / 返回完整界面」的语义。
        self.full_btn = self._title_button(bar, "", "返回完整界面", "compactCtl")
        _full_lay = QHBoxLayout(self.full_btn)
        _full_lay.setContentsMargins(0, 0, 0, 0)
        _full_lay.addWidget(Glyph("external", self.full_btn, 13, role="muted"))
        self.full_btn.clicked.connect(lambda *_: self._on_full_clicked())
        self.min_btn = self._title_button(bar, "—", "最小化到任务栏", "compactCtl")
        self.min_btn.clicked.connect(lambda *_: self.showMinimized())
        self.close_btn = self._title_button(bar, "✕", "关闭", "compactClose")
        self.close_btn.clicked.connect(lambda *_: self._on_close_clicked())
        for btn in (self.back_btn, self.fwd_btn, self.full_btn,
                    self.min_btn, self.close_btn):
            lay.addWidget(btn, 0, Qt.AlignVCenter)
        return bar

    @staticmethod
    def _title_button(parent, text, tip, obj_name):
        btn = QPushButton(text, parent)
        btn.setObjectName(obj_name)
        btn.setToolTip(tip)
        btn.setFixedSize(24, 24)
        btn.setCursor(Qt.PointingHandCursor)
        btn.setFocusPolicy(Qt.NoFocus)
        return btn

    # ================= 内部：样式 / 探针 / 日志 =================
    def _apply_compact_style(self):
        """局部 stylesheet：只引用 `style.py` 已有 token（绝不新增颜色 / 圆角）。"""
        try:
            qss = self._compact_qss()
        except Exception:
            qss = ""
        if qss and qss != self._qss_cache:
            self._qss_cache = qss
            try:
                self.setStyleSheet(qss)
            except Exception:
                pass

    @staticmethod
    def _compact_qss():
        """从当前主题 token 生成小窗局部 QSS（主题切换时由 changeEvent 重建）。"""
        try:
            tk = dict(ui_style.tokens())
        except Exception:
            tk = {}

        def c(name):
            """取已有 token 值；极端缺失时回落到 PALETTE（同一调色板），不新增颜色。"""
            try:
                val = tk.get(name)
            except Exception:
                val = None
            if val:
                return str(val)
            return str(PALETTE.get(name) or PALETTE.get("muted") or "transparent")

        return "\n".join([
            "QWidget#compactWindow { background: %s; }" % c("window_bg"),
            "QWidget#compactTitleBar { background: %s;"
            " border-bottom: 1px solid %s; }" % (c("card_bg"), c("card_border")),
            "QLabel#compactTitle { color: %s; font-size: 13px;"
            " font-weight: 700; }" % c("title_fg"),
            "QLabel#compactStatus { color: %s; font-size: 11px; }" % c("section_fg"),
            "QPushButton#compactNav, QPushButton#compactCtl,"
            " QPushButton#compactClose { background: transparent; border: none;"
            " border-radius: %s; color: %s; padding: 0; font-size: 13px; }"
            % (c("radius_ctl"), c("section_fg")),
            "QPushButton#compactNav:hover, QPushButton#compactCtl:hover"
            " { background: %s; color: %s; }" % (c("btn_hover"), c("btn_fg")),
            "QPushButton#compactNav:disabled { color: %s; }" % c("btn_dis_fg"),
            "QPushButton#compactClose:hover { background: %s; color: %s; }"
            % (c("danger_bg"), c("danger_fg")),
            "QFrame#compactDropZone { background: %s;"
            " border: 1px dashed %s; border-radius: %s; }"
            % (c("card_bg"), c("card_border"), c("radius_card")),
            "QFrame#compactDropZone[drag=\"true\"] { border: 1px dashed %s;"
            " background: %s; }" % (c("ctl_focus"), c("accent_soft")),
            "QLabel#compactDropHint { color: %s; font-size: 12px; }"
            % c("section_fg"),
            "QLabel#compactQueueHead { color: %s; font-size: 11.5px; }"
            % c("section_fg"),
            "QLabel#compactEmpty { color: %s; font-size: 11.5px; }"
            % c("chip_off_fg"),
            "QScrollArea#compactTaskScroll { background: transparent;"
            " border: none; }",
            "QWidget#compactTaskHost { background: transparent; }",
            "QWidget#compactTaskRow { background: transparent;"
            " border-bottom: 1px solid %s; }" % c("card_border"),
            "QLabel#compactRowName { color: %s; font-size: 12.5px; }"
            % c("window_fg"),
            "QLabel#compactRowState { color: %s; font-size: 11px; }"
            % c("section_fg"),
            "QLabel#compactPageTitle { color: %s; font-size: 14px;"
            " font-weight: 700; }" % c("window_fg"),
            "QLabel#compactField { color: %s; font-size: 11.5px; }"
            % c("section_fg"),
            "QLabel#compactMeta { color: %s; font-size: 11.5px; }"
            % c("chip_off_fg"),
            "QLabel#compactUrl { color: %s; font-size: 11.5px; }"
            % c("section_fg"),
            "QLabel#compactTimeout { color: %s; font-size: 11.5px; }"
            % c("chip_off_fg"),
            "QLabel#compactHint { color: %s; font-size: 11.5px; }"
            % c("chip_off_fg"),
            "QLabel#compactHint[state=\"ok\"] { color: %s; }" % c("success_fg"),
            "QLabel#compactHint[state=\"bad\"] { color: %s; }" % c("danger_fg"),
            "QPushButton#compactBack { background: transparent; border: none;"
            " border-radius: %s; color: %s; font-size: 12px; padding: 2px 6px; }"
            % (c("radius_ctl"), c("title_fg")),
            "QPushButton#compactBack:hover { background: %s; }" % c("btn_hover"),
            "QLabel#compactWarn { color: %s; background: %s;"
            " border: 1px solid %s; border-radius: %s; padding: 6px 8px;"
            " font-size: 11.5px; }"
            % (c("danger_fg"), c("danger_bg"), c("danger_border"), c("radius_ctl")),
        ])

    def _probe_share_url(self, url):
        """未知分享者探针：与 share_flow 现行为一致（仅新开 CODE 页时一次）。"""
        url = str(url or "").strip()
        low = url.lower()
        if not (low.startswith("http://") or low.startswith("https://")):
            return
        blocked = False
        try:
            from ..window.share_flow import _share_pan_open_blocked
            blocked = bool(_share_pan_open_blocked(self._host, url))
        except Exception:
            try:
                snap = self._host.state.snapshot()
                blocked = bool(snap.get("experimental_enabled")) and \
                    ("pan.baidu" in low)
            except Exception:
                blocked = False
        if blocked:
            self._log("已开启实验性：pan.baidu 网址改走静默通道，不在浏览器打开")
            return
        try:
            webbrowser.open(url, new=2)
            self._log("未知分享者，已在浏览器打开分享页: %s" % url)
        except Exception as ex:
            self._log("打开分享页失败: %s" % ex)

    def _log(self, msg):
        try:
            hub = getattr(self._host, "hub", None)
            if hub is not None:
                hub.log(str(msg))
        except Exception:
            pass

    # ================= 内部：显隐兜底 =================
    def _ensure_visible(self, raise_):
        try:
            self.show()
            if raise_:
                self.raise_()
                self.activateWindow()
        except Exception:
            pass
