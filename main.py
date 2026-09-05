# -*- coding: utf-8 -*-
"""AutoUnpacker - 兼容启动入口（推荐使用 `python -m autounpacker`）。

本文件仅负责把控制权交给包内的 app.main()。"""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

from autounpacker.app import main  # noqa: E402

if __name__ == "__main__":
    main()
