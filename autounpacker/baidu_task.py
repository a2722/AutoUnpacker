# -*- coding: utf-8 -*-
"""门面：百度网盘任务库（只读）——保持既有导入路径 `autounpacker.baidu_task` 不变。

本文件**只有转发**，实现在三个子模块里（拆分是为了让后来者/AI 不必通读大文件）：

- `baidu_db.py`      ：只读访问层（定位/选择库、短连接只读查询、列容错、读活动/历史）
- `baidu_manifest.py`：批次/分卷还原 + 任务跟踪事件（纯逻辑、无线程）
- `baidu_watch.py`   ：后台轮询线程 + 一次性诊断 + 启动探测
- `baidu_share.py`   ：分享链接「拉起客户端下载」全链路（invoke_download）

外部只需 `from . import baidu_task as bt`（或 `from .baidu_task import xxx`），
所有原有名字在此继续可用（含以 `_` 开头的内部名，供测试与 monitors 使用）。
安全铁律见 `baidu_db.py` 顶部注释：只读、短连接、绝不碰 WAL 库。
"""
from .baidu_db import (  # noqa: F401
    DB_NAME,
    _candidate_dbs,
    _registry_install_dir,
    select_task_db,
    find_task_db,
    _ro_uri,
    _query,
    _query_or_none,
    _as_text,
    _COL_CACHE,
    _COL_TABLES,
    _table_columns,
    _select,
    read_tasks,
    get_active_tasks,
    detect_download_root,
)
from .baidu_manifest import (  # noqa: F401
    _share_root,
    group_batches,
    pair_volumes,
    summarize,
    format_summary,
    format_active,
    _TRACK,
    _norm_path,
    is_enabled,
    remember_sticky,
    sticky_known,
    _hist_match,
    check_duplicate,
    observe_tasks,
    batch_files,
    batch_state,
    expected_files,
    leftover_tasks,
    volume_hint,
    gather_volume_set,
    report_events,
    # 2.F 全链路：分享链接 ↔ 下载任务 关联
    parse_share_url,
    extract_share_ids_from_html,
    parse_share_download,
    remember_share_link,
    share_link_for,
    last_share,
    # 2.F：失效分享页识别 + 进程内标记（monitors 抓取即判定、手势短路用）
    DEAD_SHARE_PREFIX,
    detect_dead_share_page,
    is_dead_share_reason,
    mark_share_dead,
    share_dead,
    # 2.F：提取码候选（**时效版**）——monitors 捕获路径回捞用。
    # 刻意**不**转发无时效的 recent_code_from_history：捕获路径必须严格按时效，
    # 拿陈年旧码去 verify 只会白烧唯一一次、且间隔受限的配额。
    mapped_code,
    fresh_code_from_history,
    CODE_CANDIDATE_TTL,
)
from .baidu_share import invoke_download  # noqa: F401
from .baidu_watch import (  # noqa: F401
    _STATE,
    _BACKOFF_AT,
    _INTERVAL_IDLE,
    _INTERVAL_ACTIVE,
    _INTERVAL_MAX,
    _baidu_running,
    start_active_watcher,
    diagnose,
    probe_and_log,
)

# 子模块本身也一并暴露，便于按职责直接引用（如 bt.manifest.observe_tasks）
from . import baidu_db, baidu_manifest, baidu_watch, baidu_share  # noqa: F401


if __name__ == "__main__":
    import sys

    _arg = sys.argv[1] if len(sys.argv) > 1 else None
    _s = summarize(_arg)
    for _line in format_summary(_s, max_batches=50, max_vols=50):
        print(_line)
