# -*- coding: utf-8 -*-
"""自更新执行器（内部模块）：独立于 autounpacker 包运行，负责「覆盖 → 自证 → 提交或回滚」。

职责：
- 以独立进程运行，等待旧程序退出后，把解压好的新版代码覆盖到安装目录（跳过用户数据）；
- 启动新版本，并通过 handshake.json 里的 token 判定「新版是否真正启动成功」；
- 成功则提交（把旧代码快照提升为 backup\\previous）；失败则用快照回滚并重启旧版本。
关键入口：本文件以 CLI 运行 —— `python runner_<token8>.py --state <pending.json>`；
  纯函数（decide_update_outcome / plan_rotation / diff_stale）可被离线测试直接导入。
依赖：仅标准库 + ctypes（Win32）。**绝不 import autounpacker 包**——新代码树损坏时
  仍必须能回滚，所以本文件必须完全自包含。
注意（allow: SIZE_OK）：按设计必须「单文件自包含」，全程不得拆包；回滚是不可失败的
  最后一道防线，宁可把流程写全，也不能为了行数把它拆到别处。
注意：只触碰 backup\\previous、backup\\rollback_*、backup\\.update 三类目录；绝不
  批量删除 backup 下用户保留的按日期归档；绝不覆盖数据文件/目录。
"""
import ctypes
import json
import os
import shutil
import subprocess
import sys
import time
from ctypes import wintypes
from pathlib import Path

# 数据白名单：与 updater.DATA_FILE_NAMES / DATA_DIR_NAMES 保持一致。
# 执行器不能 import 包，故在此独立声明（这是自包含的必然代价）。
DATA_FILE_NAMES = frozenset({
    "config.json", "toolbox.db", "temp_passwords.json", "deletion_trail.json",
    "crash.log", "libiconv.dll", "libzbar-64.dll",
})
DATA_DIR_NAMES = frozenset({"logs", "backup"})

# Win32 进程访问权限与退出码
_PROCESS_QUERY_LIMITED_INFORMATION = 0x1000
_PROCESS_TERMINATE = 0x0001
_STILL_ACTIVE = 259

# 子进程创建标志（DETACHED_PROCESS + 新进程组 + 不弹控制台）
_DETACHED_PROCESS = getattr(subprocess, "DETACHED_PROCESS", 0x00000008)
_CREATE_NEW_PROCESS_GROUP = getattr(subprocess, "CREATE_NEW_PROCESS_GROUP", 0x00000200)
_CREATE_NO_WINDOW = getattr(subprocess, "CREATE_NO_WINDOW", 0x08000000)

# 覆盖/快照前预留的磁盘余量（64MB），避免拷到一半磁盘写满把两边都写坏
_SPACE_MARGIN = 64 * 1024 * 1024

_kernel32 = ctypes.windll.kernel32
_user32 = ctypes.windll.user32


# ==================== 纯函数（离线可测，无副作用） ====================

def decide_update_outcome(saw_token, new_alive, elapsed, timeout):
    """根据握手轮询状态判定本次更新结局。

    返回 "commit" / "rollback" / "wait"：
    - 看到本 token 的握手 -> 提交（新版已成功跑起事件循环）；
    - 未看到且新进程已死 -> 立即回滚；
    - 未看到且已超时 -> 回滚；
    - 其余 -> 继续等待。
    """
    if saw_token:
        return "commit"
    if not new_alive:
        return "rollback"
    if elapsed >= timeout:
        return "rollback"
    return "wait"


def plan_rotation(previous_exists, outcome):
    """规划备份目录轮换动作（纯决策，不落盘）。

    提交 -> 把回滚快照提升为 previous，并删掉旧的 previous；
    回滚成功 -> 保留回滚快照（作为基线）；若此前没有 previous，也把它提升为 previous。
    每项为 (action, target) 二元组。
    """
    if outcome == "commit":
        return [("promote", "rollback->previous"), ("delete", "previous_old")]
    actions = [("keep", "rollback")]
    if not previous_exists:
        actions.append(("promote", "rollback->previous"))
    return actions


