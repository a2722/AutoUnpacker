# -*- coding: utf-8 -*-
"""跨名分卷配对（链 + 强制开启）：找出被改名上传的同一压缩包的兄弟分卷链，并用 7-Zip 硬链接装配验证。

职责：- find_candidates()：同目录里找「编号紧邻、基础名不同、自身不像完整压缩包」的候选尾卷（单卷）
- structural_evidence()：结构证据（编号相邻/首卷有包头/续卷无包头/尺寸相符/同目录）+ 0..5 评分
- same_base_numbers()：同目录里与首卷同基础名（normcase）的卷号（链扫描时「越过同名段」用）
- find_chain()：从首卷起逐号收集「改名过的兄弟卷」成链（遇断号停），返回 (volumes, missing, evidence, reason)
- best_chain()：唯一强链才返回 info（每卷评分 >= 4、链非空），否则拒绝
              （no_candidates / ambiguous:<n> / weak_evidence / gap:<n> / error:…）
- best_pairing()：单卷时代的薄包装（基于 find_candidates），保留兼容
- assemble()：在目标目录里用**硬链接**按首卷命名拼出一致卷名的整套分卷（零拷贝、绝不动原文件）
- verify()：装配后跑 `7z l`，先试空口令再逐个候选口令，按 7-Zip 错误文本给出机器可读状态
关键入口：find_chain() / best_chain() / find_candidates() / structural_evidence() /
          assemble() / verify() / best_pairing()
依赖：extract（复用其分卷编号/包头魔数/zip EOCD/7z 定位与解码，不另写签名表）、subprocess、tempfile
注意：本模块纯逻辑（无 Qt/线程/网络），所有异常一律吞成状态/返回值，绝不向外抛；
      原始文件永远只读；硬链接失败或跨盘一律拒绝，**绝不退化成复制**；
      只有 verify() 返回 verified 才允许调用方据此行动（改名由调用方执行）；
      链发现本身零动作：改名/日志留痕由调用方（monitors）执行，且强制开启（无 auto 开关）。

装配命名的关键实测结论（2026-09-18，7-Zip 23.01）：
- 通用 `.001` 名字（如 A.001+A.002）会被 7-Zip 当成「普通 split 容器」，`7z l` 只校验分卷
  是否都在，缺卷/错尾卷照样返回 0，**无法决断**；
- 带格式后缀的卷名（如 A.7z.001/A.7z.002）才会让 7-Zip 按真实格式解析整条链：
  正确链 rc=0；错口令报 "Cannot open encrypted archive. Wrong password?"；
  缺卷/错尾卷报 "Cannot open the file as [7z] archive / Unexpected end of archive"。
因此首卷名不含格式后缀时，装配名会补上检测到的格式（仅 7z/zip；rar 等其他格式不可决断，
一律 inconclusive，不猜、不动手）。
"""
# allow: SIZE_OK — 单文件约束（同批只允许新增本模块）+ 规则与实测结论必须随代码留存
import os
import re
import subprocess
import tempfile
import time
from pathlib import Path

from . import extract as smart_extract

# 机器可读的验证状态
STATE_VERIFIED = "verified"        # 7z 打开了装配链：确定是同一个压缩包（且完整）
STATE_INCOMPLETE = "incomplete"    # 链打开但缺卷/被截断
STATE_NO_MATCH = "no_match"        # 装上了候选但 7z 判定不是有效的同一压缩包链
STATE_UNKNOWN_PW = "unknown_pw"    # 像真归档但手里没有能打开它的口令
STATE_INCONCLUSIVE = "inconclusive"  # 无法决断（无候选/工具错误/文件被占用/超时）

MIN_VOLUME_BYTES = 1024 * 1024     # 首卷最小 1MiB：过小的文件绝不参与配对
MAX_PASSWORD_ATTEMPTS = 64         # 口令尝试上限（防跑飞）
VERIFY_TOTAL_BUDGET = 60           # 全部尝试的总时间预算（秒）：提示绝不把监听线程卡住几分钟
TEMP_PREFIX = ".au_pair_"          # 验证用临时装配目录前缀（带点，尽量不打扰用户）
CHAIN_MAX_VOLUMES = 64             # 链长上限：超过即安全拒绝（绝不因病态目录跑飞）
PAIR_SCAN_MAX_ENTRIES = 5000       # 单目录项扫描上限：防病态大目录拖垮监听线程
MIN_CHAIN_SCORE = 4                # 链中每一卷的最低结构评分（满分 5）

