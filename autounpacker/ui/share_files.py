# -*- coding: utf-8 -*-
"""分享内容挑选器：懒加载文件树，只把勾选的文件交给下载流程。

职责：
- ShareFilesDialog 展示 prepare_share 返回的根层条目；目录首次展开时经 on_expand
  懒加载其子项（1 次请求/目录），文件是叶子；
- 展开请求在工作线程执行，结果经 Queue + QTimer 回到 UI 线程，界面绝不卡顿；
- 提供「全选 / 反选 / 仅选文件」与「已选文件数 + 合计大小」实时统计；
- 「下载选中」只把勾选的文件（任意嵌套层级）拼成 [(fs_id, path), ...] 交给 on_commit。

关键入口：ShareFilesDialog / format_size
依赖：PyQt5、标准库（queue / threading）、.style（主题色）。
注意：
- 本文件**不做任何网络 / 百度 API 调用**，也不导入 baidu_share；所有 I/O 都由
  调用方经 on_expand / on_commit 提供（on_expand 必须在工作线程里被调用）；
- 任何 Qt 槽函数都不许向外抛异常（见 _safe_slot），失败一律就地显示；
- 对话框不置顶、非模态，方便托盘程序里边看主界面边勾选。
"""
import queue
import threading

from PyQt5.QtCore import Qt, QTimer
from PyQt5.QtGui import QBrush, QColor
from PyQt5.QtWidgets import (QAbstractItemView, QApplication, QDialog,
                             QHBoxLayout, QHeaderView, QLabel, QPushButton,
                             QTreeWidget, QTreeWidgetItem, QTreeWidgetItemIterator,
                             QVBoxLayout)

from .style import PALETTE

# 挂在 QTreeWidgetItem 上的自定义角色：payload=条目字典，state=加载状态。
ROLE_PAYLOAD = Qt.UserRole            # {"fs_id","path","name","size","kind"}
ROLE_STATE = Qt.UserRole + 1          # dummy / loading / loaded / failed / file

# 展开结果轮询间隔（毫秒）：worker 线程只投队列，UI 线程定时来取。
POLL_INTERVAL_MS = 80


def format_size(n):
    """字节数 → 人类可读文本（1024 进制）。

    规则：优先用更大的单位，只要「换到更大单位后仍 ≥ 0.5」就继续换，
    使读数落在 [0.5, 1024) 便于比较（故 926809955 显示 0.86 GB 而非 883.9 MB）。
    0 -> '0 B'、512 -> '512 B'、1024 -> '1.0 KB'、926809955 -> '0.86 GB'。
    """
    try:
        n = float(int(n))
    except Exception:
        return "—"
    if n < 0:
        return "—"
    units = ("B", "KB", "MB", "GB", "TB")
    i = 0
    while i < len(units) - 1 and n / 1024.0 > 0.5:
        n /= 1024.0
        i += 1
    if i == 0:
        return "%d B" % int(n)
    if i >= 3:
        return "%.2f %s" % (n, units[i])
    return "%.1f %s" % (n, units[i])


def _safe_slot(fn):
    """槽函数兜底装饰器：吸收多余信号参数，且任何异常都不许抛出 Qt 回调。

    PyQt 会把 clicked(bool) 的布尔值一并传进来（而 timer 信号又没有参数），
    故按被包装函数的形参个数裁剪多余位置参数，两种连接方式都能直接用。
    """
    code = getattr(fn, "__code__", None)
    n_pos = code.co_argcount if code is not None else None

    def wrapper(self, *args, **kwargs):
        if n_pos is not None:
            args = args[:max(n_pos - 1, 0)]
        try:
            return fn(self, *args, **kwargs)
        except Exception as e:      # noqa: BLE001 - 回调边界必须兜住一切
            try:
                self.status_lbl.setText("操作异常：%s" % (e,))
            except Exception:
                pass
            return None

    return wrapper


