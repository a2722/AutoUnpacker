# -*- coding: utf-8 -*-
"""版本检查与自动更新：查询 GitHub Releases、下载新版并交由独立执行器完成覆盖/回滚。

职责：- check_latest_version() 查询最新 tag；compare_versions() 语义化版本比较
- download_release_zip()/verify_release_zip() 下载并校验更新包（SHA256 校验和 + 防 zip slip）
- apply_update() 下载校验后写 pending.json 并启动独立执行器（_update_runner.py）：
  执行器负责备份旧代码、覆盖新版、以握手自证启动成功，进而提交或自动回滚
- write_update_handshake() 供新版本启动成功后写下握手，执行器据此判定提交
关键入口：check_latest_version() / apply_update() / compare_versions()
依赖：urllib.request、zipfile、GitHub API（a2722/AutoUnpacker，无认证 60 次/小时）
注意：绝不主动拉取（仅用户点击按钮触发）；config.json/toolbox.db/logs/backup 等数据文件绝不覆盖
"""
import hashlib
import json
import os
import shutil
import subprocess
import sys
import tempfile
import time
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

# 自更新执行器状态目录名（位于 backup 下；覆盖时被排除，故执行器自身可存活）
UPDATE_DIR_NAME = ".update"
# 新版启动后等待握手的最长时间（秒）：超时未收到即回滚
HANDSHAKE_TIMEOUT = 45
# 旧进程退出后、覆盖前的稳定等待（秒）：让单实例事件被系统释放
SETTLE_SECONDS = 2
# 等待旧进程自行退出的最长时间（秒）：超时按 PID 强制结束
APP_EXIT_TIMEOUT = 20

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


# 发布方在 Release 里附带的校验和资产名（大小写不敏感，任选其一）。
# 发布流程约定：把 sha256sum 输出保存为 SHA256SUMS 文本并作为 Release 资产上传。
SHA256_ASSET_NAMES = ("sha256sums", "sha256sums.txt", "sha256.txt")


def sha256_file(path):
    """流式计算文件的 SHA256（小写十六进制）。任何异常返回 ""。"""
    try:
        h = hashlib.sha256()
        with open(path, "rb") as f:
            while True:
                chunk = f.read(1024 * 1024)
                if not chunk:
                    break
                h.update(chunk)
        return h.hexdigest()
    except Exception:
        return ""


def _parse_sha256sums(text, tag):
    """从 SHA256SUMS 文本里取与 tag 对应的哈希。

    行格式：`<64位hex>␠␠<文件名>`（可带 `*` 二进制标记；`#` 开头为注释）。
    文件名匹配用「去掉前导 v 的 tag」（GitHub 源码包名为 `Repo-2.1.0.zip`，
    而 tag 是 `v2.1.0`）；匹配不到时，若全文只有一条也采用。都取不到返回 ""。
    """
    try:
        cands = []
        for raw in str(text or "").splitlines():
            line = raw.strip()
            if not line or line.startswith("#"):
                continue
            parts = line.split(None, 1)
            if len(parts) != 2:
                continue
            h, name = parts[0].strip().lower(), parts[1].strip().lstrip("*")
            if len(h) != 64 or any(c not in "0123456789abcdef" for c in h):
                continue
            cands.append((h, name))
        if not cands:
            return ""
        bare = str(tag or "").lstrip("vV")
        for h, name in cands:
            if bare and bare in name:
                return h
        return cands[0][0] if len(cands) == 1 else ""
    except Exception:
        return ""


