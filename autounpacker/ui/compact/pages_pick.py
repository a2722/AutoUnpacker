# -*- coding: utf-8 -*-
"""PICK 页：在小窗里挑选要下载的文件（替代独立的 `ShareFilesDialog`）。

方案 E 的 D2 硬约束是「精简模式下不弹任何子窗」，所以 Alt+3 的挑选必须在**本窗内**完成。
数据与选择契约与 `ui/share_files.py` 的对话框**完全一致**：

    entries   : [{"fs_id","path","name","size","isdir"}, ...]   根层条目
    on_expand : on_expand(path) -> (ok, children|reason)        阻塞/联网，必须在工作线程里跑
    pairs     : [(fs_id, path), ...]  **只含勾选的文件**（目录绝不提交；勾目录 = 勾其已加载子孙）

行内 payload 为 `fs_id / path / name / size / kind("dir"|"file")`，role 直接复用对话框的
`ROLE_PAYLOAD` / `ROLE_STATE`，避免出现第二套口径。

与对话框的差异（刻意如此，别当缺陷修）：
  1. **不自动递归展开**目录，只做「点开才加载」（惰性）——避免把轻量小窗变成重活；
     因此「勾目录」只把该目录**已加载**的子孙一起勾上。
  2. 加载中 / 加载失败 / 统计一律在**页内**显示（状态行 + 占位子项），绝不弹顶层窗。
  3. 展开结果经 `pyqtSignal` 从工作线程投回主线程（本仓铁律：绝不从工作线程碰控件）。

长文件名一律 `ElideMiddle` **截断、绝不换行**，完整路径始终留在悬停 tooltip 里。
"""
import threading

from PyQt5.QtCore import Qt, pyqtSignal
from PyQt5.QtWidgets import (QAbstractItemView, QHBoxLayout, QHeaderView, QLabel,
                             QPushButton, QTreeWidget, QTreeWidgetItem,
                             QVBoxLayout, QWidget)

from ..share_files import (MAX_AUTO_INFLIGHT, ROLE_PAYLOAD, ROLE_STATE,
                           format_size)
from ..textfit import fit_text_heights
from ..widgets.common import repolish
from ..widgets.inputs import _ElideLabel

MAX_INFLIGHT = MAX_AUTO_INFLIGHT        # 并发上限（与对话框同值 = 4）
LOADING_TEXT = "加载中…"
PLACEHOLDER_TEXT = "（点击展开加载）"
EMPTY_TEXT = "该分享没有可选择的内容"


