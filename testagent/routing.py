"""Windows RNDIS per-link IPv4 provisioning helpers."""

from __future__ import annotations

import ctypes
import ipaddress
import subprocess
import sys
from typing import Any


class RoutingError(RuntimeError):
    pass


def _subprocess_kwargs() -> dict[str, Any]:
    if sys.platform == "win32":
        return {"creationflags": getattr(subprocess, "CREATE_NO_WINDOW", 0)}
    return {}


def is_windows_admin() -> bool:
    if sys.platform != "win32":
        return False
    try:
        return bool(ctypes.windll.shell32.IsUserAnAdmin())
    except (AttributeError, OSError):
        return False


def require_windows_admin() -> None:
    if sys.platform != "win32":
        raise RoutingError("per-link RNDIS provisioning is currently supported on Windows only")
    if not is_windows_admin():
        raise RoutingError("请使用管理员身份运行 Pocket TestAgent 后再配置多设备网络")


def _validate_ipv4(address: str) -> str:
    try:
        return str(ipaddress.IPv4Address(address))
    except ipaddress.AddressValueError as exc:
        raise RoutingError(f"invalid IPv4 address: {address}") from exc


def _run_powershell(script: str) -> None:
    try:
        result = subprocess.run(
            ["powershell", "-NoProfile", "-NonInteractive", "-Command", script],
            capture_output=True,
            text=True,
            timeout=15,
            check=False,
            **_subprocess_kwargs(),
        )
    except (FileNotFoundError, subprocess.TimeoutExpired, OSError) as exc:
        raise RoutingError(f"无法执行 Windows 网络配置命令: {exc}") from exc
    if result.returncode != 0:
        detail = (result.stderr or result.stdout).strip() or "unknown PowerShell error"
        raise RoutingError(detail)


def ensure_interface_address(if_index: int, address: str) -> None:
    """Assign one active /24 address to a selected RNDIS interface, idempotently."""
    address = _validate_ipv4(address)
    if if_index <= 0:
        raise RoutingError("invalid Windows interface index")
    _run_powershell(
        "$ErrorActionPreference='Stop'; "
        f"$existing=Get-NetIPAddress -InterfaceIndex {if_index} -AddressFamily IPv4 "
        f"-ErrorAction SilentlyContinue | Where-Object {{$_.IPAddress -eq '{address}'}}; "
        f"if (-not $existing) {{New-NetIPAddress -InterfaceIndex {if_index} -IPAddress '{address}' "
        "-PrefixLength 24 -PolicyStore ActiveStore | Out-Null}"
    )


def ensure_host_route(destination: str, if_index: int) -> None:
    """Install one active on-link /32 route bound to the selected RNDIS interface."""
    destination = _validate_ipv4(destination)
    if if_index <= 0:
        raise RoutingError("invalid Windows interface index")
    prefix = f"{destination}/32"
    _run_powershell(
        "$ErrorActionPreference='Stop'; "
        f"$existing=Get-NetRoute -InterfaceIndex {if_index} -DestinationPrefix '{prefix}' "
        "-PolicyStore ActiveStore -ErrorAction SilentlyContinue; "
        f"if (-not $existing) {{New-NetRoute -InterfaceIndex {if_index} -DestinationPrefix '{prefix}' "
        "-NextHop '0.0.0.0' -RouteMetric 5 -PolicyStore ActiveStore | Out-Null}"
    )


def remove_host_route(destination: str, if_index: int) -> None:
    destination = _validate_ipv4(destination)
    if if_index <= 0:
        raise RoutingError("invalid Windows interface index")
    prefix = f"{destination}/32"
    _run_powershell(
        "$ErrorActionPreference='Stop'; "
        f"Get-NetRoute -InterfaceIndex {if_index} -DestinationPrefix '{prefix}' "
        "-PolicyStore ActiveStore -ErrorAction SilentlyContinue | "
        "Remove-NetRoute -Confirm:$false -ErrorAction SilentlyContinue"
    )


def prepare_configured_link(link: dict[str, Any]) -> None:
    """Restore the PC address and /32 route for one known RNDIS link.

    This never sends a command to the device or changes its address.  It is
    therefore safe to call before a normal probe after an RNDIS reconnect.
    """
    if sys.platform != "win32":
        return
    if_index = link.get("if_index")
    pc_ip = str(link.get("pc_ip", ""))
    device_ip = str(link.get("device_ip", ""))
    if not isinstance(if_index, int) or if_index <= 0 or not pc_ip or not device_ip:
        raise RoutingError("RNDIS 链路缺少接口索引或 IP，无法恢复主机路由")
    require_windows_admin()
    ensure_interface_address(if_index, pc_ip)
    ensure_host_route(device_ip, if_index)