def fetch_expected_sha256(tag, timeout=None):
    """取发布方在该 tag 的 Release 里公布的 SHA256（无则返回 ""）。

    缺资产 / 网络失败 / 解析不出都返回 ""——调用方据此按「无法校验」处理，
    **绝不因此中断更新**（否则从「尚未附带校验和的旧版本」就再也升不上来了）。
    """
    try:
        api = f"https://api.github.com/repos/{GITHUB_REPO}/releases/tags/{tag}"
        req = urllib.request.Request(api, headers={
            "User-Agent": "AutoUnpacker/" + _local_version(),
            "Accept": "application/vnd.github+json"})
        with urllib.request.urlopen(req, timeout=timeout or CHECK_TIMEOUT) as resp:
            data = json.loads(resp.read().decode("utf-8", "replace"))
        asset = None
        for a in (data.get("assets") or []):
            if str(a.get("name") or "").strip().lower() in SHA256_ASSET_NAMES:
                asset = a
                break
        if not asset:
            return ""
        url = str(asset.get("browser_download_url") or "")
        if not url:
            return ""
        req2 = urllib.request.Request(url, headers={
            "User-Agent": "AutoUnpacker/" + _local_version()})
        with urllib.request.urlopen(req2, timeout=timeout or CHECK_TIMEOUT) as resp:
            text = resp.read().decode("utf-8", "replace")
        return _parse_sha256sums(text, tag)
    except Exception:
        return ""


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


def verify_release_zip(zip_path, expected_sha256=""):
    """校验下载的 zip：SHA256（若发布方提供了）→ 可解压 → 包含 autounpacker 包。

    `expected_sha256` 非空时**先校验哈希**，不匹配直接判失败（挡下载损坏/被替换）。
    返回 (ok: bool, 解压出的顶层目录 或 None, 错误信息)。
    解压到系统临时目录；成功时调用方负责清理（更新 bat 会删），
    失败时这里清理。
    """
    if expected_sha256:
        got = sha256_file(zip_path)
        if got != str(expected_sha256).strip().lower():
            return False, None, (
                "更新包 SHA256 校验失败（下载可能被篡改或损坏）："
                f"期望 {str(expected_sha256)[:12]}…，实际 {got[:12] or '(读取失败)'}…")
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


def _interpreter():
    """挑选用于重启/执行更新的解释器：优先与当前解释器同目录的 pythonw.exe。

    顺序：sys.executable 同级 pythonw.exe（存在时）→ sys.executable →
    PYTHONW 环境变量 → PATH 上的 pythonw → sys.prefix\\pythonw.exe。
    绝不硬编码 C:\\Windows\\pyw.exe（该文件通常不存在）。
    """
    exe = getattr(sys, "executable", "") or ""
    if exe:
        sibling = Path(exe).with_name("pythonw.exe")
        if sibling.exists():
            return str(sibling)
        return exe
    env = os.environ.get("PYTHONW")
    if env:
        return env
    found = shutil.which("pythonw")
    if found:
        return found
    return str(Path(sys.prefix) / "pythonw.exe")


def update_state_dir():
    """自更新状态目录：<root>\\backup\\.update（承载 pending/handshake/runner/日志）。"""
    d = _project_root() / "backup" / UPDATE_DIR_NAME
    d.mkdir(parents=True, exist_ok=True)
    return d


def copy_runner(state_dir, token):
    """把自包含执行器复制到状态目录（覆盖时该目录被排除，执行器得以存活）。"""
    src = Path(__file__).resolve().with_name("_update_runner.py")
    dst = Path(state_dir) / ("runner_%s.py" % str(token)[:8])
    shutil.copy2(src, dst)
    return dst


def write_update_handshake():
    """新版启动成功后写 handshake.json（读取 pending.json；无 pending 则 no-op）。

    原子写：先写 .tmp 再 os.replace，避免执行器读到半截 JSON。任何异常都吞掉
    并返回 False —— 绝不因写握手失败而影响新版启动。返回是否写过。
    """
    try:
        state_dir = update_state_dir()
        pending = state_dir / "pending.json"
        if not pending.exists():
            return False
        data = json.loads(pending.read_text(encoding="utf-8"))
        token = str((data or {}).get("token") or "")
        if not token:
            return False
        handshake = state_dir / "handshake.json"
        payload = {
            "token": token,
            "tag": str(data.get("tag") or ""),
            "version": _local_version(),
            "pid": os.getpid(),
            "ts": time.time(),
        }
        tmp = handshake.with_name(handshake.name + ".tmp")
        try:
            tmp.write_text(json.dumps(payload, ensure_ascii=False),
                           encoding="utf-8")
            os.replace(str(tmp), str(handshake))
        except Exception:
            try:
                tmp.unlink(missing_ok=True)
            except Exception:
                pass
            raise
        return True
    except Exception:
        return False


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


