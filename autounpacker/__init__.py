# -*- coding: utf-8 -*-
"""AutoUnpacker - 剪贴板二维码识别 / 多路径智能解压监听。

启动方式：
    python -m autounpacker    # 推荐
    python main.py            # 兼容入口
"""
from .paths import (PROJECT_ROOT, DATA_DIR, CONFIG_FILE, TEMP_PW_FILE,
                    CRASH_LOG, LOGS_DIR, WORKERS_DIR, SINGLE_INSTANCE_EVENT)
from .config import DEFAULT_CONFIG, load_config, save_config
from .state import AppState
from .hub import Hub, StdoutCapture
from .utils import (_boot_tick, _boot_time, _norm_path_for_cfg,
                    _can_open_append, _install_crash_log)

__version__ = "1.0.1"
__all__ = [
    "PROJECT_ROOT", "DATA_DIR", "CONFIG_FILE", "TEMP_PW_FILE", "CRASH_LOG",
    "LOGS_DIR", "WORKERS_DIR", "SINGLE_INSTANCE_EVENT",
    "DEFAULT_CONFIG", "load_config", "save_config",
    "AppState", "Hub", "StdoutCapture",
    "_boot_tick", "_boot_time", "_norm_path_for_cfg", "_can_open_append",
    "_install_crash_log", "__version__",
]
