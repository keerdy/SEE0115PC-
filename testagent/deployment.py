"""FTP helpers used by the PC-side OTA workflow."""

from __future__ import annotations

import ftplib
import os
from pathlib import Path, PurePosixPath
import socket
import time
from collections.abc import Callable, Iterable
from typing import Any, Dict

from .protocol import TestAgentError, create_bound_socket, _request_lock, _SOCKET_SETTLE_SECONDS
from .app_logging import get_logger


_LOG = get_logger()


FTP_PORT = 21
FTP_USER = "anonymous"
FTP_PASS = "anonymous@"
# 大文件传输用 1 MiB 块：ftplib 默认 8192 会让 46MB 固件走 5600+ 次 sendall，
# 每次都有 FTP/TCP 协议开销，RNDIS 链路上吞吐严重下降。
_FTP_BLOCKSIZE = 1024 * 1024


class _FTP(ftplib.FTP):
    """ftplib.FTP that sets TCP_NODELAY on data sockets.

    Nagle's algorithm delays small writes on the data socket; disabling it
    removes the extra latency on RNDIS links during uploads/downloads.
    """

    def ntransfercmd(self, cmd: str, rest=None):
        conn, size = super().ntransfercmd(cmd, rest)
        try:
            conn.setsockopt(socket.IPPROTO_TCP, socket.TCP_NODELAY, 1)
        except OSError:
            pass
        return conn, size


def _connect_ftp(
    device_ip: str,
    source_ip: str | None,
    timeout: float,
    source_if_index: int | None = None,
) -> ftplib.FTP:
    ftp = _FTP()
    try:
        if source_ip and source_if_index:
            sock = create_bound_socket(
                source_ip, device_ip, FTP_PORT, timeout, source_if_index,
            )
            ftp.sock = sock
            ftp.af = sock.family
            ftp.file = sock.makefile("r", encoding=ftp.encoding)
            ftp.welcome = ftp.getresp()
        else:
            ftp.connect(
                device_ip,
                FTP_PORT,
                timeout=timeout,
                source_address=(source_ip, 0) if source_ip else None,
            )
        ftp.login(FTP_USER, FTP_PASS)
        return ftp
    except Exception:
        ftp.close()
        raise


def _close_ftp(ftp: ftplib.FTP) -> None:
    try:
        ftp.quit()
    except (ftplib.Error, OSError):
        try:
            ftp.close()
        except OSError:
            pass
    time.sleep(_SOCKET_SETTLE_SECONDS)


def ftp_upload_file(
    device_ip: str,
    local_path: str,
    remote_path: str,
    *,
    source_ip: str | None = None,
    source_if_index: int | None = None,
    timeout: float = 60.0,
    callback: Callable[[bytes], None] | None = None,
) -> None:
    if not os.path.isfile(local_path):
        raise TestAgentError(f"missing {local_path}")
    with _request_lock:
        ftp = _connect_ftp(device_ip, source_ip, timeout, source_if_index)
        try:
            remote_dir = remote_path.rsplit("/", 1)[0]
            if remote_dir:
                try:
                    ftp.mkd(remote_dir)
                except ftplib.error_perm:
                    pass
            with open(local_path, "rb") as source:
                try:
                    ftp.delete(remote_path)
                except ftplib.error_perm:
                    pass
                ftp.storbinary(f"STOR {remote_path}", source, callback=callback, blocksize=_FTP_BLOCKSIZE)
        finally:
            _close_ftp(ftp)


def ftp_list_files(
    device_ip: str,
    remote_dir: str,
    *,
    source_ip: str | None = None,
    source_if_index: int | None = None,
    timeout: float = 60.0,
) -> list[str]:
    with _request_lock:
        ftp = _connect_ftp(device_ip, source_ip, timeout, source_if_index)
        try:
            try:
                return ftp.nlst(remote_dir)
            except ftplib.error_perm as exc:
                if "550" in str(exc):
                    return []
                raise
        finally:
            _close_ftp(ftp)


def ftp_download_file(
    device_ip: str,
    remote_path: str,
    local_path: str,
    *,
    source_ip: str | None = None,
    source_if_index: int | None = None,
    timeout: float = 60.0,
    callback: Callable[[bytes], None] | None = None,
) -> None:
    destination = Path(local_path)
    destination.parent.mkdir(parents=True, exist_ok=True)
    partial_path = destination.with_name(f"{destination.name}.part")
    with _request_lock:
        ftp = _connect_ftp(device_ip, source_ip, timeout, source_if_index)
        try:
            with open(partial_path, "wb") as output:
                def write_block(block: bytes) -> None:
                    output.write(block)
                    if callback is not None:
                        callback(block)

                ftp.retrbinary(f"RETR {remote_path}", write_block, blocksize=_FTP_BLOCKSIZE)
            os.replace(partial_path, destination)
        except Exception:
            try:
                partial_path.unlink()
            except FileNotFoundError:
                pass
            raise
        finally:
            _close_ftp(ftp)


def ftp_safe_basename(remote_path: str) -> str:
    name = PurePosixPath(remote_path).name
    if not name or name in {".", ".."}:
        raise TestAgentError(f"invalid remote filename: {remote_path}")
    return name


