"""USB RNDIS device discovery, addressing, and reconnect helpers."""

from __future__ import annotations

import ipaddress
import json
import os
from pathlib import Path
import re
import socket
import subprocess
import sys
import threading
import time
from typing import Any, Dict

from .protocol import (
    DEFAULT_HOST, DEFAULT_PORT, DEFAULT_TIMEOUT,
    RemoteCommandError, TestAgentClient, TestAgentError,
    _request_lock, _SOCKET_SETTLE_SECONDS,
)
from .app_paths import runtime_data_dir
from .app_logging import get_logger


DEFAULT_DEVICE_IP = DEFAULT_HOST
_USB_NETWORK = ipaddress.IPv4Network("192.168.1.0/24")
_DEVICE_IP_OFFSET = 90
_RNDIS_MARKERS = (
    "rndis", "remote ndis", "usb ethernet", "usb network", "usb ndis", "usb gadget",
)
_link_state_lock = threading.RLock()
_link_operation_locks: dict[str, threading.RLock] = {}
_link_operation_locks_lock = threading.Lock()
_LOG = get_logger()


def _link_operation_lock(link: Dict[str, Any] | None) -> threading.RLock:
    identity = str((link or {}).get("link_id") or (link or {}).get("adapter_id") or "global")
    with _link_operation_locks_lock:
        lock = _link_operation_locks.get(identity)
        if lock is None:
            lock = threading.RLock()
            _link_operation_locks[identity] = lock
        return lock


def _link_if_index(link: Dict[str, Any] | None) -> int | None:
    value = (link or {}).get("if_index")
    return value if isinstance(value, int) and value > 0 else None


def _acquire_cancellable_lock(
    lock: threading.RLock | threading.Lock,
    cancel_event: threading.Event | None,
) -> None:
    if cancel_event is None:
        lock.acquire()
        return
    while not lock.acquire(timeout=0.1):
        if cancel_event.is_set():
            raise TestAgentError("request cancelled")
    if cancel_event.is_set():
        lock.release()
        raise TestAgentError("request cancelled")


def _subprocess_kwargs() -> Dict[str, Any]:
    """Prevent console flashes when a windowed Windows application runs ipconfig."""
    if sys.platform == "win32":
        return {"creationflags": getattr(subprocess, "CREATE_NO_WINDOW", 0)}
    return {}


def device_ip_for_pc(pc_ip: str) -> str:
    """Return the deterministic device address assigned to a PC-side RNDIS link."""
    try:
        address = ipaddress.IPv4Address(pc_ip)
    except ipaddress.AddressValueError as exc:
        raise ValueError(f"invalid PC IPv4 address: {pc_ip}") from exc
    if address not in _USB_NETWORK:
        raise ValueError(f"PC IP must be in {_USB_NETWORK}")
    target_last = int(str(address).rsplit(".", 1)[1]) + _DEVICE_IP_OFFSET
    if target_last > 254:
        raise ValueError(f"PC IP cannot map to a device IP: {pc_ip}")
    return f"192.168.1.{target_last}"


def _state_path() -> Path:
    return Path(runtime_data_dir()) / "rndis_link_addresses.json"


def _load_link_state() -> Dict[str, Dict[str, str]]:
    try:
        raw = json.loads(_state_path().read_text(encoding="utf-8"))
        return raw if isinstance(raw, dict) else {}
    except (OSError, json.JSONDecodeError) as exc:
        _LOG.warning("rndis_link_state_load_failed path=%s error=%s", _state_path(), exc)
        return {}


