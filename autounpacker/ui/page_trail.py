# -*- coding: utf-8 -*-
"""删除回溯页（正式页）：记录表 + 搜索/原因/日期三滤镜 + 行内与批量操作。

职责：- TrailPage：展示 deletion_trail.json 里的删除回溯记录（时间/文件名/大小/
        来源任务/原因），并提供行内操作与批量操作
      - 筛选：全部 / 按原因（记录状态）/ 按日期（今天/昨天/更早）；
        搜索同时命中 文件名 与 原路径（大小写不敏感）
      - 行内操作：打开所在文件夹 / 复制原路径；
        批量操作：还原选中 / 导出 CSV（QFileDialog）/ 清空记录（强二次确认）
      - 空态：无记录与「筛选无结果」两种文案
关键入口：TrailPage / record_time() / day_key() / record_reason() /
          record_matches() / export_rows()
依赖：PyQt5、pages（复用 _EmptyOverlay/_ghost_button/_icon_button 家族控件）、
      widgets（SegControl/Glyph/状态文案与配色/_fmt_size）、style（PALETTE）、trail
注意：- 页面只经 trail 模块读写记录；清空 = trail.save_records([])，绝不直接删文件
      - 还原仅「已删除（回收站）」状态可用，且需用户确认；本页不起线程、不联网
      - 真实记录含绝对路径：页内展示（含 tooltip）没问题，但 notice 回执文案
        绝不携带路径（避免被宿主写进日志）
      - 数据装载可用 set_records()（宿主），reload() 从 trail 模块重读（自身刷新）
"""
import csv
import os
import time

from PyQt5.QtCore import QAbstractTableModel, QModelIndex, Qt, QTimer, pyqtSignal
from PyQt5.QtGui import QBrush, QColor, QFont
from PyQt5.QtWidgets import (QAbstractItemView, QApplication, QFileDialog,
                             QHBoxLayout, QHeaderView, QLabel, QLineEdit,
                             QMessageBox, QTableView, QVBoxLayout, QWidget)

from .. import trail as deletion_trail
from .pages import _EmptyOverlay, _ghost_button, _icon_button
from .style import PALETTE
from .widgets import (Glyph, SegControl, TRAIL_STATUS_COLORS,
                      TRAIL_STATUS_ORDER, TRAIL_STATUS_TEXT, _fmt_size)

# 空态文案（无记录 / 筛选无结果两种）
EMPTY_TRAIL = "暂无回溯记录 · 开启「解压成功后删除源文件」后，删除记录会出现在这里"
EMPTY_TRAIL_FILTERED = "没有匹配的记录 · 换个关键词或切换筛选试试"

# CSV 导出表头（可见列 + 真实原路径：导出后仍能定位文件）
CSV_HEADERS = ("时间", "文件名", "原路径", "大小", "状态", "原因")

# 原因分段 chip 的短文案（TRAIL_STATUS_TEXT 偏长，塞进分段会撑宽）
_STATUS_SHORT = {
    "recorded": "已记录", "kept": "未删除", "deleted": "已删除",
    "restored": "已还原", "failed": "解压失败",
}

# 日期分段：今天/昨天/更早（回溯窗口期 = 本次开机，跨零点时会出现「昨天」）
DAY_FILTERS = (("today", "今天"), ("yesterday", "昨天"), ("earlier", "更早"))


# ---------------------------------------------------------------------------
# 纯函数助手（无 Qt 依赖，离线测试可直接调用）
# ---------------------------------------------------------------------------

def _record_ts(rec):
    """记录时间戳：created_ts 优先，缺失时解析 created_at；都取不到返回 0。"""
    ts = rec.get("created_ts")
    if isinstance(ts, (int, float)) and ts > 0:
        return float(ts)
    try:
        return time.mktime(time.strptime(
            str(rec.get("created_at") or ""), "%Y-%m-%d %H:%M:%S"))
    except Exception:
        return 0.0


def record_time(rec):
    """时间列文案：created_at 优先；缺失时用 created_ts 现算，最后回退 '—'。"""
    text = str(rec.get("created_at") or "").strip()
    if text:
        return text
    ts = _record_ts(rec)
    if ts <= 0:
        return "—"
    try:
        return time.strftime("%Y-%m-%d %H:%M:%S", time.localtime(ts))
    except Exception:
        return "—"


