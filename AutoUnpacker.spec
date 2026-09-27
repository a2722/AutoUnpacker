# -*- mode: python ; coding: utf-8 -*-
"""AutoUnpacker 的 PyInstaller 打包配置（onedir，不是 onefile）。

为什么必须 onedir：
- 数据目录 `paths.DATA_DIR` 在冻结态取「exe 所在目录（可写时），否则 %APPDATA%\\AutoUnpacker」。
  onefile 会把程序解到 %TEMP%\\_MEIxxxx 且**退出即删** —— 配置 / 密码本 / 日志 / 缓存会每次丢失；
- 主程序用子进程跑 workers（qr_worker / clipboard_worker），onefile 每次都要重新解包，
  启动更慢、更容易被安全软件盯上；
- onedir 启动更快、体积可控，用户把整个文件夹拷走即用（绿色版），也便于以后做增量更新。

产物：dist\\AutoUnpacker\\AutoUnpacker.exe  +  dist\\AutoUnpacker\\_internal\\

用法：python tools\\build_exe.py（会先生成图标与版本信息，再调本文件）
"""
from pathlib import Path

ROOT = Path(SPECPATH)
ASSETS = ROOT / "build_assets"

# 运行时被 import 但静态分析看不到的模块
hiddenimports = [
    "autounpacker.qr_decode",
    "autounpacker.workers",
    "autounpacker.workers.qr_worker",
    "autounpacker.workers.clipboard_worker",
    "PIL",
    "PIL.Image",
    "PIL.ImageFile",
    "PIL.PngImagePlugin",
    "cv2",
    "pyzbar",
    "pyzbar.pyzbar",
    "pyzbar.wrapper",
    "win32clipboard",
    "win32event",
    "win32api",
    "winerror",
    "pythoncom",
    "pywintypes",
]

# 仓库根目录就摆着这两颗原生库：pyzbar 靠它解码二维码、libiconv 是它的依赖。
# 目标 "." = 打包根（PyInstaller 6 里就是 _internal\\，与 sys._MEIPASS 一致），
# 正好是 paths.dll_dirs() 在冻结态会加进 DLL 搜索路径的地方。
binaries = [
    (str(ROOT / "libzbar-64.dll"), "."),
    (str(ROOT / "libiconv.dll"), "."),
]

# 本项目没有任何图片/字体/QSS 资源文件：图标与样式全是代码画的，主题 PNG 运行时自建。
datas = []

# 明确排掉不用的重家伙（Qt 的多媒体/WebEngine/QML 等绝无引用）
excludes = [
    "tkinter",
    "matplotlib",
    "scipy",
    "pandas",
    "IPython",
    "notebook",
    "PyQt5.QtWebEngineWidgets",
    "PyQt5.QtWebEngineCore",
    "PyQt5.QtQuick",
    "PyQt5.QtQml",
    "PyQt5.QtMultimedia",
    "PyQt5.QtMultimediaWidgets",
    "PyQt5.QtDesigner",
    "PyQt5.QtSql",
    "PyQt5.QtTest",
    "PyQt5.QtBluetooth",
    "PyQt5.QtNfc",
    "PyQt5.QtPositioning",
]

a = Analysis(
    [str(ROOT / "main.py")],
    pathex=[str(ROOT)],
    binaries=binaries,
    datas=datas,
    hiddenimports=hiddenimports,
    hookspath=[],
    hooksconfig={},
    runtime_hooks=[],
    excludes=excludes,
    noarchive=False,
)

pyz = PYZ(a.pure)

_exe_kw = dict(
    name="AutoUnpacker",
    debug=False,
    bootloader_ignore_signals=False,
    strip=False,
    upx=False,
    console=False,       # GUI 程序：不要黑框
    disable_windowed_traceback=False,
)
_ico = ASSETS / "AutoUnpacker.ico"
if _ico.exists():
    _exe_kw["icon"] = str(_ico)
_ver = ASSETS / "version_info.txt"
if _ver.exists():
    _exe_kw["version"] = str(_ver)

exe = EXE(pyz, a.scripts, [], exclude_binaries=True, **_exe_kw)

coll = COLLECT(
    exe,
    a.binaries,
    a.datas,
    strip=False,
    upx=False,
    name="AutoUnpacker",
)