def _save_link_state(state: Dict[str, Dict[str, str]]) -> None:
    path = _state_path()
    temp = path.with_suffix(".tmp")
    temp.write_text(json.dumps(state, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    temp.replace(path)


def get_device_note(adapter_id: str) -> str:
    """Read a user-defined memo for the device identified by adapter_id."""
    state = _load_link_state()
    entry = state.get(adapter_id)
    if not isinstance(entry, dict):
        return ""
    return entry.get("note", "")


def set_device_note(adapter_id: str, note: str) -> None:
    """Save a user-defined memo for the device identified by adapter_id."""
    with _link_state_lock:
        state = _load_link_state()
        if adapter_id not in state:
            state[adapter_id] = {}
        if note:
            state[adapter_id]["note"] = note
        else:
            state[adapter_id].pop("note", None)
        _save_link_state(state)


def _is_rndis(name: str, description: str = "") -> bool:
    value = f"{name} {description}".lower()
    return any(marker in value for marker in _RNDIS_MARKERS)


def _saved_link_addresses(value: object) -> tuple[str, str] | None:
    if not isinstance(value, dict):
        return None
    pc_ip = str(value.get("pc_ip", ""))
    device_ip = str(value.get("device_ip", ""))
    try:
        if ipaddress.IPv4Address(pc_ip) not in _USB_NETWORK:
            return None
        if ipaddress.IPv4Address(device_ip) not in _USB_NETWORK:
            return None
    except ipaddress.AddressValueError:
        return None
    return pc_ip, device_ip


def _is_usb_network_address(address: str) -> bool:
    try:
        return ipaddress.IPv4Address(address) in _USB_NETWORK
    except ipaddress.AddressValueError:
        return False


def _assign_link_addresses(links: list[Dict[str, Any]]) -> list[Dict[str, Any]]:
    with _link_state_lock:
        state = _load_link_state()
        changed = False
        claimed_saved_ids: set[str] = set()
        seen_pc_ips: dict[str, str] = {}
        seen_device_ips: dict[str, str] = {}
        for link in links:
            identity = str(link["adapter_id"])
            saved = state.get(identity)
            addresses = _saved_link_addresses(saved)
            observed = str(link.get("observed_pc_ip", ""))
            if not _is_usb_network_address(observed):
                link["pc_ip"] = ""
                link["device_ip"] = ""
                link["configured"] = False
                continue
            try:
                target = device_ip_for_pc(observed)
            except ValueError:
                link["pc_ip"] = observed
                link["device_ip"] = ""
                link["configured"] = False
                continue

            prior_device_ip = ""
            prior_configured = False
            if addresses is not None and addresses[0] == observed:
                prior_device_ip = addresses[1]
                prior_configured = bool(saved.get("configured", False))
                claimed_saved_ids.add(identity)
            else:
                migrated_id = next(
                    (
                        saved_id for saved_id, saved_value in state.items()
                        if saved_id not in claimed_saved_ids
                        and _saved_link_addresses(saved_value) is not None
                        and _saved_link_addresses(saved_value)[0] == observed
                    ),
                    None,
                )
                if migrated_id is not None:
                    migrated = state.pop(migrated_id)
                    migrated_addresses = _saved_link_addresses(migrated)
                    assert migrated_addresses is not None
                    prior_device_ip = migrated_addresses[1]
                    prior_configured = bool(migrated.get("configured", False))
                    claimed_saved_ids.add(identity)
                    changed = True

            link["pc_ip"] = observed
            link["device_ip"] = target
            link["configured"] = prior_configured and prior_device_ip == target
            previous_pc_link = seen_pc_ips.get(observed)
            previous_device_link = seen_device_ips.get(target)
            if previous_pc_link is not None or previous_device_link is not None:
                link["ambiguous"] = True
                link["configured"] = False
                _LOG.warning(
                    "rndis_address_collision adapter_id=%s pc_ip=%s device_ip=%s "
                    "previous_pc_link=%s previous_device_link=%s",
                    identity, observed, target, previous_pc_link or "", previous_device_link or "",
                )
            seen_pc_ips.setdefault(observed, identity)
            seen_device_ips.setdefault(target, identity)
            if prior_device_ip and prior_device_ip != target:
                link["previous_device_ip"] = prior_device_ip
            current_state = {
                "pc_ip": observed,
                "device_ip": target,
                "configured": link["configured"],
            }
            if state.get(identity) != current_state:
                state[identity] = current_state
                changed = True
        if changed:
            _save_link_state(state)
        return links


def _windows_links() -> list[Dict[str, Any]]:
    # 用 ctypes 直接调 iphlpapi.GetAdaptersAddresses，替代 PowerShell 子进程。
    # PowerShell (.NET) 启动时初始化 COM，在 Qt STA 主线程 + 多个后台线程
    # 环境下会触发 COM 重入崩溃 (0x8001010d)。原生 Win32 API 不涉及 COM。
    import ctypes
    from ctypes import wintypes

    AF_UNSPEC = 0
    GAA_FLAG_INCLUDE_PREFIX = 0x0010
    MAX_ADAPTER_ADDRESS_LENGTH = 8

    class _SOCKET_ADDRESS(ctypes.Structure):
        _fields_ = [
            ("lpSockaddr", ctypes.c_void_p),
            ("iSockaddrLength", wintypes.INT),
        ]

    class _IP_ADAPTER_UNICAST_ADDRESS(ctypes.Structure):
        pass

    _IP_ADAPTER_UNICAST_ADDRESS._fields_ = [
        ("Length", wintypes.ULONG),
        ("Flags", wintypes.DWORD),
        ("Next", ctypes.POINTER(_IP_ADAPTER_UNICAST_ADDRESS)),
        ("Address", _SOCKET_ADDRESS),
        ("PrefixOrigin", wintypes.INT),
        ("SuffixOrigin", wintypes.INT),
        ("DadState", wintypes.INT),
        ("ValidLifetime", wintypes.ULONG),
        ("PreferredLifetime", wintypes.ULONG),
        ("LeaseLifetime", wintypes.ULONG),
        ("OnLinkPrefixLength", wintypes.BYTE),
    ]

    class _IP_ADAPTER_ADDRESSES(ctypes.Structure):
        pass

    _IP_ADAPTER_ADDRESSES._fields_ = [
        ("Length", wintypes.ULONG),
        ("IfIndex", wintypes.DWORD),
        ("Next", ctypes.POINTER(_IP_ADAPTER_ADDRESSES)),
        ("AdapterName", ctypes.c_char_p),
        ("FirstUnicastAddress", ctypes.POINTER(_IP_ADAPTER_UNICAST_ADDRESS)),
        ("FirstAnycastAddress", ctypes.c_void_p),
        ("FirstMulticastAddress", ctypes.c_void_p),
        ("FirstDnsServerAddress", ctypes.c_void_p),
        ("DnsSuffix", ctypes.c_wchar_p),
        ("Description", ctypes.c_wchar_p),
        ("FriendlyName", ctypes.c_wchar_p),
        ("PhysicalAddress", ctypes.c_ubyte * MAX_ADAPTER_ADDRESS_LENGTH),
        ("PhysicalAddressLength", wintypes.ULONG),
        ("Flags", wintypes.DWORD),
        ("Mtu", wintypes.DWORD),
        ("IfType", wintypes.DWORD),
        ("OperStatus", wintypes.INT),
        ("Ipv6IfIndex", wintypes.DWORD),
        ("ZoneIndices", wintypes.ULONG * 16),
        ("FirstPrefix", ctypes.c_void_p),
        ("TransmitLinkSpeed", ctypes.c_ulonglong),
        ("ReceiveLinkSpeed", ctypes.c_ulonglong),
        ("FirstWinsServerAddress", ctypes.c_void_p),
        ("FirstGatewayAddress", ctypes.c_void_p),
        ("Ipv4Metric", wintypes.ULONG),
        ("Ipv6Metric", wintypes.ULONG),
        ("Luid", ctypes.c_ulonglong),
        ("Dhcpv4Server", _SOCKET_ADDRESS),
        ("CompartmentId", wintypes.ULONG),
        ("NetworkGuid", ctypes.c_ubyte * 16),
        ("ConnectionType", wintypes.INT),
        ("TunnelType", wintypes.INT),
        ("Dhcpv6Server", _SOCKET_ADDRESS),
        ("Dhcpv6ClientDuid", ctypes.c_ubyte * 130),
        ("Dhcpv6ClientDuidLength", wintypes.ULONG),
        ("Dhcpv6Iaid", wintypes.ULONG),
        ("FirstDnsSuffix", ctypes.c_void_p),
    ]

    _OPER_STATUS = {
        1: "Up", 2: "Down", 3: "Testing", 4: "Unknown",
        5: "Dormant", 6: "Not Present", 7: "Lower Layer Down",
    }

    try:
        iphlpapi = ctypes.windll.iphlpapi
        size = wintypes.ULONG(0)
        # 第一次探测调用会返回 ERROR_BUFFER_OVERFLOW(111) 并填充 size，属预期。
        ret = iphlpapi.GetAdaptersAddresses(
            AF_UNSPEC, GAA_FLAG_INCLUDE_PREFIX, None, None, ctypes.byref(size),
        )
        if ret not in (0, 111):
            _LOG.warning("rndis_windows_query_failed probe_ret=%s", ret)
            return []
        buf = ctypes.create_string_buffer(size.value)
        ret = iphlpapi.GetAdaptersAddresses(
            AF_UNSPEC, GAA_FLAG_INCLUDE_PREFIX, None, buf, ctypes.byref(size),
        )
        if ret != 0:
            _LOG.warning("rndis_windows_query_failed ret=%s", ret)
            return []
    except OSError as exc:
        _LOG.warning("rndis_windows_query_failed error=%s", exc)
        return []

    links: list[Dict[str, Any]] = []
    seen_adapter_ids: set[str] = set()
    adapter = ctypes.cast(buf, ctypes.POINTER(_IP_ADAPTER_ADDRESSES))
    while adapter:
        item = adapter.contents
        name = item.FriendlyName or ""
        description = item.Description or ""
        if not _is_rndis(name, description):
            adapter = item.Next
            continue
        if_index = item.IfIndex
        if not isinstance(if_index, int) or if_index <= 0:
            adapter = item.Next
            continue
        # 收集该网卡的 IPv4 地址
        addresses: list[str] = []
        unicast = item.FirstUnicastAddress
        while unicast:
            sockaddr = unicast.contents.Address
            if sockaddr.lpSockaddr:
                family = ctypes.cast(
                    sockaddr.lpSockaddr, ctypes.POINTER(ctypes.c_ushort),
                ).contents.value
                if family == socket.AF_INET:
                    addr_bytes = ctypes.string_at(sockaddr.lpSockaddr + 4, 4)
                    addresses.append(socket.inet_ntop(socket.AF_INET, addr_bytes))
            unicast = unicast.contents.Next
        observed = next(
            (str(value) for value in addresses if _is_usb_network_address(str(value))),
            "",
        )
        mac_bytes = bytes(item.PhysicalAddress[:item.PhysicalAddressLength])
        mac = "-".join(f"{b:02X}" for b in mac_bytes)
        adapter_id = str(mac or f"ifindex:{if_index}").replace("-", ":").lower()
        if adapter_id in seen_adapter_ids:
            adapter_id = f"{adapter_id}:if{if_index}"
            _LOG.warning("rndis_duplicate_adapter_identity base=%s assigned=%s",
                         mac or "", adapter_id)
        seen_adapter_ids.add(adapter_id)
        links.append({
            "iface": name or description or f"if{if_index}",
            "if_index": if_index,
            "adapter_id": adapter_id,
            "link_id": f"rndis:{adapter_id}",
            "observed_pc_ip": observed,
        })
        _LOG.info(
            "rndis_adapter iface=%s if_index=%s adapter_id=%s status=%s observed_pc_ip=%s",
            name or description or f"if{if_index}", if_index, adapter_id,
            _OPER_STATUS.get(item.OperStatus, str(item.OperStatus)), observed or "<none>",
        )
        adapter = item.Next
    return links


def auto_discover_devices() -> list[Dict[str, Any]]:
    """Discover each RNDIS link independently and keep a stable per-link identity."""
    with _link_state_lock:
        links: list[Dict[str, Any]] = []
        if sys.platform.startswith("linux"):
            try:
                result = subprocess.run(
                    ["ip", "-o", "-4", "addr", "show"],
                    capture_output=True,
                    text=True,
                    timeout=3,
                    check=False,
                )
                for line in result.stdout.splitlines():
                    match = re.search(r"^\d+:\s+(\S+)\s+inet\s+(\S+)/\d+", line)
                    if match:
                        iface, address = match.group(1), match.group(2)
                        if _is_rndis(iface):
                            links.append({
                                "iface": iface,
                                "if_index": 0,
                                "adapter_id": iface,
                                "link_id": f"linux:{iface}",
                                "observed_pc_ip": address,
                            })
            except (FileNotFoundError, subprocess.TimeoutExpired, OSError):
                pass
        elif sys.platform == "win32":
            links = _windows_links()
        assigned = _assign_link_addresses(links)
        _LOG.info(
            "rndis_discovery_complete platform=%s links=%s assigned=%s",
            sys.platform, len(links),
            [(item.get("link_id"), item.get("pc_ip"), item.get("device_ip"), item.get("configured"))
             for item in assigned],
        )
        return assigned


def reset_link_route_cache(link: Dict[str, Any]) -> None:
    """Compatibility hook; PC-side routing is managed manually by the user."""


def prepare_link_for_use(link: Dict[str, Any]) -> None:
    """Do not modify PC IP addresses or routes before communicating with a device."""


def _require_success(reply: Dict[str, Any], operation: str) -> Dict[str, Any]:
    if reply.get("code") != 0:
        raise RemoteCommandError(reply, f"{operation} failed")
    return reply


def _request(
    host: str,
    payload: Dict[str, Any],
    *,
    source_ip: str | None,
    port: int,
    timeout: float,
    source_if_index: int | None = None,
    cancel_event: threading.Event | None = None,
) -> Dict[str, Any]:
    _acquire_cancellable_lock(_request_lock, cancel_event)
    lock_acquired = True
    try:
        with TestAgentClient(
            host, port, timeout=timeout, source_host=source_ip,
            source_if_index=source_if_index,
            cancel_event=cancel_event,
        ) as client:
            return _require_success(client.request(payload), str(payload.get("cmd", "request")))
    finally:
        if lock_acquired:
            _request_lock.release()
        if cancel_event is not None:
            cancel_event.wait(_SOCKET_SETTLE_SECONDS)
        else:
            time.sleep(_SOCKET_SETTLE_SECONDS)


def request_case_status(
    device_ip: str,
    source_ip: str | None = None,
    *,
    port: int = DEFAULT_PORT,
    timeout: float = DEFAULT_TIMEOUT,
    source_if_index: int | None = None,
) -> Dict[str, Any]:
    """Read the status snapshot without treating a transient UI outage as a network failure."""
    try:
        with _request_lock, TestAgentClient(
            device_ip, port, timeout=timeout, source_host=source_ip,
            source_if_index=source_if_index,
        ) as client:
            return client.request({"cmd": "get_case_status"})
    finally:
        time.sleep(_SOCKET_SETTLE_SECONDS)


def set_device_ip(
    device_ip: str,
    new_ip: str,
    source_ip: str | None = None,
    timeout: float = DEFAULT_TIMEOUT,
    port: int = DEFAULT_PORT,
    source_if_index: int | None = None,
) -> Dict[str, Any]:
    return _request(
        device_ip,
        {"cmd": "set_ip", "ip": new_ip},
        source_ip=source_ip,
        port=port,
        timeout=timeout,
        source_if_index=source_if_index,
    )


def _set_device_ip_and_verify(
    current_ip: str,
    target_ip: str,
    pc_ip: str,
    source_if_index: int | None = None,
) -> tuple[Dict[str, Any], bool]:
    """Treat a lost set_ip response as success only after the target answers."""
    try:
        set_device_ip(
            current_ip, target_ip, source_ip=pc_ip,
            timeout=DEFAULT_TIMEOUT, port=DEFAULT_PORT,
            source_if_index=source_if_index,
        )
    except (OSError, TestAgentError, json.JSONDecodeError, socket.timeout) as set_error:
        try:
            return probe_device_with_reconnect(
                target_ip, pc_ip, retry_attempts=6, source_if_index=source_if_index,
            ), True
        except (OSError, TestAgentError, json.JSONDecodeError, socket.timeout):
            raise set_error
    return probe_device_with_reconnect(
        target_ip, pc_ip, retry_attempts=6, source_if_index=source_if_index,
    ), False


def probe_device_with_reconnect(
    device_ip: str,
    source_ip: str | None = None,
    *,
    port: int = DEFAULT_PORT,
    timeout: float = DEFAULT_TIMEOUT,
    retry_attempts: int = 6,
    retry_interval: float = 0.3,
    allow_default_reconfigure: bool = False,
    source_if_index: int | None = None,
    probe_payload: Dict[str, Any] | None = None,
    cancel_event: threading.Event | None = None,
) -> Dict[str, Any]:
    """Probe a persistent address; only explicit provisioning may use .2 fallback."""
    request_payload = dict(probe_payload or {"cmd": "agent_info", "include_catalog": False})
    if cancel_event is not None and cancel_event.is_set():
        raise TestAgentError("request cancelled")
    direct_error: Exception | None = None
    try:
        reply = _request(
            device_ip,
            request_payload,
            source_ip=source_ip,
            port=port,
            timeout=timeout,
            source_if_index=source_if_index,
            cancel_event=cancel_event,
        )
        reply["_connection_mode"] = "persistent"
        return reply
    except RemoteCommandError:
        # A valid device response is not a transport failure. Do not try the
        # default-address recovery path for a business-level rejection.
        raise
    except (OSError, TestAgentError, json.JSONDecodeError, socket.timeout) as exc:
        direct_error = exc

    if device_ip == DEFAULT_DEVICE_IP or not allow_default_reconfigure:
        raise TestAgentError(f"device is unreachable at {device_ip}: {direct_error}") from direct_error

    try:
        _request(
            DEFAULT_DEVICE_IP,
            request_payload,
            source_ip=source_ip,
            port=port,
            timeout=timeout,
            source_if_index=source_if_index,
            cancel_event=cancel_event,
        )
        set_device_ip(
            DEFAULT_DEVICE_IP,
            device_ip,
            source_ip=source_ip,
            timeout=timeout,
            port=port,
            source_if_index=source_if_index,
        )
    except RemoteCommandError:
        raise
    except (OSError, TestAgentError, json.JSONDecodeError, socket.timeout) as fallback_error:
        raise TestAgentError(
            f"device is unreachable at {device_ip} and fallback {DEFAULT_DEVICE_IP}: "
            f"{direct_error}; {fallback_error}"
        ) from fallback_error

    verify_error: Exception | None = None
    for attempt in range(max(1, retry_attempts)):
        if cancel_event is not None and cancel_event.is_set():
            raise TestAgentError("request cancelled")
        if attempt > 0:
            if cancel_event is not None and cancel_event.wait(max(0.0, retry_interval)):
                raise TestAgentError("request cancelled")
        try:
            reply = _request(
                device_ip,
                request_payload,
                source_ip=source_ip,
                port=port,
                timeout=timeout,
                source_if_index=source_if_index,
                cancel_event=cancel_event,
            )
            reply["_connection_mode"] = "fallback_reconfigured"
            return reply
        except RemoteCommandError:
            raise
        except (OSError, TestAgentError, json.JSONDecodeError, socket.timeout) as exc:
            verify_error = exc
    raise TestAgentError(f"device IP reconfiguration did not become reachable: {verify_error}") from verify_error


def request_device(
    device_ip: str,
    payload: Dict[str, Any],
    source_ip: str | None = None,
    *,
    port: int = DEFAULT_PORT,
    timeout: float = DEFAULT_TIMEOUT,
    link: Dict[str, Any] | None = None,
    cancel_event: threading.Event | None = None,
) -> Dict[str, Any]:
    """Ensure the target address is restored, then send one command exactly once."""
    operation_lock = _link_operation_lock(link)
    _acquire_cancellable_lock(operation_lock, cancel_event)
    try:
        if link is not None:
            prepare_link_for_use(link)
        source_if_index = _link_if_index(link)
        if payload.get("cmd") == "agent_info":
            return probe_device_with_reconnect(
                device_ip, source_ip, port=port, timeout=timeout,
                source_if_index=source_if_index, probe_payload=payload,
                cancel_event=cancel_event,
            )
        probe_device_with_reconnect(
            device_ip, source_ip, port=port, timeout=timeout,
            source_if_index=source_if_index,
            cancel_event=cancel_event,
        )
        return _request(
            device_ip, payload, source_ip=source_ip, port=port, timeout=timeout,
            source_if_index=source_if_index,
            cancel_event=cancel_event,
        )
    finally:
        operation_lock.release()


def configure_device(link: Dict[str, Any] | str) -> Dict[str, Any]:
    """Configure only the device peer using the PC address already set by the user."""
    if isinstance(link, str):
        link = next((item for item in auto_discover_devices() if item.get("pc_ip") == link), {
            "pc_ip": link,
            "device_ip": device_ip_for_pc(link),
            "adapter_id": link,
        })
    pc_ip = str(link.get("observed_pc_ip") or link.get("pc_ip", ""))
    result: Dict[str, Any] = {"pc_ip": pc_ip, "target_ip": "", "steps": []}
    if bool(link.get("ambiguous", False)):
        result["error"] = (
            "检测到多个 RNDIS 链路使用相同的 PC IP 或设备映射，无法安全配对；"
            "请给每个 RNDIS 网卡设置不同的 192.168.1.x 地址后重试"
        )
        result["success"] = False
        _LOG.error("rndis_config_rejected_ambiguous pc_ip=%s link=%s", pc_ip, link)
        return result
    if not _is_usb_network_address(pc_ip):
        result["error"] = "请先手动将 RNDIS 网卡配置为有效的 192.168.1.x 地址"
        result["success"] = False
        return result
    try:
        target_ip = device_ip_for_pc(pc_ip)
        result["target_ip"] = target_ip
    except ValueError:
        result["error"] = "当前 PC IP 无法映射到设备地址"
        result["success"] = False
        return result
    source_if_index = _link_if_index(link)
    operation_link = link if isinstance(link, dict) else None
    with _link_operation_lock(operation_link):
        try:
            # Pause the persistent status socket before changing this device's IP.
            # The local import avoids a module cycle with watch_hub -> device.
            from .watch_hub import suspend_watch_hub

            with suspend_watch_hub(target_ip, DEFAULT_PORT, pc_ip, source_if_index):
                with _link_state_lock:
                    state = _load_link_state()
                known_ips: list[str] = [target_ip]
                previous_device_ip = str(link.get("previous_device_ip", ""))
                if _is_usb_network_address(previous_device_ip) and previous_device_ip != target_ip:
                    known_ips.append(previous_device_ip)
                for saved in state.values():
                    addresses = _saved_link_addresses(saved)
                    if (addresses is not None and addresses[0] == pc_ip and
                            bool(saved.get("configured", False)) and addresses[1] not in known_ips):
                        known_ips.append(addresses[1])

                reply: Dict[str, Any] | None = None
                for candidate_ip in known_ips:
                    try:
                        reply = probe_device_with_reconnect(
                            candidate_ip, pc_ip, retry_attempts=2,
                            source_if_index=source_if_index,
                        )
                    except (OSError, TestAgentError, json.JSONDecodeError, socket.timeout):
                        continue
                    result["steps"].append({
                        "action": "restore_persisted_device_ip",
                        "ip": candidate_ip,
                        "reply": reply,
                    })
                    if candidate_ip != target_ip:
                        reply, response_lost = _set_device_ip_and_verify(
                            candidate_ip, target_ip, pc_ip, source_if_index,
                        )
                        result["steps"].append({
                            "action": "set_device_ip", "ip": target_ip,
                            "response_lost": response_lost,
                        })
                        result["steps"].append({
                            "action": "probe", "mode": "forced_pair", "reply": reply,
                        })
                    break

                if reply is None:
                    if sys.platform == "win32" and source_if_index is None:
                        raise TestAgentError("RNDIS 链路缺少接口索引，无法安全配对默认设备地址")
                    result["steps"].append({"action": "bootstrap_route", "ip": DEFAULT_DEVICE_IP})
                    _request(
                        DEFAULT_DEVICE_IP, {"cmd": "agent_info"}, source_ip=pc_ip,
                        port=DEFAULT_PORT, timeout=DEFAULT_TIMEOUT,
                        source_if_index=source_if_index,
                    )
                    reply, response_lost = _set_device_ip_and_verify(
                        DEFAULT_DEVICE_IP, target_ip, pc_ip, source_if_index,
                    )
                    result["steps"].append({
                        "action": "set_device_ip", "ip": target_ip,
                        "response_lost": response_lost,
                    })
                    result["steps"].append({
                        "action": "probe", "mode": "first_provision", "reply": reply,
                    })
                identity = str(link.get("adapter_id", ""))
                if identity:
                    with _link_state_lock:
                        state[identity] = {
                            "pc_ip": pc_ip,
                            "device_ip": target_ip,
                            "configured": True,
                        }
                        _save_link_state(state)
                result["success"] = True
        except (OSError, TestAgentError, json.JSONDecodeError, socket.timeout) as exc:
            result["steps"].append({"action": "probe", "error": str(exc)})
            result["error"] = str(exc)
            result["success"] = False
    return result


def reboot_device(
    device_ip: str,
    source_ip: str | None = None,
    timeout: float = DEFAULT_TIMEOUT,
    port: int = DEFAULT_PORT,
    link: Dict[str, Any] | None = None,
) -> Dict[str, Any]:
    return request_device(
        device_ip,
        {"cmd": "reboot"},
        source_ip,
        port=port,
        link=link,
        timeout=timeout,
    )
