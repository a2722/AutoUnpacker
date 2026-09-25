# -*- coding: utf-8 -*-
"""密码本页（M4）：口令表 + 命中统计 + 搜索 / 筛选 + 复制 / 新增 / 编辑 / 删除。

职责：
- PasswordBookPage：页头（「密码本」+ 口令总数 + 命中过）、口令视图
  （细统计条、搜索实时过滤、四筛选分段（全部 / 命中过 / 未命中 / 有来源）、口令表
  （口令 / 来源 / 命中次数 / 最近命中 / 备注 + 行内 复制 / 编辑 / 删除；临时行
  编辑本就不可用，以「设为永久」替代——加入长期密码本并移出临时记录）、空态、
  Ctrl+F 聚焦搜索、Delete 删除选中行（二次确认）、刷新、点列头排序（仅显示层）、
  多选（Shift/Ctrl）时「复制选中 / 删除选中」、查重、实时刷新（QTimer + showEvent））
- 行数据 = 「长期密码本 ∪ 本次临时密码 ∪ 解压字典」按口令合并去重（长期在前；
  字典只补缺），来源列区分：手动（长期）/ 剪贴板（临时）/ —（仅字典收录，无主动来源）
- _PwData：页面私有的行级数据层。长期口令的读 / 增 / 改 / 删按行 id 直连
  db.list_passwords / add_password / update_password / delete_password（经 state 的
  行级代理 password_rows/add_password_row/update_password_row/delete_password_row）；
  只提供旧整表接口的离线桩退回 passwords/set_passwords 合成行（保证旧桩可用）。
  临时密码与命中统计仍只读 state.temp_passwords / db.load_password_dict。
  本模块绝不直接读写 SQLite 文件（测试可对这些入口打桩）。
关键入口：PasswordBookPage / EMPTY_BOOK / EMPTY_PWFILTER
依赖：PyQt5、db、password_book（行级 helper / 行格式解析器）、style（PALETTE）、widgets（Glyph / SegControl / show_toast）
注意：口令属隐私数据——明文绝不写进任何日志、notice 回执 / toast 提示；
      刻意例外（用户已显式放宽旧约束）：口令列 hover tooltip 明文展示该行口令
      （「口令：<明文> · 双击可复制」，仅超长时中间省略，见 models._PWD_TOOLTIP_MAX）；
      复制是用户显式动作（只写系统剪贴板）；删除只动密码本条目，绝不触碰任何文件。
注意：备注列来自 passwords.note（行级 API），为空显示「—」；编辑对话框同时改口令与备注，
      备注可只改不改口令；双击备注列直接打开编辑对话框并把键盘焦点锁进备注栏，
      双击其余列仍是复制口令；重名口令按 id 定向删除，不再整体重写。
注意：排序只重排视图（点列头升 / 降序，默认命中次数降序），绝不改写密码本顺序——
      解压尝试顺序只由库内 id 顺序决定，任何表头点击都不会改变它；「查重」仍会清理
      重复行（整表覆盖时按口令保留备注与来源）。
注意：表格复用 #taskTable 的既有 QSS；口令表列头只在本地修正（_PwHeader 自绘排序
      指示器 + 本地右侧 11px padding，不改 style.py）；自绘取色一律走 PALETTE /
      widgets 的 token 机制。
"""


# ---------------------------------------------------------------------------
# 子模块布局（旧 ui/page_pwbook.py 按职责拆分；本文件只做重导出，保持
# `autounpacker.ui.page_pwbook.X` 与旧模块完全一致，含以 `_` 开头的内部名）：
#   data.py     _PwData 行级数据门面 + 纯函数助手 + 来源常量
#   models.py   _PwModel 表格数据模型
#   table.py    _PwTable / _PwDelegate / _PwHeader / _EmptyOverlay / _icon_button
#   dialogs.py  _PasswordEditDialog
#   page.py     PasswordBookPage 页面本体
# 注意：Qt / db / password_book / style / widgets 名的重导出只为保持旧模块的
#      属性表面（历史行为）；对旧模块属性的赋值由文件底部的转发 shim 同步写入
#      各归属子模块——测试会对 _PasswordEditDialog / QApplication 等打桩，
#      必须让子模块里的调用点看见。
# ---------------------------------------------------------------------------
import sys as _sys
import time  # noqa: F401
from types import ModuleType as _ModuleType

from PyQt5.QtCore import (QAbstractTableModel, QEvent, QItemSelectionModel,  # noqa: F401
                          QTimer, Qt, pyqtSignal)
from PyQt5.QtGui import (QColor, QFont, QFontMetrics, QKeySequence, QPainter,  # noqa: F401
                         QPainterPath)
from PyQt5.QtWidgets import (  # noqa: F401
    QAbstractItemView, QApplication, QDialog, QHBoxLayout,
    QHeaderView, QLabel, QLineEdit, QMessageBox, QPlainTextEdit, QPushButton,
    QShortcut, QStyledItemDelegate, QStyleOptionViewItem,
    QTableView, QVBoxLayout, QWidget)

from ... import db  # noqa: F401
from ...passwords.book import (  # noqa: F401
    add_password_row, delete_password_row, list_password_rows,
    update_password_row)
from ..style import PALETTE, tokens  # noqa: F401
from ..widgets import Glyph, SegControl, show_toast  # noqa: F401

from . import data as _data_home
from . import dialogs as _dialogs_home
from . import models as _models_home
from . import page as _page_home
from . import table as _table_home
from .data import (  # noqa: F401
    _SOURCE_BOOK, _SOURCE_LABELS, _SOURCE_TEMP, _PwData,
    _fmt_hit_time, _hit_count, _make_row, _passes_filter,
    _row_matches, _source_label)
from .dialogs import _PasswordEditDialog  # noqa: F401
from .models import _PwModel  # noqa: F401
from .page import (  # noqa: F401
    EMPTY_BOOK, EMPTY_PWFILTER, PasswordBookPage, _FILTERS)
from .table import (  # noqa: F401
    _EmptyOverlay, _PwDelegate, _PwHeader, _PwTable, _icon_button)

# 名字 → 新归属模块列表：旧模块属性被赋值时同步写入所有持有该名字的子模块
# （同一名字被多个子模块共读时全部写入，保证新旧两条路径读写同一份状态）。
_OWNERS_BY_NAME = {}
for _mod in (_data_home, _models_home, _table_home, _dialogs_home, _page_home):
    for _name in dir(_mod):
        if _name.startswith("__"):
            continue
        _OWNERS_BY_NAME.setdefault(_name, []).append(_mod)
del _mod, _name


class _PagePwbookShimModule(_ModuleType):
    """旧 page_pwbook 模块：属性赋值转发到新归属模块，读写永远指向同一份状态。"""

    def __setattr__(self, name, value):
        owners = _OWNERS_BY_NAME.get(name)
        if owners:
            for owner in owners:
                setattr(owner, name, value)
        super().__setattr__(name, value)


_sys.modules[__name__].__class__ = _PagePwbookShimModule