CREATE_NO_WINDOW = getattr(subprocess, "CREATE_NO_WINDOW", 0)

# 头部魔数识别复用 extract._header_matches_format（不另立签名表）
_HEAD_FORMATS = ("7z", "zip", "rar", "gz", "bz2", "xz", "tar")
# 只有这两种格式的「格式后缀命名」能让 7-Zip 对整条链做决断式校验
_ASSEMBLY_FORMATS = ("7z", "zip")


def _head_format(path):
    """文件头部魔数命中的归档格式；无/失败返回 None（只读 8 字节，绝不扫描全文件）。"""
    try:
        if not Path(path).is_file():
            return None
        for fmt in _HEAD_FORMATS:
            if smart_extract._header_matches_format(path, fmt):
                return fmt
    except Exception:
        pass
    return None


def _number_of(name):
    try:
        return smart_extract._volume_number(name)
    except Exception:
        return None


def _base_of(name):
    try:
        return smart_extract._volume_base(name)
    except Exception:
        return None


def _drive_key(path):
    """盘符/UNC 根标识（小写）；取不到返回空串。不同盘绝不做硬链接装配。"""
    try:
        return os.path.splitdrive(os.path.abspath(str(path)))[0].lower()
    except Exception:
        return ""


def _same_volume(a, b):
    ka, kb = _drive_key(a), _drive_key(b)
    return bool(ka) and ka == kb


def _under(path, root):
    """path 是否在 root 之下（含 root 自身）。"""
    try:
        p = os.path.normcase(os.path.abspath(str(path)))
        r = os.path.normcase(os.path.abspath(str(root)))
        return p == r or p.startswith(r + os.sep)
    except Exception:
        return False


def _contiguous(nums):
    """卷号是否从最小号起连续无缺口。"""
    try:
        return list(nums) == list(range(nums[0], nums[0] + len(nums)))
    except Exception:
        return False


def _looks_like_complete_archive(path):
    """是否像「自包含完整压缩包」的开头：有归档头魔数，或尾部有 zip EOCD。"""
    try:
        return (_head_format(path) is not None
                or bool(smart_extract.has_zip_eocd(path)))
    except Exception:
        return True   # 判不了就按「像完整包」处理（保守：不参与配对）


def structural_evidence(first_fp, cand_fp):
    """候选尾卷的结构证据与 0..5 评分（每个 True 记 1 分）。

    键：number_adjacent / first_has_signature / cand_lacks_signature /
        size_fits_split / same_dir / score，另附 format / cand_has_eocd 便于日志。
    绝不抛异常（失败项按 False 计）。
    """
    ev = {
        "number_adjacent": False,
        "first_has_signature": False,
        "cand_lacks_signature": False,
        "size_fits_split": False,
        "same_dir": False,
        "score": 0,
        "format": None,
        "cand_has_eocd": False,
    }
    try:
        first, cand = Path(first_fp), Path(cand_fp)
        n1, n2 = _number_of(first.name), _number_of(cand.name)
        ev["number_adjacent"] = (n1 is not None and n2 is not None
                                 and n2 == n1 + 1)
        fmt = _head_format(first)
        ev["format"] = fmt
        ev["first_has_signature"] = fmt is not None
        eocd = bool(smart_extract.has_zip_eocd(cand))
        ev["cand_has_eocd"] = eocd
        ev["cand_lacks_signature"] = (_head_format(cand) is None and not eocd)
        s1 = s2 = 0
        try:
            if first.is_file():
                s1 = first.stat().st_size
        except OSError:
            s1 = 0
        try:
            if cand.is_file():
                s2 = cand.stat().st_size
        except OSError:
            s2 = 0
        ev["size_fits_split"] = bool(s2 > 0 and s1 >= MIN_VOLUME_BYTES and s1 >= s2)
        ev["same_dir"] = (os.path.normcase(os.path.abspath(str(first.parent)))
                          == os.path.normcase(os.path.abspath(str(cand.parent))))
    except Exception:
        pass
    ev["score"] = sum(1 for k in ("number_adjacent", "first_has_signature",
                                  "cand_lacks_signature", "size_fits_split",
                                  "same_dir") if ev[k])
    return ev


