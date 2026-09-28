# -*- coding: utf-8 -*-
"""TLS 根证书回退：系统根证书存储不完整时，用打包内置的 CA pem 重建校验上下文。

背景：干净/长期未更新的 Windows 机器，系统根证书存储可能不完整，此时 urllib 的
HTTPS 请求会以 ssl.SSLCertVerificationError 失败（unable to get local issuer
certificate）——首次运行的 7-Zip 隔离版安装与自动更新都会当场死在联网这一步。

策略：绝不关闭证书校验（那等于把 HTTPS 降级成明文）。仅当第一次请求确因
「证书验证失败」失败、且程序带了一份内置 CA pem 时，才用
ssl.create_default_context(cafile=<pem>) 重建上下文重试一次；没有内置 pem 或
重试仍失败，一律重抛第一次的异常，不做任何降级。

职责：- ca_bundle_path() 定位内置 CA pem（冻结 = sys._MEIPASS\\cacert.pem；
        源码 = <仓库根>\\build_assets\\cacert.pem），不存在返回 None
- open_url() 统一打开入口：默认上下文 → 证书验证失败时用内置 pem 重试一次
关键入口：open_url()
依赖：仅标准库（ssl / urllib.request / sys / pathlib）——不引入 certifi/requests
注意：tools/gen_ca_bundle.py 在打包时从本机 ROOT/CA 存储导出这份 pem；
      任何情况下都不得使用 _create_unverified_context / CERT_NONE 之类「关闭校验」方案。
"""
import ssl
import sys
import urllib.error
import urllib.request
from pathlib import Path

# 内置 CA pem 文件名：打包时以目标 "." 放进打包根（PyInstaller 6 即 _internal\\，
# 与 sys._MEIPASS 一致），源码运行则在仓库根的 build_assets\\ 下。
CA_BUNDLE_NAME = "cacert.pem"


def ca_bundle_path():
    """内置 CA pem 的路径；不存在返回 None。

    冻结（PyInstaller）运行：sys._MEIPASS / cacert.pem（打包目标 "." 的落点）；
    源码运行：<仓库根>/build_assets/cacert.pem（tools/gen_ca_bundle.py 的产物）。
    只判断「存在且是文件」，内容是否可用交由 ssl.create_default_context 验证。
    """
    try:
        if getattr(sys, "frozen", False):
            meipass = getattr(sys, "_MEIPASS", "")
            if not meipass:
                return None
            cand = Path(meipass) / CA_BUNDLE_NAME
        else:
            cand = (Path(__file__).resolve().parent.parent
                    / "build_assets" / CA_BUNDLE_NAME)
        return cand if cand.is_file() else None
    except OSError:
        return None


def _is_cert_verify_error(exc):
    """是否为「证书验证失败」异常。

    urllib 会把它包进 urllib.error.URLError（原因放在 .reason），所以两种形状都要认，
    否则真正的 SSL 失败会被漏掉、回退永不触发。"""
    if isinstance(exc, ssl.SSLCertVerificationError):
        return True
    return (isinstance(exc, urllib.error.URLError)
            and isinstance(getattr(exc, "reason", None),
                           ssl.SSLCertVerificationError))


def open_url(url, timeout=15, headers=None):
    """打开 URL（TLS 校验保持开启）；系统根证书不全时用内置 CA pem 重试一次。

    第一次用 urllib 默认上下文；仅当失败原因是证书验证失败、且存在内置 pem 时，
    才用 ssl.create_default_context(cafile=<pem>) 重建上下文重试一次。没有内置
    pem 或重试也失败时，重抛**第一次**的异常（错误信息更贴近真实原因）。
    绝不关闭/削弱证书校验。成功时返回 urlopen 的响应对象（调用方负责关闭）。
    """
    req = urllib.request.Request(url, headers=dict(headers or {}))
    try:
        return urllib.request.urlopen(req, timeout=timeout)
    except (ssl.SSLCertVerificationError, urllib.error.URLError) as first:
        if not _is_cert_verify_error(first):
            raise
        pem = ca_bundle_path()
        if pem is None:
            raise
        try:
            ctx = ssl.create_default_context(cafile=str(pem))
        except (OSError, ssl.SSLError):
            raise first
        try:
            return urllib.request.urlopen(req, timeout=timeout, context=ctx)
        except Exception:
            raise first
