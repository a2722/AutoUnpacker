# -*- coding: utf-8 -*-
"""生成发布方「冻结更新包资产」与校验和（发版时运行一次）。

为什么需要它：一键更新**优先下载本资产**（见 `autounpacker/updater.py` 顶部注释）。
GitHub 的 `/archive/refs/tags/<tag>.zip` 是**服务端即时生成**的，同一 tag 的字节不
保证永久不变，因此无法为它给出长期有效的 SHA256；本脚本用 `git archive` 生成一份
**字节冻结**的更新包，并算出与它**逐字对应**的校验和，这样更新器里的强制校验才成立。

用法（在仓库任意位置都可运行，脚本会自己找仓库根）：
    python tools/make_release_asset.py v2.2.1
    python tools/make_release_asset.py v2.2.1 --asset-only     # 只传 zip，不传校验和
    python tools/make_release_asset.py v2.2.1 --upload         # 生成后直接 gh 上传
    python tools/make_release_asset.py v2.2.1 --out D:\\tmp      # 指定产物目录

产物：
    AutoUnpacker-<版本号>.zip   形状与 GitHub 源码包一致（**单顶层目录**）
    SHA256SUMS.txt              一行：`<64位hex>␠␠AutoUnpacker-<版本号>.zip`

⚠️ 发布安全提醒（重要，别踩）：
**在「能识别下载来源」的版本成为当前版本之前，不要在 Release 上公开 SHA256SUMS。**
旧版更新器（≤ 2.2.0）不认识冻结资产，只会无条件下载 GitHub 源码归档，**却仍会去取
SHA256SUMS 做强制校验**；而这份校验和只对应我们冻结的 zip，两者必然不符 →
旧用户的「一键更新」会被「SHA256 校验失败」硬挡住（程序不会坏，但更新按钮失效，
必须手动下载一次）。
因此：**首个带该资产的版本请加 `--asset-only`（只传 zip）**；等旧版本基本退场后，
再用默认模式补传 SHA256SUMS——届时新版会优先下载资产并正确校验。

安全：本脚本**只读** git 历史（`git archive` 不写工作树、不含 .git），
绝不接触 config.json / toolbox.db / logs / backup 等用户数据。
"""
import argparse
import hashlib
import subprocess
import sys
import zipfile
from pathlib import Path


def _repo_root():
    """向上找第一个含 .git 的目录。"""
    here = Path(__file__).resolve()
    for p in [here.parent] + list(here.parents):
        if (p / ".git").exists():
            return p
    return here.parents[1]


def sha256_file(path):
    """流式计算 SHA256（小写十六进制）。"""
    h = hashlib.sha256()
    with open(path, "rb") as f:
        while True:
            chunk = f.read(1024 * 1024)
            if not chunk:
                break
            h.update(chunk)
    return h.hexdigest()


def main(argv=None):
    ap = argparse.ArgumentParser(description="生成冻结更新包资产与 SHA256SUMS")
    ap.add_argument("tag", help="已存在的 tag，例如 v2.2.1")
    ap.add_argument("--out", default=".", help="产物输出目录（默认当前目录）")
    ap.add_argument("--upload", action="store_true",
                    help="生成后调用 gh release upload 上传到该 tag 的 Release")
    ap.add_argument("--asset-only", action="store_true",
                    help="只产出 zip，不产 SHA256SUMS（首个带资产的版本必须用它，"
                         "否则旧版更新器会拿资产哈希去校验源码归档而硬失败）")
    args = ap.parse_args(argv)

    tag = str(args.tag or "").strip()
    bare = tag.lstrip("vV")
    if not tag or not bare:
        print("错误：tag 不能为空，例如 v2.2.1")
        return 2

    root = _repo_root()
    out = Path(args.out).resolve()
    out.mkdir(parents=True, exist_ok=True)
    prefix = "AutoUnpacker-%s/" % bare
    zip_path = out / ("AutoUnpacker-%s.zip" % bare)
    sums_path = out / "SHA256SUMS.txt"

    # 1) git archive：形状与 GitHub 源码包一致（单顶层目录 + 同一批 tracked 文件）
    cmd = ["git", "-C", str(root), "archive", "--format=zip",
           "--prefix=" + prefix, "-o", str(zip_path), tag]
    try:
        r = subprocess.run(cmd, capture_output=True, text=True)
    except FileNotFoundError:
        print("错误：找不到 git，请确认它在 PATH 中")
        return 1
    if r.returncode != 0 or not zip_path.is_file():
        print("错误：git archive 失败：%s" % (r.stderr or r.stdout or "未知原因"))
        return 1

    # 2) 形状自检：单顶层目录 + 含 autounpacker 包（与 updater._extract_zip 的假设一致）
    try:
        with zipfile.ZipFile(zip_path) as zf:
            names = zf.namelist()
    except Exception as e:
        print("错误：生成的 zip 无法读取：%s" % e)
        return 1
    if not names:
        print("错误：生成的 zip 是空的")
        return 1
    tops = {n.split("/", 1)[0] for n in names}
    has_pkg = any(n.startswith(prefix + "autounpacker/") for n in names)
    if tops != {prefix.rstrip("/")} or not has_pkg:
        print("错误：zip 形状不符合更新器预期（顶层=%r，含 autounpacker=%s）"
              % (sorted(tops), has_pkg))
        return 1

    # 3) 校验和：**与刚生成的这一份字节逐字对应**
    digest = sha256_file(zip_path)
    if args.asset_only:
        sums_path = None
    else:
        sums_path.write_text("%s  %s\n" % (digest, zip_path.name),
                             encoding="utf-8", newline="\n")

    size_mb = zip_path.stat().st_size / 1024.0 / 1024.0
    print("已生成：%s（%.2f MB，%d 个条目）" % (zip_path, size_mb, len(names)))
    if sums_path is None:
        print("已按 --asset-only 跳过 SHA256SUMS（首个带资产的版本请这样做）")
    else:
        print("已生成：%s" % sums_path)
    print("  %s  %s" % (digest, zip_path.name))

    if args.upload:
        files = [str(zip_path)] + ([str(sums_path)] if sums_path else [])
        up = ["gh", "release", "upload", tag] + files + ["--clobber"]
        try:
            r2 = subprocess.run(up, capture_output=True, text=True, cwd=str(root))
        except FileNotFoundError:
            print("错误：找不到 gh，请先安装/登录 GitHub CLI")
            return 1
        if r2.returncode != 0:
            print("错误：gh release upload 失败：%s"
                  % (r2.stderr or r2.stdout or "未知原因"))
            return 1
        print("已上传到 Release %s（资产：%s）"
              % (tag, " + ".join(Path(f).name for f in files)))
    else:
        print("下一步（手动上传）：")
        _files = [str(zip_path)] + ([str(sums_path)] if sums_path else [])
        print("  gh release upload %s %s" % (tag, " ".join('"%s"' % f for f in _files)))
    return 0


if __name__ == "__main__":
    sys.exit(main())
