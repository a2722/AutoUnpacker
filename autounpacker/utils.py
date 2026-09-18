# -*- coding: utf-8 -*-
"""通用工具：开机时间点、配置路径规范化、文件占用检测、崩溃日志、网址边界识别。

职责：- _boot_tick()/_boot_time() 识别「同一次系统启动」（GetTickCount64）
- _norm_path_for_cfg() 配置路径去重规范化
- _can_open_append() 检测文件是否被其他进程独占（下载器写入中）
- _install_crash_log() 把未捕获异常追加进 crash.log（不吞掉原有 excepthook）
- split_urls()/trim_url()/is_url_like()/is_baidu_pan_url() 全局唯一的网址边界
  （日志渲染、剪贴板/二维码取址、百度网盘判定都走这里，别处不要再立正则）
关键入口：_boot_time() / _boot_tick() / _can_open_append() / _install_crash_log() /
          split_urls() / trim_url() / is_url_like() / is_baidu_pan_url()
依赖：ctypes、sys、threading、paths、urllib.parse
注意：_can_open_append 绝不用 "ab" 模式打开（会凭空重建 0 字节幽灵文件）
注意：split_urls 用**正向字符集**（URL_CHARS）匹配，中文/全角括号/空白处必停；
      中文说明永远不该被吞进网址（用户反馈的「一次性复制链接吞掉后续说明」）
"""
import ctypes
import os
import re
import sys
import threading
import time
from urllib.parse import urlsplit

from . import paths



def _boot_tick():
    """系统启动以来的毫秒数（GetTickCount64）。同一次开机内单调递增，
    系统重启后归零重计，用于识别"是否同一次系统启动"。失败返回 0。

    必须显式声明 restype=c_ulonglong：GetTickCount64 返回 64 位值，ctypes
    默认按 c_int(32位有符号) 读取。系统连续开机超过 2^31 毫秒(~24.8天)后
    低 32 位为负，会被误读成负数 → 临时密码在重启后被误判为"过期"而丢失。"""
    try:
        k32 = ctypes.windll.kernel32
        k32.GetTickCount64.restype = ctypes.c_ulonglong
        return int(k32.GetTickCount64())
    except Exception:
        return 0


def _boot_time():
    """系统本次开机时间（Unix 秒）。取不到时返回 0。

    相比 _boot_tick（开机以来毫秒数），开机时间点是稳定的「开机身份」：
    系统重启后 tick 归零重计，前后两个 tick 无法可靠比较（新开机运行一小段
    时间就可能超过上一开机早期保存的小 tick），而开机时间点在重启前后必然
    不同，可直接用来判断「是否同一次开机」。"""
    try:
        ticks = ctypes.windll.kernel32.GetTickCount64()
        return time.time() - ticks / 1000.0
    except Exception:
        return 0.0

def _norm_path_for_cfg(path):
    """规范化路径（忽略大小写与尾部斜杠），用于配置文件去重"""
    try:
        p = os.path.normcase(os.path.abspath(path))
        while p.endswith(("\\", "/")) and len(p) > 3:
            p = p[:-1]
        return p
    except Exception:
        return str(path)


def _can_open_append(path):
    """文件能否以追加写模式打开（False = 被其他进程独占锁定）。

    下载器（百度网盘/IDM）多线程合并碎片时会独占写入目标文件，此时
    7-Zip 打不开（报"另一个程序正在使用此文件"），应 defer 等待。

    注意：必须用「不创建文件」的方式打开。曾用 open(path, "ab")，当文件
    已被消费/回收（poll 快照与处理之间被上游解压删掉）时，ab 模式会凭空
    重建一个 0 字节文件，导致源分卷被回收后原地留下 0 字节幽灵文件。"""
    if not os.path.exists(path):
        return True  # 文件已不存在（被消费/回收），无可锁定、更不能创建
    try:
        fd = os.open(str(path), os.O_WRONLY | os.O_APPEND)
        os.close(fd)
        return True
    except OSError:
        return False


def _install_crash_log():
    """把未捕获异常写入 APP_DIR/crash.log，便于排查闪退问题。

    只"追加记录"，不破坏原有异常行为：写完 crash.log 后仍会调用安装前的
    excepthook（如控制台下的默认回溯输出），避免把全局钩子当成自家后院、
    静默吞掉标准错误输出。
    """
    try:
        crash_file = paths.CRASH_LOG
        prev_main = sys.excepthook
        prev_thread = getattr(threading, "excepthook", None)

        def _main_hook(exc_type, exc_value, exc_tb):
            try:
                import traceback
                with open(crash_file, "a", encoding="utf-8") as f:
                    f.write(f"[{time.strftime('%Y-%m-%d %H:%M:%S')}] 主线程异常:\n")
                    traceback.print_exception(exc_type, exc_value, exc_tb, file=f)
                    f.write("\n")
            except Exception:
                pass
            # 保留安装前的行为（不吞掉标准回溯）
            try:
                prev_main(exc_type, exc_value, exc_tb)
            except Exception:
                pass

        def _thread_hook(args):
            try:
                import traceback
                with open(crash_file, "a", encoding="utf-8") as f:
                    f.write(f"[{time.strftime('%Y-%m-%d %H:%M:%S')}] 后台线程异常:\n")
                    traceback.print_exception(args.exc_type, args.exc_value,
                                              args.exc_traceback, file=f)
                    f.write("\n")
            except Exception:
                pass
            if prev_thread is not None:
                try:
                    prev_thread(args)
                except Exception:
                    pass

        sys.excepthook = _main_hook
        if prev_thread is not None:
            try:
                threading.excepthook = _thread_hook
            except AttributeError:
                pass
    except Exception:
        pass


