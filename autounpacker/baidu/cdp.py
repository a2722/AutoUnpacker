# -*- coding: utf-8 -*-
"""最小 CDP 客户端（零第三方依赖，Python 3.10 可用）。

为什么自己写而不是用 `websockets`/`playwright`：
- 运行期 Python 是 3.10，环境里**没有** websockets / playwright / selenium；
- 本项目一贯零新依赖（打包简单、体积可控）；
- 我们只需要「连本地 ws:// → 发 JSON → 收 JSON」，不需要任何高级特性。

本模块只实现 RFC 6455 客户端的最小子集：
- 握手（GET + Sec-WebSocket-Key）
- 文本帧发送（客户端必须掩码）
- 文本/二进制帧接收（不请求 permessage-deflate，故不会有压缩帧）
- ping/pong、close

不实现：分片重组（服务端小消息不分片；若真遇到，按错误返回而不是挂死）、TLS。
"""

import base64
import hashlib
import json
import os
import socket
import struct
import time

__all__ = ["CDPError", "WebSocket", "CDP"]

_WS_GUID = "258EAFA5-E914-47DA-95CA-C5AB0DC85B11"


class CDPError(Exception):
    """连接/协议/超时错误（调用方统一转成「桥不可用」）。"""


class WebSocket(object):
    """极简 WebSocket 客户端（仅供本地 CDP 使用）。"""

    def __init__(self, sock):
        self._sock = sock
        self._buf = b""
        self._closed = False

    # ---------- 建立连接 ----------
    @classmethod
    def connect(cls, ws_url, timeout=10.0):
        """ws_url 形如 ws://127.0.0.1:9222/devtools/page/XXXX。"""
        u = str(ws_url or "").strip()
        if not u.startswith("ws://"):
            raise CDPError("只支持 ws:// 本地地址: %r" % u)
        rest = u[len("ws://"):]
        if "/" in rest:
            hostport, path = rest.split("/", 1)
            path = "/" + path
        else:
            hostport, path = rest, "/"
        host, _, port = hostport.partition(":")
        try:
            port = int(port or 80)
        except ValueError:
            raise CDPError("端口非法: %r" % hostport)
        try:
            sock = socket.create_connection((host, port), timeout=timeout)
        except OSError as e:
            raise CDPError("连不上调试端口: %s" % e)
        sock.settimeout(timeout)
        key = base64.b64encode(os.urandom(16)).decode("ascii")
        req = (
            "GET %s HTTP/1.1\r\n"
            "Host: %s:%d\r\n"
            "Upgrade: websocket\r\n"
            "Connection: Upgrade\r\n"
            "Sec-WebSocket-Key: %s\r\n"
            "Sec-WebSocket-Version: 13\r\n"
            "\r\n" % (path, host, port, key))
        try:
            sock.sendall(req.encode("ascii"))
            raw = b""
            while b"\r\n\r\n" not in raw:
                chunk = sock.recv(4096)
                if not chunk:
                    raise CDPError("握手时连接被关闭")
                raw += chunk
                if len(raw) > 65536:
                    raise CDPError("握手响应过大")
        except OSError as e:
            try:
                sock.close()
            except Exception:
                pass
            raise CDPError("握手失败: %s" % e)
        head, _, leftover = raw.partition(b"\r\n\r\n")
        first = head.split(b"\r\n", 1)[0].decode("latin-1", "replace")
        if " 101" not in first:
            try:
                sock.close()
            except Exception:
                pass
            raise CDPError("握手未升级: %s" % first)
        expect = base64.b64encode(
            hashlib.sha1((key + _WS_GUID).encode("ascii")).digest()).decode("ascii")
        if expect.encode("ascii") not in head.replace(b" ", b""):
            # 头名大小写/空格差异，做一次宽松比对
            if expect.encode("ascii") not in head:
                try:
                    sock.close()
                except Exception:
                    pass
                raise CDPError("Sec-WebSocket-Accept 校验失败")
        ws = cls(sock)
        ws._buf = leftover
        return ws

    # ---------- 发送 ----------
    def send_text(self, text):
        if self._closed:
            raise CDPError("连接已关闭")
        payload = text.encode("utf-8")
        header = bytearray()
        header.append(0x81)                      # FIN + text
        n = len(payload)
        if n < 126:
            header.append(0x80 | n)              # 客户端必须掩码
        elif n < (1 << 16):
            header.append(0x80 | 126)
            header += struct.pack("!H", n)
        else:
            header.append(0x80 | 127)
            header += struct.pack("!Q", n)
        mask = os.urandom(4)
        header += mask
        masked = bytes(b ^ mask[i % 4] for i, b in enumerate(payload))
        try:
            self._sock.sendall(bytes(header) + masked)
        except OSError as e:
            self._closed = True
            raise CDPError("发送失败: %s" % e)

    # ---------- 接收 ----------
    def _read_exact(self, n, deadline):
        while len(self._buf) < n:
            remain = deadline - time.time()
            if remain <= 0:
                raise CDPError("读超时")
            try:
                self._sock.settimeout(min(remain, 5.0))
                chunk = self._sock.recv(65536)
            except socket.timeout:
                continue
            except OSError as e:
                self._closed = True
                raise CDPError("接收失败: %s" % e)
            if not chunk:
                self._closed = True
                raise CDPError("连接被关闭")
            self._buf += chunk
        out, self._buf = self._buf[:n], self._buf[n:]
        return out

    def recv_text(self, timeout=20.0):
        """读一条完整文本消息；控制帧内部处理掉。超时抛 CDPError。"""
        deadline = time.time() + timeout
        while True:
            hdr = self._read_exact(2, deadline)
            b0, b1 = hdr[0], hdr[1]
            opcode = b0 & 0x0F
            masked = bool(b1 & 0x80)
            length = b1 & 0x7F
            if length == 126:
                length = struct.unpack("!H", self._read_exact(2, deadline))[0]
            elif length == 127:
                length = struct.unpack("!Q", self._read_exact(8, deadline))[0]
            if length > 64 * 1024 * 1024:
                raise CDPError("帧过大: %d" % length)
            mask = self._read_exact(4, deadline) if masked else None
            payload = self._read_exact(length, deadline) if length else b""
            if mask:
                payload = bytes(b ^ mask[i % 4] for i, b in enumerate(payload))
            if opcode == 0x8:                      # close
                self._closed = True
                raise CDPError("服务端关闭连接")
            if opcode == 0x9:                      # ping -> pong
                try:
                    self._sock.sendall(b"\x8a\x80" + os.urandom(4))
                except OSError:
                    pass
                continue
            if opcode == 0xA:                      # pong
                continue
            if opcode in (0x1, 0x2):
                if b0 & 0x80 == 0:
                    # 分片：我们不用，直接判错（避免挂死）
                    raise CDPError("不支持分片帧")
                try:
                    return payload.decode("utf-8", "replace")
                except Exception:
                    return payload.decode("latin-1", "replace")
            # 其它 opcode 忽略

    def close(self):
        if self._closed:
            return
        self._closed = True
        try:
            self._sock.sendall(b"\x88\x80" + os.urandom(4))
        except Exception:
            pass
        try:
            self._sock.close()
        except Exception:
            pass


class CDP(object):
    """CDP 会话封装：call() 发命令收结果，事件先囤起来。"""

    def __init__(self, ws):
        self.ws = ws
        self._id = 0
        self.events = []

    def call(self, method, params=None, session=None, timeout=20.0):
        self._id += 1
        mid = self._id
        msg = {"id": mid, "method": method, "params": params or {}}
        if session:
            msg["sessionId"] = session
        self.ws.send_text(json.dumps(msg, ensure_ascii=False))
        deadline = time.time() + timeout
        while True:
            remain = deadline - time.time()
            if remain <= 0:
                raise CDPError("CDP 超时: %s" % method)
            data = json.loads(self.ws.recv_text(timeout=remain))
            if data.get("id") == mid:
                if "error" in data:
                    raise CDPError("%s -> %s" % (method, data["error"]))
                return data.get("result") or {}
            if "method" in data:
                self.events.append(data)
                if len(self.events) > 200:
                    self.events = self.events[-100:]

    def close(self):
        try:
            self.ws.close()
        except Exception:
            pass