def day_key(rec, now=None):
    """记录归入 today/yesterday/earlier 三桶（本地时区自然日；无时间归 earlier）。"""
    ts = _record_ts(rec)
    if ts <= 0:
        return "earlier"
    now = time.time() if now is None else float(now)
    lt = time.localtime(now)
    today0 = time.mktime((lt.tm_year, lt.tm_mon, lt.tm_mday, 0, 0, 0, 0, 0, -1))
    if ts >= today0:
        return "today"
    if ts >= today0 - 86400:
        return "yesterday"
    return "earlier"


def record_reason(rec):
    """原因列文案：状态词 + 备注；含永久删除文件时追加不可还原提示。"""
    status = str(rec.get("status") or "")
    text = TRAIL_STATUS_TEXT.get(status, status or "—")
    note = str(rec.get("note") or "").strip()
    if note:
        text += " · " + note
    if rec.get("failed_paths") and "无法还原" not in text:
        text += " · 含无法还原的文件"
    return text


def record_matches(rec, keyword="", status=None, day=None):
    """单条记录是否命中三滤镜：搜索（文件名/原路径）+ 原因 + 日期。"""
    kw = str(keyword or "").strip().lower()
    if kw:
        hay = "%s\n%s" % (rec.get("name") or "", rec.get("original_path") or "")
        if kw not in hay.lower():
            return False
    if status and str(rec.get("status") or "") != str(status):
        return False
    if day and day_key(rec) != str(day):
        return False
    return True


def export_rows(records):
    """记录 -> CSV 行（表头 + 数据行）；导出与测试共用同一口径。"""
    out = [list(CSV_HEADERS)]
    for rec in records or []:
        if not isinstance(rec, dict):
            continue
        status = str(rec.get("status") or "")
        out.append([
            record_time(rec),
            str(rec.get("name") or ""),
            str(rec.get("original_path") or ""),
            _fmt_size(rec.get("file_size")),
            TRAIL_STATUS_TEXT.get(status, status or ""),
            record_reason(rec),
        ])
    return out


# ---------------------------------------------------------------------------
# 记录表（QTableView + 自定义模型 + 行内操作）
# ---------------------------------------------------------------------------

class _TrailModel(QAbstractTableModel):
    """删除回溯表模型：时间/文件名/大小/来源任务/原因 + 操作列（UserRole = 记录 id）。"""

    HEADERS = ("时间", "文件名", "大小", "来源任务", "原因", "")
    (COL_TIME, COL_NAME, COL_SIZE, COL_SOURCE, COL_REASON, COL_ACT) = range(6)

    def __init__(self, parent=None):
        super().__init__(parent)
        self._rows = []

    def set_records(self, rows):
        self.beginResetModel()
        self._rows = [dict(r) for r in (rows or []) if isinstance(r, dict)]
        self.endResetModel()

    def records(self):
        return list(self._rows)

    def record_at(self, row):
        try:
            i = int(row)
        except Exception:
            return None
        if 0 <= i < len(self._rows):
            return dict(self._rows[i])
        return None

    def rowCount(self, parent=QModelIndex()):
        if parent.isValid():
            return 0
        return len(self._rows)

    def columnCount(self, parent=QModelIndex()):
        if parent.isValid():
            return 0
        return len(self.HEADERS)

    def headerData(self, section, orientation, role=Qt.DisplayRole):
        if orientation != Qt.Horizontal:
            return None
        if role == Qt.DisplayRole and 0 <= section < len(self.HEADERS):
            return self.HEADERS[section]
        if role == Qt.ToolTipRole and section == self.COL_SOURCE:
            # 记录不含任务号：如实说明该列展示的是源文件所在的监听目录
            return "记录不含任务号：这里显示源文件所在的监听目录"
        return None

    def flags(self, index):
        if not index.isValid():
            return Qt.NoItemFlags
        return Qt.ItemIsEnabled | Qt.ItemIsSelectable

    def data(self, index, role=Qt.DisplayRole):
        if not index.isValid() or not (0 <= index.row() < len(self._rows)):
            return None
        rec = self._rows[index.row()]
        col = index.column()
        if role == Qt.UserRole:
            return rec.get("id")
        if role == Qt.DisplayRole:
            return self._display(rec, col)
        if role == Qt.ToolTipRole:
            if col == self.COL_NAME:
                return str(rec.get("original_path") or rec.get("name") or "")
            if col == self.COL_SOURCE:
                return str(rec.get("watch_dir") or "")
            if col == self.COL_REASON:
                return record_reason(rec)
            return None
        if role == Qt.TextAlignmentRole and col == self.COL_SIZE:
            return int(Qt.AlignRight | Qt.AlignVCenter)
        if role == Qt.ForegroundRole and col == self.COL_REASON:
            return QBrush(QColor(TRAIL_STATUS_COLORS.get(
                str(rec.get("status") or ""), PALETTE["muted"])))
        if role == Qt.FontRole and col == self.COL_REASON:
            font = QFont()
            font.setBold(True)
            return font
        return None

    def _display(self, rec, col):
        if col == self.COL_TIME:
            return record_time(rec)
        if col == self.COL_NAME:
            return str(rec.get("name") or "")
        if col == self.COL_SIZE:
            return _fmt_size(rec.get("file_size"))
        if col == self.COL_SOURCE:
            return str(rec.get("watch_dir") or "—")
        if col == self.COL_REASON:
            return record_reason(rec)
        return ""