def _batch_lookup(path):
    """(uk, 批次显示名) 或 None：清单可用时的加分信号。

    走 baidu_task 门面暴露的 baidu_manifest.batch_of（只读内存跟踪表）；
    清单不可用/未开启/任何异常一律返回 None，绝不影响结构路径。"""
    try:
        from . import baidu_task as bt
        fn = getattr(getattr(bt, "baidu_manifest", None), "batch_of", None)
        if fn is None:
            return None
        rec = fn(str(path))
        if rec and len(rec) >= 2 and rec[0]:
            return (str(rec[0]), str(rec[1] or ""))
    except Exception:
        pass
    return None


def find_candidates(first_fp, watch_root=None, limit=8):
    """找可能的跨名兄弟尾卷，按证据强度排序。

    候选硬条件（缺一不可）：
    - 同一目录的普通文件，不是正在下载的临时文件；
    - 卷号 = 首卷卷号 + 1；
    - 基础名与首卷不同（同基础名是既有分卷管线的事，不在此列）；
    - 0 < size(cand) <= size(first)，且 size(first) >= 1MiB；
    - 候选自身不像完整压缩包（无归档头魔数、无 zip EOCD）。
    watch_root 给出时，首卷与候选都必须在其下（越界一律不返回）。
    返回 [{path, size, number, evidence, batch}]；batch 为 (uk, 批次名) 或 None。
    绝不抛异常。
    """
    out = []
    try:
        first = Path(first_fp)
        if not first.is_file():
            return out
        n1 = _number_of(first.name)
        if n1 is None:
            return out
        if first.stat().st_size < MIN_VOLUME_BYTES:
            return out
        if watch_root is not None and not _under(first, watch_root):
            return out
        try:
            entries = list(first.parent.iterdir())
        except OSError:
            return out
        for e in entries:
            try:
                if not e.is_file() or e.name == first.name:
                    continue
                if _number_of(e.name) != n1 + 1:
                    continue
                b1, b2 = _base_of(first.name), _base_of(e.name)
                if not b1 or not b2:
                    continue
                if os.path.normcase(b1) == os.path.normcase(b2):
                    continue   # 同基础名：交给既有 _volume_ready 处理
                if watch_root is not None and not _under(e, watch_root):
                    continue
                if smart_extract.is_incomplete_download(e):
                    continue
                size = e.stat().st_size
                if size <= 0 or size > first.stat().st_size:
                    continue
                if _looks_like_complete_archive(e):
                    continue   # 自带包头/EOCD：更像另一个完整包，不配对
                ev = structural_evidence(first, e)
                out.append({"path": e, "size": size, "number": _number_of(e.name),
                            "evidence": ev, "batch": _batch_lookup(e)})
            except Exception:
                continue
    except Exception:
        return out
    # 证据强度优先；同为 5 分时清单批次命中者优先（加分信号，非必需）
    out.sort(key=lambda c: (-c["evidence"]["score"], 0 if c.get("batch") else 1,
                            os.path.normcase(str(c["path"]))))
    try:
        limit = max(0, int(limit))
    except Exception:
        limit = 8
    return out[:limit]


def _scan_dir(d, cap=PAIR_SCAN_MAX_ENTRIES):
    """目录项列表（最多 cap 个）；打不开/异常一律返回已扫到的部分，绝不抛。"""
    out = []
    try:
        for e in d.iterdir():
            out.append(e)
            if len(out) >= cap:
                break
    except OSError:
        pass
    return out


def same_base_numbers(first_fp):
    """同目录里与首卷基础名相同（normcase）的卷号，升序列表；无法判断返回 []。

    链扫描用它判断某个号是否已被「同基础名的既有分卷」占用：是则越过该号
    （那是既有分卷管线的事，不是缺口，也不是改名兄弟卷）。绝不抛异常。"""
    out = []
    try:
        first = Path(first_fp)
        b1 = _base_of(first.name)
        if not b1:
            return out
        nums = set()
        for e in _scan_dir(first.parent):
            try:
                if not e.is_file() or e.name == first.name:
                    continue
                b = _base_of(e.name)
                if b and os.path.normcase(b) == os.path.normcase(b1):
                    n = _number_of(e.name)
                    if n is not None:
                        nums.add(n)
            except OSError:
                continue
        out = sorted(nums)
    except Exception:
        pass
    return out


