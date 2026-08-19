"""Shared Pocket TestAgent PC-side protocol and model helpers."""

from .catalog import FALLBACK_CASES, catalog_from_agent_info, case_descriptor, case_titles
from .device import (
    auto_discover_devices,
    configure_device,
    device_ip_for_pc,
    probe_device_with_reconnect,
    reboot_device,
    request_device,
    set_device_ip,
)
from .protocol import (
    DEFAULT_HOST,
    DEFAULT_PORT,
    DEFAULT_TIMEOUT,
    RemoteCommandError,
    TestAgentClient,
    TestAgentError,
)
from .status import extract_progress, is_terminal_status

__all__ = [
    "DEFAULT_HOST",
    "DEFAULT_PORT",
    "DEFAULT_TIMEOUT",
    "FALLBACK_CASES",
    "RemoteCommandError",
    "TestAgentClient",
    "TestAgentError",
    "auto_discover_devices",
    "catalog_from_agent_info",
    "case_descriptor",
    "case_titles",
    "configure_device",
    "device_ip_for_pc",
    "extract_progress",
    "is_terminal_status",
    "probe_device_with_reconnect",
    "reboot_device",
    "request_device",
    "set_device_ip",
]
