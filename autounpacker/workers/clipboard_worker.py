# -*- coding: utf-8 -*-
"""剪贴板写入子进程：主程序把要写入的文本通过 stdin 传入。

win32clipboard 的 SetClipboardData 在并发/特殊输入下可能触发原生堆损坏
（0xc0000374），在独立子进程执行可确保崩溃只影响本进程，不拖垮常驻主程序。
文本经 stdin(UTF-8) 传输，避免命令行参数编码/长度问题。
用法:  pythonw clipboard_worker.py < 文本
退出码 0=成功, 1=写入失败, 2=解码失败。
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
