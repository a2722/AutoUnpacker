# -*- coding: utf-8 -*-
"""版本检查与自动更新模块：向 GitHub Releases API 查询最新版本、下载新版。

设计约束：
- 绝不主动拉取：只有用户点击「检查更新」按钮才发起网络请求；
- 网络失败不弹窗：返回结果由调用方在 UI 里以文本行展示（"无法连接 GitHub"）；
- 版本比较按语义化版本（major.minor.patch，忽略 v 前缀）；
- 自动更新流程：下载新版 zip → 校验 → 解压到临时目录 → 生成 update.bat
  （结束当前进程 → 备份旧代码 → 覆盖新代码 → 重启程序）。数据文件
  （config.json / toolbox.db / temp_passwords.json / deletion_trail.json /
  logs / backup）绝不覆盖。
"""
import json
import os
import shutil
import subprocess
import tempfile
import urllib.request
import uuid
import zipfile
from pathlib import Path

# 与 GitHub 仓库同步：本项目的 owner/repo
GITHUB_REPO = "a2722/AutoUnpacker"
# API 无认证时限制 60 次/小时/IP，每次检查一次足够
RELEASE_LATEST_API = f"https://api.github.com/repos/{GITHUB_REPO}/releases/latest"
# 检查超时（秒）：国内网络访问 GitHub 可能很慢，给足时间但避免无限挂起
CHECK_TIMEOUT = 8
# 下载超时（秒）：新版 zip 可能几十 MB，给更长时间
DOWNLOAD_TIMEOUT = 120

# 返回码：结果状态
STATUS_OK = "ok"             # 成功获取到最新版本
STATUS_FAILED = "failed"     # 网络/解析失败（无法连接 GitHub）

# 数据文件白名单：自动更新时绝不覆盖这些（用户数据/缓存/历史）
DATA_FILE_NAMES = {
    "config.json", "toolbox.db", "temp_passwords.json", "deletion_trail.json",
    "crash.log", "libiconv.dll", "libzbar-64.dll",
}
DATA_DIR_NAMES = {"logs", "backup"}


def compare_versions(local, latest):
    """比较两个语义化版本号字符串。

    返回:
      1  = latest 比 local 新（有可用更新）
      0  = 版本相同
     -1  = latest 比 local 旧（不应发生，防御性处理）
    None = 任一版本无法解析
    """
    def parse(v):
        v = (v or "").strip().lstrip("vV")
        parts = v.split(".")
        nums = []
        for p in parts:
            if not p.isdigit():
                return None
            nums.append(int(p))
        # 补齐三位（1.0 -> 1.0.0）
        while len(nums) < 3:
            nums.append(0)
        return tuple(nums[:3])

    lv = parse(local)
    rv = parse(latest)
    if lv is None or rv is None:
        return None
    if rv > lv:
        return 1
    if rv == lv:
        return 0
    return -1


def check_latest_version():
    """向 GitHub Releases API 查询最新发布版本的 tag_name。

    返回 (status, latest_version 或 None)：
      (STATUS_OK, "v1.1.0")      —— 成功
      (STATUS_FAILED, None)      —— 网络/解析失败（国内访问 GitHub 受限等）
    """
    req = urllib.request.Request(
        RELEASE_LATEST_API,
        headers={
            "User-Agent": "AutoUnpacker/" + _local_version(),
            "Accept": "application/vnd.github+json",
        })
    try:
        with urllib.request.urlopen(req, timeout=CHECK_TIMEOUT) as resp:
            data = json.loads(resp.read().decode("utf-8"))
        tag = (data or {}).get("tag_name")
        if not tag:
            return STATUS_FAILED, None
        return STATUS_OK, str(tag)
    except Exception:
        return STATUS_FAILED, None


def _local_version():
    """本地版本号（延迟导入避免循环依赖：updater 不 import 包）。"""
    try:
        from . import __version__
        return __version__
    except Exception:
        return "0.0.0"


def releases_url():
    """GitHub Releases 页面地址（更新按钮跳转目标）。"""
    return f"https://github.com/{GITHUB_REPO}/releases/latest"


# ==================== 自动更新 ====================

def _archive_url(tag):
    """GitHub 源码 zip 下载地址（无需认证）。"""
    return f"https://github.com/{GITHUB_REPO}/archive/refs/tags/{tag}.zip"


