# -*- coding: utf-8 -*-
"""从本机 Windows 证书存储（ROOT/CA）导出根证书包 build_assets\\cacert.pem。

为什么需要：干净/长期未更新的 Windows 机器的系统根证书存储可能不完整，此时程序里
所有 HTTPS 请求都会以 ssl.SSLCertVerificationError 失败
（unable to get local issuer certificate）。打包时把本机可用的根证书导出成一份 pem
随包分发，netca.open_url() 在系统校验失败时用这份 pem 重试一次——既不关闭证书校验，
也不依赖 certifi 之类第三方包。

行为：
- 用标准库 ssl.enum_certificates 读 ROOT 与 CA 两个存储；
- 每张证书按 DER→PEM 写出，去重后按 PEM 文本排序（确定性顺序）；
- 幂等：目标文件已存在且内容与新生成的完全一致时，什么都不写；
- 原子写：先写同目录临时文件再 os.replace，绝不留下半截文件；
- 读取失败 / 遇到无法转换的证书编码 / 一张都读不到：打印警告并非零退出。

用法：python tools\\gen_ca_bundle.py
"""
from __future__ import annotations

import os
import ssl
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
OUT = ROOT / "build_assets" / "cacert.pem"
STORES = ("ROOT", "CA")


def _collect_pems() -> list[str]:
    """读取 ROOT/CA 存储，返回去重且排序后的 PEM 文本列表；任何不可靠情形抛异常。"""
    enum = getattr(ssl, "enum_certificates", None)
    if enum is None:
        raise RuntimeError("当前 Python 不支持 ssl.enum_certificates（仅 Windows 可用）")
    pem_set = set()
    for store in STORES:
        for der, encoding, _trust in enum(store):
            if encoding != "x509_asn":
                raise RuntimeError(
                    f"{store} 存储返回了非单证书编码（{encoding}），无法可靠转成 PEM")
            pem_set.add(ssl.DER_cert_to_PEM_cert(der).strip())
    return sorted(pem_set)


def main() -> int:
    try:
        pems = _collect_pems()
    except Exception as e:
        print("!! 生成 CA 证书包失败：%s" % e)
        return 1
    if not pems:
        print("!! ROOT/CA 存储里一张证书都没读到，拒绝写空包")
        return 1
    content = "\n".join(pems) + "\n"
    try:
        if OUT.exists() and OUT.read_text(encoding="ascii") == content:
            print("CA 证书包无变化，未写入：%s（%d 张证书）" % (OUT, len(pems)))
            return 0
        OUT.parent.mkdir(parents=True, exist_ok=True)
        fd, tmp_name = tempfile.mkstemp(prefix=OUT.name + ".", suffix=".tmp",
                                        dir=str(OUT.parent))
        try:
            with os.fdopen(fd, "w", encoding="ascii", newline="\n") as f:
                f.write(content)
            os.replace(tmp_name, str(OUT))
        except Exception:
            try:
                os.unlink(tmp_name)
            except OSError:
                pass
            raise
    except Exception as e:
        print("!! 写入 CA 证书包失败：%s" % e)
        return 1
    print("已写入 CA 证书包：%s（%d 张证书，%.1f KB）"
          % (OUT, len(pems), OUT.stat().st_size / 1024.0))
    return 0


if __name__ == "__main__":
    sys.exit(main())
