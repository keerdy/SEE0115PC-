"""Wire protocol client shared by the CLI and GUI."""

from __future__ import annotations

import ctypes
import json
import os
import re
import socket
import struct
import sys
import threading
import time
from typing import Any, Dict, Iterator, Optional


DEFAULT_HOST = "192.168.1.2"
DEFAULT_PORT = 19099
DEFAULT_TIMEOUT = 3.0
MAX_JSON_FRAME = 64 * 1024
MAX_BINARY_FRAME = 16 * 1024 * 1024

# Allow Windows Winsock/RNDIS to release closed socket resources before the
# next short request creates another socket.
_SOCKET_SETTLE_SECONDS = 0.1


def init_com_mta() -> None:
    """Initialize COM as MTA on the calling thread (Windows, idempotent).

    Background threads that perform socket/DNS work get implicitly initialized
    as STA by Windows, which conflicts with the Qt main thread's COM and causes
    0x8001010d re-entrancy crashes over long sessions.  Entering MTA explicitly
    avoids the re-entrancy.  Safe to call on already-initialized threads.
    """
    if sys.platform == "win32":
        try:
            ctypes.windll.ole32.CoInitializeEx(None, 0x0)  # COINIT_MULTITHREADED
        except OSError:
            pass


class TestAgentError(RuntimeError):
    pass


class RemoteCommandError(TestAgentError):
    """The device returned a well-formed response with a non-zero code."""

    def __init__(self, response: Dict[str, Any], fallback: str) -> None:
        self.response = dict(response)
        super().__init__(str(response.get("msg", fallback)))


# 全局锁：串行化所有 socket 的 getaddrinfo/bind/connect/setsockopt 操作。
# 多设备并发 create_bound_socket 会在 Windows Winsock/COM 层触发
# RPC_E_CALL_REJECTED (0x8001010d) → 堆损坏 → 进程崩溃。串行化后 6 台探活
# 约 360ms，用户无感，且彻底消除并发崩溃。connect 完成即释放锁，后续
# recv/send（长连接）不受影响，仍可并行。
_socket_create_lock = threading.Lock()

# 全局锁：串行化所有短请求（create + send + recv）和 FTP 数据传输。
# 多设备并发 recv + FTP storbinary 会在 Windows Winsock 层触发 access violation
# / 堆损坏 (0xc0000374)。device._request 和 deployment 的 FTP 操作共用此锁，
# 确保 OTA 上传期间不会和设备探活并发 socket I/O。OTA 上传时设备在升级，
# 探活被阻塞几十秒是可接受的（设备 TestAgent 服务此时也不可用）。
_request_lock = threading.Lock()

_RUNTIME_SERIAL_PATTERN = re.compile(r"^[A-Za-z0-9._-]{1,64}$")


def create_bound_socket(
    source_ip: str,
    target_host: str,
    target_port: int,
    timeout: float,
    source_if_index: int | None = None,
    cancel_event: threading.Event | None = None,
) -> socket.socket:
    last_err: Exception | None = None
    for attempt in range(3):
        if cancel_event is not None and cancel_event.is_set():
            raise TestAgentError("request cancelled")
        if attempt > 0:
            if cancel_event is not None and cancel_event.wait(0.15 * attempt):
                raise TestAgentError("request cancelled")
        sock: socket.socket | None = None
        with _socket_create_lock:
            try:
                info = socket.getaddrinfo(target_host, target_port, socket.AF_INET, socket.SOCK_STREAM)
                if not info:
                    raise TestAgentError(f"no IPv4 address for {target_host}")
                family, type_, proto, _, address = info[0]
                sock = socket.socket(family, type_, proto)
                sock.settimeout(timeout)
                sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
                sock.bind((source_ip, 0))
                if sys.platform == "win32" and source_if_index is not None:
                    if source_if_index <= 0:
                        raise TestAgentError(f"invalid Windows interface index: {source_if_index}")
                    option = getattr(socket, "IP_UNICAST_IF", 31)
                    sock.setsockopt(socket.IPPROTO_IP, option, socket.htonl(source_if_index))
                sock.connect(address)
                if cancel_event is not None and cancel_event.is_set():
                    sock.close()
                    raise TestAgentError("request cancelled")
                return sock
            except (OSError, socket.timeout, ConnectionError) as exc:
                last_err = exc
                if sock is not None:
                    try:
                        sock.close()
                    except OSError:
                        pass
                continue
            except Exception:
                if sock is not None:
                    try:
                        sock.close()
                    except OSError:
                        pass
                raise
    raise TestAgentError(
        f"failed to connect {source_ip} → {target_host}:{target_port} "
        f"after 3 attempts: {last_err}"
    ) from last_err


