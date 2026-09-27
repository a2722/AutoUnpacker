# -*- coding: utf-8 -*-
"""做一个「7-Zip 免安装小包」（AutoUnpacker-7zip.zip），供程序零 UAC 装解压引擎。

为什么必须有它（都有实测依据）：
  - 官方对 Windows 只给 .exe / .msi / -extra.7z 三种形态，没有 .zip：
      * .exe 与 .msi 的 manifest 是 requireAdministrator —— 装一次必弹 UAC；
      * -extra.7z 是 LZMA 压缩，而 Windows 自带的 tar.exe（bsdtar 3.5.2 /
        libarchive 3.5.2）**没编 LZMA codec**，实测直接报
        "tar.exe: LZMA codec is unsupported"，解不了。
所以由维护者把 7z.exe + 7z.dll（含 License/readme）打成一个**普通 zip**，
作为本仓库的 release 资产发布；程序侧就地取用或下载后用标准库 zipfile 解到
%APPDATA%\\AutoUnpacker\\7z\\bin —— 全程不需要管理员权限，也不依赖网络页面格式。

来源目录（按顺序自动找，也可 --from 指定）：
  1) %APPDATA%\\AutoUnpacker\\7z\\bin   （本程序自己装过的隔离版）
  2) C:\\Program Files\\7-Zip 、C:\\Program Files (x86)\\7-Zip
  3) --from <目录>

用法：
    python tools\\make_7zip_bundle.py                       # 产出 dist\\AutoUnpacker-7zip.zip
    python tools\\make_7zip_bundle.py --upload --tag v2.2.3  # 顺便传到该 release 资产
"""
from __future__ import annotations

import argparse
import hashlib
import os
import subprocess
import sys
import zipfile
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

# 只装这三类文件：7z.exe 负责解压、7z.dll 里带 rar/7z 等编解码器、License 是合规要求
WHITELIST = ("7z.exe", "7z.dll", "License.txt", "readme.txt")
BUNDLE_NAME = "AutoUnpacker-7zip.zip"


def _candidates():
    out = []
    appdata = os.environ.get("APPDATA")
    if appdata:
        out.append(Path(appdata) / "AutoUnpacker" / "7z" / "bin")
    for env in ("ProgramFiles", "ProgramFiles(x86)"):
        base = os.environ.get(env)
        if base:
            out.append(Path(base) / "7-Zip")
    return out


def _find_source(explicit):
    """返回第一个同时有 7z.exe 与 7z.dll 的目录。"""
    dirs = [Path(explicit)] if explicit else _candidates()
    for d in dirs:
        try:
            if (d / "7z.exe").is_file() and (d / "7z.dll").is_file():
                return d
        except OSError:
            continue
    return None


def _sha256(p):
    h = hashlib.sha256()
    with open(p, "rb") as f:
        for chunk in iter(lambda: f.read(1024 * 256), b""):
            h.update(chunk)
    return h.hexdigest()


def main(argv):
    ap = argparse.ArgumentParser(add_help=True)
    ap.add_argument("--from", dest="src", default="", help="7z.exe/7z.dll 所在目录")
    ap.add_argument("--out", dest="out", default=str(ROOT / "dist" / BUNDLE_NAME))
    ap.add_argument("--upload", action="store_true", help="用 gh 传到 release 资产")
    ap.add_argument("--tag", dest="tag", default="", help="上传到哪个 tag（需为当前 Latest）")
    args = ap.parse_args(argv)

    src = _find_source(args.src)
    if src is None:
        print("找不到同时含 7z.exe 与 7z.dll 的目录；用 --from 指定。")
        return 2
    print("来源目录 : %s" % src)

    from autounpacker import sevenzip as sz
    ver = sz.get_version(src / "7z.exe")
    print("7z 版本  : %s（>=18 才支持经 stdin 传密码：%s）"
          % (sz.version_text(src / "7z.exe"), sz.check_version_ok(src / "7z.exe")))
    if not sz.check_version_ok(src / "7z.exe"):
        print("!! 版本过低，拒绝打包（低于 18.00 无法安全传密码）。")
        return 3

    out = Path(args.out)
    out.parent.mkdir(parents=True, exist_ok=True)
    if out.exists():
        out.unlink()
    members = []
    with zipfile.ZipFile(out, "w", zipfile.ZIP_DEFLATED) as zf:
        for name in WHITELIST:
            p = src / name
            if p.is_file():
                zf.write(p, name)          # 只放顶层文件名，绝不带目录
                members.append(name)
    if "7z.exe" not in members or "7z.dll" not in members:
        print("!! 打包内容不完整：%s" % members)
        return 4
    print("产物     : %s" % out)
    print("大小     : %.2f MB" % (out.stat().st_size / 1048576.0))
    print("成员     : %s" % members)
    digest = _sha256(out)
    print("SHA256   : %s" % digest)
    print("校验一下（应当能读出成员）: %s" % zipfile.ZipFile(out).namelist())

    if args.upload:
        if not args.tag:
            print("!! --upload 需要同时给 --tag（例如 --tag v2.2.3）")
            return 5
        print("上传到 release %s ..." % args.tag)
        r = subprocess.run(["gh", "release", "upload", args.tag, str(out), "--clobber"],
                           cwd=str(ROOT))
        if r.returncode != 0:
            print("!! 上传失败 rc=%d" % r.returncode)
            return r.returncode
        print("已上传。程序侧稳定地址（永远取最新 release）：")
        print("  https://github.com/a2722/AutoUnpacker/releases/latest/download/%s"
              % BUNDLE_NAME)
    else:
        print("（未上传）想发资产：gh release upload <tag> \"%s\"" % out)
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