def _chain_volume_ok(first, e, b1, watch_root):
    """候选卷是否满足链的硬条件；返回 (True, {path,size,number,evidence}) 或 (False, None)。

    硬条件：同目录普通文件、非下载中、0 < size <= size(首卷)、自身不像完整包、
    基础名（normcase）与首卷不同、卷号可识别；watch_root 给出时必须在其中。"""
    try:
        if not e.is_file():
            return False, None
        b2 = _base_of(e.name)
        if not b2 or os.path.normcase(b2) == os.path.normcase(b1):
            return False, None
        if watch_root is not None and not _under(e, watch_root):
            return False, None
        if smart_extract.is_incomplete_download(e):
            return False, None
        size = e.stat().st_size
        if size <= 0 or size > first.stat().st_size:
            return False, None
        if _looks_like_complete_archive(e):
            return False, None
        num = _number_of(e.name)
        if num is None:
            return False, None
        return True, {"path": e, "size": size, "number": num,
                      "evidence": structural_evidence(first, e)}
    except Exception:
        return False, None


def find_chain(first_fp, watch_root=None, max_volumes=64):
    """从首卷出发逐号收集「改名过的兄弟卷」，返回 (volumes, missing, evidence_list, reason)。

    语义（对应「作者故意把同一压缩包的各分卷改成互不相同随机名」的场景）：
    - 从 k = 首卷号 + 1 起逐号扫描；某号存在「同基础名」文件时直接越过
      （那是既有分卷管线的事，不是缺口，也不是改名兄弟卷）；
    - 逐号收集改名过的兄弟卷，每个必须满足 _chain_volume_ok 的硬条件、卷号唯一；
    - 同号出现 > 1 个候选 → 返回空链 + reason="ambiguous:<n>"（绝不猜）；
    - 遇到第一个断号即停：reason="gap:<n>"（链上一个改名卷都没收到时）或 ""（已在
      收集中的链尾）；missing = 该断号（链因命中上限而停时为 None）；
    - 链长上限 max_volumes；目录项扫描上限 PAIR_SCAN_MAX_ENTRIES；命中链长上限时
      reason="error:链长达到上限(…)"（安全拒绝）；任何内部异常 → reason="error:…"。
    volumes 为 [{path,size,number,evidence}...]（已按卷号升序，绝不抛异常）。
    """
    try:
        first = Path(first_fp)
        if not first.is_file():
            return [], None, [], "no_candidates"
        n1 = _number_of(first.name)
        b1 = _base_of(first.name)
        if n1 is None or not b1:
            return [], None, [], "no_candidates"
        try:
            if first.stat().st_size < MIN_VOLUME_BYTES:
                return [], None, [], "no_candidates"
        except OSError:
            return [], None, [], "no_candidates"
        if watch_root is not None and not _under(first, watch_root):
            return [], None, [], "no_candidates"
        try:
            cap = max(1, int(max_volumes))
        except Exception:
            cap = CHAIN_MAX_VOLUMES
        # 预分组：卷号 -> 文件列表（首卷自身、无法识别卷号者不参与）
        by_num = {}
        for e in _scan_dir(first.parent):
            try:
                if e.name == first.name or not e.is_file():
                    continue
                n = _number_of(e.name)
                if n is None:
                    continue
                by_num.setdefault(n, []).append(e)
            except OSError:
                continue
        volumes, evs = [], []
        k = n1 + 1
        skipped_same_base = False
        hit_cap = False
        while True:
            if len(volumes) >= cap:
                hit_cap = True
                break
            bucket = by_num.get(k, [])
            # 同基础名的号：既有管线负责，直接越过（不是缺口，也不改名兄弟）
            same = False
            for e in bucket:
                b = _base_of(e.name)
                if b and os.path.normcase(b) == os.path.normcase(b1):
                    same = True
                    break
            if same:
                skipped_same_base = True
                k += 1
                continue
            cands = []
            for e in bucket:
                ok, rec = _chain_volume_ok(first, e, b1, watch_root)
                if ok:
                    cands.append(rec)
            if len(cands) > 1:
                return [], k, [], f"ambiguous:{k}"
            if len(cands) == 1:
                volumes.append(cands[0])
                evs.append(cands[0]["evidence"])
                k += 1
                continue
            break   # 第一个断号
        if hit_cap:
            return volumes, None, evs, f"error:链长达到上限({cap} 卷)"
        reason = ""
        if not volumes and not skipped_same_base:
            reason = f"gap:{k}"
        return volumes, k, evs, reason
    except Exception as e:
        return [], None, [], f"error:{e}"


