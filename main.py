# -*- coding: utf-8 -*-
"""兼容启动入口：把控制权交给包内的 app.main()（推荐使用 `python -m autounpacker`）。

职责：- 把项目根目录插入 sys.path，使 main.py 可直接运行
关键入口：autounpacker.app 的 main()（本文件仅转发）
依赖：autounpacker.app
注意：本文件不含业务逻辑；真正的入口流程在 autounpacker/app.py 的 main()
"""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

from autounpacker.app import main  # noqa: E402

if __name__ == "__main__":
    main()