class PickPage(QWidget):
    """小窗内的「挑选要下载的文件」页（冻结接口见模块 docstring）。"""

    commitRequested = pyqtSignal(list)   # 点「下载选中」：[(fs_id, path), ...] 仅文件
    cancelRequested = pyqtSignal()       # 取消 / 返回（Esc 也走这里）
    _expandFinished = pyqtSignal(str, bool, object)   # 工作线程 -> 主线程

    def __init__(self, host, parent=None):
        super().__init__(parent)
        self._host = host
        self.setObjectName("compactPickPage")
        self._on_expand = None
        self._guard = False
        self._pending = {}        # path -> 目录节点（在途或排队）
        self._queue = []          # 排队等并发位的 path（FIFO）
        self._active = set()      # 正在工作线程里跑的 path
        self._expandFinished.connect(self._on_expand_finished)

        root = QVBoxLayout(self)
        root.setContentsMargins(12, 10, 12, 12)
        root.setSpacing(8)

        head = QHBoxLayout()
        head.setSpacing(8)
        self.title_label = QLabel("选择要下载的文件", self)
        self.title_label.setObjectName("compactPageTitle")
        head.addWidget(self.title_label, 0)
        self.url_label = _ElideLabel("", self)
        self.url_label.setObjectName("compactUrl")
        head.addWidget(self.url_label, 1)
        root.addLayout(head)

        self.status_lbl = _ElideLabel("", self)
        self.status_lbl.setObjectName("compactHint")
        root.addWidget(self.status_lbl)

        self.tree = QTreeWidget(self)
        self.tree.setObjectName("compactPickTree")
        self.tree.setColumnCount(2)
        self.tree.setHeaderLabels(["名称", "大小"])
        self.tree.setUniformRowHeights(True)
        self.tree.setRootIsDecorated(True)
        self.tree.setEditTriggers(QAbstractItemView.NoEditTriggers)
        self.tree.setTextElideMode(Qt.ElideMiddle)   # 长名中间截断（硬要求）
        self.tree.setWordWrap(False)                 # 绝不换行（硬要求）
        hdr = self.tree.header()
        hdr.setSectionResizeMode(0, QHeaderView.Stretch)
        hdr.setSectionResizeMode(1, QHeaderView.ResizeToContents)
        self.tree.itemChanged.connect(self._on_item_changed)
        self.tree.itemExpanded.connect(self._on_item_expanded)
        root.addWidget(self.tree, 1)

        self.empty_label = QLabel(EMPTY_TEXT, self)
        self.empty_label.setObjectName("compactEmpty")
        self.empty_label.setAlignment(Qt.AlignCenter)
        self.empty_label.hide()
        root.addWidget(self.empty_label, 1)

        foot = QHBoxLayout()
        foot.setSpacing(6)
        foot.addStretch(1)
        self.btn_download = QPushButton("下载选中", self)
        self.btn_download.setObjectName("primary")
        self.btn_download.setCursor(Qt.PointingHandCursor)
        self.btn_download.setEnabled(False)
        self.btn_download.clicked.connect(lambda *_: self._submit())
        self.btn_cancel = QPushButton("取消", self)
        self.btn_cancel.setCursor(Qt.PointingHandCursor)
        self.btn_cancel.clicked.connect(lambda *_: self.request_cancel())
        foot.addWidget(self.btn_download, 0)
        foot.addWidget(self.btn_cancel, 0)
        root.addLayout(foot)
        # CJK 墨迹盒顶/底 1~2px 裁切兜底（字体级规则、幂等、只抬不降；空标签不触碰）。
        # 本页由 CompactWindow 懒建（晚于宿主 showEvent 的整树兜底），必须在自己构造时兜一次。
        fit_text_heights(self)

    # ================= 冻结接口 =================
    def load(self, entries, on_expand, subtitle=""):
        """载入一次挑选请求：根层条目 + 展开回调（可能是联网调用）+ 分享链接。"""
        self._shutdown_pending()
        self._on_expand = on_expand if callable(on_expand) else None
        try:
            self.url_label.set_full_text(str(subtitle or ""))
        except Exception:
            pass
        self._set_status("")
        try:
            self.tree.clear()
        except Exception:
            pass
        self._guard = True
        try:
            for ent in (entries or []):
                if isinstance(ent, dict):
                    self._add_entry(None, ent)
        finally:
            self._guard = False
        has = self.tree.topLevelItemCount() > 0
        self.tree.setVisible(has)
        self.empty_label.setVisible(not has)
        self._update_totals()

    def request_cancel(self):
        """取消 / 返回（Esc 也走这里）：只发信号，由窗口决定回哪一页。"""
        try:
            self.cancelRequested.emit()
        except Exception:
            pass

    def reset(self):
        """清空状态（离页时调用）。在途结果回来时会被丢弃，不会打到已清空的树上。"""
        self._shutdown_pending()
        self._on_expand = None
        try:
            self.tree.clear()
        except Exception:
            pass
        try:
            self.url_label.set_full_text("")
        except Exception:
            pass
        self._set_status("")
        self.tree.hide()
        self.empty_label.hide()
        self._update_totals()

    # ================= 内部：状态行 =================
    def _set_status(self, text, state=""):
        """页内状态行（ok/bad 用 window QSS 的 `#compactHint[state]`，不新增颜色）。"""
        try:
            self.status_lbl.set_full_text(str(text or ""))
            self.status_lbl.setProperty("state", str(state or ""))
            repolish(self.status_lbl)
        except Exception:
            pass

    # ================= 内部：遍历 / 收集 =================
    def _iter_items(self):
        """按显示顺序深度遍历（BFS 前置父节点，保证父在子前）。"""
        stack = [self.tree.topLevelItem(i)
                 for i in range(self.tree.topLevelItemCount())]
        while stack:
            node = stack.pop(0)
            if node is None:
                continue
            yield node
            for i in range(node.childCount()):
                stack.append(node.child(i))

    def _collect_pairs(self):
        """按显示顺序收集**勾选的文件**；目录绝不提交（与对话框同口径）。"""
        out = []
        for item in self._iter_items():
            payload = item.data(0, ROLE_PAYLOAD) or {}
            if (payload.get("kind") == "file"
                    and item.checkState(0) == Qt.Checked):
                out.append((payload.get("fs_id"), payload.get("path")))
        return out

    def _has_loading_checked_dir(self):
        """是否有「已勾选且仍在加载」的目录：此时提交会漏掉还没上屏的文件。"""
        for _path, item in list(self._pending.items()):
            try:
                payload = item.data(0, ROLE_PAYLOAD) or {}
                if (payload.get("kind") == "dir"
                        and item.checkState(0) == Qt.Checked):
                    return True
            except Exception:
                continue
        return False

    def _submit(self):
        """点「下载选中」：只发信号（提交仍在宿主的 worker 线程完成）。"""
        if self._has_loading_checked_dir():
            self._set_status("目录还在加载，稍后再试", "bad")
            return
        pairs = self._collect_pairs()
        if not pairs:
            return
        try:
            self.commitRequested.emit(pairs)
        except Exception as ex:
            self._set_status("提交失败：%s" % ex, "bad")

    def _update_totals(self):
        """按勾选刷新按钮文案与可用性（`下载选中 (N)`；0 项禁用）。"""
        try:
            pairs = self._collect_pairs()
            n = len(pairs)
            size = 0
            for item in self._iter_items():
                payload = item.data(0, ROLE_PAYLOAD) or {}
                if (payload.get("kind") == "file"
                        and item.checkState(0) == Qt.Checked):
                    try:
                        size += int(payload.get("size") or 0)
                    except Exception:
                        pass
            self.btn_download.setText("下载选中 (%d)" % n if n else "下载选中")
            self.btn_download.setToolTip(
                ("已选 %d 个文件，合计 %s" % (n, format_size(size))) if n
                else "勾选要下载的文件")
            self.btn_download.setEnabled(
                n > 0 and not self._has_loading_checked_dir())
        except Exception:
            pass

    # ================= 内部：勾选联动 =================
    def _on_item_changed(self, item, _col):
        if self._guard:
            return
        try:
            payload = item.data(0, ROLE_PAYLOAD) or {}
            if payload.get("kind") == "dir":
                self._set_subtree_checked(item,
                                          item.checkState(0) == Qt.Checked)
            node = item.parent()
            while node is not None:
                self._sync_node(node)
                node = node.parent()
        except Exception:
            pass
        self._update_totals()

    def _set_subtree_checked(self, item, checked):
        """勾/取消一个目录：只作用于**已加载**的子孙（不联网自动递归展开）。"""
        self._guard = True
        try:
            for i in range(item.childCount()):
                child = item.child(i)
                payload = child.data(0, ROLE_PAYLOAD) or {}
                if payload.get("kind") not in ("file", "dir"):
                    continue
                child.setCheckState(0,
                                    Qt.Checked if checked else Qt.Unchecked)
                if payload.get("kind") == "dir":
                    self._set_subtree_checked(child, checked)
        finally:
            self._guard = False

    def _sync_node(self, item):
        """按子孙的勾选回推本目录三态（只看**已加载**的子孙）。"""
        total = 0
        done = 0
        for i in range(item.childCount()):
            child = item.child(i)
            payload = child.data(0, ROLE_PAYLOAD) or {}
            if payload.get("kind") not in ("file", "dir"):
                continue
            total += 1
            if child.checkState(0) == Qt.Checked:
                done += 1
        if total <= 0:
            return
        self._guard = True
        try:
            item.setCheckState(0, Qt.Checked if done == total
                               else (Qt.Unchecked if done == 0
                                     else Qt.PartiallyChecked))
        finally:
            self._guard = False

    # ================= 内部：惰性展开（线程 + 信号） =================
    def _on_item_expanded(self, item):
        try:
            if item.data(0, ROLE_STATE) not in ("dummy", "failed"):
                return
            payload = item.data(0, ROLE_PAYLOAD) or {}
            if payload.get("kind") != "dir":
                return
            path = str(payload.get("path") or "")
            if not path or path in self._pending:
                return
            if not callable(self._on_expand):
                self._fail_node(item, "缺少展开接口")
                return
            self._begin_expand(item, path)
        except Exception as ex:
            self._set_status("展开失败：%s" % ex, "bad")

    def _begin_expand(self, item, path):
        while item.childCount():
            item.takeChild(0)
        item.setData(0, ROLE_STATE, "loading")
        self._placeholder(item, LOADING_TEXT)
        self._pending[path] = item
        self._queue.append(path)
        self._set_status(LOADING_TEXT)
        self._pump()
        self._update_totals()

    def _pump(self):
        """按并发上限启动排队中的展开（一次最多 MAX_INFLIGHT 个）。"""
        while self._queue and len(self._active) < MAX_INFLIGHT:
            path = self._queue.pop(0)
            item = self._pending.get(path)
            if item is None:
                continue
            try:
                if item.treeWidget() is not self.tree:
                    self._pending.pop(path, None)
                    continue
            except Exception:
                self._pending.pop(path, None)
                continue
            self._active.add(path)
            threading.Thread(target=self._run_expand, args=(path,),
                             daemon=True).start()

    def _run_expand(self, path):
        """工作线程：只调注入的 `on_expand`，结果经信号投回主线程（绝不碰控件）。"""
        fn = self._on_expand
        try:
            if not callable(fn):
                ok, data = False, "缺少展开接口"
            else:
                res = fn(path)
                if isinstance(res, (tuple, list)) and len(res) == 2:
                    ok, data = bool(res[0]), res[1]
                else:
                    ok, data = False, "展开结果格式异常：%r" % (res,)
        except Exception as ex:
            ok, data = False, "展开异常：%s" % ex
        try:
            self._expandFinished.emit(str(path), bool(ok), data)
        except Exception:
            pass

    def _on_expand_finished(self, path, ok, data):
        """主线程：把展开结果落到节点上（节点可能已被清空/重置，必须判活）。"""
        try:
            item = self._pending.pop(path, None)
        except Exception:
            item = None
        try:
            self._active.discard(path)
        except Exception:
            pass
        try:
            if item is not None:
                try:
                    alive = item.treeWidget() is self.tree
                except Exception:
                    alive = False
                if alive:
                    if not ok:
                        self._fail_node(item, str(data))
                    elif not isinstance(data, list):
                        self._fail_node(item, "返回内容不是列表")
                    else:
                        self._load_children(item, data)
        finally:
            try:
                self._pump()
            except Exception:
                pass
            self._set_status("")
            self._update_totals()

    def _load_children(self, item, data):
        while item.childCount():
            item.takeChild(0)
        item.setData(0, ROLE_STATE, "loaded")
        self._guard = True
        try:
            for ent in data:
                if isinstance(ent, dict):
                    self._add_entry(item, ent)
        finally:
            self._guard = False
        if item.childCount() == 0:
            self._placeholder(item, "（空目录）")
        self._sync_node(item)
        parent = item.parent()
        if parent is not None:
            self._sync_node(parent)

    def _fail_node(self, item, reason):
        """展开失败：清空、置 failed（可再次展开重试）、并在状态行说明原因。"""
        try:
            while item.childCount():
                item.takeChild(0)
            item.setData(0, ROLE_STATE, "failed")
            self._guard = True
            try:
                item.setCheckState(0, Qt.Unchecked)   # 未知内容不得算进选择
            finally:
                self._guard = False
            text = "加载失败：%s（再次展开可重试）" % (reason or "未知原因")
            self._placeholder(item, text)
            self._set_status(text, "bad")
        except Exception:
            pass

    # ================= 内部：建行 =================
    def _placeholder(self, parent, text):
        """占位子项：不可勾选、不可选，只是给用户一个可见的状态。"""
        try:
            ph = QTreeWidgetItem([str(text), ""])
            ph.setFlags(Qt.ItemIsEnabled)
            ph.setData(0, ROLE_STATE, "placeholder")
            parent.addChild(ph)
        except Exception:
            pass

    def _add_entry(self, parent, ent):
        """把一条根层/子层条目建成树行（payload 与对话框同构）。"""
        kind = "dir" if ent.get("isdir") else "file"
        name = str(ent.get("name") or "").strip()
        path = str(ent.get("path") or "").strip()
        if not name:
            name = path.rstrip("/").rsplit("/", 1)[-1] or "（未命名）"
        payload = {"fs_id": ent.get("fs_id"), "path": path, "name": name,
                   "size": ent.get("size"), "kind": kind}
        size_text = format_size(payload["size"]) if kind == "file" else "—"
        if parent is None:
            item = QTreeWidgetItem(self.tree, [name, size_text])
        else:
            item = QTreeWidgetItem(parent, [name, size_text])
        item.setData(0, ROLE_PAYLOAD, payload)
        item.setFlags(Qt.ItemIsEnabled | Qt.ItemIsSelectable
                      | Qt.ItemIsUserCheckable)
        item.setToolTip(0, path or name)      # 长名截断后悬停看完整路径
        item.setToolTip(1, size_text)
        pre = False
        if parent is not None:
            try:
                pre = parent.checkState(0) == Qt.Checked
            except Exception:
                pre = False
        item.setCheckState(0, Qt.Checked if pre else Qt.Unchecked)
        if kind == "dir":
            item.setData(0, ROLE_STATE, "dummy")
            self._placeholder(item, PLACEHOLDER_TEXT)
        else:
            item.setData(0, ROLE_STATE, "file")
        return item

    # ================= 内部：在途清理 =================
    def _shutdown_pending(self):
        """丢掉所有在途/排队登记：在途线程结果回来时找不到节点，自然被忽略。"""
        try:
            self._pending.clear()
        except Exception:
            pass
        try:
            self._queue.clear()
        except Exception:
            pass
        try:
            self._active.clear()
        except Exception:
            pass
