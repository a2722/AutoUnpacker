# -*- coding: utf-8 -*-
"""口令表 / 固定提取码表的私有数据模型（阶段6e 自 ui/page_pwbook.py 纯搬移）。"""
from PyQt5.QtCore import QAbstractTableModel, Qt

from .data import _fmt_hit_time, _hit_count, _pick_on

# 「口令」列 tooltip 的明文显示上限（字符数）：口令超过上限才中间省略
# （头 60 + "…" + 尾 59，恰好等于上限），既保证正常口令（含 100+ 字符的随机串 /
# 短语）完整可读，又不让异常超长内容把 tooltip 撑爆。
# 注意：用户已刻意放宽旧的「tooltip 不含明文」约束——口令列 hover 明文展示。
_PWD_TOOLTIP_MAX = 120


def _pwd_tooltip(password):
    """「口令」列 tooltip：口令明文 + 双击复制提示；仅超长时中间省略（纯函数）。"""
    text = str(password or "")
    if len(text) > _PWD_TOOLTIP_MAX:
        head = _PWD_TOOLTIP_MAX // 2            # 60：保留头部
        tail = _PWD_TOOLTIP_MAX - head - 1      # 59：保留尾部（连同 "…" 恰好等于上限）
        text = text[:head] + "…" + text[-tail:]
    return "口令：%s  ·  双击可复制" % text


# ---------------------------------------------------------------------------
# 口令表：模型 / 委托 / 视图
# ---------------------------------------------------------------------------

class _PwModel(QAbstractTableModel):
    """口令表数据模型：列 口令 / 来源 / 命中次数 / 最近命中 / 备注 / 行操作。

    口令列直接显示原文（不遮蔽）；UserRole = 口令（显示 / 复制用），行身份取行字典的 id。
    口令列 hover tooltip 同样明文展示（「口令：<明文>  ·  双击可复制」，仅超长时按
    _PWD_TOOLTIP_MAX 中间省略）；其余列的 tooltip 仍是说明性文案。
    """

    HEADERS = ("口令", "来源", "命中次数", "最近命中", "备注", "")
    (COL_PWD, COL_SRC, COL_HITS, COL_LAST, COL_NOTE, COL_ACT) = range(6)

    # 每列内容对齐：单元格与表头文字共用同一口径（表头文字才能与单元格文字对齐）
    _ALIGN = {
        COL_PWD: Qt.AlignLeft | Qt.AlignVCenter,
        COL_SRC: Qt.AlignLeft | Qt.AlignVCenter,
        COL_HITS: Qt.AlignRight | Qt.AlignVCenter,
        COL_LAST: Qt.AlignLeft | Qt.AlignVCenter,
        COL_NOTE: Qt.AlignLeft | Qt.AlignVCenter,
        COL_ACT: Qt.AlignLeft | Qt.AlignVCenter,
    }

    def __init__(self, parent=None):
        super().__init__(parent)
        self._rows = []

    def set_rows(self, rows):
        self.beginResetModel()
        self._rows = [dict(r) for r in (rows or []) if isinstance(r, dict)]
        self.endResetModel()

    def rows(self):
        return [dict(r) for r in self._rows]

    def row_at(self, row):
        try:
            i = int(row)
        except Exception:
            return None
        if 0 <= i < len(self._rows):
            return dict(self._rows[i])
        return None

    def rowCount(self, parent=None):
        if parent is not None and parent.isValid():
            return 0
        return len(self._rows)

    def columnCount(self, parent=None):
        if parent is not None and parent.isValid():
            return 0
        return len(self.HEADERS)

    def headerData(self, section, orientation, role=Qt.DisplayRole):
        if orientation != Qt.Horizontal:
            return None
        if role == Qt.TextAlignmentRole:
            return int(self._ALIGN.get(section, Qt.AlignLeft | Qt.AlignVCenter))
        if role != Qt.DisplayRole:
            return None
        if 0 <= section < len(self.HEADERS):
            return self.HEADERS[section]
        return None

    def flags(self, index):
        if not index.isValid():
            return Qt.NoItemFlags
        return Qt.ItemIsEnabled | Qt.ItemIsSelectable

    def _display(self, row, col):
        if col == self.COL_PWD:
            return str(row.get("password") or "")
        if col == self.COL_SRC:
            return str(row.get("source") or "—")
        if col == self.COL_HITS:
            return str(_hit_count(row))
        if col == self.COL_LAST:
            return _fmt_hit_time(row.get("last_hit"))
        if col == self.COL_NOTE:
            return str(row.get("note") or "—")
        return ""

    def data(self, index, role=Qt.DisplayRole):
        if not index.isValid() or not (0 <= index.row() < len(self._rows)):
            return None
        row = self._rows[index.row()]
        col = index.column()
        if role == Qt.UserRole:
            return row.get("password")
        if role == Qt.DisplayRole:
            return self._display(row, col)
        if role == Qt.ToolTipRole:
            if col == self.COL_PWD:
                return _pwd_tooltip(row.get("password"))
            if col == self.COL_SRC:
                return "来源：%s" % (row.get("source") or "仅解压字典收录（无主动来源）")
            if col == self.COL_HITS:
                return "解压时命中次数：%d" % _hit_count(row)
            if col == self.COL_LAST:
                return "%s（最近一次命中）" % _fmt_hit_time(row.get("last_hit"))
            if col == self.COL_NOTE:
                note = str(row.get("note") or "")
                if note:
                    return "备注：%s" % note
                return "备注为空 · 点「编辑」可添加备注"
            return None
        if role == Qt.TextAlignmentRole:
            return int(self._ALIGN.get(col, Qt.AlignLeft | Qt.AlignVCenter))
        return None