# ---------------------------------------------------------------------------
# 网址边界识别（全局唯一权威）
#
# 旧代码曾有三套互不相同的规则（日志一套、剪贴板一套、内联正则一套），
# 结果「一次性复制的网址把后面的中文说明也吞进去」。这里统一为：
# 从 http(s):// 或 www. 起，只吃 URL_CHARS 里的 ASCII 字符，遇到第一个不在
# 集合内的字符（中文、全角括号、引号、空白…）立即停止，再剔除尾部标点。
# ---------------------------------------------------------------------------
# 正向字符集（RFC 3986 组成字符 + 括号，允许路径里的 (...)/[...]）。
URL_CHARS = r"A-Za-z0-9\-._~:/?#\[\]@!$&()*+,;=%"
# 尾部垃圾字符（中英文句读/引号/括号等，两套旧规则的并集 + ·…）。
URL_TRAIL_JUNK = ".,;:!?、。，；：！？）】》」』)]}>·…"

_URL_RE = re.compile(r"(?:https?://|www\.)[" + URL_CHARS + r"]+", re.I)
# 百度网盘主机（含子域）：pan（网页版分享/文件页）/ yun（旧域名）/ eyun（企业版）。
_BAIDU_PAN_HOSTS = ("pan.baidu.com", "yun.baidu.com", "eyun.baidu.com")


def _drop_unmatched_tail(text, opener, closer):
    """反复剥掉尾部「未配对的收尾符」（如 .../a) 里多出来的 ')'）。"""
    while text.endswith(closer) and text.count(opener) < text.count(closer):
        text = text[:-1]
    return text


def trim_url(url):
    """剔除网址尾部的标点/垃圾字符，返回干净网址（纯字符串处理，绝不抛异常）。

    规则（有界循环防异常输入死循环）：
    1. 平衡的 ASCII 括号对属于网址本身（如维基 `/wiki/Foo_(bar)`），保留；
       只有**未配对**的尾 `)` / `]` 才剥掉（如 `https://a.com/a)`）。
    2. 其余 URL_TRAIL_JUNK 字符（中英文句读/引号/全角括号等）从右侧反复剔除。
    3. 剔除后又露出的未配对收尾符回到规则 1 继续处理（如 `a)。`）。
    """
    s = str(url or "")
    for _ in range(8):
        before = s
        s = _drop_unmatched_tail(s, "(", ")")
        s = _drop_unmatched_tail(s, "[", "]")
        while s and s[-1] in URL_TRAIL_JUNK and s[-1] not in ")]":
            s = s[:-1]
        if s == before:
            break
    return s


def split_urls(text):
    """返回 [(start, end, url)]：text 里所有网址及其**真实边界**区间。

    匹配在第一个不属于 URL_CHARS 的字符处停止（中文/全角括号/引号/空白都不在
    集合内），再用 trim_url 剔除尾部标点。因此
    「[分享] 已记录: https://pan.baidu.com/s/1Abc（托盘菜单…）」只会截出裸链接，
    后面的中文说明绝不会被吞进网址。一行内多个网址全部收录；无网址返回 []。
    """
    if not text:
        return []
    spans = []
    for m in _URL_RE.finditer(text):
        url = trim_url(m.group(0))
        if not url:
            continue
        spans.append((m.start(), m.start() + len(url), url))
    return spans


def is_url_like(text):
    """整段（去首尾空白后）是否就是**一个**网址（尾部标点不算内容）。

    例：`https://a.com/x`、`https://a.com/x。` 为真；`看 https://a.com/x`、
    `https://a.com/x 提取码：abcd` 为假。
    """
    s = str(text or "").strip()
    if not s:
        return False
    spans = split_urls(s)
    if len(spans) != 1:
        return False
    start, end, _url = spans[0]
    if start != 0:
        return False
    return trim_url(s[end:]) == ""


def is_baidu_pan_url(url):
    """网址主机是否属于百度网盘：pan/yun/eyun.baidu.com 及其子域。

    用于实验性模式的「pan.baidu 一律静默、禁止显式浏览器打开」判定。
    解析失败/空值一律返回 False，绝不抛异常。
    """
    try:
        s = str(url or "").strip()
        if not s:
            return False
        parts = urlsplit(s if "://" in s else "//" + s)
        host = (parts.hostname or "").lower().rstrip(".")
    except Exception:
        return False
    if not host:
        return False
    for base in _BAIDU_PAN_HOSTS:
        if host == base or host.endswith("." + base):
            return True
    return False


