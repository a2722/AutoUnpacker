# -*- coding: utf-8 -*-
"""二维码图片解码（多引擎），独立模块供子进程隔离调用。

cv2 / pyzbar / PIL 等原生库在极端输入下可能段错误或写坏堆（0xc0000374），
把解码放到独立子进程执行可避免拖垮常驻主程序。
"""
import numpy as np


def decode_qr_image(image):
    """对 PIL Image 解码，返回去重后的文本列表。

    引擎1: cv2.QRCodeDetector（多尺度放大，对小图/带噪图更稳）
    引擎2: pyzbar（对模糊/轻微形变更稳）
    引擎3: 增强兜底（锐化 + 自适应二值化）
    """
    import cv2
    import pyzbar.pyzbar as pyzbar

    results = []
    seen = set()
    try:
        rgb = image.convert("RGB")
        bgr = np.array(rgb)[:, :, ::-1].copy()
    except Exception:
        bgr = None
    try:
        gray = np.array(image.convert("L"))
    except Exception:
        gray = None

    def _push(text):
        if text and text not in seen:
            seen.add(text)
            results.append(text)

    # 引擎1: cv2.QRCodeDetector —— 关键在放大，小图/压缩噪点图直接扫常失败
    if bgr is not None:
        for s in (1.0, 2.0, 3.0):
            work = bgr if s == 1.0 else cv2.resize(
                bgr, None, fx=s, fy=s, interpolation=cv2.INTER_CUBIC)
            data, _, _ = cv2.QRCodeDetector().detectAndDecode(
                cv2.cvtColor(work, cv2.COLOR_BGR2GRAY))
            if data:
                _push(data)
                break
    # 引擎2: pyzbar
    if gray is not None:
        for s in (1.0, 2.0, 4.0):
            g = gray if s == 1.0 else cv2.resize(
                gray, None, fx=s, fy=s, interpolation=cv2.INTER_CUBIC)
            objs = pyzbar.decode(g)
            for obj in objs:
                if obj.type == "QRCODE":
                    try:
                        text = obj.data.decode("utf-8")
                    except Exception:
                        try:
                            text = obj.data.decode("gbk")
                        except Exception:
                            continue
                    _push(text)
            if results:
                break
    # 引擎3: 增强兜底（锐化 + 自适应二值化）只在前面都失败时尝试
    if not results and gray is not None:
        g = cv2.resize(gray, None, fx=4, fy=4, interpolation=cv2.INTER_CUBIC)
        g = cv2.GaussianBlur(g, (3, 3), 0)
        g = cv2.adaptiveThreshold(
            g, 255, cv2.ADAPTIVE_THRESH_GAUSSIAN_C,
            cv2.THRESH_BINARY, 41, 11)
        data, _, _ = cv2.QRCodeDetector().detectAndDecode(g)
        if data:
            _push(data)
        if not results:
            for obj in pyzbar.decode(g):
                if obj.type == "QRCODE":
                    try:
                        text = obj.data.decode("utf-8")
                    except Exception:
                        continue
                    _push(text)
    return results