class _TrailTable(QTableView):
    """删除回溯表：_TrailModel + 行内操作按钮（打开所在文件夹 / 复制原路径）。

    行内按钮只发信号（携带原路径），由页面决定真正动作（便于测试与宿主替换）。
    """

    openRequested = pyqtSignal(str)
    copyRequested = pyqtSignal(str)

    def __init__(self, parent=None):
        super().__init__(parent)
        self.setObjectName("taskTable")        # 复用任务表 QSS，同一视觉语言
        self._model = _TrailModel(self)
        self.setModel(self._model)
        self.setSelectionBehavior(QAbstractItemView.SelectRows)
        self.setSelectionMode(QAbstractItemView.SingleSelection)
        self.setEditTriggers(QAbstractItemView.NoEditTriggers)
        self.setAlternatingRowColors(True)
        self.setShowGrid(False)
        self.setWordWrap(False)
        self.setTextElideMode(Qt.ElideMiddle)
        self.verticalHeader().setVisible(False)
        self.verticalHeader().setDefaultSectionSize(40)
        hh = self.horizontalHeader()
        hh.setHighlightSections(False)
        hh.setStretchLastSection(False)
        hh.setSectionResizeMode(_TrailModel.COL_NAME, QHeaderView.Stretch)
        hh.setSectionResizeMode(_TrailModel.COL_REASON, QHeaderView.Stretch)
        for col, width in ((_TrailModel.COL_TIME, 150), (_TrailModel.COL_SIZE, 80),
                           (_TrailModel.COL_SOURCE, 190), (_TrailModel.COL_ACT, 150)):
            hh.setSectionResizeMode(col, QHeaderView.Fixed)
            self.setColumnWidth(col, width)
        self._action_widgets = []

    def set_records(self, rows):
        self._clear_actions()
        self._model.set_records(rows)
        self._build_actions()
        self.scroll_to_top()

    def trail_model(self):
        return self._model

    def record_at(self, row):
        return self._model.record_at(row)

    def current_row(self):
        try:
            return int(self.currentIndex().row())
        except Exception:
            return -1

    def _build_actions(self):
        for row in range(self._model.rowCount()):
            widget = self._make_action_widget(self._model.record_at(row) or {})
            self.setIndexWidget(self._model.index(row, _TrailModel.COL_ACT), widget)
            self._action_widgets.append(widget)

    def _make_action_widget(self, rec):
        path = str(rec.get("original_path") or "")
        w = QWidget(self)
        lay = QHBoxLayout(w)
        lay.setContentsMargins(0, 0, 6, 0)
        lay.setSpacing(4)
        lay.addStretch(1)
        open_btn = _ghost_button("打开", w)
        open_btn.setToolTip("打开所在文件夹")
        open_btn.setEnabled(bool(path))
        open_btn.clicked.connect(lambda _=False, p=path: self.openRequested.emit(p))
        lay.addWidget(open_btn)
        copy_btn = _ghost_button("复制路径", w)
        copy_btn.setToolTip("复制原路径")
        copy_btn.setEnabled(bool(path))
        copy_btn.clicked.connect(lambda _=False, p=path: self.copyRequested.emit(p))
        lay.addWidget(copy_btn)
        return w

    def _clear_actions(self):
        for w in self._action_widgets:
            try:
                w.setParent(None)
                w.deleteLater()
            except Exception:
                pass
        self._action_widgets = []

    def scroll_to_top(self):
        try:
            self.verticalScrollBar().setValue(0)
        except Exception:
            pass