def best_chain(first_fp, watch_root=None):
    """唯一强链 → (info, "")；否则 (None, 原因)。

    info = {"volumes": [...], "evidence": [...], "missing": n|None, "score": 最小分}；
    要求链非空且每一卷结构评分 >= MIN_CHAIN_SCORE(4)。
    原因：no_candidates / ambiguous:<n> / weak_evidence / gap:<n> / error:…。"""
    try:
        volumes, missing, evs, reason = find_chain(first_fp, watch_root=watch_root)
        if reason:
            return None, reason
        if not volumes:
            return None, "no_candidates"
        score = min(int((v.get("evidence") or {}).get("score", 0)) for v in volumes)
        if score < MIN_CHAIN_SCORE:
            return None, "weak_evidence"
        return ({"volumes": volumes, "evidence": evs, "missing": missing,
                 "score": score}, "")
    except Exception as e:
        return None, f"error:{e}"


def best_pairing(first_fp, watch_root=None):
    """唯一强候选 → (cand, evidence)；否则 (None, 原因字符串)。

    原因：no_candidates（无候选）/ ambiguous（多于一个强候选，绝不猜）/
    weak_evidence（评分不足 4）/ error:...。
    多于一个候选时一律拒绝：模糊配对只提示、不动手。"""
    try:
        cands = find_candidates(first_fp, watch_root=watch_root)
        if not cands:
            return None, "no_candidates"
        if len(cands) > 1:
            return None, "ambiguous"
        cand = cands[0]
        ev = cand.get("evidence") or {}
        if ev.get("score", 0) < 4:
            return None, "weak_evidence"
        return cand, ev
    except Exception as e:
        return None, f"error: {e}"


def _volumes_plan(first, volumes):
    """(有序 (卷号, Path) 列表, None) 或 (None, 原因)：首卷在列、去重、按卷号排序。"""
    try:
        if not first.is_file():
            return None, "首卷不存在"
        plan = []
        seen = set()
        for v in [first] + [Path(x) for x in (volumes or [])]:
            key = os.path.normcase(os.path.abspath(str(v)))
            if key in seen:
                continue
            seen.add(key)
            num = _number_of(v.name)
            if num is None:
                return None, f"无法识别卷号: {v.name}"
            plan.append((num, v))
        plan.sort(key=lambda x: x[0])
        nums = [n for n, _ in plan]
        if len(nums) != len(set(nums)):
            return None, "卷号重复（可能有两套同名分卷）"
        return plan, None
    except Exception as e:
        return None, f"整理分卷列表出错: {e}"


def _assembly_names(first_name, plan, fmt):
    """装配文件名列表（与 plan 同序）；无法安全命名返回 None。

    仅 7z/zip：首卷名不含格式后缀时补上（A.001 → A.7z.001），否则 7-Zip 只当普通
    split 容器、`l` 永远返回 0，无法决断。rar 等其他格式不装配（inconclusive）。"""
    try:
        if fmt not in _ASSEMBLY_FORMATS:
            return None
        base = _base_of(first_name)
        if not base:
            return None
        if re.search(r"\.\d{3}$", first_name):
            if not base.lower().endswith("." + fmt):
                base = base + "." + fmt
            return [f"{base}.{num:03d}" for num, _ in plan]
        if re.search(r"\.[zZ]\d{2}$", first_name) and fmt == "zip":
            return [f"{base}.z{num:02d}" for num, _ in plan]
    except Exception:
        pass
    return None


