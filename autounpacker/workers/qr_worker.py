# -*- coding: utf-8 -*-
"""二维码解码子进程：加载图片并调用 qr_decode 解码，隔离原生库崩溃。

职责：- 从命令行参数读图片路径，PIL 加载后交 decode_qr_image() 解码
- 结果逐行输出到 stdout；强制 stdout/stderr 为 UTF-8 与父进程对齐
关键入口：main()
依赖：PIL、qr_decode
注意：退出码 0=成功，2=缺参数，3=图片打开失败，4=解码失败；崩溃只影响本进程
"""
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))


def _force_utf8_stdio():
    """把 stdout/stderr 强制为 UTF-8。

    子进程 stdout 被管道接走时，Python 默认用系统编码（中文 Windows 为
    cp936/GBK）写文本；父进程 _decode_qr_file 按 UTF-8 解码，中文就会变成
    「�」乱码（URL/提取码等 ASCII 不受影响）。这里与父进程对齐为 UTF-8。"""
    for stream in (sys.stdout, sys.stderr):
        if stream is None:
            continue
        try:
            stream.reconfigure(encoding="utf-8")
        except Exception:
            pass


def main():
    _force_utf8_stdio()
    if len(sys.argv) < 2:
        return 2
    path = sys.argv[1]
    try:
        from PIL import Image
        image = Image.open(path)
        image.load()          # 强制解码，C 库崩溃发生在这里（只影响本进程）
    except Exception as e:
        print(f"__OPEN_ERROR__ {e}", file=sys.stderr)
        return 3
    try:
        from qr_decode import decode_qr_image
        results = decode_qr_image(image)
    except Exception as e:
        print(f"__DECODE_ERROR__ {e}", file=sys.stderr)
        return 4
    for r in results:
        sys.stdout.write(r.replace("\r", " ").replace("\n", " ") + "\n")
    sys.stdout.flush()
    return 0


if __name__ == "__main__":
    sys.exit(main())