def diff_stale(live_root, snapshot_root, data_files, data_dirs):
    """列出存在于 live_root、但 snapshot_root 中缺失的代码相对路径。

    用于覆盖/回滚后清理陈旧代码文件，避免旧代码残留遮蔽新版。数据文件/目录
    （首个路径段命中 data_dirs，或单段名命中 data_files）永不列入。返回
    正斜杠相对路径（跨平台稳定），已排序。
    """
    live = Path(live_root)
    snap = Path(snapshot_root)
    files = frozenset(data_files or ())
    dirs = frozenset(data_dirs or ())
    out = []
    if not live.is_dir():
        return out
    for src in live.rglob("*"):
        if src.is_dir():
            continue
        try:
            rel = src.relative_to(live)
        except ValueError:
            continue
        parts = rel.parts
        if not parts:
            continue
        if parts[0] in dirs:
            continue
        if len(parts) == 1 and parts[0] in files:
            continue
        if (snap / rel).exists():
            continue
        out.append("/".join(parts))
    return sorted(out)


# ==================== 日志与工具 ====================

def _log(log_path, msg):
    """把一行带时间戳的日志追加到 log_path；任何异常都吞掉（更新流程不能被日志拖垮）。"""
    try:
        p = Path(log_path)
        p.parent.mkdir(parents=True, exist_ok=True)
        with open(p, "a", encoding="utf-8") as f:
            f.write("[%s] %s\n" % (time.strftime("%Y-%m-%d %H:%M:%S"), msg))
    except Exception:
        pass


def _message_box(title, text):
    """最后手段的可见提示（无法安全重启时用）。绝不抛异常。"""
    try:
        _user32.MessageBoxW(0, str(text), str(title), 0x10)
    except Exception:
        pass


def _file_size(path):
    try:
        return Path(path).stat().st_size
    except OSError:
        return 0


def _dir_size(path):
    total = 0
    try:
        for p in Path(path).rglob("*"):
            if p.is_file():
                try:
                    total += p.stat().st_size
                except OSError:
                    pass
    except OSError:
        pass
    return total


def _is_code_path(rel, data_files, data_dirs):
    """相对路径是否属于「代码」（即需要快照/覆盖/清理，且非用户数据）。"""
    parts = rel.parts
    if not parts:
        return False
    if parts[0] in data_dirs:
        return False
    if len(parts) == 1 and parts[0] in data_files:
        return False
    return True


def _iter_code_files(root, data_files, data_dirs):
    """遍历 root 下所有代码文件的相对路径。"""
    root = Path(root)
    if not root.is_dir():
        return
    for src in root.rglob("*"):
        if not src.is_file():
            continue
        try:
            rel = src.relative_to(root)
        except ValueError:
            continue
        if _is_code_path(rel, data_files, data_dirs):
            yield rel


def _code_size(root, data_files, data_dirs):
    return sum(_file_size(Path(root) / rel)
               for rel in _iter_code_files(root, data_files, data_dirs))


def _free_space_ok(path, need):
    try:
        return shutil.disk_usage(str(path)).free >= need
    except OSError:
        return True   # 探测不了就不拦，交给后续步骤如实失败


def _mirror_tree(src_root, dst_root, data_files, data_dirs):
    """把 src_root 下的代码文件复制到 dst_root（覆盖同名），返回复制数。

    数据文件/目录跳过；单个文件复制失败计入失败（调用方据数量判定）。
    """
    src_root = Path(src_root)
    dst_root = Path(dst_root)
    copied = 0
    for rel in _iter_code_files(src_root, data_files, data_dirs):
        dest = dst_root / rel
        try:
            dest.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(src_root / rel, dest)
            copied += 1
        except OSError:
            continue
    return copied


def _delete_rels(root, rels):
    deleted = 0
    for rel in rels:
        try:
            target = Path(root) / rel
            if target.exists():
                target.unlink()
                deleted += 1
        except OSError:
            continue
    return deleted


# ==================== Win32 进程控制 ====================

