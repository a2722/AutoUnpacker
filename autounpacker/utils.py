# -*- coding: utf-8 -*-
"""通用工具：开机时间点、配置路径规范化、文件占用检测、崩溃日志。"""
import ctypes
import os
import sys
import threading
import time

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
    7-Zip 打不开（报"另一个程序正在使用此文件"），应 defer 等待。"""
    try:
        f = open(path, "ab")
        f.close()
        return True
    except OSError:
        return False


def _install_crash_log():
    """把未捕获异常写入 APP_DIR/crash.log，便于排查闪退问题"""
    try:
        crash_file = paths.CRASH_LOG

        def _main_hook(exc_type, exc_value, exc_tb):
            try:
                import traceback
                with open(crash_file, "a", encoding="utf-8") as f:
                    f.write(f"[{time.strftime('%Y-%m-%d %H:%M:%S')}] 主线程异常:\n")
                    traceback.print_exception(exc_type, exc_value, exc_tb, file=f)
                    f.write("\n")
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

        sys.excepthook = _main_hook
        try:
            threading.excepthook = _thread_hook
        except AttributeError:
            pass
    except Exception:
        pass


