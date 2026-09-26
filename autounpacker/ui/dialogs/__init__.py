# -*- coding: utf-8 -*-
"""各类对话框：7-Zip 管理、关闭行为确认、网址信任确认、目录设置。

职责：- SevenZipSetupDialog 检测/安装/卸载 7-Zip（隔离版与全局版）
- CloseActionDialog 关闭行为询问；TrustAskDialog 新网址信任确认
- ShareCodeAskDialog 分享缺提取码时贴主窗右缘的非阻塞取码小窗（120s 到点关闭作废）
- WatchDirDialog 目录设置弹窗（对应原型 12；监听模式为两张平铺卡，严禁下拉框）
- DragBehaviorDialog 拖拽行为设置弹窗（固定胶囊打开；拖入文件后做什么）
- TaskDetailsDialog 任务详情弹窗（队列行「详细信息」/ 双击行；按状态给出出路动作）
关键入口：SevenZipSetupDialog / TrustAskDialog /
          ShareCodeAskDialog / WatchDirDialog / DragBehaviorDialog /
          TaskDetailsDialog
依赖：PyQt5、trail、sevenzip、trust、widgets
注意：7-Zip 安装/卸载在后台线程执行（_SevenZipOp），UI 仅投递任务
"""

# ---------------------------------------------------------------------------
# 子模块布局（旧 ui/dialogs.py 按对话框拆分；本文件只做重导出，保持
# `autounpacker.ui.dialogs.X` 与旧模块完全一致，含以 `_` 开头的内部名）：
#   common.py        共享模块级名字（状态词表/回收站文案/取码小窗常量、
#                    _call_decision、_CodeLineEdit）
#   sevenzip.py      SevenZipSetupDialog / _SevenZipOp
#   trust.py         TrustAskDialog / CloseActionDialog
#   share_ask.py     ShareCodeAskDialog
#   delete_policy.py DeletePolicyAskDialog
#   watch_dir.py     WatchDirDialog
#   drag_behavior.py DragBehaviorDialog（拖拽行为设置）
#   task_details.py  TaskDetailsDialog（任务详情 + 状态相关动作）
# 注意：QDialog / QMessageBox / QPlainTextEdit 的重导出只为保持旧模块的属性表面
#      （既有测试会对 dialogs.QMessageBox / dialogs.QPlainTextEdit 打桩，
#       对应子模块在调用点从包属性动态再导入以保证打桩生效）。
# ---------------------------------------------------------------------------
from PyQt5.QtWidgets import QDialog, QMessageBox, QPlainTextEdit  # noqa: F401

from .common import (  # noqa: F401
    TRAIL_STATUS_TEXT, TRAIL_STATUS_ORDER, TRASH_HINT_NORMAL,
    TRASH_HINT_NO_BIN, SHARE_ASK_TIMEOUT_SEC, SHARE_ASK_EDGE_MARGIN,
    SHARE_ASK_WINDOW_WIDTH, _call_decision, _CodeLineEdit)
from .sevenzip import SevenZipSetupDialog, _SevenZipOp  # noqa: F401
from .trust import TrustAskDialog, CloseActionDialog  # noqa: F401
from .share_ask import ShareCodeAskDialog  # noqa: F401
from .delete_policy import DeletePolicyAskDialog  # noqa: F401
from .watch_dir import WatchDirDialog  # noqa: F401
from .drag_behavior import DragBehaviorDialog  # noqa: F401
from .task_details import TaskDetailsDialog  # noqa: F401

__all__ = [
    "TRAIL_STATUS_TEXT", "TRAIL_STATUS_ORDER", "TRASH_HINT_NORMAL",
    "TRASH_HINT_NO_BIN", "SHARE_ASK_TIMEOUT_SEC", "SHARE_ASK_EDGE_MARGIN",
    "SHARE_ASK_WINDOW_WIDTH", "_call_decision", "_CodeLineEdit",
    "_SevenZipOp", "SevenZipSetupDialog",
    "CloseActionDialog", "TrustAskDialog",
    "ShareCodeAskDialog", "DeletePolicyAskDialog", "WatchDirDialog",
    "DragBehaviorDialog", "TaskDetailsDialog",
]
