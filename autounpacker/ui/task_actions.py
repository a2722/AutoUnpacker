# -*- coding: utf-8 -*-
"""任务动作集合（**唯一真源**）：状态 + 源文件是否存在 -> 可执行动作。

为什么单独成模块：任务详情弹窗、队列行内按钮、右栏「需要处理」三处都要用同一套动作
口径；其中两处（`ui.pages` / `ui.widgets.tasks`）反向依赖 `ui.dialogs`——若把本逻辑留在
`dialogs/task_details.py` 里，任何一侧引用都会在**模块级导入图**上成环
（`test_import_graph_acyclic` 会拦下）。放在这个**叶子模块**（只依赖 widgets.common，
不依赖 pages / widgets.tasks / dialogs）里，三处都能安全引用。

出路语义（2026-09-26 起，2026-09-29 统一）：
- 「从队列移除」(ignore) = **软取消**：置 canceled 终态，**保留**记录与日志；
- 「忽略」(mark_done) = **手动转为完成**：把失败 / 队列中的条目置 done 终态，并在该任务
  日志里补一条注明「人工转换」的记录（绝不冒充真实解压产出）；
- 原先的「删除记录」(delete) 硬删除入口已按要求从任务详情弹窗移除——宿主侧的 `delete`
  处理器**仍然保留**（硬删除能力不丢，只是界面不再暴露入口）。

**动作可用性按「是否终态」统一**：
- 非终态（queued / extracting / need_password / failed）：**忽略** + **从队列移除**都给
  （正动作在前，danger 恒在末位）；
- 终态（done / canceled）：只给「从队列移除」——终态已无可「忽略」者，但任何条目都该能
  从详情窗 / 行内清出队列（与宿主 `_ignore_task` 的「任何状态都可执行」一致）。
"""
import os
from pathlib import Path

from .widgets.common import _task_state_key


def resolve_source_path(task, max_dirs=800):
    """定位任务源文件的真实路径；找不到返回 None。

    先在 source_dir 直系找，再**下探一层子目录**（限量 800，跳 "_"/"." 前缀）。
    为什么要下探：百度清单模式（Tier-2）会把分享下载到**子目录**里，而历史任务行
    记录的 source_dir 是监听根（E:/test），真实文件在 E:/test/1031/xxx —— 只查直系
    就会误判"源文件已不在监听目录"，连重试按钮都不给（2026-11-10 真机）。
    """
    try:
        src = str((task or {}).get("source_dir") or "")
        name = str((task or {}).get("file_name") or "")
        if not src or not name:
            return None
        direct = os.path.join(src, name)
        if os.path.isfile(direct):
            return direct
        if not os.path.isdir(src):
            return None
        n = 0
        with os.scandir(src) as it:
            for entry in it:
                n += 1
                if n > max_dirs:
                    break
                try:
                    if not entry.is_dir() or entry.name.startswith(("_", ".")):
                        continue
                except OSError:
                    continue
                cand = os.path.join(entry.path, name)
                if os.path.isfile(cand):
                    return cand
        return None
    except Exception:
        return None


def source_exists(task):
    """源文件是否还能找到（重试类动作的成立条件）：直系或一级子目录里存在即算。"""
    return resolve_source_path(task) is not None


def task_action_set(state, src_exists):
    """按状态 + 源文件是否存在计算动作集合 -> [(kind, label, role)]。

    role: "primary" / "danger" / ""（默认样式）。不变量：任何输入都至少返回一个动作
    ——未知状态兜底为「从队列移除」，任务永远不会无处可去。语义见模块 docstring。
    """
    s = _task_state_key({"state": state})
    acts = []
    if s == "need_password":
        acts.append(("input_password", "跳转到密码本", "primary"))
        if src_exists:
            acts.append(("retry", "重试", ""))
        acts.append(("open_dir", "打开输出目录", ""))
        acts.append(("mark_done", "忽略", ""))
        acts.append(("ignore", "从队列移除", "danger"))
    elif s == "failed":
        if src_exists:
            acts.append(("retry", "重试", ""))
        acts.append(("copy_error", "复制错误", ""))
        acts.append(("open_dir", "打开输出目录", ""))
        acts.append(("mark_done", "忽略", ""))
        acts.append(("ignore", "从队列移除", "danger"))
    elif s in ("queued", "extracting"):
        acts.append(("open_dir", "打开输出目录", ""))
        acts.append(("mark_done", "忽略", ""))
        acts.append(("ignore", "从队列移除", "danger"))
    elif s == "done":
        acts.append(("open_dir", "打开输出目录", ""))
        acts.append(("copy_output", "复制输出去向", ""))
        acts.append(("ignore", "从队列移除", "danger"))
    elif s == "canceled":
        if src_exists:
            acts.append(("retry", "重试", ""))
        acts.append(("ignore", "从队列移除", "danger"))
    if not acts:
        acts = [("ignore", "从队列移除", "danger")]
    return acts
