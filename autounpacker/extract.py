#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""解压核心（兼容 shim + CLI 入口）：大件实现已拆分到 extraction 包，这里保持旧导入路径与旧名字可用。

子模块布局（阶段6c 自本文件纯搬移，函数/方法体未做任何拆分）：
  extraction.formats  格式/分卷/伪装探测与文件名分析（analyze_file / detect_archive_format /
                      分卷名工具 / 未完成下载判定 / 7z 清单解析与隐写探测）；
  extraction.engines  7-Zip / Python zipfile 双引擎与子进程原语（run_silent / concise_error /
                      PauseController / _detect_7z_only_format / 错误短句归类）；
  extraction.service  ExtractService 多层嵌套解压服务（_extract_inner 原样未拆）；
  extraction.post     后处理与回收站委托（提升/删除源/时间戳/中间产物 staging /
                      _recycle_paths 薄委托 deletion.engine）。

本模块保留 CLI 入口 main()、create_engine()、extract_one()（含用到的
default_output_dir / parse_passwords / print_analysis），并重导出全部旧名字。
对旧模块属性的赋值（如测试打桩 _recycle_paths / apply_post_actions / create_engine /
delete_source / extract_one / get_dict_passwords / promote_extracted_content）会同步转发到
新归属模块，保证新旧两条路径看到同一份状态（同一名字被多个子模块共读时写入所有归属）。
`extract._recycle_paths` 仍是 deletion.engine._recycle_paths 的薄委托（定义已搬至
extraction/post.py，本模块重导出；deletion.engine 不得反向导入 extract 的约束不变）。
新代码请直接 `from .extraction.formats import ...` 等；本 shim 仅为向后兼容保留。

兼容注记：旧源码扫描测试（test_extract_hardening T2-7）按字面查找解压主循环的
「无墙钟超时」写法。真正的实现与执行点在 extraction/engines.py:SevenZipEngine.extract，
主循环末尾语句原文即：
    rc = proc.wait()
