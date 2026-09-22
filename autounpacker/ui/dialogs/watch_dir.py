# -*- coding: utf-8 -*-
"""WatchDirDialog：目录设置弹窗（监听路径/解压到/模式/开关/删除策略/当前任务）。"""
from PyQt5.QtWidgets import (QWidget, QVBoxLayout, QHBoxLayout, QLabel,
                             QLineEdit, QPushButton, QCheckBox, QDialog,
                             QRadioButton, QButtonGroup, QFrame, QFileDialog,
                             QProgressBar)
from PyQt5.QtCore import Qt, QTimer, pyqtSignal

from ... import baidu_manifest
from ... import trail as deletion_trail
from ...config import DELETE_POLICY_DEFAULT
from ...utils import watch_path_conflict
from ..style import PALETTE
from ..widgets import (Glyph, LayoutButton, ModeSelector, DIR_STATE_TEXT,
                       dir_state_key)
from .common import TRASH_HINT_NORMAL, TRASH_HINT_NO_BIN
from .delete_policy import DeletePolicyAskDialog


class WatchDirDialog(QDialog):
    """目录设置弹窗（对应原型 12）：宽 600、模态、居中于父窗口。

    由目录胶囊点击后打开：`WatchDirDialog(state, idx, parent, entry=None)`；entry 为
    宿主（MainWindow._dir_entries）解析后的条目（含运行时 state/progress/name），
    状态徽标据此显示真实状态，不再拿配置里不存在的 state 冒充「监听中」。结构：
    - 头部：目录图标 + 标题 + 等宽路径 + 实时状态徽标 + 关闭；
    - 表单：监听路径 / 解压到 / 监听模式（**两张平铺卡，严禁 QComboBox**）/
      启用监听 + 解压成功后删除源文件 / 回收站说明 /「当前正在处理」卡片；
    - 底部：移除目录（危险，左）+ 取消 / 保存（右）。

    保存走现有通道：变动的字段逐个 `state.update_path(idx, field, value)`，
    随后 emit saved(idx)。「移除目录」只 emit removeRequested(idx)——
    二次确认由宿主负责，本弹窗绝不弹确认框。Esc / 取消 = reject()。

    遮罩：项目没有 dim-mask 原语，这里用父窗口的一个 rgba(0,0,0,.34) 子控件
    自带实现（showEvent 建、hideEvent/closeEvent 拆；无父窗口时自动忽略），
    全部包在 try/except 中，任何失败都不影响弹窗本身。
    """

    saved = pyqtSignal(int)
    removeRequested = pyqtSignal(int)

    def __init__(self, state, idx, parent=None, entry=None):
        super().__init__(parent)
        self.state = state
        self.idx = int(idx)
        self._scrim = None
        # entry：宿主解析后的条目（与目录胶囊同一口径，含运行时 state）；为 None
        # 时退回按 idx 读配置快照（兼容直接构造/离线测试）。
        self._orig = dict(entry) if isinstance(entry, dict) and entry else self._load_entry()
        self._state_key = dir_state_key(self._orig.get("state") or "listening")
        self._progress = None
        try:
            if self._orig.get("progress") is not None:
                self._progress = int(round(float(self._orig["progress"])))
        except Exception:
            self._progress = None
        self._current_name = ""
        self._current_layer = None
        # 删除策略控件：仅当卷「确定没有回收站」时显示；路径变化后延迟重探。
        self._policy_shown = False
        self._policy_probe_timer = QTimer(self)
        self._policy_probe_timer.setSingleShot(True)
        self._policy_probe_timer.setInterval(250)
        self._policy_probe_timer.timeout.connect(self._refresh_delete_policy)

        self.setWindowTitle("目录设置")
        self.setModal(True)
        self.setFixedWidth(600)

        root = QVBoxLayout(self)
        root.setContentsMargins(0, 0, 0, 0)
        root.setSpacing(0)
        root.addWidget(self._build_head())
        body = QWidget(self)
        body_lay = QVBoxLayout(body)
        body_lay.setContentsMargins(16, 14, 16, 14)
        body_lay.setSpacing(11)
        self._build_body(body_lay)
        root.addWidget(body)
        root.addWidget(self._build_foot())
        self._refresh_state_badge()
        self._render_current()
        self._refresh_delete_policy()

    # ---- 读取入口 ----
    def _load_entry(self):
        """取当前 entry：优先 state.snapshot()，退化到 state.cfg；异常一律空字典。"""
        for getter in (lambda: self.state.snapshot(),
                       lambda: getattr(self.state, "cfg", {})):
            try:
                data = getter() or {}
                paths_cfg = data.get("watch_paths") or []
                if 0 <= self.idx < len(paths_cfg) and isinstance(paths_cfg[self.idx], dict):
                    return dict(paths_cfg[self.idx])
            except Exception:
                continue
        return {}

    def _field_label(self, text):
        lbl = QLabel(text, self)
        lbl.setObjectName("fLabel")
        lbl.setFixedWidth(62)
        return lbl

    # ---- 头部 / 表单 / 底部 ----
    def _build_head(self):
        head = QFrame(self)
        head.setObjectName("dlgHead")
        lay = QHBoxLayout(head)
        lay.setContentsMargins(16, 14, 16, 12)
        lay.setSpacing(10)
        lay.addWidget(Glyph("folder", head, 20, role="accent"))
        title = QLabel("目录设置", head)
        title.setObjectName("dTitle")
        lay.addWidget(title)
        full_path = str(self._orig.get("path") or "")
        self.path_label = QLabel(head)
        self.path_label.setObjectName("dlgPath")
        try:
            self.path_label.setText(self.path_label.fontMetrics().elidedText(
                full_path or "—", Qt.ElideMiddle, 240))
        except Exception:
            self.path_label.setText(full_path or "—")
        self.path_label.setToolTip(full_path)
        lay.addWidget(self.path_label)
        lay.addStretch(1)
        self.state_badge = QLabel(head)
        self.state_badge.setObjectName("dlgState")
        lay.addWidget(self.state_badge)
        close_btn = QPushButton(head)
        close_btn.setObjectName("iconBtn")
        close_btn.setFixedSize(30, 30)
        close_btn.setToolTip("关闭")
        close_btn.setCursor(Qt.PointingHandCursor)
        close_lay = QHBoxLayout(close_btn)
        close_lay.setContentsMargins(0, 0, 0, 0)
        close_lay.addWidget(Glyph("close", close_btn, 16), 0, Qt.AlignCenter)
        close_btn.clicked.connect(self.reject)
        lay.addWidget(close_btn)
        return head

    def _build_body(self, lay):
        # 监听路径
        row1 = QHBoxLayout()
        row1.setSpacing(9)
        row1.addWidget(self._field_label("监听路径"))
        self.path_edit = QLineEdit(str(self._orig.get("path") or ""), self)
        self.path_edit.setPlaceholderText("监听目录路径")
        self.path_edit.textChanged.connect(self._on_policy_path_changed)
        row1.addWidget(self.path_edit, 1)
        browse1 = QPushButton("浏览", self)
        browse1.clicked.connect(self._browse_path)
        row1.addWidget(browse1)
        lay.addLayout(row1)

        # 解压到
        row2 = QHBoxLayout()
        row2.setSpacing(9)
        row2.addWidget(self._field_label("解压到"))
        self.out_edit = QLineEdit(str(self._orig.get("output_dir") or ""), self)
        self.out_edit.setPlaceholderText("留空 · 同目录建同名文件夹")
        row2.addWidget(self.out_edit, 1)
        browse2 = QPushButton("浏览", self)
        browse2.clicked.connect(self._browse_out)
        row2.addWidget(browse2)
        lay.addLayout(row2)

        # 监听模式：两张平铺卡（严禁 QComboBox）
        row3 = QHBoxLayout()
        row3.setSpacing(9)
        row3.addWidget(self._field_label("监听模式"), 0, Qt.AlignTop)
        self.mode_sel = ModeSelector(self)
        self.mode_sel.set_modes([
            ("surface", "表层 · 安全",
             "只处理监听目录最外一层的压缩包，行为与旧版一致，不会深入子目录。",
             "推荐"),
            ("baidu", "百度清单 · 含子目录",
             "额外按网盘任务清单处理下载到子目录里的压缩包与分卷，"
             "清单不可用时自动退回表层。", None),
        ])
        self.mode_sel.set_mode(str(self._orig.get("mode") or "surface"))
        row3.addWidget(self.mode_sel, 1)
        lay.addLayout(row3)

        # 开关
        row4 = QHBoxLayout()
        row4.setSpacing(9)
        row4.addWidget(self._field_label("开关"))
        self.enabled_cb = QCheckBox("启用监听", self)
        self.enabled_cb.setChecked(bool(self._orig.get("enabled", True)))
        row4.addWidget(self.enabled_cb)
        row4.addSpacing(14)
        self.del_cb = QCheckBox("解压成功后删除源文件", self)
        self.del_cb.setChecked(bool(self._orig.get("delete_source", False)))
        self.del_cb.stateChanged.connect(self._on_delete_toggled)
        row4.addWidget(self.del_cb)
        row4.addStretch(1)
        lay.addLayout(row4)

        # 回收站说明（无回收站的目录下，文案随「删除策略」切换为隔离区语义）
        hint_row = QHBoxLayout()
        hint_row.setSpacing(7)
        hint_row.addWidget(Glyph("shield", self, 13, role="muted"))
        self.trash_hint = QLabel(TRASH_HINT_NORMAL, self)
        self.trash_hint.setObjectName("dlgHint")
        self.trash_hint.setWordWrap(True)
        hint_row.addWidget(self.trash_hint, 1)
        lay.addLayout(hint_row)

        # 删除策略：仅当该目录所在卷「确定没有回收站」时出现（正常目录完全隐藏，
        # 不打扰）；正常有回收站的目录不显示任何多余选项。三选一，顺序与询问弹窗
        # 一致：移入隔离区（推荐/默认，可还原）→ 保留源文件 → 永久删除（危险）。
        self.policy_row = QWidget(self)
        p_lay = QVBoxLayout(self.policy_row)
        p_lay.setContentsMargins(0, 0, 0, 0)
        p_lay.setSpacing(4)
        p_top = QHBoxLayout()
        p_top.setSpacing(9)
        p_top.addWidget(self._field_label("删除策略"), 0, Qt.AlignTop)
        p_col = QVBoxLayout()
        p_col.setSpacing(4)
        self.policy_group = QButtonGroup(self.policy_row)
        self.policy_quar_rb = QRadioButton("移入隔离区（推荐）", self.policy_row)
        self.policy_quar_rb.setToolTip(
            "该卷没有可用回收站：删除源文件时移入目录下的 _已删除 文件夹，"
            "可在「删除回溯」页随时还原或彻底删除。")
        self.policy_keep_rb = QRadioButton("保留源文件", self.policy_row)
        self.policy_keep_rb.setToolTip(
            "该卷没有可用回收站：删除源文件时绝不永久删除，原文件保留在原位。")
        self.policy_perm_rb = QRadioButton("永久删除", self.policy_row)
        self.policy_perm_rb.setToolTip(
            "该卷没有可用回收站：删除源文件时直接永久删除，无法从回收站还原。")
        self.policy_group.addButton(self.policy_quar_rb)
        self.policy_group.addButton(self.policy_keep_rb)
        self.policy_group.addButton(self.policy_perm_rb)
        self.policy_quar_rb.setChecked(True)
        p_col.addWidget(self.policy_quar_rb)
        p_col.addWidget(self.policy_keep_rb)
        p_col.addWidget(self.policy_perm_rb)
        p_top.addLayout(p_col, 1)
        p_lay.addLayout(p_top)
        p_hint = QLabel(
            "该目录所在磁盘没有可用回收站，删除源文件无法从回收站还原；"
            "推荐移入隔离区（可随时还原或彻底删除）。",
            self.policy_row)
        p_hint.setObjectName("dlgHint")
        p_hint.setWordWrap(True)
        p_lay.addWidget(p_hint)
        self.policy_row.setVisible(False)
        lay.addWidget(self.policy_row)

        # 当前正在处理
        self.current_card = QFrame(self)
        self.current_card.setObjectName("dlgCurrent")
        c_lay = QVBoxLayout(self.current_card)
        c_lay.setContentsMargins(14, 12, 14, 12)
        c_lay.setSpacing(6)
        c_top = QHBoxLayout()
        c_top.setSpacing(8)
        c_top.addWidget(Glyph("archive", self.current_card, 13, role="muted"))
        c_title = QLabel("当前正在处理：", self.current_card)
        c_title.setObjectName("dlgHint")
        c_top.addWidget(c_title)
        self.current_name = QLabel("", self.current_card)
        self.current_name.setObjectName("dlgPath")
        self.current_name.setStyleSheet("font-weight: 600;")
        c_top.addWidget(self.current_name)
        c_top.addStretch(1)
        self.current_pct = QLabel("", self.current_card)
        self.current_pct.setObjectName("dlgPath")
        c_top.addWidget(self.current_pct)
        c_lay.addLayout(c_top)
        self.current_bar = QProgressBar(self.current_card)
        self.current_bar.setObjectName("thinProg")
        self.current_bar.setTextVisible(False)
        self.current_bar.setRange(0, 100)
        self.current_bar.setValue(0)
        c_lay.addWidget(self.current_bar)
        lay.addWidget(self.current_card)

    def _build_foot(self):
        foot = QFrame(self)
        foot.setObjectName("dlgFoot")
        lay = QHBoxLayout(foot)
        lay.setContentsMargins(16, 12, 16, 14)
        lay.setSpacing(8)
        self.remove_btn = LayoutButton(foot)
        self.remove_btn.setObjectName("danger")
        self.remove_btn.setCursor(Qt.PointingHandCursor)
        rm_lay = QHBoxLayout(self.remove_btn)
        rm_lay.setContentsMargins(0, 0, 0, 0)
        rm_lay.setSpacing(6)
        rm_lay.addWidget(Glyph("trash", self.remove_btn, 13, role="danger"))
        rm_lay.addWidget(QLabel("移除目录", self.remove_btn))
        self.remove_btn.clicked.connect(self._on_remove)
        lay.addWidget(self.remove_btn)
        lay.addStretch(1)
        self.cancel_btn = QPushButton("取消", foot)
        self.cancel_btn.clicked.connect(self.reject)
        lay.addWidget(self.cancel_btn)
        self.save_btn = QPushButton("保存", foot)
        self.save_btn.setObjectName("primary")
        self.save_btn.setDefault(True)
        self.save_btn.clicked.connect(self._on_save)
        lay.addWidget(self.save_btn)
        return foot

    # ---- 状态展示 ----
    def _refresh_state_badge(self):
        text = DIR_STATE_TEXT.get(self._state_key, self._state_key)
        if self._state_key == "extracting" and self._progress is not None:
            text += " %d%%" % int(self._progress)
        try:
            self.state_badge.setText("● " + text)
            color = PALETTE["success"]
            if self._state_key == "error":
                color = PALETTE["danger"]
            elif self._state_key in ("paused", "waiting", "missing"):
                color = PALETTE["muted"]
            elif self._state_key == "listening":
                color = PALETTE["accent_text"]
            self.state_badge.setStyleSheet("color: %s;" % color)
        except Exception:
            pass

    def _render_current(self):
        name = str(self._current_name or "")
        layer_txt = ""
        if self._current_layer is not None and str(self._current_layer) != "":
            layer_txt = " · 第 %s 层" % self._current_layer
        try:
            if name:
                name = self.current_name.fontMetrics().elidedText(
                    name, Qt.ElideMiddle, 240)
        except Exception:
            pass
        text = (name + layer_txt) if name else (layer_txt.lstrip(" ·") or "—")
        self.current_name.setText(text)
        self.current_name.setToolTip(text)
        pct = self._progress
        self.current_pct.setText(("%d%%" % int(pct)) if pct is not None else "")
        self.current_bar.setValue(int(pct) if pct is not None else 0)

    # ---- 公开 API ----
    def set_current(self, name, layer=None, progress=None):
        """「当前正在处理」卡片：文件名 · 第 N 层 + 百分比 + 细进度条。"""
        self._current_name = str(name or "")
        self._current_layer = layer
        if progress is not None:
            try:
                self._progress = max(0, min(100, int(round(float(progress)))))
            except Exception:
                self._progress = 0
            self._state_key = "extracting"
            self._refresh_state_badge()
        self._render_current()

    def entry(self):
        """当前面板上的值（保存前宿主可只读获取）。"""
        data = {
            "path": self.path_edit.text().strip(),
            "output_dir": self.out_edit.text().strip(),
            "mode": self.mode_sel.mode() or "surface",
            "enabled": bool(self.enabled_cb.isChecked()),
            "delete_source": bool(self.del_cb.isChecked()),
        }
        if self._policy_editable():
            data["delete_policy"] = self._policy_value()
        return data

    # ---- 删除策略（仅卷无回收站时可见） ----
    def _policy_editable(self):
        """策略控件可见、且该条目本就带 delete_policy 键时才参与读写。

        真实配置经 config.load_config/_sanitize_cfg 后每条都带该键；此守卫只用于
        兼容未经过净化、手工构造的旧条目，避免凭空写入未跟踪的字段。"""
        return self._policy_shown and "delete_policy" in self._orig

    def _policy_value(self):
        if self.policy_perm_rb.isChecked():
            return "permanent"
        if self.policy_keep_rb.isChecked():
            return "keep"
        return "quarantine"

    def _set_policy_value(self, value):
        if str(value) == "permanent":
            self.policy_perm_rb.setChecked(True)
        elif str(value) == "keep":
            self.policy_keep_rb.setChecked(True)
        else:
            # quarantine 与未选择（auto/未知）都预设为推荐项「移入隔离区」
            self.policy_quar_rb.setChecked(True)

    def _on_policy_path_changed(self, _text=""):
        """路径变化 → 延迟重探（防抖），避免每个键击都查询卷回收站。"""
        try:
            self._policy_probe_timer.start()
        except Exception:
            self._refresh_delete_policy()

    def _refresh_delete_policy(self):
        """按当前输入路径所在卷实测回收站：仅「确定没有回收站」时显示策略控件。"""
        try:
            path = self.path_edit.text().strip()
            has_bin = (deletion_trail.volume_has_recycle_bin(path)
                       if path else None)
        except Exception:
            has_bin = None
        self._policy_shown = (has_bin is False)
        try:
            if self._policy_shown:
                stored = str(self._orig.get("delete_policy") or DELETE_POLICY_DEFAULT)
                self._set_policy_value(
                    stored if stored in ("quarantine", "keep", "permanent") else "")
            self.trash_hint.setText(
                TRASH_HINT_NO_BIN if self._policy_shown else TRASH_HINT_NORMAL)
            self.policy_row.setVisible(self._policy_shown)
        except Exception:
            pass

    def _on_delete_toggled(self, _state=0):
        """用户开启「删除源文件」时（弹窗可见时）按需询问一次删除策略。"""
        try:
            if self.isVisible():
                self._maybe_prompt_delete_policy()
        except Exception:
            pass

    def _maybe_prompt_delete_policy(self):
        """卷无回收站 + 已开启删除源文件 + 尚未明确选择策略时，仅询问一次。"""
        try:
            if "delete_policy" not in self._orig:
                return              # 非标准条目（未带该字段）：不在此处新增
            stored = str(self._orig.get("delete_policy") or DELETE_POLICY_DEFAULT)
            if stored != DELETE_POLICY_DEFAULT:
                return              # 已明确选择过，绝不再打扰
            if not self.del_cb.isChecked():
                return              # 未开启删除源文件，暂无需选择
            path = self.path_edit.text().strip()
            if not path:
                return
            if deletion_trail.volume_has_recycle_bin(path) is not False:
                return              # 有回收站 / 不确定：不设置策略
            choice = DeletePolicyAskDialog.ask(self)
            if choice is None:
                return              # 用户取消：保持默认，下次仍会询问
            self._orig["delete_policy"] = choice
            self._policy_shown = True
            self._set_policy_value(choice)
            self.policy_row.setVisible(True)
            try:
                self.state.update_path(self.idx, "delete_policy", choice)
            except Exception:
                pass
        except Exception:
            pass

    # ---- 交互 ----
    def _on_save(self):
        cur = self.entry()
        # 实验性开启时：新的监听路径不得与其它条目嵌套/重叠（百度网盘那套会把
        # 子目录也纳入监听，重叠路径会让同一批文件被两条监听路径重复处理）。
        # 开关关闭时保持原行为：只依赖 update_path 的精确去重，不做重叠拦截。
        new_path = str(cur.get("path") or "").strip()
        old_path = str(self._orig.get("path") or "").strip()
        if new_path != old_path and baidu_manifest.is_enabled(self.state):
            conflict = watch_path_conflict(self._other_watch_entries(), new_path)
            if conflict is not None:
                from . import QMessageBox   # 兼容旧模块全局补丁：从包属性动态取值（test_recursion_overlap 打桩）
                QMessageBox.warning(
                    self, "目录设置",
                    "监听路径与已有路径重叠，可能重复处理同一批文件：\n"
                    f"{new_path}\n↔ {conflict.get('path')}")
                return
        fields = ["path", "output_dir", "mode", "enabled", "delete_source"]
        if self._policy_editable():
            fields.append("delete_policy")
        for field in fields:
            value = cur.get(field)
            if value != self._orig.get(field):
                try:
                    self.state.update_path(self.idx, field, value)
                except Exception:
                    pass
        try:
            self.saved.emit(self.idx)
        except Exception:
            pass
        self.accept()

    def _other_watch_entries(self):
        """除当前条目外的监听路径条目（供实验性「重叠监听目录」检测比对）。"""
        for getter in (lambda: self.state.snapshot(),
                       lambda: getattr(self.state, "cfg", {})):
            try:
                paths = list((getter() or {}).get("watch_paths") or [])
            except Exception:
                continue
            return [e for i, e in enumerate(paths)
                    if i != self.idx and isinstance(e, dict)]
        return []

    def _on_remove(self):
        """只发信号：二次确认由宿主负责（本弹窗绝不弹确认框）。"""
        try:
            self.removeRequested.emit(self.idx)
        except Exception:
            pass

    def _browse_path(self):
        try:
            d = QFileDialog.getExistingDirectory(self, "选择监听目录")
        except Exception:
            d = ""
        if d:
            self.path_edit.setText(d)

    def _browse_out(self):
        try:
            d = QFileDialog.getExistingDirectory(self, "选择解压输出目录")
        except Exception:
            d = ""
        if d:
            self.out_edit.setText(d)

    def keyPressEvent(self, event):
        if event.key() == Qt.Key_Escape:
            self.reject()
            return
        super().keyPressEvent(event)

    # ---- 遮罩（极简自带实现） ----
    def _ensure_scrim(self):
        if self._scrim is not None:
            return
        parent = self.parentWidget()
        if parent is None:
            return
        try:
            sc = QFrame(parent)
            sc.setObjectName("dlgScrim")
            sc.setAttribute(Qt.WA_TransparentForMouseEvents, True)
            sc.setAttribute(Qt.WA_StyledBackground, True)
            sc.setStyleSheet("background: rgba(0,0,0,0.34);")
            sc.setGeometry(parent.rect())
            sc.show()
            sc.raise_()
            self._scrim = sc
        except Exception:
            self._scrim = None

    def _destroy_scrim(self):
        sc = self._scrim
        self._scrim = None
        if sc is None:
            return
        try:
            sc.hide()
            sc.setParent(None)
            sc.deleteLater()
        except Exception:
            pass

    def showEvent(self, event):
        self._ensure_scrim()
        super().showEvent(event)
        self._center_on_parent()
        # 弹窗打开时若已开启「删除源文件」且卷无回收站、策略未定，询问一次。
        try:
            self._maybe_prompt_delete_policy()
        except Exception:
            pass

    def hideEvent(self, event):
        self._destroy_scrim()
        super().hideEvent(event)

    def closeEvent(self, event):
        self._destroy_scrim()
        super().closeEvent(event)

    def _center_on_parent(self):
        try:
            parent = self.parentWidget()
            if parent is None:
                return
            self.adjustSize()
            pg = parent.frameGeometry()
            self.move(pg.center().x() - self.width() // 2,
                      pg.center().y() - self.height() // 2)
        except Exception:
            pass