class TestAgentClient:
    def __init__(
        self,
        host: str = DEFAULT_HOST,
        port: int = DEFAULT_PORT,
        timeout: float = DEFAULT_TIMEOUT,
        source_host: str | None = None,
        token: str | None = None,
        source_if_index: int | None = None,
        cancel_event: threading.Event | None = None,
    ):
        self.host = host
        self.port = port
        self.timeout = timeout
        self.source_host = source_host
        self.source_if_index = source_if_index
        self.cancel_event = cancel_event
        self.token = token if token is not None else os.getenv("POCKET_TESTAGENT_TOKEN", "")
        self.sock: Optional[socket.socket] = None

    def __enter__(self) -> "TestAgentClient":
        self.connect()
        return self

    def __exit__(self, exc_type, exc, tb) -> None:
        self.close()

    def connect(self) -> None:
        self._check_cancelled()
        if self.sock is not None:
            return
        if self.source_host:
            self.sock = create_bound_socket(
                self.source_host, self.host, self.port, self.timeout,
                self.source_if_index,
                self.cancel_event,
            )
        else:
            self.sock = socket.create_connection((self.host, self.port), self.timeout)

    def close(self) -> None:
        if self.sock is not None:
            self.sock.close()
            self.sock = None

    def request(self, payload: Dict[str, Any]) -> Dict[str, Any]:
        self._check_cancelled()
        self.send(payload)
        return self.recv()

    def send(self, payload: Dict[str, Any]) -> None:
        self._check_cancelled()
        if self.sock is None:
            self.connect()
        assert self.sock is not None
        request = dict(payload)
        if self.token and "token" not in request:
            request["token"] = self.token
        body = json.dumps(request, separators=(",", ":")).encode("utf-8")
        if not body or len(body) > MAX_JSON_FRAME:
            raise TestAgentError(f"invalid request size: {len(body)}")
        self.sock.sendall(struct.pack("<I", len(body)) + body)

    def recv(self) -> Dict[str, Any]:
        self._check_cancelled()
        if self.sock is None:
            raise TestAgentError("not connected")
        length = struct.unpack("<I", self._recv_exact(4))[0]
        if length <= 0 or length > MAX_JSON_FRAME:
            raise TestAgentError(f"invalid frame length: {length}")
        decoded = json.loads(self._recv_exact(length).decode("utf-8"))
        if not isinstance(decoded, dict):
            raise TestAgentError("response is not a JSON object")
        return decoded

    def screenshot(self, capture_type: str = "fb1") -> tuple[Dict[str, Any], bytes]:
        self.send({"cmd": "screenshot", "type": capture_type})
        metadata = self.recv()
        self._require_success(metadata, "screenshot failed")
        return metadata, self._recv_binary(int(metadata.get("raw_len", 0)))

    def switch_ui_page(self, page: str) -> Dict[str, Any]:
        reply = self.request({"cmd": "switch_ui_page", "page": page})
        self._require_success(reply, "UI 页面切换失败")
        return reply

    def capture_photo(self) -> Dict[str, Any]:
        reply = self.request({"cmd": "capture_photo"})
        self._require_success(reply, "拍照失败")
        return reply

    def record_start(self) -> Dict[str, Any]:
        reply = self.request({"cmd": "record_start"})
        self._require_success(reply, "开始录像失败")
        return reply

    def record_stop(self) -> Dict[str, Any]:
        reply = self.request({"cmd": "record_stop"})
        self._require_success(reply, "停止录像失败")
        return reply

    def gimbal_move(self, direction: str, duration_ms: int = 300) -> Dict[str, Any]:
        reply = self.request({
            "cmd": "gimbal_move",
            "direction": direction,
            "duration_ms": int(duration_ms),
        })
        self._require_success(reply, "云台转动失败")
        return reply

    def swipe_screen(self, direction: str) -> Dict[str, Any]:
        reply = self.request({"cmd": "swipe_screen", "direction": direction})
        self._require_success(reply, "滑动屏幕失败")
        return reply

    def set_runtime_serial(self, serial_number: str) -> Dict[str, Any]:
        serial = str(serial_number).strip()
        if not _RUNTIME_SERIAL_PATTERN.fullmatch(serial):
            raise TestAgentError("运行态 SN 只能包含 ASCII 字母、数字、点、下划线或短横线，长度 1-64")
        reply = self.request({"cmd": "set_runtime_serial", "sn": serial})
        self._require_success(reply, "写入运行态 SN 失败")
        if reply.get("runtime_only") is not True or reply.get("serial_number") != serial:
            raise TestAgentError("设备未确认运行态 SN 写入结果")
        return reply

    def get_runtime_serial(self) -> Dict[str, Any]:
        reply = self.request({"cmd": "get_runtime_serial"})
        self._require_success(reply, "读取运行态 SN 失败")
        return reply

    def get_file(self, remote_path: str) -> tuple[Dict[str, Any], bytes]:
        self.send({"cmd": "get_file", "path": remote_path})
        metadata = self.recv()
        self._require_success(metadata, "get_file failed")
        return metadata, self._recv_binary(int(metadata.get("size", 0)))

    def watch_case_status(self, interval_ms: int = 500) -> Iterator[Dict[str, Any]]:
        reply = self.request({"cmd": "watch_case_status", "interval_ms": interval_ms})
        yield reply
        assert self.sock is not None
        self.sock.settimeout(None)
        while True:
            yield self.recv()

    def _recv_binary(self, expected_size: int) -> bytes:
        if expected_size <= 0 or expected_size > MAX_BINARY_FRAME:
            raise TestAgentError(f"invalid binary size: {expected_size}")
        frame_size = struct.unpack("<I", self._recv_exact(4))[0]
        if frame_size != expected_size:
            raise TestAgentError(
                f"binary length mismatch: metadata={expected_size}, frame={frame_size}"
            )
        return self._recv_exact(frame_size)

    def _recv_exact(self, size: int) -> bytes:
        assert self.sock is not None
        deadline = time.monotonic() + max(5.0, self.timeout)
        chunks: list[bytes] = []
        remaining = size
        while remaining > 0:
            self._check_cancelled()
            remaining_time = deadline - time.monotonic()
            if remaining_time <= 0:
                raise TestAgentError("receive timeout")
            self.sock.settimeout(min(0.25, remaining_time))
            try:
                chunk = self.sock.recv(remaining)
            except socket.timeout as exc:
                continue
            except OSError as exc:
                raise TestAgentError(f"recv error: {exc}") from exc
            if not chunk:
                raise TestAgentError("connection closed")
            chunks.append(chunk)
            remaining -= len(chunk)
        return b"".join(chunks)

    def _check_cancelled(self) -> None:
        if self.cancel_event is not None and self.cancel_event.is_set():
            self.close()
            raise TestAgentError("request cancelled")

    @staticmethod
    def _require_success(response: Dict[str, Any], fallback: str) -> None:
        if response.get("code") != 0:
            raise RemoteCommandError(response, fallback)