class ShareFilesDialog(QDialog):
    """分享内容挑选器：勾选文件后交给 on_commit，自身不碰网络。"""

    def __init__(self, parent, entries, on_expand, on_commit, title="分享内容",
                 subtitle="", timeout_hint_sec=None):
        """分享内容挑选器。

        entries      : [{"fs_id","path","name","size","isdir"}, ...]  根层条目
        on_expand    : on_expand(path) -> (ok, children|reason)
                       懒加载：调用方内部用 baidu_share.list_share_dir(prep, path)，
                       **在工作线程**里被调用（本对话框负责起线程），UI 不卡。
        on_commit    : on_commit(pairs)，pairs=[(fs_id, path), ...] 只含勾选的文件。
        timeout_hint : 会话约多少秒后可能需要重新准备；仅用于提示文案。
        """
        super().__init__(parent)
        self.on_expand = on_expand
        self.on_commit = on_commit
        self._guard = False           # 批量改勾选时抑制 itemChanged 递归
        self._busy = False            # 是否已 setOverrideCursor(WaitCursor)
        self._pending = {}            # path -> 等待展开结果的目录节点
        self._results = queue.Queue()  # worker 线程投递 (path, ok, data)
        self._poll_timer = QTimer(self)
        self._poll_timer.setInterval(POLL_INTERVAL_MS)
        self._poll_timer.timeout.connect(self._poll_results)

        self.setWindowTitle(title)
        self.setModal(False)                     # 托盘程序里允许与主界面并存
        self.setWindowFlag(Qt.WindowStaysOnTopHint, False)   # 明确不置顶
        self.resize(780, 560)

        lay = QVBoxLayout(self)
        lay.setContentsMargins(16, 14, 16, 14)
        lay.setSpacing(10)

        title_lbl = QLabel(title)
        title_lbl.setObjectName("appTitle")
        lay.addWidget(title_lbl)
        if subtitle:
            sub = QLabel(subtitle)
            sub.setWordWrap(True)
            sub.setStyleSheet("color: %s;" % PALETTE["muted"])
            lay.addWidget(sub)

        hint = ("勾选需要下载的文件；目录可逐层展开，勾选目录＝勾选其中已加载的文件。\n"
                "未展开的目录不会被提交；同一个链接可以再次打开，重新挑一部分。")
        if timeout_hint_sec:
            try:
                secs = int(timeout_hint_sec)
            except Exception:
                secs = None
            if secs:
                hint += ("\n提示：本次会话约 %d 秒后可能需要重新准备，"
                         "勾选后请及时点「下载选中」。" % secs)
        self.hint_lbl = QLabel(hint)
        self.hint_lbl.setWordWrap(True)
        self.hint_lbl.setStyleSheet("color: %s; font-size: 12px;" % PALETTE["muted"])
        lay.addWidget(self.hint_lbl)

        tools = QHBoxLayout()
        self.btn_all = QPushButton("全选")
        self.btn_all.setToolTip("勾选当前已加载的全部条目（含目录）")
        self.btn_all.clicked.connect(self._select_all)
        self.btn_invert = QPushButton("反选")
        self.btn_invert.setToolTip("把当前已加载文件的勾选状态取反")
        self.btn_invert.clicked.connect(self._invert)
        self.btn_files_only = QPushButton("仅选文件")
        self.btn_files_only.setToolTip("勾选全部已加载文件，并取消目录的勾选")
        self.btn_files_only.clicked.connect(self._select_files_only)
        self.status_lbl = QLabel("")
        self.status_lbl.setStyleSheet("color: %s; font-size: 12px;"
                                      % PALETTE["accent_text"])
        tools.addWidget(self.btn_all)
        tools.addWidget(self.btn_invert)
        tools.addWidget(self.btn_files_only)
        tools.addStretch(1)
        tools.addWidget(self.status_lbl)
        lay.addLayout(tools)

        self.tree = QTreeWidget()
        self.tree.setColumnCount(3)
        self.tree.setHeaderLabels(["名称", "类型", "大小"])
        self.tree.setUniformRowHeights(True)
        self.tree.setAlternatingRowColors(True)
        self.tree.setRootIsDecorated(True)
        self.tree.setEditTriggers(QAbstractItemView.NoEditTriggers)
        header = self.tree.header()
        header.setSectionResizeMode(0, QHeaderView.Stretch)
        header.setSectionResizeMode(1, QHeaderView.ResizeToContents)
        header.setSectionResizeMode(2, QHeaderView.ResizeToContents)
        lay.addWidget(self.tree, 1)

        bottom = QHBoxLayout()
        self.total_lbl = QLabel("未选择文件")
        self.btn_download = QPushButton("下载选中")
        self.btn_download.setObjectName("primary")
        self.btn_download.setToolTip("只把勾选的文件提交给网盘客户端")
        self.btn_download.clicked.connect(self._on_download)
        self.btn_cancel = QPushButton("取消")
        self.btn_cancel.clicked.connect(self.reject)
        bottom.addWidget(self.total_lbl, 1)
        bottom.addWidget(self.btn_download)
        bottom.addWidget(self.btn_cancel)
        lay.addLayout(bottom)

        # 根层填充期间抑制 itemChanged（此时信号尚未连接，双保险）。
        self._guard = True
        try:
            if entries:
                for ent in entries:
                    if isinstance(ent, dict):
                        self._add_entry(None, dict(ent))
            else:
                self.hint_lbl.setText("该分享没有可下载的条目。")
                self.status_lbl.setText("该分享没有可下载的条目。")
                self._make_placeholder(None, "（分享内没有内容）")
        finally:
            self._guard = False
        self.tree.itemChanged.connect(self._on_item_changed)
        self.tree.itemExpanded.connect(self._on_expanded)
        for btn in (self.btn_all, self.btn_invert, self.btn_files_only):
            btn.setEnabled(bool(entries))
        self._update_totals()

    # ── 条目构建 ────────────────────────────────────────────────────────────
    def _make_placeholder(self, parent, text, color=None):
        """不可勾选的占位行（未加载 / 正在展开 / 展开失败）。"""
        item = QTreeWidgetItem(self.tree if parent is None else parent,
                               [text, "", ""])
        item.setFlags(Qt.ItemIsEnabled)
        item.setForeground(0, QBrush(QColor(color or PALETTE["muted"])))
        return item

    def _add_entry(self, parent, ent):
        """把一条分享条目（根层或子层）挂进树，返回节点。"""
        payload = {
            "fs_id": ent.get("fs_id"),
            "path": str(ent.get("path") or ""),
            "name": str(ent.get("name") or ""),
            "size": ent.get("size"),
            "kind": "dir" if ent.get("isdir") else "file",
        }
        if not payload["name"]:
            payload["name"] = (payload["path"].rstrip("/").rsplit("/", 1)[-1]
                               or "（未命名）")
        size_text = format_size(payload["size"]) if payload["kind"] == "file" else "—"
        item = QTreeWidgetItem(self.tree if parent is None else parent,
                               [payload["name"],
                                "目录" if payload["kind"] == "dir" else "文件",
                                size_text])
        item.setData(0, ROLE_PAYLOAD, payload)
        item.setFlags(Qt.ItemIsEnabled | Qt.ItemIsSelectable
                      | Qt.ItemIsUserCheckable)
        item.setToolTip(0, payload["path"] or payload["name"])
        if payload["kind"] == "dir":
            item.setData(0, ROLE_STATE, "dummy")
            item.setCheckState(0, Qt.Unchecked)
            self._make_placeholder(item, "（点击展开加载）")
        else:
            item.setData(0, ROLE_STATE, "file")
            # 父目录已勾选时（预先勾选后展开），子文件随之勾选。
            pre = (parent is not None
                   and bool(parent.data(0, ROLE_PAYLOAD))
                   and parent.checkState(0) == Qt.Checked)
            item.setCheckState(0, Qt.Checked if pre else Qt.Unchecked)
        return item

    # ── 懒加载：worker 线程 + 队列轮询 ──────────────────────────────────────
    @_safe_slot
    def _on_expanded(self, item):
        """目录首次展开（或失败后重试）时，起线程请求子项。"""
        if item.data(0, ROLE_STATE) not in ("dummy", "failed"):
            return
        payload = item.data(0, ROLE_PAYLOAD)
        if not payload or payload["kind"] != "dir" or not payload["path"]:
            return
        if payload["path"] in self._pending:
            return
        self._begin_expand(item, payload["path"])

    def _begin_expand(self, item, path):
        """清掉旧占位、挂「正在展开…」、开工（线程只投队列）。"""
        while item.childCount():
            item.takeChild(0)
        item.setData(0, ROLE_STATE, "loading")
        self._make_placeholder(item, "正在展开…")
        self._pending[path] = item
        self.status_lbl.setText("正在展开…")
        self._set_busy(True)
        if not self._poll_timer.isActive():
            self._poll_timer.start()
        threading.Thread(target=self._run_expand, args=(path,), daemon=True).start()

    def _run_expand(self, path):
        """工作线程：只调 on_expand 并投递结果，绝不触碰任何控件。"""
        try:
            res = self.on_expand(path)
            if isinstance(res, (tuple, list)) and len(res) == 2:
                ok, data = bool(res[0]), res[1]
            else:
                ok, data = False, "展开结果格式异常：%r" % (res,)
        except Exception as e:      # noqa: BLE001 - worker 边界兜住一切
            ok, data = False, "展开异常：%s" % (e,)
        self._results.put((path, ok, data))

    @_safe_slot
    def _poll_results(self):
        """UI 线程定时取展开结果并落树。"""
        got = False
        while True:
            try:
                path, ok, data = self._results.get_nowait()
            except queue.Empty:
                break
            got = True
            self._apply_result(path, ok, data)
        if not self._pending:
            self._poll_timer.stop()
            self._set_busy(False)
            if self.status_lbl.text() == "正在展开…":
                self.status_lbl.setText("")
        if got:
            self._update_totals()

    def _apply_result(self, path, ok, data):
        item = self._pending.pop(path, None)
        if item is None or item.treeWidget() is not self.tree:
            return                      # 对话框已关闭 / 节点已不存在
        if not ok:
            self._fail_node(item, str(data))
            return
        if not isinstance(data, list):
            self._fail_node(item, "返回内容不是列表")
            return
        while item.childCount():
            item.takeChild(0)
        item.setData(0, ROLE_STATE, "loaded")
        self._guard = True
        try:
            for ent in data:
                if isinstance(ent, dict):
                    self._add_entry(item, dict(ent))
            self._sync_parents(item.parent())
        finally:
            self._guard = False

    def _fail_node(self, item, reason):
        """展开失败：就地显示原因，保留占位子项以便再次展开重试。"""
        while item.childCount():
            item.takeChild(0)
        item.setData(0, ROLE_STATE, "failed")
        self._make_placeholder(item, "展开失败：%s（再次展开可重试）" % reason,
                               PALETTE["danger"])
        item.setToolTip(0, "展开失败：%s" % reason)
        self.status_lbl.setText("展开失败：%s" % reason)

    # ── 勾选联动与统计 ──────────────────────────────────────────────────────
    @_safe_slot
    def _on_item_changed(self, item, column):
        if self._guard or column != 0:
            return
        payload = item.data(0, ROLE_PAYLOAD)
        if not payload:
            return
        self._guard = True
        try:
            if payload["kind"] == "dir":
                self._check_files_deep(item, item.checkState(0) == Qt.Checked)
            self._sync_parents(item.parent())
        finally:
            self._guard = False
        self._update_totals()

    def _check_files_deep(self, item, checked):
        """递归勾选/取消 item 下已加载的文件（占位行没有 payload，自动跳过）。"""
        for i in range(item.childCount()):
            child = item.child(i)
            payload = child.data(0, ROLE_PAYLOAD)
            if not payload:
                continue
            if payload["kind"] == "file":
                child.setCheckState(0, Qt.Checked if checked else Qt.Unchecked)
            else:
                self._check_files_deep(child, checked)

    def _subtree_counts(self, item):
        """返回 (已加载文件总数, 其中勾选数)，只数文件，目录不参与大小。"""
        total = checked = 0
        for i in range(item.childCount()):
            child = item.child(i)
            payload = child.data(0, ROLE_PAYLOAD)
            if not payload:
                continue
            if payload["kind"] == "file":
                total += 1
                if child.checkState(0) == Qt.Checked:
                    checked += 1
            else:
                sub_total, sub_checked = self._subtree_counts(child)
                total += sub_total
                checked += sub_checked
        return total, checked

    def _sync_parents(self, parent):
        """向上同步目录勾选：已加载文件全勾选则目录勾选，否则取消。"""
        while parent is not None:
            if not parent.data(0, ROLE_PAYLOAD):
                break
            total, checked = self._subtree_counts(parent)
            if total:
                parent.setCheckState(0, Qt.Checked if checked == total
                                     else Qt.Unchecked)
            parent = parent.parent()

    def _sync_all_dirs(self):
        for item in self._iter_items():
            payload = item.data(0, ROLE_PAYLOAD)
            if payload and payload["kind"] == "dir":
                total, checked = self._subtree_counts(item)
                if total:
                    item.setCheckState(0, Qt.Checked if checked == total
                                       else Qt.Unchecked)

    def _iter_items(self):
        """按显示顺序（先序）迭代全部节点。"""
        it = QTreeWidgetItemIterator(self.tree)
        item = it.value()
        while item is not None:
            yield item
            it += 1
            item = it.value()

    def _update_totals(self):
        """统计勾选的文件数与合计大小（目录单独计数，绝不计入大小）。"""
        files = dirs = 0
        size = 0
        for item in self._iter_items():
            payload = item.data(0, ROLE_PAYLOAD)
            if not payload or item.checkState(0) != Qt.Checked:
                continue
            if payload["kind"] == "file":
                files += 1
                try:
                    size += int(payload.get("size") or 0)
                except Exception:
                    pass
            else:
                dirs += 1
        if files or dirs:
            extra = ("、%d 个目录" % dirs) if dirs else ""
            self.total_lbl.setText("已选 %d 个文件%s，合计 %s"
                                   % (files, extra, format_size(size)))
        else:
            self.total_lbl.setText("未选择文件")
        self.btn_download.setEnabled(files > 0)

    @_safe_slot
    def _select_all(self):
        self._guard = True
        try:
            for item in self._iter_items():
                if item.data(0, ROLE_PAYLOAD):
                    item.setCheckState(0, Qt.Checked)
        finally:
            self._guard = False
        self._update_totals()

    @_safe_slot
    def _invert(self):
        self._guard = True
        try:
            for item in self._iter_items():
                payload = item.data(0, ROLE_PAYLOAD)
                if payload and payload["kind"] == "file":
                    item.setCheckState(
                        0, Qt.Unchecked if item.checkState(0) == Qt.Checked
                        else Qt.Checked)
            self._sync_all_dirs()
        finally:
            self._guard = False
        self._update_totals()

    @_safe_slot
    def _select_files_only(self):
        """勾选全部已加载文件，并取消目录自身的勾选（目录是容器，不作选择）。"""
        self._guard = True
        try:
            for item in self._iter_items():
                payload = item.data(0, ROLE_PAYLOAD)
                if payload:
                    item.setCheckState(0, Qt.Checked if payload["kind"] == "file"
                                       else Qt.Unchecked)
        finally:
            self._guard = False
        self._update_totals()

    # ── 提交 / 关闭 ─────────────────────────────────────────────────────────
    def _collect_pairs(self):
        """按显示顺序收集已勾选文件：(fs_id, path) 列表。"""
        pairs = []
        for item in self._iter_items():
            payload = item.data(0, ROLE_PAYLOAD)
            if (payload and payload["kind"] == "file"
                    and item.checkState(0) == Qt.Checked):
                pairs.append((payload["fs_id"], payload["path"]))
        return pairs

    @_safe_slot
    def _on_download(self):
        pairs = self._collect_pairs()
        if not pairs:
            return
        try:
            self.on_commit(pairs)
        except Exception as e:      # noqa: BLE001 - 提交失败就地提示，允许重试
            self.status_lbl.setText("提交失败：%s（可重试）" % (e,))
            return
        self.accept()

    def _set_busy(self, on):
        """忙碌光标：成对 set/restore，异常时重置标志防止卡住光标。"""
        try:
            if on and not self._busy:
                QApplication.setOverrideCursor(Qt.WaitCursor)
                self._busy = True
            elif not on and self._busy:
                QApplication.restoreOverrideCursor()
                self._busy = False
        except Exception:
            self._busy = False

    def _shutdown(self):
        """关闭清理：停轮询、丢结果、恢复光标（幂等）。"""
        try:
            self._poll_timer.stop()
            self._pending.clear()
            while True:
                try:
                    self._results.get_nowait()
                except queue.Empty:
                    break
        except Exception:
            pass
        self._set_busy(False)

    def done(self, r):
        self._shutdown()
        super().done(r)

    def closeEvent(self, event):
        self._shutdown()
        super().closeEvent(event)
