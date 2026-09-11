# -*- coding: utf-8 -*-
"""二维码解码子进程：主程序把图片/原始字节存到临时文件后调用本脚本。

原生库（cv2/pyzbar/PIL）崩溃只影响本进程，不影响常驻主程序。
用法:
    pythonw qr_worker.py <文件路径>
解码结果逐行输出到 stdout（每行一个文本）；非零退出码表示失败/崩溃。
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