def _pid_alive(pid):
    """目标 PID 是否仍存活（打开失败视为已退出）。"""
    try:
        handle = _kernel32.OpenProcess(
            _PROCESS_QUERY_LIMITED_INFORMATION, False, int(pid))
        if not handle:
            return False
        try:
            code = wintypes.DWORD()
            ok = _kernel32.GetExitCodeProcess(handle, ctypes.byref(code))
            return bool(ok) and int(code.value) == _STILL_ACTIVE
        finally:
            _kernel32.CloseHandle(handle)
    except Exception:
        return False


def _terminate_pid(pid):
    """只终止指定 PID（绝不按镜像名批量杀，避免误伤无关进程与本执行器）。"""
    try:
        handle = _kernel32.OpenProcess(_PROCESS_TERMINATE, False, int(pid))
        if not handle:
            return False
        try:
            return bool(_kernel32.TerminateProcess(handle, 1))
        finally:
            _kernel32.CloseHandle(handle)
    except Exception:
        return False


def _wait_pid_exit(pid, timeout):
    """等待 PID 退出；返回是否已退出（超时未退出返回 False）。"""
    deadline = time.time() + max(0.0, float(timeout))
    while True:
        if not _pid_alive(pid):
            return True
        if time.time() >= deadline:
            return False
        time.sleep(0.25)


def _launch_app(interpreter, root):
    """分离启动 root\\main.py --autostart，返回新进程 PID。"""
    args = [str(interpreter), str(Path(root) / "main.py"), "--autostart"]
    proc = subprocess.Popen(
        args,
        cwd=str(root),
        creationflags=(_DETACHED_PROCESS | _CREATE_NEW_PROCESS_GROUP |
                       _CREATE_NO_WINDOW),
        close_fds=True)
    return proc.pid


def _relaunch_old(interpreter, root, log_path):
    """重启更新前的（live 未被破坏的）旧版本。"""
    try:
        pid = _launch_app(interpreter, root)
        _log(log_path, "已重启旧版本: pid=%s" % pid)
        return pid
    except Exception as e:
        _log(log_path, "重启旧版本失败: %s" % e)
        return None


# ==================== 握手 ====================

def _read_handshake_token(path):
    try:
        data = json.loads(Path(path).read_text(encoding="utf-8"))
        token = str((data or {}).get("token") or "")
        return token or None
    except Exception:
        return None


# ==================== 主流程 ====================

def _load_state(state_path):
    try:
        data = json.loads(Path(state_path).read_text(encoding="utf-8"))
        return data if isinstance(data, dict) else None
    except Exception:
        return None


def _snapshot(live_root, rollback_dir, state_dir, token, data_files, data_dirs,
              log_path):
    """把 live 代码快照到 rollback_dir：先在临时目录构建、校验文件数、再原子改名。"""
    tmp = Path(state_dir) / ("snap_%s" % str(token)[:8])
    try:
        if tmp.exists():
            shutil.rmtree(tmp, ignore_errors=True)
        tmp.mkdir(parents=True, exist_ok=True)
        copied = _mirror_tree(live_root, tmp, data_files, data_dirs)
        expected = sum(1 for _ in _iter_code_files(live_root, data_files, data_dirs))
        if copied == 0 or copied != expected:
            _log(log_path, "快照文件数不符：复制=%d 期望=%d，放弃更新" % (copied, expected))
            shutil.rmtree(tmp, ignore_errors=True)
            return False
        if Path(rollback_dir).exists():
            shutil.rmtree(rollback_dir, ignore_errors=True)
        os.replace(str(tmp), str(rollback_dir))
        _log(log_path, "旧代码快照完成：%d 个文件 -> %s" % (copied, rollback_dir))
        return True
    except Exception as e:
        _log(log_path, "快照异常：%s" % e)
        try:
            shutil.rmtree(tmp, ignore_errors=True)
        except Exception:
            pass
        return False


def _restore(root, rollback_dir, data_files, data_dirs):
    """用快照恢复 root：覆盖 + 清理新版残留；返回是否恢复到「可启动」状态。"""
    snap = Path(rollback_dir)
    if not (snap / "autounpacker").is_dir():
        return False
    try:
        _mirror_tree(snap, root, data_files, data_dirs)
        stale = diff_stale(root, snap, data_files, data_dirs)
        _delete_rels(root, stale)
        return ((Path(root) / "main.py").exists()
                and (Path(root) / "autounpacker" / "app.py").exists())
    except Exception:
        return False


