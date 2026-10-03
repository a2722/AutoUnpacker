# -*- coding: utf-8 -*-
"""ZIP 解压子进程：把 Python zipfile 解压隔离到独立进程执行。

职责：- 从 stdin(UTF-8 JSON) 读取 {archive, out, passwords, options}
      - 用 zipfile 逐个候选密码尝试解压（与引擎内联版语义一致）
      - 结果以一行 `RESULT:{json}` 输出到 stdout
关键入口：main()
依赖：标准库 zipfile（不 import 任何第三方原生库）
退出码：0=成功解压；3=不是有效 ZIP；4=所有密码都失败/需要密码；5=参数或其它异常

为什么需要它（治本）：Python zipfile 是纯 Python + C 层的紧密解密循环，
在**主进程线程**里跑会长时间持有 GIL，把 Qt 主线程饿死 → 整个界面冻结（真机案：
大 zip 回退 Python 引擎后，切任何页面都卡死，CPU 跑满一个核）。丢进独立子进程后，
GIL 与原生崩溃都被隔离，主进程事件循环照常。

安全：与 qr_worker 同款——只读入参、只写目标目录，绝不接触配置/数据库/日志。
"""
import json
import os
import sys
import zipfile
from pathlib import Path

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))


def _force_utf8_stdio():
    for stream in (sys.stdout, sys.stderr):
        if stream is None:
            continue
        try:
            stream.reconfigure(encoding="utf-8")
        except Exception:
            pass


def _open(archive):
    """与 engines.PythonZipEngine._open 完全同口径：未设 UTF-8 标志的条目按
    UTF-8 → GBK 依次尝试解码，避免同一文件被写成两份不同名字。"""
    if sys.version_info >= (3, 11):
        for enc in ("utf-8", "gbk"):
            try:
                return zipfile.ZipFile(archive, metadata_encoding=enc)
            except (UnicodeDecodeError, ValueError):
                continue
    return zipfile.ZipFile(archive)


def _password_bytes(pwd):
    """与引擎 _password_bytes 同口径：一个密码展开成 UTF-8/GBK/CP936 候选字节。"""
    if not pwd:
        return [None]
    out = [pwd.encode("utf-8")]
    for codec in ("gbk", "cp936"):
        try:
            b = pwd.encode(codec)
        except Exception:
            continue
        if b not in out:
            out.append(b)
    return out


def _test_password_any(zf, pwd_bytes_list):
    """与引擎 _test_password_any 同口径：能否用候选密码读出一条加密条目。"""
    entries = [i for i in zf.infolist() if not i.is_dir()]
    encrypted = [i for i in entries if i.flag_bits & 0x1]
    targets = encrypted or entries
    if not targets:
        return pwd_bytes_list[0] if pwd_bytes_list else None
    for info in targets:
        for pb in pwd_bytes_list:
            try:
                with zf.open(info, pwd=pb) as f:
                    f.read(1)
                return pb
            except (RuntimeError, KeyError, zipfile.BadZipFile):
                continue
        return None
    return None


def _emit(payload):
    sys.stdout.write("RESULT:" + json.dumps(payload, ensure_ascii=False) + "\n")
    sys.stdout.flush()


def main():
    _force_utf8_stdio()
    try:
        # 请求既可经 argv[1]（worker_command 传的临时请求文件路径）也可经 stdin
        # （UTF-8 JSON）传入。argv 路径优先——与 worker_command 的调用约定一致；
        # 无 argv 时读 stdin（便于独立测试/其它调用方）。
        if len(sys.argv) >= 2 and sys.argv[1]:
            raw = Path(sys.argv[1]).read_bytes()
        else:
            raw = sys.stdin.buffer.read()
        req = json.loads(raw.decode("utf-8"))
    except Exception as e:
        _emit({"success": False, "kind": "bad_args",
               "error": f"入参解析失败: {e}"})
        return 5
    try:
        archive = str(req["archive"])
        out = Path(req["out"])
        passwords = list(req.get("passwords") or [])
    except Exception as e:
        _emit({"success": False, "kind": "bad_args",
               "error": f"缺参数: {e}"})
        return 5

    try:
        out.mkdir(parents=True, exist_ok=True)
    except OSError as e:
        _emit({"success": False, "kind": "oserror", "error": f"创建输出目录失败: {e}"})
        return 5

    last_error = None
    for pwd in passwords:
        try:
            with _open(archive) as zf:
                encrypted = any(i.flag_bits & 0x1
                                for i in zf.infolist() if not i.is_dir())
                if pwd:
                    pb = _test_password_any(zf, _password_bytes(pwd))
                    if pb is None:
                        raise RuntimeError("密码错误")
                    zf.extractall(out, pwd=pb)
                else:
                    if encrypted:
                        raise RuntimeError("需要密码")
                    zf.extractall(out)
            _emit({"success": True,
                   "used_password": (pwd or None) if encrypted else None,
                   "encrypted": bool(encrypted),
                   "error": None})
            return 0
        except zipfile.BadZipFile as e:
            _emit({"success": False, "kind": "bad_zip",
                   "error": f"不是有效的 ZIP 文件: {e}"})
            return 3
        except (RuntimeError, OSError, ValueError) as e:
            last_error = str(e)
            continue
    _emit({"success": False, "kind": "password",
           "error": last_error or "解压失败"})
    return 4


if __name__ == "__main__":
    sys.exit(main())
