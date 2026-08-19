"""Read-only export of device crash evidence over the existing anonymous FTP service."""

from __future__ import annotations

from datetime import datetime
import ftplib
from pathlib import Path
from typing import Callable

from .app_paths import crash_dir
from .deployment import ftp_download_file, ftp_list_files, ftp_safe_basename


CRASH_BINARIES = (
    "/customer/bin/prog_pocket_ui_controll",
    "/customer/bin/prog_sv_stream_demo",
)


def _export_folder(device_ip: str, suite: str, case_id: int) -> Path:
    root = Path(crash_dir(device_ip))
    stem = f"{suite}_case{case_id:02d}_{datetime.now().strftime('%Y%m%d_%H%M%S')}"
    folder = root / stem
    suffix = 2
    while folder.exists():
        folder = root / f"{stem}_{suffix}"
        suffix += 1
    folder.mkdir(parents=True)
    return folder


def export_crash_files(
    device_ip: str,
    source_ip: str | None,
    suite: str,
    case_id: int,
    *,
    source_if_index: int | None = None,
    timeout: float = 60.0,
    progress: Callable[[str], None] | None = None,
) -> dict[str, object]:
    """Export all core files and known UI binaries without changing the device."""
    folder = _export_folder(device_ip, suite, case_id)
    downloaded: list[str] = []
    missing: list[str] = []

    core_files = [
        name if name.startswith("/") else f"/tmp/{name}"
        for name in ftp_list_files(
            device_ip, "/tmp", source_ip=source_ip,
            source_if_index=source_if_index, timeout=timeout,
        )
        if ftp_safe_basename(name).startswith("core.")
    ]
    remote_files = [*core_files, *CRASH_BINARIES]
    for remote_path in remote_files:
        filename = ftp_safe_basename(remote_path)
        local_path = folder / filename
        if progress is not None:
            progress(f"正在导出: {remote_path}")
        try:
            ftp_download_file(
                device_ip,
                remote_path,
                str(local_path),
                source_ip=source_ip,
                source_if_index=source_if_index,
                timeout=timeout,
            )
        except ftplib.error_perm as exc:
            missing.append(f"{remote_path}: {exc}")
            if progress is not None:
                progress(f"已跳过: {remote_path} ({exc})")
            continue
        downloaded.append(str(local_path))

    if progress is not None and not core_files:
        progress("未发现 /tmp/core.* 崩溃文件")
    return {
        "directory": str(folder),
        "downloaded": downloaded,
        "missing": missing,
        "core_count": len(core_files),
    }
