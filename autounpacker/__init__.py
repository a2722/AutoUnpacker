# -*- coding: utf-8 -*-
"""AutoUnpacker - 剪贴板二维码识别 / 多路径智能解压监听。

启动方式：
    python -m autounpacker    # 推荐
    python main.py            # 兼容入口

==================================================================
模块地图（按职责分组；**改代码前先看这张表，不必通读大文件**）
------------------------------------------------------------------
入口 / 装配
  main.py                     进程入口（转发到 autounpacker.app）
  app.py                      建 state/hub、起各后台线程、装 Qt、启动主窗口
  paths.py                    路径常量（PROJECT_ROOT/DATA_DIR/日志/单实例事件名）
  utils.py                    杂项工具（开机计时、崩溃日志安装、路径规范化）

配置 / 状态 / 事件
  config.py                   默认配置、读写、迁移、sanitize（含 watch_paths.mode）
  state.py                    AppState：snapshot() 快照 / set() 写入 / update_path()
  hub.py                      Hub：log() 日志 + notify() 通知（按 NOTIFY_KEYS 过滤）
  db.py                       toolbox.db：密码本、密码字典、百度粘性记忆 baidu_sticky

监听 / 解压（核心）
  monitors.py                 FolderWatcher（表层模式 + 百度清单模式 Tier-2）、
                              QRMonitor（剪贴板二维码/链接）、翻译 JSON 归位
  extract.py                  解压核心：7z 调用、分卷判定、promote 抬升、删源到回收站、
                              归档内部穿透（--max-depth）；含 CLI 自测入口
  sevenzip.py                 7-Zip 可执行文件检测/下载/调用封装
  trail.py                    删除回溯（回收站操作 + 记录）
  password_book.py            密码本数据源（落 toolbox.db）

二维码 / 链接
  qr_decode.py                二维码解码（图片 → 文本）
  trust.py                    网址信任名单（新域名动作、白/黑名单、落库）
  workers/qr_worker.py        二维码识别子进程（强制 UTF-8 输出，防乱码）
  workers/clipboard_worker.py 剪贴板读取子进程

百度网盘任务库（实验性，只读）
  baidu_task.py               ★门面：只转发，保持既有导入路径不变
  baidu_db.py                 只读访问层（选库、短连接查询、列容错、读活动/历史）
  baidu_manifest.py           批次/分卷还原 + 任务跟踪事件（纯逻辑、无线程）
  baidu_watch.py              轮询线程（进程守卫/自适应间隔/退避）+ 诊断 + 启动探测

更新 / 界面
  updater.py                  GitHub Releases 版本检查 + 下载/校验/update.bat
  ui/main_window.py           主窗口、托盘、监听卡片列表、日志面板
  ui/dialogs.py               设置对话框（各页）、更新页、网盘任务库诊断
  ui/widgets.py               通用控件（WatchCard 监听卡片、热键输入、托盘图标等）
  ui/style.py                 QSS 主题样式

阅读建议：先读本表 → 再读目标模块顶部的 docstring（职责/入口/依赖/注意）→
只在必要时才通读实现。安全红线集中在 extract.py（删源）与 baidu_db.py（只读）。
==================================================================
"""
from .paths import (PROJECT_ROOT, DATA_DIR, CONFIG_FILE, TEMP_PW_FILE,
                    CRASH_LOG, LOGS_DIR, WORKERS_DIR, SINGLE_INSTANCE_EVENT)
from .config import DEFAULT_CONFIG, load_config, save_config
from .state import AppState
from .hub import Hub, StdoutCapture
from .utils import (_boot_tick, _boot_time, _norm_path_for_cfg,
                    _can_open_append, _install_crash_log)

__version__ = "1.1.3"
__all__ = [
    "PROJECT_ROOT", "DATA_DIR", "CONFIG_FILE", "TEMP_PW_FILE", "CRASH_LOG",
    "LOGS_DIR", "WORKERS_DIR", "SINGLE_INSTANCE_EVENT",
    "DEFAULT_CONFIG", "load_config", "save_config",
    "AppState", "Hub", "StdoutCapture",
    "_boot_tick", "_boot_time", "_norm_path_for_cfg", "_can_open_append",
    "_install_crash_log", "__version__",
]
