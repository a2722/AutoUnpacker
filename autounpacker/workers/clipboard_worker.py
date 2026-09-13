# -*- coding: utf-8 -*-
"""剪贴板写入子进程：把 stdin 传入的文本写入系统剪贴板。

职责：- 从 stdin(UTF-8) 读取文本，经 win32clipboard 写入剪贴板
- 隔离 SetClipboardData 的原生堆损坏（0xc0000374）风险
关键入口：main()
依赖：win32clipboard
注意：退出码 0=成功，1=写入失败，2=解码失败；由 QRMonitor._set_clipboard 以子进程方式调用
"""
import sys


def main():
    data = sys.stdin.buffer.read()
    try:
        text = data.decode("utf-8")
    except Exception:
        return 2
    if not text:
        return 2
    try:
        import win32clipboard
        win32clipboard.OpenClipboard()
        try:
            win32clipboard.EmptyClipboard()
            win32clipboard.SetClipboardData(
                win32clipboard.CF_UNICODETEXT, text)
        finally:
            win32clipboard.CloseClipboard()
        return 0
    except Exception:
        return 1


if __name__ == "__main__":
    sys.exit(main())