def _remove_pending(pending_path):
    """清理尚未被执行器消费的 pending.json（失败路径用；绝不抛异常）。"""
    try:
        if pending_path:
            Path(pending_path).unlink(missing_ok=True)
    except Exception:
        pass


def apply_update(tag, progress_cb=None):
    """执行自动更新：下载 → SHA256 校验 → 解压 → 写 pending → 启动独立执行器。

    本函数只负责「下载与准备」；真正的覆盖、自证、提交或回滚由
    backup\\.update\\runner_<token8>.py 以独立进程完成（本进程随后由 UI 退出）。
    任一步骤失败都返回失败且**绝不动 live 代码树**。
    返回 (status, message)：
      (STATUS_OK, "更新已开始，程序即将重启") —— 成功进入执行阶段
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
    # 1.5 取发布方公布的 SHA256（有就强制校验；没有则明确告知「跳过」，不阻断更新）
    want = fetch_expected_sha256(tag)
    if progress_cb:
        progress_cb("已取得发布方校验和，正在校验更新包…" if want
                    else "该版本未提供 SHA256SUMS，跳过完整性校验…")
    # 2. 校验（SHA256 若有）+ 解压 + 关键文件
    ok, stage, err = verify_release_zip(zip_path, expected_sha256=want)
    if not ok:
        return STATUS_FAILED, err or "更新包校验失败"
    # 3. 组装状态（单份 JSON 交给独立执行器；失败即中止且不触碰 live）
    root = _project_root()
    pending_path = None
    try:
        state_dir = update_state_dir()
        token = uuid.uuid4().hex
        t8 = token[:8]
        handshake = state_dir / "handshake.json"
        log_path = state_dir / f"update_{t8}.log"
        try:
            top_files = sorted(p.name for p in Path(stage).iterdir())
        except Exception:
            top_files = []
        state = {
            "root": str(root),
            "package": "autounpacker",
            "stage": str(stage),
            "tag": str(tag),
            "token": token,
            "app_pid": os.getpid(),
            "interpreter": _interpreter(),
            "state_dir": str(state_dir),
            "previous_dir": str(root / "backup" / "previous"),
            "rollback_dir": str(root / "backup" / f"rollback_{t8}"),
            "handshake": str(handshake),
            "log": str(log_path),
            "top_files": top_files,
            "timeout_start": HANDSHAKE_TIMEOUT,
            "settle": SETTLE_SECONDS,
        }
        pending_path = state_dir / "pending.json"
        pending_path.write_text(
            json.dumps(state, ensure_ascii=False, indent=2), encoding="utf-8")
        runner = copy_runner(state_dir, token)
    except Exception as e:
        _remove_pending(pending_path)
        return STATUS_FAILED, f"准备更新状态失败: {e}"
    # 4. 启动独立执行器（分离运行；本进程随后退出，由执行器接管重启）
    flags = (getattr(subprocess, "DETACHED_PROCESS", 0)
             | getattr(subprocess, "CREATE_NEW_PROCESS_GROUP", 0)
             | getattr(subprocess, "CREATE_NO_WINDOW", 0))
    try:
        subprocess.Popen(
            [state["interpreter"], str(runner), "--state", str(pending_path)],
            cwd=str(root), creationflags=flags, close_fds=True)
    except Exception as e:
        _remove_pending(pending_path)
        return STATUS_FAILED, f"启动更新执行器失败: {e}"
    return STATUS_OK, "更新已开始，程序即将重启"