def ftp_delete_bin_files(
    device_ip: str,
    remote_dir: str,
    *,
    source_ip: str | None = None,
    source_if_index: int | None = None,
    timeout: float = 60.0,
) -> list[str]:
    """Delete previous OTA .bin files so the device sees only the new image."""
    with _request_lock:
        ftp = _connect_ftp(device_ip, source_ip, timeout, source_if_index)
        try:
            try:
                names = ftp.nlst(remote_dir)
            except ftplib.error_perm as exc:
                # A missing firmware directory is already clean.
                if "550" in str(exc):
                    return []
                raise

            deleted: list[str] = []
            failed: list[str] = []
            for name in names:
                if not name.lower().endswith(".bin"):
                    continue
                remote_path = name if name.startswith("/") else f"{remote_dir}/{name}"
                try:
                    ftp.delete(remote_path)
                    deleted.append(remote_path)
                except (ftplib.error_perm, ftplib.Error, OSError) as exc:
                    failed.append(remote_path)
                    _LOG.warning("ftp_delete_failed path=%s error=%s", remote_path, exc)
            if failed:
                _LOG.warning("ftp_delete_bin_files_partial deleted=%s failed=%s", deleted, failed)
            return deleted
        finally:
            _close_ftp(ftp)


def ftp_list_bin_files(
    device_ip: str,
    remote_dir: str,
    *,
    source_ip: str | None = None,
    source_if_index: int | None = None,
    timeout: float = 60.0,
) -> list[str]:
    """List previous OTA .bin files without deleting them.

    Used to detect stale firmware and prompt the user to delete manually.
    """
    with _request_lock:
        ftp = _connect_ftp(device_ip, source_ip, timeout, source_if_index)
        try:
            try:
                names = ftp.nlst(remote_dir)
            except ftplib.error_perm as exc:
                if "550" in str(exc):
                    return []
                raise
            return [
                name if name.startswith("/") else f"{remote_dir}/{name}"
                for name in names
                if name.lower().endswith(".bin")
            ]
        finally:
            _close_ftp(ftp)


def first_reachable_ftp_host(
    hosts: Iterable[str],
    *,
    source_ip: str | None = None,
    source_if_index: int | None = None,
    timeout: float = 5.0,
) -> str | None:
    for host in dict.fromkeys(hosts):
        try:
            with _request_lock:
                ftp = _connect_ftp(host, source_ip, timeout, source_if_index)
            _close_ftp(ftp)
            return host
        except (ftplib.Error, OSError):
            continue
    return None


def wait_for_ftp_host(
    hosts: Iterable[str],
    *,
    source_ip: str | None = None,
    source_if_index: int | None = None,
    wait_timeout: float,
    poll_interval: float = 5.0,
) -> str:
    deadline = time.monotonic() + wait_timeout
    while time.monotonic() < deadline:
        host = first_reachable_ftp_host(
            hosts, source_ip=source_ip, source_if_index=source_if_index,
        )
        if host is not None:
            return host
        time.sleep(max(0.1, poll_interval))
    raise TestAgentError("waiting for device FTP recovery timed out")


def ftp_clean_all(
    device_ip: str,
    *,
    source_ip: str | None = None,
    source_if_index: int | None = None,
    timeout: float = 60.0,
) -> list[str]:
    """Delete everything under /sdcard (including firmware/).

    Returns a list of deleted paths (files and directories removed).
    """
    base = "/sdcard"
    with _request_lock:
        ftp = _connect_ftp(device_ip, source_ip, timeout, source_if_index)
        try:
            deleted = _ftp_recursive_delete(ftp, base, protect=set())
        finally:
            _close_ftp(ftp)
    return deleted


def _ftp_recursive_delete(
    ftp: ftplib.FTP,
    root: str,
    protect: set[str],
    depth: int = 0,
) -> list[str]:
    deleted: list[str] = []
    try:
        names = ftp.nlst(root)
    except ftplib.error_perm:
        return deleted  # directory does not exist or is empty

    for name in names:
        basename = PurePosixPath(name).name
        full_path = f"{root}/{basename}"
        if not basename or basename in {".", ".."}:
            continue
        if depth == 0 and basename in protect:
            continue
        # Determine whether name is a file or directory by trying cwd
        is_dir = False
        try:
            ftp.cwd(full_path)
            ftp.cwd(root)
            is_dir = True
        except ftplib.error_perm:
            pass
        if is_dir:
            deleted.extend(_ftp_recursive_delete(ftp, full_path, set(), depth + 1))
            try:
                ftp.rmd(full_path)
                deleted.append(full_path)
            except (ftplib.error_perm, ftplib.Error, OSError) as exc:
                _LOG.warning("ftp_rmd_failed path=%s error=%s", full_path, exc)
        else:
            try:
                ftp.delete(full_path)
                deleted.append(full_path)
            except (ftplib.error_perm, ftplib.Error, OSError) as exc:
                _LOG.warning("ftp_delete_failed path=%s error=%s", full_path, exc)
    return deleted