def assemble(first_fp, volumes, dest_dir):
    """用硬链接在 dest_dir 拼出一致命名的整套分卷。

    返回 (True, 首个装配文件 Path, "") 或 (False, None, 原因)。原始文件只读；
    跨盘/硬链接失败/目标已存在一律拒绝（清理本次已建的链接），绝不复制。
    """
    created = []
    try:
        first = Path(first_fp)
        dest = Path(dest_dir)
        plan, reason = _volumes_plan(first, volumes)
        if plan is None:
            return False, None, reason
        if not _same_volume(first, dest):
            return False, None, f"目标目录与源文件不同盘（拒绝复制）: {dest}"
        for _num, v in plan:
            if not v.is_file():
                return False, None, f"分卷不存在: {v.name}"
            if not _same_volume(v, dest):
                return False, None, f"分卷与目标目录不同盘（拒绝复制）: {v.name}"
        names = _assembly_names(first.name, plan, _head_format(first))
        if names is None:
            return False, None, "无法生成一致装配名（仅支持 7z/zip 分卷）"
        try:
            dest.mkdir(parents=True, exist_ok=True)
        except OSError as e:
            return False, None, f"无法创建装配目录: {e}"
        for (_num, v), nm in zip(plan, names):
            target = dest / nm
            if target.exists():
                _cleanup_links(created)
                return False, None, f"装配目标已存在: {nm}"
            try:
                os.link(str(v), str(target))
            except OSError as e:
                _cleanup_links(created)
                return False, None, f"硬链接失败（绝不退化为复制）: {e}"
            created.append(target)
        return True, created[0], ""
    except Exception as e:
        _cleanup_links(created)
        return False, None, f"装配出错: {e}"


def _cleanup_links(paths):
    for p in paths or []:
        try:
            Path(p).unlink()
        except OSError:
            pass


def _find_7z():
    try:
        return smart_extract.find_sevenzip_path()
    except Exception:
        return None


def _is_password_error(text):
    low = (text or "").lower()
    return any(t in low for t in (
        "wrong password", "cannot open encrypted archive", "enter password",
        "password is incorrect", "密码错误", "口令错误"))


def _classify_failure(text, nums):
    """按 7z 错误文本区分 no_match / incomplete；判不了 → inconclusive。"""
    low = (text or "").lower()
    if "missing volume" in low:
        return STATE_INCOMPLETE, "7z 报告缺少分卷"
    end_markers = ("unexpected end of archive", "headers error",
                   "crc failed", "data error")
    if any(t in low for t in end_markers):
        if len(nums) <= 1 or not _contiguous(sorted(nums)):
            return STATE_INCOMPLETE, "分卷链不完整（缺卷/跳号/只有截断的前缀）"
        return STATE_NO_MATCH, "拼出的链不是同一个有效压缩包"
    if "cannot open" in low or "not supported archive" in low:
        return STATE_NO_MATCH, "7z 无法把它识别为有效归档"
    return STATE_INCONCLUSIVE, "7z 的失败原因无法判定"


def _run_7z_list(exe, first, password, timeout):
    """跑 `7z l -- <first>`；口令经 stdin（绝不出现在命令行）。返回 (rc, out, err, timed_out)。"""
    try:
        r = subprocess.run(
            [str(exe), "l", "--", str(first)],
            capture_output=True,
            input=smart_extract._pwd_stdin_bytes(password),
            timeout=max(1.0, float(timeout)),
            creationflags=CREATE_NO_WINDOW)
    except subprocess.TimeoutExpired:
        return -1, "", "", True
    except OSError as e:
        return -1, "", str(e), False
    return (r.returncode, smart_extract._decode_7z(r.stdout or b""),
            smart_extract._decode_7z(r.stderr or b""), False)


