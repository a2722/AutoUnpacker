# -*- coding: utf-8 -*-
"""百度分享链路的图形验证码（vcode）自动处理：取图 → 子进程 OCR → 易混判定。

职责：
- `fetch_vcode(op, referer)`：在**同一个 opener / CookieJar** 会话里取
  `api/getvcode` 与对应验证码图片字节（vcode_str 与会话绑定，换会话必失败）；
- `find_ocr_python()`：定位带 ddddocr 的 Python 3.11 解释器（结果模块级缓存）；
- `solve(img_bytes)`：把图片交给子进程 `_ocr_worker.py` 做 ddddocr 识别，
  返回 (text, confidence)；任何失败一律 None（上层据此降级手动输入）；
- `is_ambiguous(text, confidence)`：易混字符 / 低置信度判定（宁换一张不硬猜）；
- `describe(text, confidence)`：给人看的一句话描述。

设计约束（为什么长这样）：
- ddddocr 只装在 Python311（onnxruntime 体积大），而程序本体跑 Python310 /
  PyInstaller 打包 —— 所以 OCR 必须走**子进程**，本模块绝不 import ddddocr；
- 子进程带 CREATE_NO_WINDOW，避免 pythonw 下闪黑色控制台框；
- ddddocr 1.6.1 的 `probability=True` 返回的是 str(dict)（含几 MB 的
  probabilities 大数组），解析与裁剪都在子进程里完成，父进程只收
  `{"ok":true,"text":...,"confidence":...}` 一行 JSON，**绝不**把大数组
  带进日志；
- 铁律：本模块不向调用方抛异常（与 share.py 同口径），任何失败返回
  None / (None, None)。
依赖：仅标准库。**禁止 import PyQt5**（offline layer guard 会扫非 ui 目录）。
"""
import json
import logging
import os
import shutil
import subprocess
import tempfile
import urllib.request

_log = logging.getLogger(__name__)

# 自动识别最多换几张图（用户要求「先不要过多尝试」）：3 张都不行就转手动。
MAX_ATTEMPTS = 3

# 取验证码接口（与分享链路同站点、同会话）。
_GETVCODE_URL = "https://pan.baidu.com/api/getvcode?prod=pan"

# 子进程超时（秒）：onnxruntime 首次加载较慢，给足 25s。
_OCR_TIMEOUT_SEC = 25

# win32 下隐藏子进程控制台窗口（老 Python 无此常量时退化 0，与 share.py 同口径）。
_CREATE_NO_WINDOW = getattr(subprocess, "CREATE_NO_WINDOW", 0)

# ---------- 易混判定阈值（经验值，可调）----------
# 置信度硬地板：低于它无论字符集如何都判模糊（实测低于 0.90 基本必错）。
CONF_FLOOR = 0.90
# 含易混字符时的严格阈值：v/y、m/rn、8/b、0/o 这类形近对即使置信度不低
# 也经常互相误读，故含易混字符时要求更高的 0.97 才肯提交，否则换一张。
CONF_STRICT = 0.97
# 易混字符集合（小写比对）：覆盖 v/y、x、m/n/r/u、8/b、0/o、6/9、q/p/d、l/1/i。
AMBIGUOUS_CHARS = frozenset("vyxmnru8b0o69qpdl1i")

# find_ocr_python 的模块级缓存：None=未探测；""=探测过但没有可用解释器；
# 其它字符串=可用解释器路径（避免每次识别都重新找一遍）。
_ocr_python_cache = None


def fetch_vcode(op, referer):
    """取一组 (vcode_str, 验证码图片字节)；网络/解析异常返回 (None, None)，绝不抛。

    必须复用分享链路的同一个 opener（同一个 CookieJar）：vcode_str 与该会话
    绑定，换会话提交必失败。响应里的图片 URL 带 `\\/` 转义，需还原后再 GET。
    """
    from . import netpref
    try:
        # 与分享链路同一套浏览器特征：验证码接口也是一次 XHR，只带 Referer
        # 会显得格外脚本（抓包里浏览器必带 Origin / X-Requested-With / Accept）。
        rq = urllib.request.Request(_GETVCODE_URL, headers={
            "Referer": referer,
            "Origin": "https://pan.baidu.com",
            "Accept": "application/json, text/plain, */*",
            "Accept-Language": "zh-CN,zh;q=0.9,en;q=0.8",
            "X-Requested-With": "XMLHttpRequest"})
        with netpref.ipv4_only(netpref.prefer_ipv4()):
            with op.open(rq, timeout=15) as r:
                data = json.loads(r.read().decode("utf-8", "replace"))
        vcode = data.get("vcode")
        img_url = str(data.get("img") or "").replace("\\/", "/")
        if not vcode or not img_url:
            return None, None
        rq2 = urllib.request.Request(img_url, headers={
            "Referer": referer,
            "Accept": ("image/avif,image/webp,image/apng,image/svg+xml,"
                       "image/*,*/*;q=0.8"),
            "Accept-Language": "zh-CN,zh;q=0.9,en;q=0.8"})
        with netpref.ipv4_only(netpref.prefer_ipv4()):
            with op.open(rq2, timeout=15) as r:
                img = r.read()
        if not img:
            return None, None
        return str(vcode), img
    except Exception:
        return None, None


