# -*- coding: utf-8 -*-
"""HOME 页：拖放落区（52px 虚线，拖动进入时高亮）+ 紧凑任务列表 + 底部三按钮。

按钮（40px 区）：`＋ 添加文件` / `提取码` / `新增口令`——只发信号，
具体动作（QFileDialog、进 CODE / PW 页）由 `CompactWindow` 统一处理，
本页不直接碰宿主私有入口，便于测试与复用。
"""
from PyQt5.QtCore import Qt, pyqtSignal
from PyQt5.QtWidgets import (QFrame, QHBoxLayout, QLabel, QPushButton,
                             QVBoxLayout, QWidget)

from ..widgets.common import repolish
from ..widgets.inputs import Glyph
from .tasklist import CompactTaskList


class HomePage(QWidget):
    """主界面页：看队列、拖文件、进提取码 / 新增口令页。"""

    addFilesRequested = pyqtSignal()
    codeRequested = pyqtSignal()
    pwRequested = pyqtSignal()
    taskActivated = pyqtSignal(int)

    def __init__(self, host, parent=None):
        super().__init__(parent)
        self._host = host
        self.setObjectName("compactHomePage")

        root = QVBoxLayout(self)
        root.setContentsMargins(10, 10, 10, 10)
        root.setSpacing(8)

        # ---- 落区（52px 虚线；dragMove 时整块高亮，见 CompactWindow.dragMoveEvent）----
        self.drop_zone = QFrame(self)
        self.drop_zone.setObjectName("compactDropZone")
        self.drop_zone.setAttribute(Qt.WA_StyledBackground, True)
        self.drop_zone.setProperty("drag", "false")
        self.drop_zone.setFixedHeight(52)
        self.drop_zone.setToolTip("把压缩包拖到这里，或点下方「＋ 添加文件」")
        zone_lay = QHBoxLayout(self.drop_zone)
        zone_lay.setContentsMargins(8, 0, 8, 0)
        zone_lay.setSpacing(8)
        zone_lay.addStretch(1)
        self.drop_glyph = Glyph("download", self.drop_zone, 16, role="muted")
        zone_lay.addWidget(self.drop_glyph, 0, Qt.AlignVCenter)
        self.drop_hint = QLabel("把压缩包拖到这里", self.drop_zone)
        self.drop_hint.setObjectName("compactDropHint")
        zone_lay.addWidget(self.drop_hint, 0, Qt.AlignVCenter)
        zone_lay.addStretch(1)
        root.addWidget(self.drop_zone)

        # ---- 队列小标题（空态时隐藏，由空态一行灰字承担）----
        self.head = QLabel("队列", self)
        self.head.setObjectName("compactQueueHead")
        self.head.hide()
        root.addWidget(self.head)

        # ---- 紧凑任务列表（最多 6 行后滚动；双击行 = 打开输出目录）----
        self.task_list = CompactTaskList(host, self)
        self.task_list.taskActivated.connect(self.taskActivated)
        root.addWidget(self.task_list, 1)

        # ---- 底部三按钮（40px 区）----
        foot = QWidget(self)
        foot.setFixedHeight(40)
        foot_lay = QHBoxLayout(foot)
        foot_lay.setContentsMargins(0, 4, 0, 4)
        foot_lay.setSpacing(6)
        self.add_btn = QPushButton("＋ 添加文件", foot)
        self.add_btn.setObjectName("primary")
        self.code_btn = QPushButton("提取码", foot)
        self.pw_btn = QPushButton("新增口令", foot)
        for btn in (self.add_btn, self.code_btn, self.pw_btn):
            btn.setCursor(Qt.PointingHandCursor)
            btn.setMinimumHeight(30)
            btn.setFocusPolicy(Qt.NoFocus)
        self.add_btn.setToolTip("选择压缩包（与拖拽同一条入队路径）")
        self.code_btn.setToolTip("输入分享提取码")
        self.pw_btn.setToolTip("新增永久口令（存入口令本）")
        foot_lay.addWidget(self.add_btn, 1)
        foot_lay.addWidget(self.code_btn, 1)
        foot_lay.addWidget(self.pw_btn, 1)
        root.addWidget(foot)

        # 信号转信号：clicked 会带一个 bool，不能直接连到无参信号上
        self.add_btn.clicked.connect(lambda *_: self.addFilesRequested.emit())
        self.code_btn.clicked.connect(lambda *_: self.codeRequested.emit())
        self.pw_btn.clicked.connect(lambda *_: self.pwRequested.emit())

    # ---- 拖拽高亮（由 CompactWindow 的 dragMove/dragLeave 驱动）----
    def set_drag_active(self, active):
        """落区高亮开关：强调色虚线 + 浅强调底（属性 + repolish）。"""
        try:
            self.drop_zone.setProperty("drag", "true" if active else "false")
            self.drop_glyph.set_role("accent" if active else "muted")
            repolish(self.drop_zone)
        except Exception:
            pass

    # ---- 数据刷新（窗口的轮询 / 拖放后调用）----
    def refresh_tasks(self):
        """刷新任务列表与小标题；返回计数字典（供窗口状态区复用）。"""
        counts = self.task_list.refresh()
        try:
            queue_n = int(counts.get("queue", 0) or 0)
            history_n = int(counts.get("history", 0) or 0)
            doing = int(counts.get("extracting", 0) or 0)
            done = int(counts.get("done", 0) or 0)
            if queue_n + history_n <= 0:
                self.head.hide()
            else:
                self.head.setText("队列 · %d 进行 / %d 完成" % (doing, done))
                self.head.show()
        except Exception:
            pass
        return counts
