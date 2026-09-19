# -*- coding: utf-8 -*-
"""密码本页 PasswordBookPage（阶段6e 自 ui/page_pwbook.py 纯搬移；方法未做任何拆分）。"""
from PyQt5.QtCore import QTimer, Qt, pyqtSignal
from PyQt5.QtGui import QKeySequence
from PyQt5.QtWidgets import (QApplication, QDialog, QHBoxLayout, QLabel,
                             QLineEdit, QMessageBox, QPushButton, QShortcut,
                             QStackedWidget, QVBoxLayout, QWidget)

from ..widgets import Glyph, SegControl, show_toast

from .data import (_PwData, _ShareData, _hit_count, _passes_filter, _pick_on,
                   _row_matches)
from .dialogs import _PasswordEditDialog, _ShareEditDialog, _ShareTextDialog
from .models import _PwModel
from .table import _EmptyOverlay, _PwTable, _ShareTable, _icon_button


# 空态文案（与 pages.py 的空态语气一致）
EMPTY_BOOK = "密码本还是空的 · 点「新增口令」，或让剪贴板 / 二维码自动收录提取码"
EMPTY_PWFILTER = "没有符合当前搜索 / 筛选条件的口令"
EMPTY_SHARE = "还没有固定提取码 · 点「新增提取码」，或「批量编辑（文本）」粘贴多行"

# 筛选分段（key, 文案）——计数在运行期刷新
_FILTERS = (("all", "全部"), ("hit", "命中过"), ("miss", "未命中"), ("src", "有来源"))

# 视图模式分段（口令本 / 固定提取码）——计数在运行期刷新
_MODES = (("pwd", "口令"), ("share", "固定提取码"))


# ---------------------------------------------------------------------------
# 密码本页
# ---------------------------------------------------------------------------