def find_ocr_python():
    """定位带 ddddocr 的 Python 3.11 解释器；找不到返回 None（结果模块级缓存）。

    查找顺序：a) 环境变量 AUTOUNPACKER_OCR_PYTHON；b) 常见安装路径
    %LOCALAPPDATA%\\Programs\\Python\\Python311\\python.exe；c) PATH 里的
    python3.11。只验证「文件存在」，不预跑解释器（真正的可用性由 solve()
    的子进程结果兜底，失败会优雅降级到手动输入）。
    """
    global _ocr_python_cache
    if _ocr_python_cache is not None:
        return _ocr_python_cache or None
    candidates = []
    try:
        env = (os.environ.get("AUTOUNPACKER_OCR_PYTHON") or "").strip()
        if env:
            candidates.append(env)
    except Exception:
        pass
    try:
        candidates.append(os.path.join(
            os.environ.get("LOCALAPPDATA") or "",
            "Programs", "Python", "Python311", "python.exe"))
    except Exception:
        pass
    try:
        w = shutil.which("python3.11")
        if w:
            candidates.append(w)
    except Exception:
        pass
    found = ""
    for c in candidates:
        try:
            if c and os.path.isfile(c):
                found = c
                break
        except Exception:
            continue
    _ocr_python_cache = found
    return found or None


def solve(img_bytes):
    """OCR 识别一张验证码图 → (text, confidence)；任何失败返回 None，绝不抛。

    图片先落临时文件，再子进程调 `_ocr_worker.py`（ddddocr 只装在 Python311，
    且 onnxruntime 不能进打包体积，故绝不在本进程 import）。只取子进程 stdout
    最后一行 JSON（前面可能有 onnxruntime 的杂讯行）。
    """
    if not img_bytes:
        return None
    py = find_ocr_python()
    if not py:
        return None
    tmp = None
    try:
        worker = os.path.join(os.path.dirname(os.path.abspath(__file__)),
                              "_ocr_worker.py")
        if not os.path.isfile(worker):
            return None
        with tempfile.NamedTemporaryFile(suffix=".png", delete=False) as f:
            f.write(img_bytes)
            tmp = f.name
        r = subprocess.run(
            [py, "-X", "utf8", worker, tmp],
            capture_output=True, timeout=_OCR_TIMEOUT_SEC,
            creationflags=_CREATE_NO_WINDOW)
        out = (r.stdout or b"").decode("utf-8", "replace").strip()
        if not out:
            return None
        line = out.splitlines()[-1].strip()
        data = json.loads(line)
        if not data.get("ok"):
            return None
        text = str(data.get("text") or "").strip()
        try:
            conf = float(data.get("confidence") or 0.0)
        except Exception:
            conf = 0.0
        if not text:
            return None
        return text, conf
    except Exception:
        return None
    finally:
        if tmp:
            try:
                os.unlink(tmp)
            except OSError:
                pass


def is_ambiguous(text, confidence):
    """是否「宁可换一张也不硬猜」：低置信度 / 含易混字符且置信度不够高。

    规则（阈值见模块级常量注释，经验值可调）：
    - confidence < CONF_FLOOR(0.90) → 模糊；
    - text 含易混字符（AMBIGUOUS_CHARS）且 confidence < CONF_STRICT(0.97) → 模糊；
    - 否则不模糊。置信度解析失败按模糊处理（宁缺毋滥）。
    """
    try:
        conf = float(confidence)
    except Exception:
        return True
    if conf < CONF_FLOOR:
        return True
    t = str(text or "").lower()
    if any(ch in AMBIGUOUS_CHARS for ch in t) and conf < CONF_STRICT:
        return True
    return False


def ambiguous_chars(text):
    """返回 text 中命中的易混字符（去重、保序），供通知文案展示「v/y」这类。"""
    seen = []
    for ch in str(text or "").lower():
        if ch in AMBIGUOUS_CHARS and ch not in seen:
            seen.append(ch)
    return "".join(seen)


def describe(text, confidence):
    """给人看的一句话描述，如 `vny8 (置信度 0.93)`。"""
    try:
        conf = float(confidence)
    except Exception:
        conf = 0.0
    return "%s (置信度 %.2f)" % (str(text or ""), conf)