def verify(first_fp, volumes, dest_dir=None, passwords=(), timeout=25):
    """assemble + 逐个口令跑 `7z l` 验证，返回 (state, detail)。

    先试空口令（-p 语义：stdin 传空行），再按顺序试候选口令，次数上限 64、
    单次超时 timeout 秒、全部尝试总预算 VERIFY_TOTAL_BUDGET 秒。
    state 只可能是五个 STATE_* 之一；detail 为中文短说明（绝不含口令）。
    绝不抛异常；finally 里删掉自己建的临时装配目录（绝不留下任何链接残留）。
    """
    workdir = None
    made_temp = False
    names = None
    try:
        first = Path(first_fp)
        exe = _find_7z()
        if exe is None:
            return STATE_INCONCLUSIVE, "未找到 7-Zip，无法验证"
        plan, reason = _volumes_plan(first, volumes)
        if plan is None:
            return STATE_INCONCLUSIVE, f"分卷列表不可用: {reason}"
        names = _assembly_names(first.name, plan, _head_format(first))
        if names is None:
            return STATE_INCONCLUSIVE, "格式不支持决断验证（仅支持 7z/zip 分卷）"
        if dest_dir is None:
            try:
                workdir = Path(tempfile.mkdtemp(prefix=TEMP_PREFIX,
                                                dir=str(first.parent)))
                made_temp = True
            except OSError as e:
                return STATE_INCONCLUSIVE, f"无法创建临时装配目录: {e}"
        else:
            workdir = Path(dest_dir)
        ok, asm_first, why = assemble(first, volumes, workdir)
        if not ok or asm_first is None:
            return STATE_INCONCLUSIVE, f"装配失败: {why}"
        # 口令候选：空口令优先；去重；上限
        attempts = [""]
        for p in passwords or []:
            s = str(p)
            if s and s not in attempts:
                attempts.append(s)
        attempts = attempts[:MAX_PASSWORD_ATTEMPTS]
        nums = [n for n, _ in plan]
        deadline = time.monotonic() + VERIFY_TOTAL_BUDGET
        last_pw_error = False
        for idx, pw in enumerate(attempts):
            remain = deadline - time.monotonic()
            if remain <= 1.0:
                if last_pw_error:
                    return STATE_UNKNOWN_PW, f"口令尝试已到时间预算（已试 {idx} 个）"
                return STATE_INCONCLUSIVE, "验证超时（时间预算用尽）"
            rc, out, err, timed_out = _run_7z_list(exe, asm_first, pw,
                                                   min(float(timeout), remain))
            if timed_out:
                return STATE_INCONCLUSIVE, "7z 列表超时（文件可能被占用）"
            text = (out or "") + "\n" + (err or "")
            if rc == 0:
                return STATE_VERIFIED, f"7z 列表成功（第 {idx + 1} 次尝试）"
            if _is_password_error(text):
                last_pw_error = True
                continue
            state, detail = _classify_failure(text, nums)
            return state, detail
        if last_pw_error:
            return STATE_UNKNOWN_PW, f"{len(attempts)} 个口令都打不开（像真归档）"
        return STATE_NO_MATCH, "7z 未报告明确可用结果"
    except Exception as e:
        return STATE_INCONCLUSIVE, f"验证出错: {e}"
    finally:
        try:
            for nm in (names or []):
                try:
                    (Path(workdir) / nm).unlink()
                except OSError:
                    pass
            if made_temp and workdir is not None:
                try:
                    Path(workdir).rmdir()
                except OSError:
                    pass
        except Exception:
            pass


def sibling_target_name(first_fp, number):
    """候选卷改名后的目标文件名（保留首卷编号风格）；无法判断返回 None。

    例：A.001/#2 → A.002；A.7z.001/#2 → A.7z.002；X.z01/#2 → X.z02；
    X.r00/#2 → X.r01；X.part1.rar/#2 → X.part02.rar。"""
    try:
        name = Path(first_fp).name
        num = int(number)
        m = re.search(r"\.(\d{3})$", name)
        if m:
            return name[:m.start()] + f".{num:03d}"
        m = re.search(r"\.([zZ])(\d{2})$", name)
        if m:
            return name[:m.start()] + f".{m.group(1)}{num:02d}"
        m = re.search(r"\.([rR])(\d{2})$", name)
        if m:
            return name[:m.start()] + f".{m.group(1)}{max(0, num - 1):02d}"
        if smart_extract._part_info(name):
            pre = re.sub(r"\.part\d+(?:\([^)]*\))?\.[^.]+$", "", name, flags=re.I)
            ext = name.rsplit(".", 1)[-1]
            if pre and ext:
                return f"{pre}.part{num:02d}.{ext}"
    except Exception:
        pass
    return None


def sibling_numbers_missing(first_fp):
    """同目录同系列分卷中缺失的卷号（1..最大卷号之间的缺口），日志辅助用。"""
    out = []
    try:
        first = Path(first_fp)
        base = _base_of(first.name)
        if not base:
            return out
        nums = set()
        for e in first.parent.iterdir():
            try:
                if not e.is_file():
                    continue
                b = _base_of(e.name)
                if b and os.path.normcase(b) == os.path.normcase(base):
                    n = _number_of(e.name)
                    if n is not None:
                        nums.add(n)
            except OSError:
                continue
        if nums:
            out = [n for n in range(1, max(nums)) if n not in nums]
    except Exception:
        pass
    return out
