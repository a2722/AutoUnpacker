# -*- coding: utf-8 -*-
"""DeleteTrailDialog：删除回溯记录查看 + 从回收站一键还原。"""
from PyQt5.QtWidgets import (QVBoxLayout, QHBoxLayout, QLabel, QPushButton,
                             QMessageBox, QDialog, QTableWidget,
                             QTableWidgetItem, QHeaderView, QAbstractItemView)
from PyQt5.QtCore import Qt
from PyQt5.QtGui import QColor, QBrush

from ... import trail as deletion_trail
from ..style import PALETTE
from ..widgets import TRAIL_STATUS_COLORS
from .common import TRAIL_STATUS_TEXT, TRAIL_STATUS_ORDER


class DeleteTrailDialog(QDialog):
    """删除回溯：查看初始源文件记录，并从回收站还原已删除的源文件"""

    def __init__(self, parent=None):
        super().__init__(parent)
        self.setWindowTitle("删除回溯")
        self.resize(820, 520)

        lay = QVBoxLayout(self)
        lay.setContentsMargins(16, 14, 16, 14)
        lay.setSpacing(10)

        title = QLabel("删除回溯")
        title.setObjectName("appTitle")
        lay.addWidget(title)

        # 状态概览（彩色统计）
        self.stat_lbl = QLabel()
        self.stat_lbl.setObjectName("statcard")
        self.stat_lbl.setTextFormat(Qt.RichText)
        self.stat_lbl.setWordWrap(True)
        self.stat_lbl.setContentsMargins(12, 8, 12, 8)
        lay.addWidget(self.stat_lbl)

        guide = QLabel(
            "这里只记录最初始的源文件（多层解压产生的次级中间文件不会记录）。\n"
            "解压后删除的源文件先移入回收站，选中「已删除」记录可一键还原；"
            "回收站被清空后则无法还原。"
        )
        guide.setWordWrap(True)
        guide.setStyleSheet(f"color: {PALETTE['muted']}; font-size: 12px;")
        lay.addWidget(guide)

        self.table = QTableWidget(0, 4)
        self.table.setHorizontalHeaderLabels(["时间", "源文件", "状态", "说明"])
        self.table.setEditTriggers(QAbstractItemView.NoEditTriggers)
        self.table.setSelectionBehavior(QAbstractItemView.SelectRows)
        self.table.setSelectionMode(QAbstractItemView.SingleSelection)
        self.table.setAlternatingRowColors(True)
        self.table.verticalHeader().setVisible(False)
        self.table.verticalHeader().setDefaultSectionSize(30)
        self.table.horizontalHeader().setSectionResizeMode(1, QHeaderView.Stretch)
        self.table.horizontalHeader().setSectionResizeMode(3, QHeaderView.Stretch)
        lay.addWidget(self.table, 1)

        btns = QHBoxLayout()
        refresh_btn = QPushButton("刷新")
        refresh_btn.clicked.connect(self._refresh)
        restore_btn = QPushButton("还原选中")
        restore_btn.setObjectName("primary")
        restore_btn.clicked.connect(self._restore_selected)
        clear_btn = QPushButton("清空记录")
        clear_btn.setObjectName("danger")
        clear_btn.clicked.connect(self._clear_records)
        close_btn = QPushButton("关闭")
        close_btn.clicked.connect(self.accept)
        btns.addWidget(refresh_btn)
        btns.addWidget(restore_btn)
        btns.addStretch(1)
        btns.addWidget(clear_btn)
        btns.addWidget(close_btn)
        lay.addLayout(btns)

        self._refresh()

    @staticmethod
    def _status_item(status):
        """状态列：加粗 + 状态色 + 圆点，提升辨识度"""
        text = TRAIL_STATUS_TEXT.get(status, status or "—")
        color = TRAIL_STATUS_COLORS.get(status, "#555")
        item = QTableWidgetItem(f"● {text}")
        item.setForeground(QBrush(QColor(color)))
        f = item.font()
        f.setBold(True)
        item.setFont(f)
        return item

    def _refresh(self):
        self._records = deletion_trail.load_records()
        # 状态概览
        counts = {}
        for rec in self._records:
            st = rec.get("status", "")
            counts[st] = counts.get(st, 0) + 1
        parts = [f"共 <b>{len(self._records)}</b> 条"]
        for st in TRAIL_STATUS_ORDER:
            n = counts.get(st, 0)
            if n:
                color = TRAIL_STATUS_COLORS.get(st, "#555")
                label = TRAIL_STATUS_TEXT.get(st, st)
                parts.append(f'<span style="color:{color};font-weight:bold;">{label} {n}</span>')
        self.stat_lbl.setText("　·　".join(parts))
        # 表格
        self.table.setRowCount(len(self._records))
        for row, rec in enumerate(self._records):
            self.table.setItem(row, 0, QTableWidgetItem(rec.get("created_at", "")))
            name = QTableWidgetItem(rec.get("name", ""))
            self.table.setItem(row, 1, name)
            status = rec.get("status", "")
            self.table.setItem(row, 2, self._status_item(status))
            note = rec.get("note", "")
            if rec.get("failed_paths"):
                note = (note + " " if note else "") + "含无法还原的文件"
            self.table.setItem(row, 3, QTableWidgetItem(note))
        self.table.horizontalHeader().setSectionResizeMode(0, QHeaderView.ResizeToContents)
        self.table.horizontalHeader().setSectionResizeMode(2, QHeaderView.ResizeToContents)

    def _selected_record(self):
        row = self.table.currentRow()
        if 0 <= row < len(self._records):
            return self._records[row]
        return None

    def _restore_selected(self):
        rec = self._selected_record()
        if rec is None:
            QMessageBox.information(self, "删除回溯", "请先选中一条记录")
            return
        if rec.get("status") != "deleted":
            QMessageBox.information(self, "删除回溯", "只有「已删除（回收站）」状态的记录可以还原")
            return
        ok, msg = deletion_trail.restore_record(rec["id"])
        QMessageBox.information(
            self, "还原结果", msg if ok else f"还原失败\n{msg}")
        self._refresh()

    def _clear_records(self):
        if QMessageBox.question(
                self, "删除回溯", "确定清空所有回溯记录？\n（不影响回收站里的文件）",
                QMessageBox.Yes | QMessageBox.No) != QMessageBox.Yes:
            return
        deletion_trail.save_records([])
        self._refresh()