def download_release_zip(tag, dest_dir=None, progress_cb=None):
    """下载指定 tag 的源码 zip。

    GitHub 下载偶发 502/超时（国内网络更常见），自动重试 3 次。
    返回 (status, zip_path 或 None, err_msg)：
      (STATUS_OK, 路径, "")              —— 下载成功
      (STATUS_FAILED, None, "错误信息")   —— 下载失败
    """
    if dest_dir is None:
        dest_dir = tempfile.gettempdir()
    dest_dir = Path(dest_dir)
    dest_dir.mkdir(parents=True, exist_ok=True)
    zip_path = dest_dir / f"autounpacker_{tag}.zip"
    url = _archive_url(tag)
    req = urllib.request.Request(url, headers={
        "User-Agent": "AutoUnpacker/" + _local_version()})
    last_err = ""
    for attempt in range(3):
        try:
            with urllib.request.urlopen(req, timeout=DOWNLOAD_TIMEOUT) as resp:
                total = int(resp.headers.get("Content-Length") or 0)
                done = 0
                with open(zip_path, "wb") as f:
                    while True:
                        chunk = resp.read(256 * 1024)
                        if not chunk:
                            break
                        f.write(chunk)
                        done += len(chunk)
                        if progress_cb and total:
                            progress_cb(done, total)
            if zip_path.stat().st_size < 1024:
                zip_path.unlink(missing_ok=True)
                return STATUS_FAILED, None, "下载内容异常（文件过小）"
            return STATUS_OK, str(zip_path), ""
        except Exception as e:
            last_err = str(e)
            zip_path.unlink(missing_ok=True)
            # 短暂等待后重试（502 通常是瞬时故障）
            if attempt < 2:
                import time
                time.sleep(1.5 * (attempt + 1))
    return STATUS_FAILED, None, f"下载失败（已重试 3 次）: {last_err}"


def _extract_zip(zip_path, dest_dir):
    """安全解压 zip 到 dest_dir，避免路径穿越（zip slip）。返回解压出的顶层目录。"""
    try:
        with zipfile.ZipFile(zip_path) as zf:
            names = zf.namelist()
            if not names:
                return None
            # 校验所有路径安全（不能绝对路径/含 ..）
            for n in names:
                p = Path(n)
                if p.is_absolute() or ".." in p.parts:
                    return None
            zf.extractall(dest_dir)
        # GitHub archive 顶层是一个目录：AutoUnpacker-<tag>/
        top = Path(dest_dir) / Path(names[0]).parts[0]
        return top if top.is_dir() else None
    except Exception:
        return None


def verify_release_zip(zip_path):
    """校验下载的 zip：可解压且包含 autounpacker 包。

    返回 (ok: bool, 解压出的顶层目录 或 None, 错误信息)。
    解压到系统临时目录；成功时调用方负责清理（更新 bat 会删），
    失败时这里清理。
    """
    tmp = Path(tempfile.gettempdir()) / f"autounpacker_stage_{uuid.uuid4().hex[:8]}"
    tmp.mkdir(parents=True, exist_ok=True)
    ok = False
    try:
        top = _extract_zip(zip_path, tmp)
        if top is None:
            return False, None, "压缩包无法解压或路径不安全"
        # 校验关键文件存在
        if not (top / "autounpacker" / "app.py").exists():
            return False, None, "压缩包缺少 autounpacker 包（不是有效更新包）"
        if not (top / "main.py").exists():
            return False, None, "压缩包缺少 main.py（不是有效更新包）"
        ok = True
        return True, str(top), ""
    finally:
        if not ok and tmp.exists():
            shutil.rmtree(tmp, ignore_errors=True)


def _project_root():
    """项目根目录（autounpacker 包的上一级）。"""
    from . import paths
    return Path(paths.PROJECT_ROOT)


def _backup_dir():
    """更新前备份目录：backup\\update_before_<tag>_<时间戳>。"""
    from . import paths
    bk = Path(paths.PROJECT_ROOT) / "backup" / \
        f"update_before_{time_str()}"
    bk.mkdir(parents=True, exist_ok=True)
    return bk


def time_str():
    import time
    return time.strftime("%Y%m%d_%H%M%S")


def _overwrite_tree(src_dir, dst_dir, progress_cb=None):
    """把 src_dir 的内容复制到 dst_dir，覆盖同名文件。

    数据文件白名单内的文件/目录跳过（绝不覆盖用户数据）。
    """
    src_dir = Path(src_dir)
    dst_dir = Path(dst_dir)
    dst_dir.mkdir(parents=True, exist_ok=True)
    copied = 0
    for src in src_dir.rglob("*"):
        if src.is_dir():
            continue
        rel = src.relative_to(src_dir)
        first = rel.parts[0] if rel.parts else ""
        if first in DATA_DIR_NAMES or (len(rel.parts) == 1 and rel.name in DATA_FILE_NAMES):
            continue   # 数据文件/目录不覆盖
        dest = dst_dir / rel
        dest.parent.mkdir(parents=True, exist_ok=True)
        try:
            shutil.copy2(src, dest)
            copied += 1
        except OSError:
            continue
    return copied


