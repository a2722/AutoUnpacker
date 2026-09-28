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
            try:
                win32clipboard.CloseClipboard()
            except Exception:
                # 1418 同族良性竞态：关的时候剪贴板已被系统/其它进程关闭。数据已经
                # 写入成功，不能把它判成失败——否则上层会认为本次静默复制没成功，
                # 不推进 last_text，刚写的链接可能被自己当成用户输入再处理一次。
                pass
        return 0
    except Exception:
        return 1


if __name__ == "__main__":
    sys.exit(main())