class PasswordBookPage(QWidget):
    """密码本页：口令视图（搜索 + 四筛选 + 口令表 + 行内操作，含排序 / 查重）+
    固定提取码视图（特殊网盘作者的固定提取码：行级增删改 + 批量文本编辑）。

    两个视图共用一页、用模式分段（口令 / 固定提取码）互斥切换；口令数据经 _PwData、
    固定提取码经 _ShareData 行级读写。
    信号：notice(text)（复制/新增/编辑/删除等用户可见回执，绝不携带明文口令或提取码）、
          changed()（口令本发生写操作后发出，宿主可据此刷新标签徽标）。
    宿主接缝：reload()（切回本页时重查两个数据集）/ refresh_theme()（主题切换后重贴内联色）。
    """

    notice = pyqtSignal(str)
    changed = pyqtSignal()

    def __init__(self, state=None, parent=None):
        super().__init__(parent)
        self.state = state
        self._data = _PwData(state)
        self._share_data = _ShareData(state)
        self._rows = []
        self._share_rows = []
        self._keyword = ""
        self._filter = "all"
        self._mode = "pwd"
        # 视图排序状态：默认按命中次数降序（命中多的排最前）；仅重排显示，不写库
        self._sort_col = _PwModel.COL_HITS
        self._sort_order = Qt.DescendingOrder
        self._live_signature = None

        root = QVBoxLayout(self)
        root.setContentsMargins(12, 12, 12, 10)
        root.setSpacing(10)

        root.addLayout(self._build_header())

        # 口令 / 固定提取码两个视图互斥切换（模式分段在页头）
        self.stack = QStackedWidget(self)
        self.stack.addWidget(self._build_pwd_view())
        self.stack.addWidget(self._build_share_view())
        root.addWidget(self.stack, 1)

        # Ctrl+F：页内任意子控件有焦点时聚焦搜索框（页不可见时不抢焦点）
        self._sc_search = QShortcut(QKeySequence("Ctrl+F"), self)
        self._sc_search.setContext(Qt.WidgetWithChildrenShortcut)
        self._sc_search.activated.connect(self._focus_search)

        self.reload()

        # 实时刷新：页面可见时每 2s 校验一次数据签名（变了才重载；弹窗打开时跳过）。
        # 定时器只在 showEvent 里启动——从未显示过的页面绝不后台轮询（hideEvent 停表）。
        self._live_timer = QTimer(self)
        self._live_timer.setInterval(2000)
        self._live_timer.timeout.connect(self._live_tick)
        self._live_signature = self._data_signature()

    # ---- 构建 ----
    def _build_header(self):
        head = QHBoxLayout()
        head.setSpacing(8)
        head.addWidget(Glyph("key", self, 16, role="muted"))
        title = QLabel("密码本", self)
        title.setObjectName("appTitle")
        head.addWidget(title)
        self.total_label = QLabel("", self)
        self.total_label.setObjectName("modeBadge")
        head.addWidget(self.total_label)
        self.hit_label = QLabel("", self)
        self.hit_label.setObjectName("modeBadge")
        head.addWidget(self.hit_label)
        head.addStretch(1)
        self.seg_mode = SegControl(self)
        self.seg_mode.set_items([(label, key) for key, label in _MODES])
        self.seg_mode.currentChanged.connect(self._on_mode)
        head.addWidget(self.seg_mode)
        self.refresh_btn = _icon_button("refresh", "刷新", self)
        self.refresh_btn.clicked.connect(self._on_refresh)
        head.addWidget(self.refresh_btn)
        return head

    def _build_pwd_view(self):
        """口令视图：细统计条 + 搜索/筛选/操作行 + 口令表 + 空态。"""
        view = QWidget(self)
        lay = QVBoxLayout(view)
        lay.setContentsMargins(0, 0, 0, 0)
        lay.setSpacing(10)

        # 细统计条：来源 / 命中口径一眼可见（与日志页 stripHint 同语气）
        strip = QHBoxLayout()
        strip.setSpacing(8)
        self.stats_label = QLabel("", self)
        self.stats_label.setObjectName("stripHint")
        strip.addWidget(self.stats_label)
        strip.addStretch(1)
        self.hint_label = QLabel("命中次数来自解压使用记录 · 双击行可复制口令", self)
        self.hint_label.setObjectName("stripHint")
        strip.addWidget(self.hint_label)
        lay.addLayout(strip)

        lay.addLayout(self._build_filters())

        self.table = _PwTable(self)
        self.table.copyRequested.connect(self._copy_row)
        self.table.editRequested.connect(self._edit_row)
        self.table.deleteRowRequested.connect(self._delete_row)
        self.table.deleteKeyPressed.connect(self._delete_selected)
        self.table.headerClicked.connect(self._on_header_clicked)
        self.table.set_sort_indicator(self._sort_col, self._sort_order)
        sm = self.table.selectionModel()
        if sm is not None:
            sm.selectionChanged.connect(self._on_selection_changed)
        lay.addWidget(self.table, 1)
        self.table_empty = _EmptyOverlay(self.table, EMPTY_BOOK)
        return view

    def _build_share_view(self):
        """固定提取码视图：说明 + 计数/操作行 + 提取码表 + 空态。"""
        view = QWidget(self)
        lay = QVBoxLayout(view)
        lay.setContentsMargins(0, 0, 0, 0)
        lay.setSpacing(10)

        guide = QLabel(
            "特殊网盘作者的固定提取码：遇到这些分享者时优先填入提取码；"
            "勾选「需挑选」表示只挑需要的文件下载。", self)
        guide.setObjectName("stripHint")
        guide.setWordWrap(True)
        lay.addWidget(guide)

        bar = QHBoxLayout()
        bar.setSpacing(8)
        self.share_count_lbl = QLabel("", self)
        self.share_count_lbl.setObjectName("stripHint")
        bar.addWidget(self.share_count_lbl)
        bar.addStretch(1)
        self.share_batch_btn = QPushButton("批量编辑（文本）", self)
        self.share_batch_btn.setObjectName("ghostSm")
        self.share_batch_btn.setCursor(Qt.PointingHandCursor)
        self.share_batch_btn.setToolTip("按「分享者UK 提取码 [pick] [#备注]」逐行批量编辑")
        self.share_batch_btn.clicked.connect(self._batch_edit_share)
        bar.addWidget(self.share_batch_btn)
        self.share_add_btn = QPushButton("新增提取码", self)
        self.share_add_btn.setObjectName("primary")
        self.share_add_btn.setCursor(Qt.PointingHandCursor)
        self.share_add_btn.setToolTip("登记一个特殊网盘作者的固定提取码")
        self.share_add_btn.clicked.connect(self._add_share)
        bar.addWidget(self.share_add_btn)
        lay.addLayout(bar)

        self.share_table = _ShareTable(self)
        self.share_table.editRequested.connect(self._edit_share)
        self.share_table.deleteRowRequested.connect(self._delete_share)
        lay.addWidget(self.share_table, 1)
        self.share_empty = _EmptyOverlay(self.share_table, EMPTY_SHARE)
        return view

    def _build_filters(self):
        row = QHBoxLayout()
        row.setSpacing(8)
        self.search = QLineEdit(self)
        self.search.setPlaceholderText("搜索口令 / 来源 / 备注")
        self.search.setFixedWidth(230)
        self.search.textChanged.connect(self._on_search_changed)   # 内存过滤，实时生效
        row.addWidget(self.search)
        self.seg_filter = SegControl(self)
        self.seg_filter.set_items([(label, key) for key, label in _FILTERS])
        self.seg_filter.currentChanged.connect(self._on_filter_changed)
        row.addWidget(self.seg_filter)
        row.addStretch(1)
        # 多选（>=2 行）时出现；0/1 行或空页时隐藏（不是禁用）
        self.sel_copy_btn = QPushButton("复制选中", self)
        self.sel_copy_btn.setObjectName("ghostSm")
        self.sel_copy_btn.setCursor(Qt.PointingHandCursor)
        self.sel_copy_btn.setToolTip("按表格从上到下的顺序复制选中的口令（每行一条）")
        self.sel_copy_btn.clicked.connect(self._copy_selected)
        self.sel_copy_btn.setVisible(False)
        row.addWidget(self.sel_copy_btn)
        self.sel_del_btn = QPushButton("删除选中", self)
        self.sel_del_btn.setObjectName("ghostSm")
        self.sel_del_btn.setCursor(Qt.PointingHandCursor)
        self.sel_del_btn.setToolTip("删除选中的口令（先确认；仅字典收录的行会跳过）")
        self.sel_del_btn.clicked.connect(self._delete_selected)
        self.sel_del_btn.setVisible(False)
        row.addWidget(self.sel_del_btn)
        self.dedup_btn = QPushButton("查重", self)
        self.dedup_btn.setObjectName("ghostSm")
        self.dedup_btn.setCursor(Qt.PointingHandCursor)
        self.dedup_btn.setToolTip("移除重复的长期口令（保留首次出现；备注保留）")
        self.dedup_btn.clicked.connect(self._on_dedup)
        row.addWidget(self.dedup_btn)
        self.add_btn = QPushButton("新增口令", self)
        self.add_btn.setObjectName("primary")
        self.add_btn.setCursor(Qt.PointingHandCursor)
        self.add_btn.clicked.connect(self._on_add)
        row.addWidget(self.add_btn)
        return row

    # ---- 状态读取（宿主 / 测试） ----
    def rows(self):
        """全部行（不受搜索 / 筛选影响）。"""
        return [dict(r) for r in self._rows]

    def visible_rows(self):
        """当前表内可见行。"""
        return self.table.pw_model().rows()

    def keyword(self):
        return self._keyword

    def current_filter(self):
        return self._filter

    def current_mode(self):
        """当前视图模式：pwd（口令）| share（固定提取码）。"""
        return self._mode

    def share_rows(self):
        """固定提取码全部行（不受口令搜索 / 筛选影响）。"""
        return [dict(r) for r in self._share_rows]

    def stats(self):
        """页头口径统计：total / hit / miss / source / book / temp / dict。"""
        out = {"total": len(self._rows), "hit": 0, "miss": 0, "source": 0,
               "book": 0, "temp": 0, "dict": 0}
        for row in self._rows:
            if _hit_count(row) > 0:
                out["hit"] += 1
            else:
                out["miss"] += 1
            if str(row.get("source") or ""):
                out["source"] += 1
            kind = str(row.get("kind") or "")
            if kind in out:
                out[kind] += 1
        return out

    # ---- 装载 ----
    def reload(self, keep_view=False):
        """从数据层重读全部口令与固定提取码并重建行（保留仍可见的选中行）。

        keep_view=True 时额外保持竖向滚动位置（实时刷新用，绝不跳回顶部）。"""
        try:
            rows = self._data.rows()
        except Exception:
            rows = []
        self._rows = [dict(r) for r in rows if isinstance(r, dict)]
        self._apply_filters(keep_scroll=keep_view)
        self._reload_share()
        self._live_signature = self._data_signature()

    def _data_signature(self):
        """当前展示数据的廉价指纹：库内任何会改变表格内容的变化都会改变它。"""
        try:
            rows = self._data.rows()
        except Exception:
            rows = []
        out = []
        for rec in rows:
            if not isinstance(rec, dict):
                continue
            try:
                last = float(rec.get("last_hit") or 0)
            except Exception:
                last = 0.0
            out.append((rec.get("id"), str(rec.get("password") or ""),
                        str(rec.get("source") or ""), _hit_count(rec), last,
                        str(rec.get("note") or ""), str(rec.get("kind") or "")))
        return tuple(out)

    @staticmethod
    def _modal_open():
        """是否有本进程的模态对话框打开（有则实时刷新让路，避免动到弹窗下的数据）。"""
        try:
            return QApplication.activeModalWidget() is not None
        except Exception:
            return False

    def _live_tick(self):
        """实时刷新 tick：数据签名变了才重载（保留选中 / 滚动位置）。"""
        if self._modal_open():
            return
        try:
            sig = self._data_signature()
        except Exception:
            return
        if sig == self._live_signature:
            return
        self._reload_keep_view()

    def _reload_keep_view(self):
        """重载并恢复选中行（按口令）与竖向滚动位置（实时刷新专用）。"""
        passwords = []
        scroll = 0
        try:
            passwords = self.table.selected_passwords()
            scroll = int(self.table.verticalScrollBar().value())
        except Exception:
            pass
        self.reload(keep_view=True)
        try:
            self.table.select_passwords(passwords)
            self.table.verticalScrollBar().setValue(scroll)
        except Exception:
            pass

    def showEvent(self, event):
        """切回本页时立即校验一次数据（停留期间 QTimer 每 2s 校验）。"""
        super().showEvent(event)
        try:
            self._live_timer.start()
            self._live_tick()
        except Exception:
            pass

    def hideEvent(self, event):
        """离开本页时停表（页面不可见就不轮询；回到本页由 showEvent 立即补一次）。"""
        try:
            self._live_timer.stop()
        except Exception:
            pass
        super().hideEvent(event)

    def _reload_share(self):
        """重读固定提取码并重建表（保留空态与模式分段计数）。"""
        try:
            rows = self._share_data.rows()
        except Exception:
            rows = []
        self._share_rows = [dict(r) for r in rows if isinstance(r, dict)]
        self.share_table.set_rows(self._share_rows)
        self.share_count_lbl.setText("共 %d 条" % len(self._share_rows))
        self.share_empty.set_empty(not self._share_rows)
        self._update_mode_labels()

    def _update_mode_labels(self):
        """模式分段计数：口令 N / 固定提取码 N（与列表页分段计数同语气）。"""
        self.seg_mode.set_label("pwd", "口令 %d" % len(self._rows))
        self.seg_mode.set_label("share", "固定提取码 %d" % len(self._share_rows))

    def _sorted_rows(self, rows):
        """按当前列头排序状态对可见行做「仅显示层」重排（绝不改写库内顺序）。

        列头点击只在页面数据层重排本列表：解压尝试顺序只由库内 id 顺序决定。
        排序键：口令（大小写不敏感）/ 来源 / 命中次数（整数）/ 最近命中（原始
        时间戳）/ 备注；「从未命中」在升 / 降序都排最后；并列按口令作稳定兜底。"""
        col = self._sort_col
        order = self._sort_order
        if col not in (_PwModel.COL_PWD, _PwModel.COL_SRC, _PwModel.COL_HITS,
                       _PwModel.COL_LAST, _PwModel.COL_NOTE):
            return list(rows)
        out = sorted(rows, key=lambda r: str(r.get("password") or "").casefold())
        if col == _PwModel.COL_LAST:
            def _ts(rec):
                try:
                    return float(rec.get("last_hit") or 0)
                except Exception:
                    return 0.0

            fresh = [r for r in out if _ts(r) > 0]
            never = [r for r in out if _ts(r) <= 0]
            fresh.sort(key=_ts, reverse=(order == Qt.DescendingOrder))
            return fresh + never
        keys = {
            _PwModel.COL_PWD: lambda r: str(r.get("password") or "").casefold(),
            _PwModel.COL_SRC: lambda r: str(r.get("source") or "—").casefold(),
            _PwModel.COL_HITS: lambda r: _hit_count(r),
            _PwModel.COL_NOTE: lambda r: str(r.get("note") or "—").casefold(),
        }
        key = keys.get(col)
        if key is None:
            return out
        return sorted(out, key=key, reverse=(order == Qt.DescendingOrder))

    def _apply_filters(self, keep_scroll=False):
        base = [r for r in self._rows if _row_matches(r, self._keyword)]
        picked = [r for r in base if _passes_filter(r, self._filter)]
        picked = self._sorted_rows(picked)
        self._update_stats()
        self._update_seg_labels(base)
        keep = self.table.selected_passwords()          # 单选 / 多选都保留（按口令）
        self.table.set_rows(picked, keep_scroll=keep_scroll)
        if keep:
            self.table.select_passwords(keep)
        self.table_empty.set_empty(
            not picked, EMPTY_PWFILTER if self._rows else EMPTY_BOOK)

    def _update_stats(self):
        st = self.stats()
        self.total_label.setText("口令总数 %d" % st["total"])
        self.hit_label.setText("命中过 %d" % st["hit"])
        self.stats_label.setText(
            "长期 %d · 临时 %d · 字典 %d · 有来源 %d"
            % (st["book"], st["temp"], st["dict"], st["source"]))

    def _update_seg_labels(self, base):
        """分段计数按「搜索后的基础集」算（与日志页级别计数同口径）。"""
        counts = {key: 0 for key, _label in _FILTERS}
        counts["all"] = len(base)
        for row in base:
            if _hit_count(row) > 0:
                counts["hit"] += 1
            else:
                counts["miss"] += 1
            if str(row.get("source") or ""):
                counts["src"] += 1
        for key, label in _FILTERS:
            self.seg_filter.set_label(key, "%s %d" % (label, counts[key]))

    def _selected_password(self):
        row = self.table.selected_row()
        return str((row or {}).get("password") or "")

    # ---- 交互 ----
    def _on_mode(self, data):
        """模式分段：切换口令 / 固定提取码视图（不重查数据，重查由 reload 负责）。"""
        key = str(data or "pwd")
        self._mode = key if key in dict(_MODES) else "pwd"
        self.stack.setCurrentIndex(0 if self._mode == "pwd" else 1)

    def _on_search_changed(self, text):
        self._keyword = str(text).strip()
        self._apply_filters()

    def _on_filter_changed(self, data):
        key = str(data or "all")
        self._filter = key if key in dict(_FILTERS) else "all"
        self._apply_filters()

    def _on_header_clicked(self, section):
        """点击列头：按该列排序；再点同一列头切换升 / 降序（纯显示层，不写库）。"""
        col = int(section)
        if col == _PwModel.COL_ACT:
            return
        if col == self._sort_col:
            self._sort_order = (Qt.AscendingOrder
                                if self._sort_order == Qt.DescendingOrder
                                else Qt.DescendingOrder)
        else:
            self._sort_col = col
            # 数字列（命中次数）先看「多」的，文本列先看字母序
            self._sort_order = (Qt.DescendingOrder if col == _PwModel.COL_HITS
                                else Qt.AscendingOrder)
        self.table.set_sort_indicator(self._sort_col, self._sort_order)
        self._apply_filters()

    def _focus_search(self):
        """Ctrl+F：聚焦搜索框并全选（重复按 Ctrl+F 便于直接改关键词）。"""
        try:
            self.search.setFocus(Qt.ShortcutFocusReason)
            self.search.selectAll()
        except Exception:
            pass

    def _on_refresh(self):
        self.reload()
        self.notice.emit("密码本已刷新")

    def _copy_row(self, row_index):
        row = self.table.row_at(row_index) or {}
        password = str(row.get("password") or "")
        if not password:
            return
        try:
            QApplication.clipboard().setText(password)
        except Exception:
            self.notice.emit("复制失败：无法写入剪贴板")
            return
        self._toast_row(row_index, "已复制")
        self.notice.emit("已复制口令到剪贴板")

    def _copy_selected(self):
        """复制全部选中行（视图从上到下的顺序，每行一条）到系统剪贴板。"""
        passwords = self.table.selected_passwords()
        if len(passwords) < 2:
            return
        try:
            QApplication.clipboard().setText("\n".join(passwords))
        except Exception:
            self.notice.emit("复制失败：无法写入剪贴板")
            return
        self.notice.emit("已复制选中的 %d 条口令到剪贴板" % len(passwords))

    def _on_selection_changed(self, *_args):
        """选中数 >=2 时显示「复制选中 / 删除选中」；0/1 行或空页时隐藏。"""
        try:
            n = len(self.table.selected_rows_data())
        except Exception:
            n = 0
        show = n >= 2
        for btn in (getattr(self, "sel_copy_btn", None),
                    getattr(self, "sel_del_btn", None)):
            if btn is not None:
                try:
                    btn.setVisible(show)
                except Exception:
                    pass

    def _toast_row(self, row_index, text):
        """在对应行附近弹提示气泡（失败静默——提示绝不打断复制本身）。"""
        try:
            rect = self.table.visualRect(
                self.table.pw_model().index(int(row_index), _PwModel.COL_PWD))
            pos = self.table.viewport().mapToGlobal(rect.center())
            show_toast(self, pos, str(text))
        except Exception:
            pass

    def _ask_edit(self, title, current, note):
        """弹新增 / 编辑对话框（口令 + 备注）；取消返回 None（口令绝不进日志）。

        真实对话框接受备注；旧测试桩只接受 (parent, title, password) 三参，构造
        失败时退回三参并令 note=None（表示该数据源 / 对话框不支持备注）。
        批量导入（仅新增对话框提供）经返回值的 batch 字段传递，页面统一走
        与单条新增相同的 _PwData.add 入口写入。
        """
        try:
            dlg = _PasswordEditDialog(self, title, current, note)
            has_note = True
        except TypeError:
            dlg = _PasswordEditDialog(self, title, current)
            has_note = False
        if dlg.exec_() != QDialog.Accepted:
            return None
        out = {"password": str(dlg.password() or "").strip()}
        out["note"] = str(dlg.note()).strip() if has_note else None
        out["batch"] = None
        try:
            if has_note and dlg.is_batch():
                values, empty, dup = dlg.batch_values()
                out["batch"] = {"passwords": values, "empty": empty, "dup": dup}
        except Exception:
            out["batch"] = None
        return out

    def _on_add(self):
        result = self._ask_edit("新增口令", "", "")
        if result is None:
            return
        if result.get("batch"):
            self._batch_add(result["batch"], result.get("note") or "")
            return
        password = result["password"]
        if not password:
            return
        if password in self._data.book():
            QMessageBox.information(self, "新增口令", "该口令已在密码本中。")
            return
        if not self._data.add(password, result["note"] or ""):
            self.notice.emit("新增失败：无法写入密码本")
            return
        self.reload()
        self.changed.emit()
        self.notice.emit("已新增口令")

    def _batch_add(self, batch, note):
        """批量导入：逐条走单条新增的同一入口（_PwData.add），最后给出诚实回执。

        依赖 db.add_password 的 INSERT OR IGNORE + UNIQUE(password) 语义：即便
        预检查 / 计数有偏差，重复口令也只会被忽略、绝不覆盖已有行、绝不抛错。"""
        values = [str(p).strip() for p in (batch.get("passwords") or [])
                  if str(p).strip()]
        empty = int(batch.get("empty") or 0)
        dup_batch = int(batch.get("dup") or 0)
        existing = set(self._data.book())
        imported = 0
        dup = dup_batch
        failed = 0
        touched = False
        for p in values:
            if p in existing:
                dup += 1
                continue
            if self._data.add(p, note):
                existing.add(p)
                imported += 1
                touched = True
            else:
                failed += 1
        skipped = dup + empty + failed
        if touched:
            self.reload()
            self.changed.emit()
        self.notice.emit(
            "批量导入完成：新增 %d 条，跳过 %d 条（重复 %d / 空行 %d / 失败 %d）"
            % (imported, skipped, dup, empty, failed))

    def _edit_row(self, row_index):
        row = self.table.row_at(row_index) or {}
        if str(row.get("kind")) != "book":
            self.notice.emit("临时 / 字典口令不支持编辑")
            return
        old = str(row.get("password") or "")
        old_note = str(row.get("note") or "")
        result = self._ask_edit("编辑口令", old, old_note)
        if result is None:
            return
        new = result["password"]
        note = result["note"]                      # None = 该数据源不支持备注
        if not new:
            return
        if new != old and new in self._data.book():
            QMessageBox.information(self, "编辑口令", "该口令已在密码本中。")
            return
        if new == old and (note is None or note == old_note):
            return                                 # 口令与备注都没改
        if not self._data.save(row.get("id"), old, new, note):
            self.notice.emit("编辑失败：无法写入密码本")
            return
        self.reload()
        self.changed.emit()
        self.notice.emit("口令已更新" if new != old else "备注已更新")

    def _remove_temp(self, password):
        """移除一条临时（剪贴板）口令；state 不支持该接口（旧整表桩）时返回 False。"""
        try:
            remover = getattr(self.state, "remove_temp_password", None)
            if remover is None:
                return False
            return bool(remover(password))
        except Exception:
            return False

    def _delete_rows(self, rows):
        """按行 kind 逐行删除，返回 (deleted, skipped)。

        book -> 数据库按 id 精确删除；temp -> state.remove_temp_password（同步落盘
        剪贴板清单）；仅字典收录（dict）的行是派生数据，一律跳过。"""
        deleted = 0
        skipped = 0
        for row in rows or []:
            kind = str((row or {}).get("kind") or "")
            if kind == "book":
                if self._data.remove(row):
                    deleted += 1
                else:
                    skipped += 1
            elif kind == "temp":
                if self._remove_temp(str((row or {}).get("password") or "")):
                    deleted += 1
                else:
                    skipped += 1
            else:
                skipped += 1
        return deleted, skipped

    def _confirm_delete(self, rows):
        """批量删除确认（只说条数与后果，绝不携带明文口令）；确认返回 True。"""
        n_book = sum(1 for r in rows if str(r.get("kind")) == "book")
        n_temp = sum(1 for r in rows if str(r.get("kind")) == "temp")
        detail = []
        if n_book:
            detail.append("长期口令 %d 条（解压将不再尝试）" % n_book)
        if n_temp:
            detail.append("临时口令 %d 条（剪贴板捕获记录）" % n_temp)
        text = ("确定删除选中的 %d 条口令吗？\n%s\n"
                "删除只影响密码本 / 剪贴板记录，不会删除任何已解压的文件。"
                % (len(rows), " · ".join(detail)))
        answer = QMessageBox.question(
            self, "删除口令", text,
            QMessageBox.Yes | QMessageBox.No, QMessageBox.No)
        return answer == QMessageBox.Yes

    def _delete_row(self, row_index):
        """行内「删除」按钮：book 先二次确认；temp 是剪贴板临时记录，直接移除；
        dict 行（仅解压命中收录）拒绝删除。"""
        row = self.table.row_at(row_index) or {}
        kind = str(row.get("kind") or "")
        if kind == "book":
            answer = QMessageBox.question(
                self, "删除口令",
                "确定从密码本删除选中的口令吗？\n"
                "删除后解压将不再尝试该口令（不会删除任何已解压的文件）。",
                QMessageBox.Yes | QMessageBox.No, QMessageBox.No)
            if answer != QMessageBox.Yes:
                return
            deleted, _skipped = self._delete_rows([row])
            if deleted:
                self.reload()
                self.changed.emit()
                self.notice.emit("已删除口令")
            else:
                self.notice.emit("删除失败：无法写入密码本")
            return
        if kind == "temp":
            deleted, _skipped = self._delete_rows([row])
            if deleted:
                self.reload()
                self.changed.emit()
                self.notice.emit("已移除临时口令")
            else:
                self.notice.emit("删除失败：无法移除临时口令")
            return
        self.notice.emit("字典口令来自解压命中记录，不支持删除")

    def _delete_selected(self):
        """删除全部选中行（Delete 键与「删除选中」共用）：先确认，再逐行删除。

        长期行按 id 删、临时行从剪贴板记录移除；仅字典收录的行跳过并如实回执。"""
        rows = self.table.selected_rows_data()
        if not rows:
            self.notice.emit("请先选择要删除的口令")
            return
        deletable = [r for r in rows
                     if str(r.get("kind") or "") in ("book", "temp")]
        if not deletable:
            self.notice.emit("字典口令来自解压命中记录，不支持删除")
            return
        if not self._confirm_delete(deletable):
            return
        dict_skipped = len(rows) - len(deletable)
        deleted, skipped = self._delete_rows(deletable)
        skipped += dict_skipped
        if deleted:
            self.reload()
            self.changed.emit()
        if deleted and skipped:
            self.notice.emit("已删除 %d 条口令，跳过 %d 条（字典 / 无法写入）"
                             % (deleted, skipped))
        elif deleted:
            self.notice.emit("已删除 %d 条口令" % deleted)
        else:
            self.notice.emit("删除失败：无法写入密码本")

    # ---- 口令整理：查重（排序已改为点列头的纯视图重排，见 _on_header_clicked） ----
    def _on_dedup(self):
        """移除重复的长期口令（保留首次出现）。"""
        removed = self._data.dedup_book()
        if removed < 0:
            self.notice.emit("查重失败：无法写入密码本")
            return
        if removed == 0:
            self.notice.emit("未发现重复口令")
            return
        self.reload()
        self.changed.emit()
        self.notice.emit("已移除重复口令 %d 条" % removed)

    # ---- 固定提取码：行级增 / 改 / 删 + 批量文本编辑 ----
    def _ask_share_edit(self, title, share_uk, code, note, pick):
        """弹新增 / 编辑固定提取码对话框（UK + 提取码 + 需挑选 + 备注）；取消返回 None。

        提取码 / UK 绝不进入日志或回执——只在这两个对话框控件里出现。
        """
        try:
            dlg = _ShareEditDialog(self, title, share_uk, code, note, pick)
        except Exception:
            self.notice.emit("打开编辑框失败")
            return None
        if dlg.exec_() != QDialog.Accepted:
            return None
        return dlg.values()

    def _add_share(self):
        result = self._ask_share_edit("新增提取码", "", "", "", 0)
        if result is None:
            return
        uk, code, note, pick = result
        if not uk or not code:
            return
        if self._share_data.find(uk) is not None:
            QMessageBox.information(
                self, "新增提取码", "该分享者 UK 已有固定提取码，请改用「编辑」。")
            return
        if not self._share_data.add(uk, code, note, pick):
            self.notice.emit("新增失败：无法写入固定提取码")
            return
        self._reload_share()
        self.notice.emit("已新增固定提取码")

    def _edit_share(self, row_index):
        row = self.share_table.row_at(row_index) or {}
        old_uk = str(row.get("share_uk") or "")
        old_code = str(row.get("code") or "")
        old_note = str(row.get("note") or "")
        old_pick = 1 if _pick_on(row.get("pick")) else 0
        if not old_uk:
            return
        result = self._ask_share_edit("编辑提取码", old_uk, old_code, old_note, old_pick)
        if result is None:
            return
        uk, code, note, pick = result
        if not uk or not code:
            return
        if (uk == old_uk and code == old_code and note == old_note
                and pick == old_pick):
            return                                 # 什么都没改
        if uk != old_uk and self._share_data.find(uk) is not None:
            QMessageBox.information(self, "编辑提取码", "该分享者 UK 已有固定提取码。")
            return
        if not self._share_data.update(old_uk, new_share_uk=uk, code=code,
                                       note=note, pick=pick):
            self.notice.emit("保存失败：无法写入固定提取码")
            return
        self._reload_share()
        self.notice.emit("固定提取码已更新")

    def _delete_share(self, row_index):
        row = self.share_table.row_at(row_index) or {}
        uk = str(row.get("share_uk") or "")
        if not uk:
            return
        answer = QMessageBox.question(
            self, "删除固定提取码",
            "确定删除这条固定提取码吗？\n"
            "删除后遇到该分享者将不再自动填入提取码（不会删除任何文件）。",
            QMessageBox.Yes | QMessageBox.No, QMessageBox.No)
        if answer != QMessageBox.Yes:
            return
        if self._share_data.remove(uk):
            self._reload_share()
            self.notice.emit("已删除固定提取码")
        else:
            self.notice.emit("删除失败：无法写入固定提取码")

    def _batch_edit_share(self):
        """批量编辑（文本）：复用既有行格式的解析 / 格式化，保存走整表覆盖。"""
        try:
            dlg = _ShareTextDialog(self, self._share_rows)
        except Exception:
            self.notice.emit("打开批量编辑失败")
            return
        if dlg.exec_() != QDialog.Accepted:
            return
        if not self._share_data.replace_all(dlg.items()):
            self.notice.emit("保存失败：无法写入固定提取码")
            return
        self._reload_share()
        self.notice.emit("固定提取码已保存")

    def refresh_theme(self):
        """主题切换后重贴内联色（两张表的行内删除按钮 danger 色来自 PALETTE）。"""
        for table in (self.table, self.share_table):
            try:
                table.refresh_theme()
            except Exception:
                pass
