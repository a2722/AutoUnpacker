# -*- coding: utf-8 -*-
"""ExtractService 多层嵌套解压服务（阶段6c 自 extract.py 纯搬移；_extract_inner 原样未拆）。

职责：队列驱动逐层解压、RAR 分卷规范化 staging、按层生成密码候选、失败层留痕与
「干净整体成功」闸门、中间目录清理（失败路径走回收站）。
依赖：formats（探测/分卷判定）、engines（双引擎/错误归类）、post（提升/删除/staging）、
      ..passwords.resolution（密码候选与字典）。
"""
import shutil
import tempfile
import time
import zipfile
from collections import deque
from pathlib import Path

from ..passwords.resolution import get_dict_passwords, get_password_for_layer
from .engines import (PythonZipEngine, SevenZipEngine, is_archive_open_error,
                      is_zip_open_error, result_raw_error, run_silent)
from .formats import (_volume_number, analyze_file, detect_format_by_magic,
                      find_sevenzip_path, is_archive_file, is_do_not_extract,
                      is_fake_volume_name, is_first_volume, is_split_gap_error,
                      is_volume_file, is_volume_name, should_skip_volume)
from .post import (_recycle_paths, _stage_rar_volumes, is_clean_success,
                   remove_empty_dirs, strip_embedded_zip, unique_dest_path)


CONTENT_STOP_MIN_FILES = 30
CONTENT_STOP_MIN_SMALL = 10
CONTENT_STOP_MIN_BUCKETS = 3


