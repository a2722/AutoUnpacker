# -*- coding: utf-8 -*-
"""文本高度兜底（CJK 墨迹盒顶/底 1~2px 裁切的唯一真源）。

为什么单独成模块：调用方横跨 `ui/`（page_settings / main_window / compact.window /
dialogs.task_details），彼此已有导入关系；放进 style.py 会让「纯 QSS 模块」被迫依赖
QWidget，或逼每个调用方写惰性导入。本模块只依赖 PyQt5、不 import 包内任何模块，
因此不参与循环（见 test_import_graph_acyclic.py），任何 UI 模块都可模块级 import。

规则（字体级，非 per-string 墨迹盒）：
    need = ceil(fontMetrics().lineSpacing()) + 2
    minimumHeight = max(minimumHeight, need)

为什么是 lineSpacing() + 2：
  - QLabel 单行按 ascent+descent（== fontMetrics().height()）居中，但 CJK（SimSun
    等）的墨迹盒可比 height() 高 1~2px（探针实测：rect 12px vs ink 13~14px，
    顶缘越界 -1px）；多行还会按 lineSpacing() 排线（行间 leading）。
  - 字体级常量对「文本运行期会变」的标签同样成立；per-string tightBoundingRect
    只对当前字符串成立，setText 后立即失效，不能当兜底依据。
  - 单行标签**不再豁免**：历史实现以「单行不裁」为由跳过，实测该假设为假。

幂等：只抬不降；空文本标签绝不触碰；任何异常吞掉（UI 兜底绝不反噬）。
性能：只在 show / polish / 主题切换时整树调用，绝不挂进 paint / resize / 逐行路径。
"""
from PyQt5.QtWidgets import QLabel


def ensure_min_height(widget):
    """把单个控件（QLabel / 气泡框等任意 QWidget）最小高度抬到 lineSpacing()+2。

    返回是否实际抬高（幂等：已达标返回 False）；任何异常静默返回 False。
    """
    try:
        need = int(widget.fontMetrics().lineSpacing()) + 2
        if widget.minimumHeight() < need:
            widget.setMinimumHeight(need)
            return True
    except Exception:
        pass
    return False


def fit_text_heights(root):
    """把 root 下所有「会绘制文本」的 QLabel 抬到 lineSpacing()+2。

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
        if ensure_min_height(lbl):
            fixed += 1
    return fixed