def write_update_bat(stage_dir, tag):
    """生成 update.bat（更新执行脚本）。

    流程：结束当前进程 → 备份旧代码 → 覆盖新代码 → 清理临时目录 → 重启程序。
    返回 bat 文件路径。
    """
    root = _project_root()
    bk = _backup_dir()
    stage = Path(stage_dir)
    bat = root / "update.bat"
    # 用当前 pythonw 启动（与用户自启一致）
    pythonw = Path(os.environ.get("PYTHONW") or
                   r"C:\Windows\pyw.exe")
    script = f"""@echo off
chcp 65001 >nul
echo [AutoUnpacker] 正在更新到 {tag} ...
rem 1. 结束当前程序进程（通过 update.bat 独立运行，程序已自行退出）
taskkill /IM pythonw.exe /F >nul 2>&1
timeout /t 2 /nobreak >nul
rem 2. 备份旧代码到 backup 目录
if not exist "{bk}" mkdir "{bk}"
if exist "{root}\\autounpacker" robocopy "{root}\\autounpacker" "{bk}\\autounpacker" /E /NFL /NDL /NJH /NJS >nul 2>&1
if exist "{root}\\main.py" copy /Y "{root}\\main.py" "{bk}\\main.py" >nul 2>&1
if exist "{root}\\requirements.txt" copy /Y "{root}\\requirements.txt" "{bk}\\requirements.txt" >nul 2>&1
if exist "{root}\\README.md" copy /Y "{root}\\README.md" "{bk}\\README.md" >nul 2>&1
if exist "{root}\\CHANGELOG.md" copy /Y "{root}\\CHANGELOG.md" "{bk}\\CHANGELOG.md" >nul 2>&1
if exist "{root}\\LICENSE" copy /Y "{root}\\LICENSE" "{bk}\\LICENSE" >nul 2>&1
if exist "{root}\\.gitignore" copy /Y "{root}\\.gitignore" "{bk}\\.gitignore" >nul 2>&1
if exist "{root}\\config.example.json" copy /Y "{root}\\config.example.json" "{bk}\\config.example.json" >nul 2>&1
rem 3. 用新版覆盖（跳过数据文件：config/toolbox.db/temp_passwords/deletion_trail/logs/backup）
robocopy "{stage}" "{root}" /E /NFL /NDL /NJH /NJS /XD logs backup /XF config.json toolbox.db temp_passwords.json deletion_trail.json crash.log >nul 2>&1
rem 4. 清理临时目录
if exist "{root}\\.update_stage" rmdir /S /Q "{root}\\.update_stage" >nul 2>&1
rem 5. 重启程序
start "" "{pythonw}" "{root}\\main.py" --autostart
echo [AutoUnpacker] 更新完成，程序已重启。
"""
    bat.write_text(script, encoding="utf-8")
    return str(bat)


def apply_update(tag, progress_cb=None):
    """执行自动更新：下载 → 校验 → 解压 → 生成 bat → 启动 bat。

    返回 (status, message)：
      (STATUS_OK, "更新已完成，程序即将重启") —— 成功
      (STATUS_FAILED, "错误信息")             —— 任一步骤失败
    """
    def _progress(done, total):
        if progress_cb:
            pct = int(done * 100 / total) if total else 0
            progress_cb(f"正在下载更新包… {pct}%")

    # 1. 下载
    st, zip_path, err = download_release_zip(tag, progress_cb=_progress)
    if st != STATUS_OK:
        return STATUS_FAILED, err or "下载更新包失败"
    # 2. 校验 + 解压
    ok, stage, err = verify_release_zip(zip_path)
    if not ok:
        return STATUS_FAILED, err or "更新包校验失败"
    # 3. 生成 update.bat
    try:
        bat = write_update_bat(stage, tag)
    except Exception as e:
        return STATUS_FAILED, f"生成更新脚本失败: {e}"
    # 4. 启动 update.bat（独立于本进程运行）并退出
    try:
        subprocess.Popen(
            ["cmd.exe", "/c", bat],
            cwd=str(_project_root()),
            creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
    except Exception as e:
        return STATUS_FAILED, f"启动更新脚本失败: {e}"
    return STATUS_OK, "更新已开始，程序将自动重启"