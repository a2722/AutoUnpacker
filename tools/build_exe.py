# -*- coding: utf-8 -*-
"""一键把本项目打成 Windows 绿色版（PyInstaller onedir）。

做四件事：
1. 用程序自己的托盘图标画一份 build_assets\\AutoUnpacker.ico（离屏渲染，不弹窗）；
2. 从 autounpacker.__version__ 生成 Windows 版本信息资源 build_assets\\version_info.txt；
3. 调 PyInstaller 跑仓库根的 AutoUnpacker.spec，产出 dist\\AutoUnpacker\\；
4. --zip 时再打成 dist\\AutoUnpacker-<版本>-win64.zip，方便拷到干净机器上试。

为什么图标要自己生成：本项目没有任何图片资源，窗口 / 托盘图标是 QPainter 画的
（ui/widgets/inputs.py 的 make_tray_icon），所以 exe 图标也从同一处取，保证一致。

用法：
    python tools\\build_exe.py            # 干净重建
    python tools\\build_exe.py --no-clean # 复用上次的 build 缓存
    python tools\\build_exe.py --zip      # 顺便打 zip
"""
from __future__ import annotations

import shutil
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
ASSETS = ROOT / "build_assets"
SPEC = ROOT / "AutoUnpacker.spec"
DIST = ROOT / "dist" / "AutoUnpacker"


def _version() -> str:
    sys.path.insert(0, str(ROOT))
    import autounpacker
    return autounpacker.__version__


def _gen_icon() -> bool:
    """离屏渲染托盘图标 → 多尺寸 ico。失败只警告，不阻断打包。"""
    try:
        import os
        os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
        sys.path.insert(0, str(ROOT))
        from PyQt5.QtWidgets import QApplication
        _app = QApplication.instance() or QApplication([])
        from autounpacker.ui.widgets.inputs import make_tray_icon
        from PIL import Image

        ASSETS.mkdir(parents=True, exist_ok=True)
        png = ASSETS / "_icon_src.png"
        # make_tray_icon() 只提供 64x64 的位图；放大会糊，所以按需要平滑放大到 256 再交给 Pillow 合成多尺寸 ico
        from PyQt5.QtCore import Qt
        pm = make_tray_icon().pixmap(256, 256)
        if pm.width() < 256 or pm.height() < 256:
            pm = pm.scaled(256, 256, Qt.KeepAspectRatio, Qt.SmoothTransformation)
        if not pm.save(str(png), "PNG"):
            print("  [warn] 托盘图标渲染失败，跳过 exe 图标")
            return False
        img = Image.open(png)
        sizes = [(s, s) for s in (16, 24, 32, 48, 64, 128, 256)]
        img.save(str(ASSETS / "AutoUnpacker.ico"), sizes=sizes)
        png.unlink(missing_ok=True)
        return True
    except Exception as e:  # 图标不是关键路径，绝不让它挡住打包
        print("  [warn] 生成图标失败（将不设置 exe 图标）：%s" % e)
        return False


_FMT = """\
VSVersionInfo(
  ffi=FixedFileInfo(
    filevers=({a}, {b}, {c}, 0),
    prodvers=({a}, {b}, {c}, 0),
    mask=0x3f, flags=0x0, OS=0x40004, fileType=0x1, subtype=0x0, date=(0, 0)
  ),
  kids=[
    StringFileInfo([
      StringTable(
        '080404B0',
        [StringStruct('CompanyName', 'AutoUnpacker'),
         StringStruct('FileDescription', 'AutoUnpacker'),
         StringStruct('FileVersion', '{v}'),
         StringStruct('InternalName', 'AutoUnpacker'),
         StringStruct('LegalCopyright', 'GPL-3.0-only'),
         StringStruct('OriginalFilename', 'AutoUnpacker.exe'),
         StringStruct('ProductName', 'AutoUnpacker'),
         StringStruct('ProductVersion', '{v}')])
    ]),
    VarFileInfo([VarStruct('Translation', [2052, 1200])])
  ]
)
"""


def _gen_version_info(v: str) -> bool:
    try:
        parts = [int(x) if x.isdigit() else 0 for x in (v.split(".") + ["0", "0", "0"])][:4]
        ASSETS.mkdir(parents=True, exist_ok=True)
        (ASSETS / "version_info.txt").write_text(
            _FMT.format(a=parts[0], b=parts[1], c=parts[2], v=v), encoding="utf-8"
        )
        return True
    except Exception as e:
        print("  [warn] 生成版本信息失败：%s" % e)
        return False


def main(argv: list[str]) -> int:
    if not SPEC.exists():
        print("找不到 %s" % SPEC)
        return 2
    clean = "--no-clean" not in argv
    want_zip = "--zip" in argv
    v = _version()
    print("=== AutoUnpacker %s 打包（onedir） ===" % v)
    print("[1/4] 生成 exe 图标      :", "OK" if _gen_icon() else "跳过")
    print("[2/4] 生成版本信息资源  :", "OK" if _gen_version_info(v) else "跳过")
    print("[3/4] 调 PyInstaller ...")
    cmd = [sys.executable, "-m", "PyInstaller", "--noconfirm"]
    if clean:
        cmd.append("--clean")
    cmd.append(str(SPEC))
    # 低优先级跑：打包本身很吃 CPU（opencv/numpy 一堆二进制），
    # 这台机器上还开着别的东西时别去抢（用户可能正在玩游戏）。
    _flags = getattr(subprocess, "BELOW_NORMAL_PRIORITY_CLASS", 0)
    rc = subprocess.run(cmd, cwd=str(ROOT), creationflags=_flags).returncode
    if rc != 0:
        print("!! PyInstaller 失败 rc=%d" % rc)
        return rc
    exe = DIST / "AutoUnpacker.exe"
    if not DIST.exists() or not exe.exists():
        print("!! 未产出 %s" % exe)
        return 3
    files = [p for p in DIST.rglob("*") if p.is_file()]
    total = sum(p.stat().st_size for p in files)
    print("[4/4] 产物 : %s" % DIST)
    print("      文件数 %d，合计 %.1f MB，exe 本体 %.1f MB"
          % (len(files), total / 1048576.0, exe.stat().st_size / 1048576.0))
    if want_zip:
        zp = ROOT / "dist" / ("AutoUnpacker-%s-win64.zip" % v)
        if zp.exists():
            zp.unlink()
        print("      打 zip → %s" % zp)
        shutil.make_archive(str(zp.with_suffix("")), "zip", root_dir=str(DIST.parent),
                            base_dir=DIST.name)
        print("      zip 大小 %.1f MB" % (zp.stat().st_size / 1048576.0))
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
