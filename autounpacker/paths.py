# -*- coding: utf-8 -*-
"""路径常量：数据文件统一放在项目根目录，各模块经 `paths.X` 引用。

职责：- 定义 PROJECT_ROOT / DATA_DIR / CONFIG_FILE / TEMP_PW_FILE / CRASH_LOG / LOGS_DIR 等常量
- 测试可重定向 DATA_DIR（或直接改 CONFIG_FILE 等），各模块统一引用保证全局生效
关键入口：无（纯常量模块）
依赖：仅 pathlib
注意：各模块统一 `from . import paths` 后以 `paths.X` 引用，保证重定向全局生效
"""
from pathlib import Path

PACKAGE_DIR = Path(__file__).resolve().parent
PROJECT_ROOT = PACKAGE_DIR.parent
DATA_DIR = PROJECT_ROOT
CONFIG_FILE = DATA_DIR / "config.json"
TEMP_PW_FILE = DATA_DIR / "temp_passwords.json"
CRASH_LOG = DATA_DIR / "crash.log"
LOGS_DIR = DATA_DIR / "logs"
CACHE_DIR = DATA_DIR / "cache"        # 运行时生成的缓存（如主题图标），可随时重建
WORKERS_DIR = PACKAGE_DIR / "workers"
SINGLE_INSTANCE_EVENT = "Local\\AutoUnpacker_ShowEvent"