# ---------------------------------------------------------------------------
# 删除回溯页
# ---------------------------------------------------------------------------

class TrailPage(QWidget):
    """删除回溯页：记录表 + 搜索/原因/日期三滤镜 + 行内与批量操作。

    只持有状态与控件；记录装载由宿主 set_records() 或页面自身 reload() 完成。
    信号：notice(text)——用户可见回执（绝不携带真实路径）。
    """

    notice = pyqtSignal(str)

    def __init__(self, parent=None):
        super().__init__(parent)
        self._records = []
        self._visible = []
        self._mode = "all"          # all | reason | date
        self._reason = "deleted"    # 原因分段当前值（mode=reason 时生效）
        self._day = "today"         # 日期分段当前值（mode=date 时生效）

        root = QVBoxLayout(self)
        root.setContentsMargins(12, 12, 12, 10)
        root.setSpacing(10)

        # 第一行：标题 + 记录条数 + 搜索 + 刷新
        row = QHBoxLayout()
        row.setSpacing(8)
        row.addWidget(Glyph("history", self, 16, role="accent"))
        self.title_label = QLabel("删除回溯", self)
        self.title_label.setObjectName("appTitle")
        row.addWidget(self.title_label)
        self.count_label = QLabel("共 0 条", self)
        self.count_label.setObjectName("stripHint")
        row.addWidget(self.count_label)
        row.addStretch(1)
        self.search = QLineEdit(self)
        self.search.setPlaceholderText("搜索文件名 / 原路径")
        self.search.setFixedWidth(230)
        self._search_timer = QTimer(self)
        self._search_timer.setSingleShot(True)
        self._search_timer.setInterval(250)
        self._search_timer.timeout.connect(self.apply_filters)
        self.search.textChanged.connect(lambda _t: self._search_timer.start())
        row.addWidget(self.search)
        self.refresh_btn = _icon_button("refresh", "刷新", self)
        self.refresh_btn.clicked.connect(self.reload)
        row.addWidget(self.refresh_btn)
        root.addLayout(row)

        # 记录范围说明（与旧弹窗的引导文案同义）
        self.guide_label = QLabel(
            "这里只记录最初始的源文件（多层解压产生的中间文件不记录）。"
            "解压后删除的源文件先移入回收站，仅「已删除（回收站）」状态可还原；"
            "回收站被清空后无法还原。", self)
        self.guide_label.setObjectName("stripHint")
        self.guide_label.setWordWrap(True)
        root.addWidget(self.guide_label)

        # 第二行：筛选分段（全部 / 按原因 / 按日期，按需显示细分段）+ 批量操作
        frow = QHBoxLayout()
        frow.setSpacing(8)
        self.seg_mode = SegControl(self)
        self.seg_mode.set_items([("全部 0", "all"), ("按原因", "reason"),
                                 ("按日期", "date")])
        self.seg_mode.currentChanged.connect(self._on_mode)
        frow.addWidget(self.seg_mode)
        self.seg_reason = SegControl(self)
        self.seg_reason.set_items([(_STATUS_SHORT.get(k, k), k)
                                   for k in TRAIL_STATUS_ORDER])
        self.seg_reason.currentChanged.connect(self._on_reason)
        self.seg_reason.setVisible(False)
        frow.addWidget(self.seg_reason)
        self.seg_day = SegControl(self)
        self.seg_day.set_items([(text, key) for key, text in DAY_FILTERS])
        self.seg_day.currentChanged.connect(self._on_day)
        self.seg_day.setVisible(False)
        frow.addWidget(self.seg_day)
        frow.addStretch(1)
        self.restore_btn = _ghost_button("还原选中", self)
        self.restore_btn.setEnabled(False)
        self.restore_btn.clicked.connect(self._on_restore)
        frow.addWidget(self.restore_btn)
        self.export_btn = _ghost_button("导出 CSV", self)
        self.export_btn.clicked.connect(self._on_export)
        frow.addWidget(self.export_btn)
        self.clear_btn = _ghost_button("清空记录", self)
        self.clear_btn.setObjectName("danger")
        self.clear_btn.clicked.connect(self._on_clear)
        frow.addWidget(self.clear_btn)
        root.addLayout(frow)

        # 记录表 + 空态
        self.table = _TrailTable(self)
        self.table.openRequested.connect(self._on_open)
        self.table.copyRequested.connect(self._on_copy)
        root.addWidget(self.table, 1)
        self.table_empty = _EmptyOverlay(self.table, EMPTY_TRAIL)
        sm = self.table.selectionModel()
        if sm is not None:
            sm.currentRowChanged.connect(lambda *_a: self._sync_restore_button())
        self._refresh_counts()
        self.reload()

    # ---- 状态读取 ----
    def keyword(self):
        return str(self.search.text()).strip()

    def visible_records(self):
        return [dict(r) for r in self._visible]

    def mode(self):
        return self._mode

    def reason_filter(self):
        return self._reason

    def day_filter(self):
        return self._day

    # ---- 宿主装载入口 / 自身刷新 ----
    def set_records(self, rows):
        """装载记录（dict 列表）并整体刷新；宿主与页面 reload() 共用。"""
        self._records = [dict(r) for r in (rows or []) if isinstance(r, dict)]
        self._refresh_counts()
        self.apply_filters()

    def reload(self):
        """从 trail 模块重读记录并刷新（刷新按钮 / 还原 / 清空后调用）。"""
        try:
            rows = deletion_trail.load_records()
        except Exception:
            rows = []
        self.set_records(rows)

    def refresh_theme(self):
        """主题切换后重绘表格（状态色在 data() 里现取调色板，重绘即生效）。"""
        try:
            self.table.viewport().update()
        except Exception:
            pass

    # ---- 筛选（程序化入口；供宿主与测试使用，不发信号） ----
    def set_keyword(self, text):
        """程序化设置搜索词并立即过滤（不走 250ms 防抖）。"""
        self.search.setText(str(text or ""))
        self.apply_filters()

    def set_mode(self, mode):
        """程序化切换筛选模式（all/reason/date），走与点击同一状态更新。"""
        mode = str(mode)
        if mode not in ("all", "reason", "date"):
            mode = "all"
        self.seg_mode.set_current(mode)
        self._on_mode(mode)

    def set_reason(self, status):
        """程序化选择原因（仅在 mode=reason 时参与过滤）。"""
        status = str(status)
        self.seg_reason.set_current(status)
        self._on_reason(status)

    def set_day(self, key):
        """程序化选择日期桶（today/yesterday/earlier）。"""
        key = str(key)
        self.seg_day.set_current(key)
        self._on_day(key)

    def apply_filters(self):
        """按「模式（原因/日期）+ 搜索」重算可见记录，刷新表格与空态。"""
        status = self._reason if self._mode == "reason" else None
        day = self._day if self._mode == "date" else None
        rows = [r for r in self._records
                if record_matches(r, self.keyword(), status, day)]
        self._visible = rows
        self.table.set_records(rows)
        if not self._records:
            self.table_empty.set_empty(True, EMPTY_TRAIL)
        else:
            self.table_empty.set_empty(not rows, EMPTY_TRAIL_FILTERED)
        self._sync_restore_button()

    # ---- 内部 ----
    def _refresh_counts(self):
        counts = {}
        days = {}
        for rec in self._records:
            st = str(rec.get("status") or "")
            counts[st] = counts.get(st, 0) + 1
            dk = day_key(rec)
            days[dk] = days.get(dk, 0) + 1
        self.count_label.setText("共 %d 条" % len(self._records))
        self.seg_mode.set_label("all", "全部 %d" % len(self._records))
        for st in TRAIL_STATUS_ORDER:
            self.seg_reason.set_label(
                st, "%s %d" % (_STATUS_SHORT.get(st, st), counts.get(st, 0)))
        for key, text in DAY_FILTERS:
            self.seg_day.set_label(key, "%s %d" % (text, days.get(key, 0)))

    def _on_mode(self, data):
        self._mode = str(data) if str(data) in ("reason", "date") else "all"
        self.seg_reason.setVisible(self._mode == "reason")
        self.seg_day.setVisible(self._mode == "date")
        self.apply_filters()

    def _on_reason(self, data):
        self._reason = str(data)
        self.apply_filters()

    def _on_day(self, data):
        self._day = str(data)
        self.apply_filters()

    def _selected_record(self):
        row = self.table.current_row()
        if row < 0:
            return None
        return self.table.record_at(row)

    def _sync_restore_button(self):
        rec = self._selected_record()
        ok = bool(rec) and str(rec.get("status") or "") == "deleted"
        self.restore_btn.setEnabled(ok)

    # ---- 行内操作 ----
    def _on_open(self, path):
        """打开记录文件所在文件夹（与主窗 _open_task_dir 同一 os.startfile 入口）。"""
        folder = os.path.dirname(str(path or ""))
        if not folder or not os.path.isdir(folder):
            self.notice.emit("原文件夹不存在，无法打开")
            return
        try:
            os.startfile(folder)         # 仅 Windows；本程序只支持 Windows
        except Exception:
            self.notice.emit("打开文件夹失败")
            return
        self.notice.emit("已打开所在文件夹")

    def _on_copy(self, path):
        """复制原路径到剪贴板；回执文案绝不携带真实路径。"""
        text = str(path or "")
        if not text:
            self.notice.emit("该记录没有原路径")
            return
        try:
            QApplication.clipboard().setText(text)
        except Exception:
            self.notice.emit("复制失败")
            return
        self.notice.emit("已复制原路径到剪贴板")

    # ---- 批量操作 ----
    def _on_restore(self):
        """还原选中记录：仅「已删除」可还原，确认后经 trail.restore_record 还原。"""
        rec = self._selected_record()
        if rec is None:
            QMessageBox.information(self, "删除回溯", "请先选中一条记录")
            return
        if str(rec.get("status") or "") != "deleted":
            QMessageBox.information(
                self, "删除回溯", "只有「已删除（回收站）」状态的记录可以还原")
            return
        if QMessageBox.question(
                self, "删除回溯", "从回收站还原所选文件？\n文件将回到原位置。",
                QMessageBox.Yes | QMessageBox.No) != QMessageBox.Yes:
            return
        try:
            ok, msg = deletion_trail.restore_record(str(rec.get("id") or ""))
        except Exception as e:
            ok, msg = False, str(e)
        self.reload()
        if ok:
            self.notice.emit(str(msg))
        QMessageBox.information(
            self, "还原结果", str(msg) if ok else "还原失败\n%s" % msg)

    def _on_export(self):
        """导出当前可见记录为 CSV（QFileDialog 选路径；UTF-8 BOM 便于 Excel 打开）。"""
        try:
            default = time.strftime("autounpacker-trail-%Y%m%d-%H%M%S.csv")
            path, _sel = QFileDialog.getSaveFileName(
                self, "导出删除回溯", default, "CSV 文件 (*.csv)")
            if not path:
                return
            rows = export_rows(self._visible)
            with open(path, "w", encoding="utf-8-sig", newline="") as f:
                csv.writer(f).writerows(rows)
            self.notice.emit("已导出 %d 条记录到 CSV 文件" % max(0, len(rows) - 1))
        except Exception as e:
            try:
                QMessageBox.warning(self, "导出删除回溯", "导出失败：%s" % e)
            except Exception:
                pass

    def _on_clear(self):
        """清空全部回溯记录：连续两次确认（强二次确认）后经 trail 写空。"""
        n = len(self._records)
        if n <= 0:
            self.notice.emit("当前没有可清空的记录")
            return
        if QMessageBox.question(
                self, "清空记录",
                "确定清空全部 %d 条删除回溯记录？\n（不影响回收站里的文件）" % n,
                QMessageBox.Yes | QMessageBox.No) != QMessageBox.Yes:
            return
        if QMessageBox.warning(
                self, "再次确认",
                "清空后本页记录将全部消失且无法恢复（回收站里的文件不受影响），"
                "确定继续？",
                QMessageBox.Yes | QMessageBox.No, QMessageBox.No) != QMessageBox.Yes:
            return
        try:
            deletion_trail.save_records([])
        except Exception as e:
            try:
                QMessageBox.warning(self, "清空记录", "清空失败：%s" % e)
            except Exception:
                pass
            return
        self.reload()
        self.notice.emit("已清空删除回溯记录")
