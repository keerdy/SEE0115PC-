"""Paths that remain valid both from source and a PyInstaller bundle."""

from __future__ import annotations

import os
import sys
from pathlib import Path


APP_NAME = "PocketTestAgent"


def application_dir() -> Path:
    """Return the folder containing packaged resources or the source tree."""
    if getattr(sys, "frozen", False):
        meipass = getattr(sys, "_MEIPASS", None)
        if meipass:
            return Path(meipass)
        return Path(sys.executable).resolve().parent
    return Path(__file__).resolve().parent.parent


def resource_path(*parts: str) -> str:
    return str(application_dir().joinpath(*parts))


def install_dir() -> Path:
    """Return the directory holding the executable (frozen) or project root (source).

    Used as the base for writable runtime data (logs, reports, link state) so
    users can find artifacts next to the app (e.g. D:\\log\\Pocket TestAgent\\
    data\\logs\\app.log) instead of buried in %LOCALAPPDATA%.
    """
    if getattr(sys, "frozen", False):
        return Path(sys.executable).resolve().parent
    return Path(__file__).resolve().parent.parent


def runtime_data_dir() -> str:
    """Return a writable location for logs, reports, and link state.

    Placed under the install directory so users can find artifacts next to the
    app. Falls back to %LOCALAPPDATA% only if the install directory is not
    writable (e.g. Program Files without elevation).
    """
    path = install_dir() / "data"
    try:
        path.mkdir(parents=True, exist_ok=True)
        return str(path)
    except OSError:
        # Install dir not writable (e.g. C:\Program Files without admin).
        # Fall back to per-user LOCALAPPDATA so the app still works.
        root = os.environ.get("LOCALAPPDATA") or os.environ.get("APPDATA")
        base = Path(root) if root else Path.home() / ".local" / "share"
        fallback = base / APP_NAME
        try:
            fallback.mkdir(parents=True, exist_ok=True)
        except OSError:
            fallback = Path.home() / APP_NAME
            fallback.mkdir(parents=True, exist_ok=True)
        return str(fallback)


def defects_dir(device_ip: str | None = None) -> str:
    path = Path(runtime_data_dir()) / "defects"
    if device_ip:
        path /= device_ip
    path.mkdir(parents=True, exist_ok=True)
    return str(path)


def crash_dir(device_ip: str | None = None) -> str:
    path = Path(runtime_data_dir()) / "pocket_crash"
    if device_ip:
        path /= device_ip
    path.mkdir(parents=True, exist_ok=True)
    return str(path)


def logs_dir() -> str:
    path = Path(runtime_data_dir()) / "logs"
    path.mkdir(parents=True, exist_ok=True)
    return str(path)


def custom_profiles_path() -> str:
    """Return the PC-local file that stores reusable custom-test schemes."""
    return str(Path(runtime_data_dir()) / "custom_test_profiles.json")
