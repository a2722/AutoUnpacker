"""autounpacker.workers——后台工作模块包入口。

本包存放二维码识别、剪贴板读取等 worker 模块。此文件仅作包标记与模块说明：
不导入任何子模块或第三方库，保证 `import autounpacker.workers` 轻量且无副作用。
"""


def worker_command(name, *args):
    """构造 worker 子进程 argv（源码运行与冻结运行统一入口）。

    源码运行（dev）：沿用历史的命令
        [sys.executable, <workers>/<name>_worker.py, *args]
    冻结运行：PyInstaller 下 sys.executable 是打包 exe，直接跑 .py 路径会
        重开 GUI；改为自律式分发
        [sys.executable, "--run-worker", name, *args]
        由 app.main() 在导入 Qt 前转交给对应 worker 的 main()。

    `name` 为 "qr"、"clipboard" 或 "zip"。paths 延迟导入，保持本包导入零副作用。
    """
    import sys
    if getattr(sys, "frozen", False):
        return [sys.executable, "--run-worker", name, *args]
    from .. import paths
    return [sys.executable, str(paths.WORKERS_DIR / f"{name}_worker.py"), *args]