def _cleanup_pending(pending_path):
    try:
        Path(pending_path).unlink(missing_ok=True)
    except Exception:
        pass


def _commit(root, rollback_dir, previous_dir, stage, tag, log_path, pending_path):
    """提交：轮换备份 -> 记录版本 -> 清理 pending 与 stage。"""
    _log(log_path, "收到握手，提交更新")
    try:
        if Path(previous_dir).exists():
            shutil.rmtree(previous_dir, ignore_errors=True)
        os.replace(str(rollback_dir), str(previous_dir))
        version_txt = Path(previous_dir) / "version.txt"
        version_txt.write_text(
            "tag: %s\nversion: %s\ndate: %s\n" % (
                tag, str(tag or "").lstrip("vV"),
                time.strftime("%Y-%m-%d %H:%M:%S")),
            encoding="utf-8")
    except Exception as e:
        _log(log_path, "备份轮换失败（新版仍可运行）：%s" % e)
    _cleanup_pending(pending_path)
    try:
        if Path(stage).exists():
            shutil.rmtree(stage, ignore_errors=True)
    except Exception:
        pass
    _log(log_path, "更新完成")


def _do_rollback(root, rollback_dir, previous_dir, interpreter, new_pid,
                 data_files, data_dirs, log_path, pending_path):
    """回滚：结束新进程 -> 用快照恢复（最多重试一次）-> 重启旧版本。"""
    if new_pid:
        try:
            if _pid_alive(new_pid):
                _terminate_pid(new_pid)
        except Exception:
            pass
    for attempt in (1, 2):
        if _restore(root, rollback_dir, data_files, data_dirs):
            _log(log_path, "已回滚到更新前版本（第 %d 次尝试）" % attempt)
            _relaunch_old(interpreter, root, log_path)
            _cleanup_pending(pending_path)
            if not Path(previous_dir).exists():
                try:
                    os.replace(str(rollback_dir), str(previous_dir))
                    _log(log_path, "回滚快照提升为 previous")
                except Exception as e:
                    _log(log_path, "提升 previous 失败：%s" % e)
            return True
        _log(log_path, "回滚失败（第 %d 次尝试）" % attempt)
    # 两次都失败：两份代码都保留，绝不启动状态未知的代码，弹出确切备份路径
    _log(log_path, "回滚连续失败，保留快照与备份，等待人工恢复")
    _message_box(
        "AutoUnpacker 更新失败",
        "自动回滚连续失败，程序未重启（以免启动状态未知的代码）。\n\n"
        "更新前的备份完好保存在：\n%s\n\n"
        "本次回滚快照保存在：\n%s\n\n"
        "可复制上述任一份覆盖回安装目录后手动启动；"
        "详细日志见：\n%s" % (previous_dir, rollback_dir, log_path))
    return False