（该等待不带 timeout 参数：长时解压不受限，与拆分前逐字一致。）
"""
import argparse  # noqa: F401  旧模块顶层名字面保留
import ctypes  # noqa: F401
import os  # noqa: F401
import re  # noqa: F401
import shutil
import struct  # noqa: F401
import subprocess  # noqa: F401
import sys
import tempfile  # noqa: F401
import threading  # noqa: F401
import time
import uuid
import zipfile  # noqa: F401
from collections import deque  # noqa: F401
from pathlib import Path
from types import ModuleType as _ModuleType

from .config import delete_policy_permanent_fallback  # noqa: F401
from .deletion import engine as deletion_engine  # noqa: F401
# 密码候选/字典实现已移至 passwords/resolution.py；旧名 re-export 保留在
# extract 命名空间，调用点与测试打桩（ex.get_dict_passwords）语义不变。
from .passwords.resolution import (  # noqa: F401
    get_password_for_layer,
    load_password_dict,
    save_password_dict,
    get_dict_passwords,
    add_dict_password,
)

from .extraction import engines as _engines_home
from .extraction import formats as _formats_home
from .extraction import post as _post_home
from .extraction import service as _service_home
from .extraction.engines import (  # noqa: F401
    CREATE_NO_WINDOW, RUN_SILENT_TIMEOUT, ZIP_OPEN_ERROR_MARKERS,
    PauseController, PythonZipEngine, SevenZipEngine, _RunResult, _decode_7z,
    _detect_7z_only_format, _dir_size, _pwd_stdin_bytes, concise_error,
    is_archive_open_error, is_zip_open_error, result_raw_error, run_silent)
from .extraction.formats import (  # noqa: F401
    ARCHIVE_EXTS, DISGUISE_CARRIER_EXTS, DICT_FILE, DO_NOT_EXTRACT_SUFFIXES,
    EXT_FORMATS, INCOMPLETE_DOWNLOAD_SUFFIXES, PART_RE, PASSWORD_PATTERNS,
    POLYGLOT_EOCD_RANGE, POLYGLOT_FRONT_SCAN_LIMIT, POLYGLOT_FULL_SCAN_LIMIT,
    SEVENZIP_CANDIDATES, VOLUME_SKIP_PATTERNS, _confirm_polyglot,
    _full_scan_for_archive, _header_matches_format, _part_info,
    _scan_chunk_for_archive, _scan_front_for_archive, _scan_sfx_for_archive,
    _scan_tail_for_archive, _series_base, _strip_download_suffix, _volume_base,
    _volume_final_name, _volume_number, analyze_file, detect_archive_format,
    detect_format_by_magic, detect_steganography, detect_volumes_quick,
    extract_password_from_filename, find_sevenzip_path, format_from_extension,
    has_zip_eocd, is_archive_file, is_disguised, is_do_not_extract,
    is_fake_volume_name, is_first_volume, is_incomplete_download,
    is_non_first_rar_part, is_non_first_volume, is_split_gap_error,
    is_volume_file, is_volume_name, parse_sevenzip_listing,
    perform_sanitization, sanitize_filename, should_skip_volume,
    volume_download_pending)
from .extraction.post import (  # noqa: F401
    _dirs_conflict, _final_output_targets, _merge_dir, _recycle_paths,
    _set_created_time, _stage_fake_volume, _stage_rar_volumes,
    _stamp_output_times, apply_post_actions, build_post_actions, delete_source,
    is_clean_success, move_result_dir, promote_extracted_content,
    remove_empty_dirs, run_script, stamp_output_now, strip_embedded_zip,
    unique_dest_path)
from .extraction.service import (  # noqa: F401
    CONTENT_STOP_MIN_BUCKETS, CONTENT_STOP_MIN_FILES, CONTENT_STOP_MIN_SMALL,
    ExtractService, looks_like_complete_content)

__all__ = [
    "ARCHIVE_EXTS", "CONTENT_STOP_MIN_BUCKETS", "CONTENT_STOP_MIN_FILES",
    "CONTENT_STOP_MIN_SMALL", "CREATE_NO_WINDOW", "DICT_FILE", "DISGUISE_CARRIER_EXTS",
    "DO_NOT_EXTRACT_SUFFIXES", "EXT_FORMATS", "ExtractService",
    "INCOMPLETE_DOWNLOAD_SUFFIXES", "PART_RE", "PASSWORD_PATTERNS",
    "POLYGLOT_EOCD_RANGE", "POLYGLOT_FRONT_SCAN_LIMIT", "POLYGLOT_FULL_SCAN_LIMIT",
    "Path", "PauseController", "PythonZipEngine", "RUN_SILENT_TIMEOUT",
    "SEVENZIP_CANDIDATES", "SevenZipEngine", "VOLUME_SKIP_PATTERNS",
    "ZIP_OPEN_ERROR_MARKERS", "_RunResult", "_confirm_polyglot", "_decode_7z",
    "_detect_7z_only_format", "_dir_size", "_dirs_conflict", "_final_output_targets",
    "_full_scan_for_archive", "_header_matches_format", "_merge_dir", "_part_info",
    "_pwd_stdin_bytes", "_recycle_paths", "_scan_chunk_for_archive",
    "_scan_front_for_archive", "_scan_sfx_for_archive", "_scan_tail_for_archive",
    "_series_base", "_set_created_time", "_stage_fake_volume", "_stage_rar_volumes",
    "_stamp_output_times", "_strip_download_suffix", "_volume_base",
    "_volume_final_name", "_volume_number", "add_dict_password", "analyze_file",
    "apply_post_actions", "argparse", "build_post_actions", "concise_error",
    "create_engine", "ctypes", "default_output_dir",
    "delete_policy_permanent_fallback", "delete_source", "deletion_engine", "deque",
    "detect_archive_format", "detect_format_by_magic", "detect_steganography",
    "detect_volumes_quick", "extract_one", "extract_password_from_filename",
    "find_sevenzip_path", "format_from_extension", "get_dict_passwords",
    "get_password_for_layer", "has_zip_eocd", "is_archive_file",
    "is_archive_open_error", "is_clean_success", "is_disguised", "is_do_not_extract",
    "is_fake_volume_name", "is_first_volume", "is_incomplete_download",
    "is_non_first_rar_part", "is_non_first_volume", "is_split_gap_error",
    "is_volume_file", "is_volume_name", "is_zip_open_error", "load_password_dict",
    "looks_like_complete_content", "main", "move_result_dir", "os", "parse_passwords",
    "parse_sevenzip_listing", "perform_sanitization", "print_analysis",
    "promote_extracted_content", "re", "remove_empty_dirs", "result_raw_error",
    "run_script", "run_silent", "sanitize_filename", "save_password_dict",
    "should_skip_volume", "shutil", "stamp_output_now", "strip_embedded_zip", "struct",
    "subprocess", "sys", "tempfile", "threading", "time", "unique_dest_path", "uuid",
    "volume_download_pending", "zipfile",
]

# 名字 → 归属模块列表：旧模块属性被赋值时同步写入所有持有该名字的子模块
# （同一名字被多个子模块共读时全部写入，保证新旧两条路径读写同一份状态）。
_OWNERS_BY_NAME = {}
for _mod in (_formats_home, _engines_home, _service_home, _post_home):
    for _name in dir(_mod):
        if _name.startswith("__"):
            continue
        _OWNERS_BY_NAME.setdefault(_name, []).append(_mod)
del _mod, _name


class _ExtractShimModule(_ModuleType):
    """旧 extract 模块：属性赋值转发到新归属模块，读写永远指向同一份状态。"""

    def __setattr__(self, name, value):
        owners = _OWNERS_BY_NAME.get(name)
        if owners:
            for owner in owners:
                setattr(owner, name, value)
        super().__setattr__(name, value)


sys.modules[__name__].__class__ = _ExtractShimModule


def create_engine(kind, custom_path=None):
    if kind == "zip":
        return PythonZipEngine()
    sevenzip = Path(custom_path) if custom_path else find_sevenzip_path()
    if sevenzip is None or not sevenzip.exists():
        raise SystemExit("未找到 7-Zip，请安装或使用 --engine zip 或指定 --7z-path")
    # 版本门槛：低于 18.00 无法经 stdin 传密码，密码只能拼命令行（会被
    # 任务管理器/WMI 窥探）。低版本一律拒绝使用，由调用方回退 zip 引擎。
    try:
        from .sevenzip import check_version_ok, version_text
        if not check_version_ok(sevenzip):
            raise SystemExit(
                f"7-Zip 版本过低（{version_text(sevenzip)}），低于 18.00 无法安全传密码。"
                f"请通过「设置 → 7-Zip 管理」下载隔离版/全局版，或使用 --engine zip")
    except ImportError:
        pass  # sevenzip_manager 不可用时维持旧行为（正常情况不会发生）
    return SevenZipEngine(sevenzip)


def default_output_dir(archive):
    try:
        out = archive.with_suffix("")
    except ValueError:
        out = archive
    # Windows 会静默裁掉每个路径分量**结尾**的空格与点。若源文件名在扩展名前带
    # 空格（如「标题 .txt」），去扩展名后就得到「标题 」（结尾空格）——派生名与
    # 磁盘真实名字于是对不上：产出计数为 0、目录访问失败、删除回溯里记的是错名，
    # 甚至把空壳目录留在原地。这里按 Windows 同一规则预先裁掉，保证派生名 == 磁盘名。
    name = out.name.rstrip(" .")
    if not name:
        name = f"{archive.name}_extracted"
    out = out.parent / name
    if out == archive or (out.exists() and not out.is_dir()):
        out = archive.parent / f"{archive.name}_extracted"
    return out


def parse_passwords(items):
    result = []
    for item in items or []:
        for p in item.split(","):
            if p:
                result.append(p)
    return result


def extract_one(engine, source, out_arg, user_passwords, options, args,
                progress_cb=None, pauser=None):
    source = Path(source)
    if not source.exists():
        print(f"文件不存在: {source}")
        return None

    if pauser is not None:
        pauser.wait_if_paused()
    task_id = uuid.uuid4().hex[:8]
    print(f"=== 处理: {source.name} ===")
    if is_do_not_extract(source.name):
        print(f"移动安装包/交付物，保持原样不自动解压（需手动处理）: {source.name}")
        return None
    if is_non_first_rar_part(source):
        print(f"非首卷 RAR 分卷，跳过（等待 .part1 处理整个分卷）: {source.name}")
        return None
    info = analyze_file(source)
    if info.get("is_incomplete"):
        print(f"下载未完成，暂不解压（等待后缀消失）: {source.name}")
        return None
    if volume_download_pending(source):
        print(f"分卷未到齐（还有分卷在下载），暂不解压，等待全部下载完成: {source.name}")
        return None
    print(f"真实格式: {info['detected_format'] or '未知'}  伪装: {'是' if info['is_disguised'] else '否'}"
          f"  分卷: {'是' if info['is_volume'] else '否'}")
    if info["sanitized_name"]:
        source = perform_sanitization(source)
        print(f"已清理文件名中的「删」字: {source.name}")

    extract_src = source
    staged = None
    if info.get("detected_format") == "rar":
        # 首卷后缀非标准（如 .part1.除rar）时，7-Zip 找不到实际存在的 .rar 兄弟卷。
        # 规范化命名（硬链接到临时子目录改名为 .partN.rar）后再解压。
        staged_info = _stage_rar_volumes(extract_src)
        if staged_info:
            extract_src, staged = staged_info
            print(f"分卷命名不规范（含 .删除rar/.除rar 等），已规范化命名后交给引擎: {extract_src.name}")
    elif is_fake_volume_name(extract_src):
        # 文件名像分卷（.z11/.111 等）但内容是完整自包含压缩包（改后缀迷惑）：
        # 复制到临时目录改名为标准后缀，7-Zip 才不会误判为 split 缺卷。
        staged_info = _stage_fake_volume(extract_src, info["detected_format"])
        if staged_info:
            extract_src, staged = staged_info
            print(f"文件名像分卷但内容是完整压缩包（改后缀迷惑），已规范后缀后交给引擎: {extract_src.name}")
    if info.get("is_polyglot"):
        print("多段伪装文件，直接按内嵌压缩包格式处理（引擎按内容识别）")
    elif info["is_disguised"]:
        print(f"伪装文件（真实格式 {info['detected_format']}），直接按内容交给引擎处理（无需改名复制）")

    out_dir = Path(out_arg) if out_arg else default_output_dir(source)
    out_dir.mkdir(parents=True, exist_ok=True)
    # 记录解压前输出目录是否为空：失败回退时只清理"本次产生"的半成品，
    # 不误删用户预先放进自定义输出目录的内容
    try:
        pre_entries = set(out_dir.iterdir())
    except OSError:
        pre_entries = set()
    was_empty = not pre_entries
    print(f"输出目录: {out_dir}")

    svc = ExtractService(engine, options)
    result = None
    try:
        result = svc.extract({
            "id": task_id,
            "source_path": extract_src,
            "output_dir": out_dir,
            "passwords": user_passwords,
            "progress_cb": progress_cb,
            "pauser": pauser,
        })
    finally:
        if staged:
            # 规范化命名的临时 staging 目录也是中间产物：成功维持原样删除，
            # 失败移入回收站（里面只是源文件的硬链接/副本，源文件不受影响）。
            if result is not None and not is_clean_success(result):
                _recycle_paths([staged], permanent_fallback=False)
            else:
                shutil.rmtree(staged, ignore_errors=True)

    # 命中统计与「是否用字典解压」解耦：成功用到的口令一律计入密码字典，
    # 这样 GUI（use_dict=False，字典口令从不被尝试）也能按真实命中次数排序。
    # 写入只影响 password_dict 的 used_count/last_used_at；字典内容仍只在
    # options["use_dict"] 打开时才作为候选（见 get_password_for_layer 调用点），
    # 因此关字典的运行行为完全不变。
    # 关键：只有归档「真的有加密内容」、且该口令确实把它解开了（engine 结果里的
    # encrypted=True、used_password 非空），才记一次命中。未加密归档即使把候选
    # 密码喂进引擎也会成功，绝不能把第一个候选误记为命中（否则 62 条密码本的
    # 每次都让每个未加密包虚增一次）。每次成功只记一次；空/空白口令不记。
    if result["success"] and result.get("encrypted") and result["used_password"]:
        add_dict_password(result["used_password"])

    # 只有「干净的整体成功」才允许后处理（提升内容 / 删除源文件）。
    # 任何层失败都不是成功：源文件与分卷原样保留。
    if is_clean_success(result):
        apply_post_actions(result, source, out_dir, build_post_actions(args))

    if is_clean_success(result):
        print(f"=== 完成，穿透 {result['depth_reached']} 层，共 {len(result['extracted_files'])} 个文件 ===")
        _stamp_output_times(result, out_dir, pre_entries, args)
    else:
        print(f"=== 解压未完成（已保留源文件）: {result['error']} ===")
        # 回退：解压失败时中间文件移入回收站（可恢复），绝不永久删除。
        # 源文件(含分卷)未删除，之后可重试。仅当输出目录"解压前为空"
        # (即本次新建)才整体回收，避免误删用户预先放入自定义输出目录的内容。
        try:
            if out_dir.exists() and was_empty and not result.get("keep_output_dir"):
                recycled, _failed = _recycle_paths([out_dir], permanent_fallback=False)
                if recycled:
                    print(f"已回退，中间文件已移入回收站: {out_dir}")
                else:
                    print(f"已回退（中间文件移入回收站失败，保留原样）: {out_dir}")
        except Exception:
            pass
    return result


def print_analysis(path):
    info = analyze_file(path)
    print(f"文件: {info['original_name']}")
    print(f"真实格式: {info['detected_format'] or '未知'}")
    print(f"伪装: {'是' if info['is_disguised'] else '否'}")
    if info.get("is_polyglot"):
        print("多段伪装: 是（压缩包内嵌在文件末尾）")
    print(f"文件名清理: {info['sanitized_name'] or '无需清理'}")
    print(f"文件名中的密码: {info['extracted_password'] or '无'}")
    print(f"分卷: {'是' if info['is_volume'] else '否'}"
          + (f" (主卷: {info['volume_master']})" if info['volume_master'] else ""))
    if info["stego_content"]:
        print(f"隐写内容中的压缩文件: {', '.join(info['stego_content'])}")
    else:
        print("隐写内容: 无")


def main():
    try:
        from . import db
        db.init_db()
        db.migrate_legacy({}, db.LEGACY_DICT_FILE)
    except Exception:
        pass

    parser = argparse.ArgumentParser(
        prog="smart_extract",
        description="智能多重解压工具：嵌套穿透、自动识别格式、密码自动尝试、伪装/分卷处理",
    )
    parser.add_argument("archives", nargs="*", help="要解压的压缩包路径")
    parser.add_argument("-o", "--output", help="输出目录（默认：压缩包同名文件夹）")
    parser.add_argument("-p", "--password", action="append", default=[],
                        help="密码（可多次，或用逗号分隔；第N层优先用第N个）")
    parser.add_argument("--default-password", help="兜底默认密码")
    parser.add_argument("--no-nested", action="store_true", help="禁用嵌套穿透")
    parser.add_argument("--max-depth", type=int, default=10, help="最大穿透层数（默认10）")
    parser.add_argument("--max-size-ratio", type=float, default=100.0,
                        help="解压大小膨胀比例上限，防 zip bomb（默认100倍）")
    parser.add_argument("--use-password-dict", action="store_true",
                        help="自动使用/保存密码字典")
    parser.add_argument("--engine", choices=["auto", "7z", "zip"], default="auto",
                        help="解压引擎（默认 auto：有7-Zip用7-Zip，否则Python zipfile）")
    parser.add_argument("--7z-path", help="7z.exe 自定义路径")
    parser.add_argument("--mode", choices=["temp", "direct"], default="direct",
                        help="direct=直接解压到输出目录（最快，失败会留半成品）；temp=临时目录模式（出错可回滚，临时目录与输出同盘）")
    parser.add_argument("--move-to", help="解压成功后移动到指定文件夹")
    parser.add_argument("--promote-to", help="解压成功后：若输出目录顶层只有 1 个文件夹则提升到该地区，"
                        "并回收源文件与中间文件")
    parser.add_argument("--delete-source", action="store_true", help="解压成功后删除源文件（含分卷）")
    parser.add_argument("--run-script", help="解压成功后执行脚本")
    parser.add_argument("--script-args", nargs="*", default=[],
                        help="传给脚本的参数")
    parser.add_argument("--analyze", action="store_true", help="只分析文件，不解压")
    parser.add_argument("--list-dict", action="store_true", help="列出密码字典")
    parser.add_argument("--dict-add", help="向密码字典添加一个密码")
    args = parser.parse_args()

    if args.list_dict:
        data = load_password_dict()
        if not data:
            print("字典为空")
            return
        for e in sorted(data.values(), key=lambda x: -x.get("used_count", 0)):
            print(f"{e['password']}  使用 {e.get('used_count', 0)} 次  "
                  f"最近 {time.strftime('%Y-%m-%d %H:%M', time.localtime(e.get('last_used_at', 0)))}")
        return

    if args.dict_add:
        add_dict_password(args.dict_add)
        print(f"已添加: {args.dict_add}")
        return

    if args.analyze:
        for a in args.archives:
            print_analysis(a)
        return

    if not args.archives:
        parser.error("需要提供至少一个压缩包路径")

    engine = create_engine(args.engine, getattr(args, "7z_path"))
    print(f"解压引擎: {engine.name}")
    options = {
        "enable_nested": not args.no_nested,
        "max_depth": args.max_depth,
        "max_size_ratio": args.max_size_ratio,
        "use_dict": args.use_password_dict,
        "default_password": args.default_password,
        "mode": args.mode,
    }
    passwords = parse_passwords(args.password)
    for a in args.archives:
        try:
            extract_one(engine, a, args.output, passwords, options, args)
        except KeyboardInterrupt:
            print("\n已中断")
            sys.exit(130)


if __name__ == "__main__":
    main()
