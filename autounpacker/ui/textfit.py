# -*- coding: utf-8 -*-
"""文本高度兜底（CJK 墨迹盒顶/底 1~2px 裁切的唯一真源）。

为什么单独成模块：调用方横跨 `ui/`（page_settings / main_window / compact.window /
dialogs.task_details），彼此已有导入关系；放进 style.py 会让「纯 QSS 模块」被迫依赖
QWidget，或逼每个调用方写惰性导入。本模块只依赖 PyQt5、不 import 包内任何模块，
因此不参与循环（见 test_import_graph_acyclic.py），任何 UI 模块都可模块级 import。

规则（字体级，非 per-string 墨迹盒）：
    need = ceil(fontMetrics().lineSpacing()) + 3
    minimumHeight = max(minimumHeight, need)

为什么是 lineSpacing() + 3：
  - QLabel 单行按 ascent+descent（== fontMetrics().height()）居中，但 CJK（SimSun
    等）的墨迹盒可比 height() 高 1~2px（探针实测：rect 12px vs ink 13~14px，
    顶缘越界 -1px）；多行还会按 lineSpacing() 排线（行间 leading）。
  - +2 曾被实测为「恰好贴边」：探针判据里 FLUSH = 不越界但 top/bot_off == 0，
    即墨迹盒边正好压住矩形边、零余量。字体/DPI/字体回退/文案任一变化 1px 就
    会从 FLUSH 掉成 SHORTFALL（真裁切）。故抬到 +3，给所有文本行留 1px 余量，
    让探针判定从 FLUSH 收敛为 OK（金丝雀消失）。
  - 字体级常量对「文本运行期会变」的标签同样成立；per-string tightBoundingRect
    只对当前字符串成立，setText 后立即失效，不能当兜底依据。
  - 单行标签**不再豁免**：历史实现以「单行不裁」为由跳过，实测该假设为假。

幂等：只抬不降；空文本标签绝不触碰；任何异常吞掉（UI 兜底绝不反噬）。
性能：只在 show / polish / 主题切换时整树调用，绝不挂进 paint / resize / 逐行路径。
"""
from PyQt5.QtWidgets import QLabel


def ensure_min_height(widget):
    """把单个控件（QLabel / 气泡框等任意 QWidget）最小高度抬到 lineSpacing()+3。

    返回是否实际抬高（幂等：已达标返回 False）；任何异常静默返回 False。
    """
    try:
        need = int(widget.fontMetrics().lineSpacing()) + 3
        if widget.minimumHeight() < need:
            widget.setMinimumHeight(need)
            return True
    except Exception:
        pass
    return False


def ensure_natural_height(widget):
    """把**不折行**标签的最小高度抬到它的自然高度（sizeHint().height()）。

    为什么需要（真机 bug，2026-11-10「检测状态显示成叠字」）：只读值芯片
    （QLabel#roval：`padding: 6px 11px` + 1px 边框）的自然高度是 27px
    （字体 13 + 上下 padding 12 + 边框 2），而 ensure_min_height 只把它抬到
    lineSpacing()+3 = 18px —— **最小高度低于自然高度**。于是上游布局一旦欠
    9px（页面内容高度在 QSS 把芯片垫高之前就已同步过一次，滚动区此后不再
    重算），行就有权把芯片压到 18px：文字上下各被裁掉约 4px，屏幕上表现为
    「叠字 / 糊字」，而不是干净的截断。

    只处理**不折行**标签：它们的 sizeHint().height() 与宽度无关，钉死它不会
    引起布局抖动；折行标签（#hint / #stripHint）的 sizeHint 随宽度变，仍只走
    ensure_min_height 的字体级兜底。

    幂等（只抬不降）；任何异常静默返回 False。
    """
    try:
        need = int(widget.sizeHint().height())
        if widget.minimumHeight() < need:
            widget.setMinimumHeight(need)
            return True
    except Exception:
        pass
    return False


def fit_text_heights(root):
    """把 root 下所有「会绘制文本」的 QLabel 抬到 lineSpacing()+3。

    不折行的标签额外钉到自然高度（含 QSS padding / 边框，见
    ensure_natural_height —— 否则只读值芯片会被上游欠额压扁成叠字）。

    返回被抬高的标签数（0 = 无 / 全已达标 / root 为空或异常）。
    root 自身若是 QLabel 也会被处理；空文本（含全空白）标签绝不触碰。
    """
    if root is None:
        return 0
    try:
        labels = list(root.findChildren(QLabel))
    except Exception:
        return 0
    try:
        if isinstance(root, QLabel):
            labels.append(root)
    except Exception:
        pass
    fixed = 0
    for lbl in labels:
        try:
            if not str(lbl.text() or "").strip():
                continue
        except Exception:
            continue
        changed = ensure_min_height(lbl)
        wrapped = True
        try:
            wrapped = bool(lbl.wordWrap())
        except Exception:
            pass
        if not wrapped:
            changed = ensure_natural_height(lbl) or changed
        if changed:
            fixed += 1
    return fixed
