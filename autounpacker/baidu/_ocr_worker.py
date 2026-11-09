# -*- coding: utf-8 -*-
"""OCR 子进程 worker：`python _ocr_worker.py <图片路径>` → stdout 一行 JSON。

为什么独立成脚本：ddddocr（含 onnxruntime）只装在 Python311，程序本体跑
Python310 / PyInstaller —— 识别必须在子进程里做，主进程绝不 import ddddocr。

ddddocr 1.6.1 的 `classification(img, probability=True)` 返回的是 **str(dict)**
（`{'text': 'vsah', 'probabilities': [[[...几 MB...]]], 'confidence': 0.93}`），
这里用 ast.literal_eval 解析后**只取 text / confidence** 两个字段输出 ——
probabilities 大数组绝不带出本进程（否则一进日志就是几 MB）。

输出约定（永远只有最后一行是 JSON，ensure_ascii=False）：
  成功 `{"ok": true, "text": "vsah", "confidence": 0.93}`
  失败 `{"ok": false, "error": "<异常类型>: <消息>"}`
本脚本会被 subprocess 直接执行，**不做任何相对导入**（不依赖 autounpacker 包）。
"""
import ast
import json
import sys


def main(argv):
    if len(argv) < 2:
        print(json.dumps({"ok": False, "error": "missing image path"},
                         ensure_ascii=False))
        return 2
    try:
        import ddddocr
        ocr = ddddocr.DdddOcr(show_ad=False)
        with open(argv[1], "rb") as f:
            img = f.read()
        res = ocr.classification(img, probability=True)
        # 1.6.1 probability=True 返回 str(dict)；个别版本可能直接给 dict，两种都认。
        if isinstance(res, str):
            res = ast.literal_eval(res)
        text = str((res or {}).get("text") or "")
        try:
            conf = float((res or {}).get("confidence") or 0.0)
        except Exception:
            conf = 0.0
        print(json.dumps({"ok": True, "text": text, "confidence": conf},
                         ensure_ascii=False))
        return 0
    except Exception as e:
        print(json.dumps({"ok": False,
                          "error": "%s: %s" % (type(e).__name__, e)},
                         ensure_ascii=False))
        return 1


if __name__ == "__main__":
    sys.exit(main(sys.argv))
