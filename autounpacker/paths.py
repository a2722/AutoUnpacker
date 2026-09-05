# -*- coding: utf-8 -*-
"""路径常量：代码位于 autounpacker/ 包，数据文件统一放在项目根目录。

测试可重定向 DATA_DIR（或直接改 CONFIG_FILE 等），各模块统一经
``from . import paths`` 后以 ``paths.X`` 引用，保证重定向全局生效。"""
from pathlib import Path

PACKAGE_DIR = Path(__file__).resolve().parent
PROJECT_ROOT = PACKAGE_DIR.parent
DATA_DIR = PROJECT_ROOT
CONFIG_FILE = DATA_DIR / "config.json"
TEMP_PW_FILE = DATA_DIR / "temp_passwords.json"
CRASH_LOG = DATA_DIR / "crash.log"
LOGS_DIR = DATA_DIR / "logs"
WORKERS_DIR = PACKAGE_DIR / "workers"
SINGLE_INSTANCE_EVENT = "Local\\AutoUnpacker_ShowEvent"