def run_update(state):
    """执行一次完整更新：等待旧进程 -> 快照 -> 覆盖 -> 启动新版 -> 提交/回滚。"""
    root = Path(state["root"])
    stage = Path(state["stage"])
    token = str(state["token"])
    handshake = Path(state["handshake"])
    log_path = Path(state["log"])
    rollback_dir = Path(state["rollback_dir"])
    previous_dir = Path(state["previous_dir"])
    state_dir = Path(state.get("state_dir") or log_path.parent)
    interpreter = str(state["interpreter"])
    app_pid = int(state.get("app_pid") or 0)
    timeout_start = float(state.get("timeout_start") or 45)
    settle = float(state.get("settle") or 2)
    tag = str(state.get("tag") or "")
    data_files = DATA_FILE_NAMES
    data_dirs = DATA_DIR_NAMES
    pending_path = state_dir / "pending.json"
    new_pid = None

    _log(log_path, "更新执行器启动：token=%s tag=%s" % (token, tag))

    # 2. 等旧程序退出；超时则按 PID 强制结束（绝不按镜像名批量杀）
    if app_pid:
        if not _wait_pid_exit(app_pid, 20):
            _log(log_path, "旧进程 %d 未在 20s 内退出，强制结束" % app_pid)
            _terminate_pid(app_pid)
            _wait_pid_exit(app_pid, 5)

    # 3. 让单实例事件被系统释放
    time.sleep(max(0.0, settle))

    # 4. 磁盘空间预检：不足则原样重启旧版本，绝不动 live
    stage_size = _dir_size(stage)
    live_size = _code_size(root, data_files, data_dirs)
    if not _free_space_ok(root, stage_size + live_size + _SPACE_MARGIN):
        _log(log_path, "磁盘空间不足")
        _relaunch_old(interpreter, root, log_path)
        _cleanup_pending(pending_path)
        return

    # 5. 快照旧代码（失败则放弃，重启旧版本）
    if not _snapshot(root, rollback_dir, state_dir, token,
                     data_files, data_dirs, log_path):
        _log(log_path, "旧代码快照失败，放弃更新，重启旧版本")
        _relaunch_old(interpreter, root, log_path)
        _cleanup_pending(pending_path)
        return

    # 6. 覆盖新代码并清理陈旧代码
    try:
        copied = _mirror_tree(stage, root, data_files, data_dirs)
        stale = diff_stale(root, stage, data_files, data_dirs)
        removed = _delete_rels(root, stale)
        _log(log_path, "覆盖新代码：复制 %d，清理陈旧 %d" % (copied, removed))
        if not ((root / "main.py").exists()
                and (root / "autounpacker" / "app.py").exists()):
            _log(log_path, "覆盖后关键文件缺失，立即回滚")
            _do_rollback(root, rollback_dir, previous_dir, interpreter, None,
                         data_files, data_dirs, log_path, pending_path)
            return
    except Exception as e:
        _log(log_path, "覆盖新代码失败：%s，立即回滚" % e)
        _do_rollback(root, rollback_dir, previous_dir, interpreter, None,
                     data_files, data_dirs, log_path, pending_path)
        return

    # 7. 清掉可能残留的握手标记（只认新版写下的）
    try:
        handshake.unlink(missing_ok=True)
    except Exception:
        pass

    # 8. 启动新版本
    try:
        new_pid = _launch_app(interpreter, root)
    except Exception as e:
        _log(log_path, "启动新版本失败：%s，立即回滚" % e)
        _do_rollback(root, rollback_dir, previous_dir, interpreter, None,
                     data_files, data_dirs, log_path, pending_path)
        return
    _log(log_path, "已启动新版本：pid=%s" % new_pid)

    # 9. 轮询握手，直到提交或回滚
    saw_token = False
    start = time.time()
    outcome = "rollback"
    while True:
        if _read_handshake_token(handshake) == token:
            saw_token = True
        elapsed = time.time() - start
        outcome = decide_update_outcome(
            saw_token, _pid_alive(new_pid), elapsed, timeout_start)
        if outcome != "wait":
            break
        time.sleep(0.5)

    if outcome == "commit":
        _commit(root, rollback_dir, previous_dir, stage, tag, log_path,
                pending_path)
    else:
        _log(log_path, "未收到有效握手（saw_token=%s），回滚" % saw_token)
        _do_rollback(root, rollback_dir, previous_dir, interpreter, new_pid,
                     data_files, data_dirs, log_path, pending_path)


def main(argv=None):
    """CLI 入口：`--state <pending.json>`。"""
    args = list(sys.argv[1:] if argv is None else argv)
    state_path = None
    for i, a in enumerate(args):
        if a == "--state" and i + 1 < len(args):
            state_path = args[i + 1]
            break
    if not state_path:
        return 2
    state = _load_state(state_path)
    if not state or not state.get("root") or not state.get("token"):
        return 2
    try:
        run_update(state)
    except Exception as e:
        _log(state.get("log") or "update.log", "更新执行器未捕获异常：%s" % e)
        try:
            _message_box("AutoUnpacker 更新异常",
                         "更新过程发生未预期错误：%s\n备份见：\n%s"
                         % (e, state.get("previous_dir") or state.get("rollback_dir")))
        except Exception:
            pass
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