# ---------------------------------------------------------------------------
# 固定提取码表：模型 / 视图
# ---------------------------------------------------------------------------

class _ShareModel(QAbstractTableModel):
    """固定提取码表数据模型：列 分享者UK / 提取码 / 需挑选 / 备注 / 行操作。

    行身份取行字典的 share_uk（库主键）；提取码按用户预期明文展示（旧弹窗也明文），
    但本模块绝不把它写进任何日志 / 回执 / tooltip 之外的输出。
    """

    HEADERS = ("分享者UK", "提取码", "需挑选", "备注", "")
    (COL_UK, COL_CODE, COL_PICK, COL_NOTE, COL_ACT) = range(5)

    # 每列内容对齐：单元格与表头文字共用同一口径（表头文字才能与单元格文字对齐）
    _ALIGN = {
        COL_UK: Qt.AlignLeft | Qt.AlignVCenter,
        COL_CODE: Qt.AlignLeft | Qt.AlignVCenter,
        COL_PICK: Qt.AlignCenter,
        COL_NOTE: Qt.AlignLeft | Qt.AlignVCenter,
        COL_ACT: Qt.AlignLeft | Qt.AlignVCenter,
    }

    def __init__(self, parent=None):
        super().__init__(parent)
        self._rows = []

    def set_rows(self, rows):
        self.beginResetModel()
        self._rows = [dict(r) for r in (rows or []) if isinstance(r, dict)]
        self.endResetModel()

    def rows(self):
        return [dict(r) for r in self._rows]

    def row_at(self, row):
        try:
            i = int(row)
        except Exception:
            return None
        if 0 <= i < len(self._rows):
            return dict(self._rows[i])
        return None

    def rowCount(self, parent=None):
        if parent is not None and parent.isValid():
            return 0
        return len(self._rows)

    def columnCount(self, parent=None):
        if parent is not None and parent.isValid():
            return 0
        return len(self.HEADERS)

    def headerData(self, section, orientation, role=Qt.DisplayRole):
        if orientation != Qt.Horizontal:
            return None
        if role == Qt.TextAlignmentRole:
            return int(self._ALIGN.get(section, Qt.AlignLeft | Qt.AlignVCenter))
        if role != Qt.DisplayRole:
            return None
        if 0 <= section < len(self.HEADERS):
            return self.HEADERS[section]
        return None

    def flags(self, index):
        if not index.isValid():
            return Qt.NoItemFlags
        return Qt.ItemIsEnabled | Qt.ItemIsSelectable

    def _display(self, row, col):
        if col == self.COL_UK:
            return str(row.get("share_uk") or "")
        if col == self.COL_CODE:
            return str(row.get("code") or "")
        if col == self.COL_PICK:
            return "是" if _pick_on(row.get("pick")) else "—"
        if col == self.COL_NOTE:
            return str(row.get("note") or "—")
        return ""

    def data(self, index, role=Qt.DisplayRole):
        if not index.isValid() or not (0 <= index.row() < len(self._rows)):
            return None
        row = self._rows[index.row()]
        col = index.column()
        if role == Qt.DisplayRole:
            return self._display(row, col)
        if role == Qt.ToolTipRole:
            if col == self.COL_UK:
                return "分享者UK：分享页上传者的数字 uid"
            if col == self.COL_CODE:
                return "固定提取码：命中该分享者时自动填入"
            if col == self.COL_PICK:
                return "需挑选：只下载选中的文件；不勾选则整包下载"
            if col == self.COL_NOTE:
                note = str(row.get("note") or "")
                if note:
                    return "备注：%s" % note
                return "备注为空 · 点「编辑」可添加备注"
            return None
        if role == Qt.TextAlignmentRole:
            return int(self._ALIGN.get(col, Qt.AlignLeft | Qt.AlignVCenter))
        return None