def looks_like_complete_content(paths):
    """启发式：解出的内容里存在大量大小不一的零碎文件时，通常已到达真实内容层，
    继续剥壳反而会破坏成品（如 apk 等），此时判定解压已完成。"""
    files = [p for p in paths if p.is_file()]
    if len(files) < CONTENT_STOP_MIN_FILES:
        return False
    sizes = []
    for p in files:
        try:
            sizes.append(p.stat().st_size)
        except OSError:
            continue
    small = sum(1 for s in sizes if 0 < s < 1024 * 1024)
    if small < CONTENT_STOP_MIN_SMALL:
        return False
    buckets = len({s // (1024 * 1024) for s in sizes})
    return buckets >= CONTENT_STOP_MIN_BUCKETS


class ExtractService:
    def __init__(self, engine, options):
        self.engine = engine
        self.options = options
        self.logs = []
        self.layer_records = []
        self.temp_dirs = set()
        self.temp_root = Path(tempfile.gettempdir())
        # 失败路径标记：置位后 _cleanup 把中间目录移入回收站（绝不永久删除）；
        # 成功路径保持原有 rmtree 行为不变。
        self.recycle_cleanup = False

    def emit(self, msg):
        self.logs.append(msg)
        print(msg)

    def _make_temp(self, task_id, depth):
        path = self.temp_root / f"extract_{task_id}_{depth}"
        if path.exists():
            shutil.rmtree(path, ignore_errors=True)
        path.mkdir(parents=True, exist_ok=True)
        self.temp_dirs.add(path)
        return path

    def _cleanup(self):
        for p in self.temp_dirs:
            self._release_temp(p)
        self.temp_dirs.clear()
        for p in getattr(self, "stage_dirs", []):
            self._release_temp(p)
        self.stage_dirs.clear()

    def _release_temp(self, path):
        """清掉一个中间目录：成功路径维持 rmtree；失败路径移入回收站
        （用户规则：解压失败的中间文件必须可恢复，绝不永久删除）。"""
        if self.recycle_cleanup:
            _recycle_paths([path], permanent_fallback=False)
        else:
            shutil.rmtree(path, ignore_errors=True)

    def _primary_layer_ok(self):
        """第 1 层（最外层归档）是否成功解出：是则本次已有真实成果，失败回退时
        不该把成果一起端走（更深的嵌套层没解开只是附赠部分没拿到）。

        例外：安全拦截（zip bomb 预检等）**不算**「嵌套层没解开」——那是我们为安全
        主动拒绝继续解压，仍按旧口径整体回退，半成品不留在输出目录里。
        """
        recs = self.layer_records or []
        if any(r.get("safety_block") for r in recs):
            return False
        return bool(recs) and recs[0].get("layer") == 1 and bool(recs[0].get("success"))

    @staticmethod
    def _retry_unlink(path, max_attempts=5):
        """删除文件，短暂重试几次。刚解压出来的大文件可能被 7z 进程/
        杀软实时扫描瞬时占用，一次性 unlink 容易静默失败留下残留。"""
        path = Path(path)
        for attempt in range(max_attempts):
            try:
                path.unlink()
                return True
            except OSError:
                if attempt >= max_attempts - 1:
                    return False
                time.sleep(0.3)
        return False

    def extract(self, task):
        self.temp_root = self._temp_root_for(task.get("output_dir"))
        self.stage_dirs = []
        self.recycle_cleanup = False
        try:
            result = self._extract_inner(task)
        except BaseException:
            # 异常中断同样按失败处理：中间目录走回收站
            self.recycle_cleanup = True
            raise
        else:
            if not is_clean_success(result):
                self.recycle_cleanup = True
            return result
        finally:
            self._cleanup()

    @staticmethod
    def _temp_root_for(output_dir):
        """临时目录与输出目录同盘（同盘最终移动=瞬间改名，避免跨盘搬运与发热）；
        输出在系统盘时沿用系统临时目录，避免盘根建目录的权限问题"""
        try:
            anchor = Path(output_dir).anchor
            sys_anchor = Path(tempfile.gettempdir()).anchor
            if anchor and len(anchor) == 3 and anchor != sys_anchor:
                return Path(anchor) / "ExtractTemp"
        except Exception:
            pass
        return Path(tempfile.gettempdir())

    def _extract_inner(self, task):
        task_id = task["id"]
        output_dir = Path(task["output_dir"])
        user_passwords = task.get("passwords", [])
        original_size = Path(task["source_path"]).stat().st_size
        max_depth = self.options["max_depth"]

        source_name = Path(task["source_path"]).name
        # 分卷首卷（.001/.part1/.z01…）：首卷拼出的载荷打不开 ⇒ 缺兄弟分卷
        root_is_first_volume = (is_volume_name(source_name)
                                and is_first_volume(source_name))
        failed_layers = []        # 任何失败层都让整体结果不再是成功
        split_incomplete = False  # 分卷链不完整（缺兄弟分卷）
        depth_exceeded = False    # 达到最大深度限制：仍有归档未处理，整体不算成功
        depth_left = 0            # 触顶时仍未处理的排队归档数（含当前项）

        queue = deque([{"archive": Path(task["source_path"]),
                        "depth": 1}])

        while queue:
            item = queue.popleft()
            depth = item["depth"]
            if depth > max_depth:
                # 触顶：剩余排队归档（含当前项）一律不再处理。必须留痕，否则整体
                # 结果仍会被判「干净成功」→ 源文件被删除策略回收，未处理的深层
                # 归档随临时目录被永久删除（_cleanup 成功路径走 rmtree）。
                depth_exceeded = True
                depth_left = len(queue) + 1   # 当前项本身也是未处理的排队归档
                self.emit(f"[第{depth}层] 超过最大深度限制 {max_depth}，"
                          f"仍有 {depth_left} 个归档未处理，将保留源文件")
                break

            if self.options["mode"] == "direct" and depth == 1:
                extract_dir = output_dir
                is_direct = True
            else:
                extract_dir = self._make_temp(task_id, depth)
                is_direct = False

            info = analyze_file(item["archive"])
            self.emit(f"[第{depth}层] 开始解压: {item['archive'].name} (格式: {info['detected_format'] or '未知'})")

            # 嵌套层也可能是非标准命名分卷（如 feal.part01(1).rar 这类带括号
            # 批次标记、或 .part1.除rar 改后缀）：7-Zip 按字面名找兄弟卷会报
            # Missing volume。规范化成 .partN.rar（硬链接到临时子目录）再解压。
            extract_src = item["archive"]
            if info.get("detected_format") == "rar":
                staged_info = _stage_rar_volumes(extract_src)
                if staged_info:
                    extract_src, stage_dir = staged_info
                    self.stage_dirs.append(stage_dir)
                    self.emit(f"[第{depth}层] 分卷命名不规范，已规范化后交由引擎: {extract_src.name}")

            # 嵌套层密码优先沿用上一层成功密码（内外层常共用同一密码，
            # 机械按层序号取密码列表第 N 个容易取错，如外层用第 1 个、
            # 内层却是第 2 个）。再补上本层序号映射与其余候选。
            prev_pw = None
            for rec in reversed(self.layer_records):
                if rec.get("success") and rec.get("used_password"):
                    prev_pw = rec["used_password"]
                    break
            passwords = get_password_for_layer(
                depth, user_passwords,
                extracted=info["extracted_password"],
                default=self.options.get("default_password"),
                dict_passwords=get_dict_passwords() if self.options.get("use_dict") else [],
                prev_used=prev_pw,
            )

            layer_task = {
                "id": f"{task_id}-layer-{depth}",
                "source_path": extract_src,
                "output_dir": extract_dir,
                "passwords": passwords,
                "progress_cb": task.get("progress_cb"),
                "pauser": task.get("pauser"),
            }

            # 解压前安全检查（P1-14 选项 B+C）：先只读 7z 清单判断声明大小/
            # 膨胀比/条目数，命中硬阈值就按失败层留痕并停止，绝不把炸弹先落盘
            # 再事后报警（旧的 _check_size 是解压后才提醒，拦不住任何东西）。
            # 同一次清单再认「Missing volume」：分卷缺兄弟卷时提前判失败，省掉
            # 一次注定失败的大解压与数百行 7-Zip 噪声（见 _precheck）。
            bomb_reason, gap_reason = self._precheck(extract_src, depth)
            if bomb_reason:
                self.emit(f"[第{depth}层] [安全拦截] {bomb_reason}")
                failed_record = {
                    "layer": depth,
                    "archive_name": item["archive"].name,
                    "used_password": None,
                    "success": False,
                    "error": bomb_reason,
                    # 安全拦截 ≠ 「嵌套层没解开」：见 _primary_layer_ok 的例外说明
                    "safety_block": True,
                }
                self.layer_records.append(failed_record)
                failed_layers.append(failed_record)
                break

            if gap_reason:
                # 预检即发现缺兄弟分卷：不跑注定失败的引擎解压（省掉一次完整解压
                # 尝试与数百行 7-Zip 噪声），合成一个同文案的失败结果，交给下方统一
                # 的失败处理归类——嵌套层经 is_split_gap_error 留缺卷锚点待跨目录
                # 归拢；首层经 is_archive_open_error 判 split_incomplete 保留源文件
                # 等分卷补齐。语义与「真跑完引擎再失败」完全一致。
                self.emit(f"[第{depth}层] 预检发现分卷缺卷（{gap_reason[:80]}），"
                          f"跳过注定失败的解压")
                result = {
                    "success": False,
                    "used_password": None,
                    "encrypted": False,
                    "error": gap_reason,
                    "logs": [f"[预检] 7-Zip 只读清单报告缺兄弟分卷，已跳过解压："
                             f"{gap_reason}"],
                }
            else:
                result = self.engine.extract(layer_task, self.options, depth)
            if (not result["success"]
                    and info["detected_format"] == "zip"
                    and isinstance(self.engine, SevenZipEngine)
                    and is_zip_open_error(result_raw_error(result))):
                fallback_attempts = []
                if info.get("is_polyglot"):
                    stripped = strip_embedded_zip(item["archive"], self.temp_root)
                    if stripped:
                        self.emit(f"[第{depth}层] 7-Zip 打不开多段伪装 ZIP（可能是 ZIP64/大文件偏移），剥离伪装头后重试")
                        retry_task = dict(layer_task)
                        retry_task["source_path"] = stripped
                        retry_out = SevenZipEngine(self.engine.path).extract(
                            retry_task, self.options, depth)
                        fallback_attempts.append(retry_out)
                        # 剥离出的临时 zip 是中间产物：重试成功维持原样删除，
                        # 失败移入回收站（绝不永久删除）
                        if retry_out.get("success"):
                            try:
                                stripped.unlink(missing_ok=True)
                            except OSError:
                                pass
                        else:
                            _recycle_paths([stripped], permanent_fallback=False)
                if any(a["success"] for a in fallback_attempts):
                    result = next(a for a in fallback_attempts if a["success"])
                elif self._all_entries_extracted(extract_dir, item["archive"]):
                    # 7-Zip 已经把全部文件都解出来了，只是返回了非零警告码（常见于
                    # 目录条目的 Unavailable data）。此时若再用 Python 全量重解，会因
                    # 文件名解码不同把同一批文件写成另一份不同名字（GBK vs cp437），
                    # 且耗时翻倍。判定已完成，直接按成功处理。
                    self.emit(f"[第{depth}层] 7-Zip 已解出全部文件但返回警告码，按成功处理")
                    best = fallback_attempts[0] if fallback_attempts else result
                    result = {"success": True,
                              "used_password": best.get("used_password"),
                              "encrypted": best.get("encrypted", False),
                              "error": None,
                              "logs": list(best.get("logs") or [])
                                       + ["[警告] 7-Zip 返回非零退出码，但校验文件数量与大小后确认全部解出"]}
                else:
                    self.emit(f"[第{depth}层] 改用 Python zipfile 重试")
                    fallback_attempts.append(PythonZipEngine().extract(layer_task, self.options, depth))
                    winner = next((a for a in fallback_attempts if a["success"]), None)
                    if winner is not None:
                        result = winner
                    else:
                        errs = [a["error"] for a in fallback_attempts if a.get("error")]
                        for a in fallback_attempts:
                            result["logs"].extend(a["logs"])
                        result["error"] = "；".join(dict.fromkeys(errs)) or result["error"]
            for log in result["logs"]:
                self.emit(f"[第{depth}层] {log}")

            if not result["success"]:
                if depth == 1:
                    self.emit(f"[第{depth}层] 解压失败: {result['error']}")
                    # 首卷打不开/被截断 ⇒ 分卷链不完整（缺兄弟分卷），不是终态失败
                    split_incomplete = (root_is_first_volume
                                        and is_archive_open_error(result_raw_error(result)))
                    if split_incomplete:
                        self.emit(f"[第{depth}层] 分卷链不完整（缺少兄弟分卷，"
                                  f"首卷拼出的载荷不是有效归档），保留源文件等待到齐")
                    # 第 1 层失败同样入 layer_records（失败层必须留痕，供审计）
                    failed_record = {
                        "layer": depth,
                        "archive_name": item["archive"].name,
                        "used_password": None,
                        "success": False,
                        "error": result["error"],
                    }
                    self.layer_records.append(failed_record)
                    failed_layers.append(failed_record)
                    self.recycle_cleanup = True   # 失败：中间文件走回收站
                    self._cleanup()
                    err_text = result["error"]
                    if split_incomplete:
                        err_text = f"分卷链不完整（缺少兄弟分卷），{err_text}"
                    failure = {
                        "task_id": task_id, "success": False,
                        "incomplete": True,
                        "failed_layers": failed_layers,
                        "depth_reached": len(self.layer_records),
                        "extracted_files": [], "used_password": None,
                        "layer_records": self.layer_records,
                        "logs": self.logs, "error": err_text,
                    }
                    if split_incomplete:
                        failure["split_incomplete"] = True
                    return failure
                # 嵌套层失败：失败文件先保留（移到输出目录），但任何失败层都会
                # 让整体判失败（见循环末尾 failed_layers 汇总）——不提升、不删源、
                # 不报「完成」；前面层已解出的内容由失败回退统一移入回收站。
                # 例外：嵌套层是分卷且报缺卷/打不开（Unexpected end / Missing
                # volume / Cannot open）时，说明分卷未到齐或兄弟卷未归拢，
                # 不应当作"已完成"吞掉失败——整体标记为失败，让监听层 defer
                # 重试（等分卷到齐 / 修复归拢后再解），避免把半成品当成品。
                err_text = result_raw_error(result)
                # 末卷 base.zip / base.rar 不带编号，不在 is_volume_name 内；但 7-Zip
                # 报 "Missing volume" 说明归档自身元数据已声明是多卷（缺 .z01/.r00
                # 兄弟卷）。因此把 "Missing volume" 作为独立的判定依据。截断的独立
                # zip 只报 Unexpected end of archive，不会误入此分支。
                is_split_gap = is_split_gap_error(item["archive"].name, err_text)
                if is_split_gap:
                    self.emit(f"[第{depth}层] 嵌套分卷缺卷（{err_text[:80]}），整体判失败待重试")
                    self.layer_records.append({
                        "layer": depth,
                        "archive_name": item["archive"].name,
                        "used_password": None,
                        "success": False,
                        "error": result["error"],
                    })
                    # 保留缺兄弟卷的锚点（该分卷），供监听层跨目录归拢后重试：锚点
                    # 若已在输出目录（直接模式）就原地不动；若在临时目录则搬到输出
                    # 目录，避免 _cleanup / 失败回退把它删掉。
                    anchor = item["archive"]
                    try:
                        if (anchor.exists()
                                and anchor.parent != output_dir
                                and output_dir not in anchor.parents):
                            dest = output_dir / anchor.name
                            if dest.exists():
                                dest = output_dir / f"{anchor.stem}_failed{anchor.suffix}"
                            dest.parent.mkdir(parents=True, exist_ok=True)
                            shutil.move(str(anchor), str(dest))
                            anchor = dest
                    except OSError as e:
                        self.emit(f"[第{depth}层] 保留缺卷锚点失败: {e}")
                    return {
                        "task_id": task_id, "success": False,
                        "depth_reached": len(self.layer_records),
                        "extracted_files": [], "used_password": None,
                        "layer_records": self.layer_records,
                        "logs": self.logs, "error": result["error"],
                        "split_gap_archive": str(anchor),   # 缺卷锚点：供跨目录归拢
                        "keep_output_dir": True,            # 失败回退时保留输出目录
                    }
                self.emit(f"[第{depth}层] 嵌套解压失败: {result['error']}（已跳过，保留原文件）")
                try:
                    failed_archive = item["archive"]
                    # 已在输出目录里的就地保留；只有仍留在临时目录的才搬过去。
                    # 否则 dest 恰好就是它自己 → dest.exists() 为真 → 会被误改成
                    # 「_failed」后缀，破坏分卷系列名（后续无法按系列名归拢）。
                    if failed_archive.exists() and failed_archive.parent != output_dir:
                        dest = output_dir / failed_archive.name
                        if dest.exists():
                            dest = output_dir / f"{failed_archive.stem}_failed{failed_archive.suffix}"
                        dest.parent.mkdir(parents=True, exist_ok=True)
                        shutil.move(str(failed_archive), str(dest))
                        self.emit(f"[第{depth}层] 已保留失败文件: {dest.name}")
                except OSError as e:
                    self.emit(f"[第{depth}层] 保留失败文件出错: {e}")
                failed_record = {
                    "layer": depth,
                    "archive_name": item["archive"].name,
                    "used_password": None,
                    "success": False,
                    "error": result["error"],
                }
                self.layer_records.append(failed_record)
                failed_layers.append(failed_record)
                # 首卷拼出的直接载荷（第 2 层）打不开 ⇒ 分卷链被截断（缺兄弟
                # 分卷），不能当「嵌套失败可跳过」吞掉：整体判失败等分卷补齐。
                if (depth == 2 and root_is_first_volume
                        and is_archive_open_error(result_raw_error(result))):
                    split_incomplete = True
                    self.emit(f"[第{depth}层] 分卷链不完整（缺少兄弟分卷，"
                              f"首卷拼出的载荷不是有效归档），整体判失败待重试")
                continue

            used_password = result["used_password"]
            # 加密信号与 used_password 同源：老测试桩/老引擎结果没有该键时，退回
            # 「used_password 非空即视为用到密码」，保持既有语义不回归。
            encrypted = bool(result.get("encrypted", used_password is not None))
            self.emit(f"[第{depth}层] 使用密码: {used_password or '无密码'}")
            self.layer_records.append({
                "layer": depth,
                "archive_name": item["archive"].name,
                "used_password": used_password,
                "encrypted": encrypted,
                "success": True,
            })

            # 本层源压缩包已被解压消费，先删掉（含其分卷兄弟），再移动解出内容。
            # 必须提前：若解出内容里有与源压缩包同名的条目（如层层同名文件夹
            # 2026年07月），晚删会让 move 撞上「已存在的源文件」报 WinError 183。
            if depth > 1 and item["archive"].exists():
                if not self._retry_unlink(item["archive"]):
                    self.emit(f"[第{depth}层] 警告: 删除主卷失败: {item['archive'].name}")
            if depth > 1 and is_volume_name(item["archive"].name):
                # 仅当被解压的本身就是分卷（如 xxx.7z.001）时才清理其分卷兄弟
                #（.002 等）。普通压缩包（如 xx.mp4）执行这步会误删内部嵌套
                # 分卷的 .002（stem 前缀匹配过于宽松），导致下层解压缺卷失败。
                arch_name = item["archive"].name
                arch_stem = item["archive"].stem
                parent = item["archive"].parent
                if parent.exists():
                    for entry in parent.iterdir():
                        if (entry.is_file()
                                and is_volume_file(arch_name, entry.name, arch_stem)):
                            if self._retry_unlink(entry):
                                self.emit(f"[第{depth}层] 已删除分卷文件: {entry.name}")
                            else:
                                self.emit(f"[第{depth}层] 警告: 删除分卷失败: {entry.name}")

            self._check_size(extract_dir, original_size)

            extracted_files = [p for p in extract_dir.rglob("*") if p.is_file()]

            nested_files = self._detect_nested(extracted_files) if self.options["enable_nested"] else []
            if nested_files:
                if looks_like_complete_content(extracted_files):
                    self.emit(f"[第{depth}层] 解压内容已是成品内容（大量大小不一的零碎文件），判定解压完成，停止继续剥壳")
                    if not is_direct:
                        self.move_to_output(extract_dir, output_dir)
                    self.emit(f"[第{depth}层] 完成")
                else:
                    self.emit(f"[第{depth}层] 检测到嵌套压缩包，继续解压下一层")
                    # 先处理「非嵌套」文件（混淆文件、零散小文件）：
                    # 分卷兄弟（如 .001 的 .002）必须留在主卷旁跟主卷一起进下一层，
                    # 不能提前移走；普通非嵌套文件移到输出目录。
                    for f in extracted_files:
                        if f not in nested_files:
                            # 嵌套分卷（如 .001）的分卷兄弟（.002）要留在原地
                            # 跟主卷一起进下一层，不能提前移走，否则下层缺卷解压失败
                            if not is_direct and not any(
                                    is_volume_file(nf.name, f.name, nf.stem)
                                    for nf in nested_files):
                                self.move_file(f, extract_dir, output_dir)
                    # 把散落在子目录里的分卷兄弟收集到首卷旁边：
                    # 多段伪装解出的分卷可能被 7-Zip 按内部目录结构展开
                    #（如 1\xxx.7z.002、2\xxx.7z.003），首卷 .001 在根目录时
                    # 7-Zip 在 .001 同目录找不到兄弟卷会报 Unexpected end of
                    # archive。这里把同系列分卷全部归拢到首卷所在目录。
                    for nf in list(nested_files):
                        if (is_volume_name(nf.name)
                                and _volume_number(nf.name) == 1
                                and not is_fake_volume_name(nf)):
                            series_dir = nf.parent
                            for cand in extract_dir.rglob("*"):
                                if (cand.is_file()
                                        and cand != nf
                                        and is_volume_file(nf.name, cand.name, nf.stem)
                                        and cand.parent != series_dir):
                                    try:
                                        dest = series_dir / cand.name
                                        if dest.exists():
                                            dest = series_dir / f"{cand.stem}_sibling{cand.suffix}"
                                        shutil.move(str(cand), str(dest))
                                        self.emit(f"[第{depth}层] 已归拢分卷兄弟: {cand.name} -> {dest.name}")
                                    except OSError as e:
                                        self.emit(f"[第{depth}层] 归拢分卷兄弟失败: {cand.name}: {e}")
                    queued_any = False
                    for f in nested_files:
                        # 假分卷名的完整压缩包（改后缀迷惑）不是分卷，应继续剥壳；
                        # 真分卷（如 .z01 的兄弟 .z02）才跳过，留给首卷一起处理
                        if (should_skip_volume(f.name)
                                and not is_fake_volume_name(f)):
                            self.emit(f"[第{depth}层] 跳过分卷文件: {f.name}")
                            continue
                        queue.append({"archive": f, "depth": depth + 1})
                        queued_any = True
                    if not queued_any and not is_direct:
                        # 所有嵌套文件都是非首卷（缺首卷）等异常：不能留在临时目录
                        # 被清理丢弃，移入输出目录保留，避免数据丢失。
                        for f in nested_files:
                            self.move_file(f, extract_dir, output_dir)
                        self.emit(f"[第{depth}层] 嵌套分卷缺少首卷，已保留到输出目录（未丢弃）")
            else:
                if not is_direct:
                    self.move_to_output(extract_dir, output_dir)
                self.emit(f"[第{depth}层] 完成")

        if failed_layers or depth_exceeded:
            self.recycle_cleanup = True   # 失败：中间文件走回收站
        self._cleanup()
        # 正常解压路径也会清理空目录（如内层压缩包所在文件夹在内容被
        # 消费后变空），避免残留空文件夹。
        if output_dir.exists():
            remove_empty_dirs(output_dir)
        if failed_layers:
            # 有层失败就绝不是成功：不提升、不回收源文件、不报「完成」。
            first = failed_layers[0]
            err = f"第{first['layer']}层解压失败: {first['error']}"
            if len(failed_layers) > 1:
                err += f"；共 {len(failed_layers)} 层未解出"
            if split_incomplete:
                err = f"分卷链不完整（缺少兄弟分卷），{err}"
            self.emit(f"[结果] 解压未完成（{err}），已保留源文件")
            return {
                "task_id": task_id, "success": False,
                "incomplete": True,
                "failed_layers": failed_layers,
                "split_incomplete": bool(split_incomplete),
                "depth_reached": len(self.layer_records),
                "extracted_files": [], "used_password": None,
                "layer_records": self.layer_records,
                "logs": self.logs, "error": err,
                # 分卷链不完整是「待重试」而非「部分完成」：主层解出的只是截断
                # 载荷，回退语义必须保持（截断分卷整体走回收站，可恢复）。
                "keep_output_dir": self._primary_layer_ok() and not split_incomplete,
            }
        if depth_exceeded:
            # 达到最大深度限制：仍有归档未处理，绝不能报「干净成功」——否则源文件
            # 会被删除策略回收，未处理的深层归档也会随临时目录被永久删除。
            # 与失败层同一口径：不提升、不回收源文件、不报「完成」。
            err = f"超过最大深度限制 {max_depth}，仍有 {depth_left} 个归档未处理"
            self.emit(f"[结果] 解压未完成（{err}），已保留源文件")
            return {
                "task_id": task_id, "success": False,
                "incomplete": True,
                "depth_exceeded": True,
                "failed_layers": failed_layers,
                "depth_reached": len(self.layer_records),
                "extracted_files": [], "used_password": None,
                "layer_records": self.layer_records,
                "logs": self.logs, "error": err,
                "keep_output_dir": self._primary_layer_ok(),
            }
        # 产出清单 = 解压后输出目录里的全部文件（整目录口径，与原行为一致）。
        # 为什么不用「本次新增」增量：重复解压同一个包到同一输出目录时路径集合
        # 不变、增量为空，会把正常的重复解压误判成零产出（实测踩到，见
        # test_hit_count_gate C1）。零产出防线改由 is_clean_success 的
        # 「显式空清单即不算成功」承担（见 post.py）。
        final_files = []
        if output_dir.exists():
            try:
                final_files = [p for p in output_dir.rglob("*") if p.is_file()]
            except Exception:
                final_files = []
        _first = self.layer_records[0] if self.layer_records else {}
        return {
            "task_id": task_id, "success": True,
            "depth_reached": len(self.layer_records),
            "extracted_files": final_files,
            "used_password": _first.get("used_password"),
            # 顶层「是否真的用到密码」取自与 used_password 同一层（第 1 层），
            # 供 extract_one 据此只记录真实命中，杜绝未加密包误报。
            "encrypted": bool(_first.get("encrypted", False)) if self.layer_records else False,
            "layer_records": self.layer_records,
            "logs": self.logs, "error": None,
        }

    def _detect_nested(self, files):
        return [f for f in files
                if not is_do_not_extract(f.name)
                and (is_archive_file(f) or f.name.lower().endswith(".001") or detect_format_by_magic(f))]

    def move_file(self, src_file, src_dir, dst_dir):
        rel = src_file.relative_to(src_dir)
        dest = dst_dir / rel
        dest.parent.mkdir(parents=True, exist_ok=True)
        final = unique_dest_path(dest)
        if final != dest:
            self.emit(f"[输出] 目标同名已存在，另存为: {final.name}")
        shutil.move(str(src_file), str(final))

    def _check_size(self, dir_path, original_size):
        total = sum(p.stat().st_size for p in dir_path.rglob("*") if p.is_file())
        if original_size > 0:
            ratio = total / original_size
            if ratio > self.options["max_size_ratio"]:
                self.emit(f"[安全警告] 解压后大小膨胀 {ratio:.1f} 倍，可能存在 zip bomb")

    def _opt_number(self, key, default):
        """读数值型安全选项：缺失/None/非法值一律回退默认值，绝不抛异常。"""
        try:
            value = self.options.get(key, default)
            return default if value is None else float(value)
        except (TypeError, ValueError):
            return default

    @staticmethod
    def _parse_declared_listing(output):
        """解析 `7z l -slt` 输出：返回 (声明未压缩总字节, 文件条目数)。

        与 engines._listing_info 同一口径：Path = 重置目录标记，Folder = +
        的目录条目不计入；一条 Size 都解析不出（空归档/输出异常）时返回
        (None, None) = 未知，由调用方按未知放行。"""
        total = 0
        entries = 0
        is_dir = False
        has_size = False
        for line in (output or "").splitlines():
            s = line.strip()
            if s.startswith("Path = "):
                is_dir = False
            elif s.startswith("Folder = "):
                is_dir = (s[9:].strip() == "+")
            elif s.startswith("Size = ") and not is_dir:
                try:
                    size = int(s[7:].strip())
                except ValueError:
                    continue
                total += size
                entries += 1
                has_size = True
        if not has_size:
            return None, None
        return total, entries

    def _list_archive_raw(self, archive):
        """单次只读 `7z l -slt` 调用（不传密码、不解压）的原始结果。

        7-Zip 缺失或调用异常 → None。刻意保留非 0 退出码时的 stdout/stderr：
        分卷缺兄弟卷正是以「非 0 退出 + 输出里 Missing volume」体现的，不能像
        旧 _list_declared_size 那样在非 0 时把输出丢掉。"""
        sevenzip = find_sevenzip_path()
        if sevenzip is None:
            return None
        try:
            return run_silent([str(sevenzip), "l", "-slt", str(archive)])
        except Exception:
            return None

    @staticmethod
    def _listing_gap_evidence(archive, result):
        """从只读清单结果里提取「分卷缺兄弟卷」证据行（无则空串）。

        只认两个「结构上确实缺卷」的硬证据（本机 7z 26.03 实测）：
          - WinZip 跨卷（B7236.zip 缺 .z01）→ `Missing volume`；
          - 7-Zip -v 的 zip 分卷（set.zip.001..）缺任一卷 → `Unexpected end of
            archive`（缺首/中/末卷都报它、退出码 2）。
        末卷 base.zip 不带编号也照样命中（Missing volume 不看文件名）。

        刻意**不含**泛化的 "Cannot open the file as"：它对单个损坏/非归档文件、
        以及截断的 7z 分卷清单（.7z.001 报 `Cannot open the file as [7z] archive`）
        也会出现——那类情况恰恰要留给引擎真跑一次的既有兜底与原始输出留痕，提前
        短接会架空前者的日志与文案。正常归档（含不传口令时的加密分卷，实测清单
        rc=0）不含这些错误文本 → 不触发，绝不误伤需要口令的正常分卷。"""
        if result is None:
            return ""
        text = (str(getattr(result, "stdout", "") or "")
                + "\n" + str(getattr(result, "stderr", "") or ""))
        if not any(tok in text
                   for tok in ("Missing volume", "Unexpected end of archive")):
            return ""
        if not (is_volume_name(Path(archive).name) or "Missing volume" in text):
            return ""
        for line in text.splitlines():
            s = line.strip()
            if "Missing volume" in s or "Unexpected end of archive" in s:
                return s
        return "分卷缺卷"

    def _precheck(self, archive, depth):
        """解压前的单次只读清单预检（`7z l -slt`，不解压、不传密码）。

        返回 (bomb_reason, gap_reason)：
          - bomb_reason：命中 zip bomb 硬阈值的中文原因，None = 放行；
          - gap_reason：清单报 Missing volume（缺兄弟分卷）时的证据行，
            None = 未发现缺卷。

        两条铁律：
          1. bomb_guard_enabled=False → 一律 (None, None)：连 7z 路径都不查、
             清单都不列（沿用既有开关语义，见 test_p14 E）。
          2. 任何「未知」（7z 缺失 / 列不出 / 解析不出）都放行——密码或头部
             加密的归档本来就列不出清单，绝不能因为探测不到就拦住正常解压。"""
        if not self.options.get("bomb_guard_enabled", True):
            return None, None
        r = self._list_archive_raw(archive)
        if r is None:
            return None, None
        gap_reason = self._listing_gap_evidence(archive, r)
        if r.returncode != 0:
            return None, gap_reason
        declared, entries = self._parse_declared_listing(r.stdout)
        if declared is None:
            return None, gap_reason
        try:
            archive_size = Path(archive).stat().st_size
        except OSError:
            return None, gap_reason
        hard_size_gb = self._opt_number("bomb_hard_size_gb", 50.0)
        hard_ratio = self._opt_number("bomb_hard_ratio", 200.0)
        hard_min_gb = self._opt_number("bomb_hard_min_gb", 1.0)
        soft_ratio = self._opt_number("bomb_soft_ratio", 100.0)
        soft_entries = self._opt_number("bomb_soft_entries", 50000.0)
        gib = 1024 ** 3
        declared_gb = declared / gib
        if declared > hard_size_gb * gib:
            return (f"声明解压后大小 {declared_gb:.1f} GB 超过硬上限 "
                    f"{hard_size_gb:g} GB（疑似 zip bomb），已停止解压"), gap_reason
        ratio = declared / archive_size if archive_size > 0 else 0.0
        if ratio >= hard_ratio and declared_gb >= hard_min_gb:
            return (f"声明膨胀比 {ratio:.0f}:1（解压后 {declared_gb:.2f} GB / "
                    f"压缩包 {archive_size / 1048576:.1f} MB）达到硬阈值 "
                    f"{hard_ratio:g}:1（疑似 zip bomb），已停止解压"), gap_reason
        if ratio > soft_ratio:
            self.emit(f"[第{depth}层] [安全提示] 声明膨胀比 {ratio:.0f}:1 超过提示阈值 "
                      f"{soft_ratio:g}:1，继续解压，请留意磁盘空间")
        if entries > soft_entries:
            self.emit(f"[第{depth}层] [安全提示] 归档条目数 {entries} 超过提示阈值 "
                      f"{soft_entries:g}，继续解压，请留意耗时与磁盘空间")
        return None, gap_reason

    def _all_entries_extracted(self, extract_dir, archive):
        """7-Zip 返回非零退出码后，用 Python 核对输出目录是否已包含归档的全部文件。

        必须比较 (相对路径, 大小) 对而非仅大小多重集：只比大小的话，一个把
        a.bin(1024B) 解出、b.bin(1024B) 漏掉（或改名）的半成品会因大小多重集
        相同被误判为「全部解出」而报成功。路径按 as_posix().lower() 归一化
        （与 _dirs_conflict 的路径比较口径一致），兼容分隔符与 Windows 大小写。"""
        try:
            with zipfile.ZipFile(archive) as zf:
                expected = sorted(
                    (i.filename.replace("\\", "/").lower(), i.file_size)
                    for i in zf.infolist() if not i.is_dir())
        except Exception:
            return False
        if not expected:
            return False
        try:
            root = Path(extract_dir)
            actual = sorted(
                (p.relative_to(root).as_posix().lower(), p.stat().st_size)
                for p in root.rglob("*") if p.is_file())
        except OSError:
            return False
        return actual == expected

    def move_to_output(self, src_dir, dst_dir):
        dst_dir.mkdir(parents=True, exist_ok=True)
        for src in src_dir.rglob("*"):
            rel = src.relative_to(src_dir)
            dest = dst_dir / rel
            if src.is_dir():
                dest.mkdir(parents=True, exist_ok=True)
            elif src.is_file():
                dest.parent.mkdir(parents=True, exist_ok=True)
                final = unique_dest_path(dest)
                if final != dest:
                    self.emit(f"[输出] 目标同名已存在，另存为: {final.name}")
                shutil.move(str(src), str(final))
