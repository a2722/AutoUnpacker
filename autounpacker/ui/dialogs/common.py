# -*- coding: utf-8 -*-
"""对话框共享模块级名字：回溯状态词表/回收站文案/取码小窗常量、_call_decision、_CodeLineEdit。"""
import inspect

from PyQt5.QtWidgets import QLineEdit


TRAIL_STATUS_TEXT = {
    "recorded": "已记录（处理中）",
    "kept": "未删除",
    "deleted": "已删除（回收站）",
    "restored": "已还原",
    "failed": "解压失败",
}

# 状态颜色统一由 widgets/style 提供（TRAIL_STATUS_COLORS = PALETTE["trail"]，
# 与主题同一对象，切换主题后就地更新），此处不再重复定义。

TRAIL_STATUS_ORDER = ["deleted", "restored", "kept", "failed", "recorded"]

# 「回收站说明」两种文案：有回收站的目录 vs 无回收站（删除按策略执行，可能是隔离区）
TRASH_HINT_NORMAL = "删除是把源文件移入回收站，可在「删除回溯」标签页一键还原，不会永久丢失。"
TRASH_HINT_NO_BIN = ("该磁盘没有可用回收站：删除源文件按下方「删除策略」执行，"
                     "可在「删除回溯」标签页还原或彻底删除。")

# 分享缺提取码取码小窗的超时（秒）：到点自动关闭并丢弃框内内容（不回调）。
SHARE_ASK_TIMEOUT_SEC = 120

# 取码小窗几何常量（px）：
# SHARE_ASK_EDGE_MARGIN —— 与**主窗右缘**的间隙（贴着主窗、略向外一点）；
#   主窗不可用（无父 / 测试桩直接构造）时，退回旧行为「距屏幕可用区右边距」。
# SHARE_ASK_WINDOW_WIDTH —— 小窗固定宽度。
SHARE_ASK_EDGE_MARGIN = 16
SHARE_ASK_WINDOW_WIDTH = 300


def _call_decision(cb, kind, code, url, surl, share_uk):
    """按注入 callable 可接受的位置参数个数回调 on_decision，兼容旧/新签名。

    冻结词表：kind ∈ {"mapped", "once", "ignore"}（ignore 时 code 为空串）。
    - 新接线：cb(kind, code, url, surl, share_uk)
    - 旧接线（仍闭包 url/surl/uk 的 2 参 lambda）：cb(kind, code)
    """
    try:
        params = list(inspect.signature(cb).parameters.values())
        positional = sum(1 for p in params if p.kind in (
            p.POSITIONAL_ONLY, p.POSITIONAL_OR_KEYWORD))
        var = any(p.kind == p.VAR_POSITIONAL for p in params)
        use5 = var or positional >= 5
    except (TypeError, ValueError):
        use5 = True
    if use5:
        cb(kind, code, url, surl, share_uk)
    else:
        cb(kind, code)


class _CodeLineEdit(QLineEdit):
    """4 位提取码输入框：额外记住「setText 被 maxLength 截断」的越界输入。

    QLineEdit.setText() 不经过校验器，且按 maxLength 静默截断（"abcde" ->
    "abcd"），于是越界输入在框内看起来像合法 4 位码。这里在截断发生前记录
    越界标记，供 current_code() 判为非法；用户实际键入/粘贴会清掉该标记，
    回到正常校验路径（不改变可见外观与既有控件风格）。
    """

    def __init__(self, parent=None):
        super().__init__(parent)
        self._overlong = False

    def setText(self, text):
        raw = str(text or "")
        ml = self.maxLength()
        # 先置标记再 super().setText()：textChanged 监听者能立刻读到最终状态
        self._overlong = bool(ml >= 0 and len(raw) > ml)
        super().setText(raw)

    def keyPressEvent(self, event):
        self._overlong = False
        super().keyPressEvent(event)

    def is_overlong(self):
        return self._overlong
