#!/usr/bin/env python3
"""Multi-device PySide6 dashboard for Pocket TestAgent."""

from __future__ import annotations

from dataclasses import dataclass, field
import faulthandler
import ipaddress
import json
import os
import queue
import re
import socket
import subprocess
import sys
import threading
import time
from typing import Any, Dict, Mapping

from testagent_client import (
    DEFAULT_PORT,
    TestAgentClient,
    classify_defect,
    extract_progress,
    generate_report,
    is_ambiguous_start_reply,
    is_current_case_running,
    is_terminal_status,
    make_run_command,
    make_record_path,
    summarize_record,
)
from testagent.catalog import (
    FALLBACK_CASES,
    catalog_from_agent_info,
    case_descriptor,
)
from testagent.custom_config import (
    ACTION_CLICK,
    ACTION_LABELS,
    ACTION_MODE,
    ACTION_NAV,
    ACTION_NAV_TARGET,
    ACTION_PHOTO_CAPTURE,
    ACTION_SCREEN_ROTATE,
    ACTION_SLOW_MOTION_RECORD,
    ACTION_SLIDER,
    ACTION_SWIPE,
    ACTION_VERIFY,
    ACTION_VIDEO,
    ACTION_VIDEO_RECORD,
    CHECK_BASELINE,
    CHECK_FILE,
    CHECK_PLAYBACK,
    CHECK_PLAYBACK_DAMAGE,
    CUSTOM_CONFIG_VERSION,
    CUSTOM_POLICY_VERSION,
    PAGE_WAIT_ADDITIONAL,
    PAGE_WAIT_AUTO,
    VIDEO_CANVAS_LANDSCAPE,
    VIDEO_CANVAS_PORTRAIT,
    CustomCapabilities,
    CustomConfig,
    CustomConfigError,
    CustomStep,
    custom_c01_compatibility_media_interval_ms,
    custom_c01_estimated_runtime_ms,
    custom_c01_media_artifact_counts,
    custom_c01_max_tracked_media_artifacts,
    custom_c01_storage_budget_bytes,
    custom_c01_monitor_timeout_seconds,
    custom_config_error_text,
    is_config_revision_conflict,
    make_get_custom_config_payload,
    make_set_custom_config_payload,
    saved_config_from_reply,
    validate_config,
)
from testagent.deployment import (
    first_reachable_ftp_host,
    ftp_clean_all,
    ftp_delete_bin_files,
    ftp_list_bin_files,
    ftp_upload_file,
)
from testagent.app_paths import custom_profiles_path, defects_dir, logs_dir, resource_path
from testagent.app_logging import get_logger, install_logging
from testagent.custom_profiles import CustomProfile, CustomProfileStore
from testagent.crash_export import export_crash_files
from testagent.protocol import RemoteCommandError, _request_lock, _SOCKET_SETTLE_SECONDS, init_com_mta
from testagent.watch_hub import WatchSubscription, acquire_watch_subscription, purge_stale_hubs, suspend_all_watch_hubs, resume_all_watch_hubs
from testagent.device import (
    DEFAULT_DEVICE_IP,
    auto_discover_devices,
    configure_device,
    get_device_note,
    set_device_note,
    probe_device_with_reconnect,
    reset_link_route_cache,
    reboot_device,
    request_device,
)

try:
    from PySide6.QtCore import QObject, QThread, QTimer, QUrl, Qt, Signal, Slot
    from PySide6.QtGui import QColor, QDesktopServices, QIcon, QImage, QPixmap, QTransform
    from PySide6.QtWidgets import (
        QApplication,
        QAbstractItemView,
        QCheckBox,
        QComboBox,
        QDialog,
        QFileDialog,
        QFormLayout,
        QFrame,
        QGridLayout,
        QGroupBox,
        QHBoxLayout,
        QHeaderView,
        QInputDialog,
        QLabel,
        QLineEdit,
        QListWidget,
        QListWidgetItem,
        QMainWindow,
        QMessageBox,
        QPlainTextEdit,
        QProgressBar,
        QPushButton,
        QScrollArea,
        QSpinBox,
        QSplitter,
        QStackedWidget,
        QTableWidget,
        QTableWidgetItem,
        QVBoxLayout,
        QWidget,
    )
except ImportError as exc:  # pragma: no cover - Windows runtime dependency
    print(f"PySide6 is required: {exc}", file=sys.stderr)
    raise

from apptest_page import AppTestPage, OtgSection
from help_page import HelpPage


def _custom_capability_diagnostics(reply: Mapping[str, Any]) -> Dict[str, Any]:
    """Keep protocol/version evidence compact enough for app.log."""
    keys = sorted(str(key) for key in reply.keys())
    return {
        "code": reply.get("code"),
        "msg": reply.get("msg"),
        "config_version": reply.get("config_version"),
        "policy_version": reply.get("policy_version"),
        "media_manifest_supported": reply.get("media_manifest_supported"),
        "cleanup_supported": reply.get("cleanup_supported"),
        "page_target_ids": [
            target.get("target_id") for target in reply.get("page_targets", [])
            if isinstance(target, Mapping)
        ],
        "active_config_revision": reply.get("active_config_revision"),
        "active_config_crc": reply.get("active_config_crc"),
        "reason_code": reply.get("reason_code"),
        "response_keys": keys,
    }


def _custom_snapshot_diagnostics(snapshot: Mapping[str, Any] | None) -> Dict[str, Any]:
    """Keep the saved C01 metadata without duplicating the full test payload."""
    if not isinstance(snapshot, Mapping):
        return {}
    return {
        "config_version": snapshot.get("config_version"),
        "policy_version": snapshot.get("policy_version"),
        "media_manifest_supported": snapshot.get("media_manifest_supported"),
        "cleanup_supported": snapshot.get("cleanup_supported"),
        "firmware_version": snapshot.get("firmware_version"),
        "device_ip": snapshot.get("device_ip"),
        "config_revision": snapshot.get("config_revision"),
        "config_crc": snapshot.get("config_crc"),
    }


MAX_LOG_ENTRIES = 2000
MAX_QUEUE_EVENTS_PER_TICK = 32
CATALOG_PROBE_TIMEOUT = 15.0  # for full catalog enumeration (slow device-side op)
FAST_PROBE_TIMEOUT = 5.0      # for liveness check during refresh
STATUS_COLORS = {
    "finished": ("#166534", "#dcfce7"),
    "failed": ("#dc2626", "#fef2f2"),
    "error": ("#dc2626", "#fef2f2"),
    "stopped": ("#c2410c", "#fff7ed"),
    "running": ("#0369a1", "#e0f2fe"),
    "queued": ("#6d28d9", "#f3e8ff"),
}
SUITE_LABELS = {
    "stable_test": "Stable Test",
    "bug_test": "Bug Test",
    "stress_test": "压力测试",
    "custom_test": "自定义测试",
}
STRESS_TEST_ARTIFACTS = (
    "/tmp/pocket_ui_test/stress_test.log",
    "/tmp/pocket_ui_test/stress_test_report.txt",
)
CUSTOM_TEST_C01_REPORT_ARTIFACT = "/userdata/pocket_ui_test/custom_test/latest_report.txt"


_diagnostics_file = None
_APP_LOG = get_logger()


def enable_crash_diagnostics() -> None:
    """Write fatal Python tracebacks when a windowed bundle has no stderr."""
    global _diagnostics_file
    logger = install_logging()
    try:
        path = os.path.join(logs_dir(), "gui-crash.log")
        _diagnostics_file = open(path, "a", encoding="utf-8")
        faulthandler.enable(file=_diagnostics_file, all_threads=True)
        logger.info("faulthandler_enabled path=%s", path)
    except OSError:
        logger.exception("faulthandler_enable_failed")
        if sys.stderr is not None:
            faulthandler.enable(all_threads=True)


@dataclass
class DeviceRuntime:
    iface: str
    pc_ip: str
    device_ip: str
    link: Dict[str, Any] = field(default_factory=dict)
    configured: bool = False
    port: int = DEFAULT_PORT
    online: bool | None = None
    suite: str = "stable_test"
    case_id: int = 1
    status: str = "idle"
    progress_current: int = 0
    progress_total: int = 0
    error_code: int = 0
    started_at_ms: int = 0
    finished_at_ms: int = 0
    ui_status_unavailable: bool = False
    firmware_version: str = ""
    catalog_firmware_version: str = ""
    catalog_loaded: bool = False
    last_msg: str = "等待连接"
    catalog: Dict[str, list[Dict[str, Any]]] = field(
        default_factory=lambda: catalog_from_agent_info({})
    )
    command_thread: QThread | None = None
    command_worker: QObject | None = None
    watch_worker: "WatchWorker | None" = None
    record_worker: "RecordWorker | None" = None
    recording_start_pending: bool = False
    ui_bridge_unavailable: bool = False
    link_invalid: bool = False  # source_ip 失效（设备断开），暂停探活直到 refresh
    transport_error_streak: int = 0
    custom_config_revision: int = 0
    custom_config_crc: int | None = None
    custom_estimated_runtime_ms: int | None = None
    custom_config_snapshot: Dict[str, Any] | None = None
    notes: str = ""  # 用户备注

    @property
    def key(self) -> str:
        return str(self.link.get("link_id") or self.link.get("adapter_id") or self.pc_ip)


class CommandWorker(QObject):
    result = Signal(object)
    error = Signal(str)
    finished = Signal()

    def __init__(self, host: str, port: int, source_host: str, payload: Dict[str, Any],
                 link: Dict[str, Any] | None = None, timeout: float = 3.0):
        super().__init__()
        self.host = host
        self.port = port
        self.source_host = source_host
        self.payload = payload
        self.link = dict(link or {})
        self.timeout = timeout
        self._cancel_event = threading.Event()

    def cancel(self) -> None:
        """Request cancellation of the current bounded socket operation."""
        self._cancel_event.set()

    @Slot()
    def run(self) -> None:
        init_com_mta()
        _APP_LOG.info(
            "command_start host=%s source_ip=%s purpose=%s cmd=%s",
            self.host, self.source_host, self.payload.get("purpose", ""),
            self.payload.get("cmd", ""),
        )
        try:
            if self._cancel_event.is_set():
                return
            reply = request_device(
                self.host,
                self.payload,
                self.source_host,
                port=self.port,
                timeout=self.timeout,
                link=self.link,
                cancel_event=self._cancel_event,
            )
            if self._cancel_event.is_set():
                return
            _APP_LOG.info("command_result host=%s cmd=%s code=%s", self.host,
                          self.payload.get("cmd", ""), reply.get("code"))
            self.result.emit(reply)
        except RemoteCommandError as exc:
            if not self._cancel_event.is_set():
                _APP_LOG.info(
                    "command_remote_rejection host=%s cmd=%s code=%s",
                    self.host, self.payload.get("cmd", ""), exc.response.get("code"),
                )
                self.result.emit(exc.response)
        except Exception as exc:
            if self._cancel_event.is_set():
                _APP_LOG.info("command_cancelled host=%s cmd=%s", self.host, self.payload.get("cmd", ""))
            else:
                _APP_LOG.exception("command_error host=%s cmd=%s", self.host, self.payload.get("cmd", ""))
                self.error.emit(str(exc))
        finally:
            _APP_LOG.info("command_finished_emit cmd=%s", self.payload.get("cmd", ""))
            self.finished.emit()


class ConfigureWorker(QObject):
    result = Signal(object)
    error = Signal(str)
    finished = Signal()

    def __init__(self, link: Dict[str, Any]):
        super().__init__()
        self.link = dict(link)
        self._done = False

    @Slot()
    def run(self) -> None:
        init_com_mta()
        try:
            self.result.emit(configure_device(self.link))
        except Exception as exc:
            self.error.emit(str(exc))
        finally:
            self.finished.emit()


class DiscoveryWorker(QObject):
    result = Signal(object)
    error = Signal(str)
    finished = Signal()

    @Slot()
    def run(self) -> None:
        _APP_LOG.info("discovery_start")
        try:
            devices = auto_discover_devices()
            _APP_LOG.info("discovery_result links=%s", len(devices))
            self.result.emit(devices)
        except Exception as exc:
            _APP_LOG.exception("discovery_error")
            self.error.emit(str(exc))
        finally:
            self.finished.emit()


class BatchConfigureWorker(QObject):
    result = Signal(object)
    finished = Signal(object)

    def __init__(self, links: list[Dict[str, Any]]):
        super().__init__()
        self.links = [dict(link) for link in links]

    @Slot()
    def run(self) -> None:
        init_com_mta()
        results: list[Dict[str, Any]] = []
        for link in self.links:
            _APP_LOG.info("configure_start link=%s pc_ip=%s device_ip=%s",
                          link.get("link_id", ""), link.get("pc_ip", ""),
                          link.get("device_ip", ""))
            try:
                result = configure_device(link)
            except Exception as exc:
                _APP_LOG.exception("configure_error link=%s", link.get("link_id", ""))
                result = {"success": False, "error": str(exc)}
            _APP_LOG.info("configure_result link=%s success=%s error=%s",
                          link.get("link_id", ""), result.get("success", False),
                          result.get("error", ""))
            payload = {"device": link, "result": result}
            results.append(payload)
            self.result.emit(payload)
        self.finished.emit(results)


DEPLOY_FTP_IP = DEFAULT_DEVICE_IP


class OTAUpgradeWorker:
    """Upload firmware and wait for the Jenkins-built TestAgent image to recover."""

    def __init__(self, firmware_path: str, device_ip: str, link: Dict[str, Any]):
        self.firmware_path = firmware_path
        self.device_ip = device_ip
        self.link = dict(link)
        self.pc_ip = str(self.link.get("pc_ip", ""))
        self.source_if_index = int(self.link.get("if_index", 0) or 0) or None
        self._thread: threading.Thread | None = None
        self.queue: queue.Queue = queue.Queue()

    def start(self) -> None:
        self._thread = threading.Thread(target=self._run, daemon=True)
        self._thread.start()

    def _run(self) -> None:
        init_com_mta()
        try:
            if not os.path.isfile(self.firmware_path):
                self.queue.put(("error", "固件文件不存在: " + self.firmware_path))
                return
            fw_name = os.path.basename(self.firmware_path)
            self.queue.put(("step", f"固件文件: {fw_name}", "info"))

            ftp_host = first_reachable_ftp_host(
                (self.device_ip, DEPLOY_FTP_IP), source_ip=self.pc_ip,
                source_if_index=self.source_if_index, timeout=5,
            )
            if ftp_host is None:
                self.queue.put(("error", "无法通过 FTP 连接设备"))
                return
            self.queue.put(("step", f"FTP 连接成功 ({ftp_host})", "info"))
            remote_fw_dir = "/sdcard/firmware"
            self.queue.put(("step", "正在检测设备上的旧 .bin 固件...", "info"))
            existing_bins = ftp_list_bin_files(
                ftp_host,
                remote_fw_dir,
                source_ip=self.pc_ip,
                source_if_index=self.source_if_index,
            )
            if existing_bins:
                self.queue.put(("bin_conflict", existing_bins, self.firmware_path))
                return
            else:
                self.queue.put(("step", "设备上没有旧 .bin 固件", "info"))
            remote_fw = f"/sdcard/firmware/{fw_name}"
            fw_size = os.path.getsize(self.firmware_path)
            if fw_size <= 0:
                self.queue.put(("error", "固件文件为空"))
                return
            total_mb = fw_size / (1024 * 1024)
            self.queue.put(("step", f"正在上传固件 ({total_mb:.1f} MB)...", "info"))
            sent = [0]
            last_report_pct = [0]

            def progress_callback(chunk):
                sent[0] += len(chunk)
                pct = sent[0] * 100 // fw_size
                if pct >= last_report_pct[0] + 10 or pct == 100:
                    last_report_pct[0] = pct
                    cur_mb = sent[0] / (1024 * 1024)
                    self.queue.put(("step", f"上传进度: {cur_mb:.1f}/{total_mb:.1f} MB ({pct}%)", "info"))

            ftp_upload_file(
                ftp_host,
                self.firmware_path,
                remote_fw,
                source_ip=self.pc_ip,
                source_if_index=self.source_if_index,
                callback=progress_callback,
            )
            self.queue.put(("step", "固件上传完成", "info"))

            self.queue.put(("step", f"正在发送 OTA 升级命令到 {self.device_ip}:19099 ...", "info"))
            reply = request_device(
                self.device_ip,
                {"cmd": "ota_upgrade", "path": remote_fw},
                self.pc_ip,
                timeout=10,
                link=self.link,
            )
            self.queue.put(("step", f"OTA 命令回复: {json.dumps(reply, ensure_ascii=False)}", "info"))

            self.queue.put(("step", "设备正在升级并重启，等待设备重新上线并恢复网络配置...", "info"))
            self.queue.put(("need_reboot", self.link))
        except Exception as exc:
            self.queue.put(("error", str(exc)))
        finally:
            self.queue.put(("finished", self))

    def join(self, timeout: float) -> bool:
        if self._thread is None:
            return True
        self._thread.join(timeout)
        return not self._thread.is_alive()

    def is_running(self) -> bool:
        return self._thread is not None and self._thread.is_alive()


class OTAPostWorker:
    """Wait for the post-OTA reboot and restore this RNDIS link automatically."""

    def __init__(self, link: Dict[str, Any]):
        self.link = dict(link)
        self.expected_ip = str(self.link.get("device_ip", ""))
        self._thread: threading.Thread | None = None
        self.queue: queue.Queue = queue.Queue()

    def start(self) -> None:
        self._thread = threading.Thread(target=self._run, daemon=True)
        self._thread.start()

    def _run(self) -> None:
        init_com_mta()
        try:
            self.queue.put(("step", "等待设备重启并自动恢复网络配置...", "info"))
            deadline = time.monotonic() + 120
            while time.monotonic() < deadline:
                try:
                    result = configure_device(self.link)
                    if not result.get("success", False):
                        time.sleep(2)
                        continue
                    target_ip = str(result.get("target_ip", self.expected_ip))
                    self.queue.put(("step", f"设备已就绪: {target_ip}:19099", "info"))
                    self.queue.put(("result", {
                        "target_ip": target_ip,
                        "network_result": result,
                    }))
                    return
                except Exception:
                    time.sleep(2)
            self.queue.put(("error", "等待设备重启并恢复网络配置超时（2分钟）"))
        except Exception as exc:
            self.queue.put(("error", str(exc)))
        finally:
            self.queue.put(("finished_post", self))

    def join(self, timeout: float) -> bool:
        if self._thread is None:
            return True
        self._thread.join(timeout)
        return not self._thread.is_alive()

    def is_running(self) -> bool:
        return self._thread is not None and self._thread.is_alive()


class OTABatchWorker:
    """Serial upload firmware to all devices, then parallel wait for reboot."""

    def __init__(self, firmware_path: str, links: list[Dict[str, Any]]):
        self.firmware_path = firmware_path
        self.links = links
        self.queue: queue.Queue = queue.Queue()
        self._thread: threading.Thread | None = None
        self._uploaded = 0
        self._ready = 0
        self._failed: list[str] = []

    def start(self) -> None:
        self._thread = threading.Thread(target=self._run, daemon=True)
        self._thread.start()

    def is_running(self) -> bool:
        return self._thread is not None and self._thread.is_alive()

    def _run(self) -> None:
        total = len(self.links)
        fw_name = os.path.basename(self.firmware_path)
        fw_size = os.path.getsize(self.firmware_path)
        total_mb = fw_size / (1024 * 1024)
        post_workers: list[tuple[str, OTAPostWorker]] = []
        init_com_mta()
        # FTP 传输会长时间持有全局请求锁，暂停所有监视轮询避免线程阻塞等锁
        # 触发 Windows COM 重入崩溃 (0x8001010d)。
        suspend_all_watch_hubs()

        try:
            # Phase 1: Serial upload + OTA command per device
            for i, link in enumerate(self.links):
                device_ip = str(link.get("device_ip", ""))
                pc_ip = str(link.get("pc_ip", ""))
                si = int(link.get("if_index", 0) or 0) or None

                self.queue.put(("step", f"[{i + 1}/{total}] {device_ip}: 删除旧固件...", "info"))
                deleted = None
                for attempt in range(1, 4):
                    try:
                        deleted = ftp_delete_bin_files(
                            device_ip, "/sdcard/firmware",
                            source_ip=pc_ip, source_if_index=si,
                        )
                        break
                    except Exception as exc:
                        if attempt < 3:
                            self.queue.put(("step", f"[{i + 1}/{total}] {device_ip}: 第 {attempt} 次删除失败，重试...", "info"))
                            time.sleep(1)
                        else:
                            self.queue.put(("error", f"[{i + 1}/{total}] {device_ip}: 删除失败（3次）— {exc}"))
                if deleted is None:
                    self._failed.append(device_ip)
                    continue
                if deleted:
                    self.queue.put(("step", f"[{i + 1}/{total}] {device_ip}: 已删除 {len(deleted)} 个旧固件", "info"))

                remote_path = f"/sdcard/firmware/{fw_name}"
                self.queue.put(("step", f"[{i + 1}/{total}] {device_ip}: 上传固件 ({total_mb:.1f} MB)...", "info"))
                uploaded = False
                for attempt in range(1, 4):
                    try:
                        sent = [0]
                        last_pct = [0]

                        def _cb(chunk: bytes) -> None:
                            sent[0] += len(chunk)
                            pct = sent[0] * 100 // fw_size
                            if pct >= last_pct[0] + 20 or pct == 100:
                                last_pct[0] = pct
                                self.queue.put(("step", f"[{i + 1}/{total}] {device_ip}: 上传 {pct}%", "info"))

                        ftp_upload_file(
                            device_ip, self.firmware_path, remote_path,
                            source_ip=pc_ip, source_if_index=si, callback=_cb,
                        )
                        self.queue.put(("step", f"[{i + 1}/{total}] {device_ip}: 上传完成", "info"))
                        uploaded = True
                        break
                    except Exception as exc:
                        if attempt < 3:
                            self.queue.put(("step", f"[{i + 1}/{total}] {device_ip}: 第 {attempt} 次上传失败，重试...", "info"))
                            time.sleep(2)
                        else:
                            self.queue.put(("error", f"[{i + 1}/{total}] {device_ip}: 上传失败（3次）— {exc}"))
                if not uploaded:
                    self._failed.append(device_ip)
                    continue

                self.queue.put(("step", f"[{i + 1}/{total}] {device_ip}: 发送 OTA 命令...", "info"))
                ota_sent = False
                for attempt in range(1, 4):
                    try:
                        reply = request_device(
                            device_ip, {"cmd": "ota_upgrade", "path": remote_path},
                            pc_ip, timeout=10, link=link,
                        )
                        self.queue.put(("step", f"[{i + 1}/{total}] {device_ip}: OTA 命令已发送 (code={reply.get('code')})", "info"))
                        ota_sent = True
                        break
                    except Exception as exc:
                        if attempt < 3:
                            self.queue.put(("step", f"[{i + 1}/{total}] {device_ip}: 第 {attempt} 次 OTA 命令失败，重试...", "info"))
                            time.sleep(1)
                        else:
                            self.queue.put(("error", f"[{i + 1}/{total}] {device_ip}: OTA 命令失败（3次）— {exc}"))
                if not ota_sent:
                    self._failed.append(device_ip)
                    continue

                pw = OTAPostWorker(link)
                pw.start()
                post_workers.append((device_ip, pw))
                self._uploaded += 1
                self.queue.put(("step", f"[{i + 1}/{total}] {device_ip}: 设备正在重启...", "info"))

            if not post_workers:
                self.queue.put(("error", "没有设备成功进入升级阶段"))
                self.queue.put(("summary_fail", list(self._failed), 0))
                return

            self.queue.put(("step", f"全部 {len(post_workers)} 台设备已进入升级重启，等待设备就绪...", "info"))

            # Phase 2: Parallel wait (device reboot happens on-device, not over wire)
            deadline = time.monotonic() + 300
            total_phase2 = len(post_workers)
            while post_workers and time.monotonic() < deadline:
                for device_ip, pw in list(post_workers):
                    if (device_ip, pw) not in post_workers:
                        continue
                    # 一次性排空该 worker 的队列，跳过 step/info 等非终态事件，
                    # 直到拿到终态（result/error/finished_post）或队列空为止。
                    # 之前每轮只取一条，取到开头那条 "step" 时 worker 线程早已退出，
                    # 误判为"等待线程异常退出"，把已成功的设备算成失败。
                    terminal_reached = False
                    while True:
                        try:
                            item = pw.queue.get_nowait()
                        except queue.Empty:
                            break
                        t = item[0]
                        if t == "result":
                            post_workers.remove((device_ip, pw))
                            self._ready += 1
                            self.queue.put(("step", f"设备 {item[1].get('target_ip', device_ip)}: 就绪 ({self._ready}/{total_phase2})", "info"))
                            terminal_reached = True
                            break
                        if t == "error":
                            post_workers.remove((device_ip, pw))
                            self._failed.append(device_ip)
                            self.queue.put(("step", f"设备 {device_ip}: 重启失败 — {item[1]}", "error"))
                            terminal_reached = True
                            break
                        if t == "finished_post":
                            post_workers.remove((device_ip, pw))
                            self._failed.append(device_ip)
                            self.queue.put(("step", f"设备 {device_ip}: 等待线程异常退出", "error"))
                            terminal_reached = True
                            break
                        # 其它事件（step/info）继续排空
                    if not terminal_reached and (device_ip, pw) in post_workers and not pw.is_running():
                        post_workers.remove((device_ip, pw))
                        self._failed.append(device_ip)
                        self.queue.put(("step", f"设备 {device_ip}: 等待线程异常退出", "error"))
                time.sleep(1)

            if post_workers:
                for device_ip, _pw in post_workers:
                    self._failed.append(device_ip)
                self.queue.put(("step", f"等待超时，{len(post_workers)} 台设备未就绪", "error"))
            if self._failed:
                self.queue.put(("summary_fail", list(self._failed), self._uploaded))
            elif self._uploaded > 0:
                self.queue.put(("step", "所有设备升级完成，请刷新设备列表", "info"))
        except Exception as exc:
            self.queue.put(("error", str(exc)))
        finally:
            resume_all_watch_hubs()
            self.queue.put(("finished", self))


class SDSDCleanWorker:
    """Delete all /sdcard contents except firmware/ on multiple devices."""

    def __init__(self, links: list[Dict[str, Any]]):
        self.links = links
        self.queue: queue.Queue = queue.Queue()
        self._thread: threading.Thread | None = None
        self._failed: list[str] = []
        self._success = 0

    def start(self) -> None:
        self._thread = threading.Thread(target=self._run, daemon=True)
        self._thread.start()

    def is_running(self) -> bool:
        return self._thread is not None and self._thread.is_alive()

    def _run(self) -> None:
        total = len(self.links)
        init_com_mta()
        # 同 OTA：FTP 传输期间暂停监视轮询，避免锁竞争触发 COM 重入。
        suspend_all_watch_hubs()
        try:
            for i, link in enumerate(self.links):
                device_ip = str(link.get("device_ip", ""))
                pc_ip = str(link.get("pc_ip", ""))
                si = int(link.get("if_index", 0) or 0) or None
                self.queue.put(("step", f"[{i + 1}/{total}] {device_ip}: 清理 SD 卡...", "info"))
                cleaned = False
                for attempt in range(1, 4):
                    try:
                        deleted = ftp_clean_all(device_ip, source_ip=pc_ip, source_if_index=si)
                        self.queue.put(("step", f"[{i + 1}/{total}] {device_ip}: 已删除 {len(deleted)} 个项目", "info"))
                        cleaned = True
                        break
                    except Exception as exc:
                        if attempt < 3:
                            self.queue.put(("step", f"[{i + 1}/{total}] {device_ip}: 第 {attempt} 次清理失败，重试...", "info"))
                            time.sleep(1)
                        else:
                            self.queue.put(("error", f"[{i + 1}/{total}] {device_ip}: 清理失败（3次）— {exc}"))
                if cleaned:
                    self._success += 1
                    continue
                self._failed.append(device_ip)
            if self._failed:
                self.queue.put(("summary_fail", list(self._failed), self._success))
            elif self._success > 0:
                self.queue.put(("step", "SD 卡清理完成", "info"))
        except Exception as exc:
            self.queue.put(("error", str(exc)))
        finally:
            resume_all_watch_hubs()
            self.queue.put(("finished", self))


class WatchWorker:
    def __init__(
        self, key: str, host: str, port: int, source_host: str,
        source_if_index: int | None = None, interval_ms: int = 1000,
    ):
        self.key = key
        self.host = host
        self.port = port
        self.source_host = source_host
        self.source_if_index = source_if_index
        self.interval_ms = interval_ms
        self.queue: queue.Queue = queue.Queue()
        self._subscription: WatchSubscription | None = None

    def start(self) -> None:
        if self._subscription is None:
            self._subscription = acquire_watch_subscription(
                self.key, self.host, self.port, self.source_host, self.interval_ms,
                self.source_if_index,
            )
            self.queue = self._subscription.queue  # type: ignore[assignment]

    def stop(self) -> None:
        if self._subscription is not None:
            self._subscription.close()
            self._subscription = None

    def join(self, timeout: float) -> bool:
        return True

    def is_running(self) -> bool:
        return self._subscription is not None


class RecordWorker:
    def __init__(
        self,
        key: str,
        host: str,
        port: int,
        source_host: str,
        suite: str,
        case_id: int,
        record_dir: str,
        source_if_index: int | None = None,
        confirm_risk: bool = False,
        interval_ms: int = 500,
        wait_timeout: float = 3600.0,
        custom_config_snapshot: Mapping[str, Any] | None = None,
    ):
        self.key = key
        self.host = host
        self.port = port
        self.source_host = source_host
        self.source_if_index = source_if_index
        self.suite = suite
        self.case_id = case_id
        self.record_dir = record_dir
        self.confirm_risk = confirm_risk
        self.interval_ms = interval_ms
        self.wait_timeout = wait_timeout
        self.custom_config_snapshot = (
            json.loads(json.dumps(custom_config_snapshot, ensure_ascii=False))
            if custom_config_snapshot is not None else None
        )
        self._stop_event = threading.Event()
        self._thread: threading.Thread | None = None
        self.queue: queue.Queue = queue.Queue()

    def start(self) -> None:
        self._thread = threading.Thread(target=self._run, daemon=True)
        self._thread.start()

    def _run(self) -> None:
        init_com_mta()
        events: list[Dict[str, Any]] = []
        final_status: Dict[str, Any] = {}
        started_at = time.strftime("%Y-%m-%dT%H:%M:%S")
        deadline = time.monotonic() + self.wait_timeout
        start_confirmation_deadline = time.monotonic() + 10.0
        subscription: WatchSubscription | None = None
        start_confirmed = False
        baseline_started_at_ms = 0
        active_started_at_ms = 0

        try:
            subscription = acquire_watch_subscription(
                self.key, self.host, self.port, self.source_host, self.interval_ms,
                self.source_if_index,
            )
            baseline = subscription.snapshot_status()
            if is_current_case_running(baseline, self.case_id, self.suite):
                baseline_started_at_ms = int(baseline.get("started_at_ms", 0) or 0)
            probe_device_with_reconnect(
                self.host, self.source_host, port=self.port, timeout=3.0,
                source_if_index=self.source_if_index,
            )
            try:
                with _request_lock, TestAgentClient(
                    self.host,
                    self.port,
                    timeout=3.0,
                    source_host=self.source_host,
                    source_if_index=self.source_if_index,
                ) as client:
                    run_reply = client.request(make_run_command(
                        self.suite, self.case_id, confirm_risk=self.confirm_risk,
                    ))
                    if not is_ambiguous_start_reply(run_reply):
                        self._append_event(events, run_reply)
                    if run_reply.get("code") != 0 and not is_ambiguous_start_reply(run_reply):
                        final_status = run_reply
                    elif is_current_case_running(run_reply, self.case_id, self.suite):
                        active_started_at_ms = int(run_reply.get("started_at_ms", 0) or 0)
                        start_confirmed = (
                            baseline_started_at_ms == 0
                            or active_started_at_ms > baseline_started_at_ms
                        )
                        if start_confirmed:
                            start_confirmation_deadline = 0.0
            finally:
                time.sleep(_SOCKET_SETTLE_SECONDS)

            if not final_status:
                assert subscription is not None
                while not self._stop_event.is_set() and time.monotonic() < deadline:
                    if start_confirmation_deadline and time.monotonic() >= start_confirmation_deadline:
                        final_status = {
                            "cmd": "run_case",
                            "code": -10,
                            "status": "error",
                            "suite": self.suite,
                            "case_id": self.case_id,
                            "last_msg": "case start was not confirmed within 10 seconds",
                        }
                        self._append_event(events, final_status)
                        break
                    try:
                        item = subscription.queue.get(timeout=1.0)
                    except queue.Empty:
                        continue
                    if item[0] == "error":
                        events.append({
                            "time": time.strftime("%Y-%m-%dT%H:%M:%S"),
                            "event": {"cmd": "watch_error", "msg": item[2]},
                        })
                        self.queue.put(("watch_error", self.key, item[2]))
                        continue
                    if item[0] != "event":
                        continue
                    event = item[2]
                    self._append_event(events, event)
                    status = event.get("status") if event.get("cmd") == "case_status_event" else event
                    if not isinstance(status, dict):
                        continue
                    if is_current_case_running(status, self.case_id, self.suite):
                        started_at_ms = int(status.get("started_at_ms", 0) or 0)
                        if baseline_started_at_ms == 0 or started_at_ms > baseline_started_at_ms:
                            start_confirmed = True
                            active_started_at_ms = started_at_ms
                            start_confirmation_deadline = 0.0
                    if start_confirmed and is_terminal_status(status, self.case_id, self.suite):
                        finished_started_at_ms = int(status.get("started_at_ms", 0) or 0)
                        if active_started_at_ms == 0 or finished_started_at_ms >= active_started_at_ms:
                            final_status = status
                            break
        except Exception as exc:
            final_status = {
                "cmd": "get_case_status",
                "code": -20,
                "status": "error",
                "last_suite": self.suite,
                "last_case_id": self.case_id,
                "last_msg": f"connection lost: {exc}",
            }
            self._append_event(events, final_status)

        if not final_status:
            final_status = {
                "cmd": "get_case_status",
                "code": -21,
                "status": "error",
                "last_suite": self.suite,
                "last_case_id": self.case_id,
                "last_msg": "record timeout",
            }
            self._append_event(events, final_status)

        try:
            defect_kind = classify_defect(final_status)
            final_name = defect_kind if defect_kind != "none" else str(final_status.get("status", "unknown"))
            path = make_record_path(self.record_dir, self.case_id, final_name, self.suite)
            record = {
                "version": 1,
                "started_at": started_at,
                "host": self.host,
                "device_host": self.host,
                "source_host": self.source_host,
                "port": self.port,
                "suite": self.suite,
                "case_id": self.case_id,
                "command": make_run_command(
                    self.suite, self.case_id, confirm_risk=self.confirm_risk,
                ),
                "final_status": final_status,
                "defect_kind": defect_kind,
                "events": events,
            }
            if self.suite == "stress_test":
                record["attachments"] = self._download_stress_artifacts(path)
            elif self.suite == "custom_test" and self.case_id == 1:
                record["custom_config_snapshot"] = self.custom_config_snapshot or {
                    "capture_error": "canonical C01 config was unavailable when the PC run started",
                }
                record["attachments"] = self._download_custom_c01_artifacts(path, final_status)
            with open(path, "w", encoding="utf-8") as fp:
                json.dump(record, fp, ensure_ascii=False, indent=2)
                fp.write("\n")
            self.queue.put(("saved", self.key, path))
        except Exception as exc:
            self.queue.put(("error", self.key, str(exc)))
        finally:
            if subscription is not None:
                subscription.close()
            self.queue.put(("finished", self.key))

    def _download_stress_artifacts(self, record_path: str) -> list[Dict[str, Any]]:
        return self._download_artifacts(record_path, STRESS_TEST_ARTIFACTS)

    def _download_custom_c01_artifacts(
        self, record_path: str, final_status: Mapping[str, Any],
    ) -> list[Dict[str, Any]]:
        if not is_terminal_status(dict(final_status), self.case_id, self.suite):
            return [{
                "remote_path": CUSTOM_TEST_C01_REPORT_ARTIFACT,
                "skipped": "C01 did not reach a confirmed terminal state; skipped to avoid collecting a stale report",
            }]
        return self._download_artifacts(record_path, (CUSTOM_TEST_C01_REPORT_ARTIFACT,))

    def _download_artifacts(
        self, record_path: str, remote_paths: tuple[str, ...],
    ) -> list[Dict[str, Any]]:
        attachments: list[Dict[str, Any]] = []
        record_base = os.path.splitext(record_path)[0]
        for remote_path in remote_paths:
            local_path = f"{record_base}_{os.path.basename(remote_path)}"
            try:
                try:
                    with _request_lock, TestAgentClient(
                        self.host, self.port, timeout=5.0, source_host=self.source_host,
                        source_if_index=self.source_if_index,
                    ) as client:
                        _, data = client.get_file(remote_path)
                finally:
                    time.sleep(_SOCKET_SETTLE_SECONDS)
                with open(local_path, "wb") as fp:
                    fp.write(data)
                attachments.append({
                    "remote_path": remote_path,
                    "local_path": local_path,
                    "size": len(data),
                })
            except Exception as exc:  # noqa: BLE001 - artifact collection is best-effort
                attachments.append({"remote_path": remote_path, "error": str(exc)})
        return attachments

    def stop(self) -> None:
        self._stop_event.set()

    def join(self, timeout: float) -> bool:
        if self._thread is None:
            return True
        self._thread.join(timeout)
        return not self._thread.is_alive()

    def is_running(self) -> bool:
        return self._thread is not None and self._thread.is_alive()

    def _append_event(self, events: list[Dict[str, Any]], event: Dict[str, Any]) -> None:
        events.append({"time": time.strftime("%Y-%m-%dT%H:%M:%S"), "event": event})
        self.queue.put(("event", self.key, event))


class ScreenshotWorker:
    def __init__(
        self, host: str, port: int, source_host: str, capture_type: str,
        source_if_index: int | None = None,
    ):
        self.host = host
        self.port = port
        self.source_host = source_host
        self.source_if_index = source_if_index
        self.capture_type = capture_type
        self._thread: threading.Thread | None = None
        self.queue: queue.Queue = queue.Queue()

    def start(self) -> None:
        self._thread = threading.Thread(target=self._run, daemon=True)
        self._thread.start()

    def _run(self) -> None:
        init_com_mta()
        try:
            probe_device_with_reconnect(
                self.host, self.source_host, port=self.port, timeout=8.0,
                source_if_index=self.source_if_index,
            )
            try:
                with _request_lock, TestAgentClient(
                    self.host, self.port, timeout=8.0, source_host=self.source_host,
                    source_if_index=self.source_if_index,
                ) as client:
                    metadata, raw = client.screenshot(self.capture_type)
            finally:
                time.sleep(_SOCKET_SETTLE_SECONDS)
            self.queue.put(("result", metadata, raw))
        except Exception as exc:
            self.queue.put(("error", str(exc)))
        finally:
            self.queue.put(("finished", self))

    def join(self, timeout: float) -> bool:
        if self._thread is None:
            return True
        self._thread.join(timeout)
        return not self._thread.is_alive()

    def is_running(self) -> bool:
        return self._thread is not None and self._thread.is_alive()


class CrashExportWorker:
    def __init__(
        self, key: str, host: str, source_host: str, suite: str, case_id: int,
        source_if_index: int | None = None,
    ):
        self.key = key
        self.host = host
        self.source_host = source_host
        self.source_if_index = source_if_index
        self.suite = suite
        self.case_id = case_id
        self.queue: queue.Queue = queue.Queue()
        self._thread: threading.Thread | None = None

    def start(self) -> None:
        self._thread = threading.Thread(target=self._run, daemon=True)
        self._thread.start()

    def _run(self) -> None:
        init_com_mta()
        try:
            result = export_crash_files(
                self.host,
                self.source_host,
                self.suite,
                self.case_id,
                source_if_index=self.source_if_index,
                progress=lambda text: self.queue.put(("progress", text)),
            )
            self.queue.put(("result", result))
        except Exception as exc:
            self.queue.put(("error", str(exc)))
        finally:
            self.queue.put(("finished", self))

    def is_running(self) -> bool:
        return self._thread is not None and self._thread.is_alive()


class ScreenshotDialog(QDialog):
    closed = Signal()

    def __init__(self, parent: QWidget | None = None):
        super().__init__(parent)
        self.setWindowTitle("设备屏幕")
        self.resize(720, 560)
        self._image = QImage()

        self.info_label = QLabel("正在获取画面...")
        self.info_label.setStyleSheet("font-weight:600; color:#475569;")
        self.image_label = QLabel("正在获取画面...")
        self.image_label.setAlignment(Qt.AlignCenter)
        self.image_label.setMinimumSize(320, 320)
        self.image_label.setStyleSheet("background:#0f172a; color:#cbd5e1; border-radius:6px;")

        self.save_btn = _make_button("保存图片", "#059669")
        self.close_btn = _make_button("关闭", "#475569")
        self.save_btn.setEnabled(False)
        self.save_btn.clicked.connect(self.save_image)
        self.close_btn.clicked.connect(self.close)

        buttons = QHBoxLayout()
        buttons.addStretch(1)
        buttons.addWidget(self.save_btn)
        buttons.addWidget(self.close_btn)

        layout = QVBoxLayout()
        layout.addWidget(self.info_label)
        layout.addWidget(self.image_label, 1)
        layout.addLayout(buttons)
        self.setLayout(layout)

    def set_frame(self, image: QImage, metadata: Dict[str, Any]) -> None:
        self._image = image
        self.info_label.setText(
            f"{metadata.get('type', '-')}  |  {image.width()}×{image.height()}  |  "
            f"{metadata.get('bpp', '-')} bpp"
        )
        self.save_btn.setEnabled(not image.isNull())
        self._refresh_pixmap()

    def set_error(self, message: str) -> None:
        self.info_label.setText(f"获取画面失败：{message}")
        self.image_label.setText("无可用画面")

    def resizeEvent(self, event) -> None:  # noqa: N802 - Qt API
        super().resizeEvent(event)
        self._refresh_pixmap()

    def closeEvent(self, event) -> None:  # noqa: N802 - Qt API
        self.closed.emit()
        super().closeEvent(event)

    def _refresh_pixmap(self) -> None:
        if self._image.isNull():
            return
        pixmap = QPixmap.fromImage(self._image).scaled(
            self.image_label.size(), Qt.KeepAspectRatio, Qt.SmoothTransformation,
        )
        self.image_label.setPixmap(pixmap)

    def save_image(self) -> None:
        if self._image.isNull():
            return
        default_name = f"pocket_screenshot_{time.strftime('%Y%m%d_%H%M%S')}.png"
        path, _ = QFileDialog.getSaveFileName(
            self, "保存截图", default_name, "PNG 图片 (*.png);;JPEG 图片 (*.jpg *.jpeg)",
        )
        if path and not self._image.save(path):
            QMessageBox.warning(self, "保存截图", f"无法保存到：{path}")


class NetworkConfigDialog(QDialog):
    configured = Signal(object)

    def __init__(self, parent: QWidget | None = None):
        super().__init__(parent)
        self.setWindowTitle("设备连接")
        self.setMinimumSize(620, 300)
        self._devices: list[Dict[str, str]] = []
        self._thread: QThread | None = None
        self._worker: ConfigureWorker | None = None
        self._config_data: Dict[str, str] | None = None
        self._discovery_thread: QThread | None = None
        self._discovery_worker: DiscoveryWorker | None = None

        self.table = QTableWidget(0, 4)
        self.table.setHorizontalHeaderLabels(["接口", "检测到的 PC IP", "设备 IP", "状态"])
        self.table.setSelectionBehavior(QAbstractItemView.SelectRows)
        self.table.setSelectionMode(QAbstractItemView.SingleSelection)
        self.table.setEditTriggers(QAbstractItemView.NoEditTriggers)
        self.table.verticalHeader().setVisible(False)
        self.table.horizontalHeader().setSectionResizeMode(QHeaderView.Stretch)

        self.status = QLabel("PC RNDIS IP 和路由由用户手工配置；本工具只设置设备 IP。")
        self.refresh_btn = _make_button("刷新", "#64748b")
        self.configure_btn = _make_button("连接选中设备", "#0f766e")
        self.close_btn = _make_button("关闭", "#475569")
        self.refresh_btn.clicked.connect(self.refresh)
        self.configure_btn.clicked.connect(self.configure_selected)
        self.close_btn.clicked.connect(self.close)

        buttons = QHBoxLayout()
        buttons.addWidget(self.refresh_btn)
        buttons.addWidget(self.configure_btn)
        buttons.addStretch(1)
        buttons.addWidget(self.close_btn)

        root = QVBoxLayout()
        root.addWidget(self.table)
        root.addWidget(self.status)
        root.addLayout(buttons)
        self.setLayout(root)
        self.refresh()

    def refresh(self) -> None:
        if self._discovery_thread is not None:
            return
        self.refresh_btn.setEnabled(False)
        self.status.setText("正在发现网络链路...")
        self._discovery_thread = QThread(self)
        self._discovery_worker = DiscoveryWorker()
        self._discovery_worker.moveToThread(self._discovery_thread)
        self._discovery_thread.started.connect(self._discovery_worker.run)
        self._discovery_worker.result.connect(self._on_discovered, Qt.QueuedConnection)
        self._discovery_worker.error.connect(self._on_discovery_error, Qt.QueuedConnection)
        self._discovery_worker.finished.connect(self._discovery_worker.deleteLater)
        self._discovery_worker.finished.connect(self._discovery_thread.quit)
        self._discovery_thread.finished.connect(self._discovery_thread.deleteLater)
        self._discovery_thread.finished.connect(self._discovery_finished, Qt.QueuedConnection)
        self._discovery_thread.start()

    def _on_discovered(self, devices: list[Dict[str, str]]) -> None:
        self._devices = devices
        self.table.setRowCount(len(self._devices))
        for row, device in enumerate(self._devices):
            self.table.setItem(row, 0, QTableWidgetItem(device["iface"]))
            self.table.setItem(row, 1, QTableWidgetItem(device["pc_ip"]))
            self.table.setItem(row, 2, QTableWidgetItem(device["device_ip"]))
            if not device.get("pc_ip"):
                link_status = "请手工配置 PC IP"
            elif device.get("configured", False):
                link_status = "已配置"
            else:
                link_status = "待连接"
            self.table.setItem(row, 3, QTableWidgetItem(link_status))
        self.status.setText(f"发现 {len(self._devices)} 条网络链路。")

    def _on_discovery_error(self, message: str) -> None:
        self.status.setText(f"发现网络链路失败：{message}")

    def _discovery_finished(self) -> None:
        self._discovery_thread = None
        self._discovery_worker = None
        self.refresh_btn.setEnabled(True)

    def configure_selected(self) -> None:
        row = self.table.currentRow()
        if row < 0 or row >= len(self._devices):
            QMessageBox.warning(self, "设备连接", "请选择一条网络链路")
            return
        if self._thread is not None:
            return

        self._config_data = self._devices[row]
        self.status.setText(
            f"正在使用 {self._config_data['pc_ip']} 连接并配置设备 {self._config_data['device_ip']} ..."
        )
        self.configure_btn.setEnabled(False)
        self._thread = QThread(self)
        self._worker = ConfigureWorker(self._config_data)
        self._worker.moveToThread(self._thread)
        self._thread.started.connect(self._worker.run)
        self._worker.result.connect(self._on_config_result, Qt.QueuedConnection)
        self._worker.error.connect(self._on_config_error, Qt.QueuedConnection)
        self._worker.finished.connect(self._worker.deleteLater)
        self._worker.finished.connect(self._thread.quit)
        self._thread.finished.connect(self._thread.deleteLater)
        self._thread.finished.connect(self._config_thread_done, Qt.QueuedConnection)
        self._thread.start()

    def _on_config_result(self, result: Dict[str, Any]) -> None:
        if result.get("success"):
            self.status.setText(f"设备连接完成：{result['target_ip']} 已可达。")
        else:
            msg = result.get("error", "设备连接失败")
            self.status.setText(msg)
        self.configured.emit({"device": self._config_data, "result": result})

    def _on_config_error(self, message: str) -> None:
        self.status.setText(f"配置异常：{message}")

    def _clear_worker(self) -> None:
        self._thread = None
        self._worker = None
        self._config_data = None
        self.configure_btn.setEnabled(True)

    def _config_thread_done(self) -> None:
        self._thread = None
        self._worker = None
        self.configure_btn.setEnabled(True)

    def shutdown(self) -> bool:
        threads = [thread for thread in (self._thread, self._discovery_thread) if thread is not None]
        for thread in threads:
            thread.quit()
        return all(thread.wait(7000) for thread in threads)


def _make_button(text: str, color: str) -> QPushButton:
    button = QPushButton(text)
    button.setStyleSheet(
        f"QPushButton {{ background:{color}; color:white; border:none; border-radius:5px; "
        "padding:6px 12px; font-size:12px; font-weight:600; }"
        f"QPushButton:hover {{ background:{color}dd; }}"
        "QPushButton:disabled { background:#94a3b8; }"
    )
    return button


_CUSTOM_CAPABILITY_LABELS = {
    "Main": "主界面",
    "Control Center": "控制中心",
    "Playback": "播放",
    "Quick Settings": "快速设置",
    "Exposure Settings": "曝光设置",
    "White Balance": "白平衡",
    "Focus Mode": "对焦模式",
    "Image Adjustment": "图像调节",
    "Mode": "模式",
    "Video Parameters": "视频参数",
    "System Settings": "系统设置",
    "Customer Record": "客户录像",
    "Record Screen Rotation": "录像旋转",
    "Brightness": "亮度",
    "Gimbal Speed": "云台速度",
    "Gimbal Mode": "云台模式",
    "Joystick Speed": "摇杆速度",
    "Gimbal Boot Direction": "云台启动方向",
    "Prompt Tone": "提示音",
    "Anti-flicker": "防闪烁",
    "Language": "语言",
    "Slider Control": "滑杆控制",
    "Selfie Mirror": "自拍镜像",
    "Cancel Recording": "取消录像",
    "Gimbal Calibration": "云台校准",
    "LED Light": "指示灯",
    "Screen Off After Record": "录像后息屏",
    "Auto Shutdown": "自动关机",
    "Wireless Connection": "无线连接",
    "Wireless Connection Info": "无线连接信息",
    "Wi-Fi Band": "Wi-Fi 频段",
    "Video Compression": "视频压缩",
    "Reference Line": "参考线",
    "Password": "密码",
    "Device Info": "设备信息",
    "Certification Info": "认证信息",
    "Wireless Microphone": "无线麦克风",
    "Naming Manage": "命名管理",
    "Folder Naming": "文件夹命名",
    "File Naming": "文件命名",
    "Export Log": "导出日志",
    "Gimbal Track Rotate": "云台跟踪旋转",
    "Quick Start Guide": "快速入门指南",
    "Photo": "拍照模式",
    "Video": "录像模式",
    "4K 30fps 16:9": "4K 30帧/秒（16:9）",
    "1080P 25fps 16:9": "1080P 25帧/秒（16:9）",
    "1080P 30fps 16:9": "1080P 30帧/秒（16:9）",
    "2.7K 25fps 16:9": "2.7K 25帧/秒（16:9）",
    "2.7K 30fps 16:9": "2.7K 30帧/秒（16:9）",
    "4K 25fps 16:9": "4K 25帧/秒（16:9）",
    "Photo Mode": "拍照模式",
    "Video Mode": "录像模式",
    "Page": "页面",
    "Open Control Center": "打开控制中心",
    "Close Control Center": "关闭控制中心",
    "Open System Settings": "打开系统设置",
    "Zoom In": "放大",
    "Zoom Out": "缩小",
    "30 fps": "30 帧/秒",
    "2X (60fps)": "2 倍（60 帧/秒）",
    "4X (120fps)": "4 倍（120 帧/秒）",
}

# VIDEO is kept in the wire protocol so older saved C01 configurations can be
# read and removed, but new configurations must use VIDEO_RECORD.  VIDEO has
# no orientation field and therefore can only express the legacy 16:9 path.
_LEGACY_CUSTOM_ACTIONS = frozenset({ACTION_VIDEO})


def _custom_display_label(value: object) -> str:
    text = str(value)
    return _CUSTOM_CAPABILITY_LABELS.get(text, text)


_CUSTOM_DURATION_UNITS = (
    ("秒", 1, 60),
    ("分钟", 60, 60),
    ("小时", 60 * 60, 24),
)


def _format_custom_record_duration(seconds: int) -> str:
    """Format canonical seconds without hiding non-minute/hour precision."""
    seconds = max(0, int(seconds))
    for label, multiplier, _ in reversed(_CUSTOM_DURATION_UNITS):
        if seconds >= multiplier and seconds % multiplier == 0:
            return f"{seconds // multiplier}{label}"
    return f"{seconds}秒"


class CustomConfigDialog(QDialog):
    """Bounded C01 editor backed exclusively by device capabilities."""

    save_requested = Signal(object, int, bool)
    profiles_changed = Signal(object)

    def __init__(
        self,
        capabilities: CustomCapabilities,
        config: CustomConfig,
        revision: int,
        firmware_version: str,
        parent: QWidget | None = None,
    ):
        super().__init__(parent)
        self.capabilities = capabilities
        self.revision = revision
        self.firmware_version = firmware_version
        self.steps = list(config.steps)
        self._profile_store = CustomProfileStore(custom_profiles_path())
        self._profiles = self._profile_store.load(capabilities)
        self._loading = False
        self.setWindowTitle("配置自定义测试 C01")
        self.setMinimumSize(900, 680)
        self.resize(980, 760)
        # Keep the editor visibly above the main window.  A modeless child can
        # otherwise be opened behind the maximized main window on Windows,
        # which looks exactly like the application stopped responding.
        self.setWindowModality(Qt.NonModal)
        self.setWindowFlag(Qt.WindowStaysOnTopHint, True)
        self.setStyleSheet(
            "QDialog { background:#f8fafc; }"
            "QGroupBox { font-weight:700; border:1px solid #cbd5e1; border-radius:8px; "
            "margin-top:10px; padding:10px; background:white; }"
            "QGroupBox::title { subcontrol-origin:margin; left:12px; padding:0 5px; color:#0f766e; }"
            "QComboBox, QSpinBox, QListWidget { min-height:30px; border:1px solid #cbd5e1; "
            "border-radius:5px; padding:2px 6px; background:white; }"
            "QListWidget::item { padding:6px; }"
            "QListWidget::item:selected { background:#ccfbf1; color:#134e4a; }"
        )
        self._busy_timeout_timer = QTimer(self)
        self._busy_timeout_timer.setSingleShot(True)
        self._busy_timeout_timer.timeout.connect(self._on_busy_timeout)

        self.device_label = QLabel(
            f"固件：{firmware_version or '-'}  |  策略：{capabilities.policy_version}  |  "
            f"配置修订版：{revision or '新建'}"
        )
        self.device_label.setStyleSheet("color:#475569;")
        self.profile_combo = QComboBox()
        self.profile_name_edit = QLineEdit()
        self.profile_name_edit.setPlaceholderText("例如：4K30 录像稳定性")
        self.load_profile_btn = _make_button("载入方案", "#2563eb")
        self.load_save_profile_btn = _make_button("载入并覆盖设备", "#0f766e")
        self.save_profile_btn = _make_button("另存为新方案", "#059669")
        self.update_profile_btn = _make_button("更新所选方案", "#0f766e")
        self.delete_profile_btn = _make_button("删除所选方案", "#dc2626")
        self.load_profile_btn.setToolTip("仅载入到编辑器，不修改设备配置")
        self.load_save_profile_btn.setToolTip("载入所选 PC 方案，确认后原子覆盖设备当前 C01 配置")
        self._refresh_profile_combo()
        self.step_list = QListWidget()
        self.step_list.setSelectionMode(QAbstractItemView.SingleSelection)

        self.action_combo = QComboBox()
        for action in capabilities.actions:
            if action in _LEGACY_CUSTOM_ACTIONS:
                continue
            self.action_combo.addItem(ACTION_LABELS[action], action)
        self.page_wait_combo = QComboBox()
        for mode in capabilities.page_wait_modes:
            self.page_wait_combo.addItem(
                "使用默认页面等待" if mode == PAGE_WAIT_AUTO else "额外等待页面稳定",
                mode,
            )
        self.parameter_stack = QStackedWidget()
        self._parameter_pages: Dict[int, int] = {}
        self._build_parameter_pages()
        self.add_step_btn = _make_button("新增步骤", "#2563eb")
        self.move_up_step_btn = _make_button("上移", "#0f766e")
        self.move_down_step_btn = _make_button("下移", "#0f766e")
        self.remove_step_btn = _make_button("删除选中步骤", "#dc2626")
        self.clear_steps_btn = _make_button("清空步骤", "#64748b")

        self.cycles_spin = QSpinBox()
        self.cycles_spin.setRange(*capabilities.cycles_range)
        self.cycles_spin.setValue(config.cycles)
        self.cycles_spin.setKeyboardTracking(False)
        self.cycles_spin.setToolTip("循环上限由设备固件决定；超过该范围的配置会被设备拒绝。")
        self.interval_combos = {
            field: self._option_combo(capabilities.interval_options[field], suffix=" 毫秒")
            for field in ("page_settle_ms", "cycle_interval_ms")
        }
        self._compatibility_media_interval_ms = custom_c01_compatibility_media_interval_ms(
            capabilities,
        )
        self.step_interval_combo = self._option_combo(
            capabilities.interval_options["step_interval_ms"], suffix=" 毫秒",
        )
        self.run_once_check = QCheckBox("仅第一轮执行此步骤，后续循环自动跳过")
        self.run_once_check.setToolTip("适合一次性参数设置、进入初始页面等前置条件；不是必选项。")
        self.check_ui_complete = QCheckBox("步骤完成后检查 UI 是否完整")
        self.check_ui_complete.setToolTip("检查当前页面、root 和页面关键控件；默认关闭。")
        self.check_ui_complete.setVisible(capabilities.supports_ui_complete)
        self.check_ui_complete.setEnabled(capabilities.supports_ui_complete)
        self.check_ui_frozen = QCheckBox("步骤完成后检查 UI 是否卡死")
        self.check_ui_frozen.setToolTip("由独立 UI heartbeat 检查页面是否继续响应；默认关闭。")
        self.check_ui_frozen.setVisible(capabilities.supports_ui_frozen)
        self.check_ui_frozen.setEnabled(capabilities.supports_ui_frozen)
        self.photo_check_combo = self._check_combo(capabilities.photo_check_modes, photo=True)
        self.video_check_combo = self._check_combo(capabilities.video_check_modes, photo=False)
        self.photo_check_every_spin = QSpinBox()
        self.video_check_every_spin = QSpinBox()
        self.photo_cleanup_every_spin = QSpinBox()
        self.video_cleanup_every_spin = QSpinBox()
        for spin in (
            self.photo_check_every_spin, self.video_check_every_spin,
            self.photo_cleanup_every_spin, self.video_cleanup_every_spin,
        ):
            # Let users type a target N before adjusting the total cycle count.
            # Validation still prevents saving while N exceeds the total cycles.
            spin.setRange(0, capabilities.cycles_range[1])
            spin.setKeyboardTracking(False)
            spin.setToolTip("0 表示不执行；填写 N 表示每完成 N 轮执行一次，且 N 不能大于总循环次数。")
        self.cleanup_wait_combos = [
            self._option_combo(capabilities.cleanup_wait_options, suffix=" 毫秒", data_is_index=True)
            for _ in range(3)
        ]
        self.cleanup_supported = capabilities.cleanup_supported and capabilities.media_manifest_supported
        if not self.cleanup_supported:
            self.photo_cleanup_every_spin.setEnabled(False)
            self.video_cleanup_every_spin.setEnabled(False)
            for combo in self.cleanup_wait_combos:
                combo.setEnabled(False)

        self.validation_label = QLabel()
        self.validation_label.setWordWrap(True)
        self.validation_label.setStyleSheet("color:#b45309;")
        self.preflight_summary_label = QLabel()
        self.preflight_summary_label.setWordWrap(True)
        self.preflight_summary_label.setStyleSheet("color:#475569;")
        self.save_btn = _make_button("覆盖设备当前方案", "#059669")
        self.save_run_btn = _make_button("覆盖并开始测试", "#16a34a")
        self.close_btn = _make_button("关闭", "#64748b")
        self.save_btn.setToolTip("原子覆盖设备当前 C01 配置，不启动测试")
        self.save_run_btn.setToolTip("原子覆盖设备当前 C01 配置，回读确认后运行 C01")
        self._busy_controls = [
            self.action_combo, self.page_wait_combo, self.step_interval_combo,
            self.run_once_check, self.check_ui_complete, self.check_ui_frozen,
            self.parameter_stack,
            self.add_step_btn, self.move_up_step_btn, self.move_down_step_btn,
            self.remove_step_btn, self.clear_steps_btn,
            self.step_list, self.cycles_spin, *self.interval_combos.values(),
            self.photo_check_combo, self.video_check_combo,
            self.photo_check_every_spin, self.video_check_every_spin,
            self.photo_cleanup_every_spin, self.video_cleanup_every_spin,
            *self.cleanup_wait_combos, self.save_btn, self.save_run_btn,
            self.profile_combo, self.profile_name_edit, self.load_profile_btn,
            self.load_save_profile_btn,
            self.save_profile_btn, self.update_profile_btn, self.delete_profile_btn,
            self.record_seconds_spin, self.record_duration_unit_combo,
            self.slow_motion_seconds_spin, self.slow_motion_duration_unit_combo,
        ]

        self._build_layout()
        self._apply_config(config)
        self.action_combo.currentIndexChanged.connect(self._on_action_changed)
        self.add_step_btn.clicked.connect(self._add_step)
        self.move_up_step_btn.clicked.connect(self._move_selected_step_up)
        self.move_down_step_btn.clicked.connect(self._move_selected_step_down)
        self.remove_step_btn.clicked.connect(self._remove_selected_step)
        self.clear_steps_btn.clicked.connect(self._clear_steps)
        self.cycles_spin.valueChanged.connect(self._cycles_changed)
        for combo in self.interval_combos.values():
            combo.currentIndexChanged.connect(self._refresh_validation)
        self.step_interval_combo.currentIndexChanged.connect(self._refresh_validation)
        self.run_once_check.toggled.connect(self._refresh_validation)
        self.check_ui_complete.toggled.connect(self._refresh_validation)
        self.check_ui_frozen.toggled.connect(self._refresh_validation)
        for combo in (self.photo_check_combo, self.video_check_combo):
            combo.currentIndexChanged.connect(self._check_mode_changed)
        for spin in (
            self.photo_check_every_spin, self.video_check_every_spin,
            self.photo_cleanup_every_spin, self.video_cleanup_every_spin,
        ):
            spin.valueChanged.connect(self._refresh_validation)
        self.record_seconds_spin.valueChanged.connect(self._refresh_validation)
        self.slow_motion_seconds_spin.valueChanged.connect(self._refresh_validation)
        for combo in self.cleanup_wait_combos:
            combo.currentIndexChanged.connect(self._refresh_validation)
        self.save_btn.clicked.connect(lambda: self._request_save(False))
        self.save_run_btn.clicked.connect(lambda: self._request_save(True))
        self.profile_combo.currentIndexChanged.connect(self._profile_selection_changed)
        self.load_profile_btn.clicked.connect(self._load_selected_profile)
        self.load_save_profile_btn.clicked.connect(self._load_and_save_selected_profile)
        self.save_profile_btn.clicked.connect(self._save_new_profile)
        self.update_profile_btn.clicked.connect(self._update_selected_profile)
        self.delete_profile_btn.clicked.connect(self._delete_selected_profile)
        self.close_btn.clicked.connect(self.close)
        self._on_action_changed()
        self._refresh_steps()
        self._refresh_validation()
        if self._profile_store.load_error is not None or self._profile_store.load_warning is not None:
            self.validation_label.setText(
                "本地方案文件存在异常，已忽略无效方案并禁止覆盖；请先备份或修复该文件。"
            )
            self.validation_label.setStyleSheet("color:#b91c1c;")

    @staticmethod
    def _option_combo(values: tuple[int, ...], *, suffix: str, data_is_index: bool = False) -> QComboBox:
        combo = QComboBox()
        for index, value in enumerate(values):
            combo.addItem(f"{value}{suffix}", index if data_is_index else value)
        return combo

    @staticmethod
    def _form_page(widget: QWidget) -> QWidget:
        page = QWidget()
        layout = QFormLayout(page)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.addRow(widget)
        return page

    def _mapping_combo(self, values: Dict[int, str]) -> QComboBox:
        combo = QComboBox()
        for value, label in values.items():
            combo.addItem(_custom_display_label(label), value)
        return combo

    def _check_combo(self, modes: tuple[int, ...], *, photo: bool) -> QComboBox:
        labels = {
            CHECK_BASELINE: "不检查（仅确认操作已发送）",
            CHECK_FILE: "检查新生成的媒体文件",
            CHECK_PLAYBACK: "回放启动检查（确认可打开并开始播放）",
            CHECK_PLAYBACK_DAMAGE: "检查视频文件损坏（回放失败即不通过）",
        }
        combo = QComboBox()
        for mode in modes:
            if photo and mode in (CHECK_PLAYBACK, CHECK_PLAYBACK_DAMAGE):
                continue
            combo.addItem(labels.get(mode, f"模式 {mode}"), mode)
        return combo

    def _build_parameter_pages(self) -> None:
        no_param = QLabel("此操作没有可编辑参数。")
        self._parameter_pages[ACTION_PHOTO_CAPTURE] = self.parameter_stack.addWidget(self._form_page(no_param))

        self.screen_rotation_combo = QComboBox()
        self.screen_rotation_combo.addItem("横屏", 0)
        self.screen_rotation_combo.addItem("竖屏", 1)
        self._parameter_pages[ACTION_SCREEN_ROTATE] = self.parameter_stack.addWidget(
            self._form_page(self.screen_rotation_combo)
        )

        self.nav_page_combo = self._mapping_combo(self.capabilities.safe_pages)
        self.nav_page_combo.setToolTip(
            "这里只显示可直接稳定进入的页面；控制中心和系统设置请使用“界面滑动”或“界面点击”操作。"
        )
        nav_page = QWidget()
        nav_page_layout = QFormLayout(nav_page)
        nav_page_layout.setContentsMargins(0, 0, 0, 0)
        nav_page_layout.addRow("固定页面", self.nav_page_combo)
        target_labels = [
            f"{target.target_id}: {target.label}"
            for target in self.capabilities.page_targets.values()
        ]
        target_summary = QLabel(
            "；".join(target_labels) if target_labels else "设备未发布动态页面目标"
        )
        target_summary.setWordWrap(True)
        target_summary.setStyleSheet("color:#475569; padding:4px 0;")
        target_summary.setToolTip(
            "动态页面目标会在后续语义导航步骤中使用；本阶段保留固定页面操作兼容性。"
        )
        nav_page_layout.addRow("动态页面目标", target_summary)
        self._parameter_pages[ACTION_NAV] = self.parameter_stack.addWidget(nav_page)
        self.nav_target_combo = QComboBox()
        for target_id, target in self.capabilities.page_targets.items():
            self.nav_target_combo.addItem(
                f"{target_id}: {_custom_display_label(target.label)}", target_id,
            )
        nav_target_page = self._form_page(self.nav_target_combo)
        self._parameter_pages[ACTION_NAV_TARGET] = self.parameter_stack.addWidget(nav_target_page)
        self.mode_combo = self._mapping_combo(self.capabilities.mode_options)
        self._parameter_pages[ACTION_MODE] = self.parameter_stack.addWidget(self._form_page(self.mode_combo))
        self.video_preset_combo = self._mapping_combo(self.capabilities.video_presets)
        self._parameter_pages[ACTION_VIDEO] = self.parameter_stack.addWidget(self._form_page(self.video_preset_combo))
        self.verify_combo = self._mapping_combo(self.capabilities.verify_options)
        self._parameter_pages[ACTION_VERIFY] = self.parameter_stack.addWidget(self._form_page(self.verify_combo))

        self.policy_combo = QComboBox()
        self._parameter_pages[ACTION_CLICK] = self.parameter_stack.addWidget(self._form_page(self.policy_combo))
        self._parameter_pages[ACTION_SWIPE] = self._parameter_pages[ACTION_CLICK]
        self._parameter_pages[ACTION_SLIDER] = self._parameter_pages[ACTION_CLICK]

        video_record_page = QWidget()
        video_record_layout = QFormLayout(video_record_page)
        video_record_layout.setContentsMargins(0, 0, 0, 0)
        self.record_canvas_combo = QComboBox()
        for canvas, label in (
            (VIDEO_CANVAS_LANDSCAPE, "横屏 16:9"),
            (VIDEO_CANVAS_PORTRAIT, "竖屏 9:16"),
        ):
            if any(profile.canvas == canvas for profile in self.capabilities.video_profiles.values()):
                self.record_canvas_combo.addItem(label, canvas)
        self.record_resolution_combo = QComboBox()
        self.record_fps_combo = QComboBox()
        self.record_seconds_spin = QSpinBox()
        self.record_duration_unit_combo = self._duration_unit_combo()
        self._configure_duration_editor(
            self.record_seconds_spin, self.record_duration_unit_combo,
            self.capabilities.record_seconds_range,
        )
        record_hint = QLabel(
            "录制视频会自动进入录像模式并应用以下规格，无需额外添加“切换拍摄模式”或“设置视频规格”步骤。\n"
            "先选择横屏或竖屏，再选择该方向下由设备固件支持的分辨率和帧率。"
        )
        record_hint.setWordWrap(True)
        record_hint.setStyleSheet("color:#64748b;")
        video_record_layout.addRow(record_hint)
        video_record_layout.addRow("拍摄方向（必填）", self.record_canvas_combo)
        video_record_layout.addRow("分辨率（必填）", self.record_resolution_combo)
        video_record_layout.addRow("帧率（必填）", self.record_fps_combo)
        video_record_layout.addRow(
            "录像时长（必填）", self._duration_editor_row(
                self.record_seconds_spin, self.record_duration_unit_combo,
            ),
        )
        self.record_canvas_combo.currentIndexChanged.connect(self._refresh_record_resolution)
        self.record_resolution_combo.currentIndexChanged.connect(self._refresh_record_fps)
        self._refresh_record_resolution()
        self._parameter_pages[ACTION_VIDEO_RECORD] = self.parameter_stack.addWidget(video_record_page)

        slow_motion_page = QWidget()
        slow_motion_layout = QFormLayout(slow_motion_page)
        slow_motion_layout.setContentsMargins(0, 0, 0, 0)
        self.slow_motion_resolution_combo = self._mapping_combo(self.capabilities.slow_motion_resolutions)
        self.slow_motion_rate_combo = self._mapping_combo(self.capabilities.slow_motion_rates)
        self.slow_motion_seconds_spin = QSpinBox()
        self.slow_motion_duration_unit_combo = self._duration_unit_combo()
        slow_motion_range = self.capabilities.slow_motion_record_seconds_range
        if slow_motion_range is None:
            slow_motion_range = self.capabilities.record_seconds_range
        self._configure_duration_editor(
            self.slow_motion_seconds_spin, self.slow_motion_duration_unit_combo,
            slow_motion_range,
        )
        slow_motion_hint = QLabel(
            "慢动作使用设备现有慢动作录制链路，不复用普通录像。画幅会随设备当前方向自适应，"
            "此处不提供横竖屏锁定。观察时长是实际录制时长，不是慢放后的播放时长。"
        )
        slow_motion_hint.setWordWrap(True)
        slow_motion_hint.setStyleSheet("color:#64748b;")
        slow_motion_layout.addRow(slow_motion_hint)
        slow_motion_layout.addRow("分辨率（设备能力）", self.slow_motion_resolution_combo)
        slow_motion_layout.addRow("慢放倍率 / 采集帧率（必填）", self.slow_motion_rate_combo)
        slow_motion_layout.addRow(
            "观察录制时长（必填）", self._duration_editor_row(
                self.slow_motion_seconds_spin, self.slow_motion_duration_unit_combo,
            ),
        )
        self._parameter_pages[ACTION_SLOW_MOTION_RECORD] = self.parameter_stack.addWidget(slow_motion_page)

    @staticmethod
    def _duration_unit_combo() -> QComboBox:
        combo = QComboBox()
        for label, multiplier, _ in _CUSTOM_DURATION_UNITS:
            combo.addItem(label, multiplier)
        combo.setProperty("duration_multiplier", 1)
        return combo

    @staticmethod
    def _duration_editor_row(spin: QSpinBox, unit_combo: QComboBox) -> QWidget:
        row = QWidget()
        layout = QHBoxLayout(row)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.addWidget(spin, 1)
        layout.addWidget(unit_combo)
        return row

    @staticmethod
    def _duration_bounds(seconds_range: tuple[int, int], multiplier: int) -> tuple[int, int]:
        minimum, maximum = seconds_range
        unit_maximum = next(
            limit for _, value, limit in _CUSTOM_DURATION_UNITS if value == multiplier
        )
        low = (minimum + multiplier - 1) // multiplier
        high = min(maximum // multiplier, unit_maximum)
        return low, max(low, high)

    def _configure_duration_editor(
        self, spin: QSpinBox, unit_combo: QComboBox, seconds_range: tuple[int, int],
    ) -> None:
        multiplier = int(unit_combo.currentData())
        low, high = self._duration_bounds(seconds_range, multiplier)
        spin.setRange(low, high)
        spin.setValue(min(max(10, low), high))
        unit_combo.currentIndexChanged.connect(
            lambda _index, s=spin, c=unit_combo, r=seconds_range:
            self._duration_unit_changed(s, c, r)
        )

    def _duration_unit_changed(
        self, spin: QSpinBox, unit_combo: QComboBox, seconds_range: tuple[int, int],
    ) -> None:
        old_multiplier = int(unit_combo.property("duration_multiplier") or 1)
        seconds = spin.value() * old_multiplier
        multiplier = int(unit_combo.currentData())
        low, high = self._duration_bounds(seconds_range, multiplier)
        converted = (seconds + multiplier - 1) // multiplier
        spin.blockSignals(True)
        spin.setRange(low, high)
        spin.setValue(min(max(converted, low), high))
        spin.blockSignals(False)
        unit_combo.setProperty("duration_multiplier", multiplier)
        self._refresh_validation()

    @staticmethod
    def _duration_seconds(spin: QSpinBox, unit_combo: QComboBox) -> int:
        return spin.value() * int(unit_combo.currentData())

    def _build_layout(self) -> None:
        root = QVBoxLayout(self)
        root.setContentsMargins(18, 16, 18, 16)
        root.setSpacing(10)
        root.addWidget(self.device_label)

        intro = QLabel("按顺序编排设备操作；保存后由设备执行，PC 会自动回读确认保存结果。")
        intro.setWordWrap(True)
        intro.setStyleSheet("color:#475569; padding:6px 0;")
        root.addWidget(intro)

        scroll = QScrollArea()
        scroll.setWidgetResizable(True)
        scroll.setFrameShape(QFrame.NoFrame)
        content = QWidget()
        content_layout = QVBoxLayout(content)
        content_layout.setContentsMargins(0, 0, 0, 0)
        content_layout.setSpacing(12)

        profiles_group = QGroupBox("0. 本地方案（仅保存在 PC）")
        profiles_form = QFormLayout(profiles_group)
        profiles_form.addRow(QLabel(
            "方案可重复载入、修改或删除；不会自动修改设备。"
        ))
        profiles_form.addRow("已保存方案", self.profile_combo)
        profiles_form.addRow("方案名称", self.profile_name_edit)
        profile_buttons = QHBoxLayout()
        profile_buttons.addWidget(self.load_profile_btn)
        profile_buttons.addWidget(self.save_profile_btn)
        profile_buttons.addWidget(self.update_profile_btn)
        profile_buttons.addWidget(self.delete_profile_btn)
        profile_buttons.addStretch(1)
        profiles_form.addRow(profile_buttons)
        content_layout.addWidget(profiles_group)

        steps_group = QGroupBox("1. 编排测试步骤")
        steps_layout = QVBoxLayout(steps_group)
        steps_layout.addWidget(QLabel(
            "已添加的步骤会按列表从上到下执行。先选择操作和参数，再点击“新增步骤”；可用上移/下移调整顺序。"
        ))
        self.step_list.setMinimumHeight(150)
        self.step_list.setAlternatingRowColors(True)
        steps_layout.addWidget(self.step_list)

        action_form = QFormLayout()
        action_form.addRow("操作（必填）", self.action_combo)
        action_form.addRow("页面等待方式", self.page_wait_combo)
        action_form.addRow("参数（按操作必填）", self.parameter_stack)
        action_form.addRow("此步骤执行后等待", self.step_interval_combo)
        action_panel = QWidget()
        action_panel.setLayout(action_form)
        steps_layout.addWidget(action_panel)
        precondition_group = QGroupBox("前置条件（可选）")
        precondition_layout = QVBoxLayout(precondition_group)
        precondition_layout.addWidget(self.run_once_check)
        precondition_layout.addWidget(QLabel(
            "勾选后，该步骤只在第 1 轮执行；后续轮次直接跳过，不额外等待。"
        ))
        steps_layout.addWidget(precondition_group)
        ui_check_group = QGroupBox("步骤完成后检查（可选）")
        ui_check_layout = QVBoxLayout(ui_check_group)
        ui_check_layout.addWidget(self.check_ui_complete)
        ui_check_layout.addWidget(self.check_ui_frozen)
        ui_check_layout.addWidget(QLabel(
            "默认关闭；只有勾选对应选项时，设备才会执行该步骤的 UI 检查。"
        ))
        ui_check_group.setVisible(
            self.capabilities.supports_ui_complete or self.capabilities.supports_ui_frozen
        )
        steps_layout.addWidget(ui_check_group)
        step_buttons = QHBoxLayout()
        step_buttons.addWidget(self.add_step_btn)
        step_buttons.addWidget(self.move_up_step_btn)
        step_buttons.addWidget(self.move_down_step_btn)
        step_buttons.addWidget(self.remove_step_btn)
        step_buttons.addWidget(self.clear_steps_btn)
        step_buttons.addStretch(1)
        steps_layout.addLayout(step_buttons)
        content_layout.addWidget(steps_group)

        cadence = QGroupBox("2. 执行节奏")
        cadence_form = QFormLayout(cadence)
        cadence_hint = QLabel(
            "每个步骤的执行后等待，已在步骤编辑区单独设置；拍照、录像和页面操作都按各自的设置执行。\n"
            "此处只设置额外页面稳定等待，以及一整轮步骤完成后到下一轮开始前的等待。"
        )
        cadence_hint.setWordWrap(True)
        cadence_hint.setStyleSheet("color:#475569;")
        cadence_form.addRow(cadence_hint)
        cycles_widget = QWidget()
        cycles_layout = QHBoxLayout(cycles_widget)
        cycles_layout.setContentsMargins(0, 0, 0, 0)
        cycles_layout.addWidget(self.cycles_spin)
        cycles_limit = QLabel(
            f"当前固件允许 {self.capabilities.cycles_range[0]}–{self.capabilities.cycles_range[1]} 轮"
        )
        cycles_limit.setStyleSheet("color:#64748b;")
        cycles_layout.addWidget(cycles_limit)
        cycles_layout.addStretch(1)
        cadence_form.addRow("循环次数（必填）", cycles_widget)
        cadence_form.addRow("额外页面稳定等待（按步骤启用）", self.interval_combos["page_settle_ms"])
        cadence_form.addRow("每轮结束后等待", self.interval_combos["cycle_interval_ms"])
        content_layout.addWidget(cadence)

        preflight_group = QGroupBox("运行预估（保存前预览）")
        preflight_layout = QVBoxLayout(preflight_group)
        preflight_layout.addWidget(self.preflight_summary_label)
        content_layout.addWidget(preflight_group)

        device_save_group = QGroupBox("3. 保存到设备")
        device_save_layout = QVBoxLayout(device_save_group)
        device_save_notice = QLabel(
            "设备端仅保留一份 C01 活动方案。保存会原子覆盖当前方案，无需先删除旧配置；"
            "保存成功后 PC 会回读修订版和 CRC 确认。若其它窗口先修改了设备，设备会拒绝过期版本的保存。"
        )
        device_save_notice.setWordWrap(True)
        device_save_notice.setStyleSheet("color:#475569;")
        device_save_layout.addWidget(device_save_notice)
        device_save_buttons = QHBoxLayout()
        device_save_buttons.addWidget(self.load_save_profile_btn)
        device_save_buttons.addWidget(self.save_btn)
        device_save_buttons.addWidget(self.save_run_btn)
        device_save_buttons.addStretch(1)
        device_save_layout.addLayout(device_save_buttons)
        content_layout.addWidget(device_save_group)

        self.media_group = QGroupBox("4. 媒体检测与清理（按已添加步骤显示）")
        media_layout = QVBoxLayout(self.media_group)
        media_hint = QLabel(
            "只有添加“拍摄照片”或“录制视频”步骤后，才显示对应配置。\n"
            "“每 N 轮”：0 表示关闭；N 表示每完成 N 轮执行一次，且 N 不得超过总循环次数。\n"
            "照片检查/删除必须同时包含“拍摄照片”步骤；视频检查、回放或删除必须同时包含“录制视频”步骤。\n"
            "自动删除只会删除本次测试新生成的对应媒体。"
        )
        media_hint.setWordWrap(True)
        media_hint.setStyleSheet("color:#475569;")
        media_layout.addWidget(media_hint)

        self.photo_media_panel = QGroupBox("照片（仅在步骤中包含“拍摄照片”时显示）")
        photo_media_form = QFormLayout(self.photo_media_panel)
        photo_media_form.addRow("检测方式", self.photo_check_combo)
        photo_media_form.addRow("每 N 轮检查（0=关闭）", self.photo_check_every_spin)
        photo_media_form.addRow("每 N 轮自动删除（0=不删除）", self.photo_cleanup_every_spin)
        media_layout.addWidget(self.photo_media_panel)

        self.video_media_panel = QGroupBox("视频（普通录像或慢动作录像步骤存在时显示）")
        video_media_form = QFormLayout(self.video_media_panel)
        video_media_form.addRow("检测方式", self.video_check_combo)
        video_media_form.addRow("每 N 轮检查（0=关闭）", self.video_check_every_spin)
        video_media_form.addRow("每 N 轮自动删除（0=不删除）", self.video_cleanup_every_spin)
        media_layout.addWidget(self.video_media_panel)
        cleanup_hint = QLabel("删除前、照片/视频之间、删除后重新扫描的等待时间由当前固件固定为 2 秒。")
        cleanup_hint.setWordWrap(True)
        cleanup_hint.setStyleSheet("color:#475569;")
        media_layout.addWidget(cleanup_hint)
        content_layout.addWidget(self.media_group)
        content_layout.addStretch(1)
        scroll.setWidget(content)
        root.addWidget(scroll, 1)
        root.addWidget(self.validation_label)
        buttons = QHBoxLayout()
        buttons.addStretch(1)
        buttons.addWidget(self.close_btn)
        root.addLayout(buttons)

    def _on_action_changed(self) -> None:
        action = self.action_combo.currentData()
        if not isinstance(action, int):
            return
        if action in (ACTION_CLICK, ACTION_SWIPE, ACTION_SLIDER):
            self.policy_combo.clear()
            for policy_id, policy in self.capabilities.policy_steps.items():
                if policy["action"] == action:
                    self.policy_combo.addItem(_custom_display_label(policy["label"]), policy_id)
        index = self._parameter_pages.get(action, self._parameter_pages[ACTION_PHOTO_CAPTURE])
        self.parameter_stack.setCurrentIndex(index)
        _APP_LOG.info("custom_config_editor_action action=%s", action)

    def _refresh_record_resolution(self) -> None:
        canvas = self.record_canvas_combo.currentData()
        previous = self.record_resolution_combo.currentData()
        self.record_resolution_combo.clear()
        for profile in self.capabilities.video_profiles.values():
            if profile.canvas == canvas:
                self.record_resolution_combo.addItem(
                    _custom_display_label(profile.resolution_label), profile.resolution_id,
                )
        if isinstance(previous, int):
            index = self.record_resolution_combo.findData(previous)
            if index >= 0:
                self.record_resolution_combo.setCurrentIndex(index)
        self._refresh_record_fps()

    def _refresh_record_fps(self) -> None:
        canvas = self.record_canvas_combo.currentData()
        resolution_id = self.record_resolution_combo.currentData()
        profile = self.capabilities.video_profiles.get((canvas, resolution_id))
        self.record_fps_combo.clear()
        if profile is not None:
            for fps_id, label in profile.fps.items():
                self.record_fps_combo.addItem(_custom_display_label(label), fps_id)

    def _step_from_editor(self) -> CustomStep:
        action = self.action_combo.currentData()
        if not isinstance(action, int):
            raise CustomConfigError("请选择操作")
        wait_mode = self.page_wait_combo.currentData()
        if not isinstance(wait_mode, int):
            raise CustomConfigError("请选择页面等待方式")
        check_kwargs = {
            "check_ui_complete": int(self.check_ui_complete.isChecked()),
            "check_ui_frozen": int(self.check_ui_frozen.isChecked()),
        }
        if action == ACTION_NAV:
            return CustomStep(action, page=int(self.nav_page_combo.currentData()), page_wait_mode=wait_mode,
                              step_interval_ms=int(self.step_interval_combo.currentData()),
                              run_once=int(self.run_once_check.isChecked()), **check_kwargs)
        if action == ACTION_NAV_TARGET:
            return CustomStep(action, page=int(self.nav_target_combo.currentData()), page_wait_mode=wait_mode,
                              step_interval_ms=int(self.step_interval_combo.currentData()),
                              run_once=int(self.run_once_check.isChecked()), **check_kwargs)
        if action == ACTION_MODE:
            return CustomStep(action, arg0=int(self.mode_combo.currentData()), page_wait_mode=wait_mode,
                              step_interval_ms=int(self.step_interval_combo.currentData()),
                              run_once=int(self.run_once_check.isChecked()), **check_kwargs)
        if action == ACTION_VIDEO:
            return CustomStep(action, arg0=int(self.video_preset_combo.currentData()), page_wait_mode=wait_mode,
                              step_interval_ms=int(self.step_interval_combo.currentData()),
                              run_once=int(self.run_once_check.isChecked()), **check_kwargs)
        if action == ACTION_VERIFY:
            return CustomStep(action, arg0=int(self.verify_combo.currentData()), page_wait_mode=wait_mode,
                              step_interval_ms=int(self.step_interval_combo.currentData()),
                              run_once=int(self.run_once_check.isChecked()), **check_kwargs)
        if action in (ACTION_CLICK, ACTION_SWIPE, ACTION_SLIDER):
            policy_id = int(self.policy_combo.currentData())
            policy = self.capabilities.policy_steps.get(policy_id, {})
            return CustomStep(
                action, page=int(policy.get("page", 0)), arg0=policy_id,
                page_wait_mode=wait_mode,
                step_interval_ms=int(self.step_interval_combo.currentData()),
                run_once=int(self.run_once_check.isChecked()),
                **check_kwargs,
            )
        if action == ACTION_VIDEO_RECORD:
            return CustomStep(
                action,
                arg0=int(self.record_resolution_combo.currentData()),
                arg1=int(self.record_fps_combo.currentData()),
                arg2=self._duration_seconds(
                    self.record_seconds_spin, self.record_duration_unit_combo,
                ),
                page_wait_mode=wait_mode,
                step_interval_ms=int(self.step_interval_combo.currentData()),
                run_once=int(self.run_once_check.isChecked()),
                video_canvas=int(self.record_canvas_combo.currentData()),
                **check_kwargs,
            )
        if action == ACTION_SLOW_MOTION_RECORD:
            return CustomStep(
                action,
                page_wait_mode=wait_mode,
                step_interval_ms=int(self.step_interval_combo.currentData()),
                run_once=int(self.run_once_check.isChecked()),
                params=(
                    int(self.slow_motion_resolution_combo.currentData()),
                    int(self.slow_motion_rate_combo.currentData()),
                    self._duration_seconds(
                        self.slow_motion_seconds_spin, self.slow_motion_duration_unit_combo,
                    ),
                    0,
                ),
                **check_kwargs,
            )
        if action == ACTION_SCREEN_ROTATE:
            return CustomStep(
                action,
                arg0=int(self.screen_rotation_combo.currentData()),
                page_wait_mode=wait_mode,
                step_interval_ms=int(self.step_interval_combo.currentData()),
                run_once=int(self.run_once_check.isChecked()),
                **check_kwargs,
            )
        return CustomStep(action, page_wait_mode=wait_mode,
                          step_interval_ms=int(self.step_interval_combo.currentData()),
                          run_once=int(self.run_once_check.isChecked()), **check_kwargs)

    def _step_text(self, step: CustomStep) -> str:
        label = ACTION_LABELS.get(step.action, f"操作 {step.action}")
        if step.action == ACTION_NAV:
            label += f": {_custom_display_label(self.capabilities.safe_pages.get(step.page, step.page))}"
        elif step.action == ACTION_NAV_TARGET:
            target = self.capabilities.page_targets.get(step.page)
            label += f": {_custom_display_label(target.label if target else step.page)}"
        elif step.action == ACTION_MODE:
            label += f": {_custom_display_label(self.capabilities.mode_options.get(step.arg0, step.arg0))}"
        elif step.action == ACTION_VIDEO:
            label += f": {_custom_display_label(self.capabilities.video_presets.get(step.arg0, step.arg0))}"
        elif step.action == ACTION_VERIFY:
            label += f": {_custom_display_label(self.capabilities.verify_options.get(step.arg0, step.arg0))}"
        elif step.action in (ACTION_CLICK, ACTION_SWIPE, ACTION_SLIDER):
            label += f": {_custom_display_label(self.capabilities.policy_steps.get(step.arg0, {}).get('label', step.arg0))}"
        elif step.action == ACTION_VIDEO_RECORD:
            profile = self.capabilities.video_profiles.get((step.video_canvas, step.arg0))
            fps = profile.fps.get(step.arg1, step.arg1) if profile else step.arg1
            orientation = "竖屏 9:16" if step.video_canvas == VIDEO_CANVAS_PORTRAIT else "横屏 16:9"
            label += f": {orientation} / {_custom_display_label(profile.resolution_label if profile else step.arg0)} / {_custom_display_label(fps)} / {_format_custom_record_duration(step.arg2)}"
        elif step.action == ACTION_SLOW_MOTION_RECORD:
            resolution = self.capabilities.slow_motion_resolutions.get(step.params[0], step.params[0])
            rate = self.capabilities.slow_motion_rates.get(step.params[1], step.params[1])
            label += f": 自适应方向 / {_custom_display_label(resolution)} / {_custom_display_label(rate)} / {_format_custom_record_duration(step.params[2])}"
        elif step.action == ACTION_SCREEN_ROTATE:
            label += ": 竖屏" if step.arg0 == 1 else ": 横屏"
        suffix = " + 额外页面等待" if step.page_wait_mode == PAGE_WAIT_ADDITIONAL else ""
        cadence = f"，完成后等待 {step.step_interval_ms} 毫秒"
        first_cycle = " [仅首轮]" if step.run_once else ""
        ui_checks = ""
        if step.check_ui_complete:
            ui_checks += " [检查UI完整]"
        if step.check_ui_frozen:
            ui_checks += " [检查UI卡死]"
        return label + suffix + cadence + first_cycle + ui_checks

    def _refresh_steps(self, selected_row: int | None = None) -> None:
        self.step_list.clear()
        for index, step in enumerate(self.steps, start=1):
            self.step_list.addItem(f"{index}. {self._step_text(step)}")
        if selected_row is not None and self.steps:
            self.step_list.setCurrentRow(min(max(selected_row, 0), len(self.steps) - 1))
        self._refresh_media_sections()

    def _refresh_media_sections(self) -> None:
        """Show only media policies that can apply to the current step list."""
        has_photo_step = any(step.action == ACTION_PHOTO_CAPTURE for step in self.steps)
        has_video_step = any(
            step.action in (ACTION_VIDEO_RECORD, ACTION_SLOW_MOTION_RECORD) for step in self.steps
        )
        self.photo_media_panel.setVisible(has_photo_step)
        self.video_media_panel.setVisible(has_video_step)
        self.media_group.setVisible(has_photo_step or has_video_step)

    @staticmethod
    def _profile_label(profile: CustomProfile, number: int) -> str:
        record_seconds = sum(
            step.arg2 if step.action == ACTION_VIDEO_RECORD else step.params[2]
            for step in profile.config.steps
            if step.action in (ACTION_VIDEO_RECORD, ACTION_SLOW_MOTION_RECORD)
        )
        media_text = f"录像 {_format_custom_record_duration(record_seconds)}/轮" if record_seconds else "无录像步骤"
        return f"方案 {number}｜{profile.name}｜{profile.config.cycles} 轮｜{media_text}"

    def _selected_profile(self) -> CustomProfile | None:
        profile_id = self.profile_combo.currentData()
        if not isinstance(profile_id, str) or not profile_id:
            return None
        return next((item for item in self._profiles if item.profile_id == profile_id), None)

    def _refresh_profile_combo(self, selected_id: str | None = None) -> None:
        current = self.profile_combo.currentData() if hasattr(self, "profile_combo") else None
        if selected_id is None and isinstance(current, str):
            selected_id = current
        self.profile_combo.blockSignals(True)
        self.profile_combo.clear()
        self.profile_combo.addItem("选择要载入或修改的方案", "")
        for number, profile in enumerate(self._profiles, start=1):
            self.profile_combo.addItem(self._profile_label(profile, number), profile.profile_id)
        selected_index = self.profile_combo.findData(selected_id)
        self.profile_combo.setCurrentIndex(max(selected_index, 0))
        self.profile_combo.blockSignals(False)
        self._profile_selection_changed()

    def _profile_selection_changed(self) -> None:
        profile = self._selected_profile()
        self.load_save_profile_btn.setEnabled(profile is not None)
        self.update_profile_btn.setEnabled(profile is not None)
        self.delete_profile_btn.setEnabled(profile is not None)
        if profile is not None:
            self.profile_name_edit.setText(profile.name)

    def _current_config_for_profile(self) -> CustomConfig | None:
        try:
            config = self._config_from_widgets()
            validate_config(config, self.capabilities)
            return config
        except (TypeError, CustomConfigError) as exc:
            self.validation_label.setText(custom_config_error_text(str(exc)))
            self.validation_label.setStyleSheet("color:#b91c1c;")
            return None

    def _save_profiles(self, profiles: list[CustomProfile], selected_id: str | None) -> bool:
        try:
            self._profile_store.save(profiles)
        except OSError as exc:
            self.validation_label.setText(f"保存 PC 方案失败：{exc}")
            self.validation_label.setStyleSheet("color:#b91c1c;")
            return False
        self._profiles = profiles
        self._refresh_profile_combo(selected_id)
        self.profiles_changed.emit(list(self._profiles))
        return True

    def _save_new_profile(self) -> None:
        config = self._current_config_for_profile()
        name = self.profile_name_edit.text().strip()
        if config is None:
            return
        if not name:
            self.validation_label.setText("请填写方案名称后再保存。")
            self.validation_label.setStyleSheet("color:#b91c1c;")
            return
        profile = self._profile_store.new(name, config)
        if not self._save_profiles([*self._profiles, profile], profile.profile_id):
            return
        self.validation_label.setText(
            f"已在 PC 保存“{profile.name}”；需要时点击“覆盖设备当前方案”或“载入并覆盖设备”下发。"
        )
        self.validation_label.setStyleSheet("color:#166534;")

    def _load_selected_profile(self) -> None:
        profile = self._selected_profile()
        if profile is None:
            self.validation_label.setText("请先选择一个已保存方案。")
            self.validation_label.setStyleSheet("color:#b91c1c;")
            return
        self._apply_config(profile.config)
        self.profile_name_edit.setText(profile.name)
        self._refresh_steps()
        self._refresh_validation()
        self.validation_label.setText(f"已载入“{profile.name}”；检查后点击“覆盖设备当前方案”使其生效。")
        self.validation_label.setStyleSheet("color:#166534;")

    def _load_and_save_selected_profile(self) -> None:
        """Load the selected PC profile and immediately send that exact config."""
        profile = self._selected_profile()
        if profile is None:
            self.validation_label.setText("请先选择一个已保存方案。")
            self.validation_label.setStyleSheet("color:#b91c1c;")
            return
        try:
            validate_config(profile.config, self.capabilities)
        except (TypeError, CustomConfigError) as exc:
            message = custom_config_error_text(str(exc))
            _APP_LOG.error(
                "custom_profile_local_validation_failed firmware_version=%s "
                "raw_error=%s friendly_error=%s "
                "media_manifest_supported=%s cleanup_supported=%s",
                self.firmware_version or "<unknown>",
                str(exc),
                message,
                self.capabilities.media_manifest_supported,
                self.capabilities.cleanup_supported,
            )
            self.validation_label.setText(message)
            self.validation_label.setStyleSheet("color:#b91c1c;")
            return
        self._apply_config(profile.config)
        self.profile_name_edit.setText(profile.name)
        self._refresh_steps()
        self._refresh_validation()
        self._request_save(False)

    def _update_selected_profile(self) -> None:
        profile = self._selected_profile()
        config = self._current_config_for_profile()
        name = self.profile_name_edit.text().strip()
        if profile is None or config is None:
            return
        if not name:
            self.validation_label.setText("请填写方案名称后再更新。")
            self.validation_label.setStyleSheet("color:#b91c1c;")
            return
        replacement = self._profile_store.replace(profile, name, config)
        profiles = list(self._profiles)
        profiles[profiles.index(profile)] = replacement
        if not self._save_profiles(profiles, replacement.profile_id):
            return
        self.validation_label.setText(f"已更新 PC 方案“{replacement.name}”。")
        self.validation_label.setStyleSheet("color:#166534;")

    def _delete_selected_profile(self) -> None:
        profile = self._selected_profile()
        if profile is None:
            return
        answer = QMessageBox.question(
            self, "删除方案", f"确定删除 PC 本地方案“{profile.name}”吗？设备当前配置不会受影响。",
            QMessageBox.Yes | QMessageBox.No, QMessageBox.No,
        )
        if answer != QMessageBox.Yes:
            return
        profiles = [item for item in self._profiles if item != profile]
        if not self._save_profiles(profiles, None):
            return
        self.profile_name_edit.clear()
        self.validation_label.setText(f"已删除 PC 方案“{profile.name}”。")
        self.validation_label.setStyleSheet("color:#166534;")

    def _add_step(self) -> None:
        try:
            step = self._step_from_editor()
        except (TypeError, CustomConfigError) as exc:
            message = custom_config_error_text(str(exc))
            _APP_LOG.error(
                "custom_step_local_validation_failed firmware_version=%s "
                "raw_error=%s friendly_error=%s "
                "media_manifest_supported=%s cleanup_supported=%s",
                self.firmware_version or "<unknown>",
                str(exc),
                message,
                self.capabilities.media_manifest_supported,
                self.capabilities.cleanup_supported,
            )
            self.validation_label.setText(message)
            return
        if len(self.steps) >= self.capabilities.max_steps:
            self.validation_label.setText(f"设备最多允许 {self.capabilities.max_steps} 个步骤。")
            return
        self.steps.append(step)
        self._refresh_steps(len(self.steps) - 1)
        self._refresh_validation()

    def _move_selected_step_up(self) -> None:
        self._move_selected_step(-1)

    def _move_selected_step_down(self) -> None:
        self._move_selected_step(1)

    def _move_selected_step(self, direction: int) -> None:
        row = self.step_list.currentRow()
        target = row + direction
        if not 0 <= row < len(self.steps) or not 0 <= target < len(self.steps):
            return
        self.steps[row], self.steps[target] = self.steps[target], self.steps[row]
        self._refresh_steps(target)
        self._refresh_validation()

    def _remove_selected_step(self) -> None:
        row = self.step_list.currentRow()
        if 0 <= row < len(self.steps):
            del self.steps[row]
            self._refresh_steps(row)
            self._refresh_validation()

    def _clear_steps(self) -> None:
        self.steps.clear()
        self._refresh_steps()
        self._refresh_validation()

    def _cycles_changed(self, cycles: int) -> None:
        self._refresh_validation()

    def _check_mode_changed(self) -> None:
        pairs = (
            (self.photo_check_combo, self.photo_check_every_spin),
            (self.video_check_combo, self.video_check_every_spin),
        )
        for combo, spin in pairs:
            baseline = combo.currentData() == CHECK_BASELINE
            if baseline:
                spin.setValue(0)
            spin.setEnabled(not baseline)
        self._refresh_validation()

    def _config_from_widgets(self) -> CustomConfig:
        has_photo_step = any(step.action == ACTION_PHOTO_CAPTURE for step in self.steps)
        has_video_step = any(
            step.action in (ACTION_VIDEO_RECORD, ACTION_SLOW_MOTION_RECORD) for step in self.steps
        )
        return CustomConfig(
            cycles=self.cycles_spin.value(),
            steps=list(self.steps),
            page_settle_ms=int(self.interval_combos["page_settle_ms"].currentData()),
            step_interval_ms=0,
            media_interval_ms=self._compatibility_media_interval_ms,
            cycle_interval_ms=int(self.interval_combos["cycle_interval_ms"].currentData()),
            photo_check_mode=int(self.photo_check_combo.currentData()) if has_photo_step else CHECK_BASELINE,
            photo_check_every_cycles=self.photo_check_every_spin.value() if has_photo_step else 0,
            video_check_mode=int(self.video_check_combo.currentData()) if has_video_step else CHECK_BASELINE,
            video_check_every_cycles=self.video_check_every_spin.value() if has_video_step else 0,
            photo_cleanup_every_cycles=self.photo_cleanup_every_spin.value() if has_photo_step else 0,
            video_cleanup_every_cycles=self.video_cleanup_every_spin.value() if has_video_step else 0,
            cleanup_before_wait_index=int(self.cleanup_wait_combos[0].currentData()),
            cleanup_between_wait_index=int(self.cleanup_wait_combos[1].currentData()),
            cleanup_after_wait_index=int(self.cleanup_wait_combos[2].currentData()),
        )

    def _apply_combo_value(self, combo: QComboBox, value: int) -> None:
        index = combo.findData(value)
        if index >= 0:
            combo.setCurrentIndex(index)

    def _apply_config(self, config: CustomConfig) -> None:
        self._loading = True
        self.steps = list(config.steps)
        self.cycles_spin.setValue(config.cycles)
        for field, combo in self.interval_combos.items():
            self._apply_combo_value(combo, int(getattr(config, field)))
        self._apply_combo_value(self.photo_check_combo, config.photo_check_mode)
        self._apply_combo_value(self.video_check_combo, config.video_check_mode)
        self.photo_check_every_spin.setValue(config.photo_check_every_cycles)
        self.video_check_every_spin.setValue(config.video_check_every_cycles)
        self.photo_cleanup_every_spin.setValue(config.photo_cleanup_every_cycles)
        self.video_cleanup_every_spin.setValue(config.video_cleanup_every_cycles)
        for combo, value in zip(self.cleanup_wait_combos, (
            config.cleanup_before_wait_index,
            config.cleanup_between_wait_index,
            config.cleanup_after_wait_index,
        )):
            self._apply_combo_value(combo, value)
        self._loading = False
        self._check_mode_changed()

    def _refresh_validation(self) -> None:
        if self._loading:
            return
        try:
            config = self._config_from_widgets()
            legacy_steps = [
                index for index, step in enumerate(config.steps, start=1)
                if step.action in _LEGACY_CUSTOM_ACTIONS
            ]
            if legacy_steps:
                raise CustomConfigError(
                    "legacy VIDEO step found at step(s) "
                    + ", ".join(str(index) for index in legacy_steps)
                    + "; remove it and add a VIDEO_RECORD step"
                )
            validate_config(config, self.capabilities)
        except (TypeError, CustomConfigError) as exc:
            self.validation_label.setText(custom_config_error_text(str(exc)))
            self.validation_label.setStyleSheet("color:#b91c1c;")
            self.save_btn.setEnabled(False)
            self.save_run_btn.setEnabled(False)
        else:
            self._refresh_preflight_summary(config)
            self.validation_label.setText("本地配置校验通过。保存时设备将再次校验总时长和存储空间。")
            self.validation_label.setStyleSheet("color:#166534;")
            self.save_btn.setEnabled(True)
            self.save_run_btn.setEnabled(True)

    @staticmethod
    def _format_duration(milliseconds: int) -> str:
        total_seconds = max(0, milliseconds) // 1000
        days, remainder = divmod(total_seconds, 24 * 60 * 60)
        hours, remainder = divmod(remainder, 60 * 60)
        minutes, seconds = divmod(remainder, 60)
        if days:
            return f"{days}天{hours}小时{minutes}分"
        if hours:
            return f"{hours}小时{minutes}分{seconds}秒"
        if minutes:
            return f"{minutes}分{seconds}秒"
        return f"{seconds}秒"

    def _refresh_preflight_summary(self, config: CustomConfig) -> None:
        estimated_ms = custom_c01_estimated_runtime_ms(config, self.capabilities)
        photos, videos = custom_c01_media_artifact_counts(config)
        budget_bytes = custom_c01_storage_budget_bytes(config, self.capabilities)
        tracked = custom_c01_max_tracked_media_artifacts(config)
        text = (
            f"设备同公式预计运行：{self._format_duration(estimated_ms)}；"
            f"本次请求媒体：照片 {photos}、视频 {videos}；"
            f"单个清理窗口最大驻留：{(budget_bytes + 1024 * 1024 - 1) // (1024 * 1024)} MiB；"
            f"最大追踪主文件：{tracked}。"
        )
        available = self.capabilities.available_storage_bytes
        safe_free = self.capabilities.safe_free_storage_bytes
        if (available is not None and safe_free is not None and budget_bytes > 0 and
                available < safe_free + 2 * budget_bytes):
            text += " 当前空间可运行，但存储余量较小；建议提高清理频率或减少媒体步骤。"
            self.preflight_summary_label.setStyleSheet("color:#b45309;")
        else:
            self.preflight_summary_label.setStyleSheet("color:#475569;")
        self.preflight_summary_label.setText(text)

    def _request_save(self, run_after: bool) -> None:
        try:
            config = self._config_from_widgets()
            if any(step.action in _LEGACY_CUSTOM_ACTIONS for step in config.steps):
                raise CustomConfigError(
                    "legacy VIDEO step is not supported for new saves; "
                    "remove it and add a VIDEO_RECORD step"
                )
            validate_config(config, self.capabilities)
        except (TypeError, CustomConfigError) as exc:
            message = custom_config_error_text(str(exc))
            _APP_LOG.error(
                "custom_config_local_validation_failed firmware_version=%s "
                "raw_error=%s friendly_error=%s media_manifest_supported=%s "
                "cleanup_supported=%s",
                self.firmware_version or "<unknown>",
                str(exc),
                message,
                self.capabilities.media_manifest_supported,
                self.capabilities.cleanup_supported,
            )
            self.validation_label.setText(message)
            return
        if self.revision > 0:
            follow_up = "保存并回读后将启动 C01。" if run_after else "不会启动测试。"
            answer = QMessageBox.question(
                self,
                "覆盖设备当前方案",
                f"设备端当前已保存修订版 {self.revision} 的 C01 方案。\n\n"
                "继续会原子覆盖该方案，无需先删除旧配置。"
                f"{follow_up}\n\n是否继续？",
                QMessageBox.Yes | QMessageBox.No,
                QMessageBox.No,
            )
            if answer != QMessageBox.Yes:
                self.validation_label.setText("已取消保存，设备当前 C01 方案未修改。")
                self.validation_label.setStyleSheet("color:#b45309;")
                return
        _APP_LOG.info(
            "custom_config_editor_save_clicked revision=%s steps=%s cycles=%s run_after=%s",
            self.revision, len(config.steps), config.cycles, run_after,
        )
        self.set_busy(True, "正在保存配置到设备…")
        self.save_requested.emit(config, self.revision, run_after)

    def set_busy(self, busy: bool, message: str = "") -> None:
        # Never disable the whole window while a request is in flight: users
        # must still be able to close a stale editor after a transport failure.
        for control in self._busy_controls:
            control.setEnabled(not busy)
        if not busy:
            if not self.cleanup_supported:
                self.photo_cleanup_every_spin.setEnabled(False)
                self.video_cleanup_every_spin.setEnabled(False)
                for combo in self.cleanup_wait_combos:
                    combo.setEnabled(False)
            self._check_mode_changed()
            self._profile_selection_changed()
        self._busy_timeout_timer.stop()
        if busy:
            self._busy_timeout_timer.start(15000)
        if message:
            self.validation_label.setText(message)
            self.validation_label.setStyleSheet("color:#0369a1;" if busy else "color:#b45309;")

    def _on_busy_timeout(self) -> None:
        """Safety net: if save chain hangs, re-enable the dialog after 15s."""
        self.set_busy(False, "保存配置超时，请检查设备连接后重试。")

    def showEvent(self, event) -> None:  # noqa: N802 - Qt API
        super().showEvent(event)
        _APP_LOG.info("custom_config_dialog_visible revision=%s", self.revision)

    def save_failed(self, message: str) -> None:
        """Restore the editor after a device-side rejection without hiding its reason."""
        self.set_busy(False)
        self._refresh_validation()
        self.validation_label.setText(message)
        self.validation_label.setStyleSheet("color:#b91c1c;")
        failed_step = re.search(r"设备拒绝第\s*(\d+)\s*步", message)
        if failed_step:
            row = int(failed_step.group(1)) - 1
            if 0 <= row < self.step_list.count():
                self.step_list.setCurrentRow(row)
                self.step_list.scrollToItem(self.step_list.currentItem())

    def save_succeeded(self, revision: int, crc: int | None, estimated_runtime_ms: int | None) -> None:
        self.revision = revision
        text = f"已保存修订版 {revision}"
        if crc is not None:
            text += f" (CRC {crc})"
        if estimated_runtime_ms is not None:
            text += f"；设备预计耗时 {estimated_runtime_ms / 1000:.1f} 秒"
        self.device_label.setText(text)
        self.set_busy(False)
        self._refresh_validation()

    def refresh_device_revision(
        self, revision: int, crc: int | None, estimated_runtime_ms: int | None,
    ) -> None:
        """Accept the current device revision without replacing the local draft."""
        self.revision = revision
        if revision == 0:
            text = "设备当前无已保存方案；已保留本地草稿，可再次保存。"
        else:
            text = f"已刷新设备修订版 {revision}"
            if crc is not None:
                text += f" (CRC {crc})"
            if estimated_runtime_ms is not None:
                text += f"；设备当前方案预计耗时 {estimated_runtime_ms / 1000:.1f} 秒"
            text += "；已保留本地草稿，请再次确认覆盖保存。"
        self.device_label.setText(text)
        self.set_busy(False)
        self._refresh_validation()

    def save_verified(
        self,
        config: CustomConfig,
        revision: int,
        crc: int | None,
        estimated_runtime_ms: int | None,
    ) -> None:
        """Replace the draft with the device's canonical saved configuration."""
        self._apply_config(config)
        self._refresh_steps()
        self.save_succeeded(revision, crc, estimated_runtime_ms)


def _label(text: str, width: int = 76) -> QLabel:
    label = QLabel(text)
    label.setFixedWidth(width)
    label.setStyleSheet("font-weight:600; color:#475569;")
    return label


# 手动设备操作的 purpose 值，命令完成后需立即恢复按钮状态。
_MANUAL_ACTION_PURPOSES = frozenset({
    "capture_photo", "record_start", "record_stop",
    "switch_ui_page", "gimbal_move", "swipe_screen",
})


class MainWindow(QMainWindow):
    DEVICE_COLUMNS = ["选择", "接口", "PC IP", "设备 IP", "连接", "用例", "状态", "进度", "最后消息", "备注"]
    _command_result_ready = Signal(str, str, bool, object, object)
    _command_error_ready = Signal(str, str, bool, str, object)
    _command_thread_finished = Signal(str, object, object)

    def __init__(self):
        super().__init__()
        self.setWindowTitle("SX Pocket TestAgent")
        self.resize(1080, 760)

        self.runtimes: dict[str, DeviceRuntime] = {}
        self._retired_runtimes: list[DeviceRuntime] = []
        self._discovery_thread: QThread | None = None
        self._discovery_worker: DiscoveryWorker | None = None
        self._discovery_bg_thread: threading.Thread | None = None
        self._discovery_auto_configure = False
        self.table_keys: list[str] = []
        self.current_key: str | None = None
        self.log_entries: list[Dict[str, str]] = []
        self.network_dialog: NetworkConfigDialog | None = None
        self._screenshot_worker: ScreenshotWorker | None = None
        self._crash_export_worker: CrashExportWorker | None = None
        self._screenshot_dialog: ScreenshotDialog | None = None
        self._screenshot_key: str | None = None
        self._screenshot_type = "fb0"
        self._manual_recording_keys: set[str] = set()
        self._manual_record_commands_inflight: set[str] = set()
        self._pending_manual_record_commands: dict[str, tuple[DeviceRuntime, str, float]] = {}
        self._custom_capabilities_by_key: dict[str, CustomCapabilities] = {}
        self._custom_profiles = CustomProfileStore(custom_profiles_path()).load()
        self._custom_profile_load_after_capabilities: dict[str, CustomProfile] = {}
        self._custom_saved_revision_by_key: dict[str, int] = {}
        self._custom_load_after_capabilities: set[str] = set()
        self._custom_revision_refresh_after_conflict: set[str] = set()
        self._catalog_refresh_after_probe: set[str] = set()
        self._custom_save_run_after: dict[str, bool] = {}
        self._custom_verify_after_save: dict[str, tuple[int, int]] = {}
        self._custom_dialog_key: str | None = None
        self.custom_config_dialog: CustomConfigDialog | None = None
        self._ota_worker: OTAUpgradeWorker | None = None
        self._ota_post_worker: OTAPostWorker | None = None
        self._batch_ota_worker: OTABatchWorker | None = None
        self._sd_clean_worker: SDSDCleanWorker | None = None
        self._auto_config_thread: QThread | None = None
        self._active_probe_count: int = 0
        self._max_active_probes: int = 2
        self._status_refresh_after_device_refresh = False
        self._status_refresh_retry_count = 0
        self._auto_config_worker: BatchConfigureWorker | None = None
        self._preview_active = False
        self._closing = False
        self._status_dirty = False
        self._log_pending = False
        self._log_last_mode = ""
        self._log_last_snapshot: list[str] = []
        self._ui_refresh_pending = False
        self._REFRESH_DEBOUNCE_MS = 80

        # CommandWorker lives in a QThread.  Do not connect its signals to
        # capturing lambdas that manipulate widgets: PySide cannot associate
        # such callbacks with this window's thread.  Relay first, then force
        # the actual UI handlers onto the main thread.
        self._command_result_ready.connect(self._on_command_result, Qt.QueuedConnection)
        self._command_error_ready.connect(self._on_command_error, Qt.QueuedConnection)
        self._command_thread_finished.connect(self._thread_cleanup, Qt.QueuedConnection)

        self._worker_poll_timer = QTimer(self)
        self._worker_poll_timer.timeout.connect(self._poll_workers)
        self._worker_poll_timer.start(50)
        self._status_flush_timer = QTimer(self)
        self._status_flush_timer.timeout.connect(self._flush_status_updates)
        self._status_flush_timer.start(500)
        self._probe_timer = QTimer(self)
        self._probe_timer.timeout.connect(self._probe_configured_runtimes)

        self.summary_label = QLabel("设备 0  |  在线 0  |  运行 0  |  异常 0")
        self.summary_label.setStyleSheet("font-size:13px; font-weight:700; color:#334155;")
        self.refresh_btn = _make_button("刷新设备", "#2563eb")
        self.network_btn = _make_button("设备连接", "#0f766e")
        self.ota_btn = _make_button("OTA升级", "#dc2626")
        self.batch_ota_btn = _make_button("一键OTA", "#b91c1c")
        self.sd_clean_btn = _make_button("清理SD卡", "#ea580c")
        self.refresh_btn.clicked.connect(self.refresh_and_configure_devices)
        self.network_btn.clicked.connect(self.open_network_config)
        self.ota_btn.clicked.connect(self.start_ota)
        self.batch_ota_btn.clicked.connect(self.start_batch_ota)
        self.sd_clean_btn.clicked.connect(self.start_sd_clean)

        self.device_table = QTableWidget(0, len(self.DEVICE_COLUMNS))
        self.device_table.setHorizontalHeaderLabels(self.DEVICE_COLUMNS)
        self.device_table.setSelectionBehavior(QAbstractItemView.SelectRows)
        self.device_table.setSelectionMode(QAbstractItemView.SingleSelection)
        self.device_table.setEditTriggers(QAbstractItemView.DoubleClicked | QAbstractItemView.EditKeyPressed)
        self.device_table.cellChanged.connect(self._on_table_cell_changed)
        self.device_table.setAlternatingRowColors(True)
        self.device_table.verticalHeader().setVisible(False)
        self.device_table.verticalHeader().setDefaultSectionSize(30)
        header = self.device_table.horizontalHeader()
        for column in range(8):
            header.setSectionResizeMode(column + 1, QHeaderView.ResizeToContents)
        header.setSectionResizeMode(8, QHeaderView.Interactive)
        header.resizeSection(8, 240)
        header.setSectionResizeMode(9, QHeaderView.Stretch)
        self.device_table.setColumnHidden(0, True)
        self.device_table.itemSelectionChanged.connect(self._on_table_selection_changed)

        self.selected_label = QLabel("未选择设备")
        self.selected_label.setStyleSheet("font-size:14px; font-weight:700; color:#1e293b;")
        self.version_label = QLabel("-")
        self.version_label.setStyleSheet("font-size:11px; color:#64748b;")
        self.version_label.setTextInteractionFlags(
            Qt.TextSelectableByMouse | Qt.TextSelectableByKeyboard,
        )
        self.version_label.setToolTip("可选中版本文本后复制")
        self.version_refresh_btn = _make_button("刷新", "#64748b")
        self.version_refresh_btn.setFixedSize(52, 26)
        self.version_refresh_btn.setToolTip("重新读取当前设备版本和能力信息")
        self.version_copy_btn = _make_button("复制", "#0f766e")
        self.version_copy_btn.setFixedSize(52, 26)
        self.version_copy_btn.setToolTip("复制当前设备版本")
        self.version_refresh_btn.clicked.connect(self.refresh_version_selected)
        self.version_copy_btn.clicked.connect(self.copy_version_selected)
        self.suite_combo = QComboBox()
        self._populate_suite_combo(FALLBACK_CASES, "stable_test")
        self.suite_combo.currentIndexChanged.connect(self._on_suite_changed)
        self.case_combo = QComboBox()
        self._populate_case_combo("stable_test", 1)
        self.case_combo.currentIndexChanged.connect(self._on_case_changed)
        self.custom_profile_label = _label("自定义方案")
        self.custom_profile_combo = QComboBox()
        self.custom_profile_combo.currentIndexChanged.connect(self._on_custom_profile_changed)
        self.progress_bar = QProgressBar()
        self.progress_bar.setRange(0, 100)
        self.progress_bar.setValue(0)
        self.progress_bar.setTextVisible(True)
        self.progress_text = QLabel("--/--")
        self.progress_text.setFixedWidth(72)
        self.status_label = QLabel("-")
        self.status_label.setStyleSheet("font-size:13px; font-weight:700; color:#64748b;")
        self.message_label = QLabel("-")
        self.message_label.setWordWrap(True)
        self.message_label.setStyleSheet("color:#475569;")

        self.custom_config_btn = _make_button("配置自定义步骤", "#0f766e")
        self.custom_config_btn.setToolTip("仅 custom_test / C01：从设备读取能力后，在 PC 配置并保存")
        self.stop_btn = _make_button("停止", "#dc2626")
        self.watch_btn = _make_button("开始监视", "#0284c7")
        self.record_btn = _make_button("开始运行", "#db2777")
        self.status_btn = _make_button("刷新状态", "#7c3aed")
        self.report_btn = _make_button("生成报告", "#059669")
        self.records_btn = _make_button("查看记录", "#4f46e5")
        self.device_folder_btn = _make_button("打开设备文件夹", "#2563eb")
        self.device_folder_btn.setToolTip("在系统文件管理器中打开当前设备的 FTP 文件夹")
        self.reboot_btn = _make_button("重启", "#dc2626")
        self.screen_combo = QComboBox()
        self.screen_combo.addItem("摄像头+UI (fb0)", "fb0")
        self.screen_combo.addItem("UI画面 (fb1)", "fb1")
        self.screenshot_btn = _make_button("截屏", "#0f766e")
        self.preview_btn = _make_button("屏幕预览", "#0369a1")
        self.export_crash_btn = _make_button("导出崩溃日志", "#9333ea")
        self.photo_action_btn = _make_button("拍照", "#0f766e")
        self.device_record_btn = _make_button("录像", "#be123c")
        self.device_record_btn.setToolTip("调用设备正式拍摄流程开始或停止录像")
        self.ui_page_combo = QComboBox()
        self.ui_page_combo.setMinimumWidth(118)
        for label, value in (
            ("主界面", "main"),
            ("控制中心", "control_center"),
            ("模式页", "mode"),
            ("视频参数", "video_params"),
            ("回放", "playback"),
            ("拍照参数", "photo_params"),
            ("系统设置", "settings"),
            ("云台模式", "gimbal_mode"),
        ):
            self.ui_page_combo.addItem(label, value)
        self.ui_page_combo.setToolTip("选择设备 UI 页面，点击“切换画面”后执行")
        self.switch_ui_page_btn = _make_button("切换画面", "#2563eb")
        self.gimbal_action_btn = _make_button("云台转动", "#7c3aed")
        self.gimbal_action_btn.setToolTip("选择方向后让设备云台短时间转动")
        self.swipe_screen_btn = _make_button("滑动屏幕", "#0f766e")
        self.swipe_screen_btn.setToolTip("选择方向后向设备当前页面注入一次滑动手势")
        self.custom_config_btn.clicked.connect(self.open_custom_config_selected)
        self.stop_btn.clicked.connect(self.stop_selected)
        self.watch_btn.clicked.connect(self.toggle_watch_selected)
        self.record_btn.clicked.connect(self.toggle_record_selected)
        self.status_btn.clicked.connect(self.status_selected)
        self.report_btn.clicked.connect(self.generate_report_selected)
        self.records_btn.clicked.connect(self.list_records)
        self.device_folder_btn.clicked.connect(self.open_device_folder)
        self.reboot_btn.clicked.connect(self.reboot_selected)
        self.screenshot_btn.clicked.connect(self.screenshot_selected)
        self.preview_btn.clicked.connect(self.toggle_preview_selected)
        self.export_crash_btn.clicked.connect(self.export_crash_selected)
        self.photo_action_btn.clicked.connect(self.capture_photo_selected)
        self.device_record_btn.clicked.connect(self.toggle_device_record_selected)
        self.switch_ui_page_btn.clicked.connect(self.switch_ui_page_selected)
        self.gimbal_action_btn.clicked.connect(self.gimbal_move_selected)
        self.swipe_screen_btn.clicked.connect(self.swipe_screen_selected)

        self.log_filter = QComboBox()
        self.log_filter.addItems(["当前设备", "全部设备", "仅错误"])
        self.log_filter.currentIndexChanged.connect(self.render_logs)
        self.log_toggle_btn = _make_button("收起日志", "#64748b")
        self.log_toggle_btn.clicked.connect(self.toggle_logs)
        self.log_view = QPlainTextEdit()
        self.log_view.setReadOnly(True)
        self.log_view.setMaximumBlockCount(MAX_LOG_ENTRIES)

        top_bar = QHBoxLayout()
        top_bar.addWidget(self.summary_label)
        top_bar.addStretch(1)
        top_bar.addWidget(self.refresh_btn)
        top_bar.addWidget(self.network_btn)
        top_bar.addWidget(self.ota_btn)
        top_bar.addWidget(self.batch_ota_btn)
        top_bar.addWidget(self.sd_clean_btn)

        table_group = QGroupBox("设备监控")
        table_layout = QVBoxLayout()
        table_layout.addWidget(self.device_table)
        table_group.setLayout(table_layout)

        detail_grid = QGridLayout()
        detail_grid.setHorizontalSpacing(8)
        detail_grid.setVerticalSpacing(8)
        detail_grid.addWidget(_label("版本"), 0, 0)
        version_row = QHBoxLayout()
        version_row.setContentsMargins(0, 0, 0, 0)
        version_row.setSpacing(4)
        version_row.addWidget(self.version_label, 1)
        version_row.addWidget(self.version_refresh_btn)
        version_row.addWidget(self.version_copy_btn)
        version_widget = QWidget()
        version_widget.setLayout(version_row)
        detail_grid.addWidget(version_widget, 0, 1, 1, 4)
        detail_grid.addWidget(_label("设备"), 1, 0)
        detail_grid.addWidget(self.selected_label, 1, 1, 1, 4)
        detail_grid.addWidget(_label("测试集"), 2, 0)
        detail_grid.addWidget(self.suite_combo, 2, 1, 1, 4)
        detail_grid.addWidget(_label("用例"), 3, 0)
        detail_grid.addWidget(self.case_combo, 3, 1, 1, 4)
        detail_grid.addWidget(self.custom_profile_label, 4, 0)
        detail_grid.addWidget(self.custom_profile_combo, 4, 1, 1, 4)
        detail_grid.addWidget(_label("进度"), 5, 0)
        detail_grid.addWidget(self.progress_bar, 5, 1, 1, 3)
        detail_grid.addWidget(self.progress_text, 5, 4)
        detail_grid.addWidget(_label("状态"), 6, 0)
        detail_grid.addWidget(self.status_label, 6, 1)
        detail_grid.addWidget(self.message_label, 6, 2, 1, 3)

        command_row = QHBoxLayout()
        for button in (
            self.custom_config_btn,
            self.stop_btn,
            self.watch_btn,
            self.record_btn,
            self.status_btn,
            self.records_btn,
            self.report_btn,
            self.device_folder_btn,
            self.reboot_btn,
            self.screen_combo,
            self.screenshot_btn,
            self.preview_btn,
            self.export_crash_btn,
        ):
            command_row.addWidget(button)
        command_row.addStretch(1)
        detail_layout = QVBoxLayout()
        detail_layout.addLayout(detail_grid)
        detail_layout.addLayout(command_row)
        detail_group = QGroupBox("选中设备")
        detail_group.setLayout(detail_layout)

        main_panel = QWidget()
        main_layout = QVBoxLayout()
        main_layout.setSpacing(10)
        main_layout.addLayout(top_bar)
        main_layout.addWidget(table_group, 1)
        main_layout.addWidget(detail_group)
        main_panel.setLayout(main_layout)

        # Log toggle bar (always visible at bottom of main panel)
        log_bar = QHBoxLayout()
        log_bar.addWidget(QLabel("日志"))
        log_bar.addWidget(self.photo_action_btn)
        log_bar.addWidget(self.device_record_btn)
        log_bar.addWidget(self.ui_page_combo)
        log_bar.addWidget(self.switch_ui_page_btn)
        log_bar.addWidget(self.gimbal_action_btn)
        log_bar.addWidget(self.swipe_screen_btn)
        log_bar.addStretch(1)
        log_bar.addWidget(self.log_toggle_btn)
        main_layout.addLayout(log_bar)

        # Collapsible log panel (in splitter)
        log_header = QHBoxLayout()
        log_header.addWidget(QLabel("过滤"))
        log_header.addWidget(self.log_filter)
        log_header.addStretch(1)
        log_panel = QWidget()
        log_layout = QVBoxLayout()
        log_layout.setContentsMargins(0, 0, 0, 0)
        log_layout.addLayout(log_header)
        log_layout.addWidget(self.log_view)
        log_panel.setLayout(log_layout)
        self.log_panel = log_panel

        self.splitter = QSplitter(Qt.Vertical)
        self.splitter.addWidget(main_panel)
        self.splitter.addWidget(log_panel)
        self.splitter.setSizes([560, 180])

        self.nav_list = QListWidget()
        self.nav_list.setFixedWidth(100)
        self.nav_list.setStyleSheet(
            "QListWidget { background:#0f172a; border:none; border-right:1px solid #1e293b;"
            "font-size:13px; color:#cbd5e1; outline:0; }"
            "QListWidget::item { height:40px; padding:0 12px;"
            "border-left:3px solid transparent; }"
            "QListWidget::item:hover { background:#1e293b; color:#f1f5f9; }"
            "QListWidget::item:selected { background:#1d4ed8; color:#ffffff;"
            "border-left:3px solid #38bdf8; }"
        )
        self.nav_list.addItem("设备测试")
        self.nav_list.addItem("App 测试")
        self.nav_list.addItem("OTG 传输")
        self.nav_list.addItem("使用帮助")
        self.nav_list.currentRowChanged.connect(self._on_nav_changed)

        self.app_test_page = AppTestPage(parent=self, main_window=self)
        self.otg_page = OtgSection()
        self.help_page = HelpPage()
        self.stack = QStackedWidget()
        self.stack.addWidget(self.splitter)
        self.stack.addWidget(self.app_test_page)
        self.stack.addWidget(self.otg_page)
        self.stack.addWidget(self.help_page)

        self.nav_shell = QWidget()
        nav_layout = QHBoxLayout()
        nav_layout.setContentsMargins(0, 0, 0, 0)
        nav_layout.setSpacing(0)
        nav_layout.addWidget(self.nav_list)
        nav_layout.addWidget(self.stack, 1)
        self.nav_shell.setLayout(nav_layout)
        self.setCentralWidget(self.nav_shell)

        self.setStyleSheet(
            "QMainWindow { background:#f1f5f9; }"
            "QGroupBox { background:#ffffff; border:1px solid #cbd5e1; border-radius:8px;"
            "margin-top:12px; padding:12px; font-weight:700; color:#334155; }"
            "QGroupBox::title { subcontrol-origin:margin; left:12px; padding:0 5px; }"
            "QTableWidget { background:#ffffff; border:1px solid #dbe3ed; border-radius:6px;"
            "gridline-color:#e2e8f0; font-size:12px; }"
            "QHeaderView::section { background:#f8fafc; color:#475569; border:none;"
            "border-bottom:1px solid #dbe3ed; padding:7px; font-weight:700; }"
            "QComboBox { min-height:24px; padding:3px 8px; border:1px solid #cbd5e1; border-radius:4px; }"
            "QProgressBar { border:1px solid #cbd5e1; border-radius:5px; height:20px;"
            "text-align:center; background:#e2e8f0; }"
            "QProgressBar::chunk { background:#16a34a; border-radius:4px; }"
            "QPlainTextEdit { font-family:monospace; font-size:12px; background:#1e293b;"
            "color:#f1f5f9; border:1px solid #475569; border-radius:6px; }"
        )

        # Device discovery and TestAgent TCP traffic belong only to this page.
        self.nav_list.setCurrentRow(0)

    # --- Device discovery and table -------------------------------------------------
    def refresh_devices(
        self,
        discovered: list[Dict[str, Any]] | None = None,
        *,
        probe: bool = True,
    ) -> None:
        if discovered is None:
            self._start_device_discovery(auto_configure=False)
            return
        discovered_keys = {
            str(item.get("link_id") or item.get("adapter_id") or item["pc_ip"])
            for item in discovered
        }

        for key in list(self.runtimes):
            if key not in discovered_keys:
                runtime = self.runtimes.pop(key)
                self._manual_record_commands_inflight.discard(key)
                self._pending_manual_record_commands.pop(key, None)
                self._retire_runtime(runtime)
                self._add_log(runtime, "网络链路已移除", "error")

        for item in discovered:
            link = dict(item)
            link_key = str(link.get("link_id") or link.get("adapter_id") or link["pc_ip"])
            runtime = self.runtimes.get(link_key)
            if runtime is None:
                runtime = DeviceRuntime(
                    iface=item["iface"],
                    pc_ip=item["pc_ip"],
                    device_ip=item["device_ip"],
                    link=link,
                    configured=bool(item.get("configured", False)),
                    notes=get_device_note(link.get("adapter_id", "")),
                )
                self.runtimes[runtime.key] = runtime
                self._add_log(runtime, "发现设备链路")
            else:
                runtime.iface = item["iface"]
                runtime.pc_ip = item["pc_ip"]
                runtime.device_ip = item["device_ip"]
                runtime.link = link
                runtime.configured = bool(item.get("configured", False))
                runtime.link_invalid = False  # 重新发现设备，清除失效标记

        if self.current_key not in self.runtimes:
            self.current_key = next(iter(self.runtimes), None)

        _APP_LOG.info(
            "gui_refresh_devices discovered=%s runtimes=%s probe=%s keys=%s",
            len(discovered), len(self.runtimes), probe, list(self.runtimes),
        )
        self._schedule_ui_sync()
        self._select_current_row()
        if probe:
            self._probe_configured_runtimes(skip_watched=True)
        self._schedule_ui_sync()

    def refresh_and_configure_devices(self) -> None:
        if self._auto_config_thread is not None:
            return
        if self.network_dialog is not None and self.network_dialog._thread is not None:
            QMessageBox.warning(self, "刷新设备", "手动网络配置正在执行，请完成后再自动配置")
            return
        if self._closing or not self._device_test_page_active():
            return
        if self._discovery_bg_thread is not None and self._discovery_bg_thread.is_alive():
            return

        self._status_refresh_after_device_refresh = True
        self._status_refresh_retry_count = 0
        self._start_device_discovery(auto_configure=True)

    def _start_device_discovery(self, *, auto_configure: bool) -> None:
        if self._closing:
            return
        if not self._device_test_page_active():
            return
        if self._discovery_bg_thread is not None and self._discovery_bg_thread.is_alive():
            return
        self._discovery_auto_configure = auto_configure
        if auto_configure:
            self.refresh_btn.setEnabled(False)
            self.network_btn.setEnabled(False)
            self.refresh_btn.setText("正在发现设备...")
        self._discovery_worker = DiscoveryWorker()
        self._discovery_worker.result.connect(self._on_device_discovered, Qt.QueuedConnection)
        self._discovery_worker.error.connect(self._on_device_discovery_error, Qt.QueuedConnection)
        self._discovery_worker.finished.connect(
            self._device_discovery_finished, Qt.QueuedConnection,
        )
        self._discovery_bg_thread = threading.Thread(
            target=self._discovery_worker.run, daemon=True,
        )
        self._discovery_bg_thread.start()

    def _on_device_discovered(self, discovered: list[Dict[str, Any]]) -> None:
        if self._closing:
            return
        auto_configure = self._discovery_auto_configure
        # 自动配对前不要先对旧地址发 agent_info；多设备同时刷新时，
        # 这会与 set_ip/重连竞争同一条 RNDIS 链路。
        self.refresh_devices(discovered, probe=not auto_configure)
        _APP_LOG.info("gui_discovery_consumed links=%s auto_configure=%s",
                      len(discovered), auto_configure)
        if not auto_configure:
            self._schedule_status_after_device_refresh()
            return
        pending = list(discovered)
        if not pending:
            if not discovered:
                self.refresh_btn.setEnabled(True)
                self.network_btn.setEnabled(True)
                self.refresh_btn.setText("刷新设备")
                QMessageBox.warning(
                    self, "自动设备连接",
                    "未发现 RNDIS/USB 网络适配器。请确认设备已通过 USB 连接且 Windows 已枚举网卡。",
                )
                return
            for runtime in self.runtimes.values():
                self._add_log(runtime, "刷新完成：设备网络已配置，正在探活")
            self.refresh_btn.setEnabled(True)
            self.network_btn.setEnabled(True)
            self.refresh_btn.setText("刷新设备")
            self._schedule_status_after_device_refresh()
            return

        self.refresh_btn.setText("正在自动配置...")
        if self.network_dialog is not None:
            self.network_dialog.configure_btn.setEnabled(False)

        self._auto_config_thread = QThread(self)
        self._auto_config_worker = BatchConfigureWorker(pending)
        self._auto_config_worker.moveToThread(self._auto_config_thread)
        self._auto_config_thread.started.connect(self._auto_config_worker.run)
        self._auto_config_worker.result.connect(self._on_auto_config_result, Qt.QueuedConnection)
        self._auto_config_worker.finished.connect(
            self._on_auto_config_finished, Qt.QueuedConnection,
        )
        self._auto_config_worker.finished.connect(self._auto_config_worker.deleteLater)
        self._auto_config_worker.finished.connect(self._auto_config_thread.quit)
        self._auto_config_thread.finished.connect(self._auto_config_thread.deleteLater)
        self._auto_config_thread.finished.connect(
            self._auto_config_thread_done, Qt.QueuedConnection,
        )
        self._auto_config_thread.start()

    def _on_device_discovery_error(self, message: str) -> None:
        if self._closing:
            return
        _APP_LOG.error("gui_discovery_error message=%s", message)
        if self._discovery_auto_configure:
            self.refresh_btn.setEnabled(True)
            self.network_btn.setEnabled(True)
            self.refresh_btn.setText("刷新设备")
            self._status_refresh_after_device_refresh = False
            self._status_refresh_retry_count = 0
        runtime = self.current_runtime()
        if runtime is not None:
            self._add_log(runtime, f"发现设备失败: {message}", "error")

    def _device_discovery_finished(self) -> None:
        self._discovery_thread = None
        self._discovery_worker = None
        self._discovery_bg_thread = None

    def _on_auto_config_result(self, payload: Dict[str, Any]) -> None:
        self._on_network_configured(payload, refresh=False)

    def _on_auto_config_finished(self, results: list[Dict[str, Any]]) -> None:
        succeeded = sum(bool(item.get("result", {}).get("success", False)) for item in results)
        failed = len(results) - succeeded
        _APP_LOG.info("gui_auto_config_finished total=%s succeeded=%s failed=%s",
                      len(results), succeeded, failed)
        self.refresh_btn.setEnabled(True)
        self.network_btn.setEnabled(True)
        self.refresh_btn.setText("刷新设备")
        if self.network_dialog is not None:
            self.network_dialog.configure_btn.setEnabled(True)
            self.network_dialog.refresh()
        self.refresh_devices()
        if failed:
            QMessageBox.warning(
                self, "自动设备连接",
                f"已连接 {succeeded} 台，失败 {failed} 台。请查看日志，并确认 RNDIS 网卡已手工配置为 192.168.1.x。",
            )
        else:
            QMessageBox.information(self, "自动设备连接", f"已完成 {succeeded} 台设备连接。")

    def _auto_config_thread_done(self) -> None:
        self._auto_config_thread = None
        self._auto_config_worker = None

    def _probe_configured_runtimes(self, *, skip_watched: bool = False) -> None:
        if not self._device_test_page_active():
            return
        for runtime in self.runtimes.values():
            if not runtime.configured or runtime.link_invalid:
                continue
            # 刷新设备时跳过已有 WatchWorker 的在线设备，避免探测与轮询争抢
            # _request_lock，6 台设备排队 37 秒。仅探新上线或无监视的设备。
            if skip_watched and runtime.watch_worker is not None and runtime.online is True:
                continue
            self._probe_runtime(runtime)

    def _schedule_status_after_device_refresh(self) -> None:
        """Refresh the selected device status once after a user device refresh."""
        if not self._status_refresh_after_device_refresh:
            return
        self._status_refresh_after_device_refresh = False
        self._status_refresh_retry_count = 0
        if self._closing:
            return
        QTimer.singleShot(500, self._refresh_status_after_device_refresh)

    def _refresh_status_after_device_refresh(self) -> None:
        if self._closing or not self._device_test_page_active():
            return
        runtime = self.current_runtime()
        if runtime is None:
            return
        if self._command_thread_alive(runtime):
            if self._status_refresh_retry_count < 80:
                self._status_refresh_retry_count += 1
                QTimer.singleShot(250, self._refresh_status_after_device_refresh)
            else:
                self._add_log(runtime, "刷新设备后的状态更新因设备探测持续占用而跳过", "error")
            return
        self._status_refresh_retry_count = 0
        self.status_selected()

    def _schedule_ui_sync(self) -> None:
        """Coalesce rapid _refresh_table / _update_summary calls into a single delayed invocation."""
        if self._ui_refresh_pending:
            return
        self._ui_refresh_pending = True
        QTimer.singleShot(self._REFRESH_DEBOUNCE_MS, self._do_ui_sync)

    def _do_ui_sync(self) -> None:
        self._ui_refresh_pending = False
        if self._closing:
            return
        try:
            self._refresh_table()
            self._update_summary()
        except RuntimeError:
            if not self._closing:
                raise

    def _refresh_table(self) -> None:
        if self._closing:
            return
        self.table_keys = list(self.runtimes)
        self.device_table.blockSignals(True)
        self.device_table.setRowCount(len(self.table_keys))
        for row, key in enumerate(self.table_keys):
            runtime = self.runtimes[key]

            check = QTableWidgetItem()
            check.setFlags(Qt.ItemIsUserCheckable | Qt.ItemIsEnabled | Qt.ItemIsSelectable)
            check.setCheckState(Qt.Unchecked)
            self.device_table.setItem(row, 0, check)

            values = [
                runtime.iface,
                runtime.pc_ip,
                runtime.device_ip,
                self._connection_text(runtime),
                self._case_short_title(runtime),
                runtime.status,
                self._progress_text(runtime),
                runtime.last_msg or "-",
                runtime.notes or "-",
            ]
            row_color = self._row_color(runtime)
            for column, value in enumerate(values):
                item = QTableWidgetItem(value)
                item.setToolTip(value)
                if column in (3, 4, 5, 6):
                    item.setTextAlignment(Qt.AlignCenter)
                if column == 3:
                    if runtime.online is True:
                        item.setBackground(QColor("#dcfce7"))
                        item.setForeground(QColor("#16a34a"))
                    elif runtime.online is False:
                        item.setBackground(QColor("#fee2e2"))
                        item.setForeground(QColor("#dc2626"))
                    else:
                        item.setBackground(QColor("#f1f5f9"))
                        item.setForeground(QColor("#64748b"))
                    font = item.font()
                    font.setBold(True)
                    item.setFont(font)
                elif row_color is not None:
                    item.setBackground(row_color)
                if column == 5:
                    color, _ = STATUS_COLORS.get(runtime.status, ("#475569", "#f8fafc"))
                    item.setForeground(QColor(color))
                if column == 8 and runtime.notes:
                    item.setForeground(QColor("#475569"))
                    font = item.font()
                    font.setItalic(True)
                    item.setFont(font)
                if column == 8:
                    item.setFlags(item.flags() | Qt.ItemIsEditable)
                else:
                    item.setFlags(item.flags() & ~Qt.ItemIsEditable)
                self.device_table.setItem(row, column + 1, item)
        if self.current_key in self.table_keys:
            self.device_table.selectRow(self.table_keys.index(self.current_key))
        self.device_table.blockSignals(False)

    def _select_current_row(self) -> None:
        if self.current_key in self.table_keys:
            row = self.table_keys.index(self.current_key)
            self.device_table.selectRow(row)
        self._update_detail_panel()

    def _on_table_selection_changed(self) -> None:
        selected = self.device_table.selectionModel().selectedRows()
        if not selected:
            return
        row = selected[0].row()
        if 0 <= row < len(self.table_keys):
            self.current_key = self.table_keys[row]
            self._update_detail_panel()
            self.render_logs()

    def _on_table_cell_changed(self, row: int, column: int) -> None:
        # Only handle备注 column (index 9, values index 8 → column+1 = 9)
        if column != 9:
            return
        if row < 0 or row >= len(self.table_keys):
            return
        key = self.table_keys[row]
        runtime = self.runtimes.get(key)
        if runtime is None:
            return
        item = self.device_table.item(row, column)
        if item is None:
            return
        new_note = item.text().strip()
        if new_note == runtime.notes:
            return
        runtime.notes = new_note
        adapter_id = runtime.link.get("adapter_id", "")
        if adapter_id:
            set_device_note(adapter_id, new_note)
        self._add_log(runtime, f"备注已更新: {new_note}" if new_note else "备注已清除", "info")

    # --- Detail panel ---------------------------------------------------------------
    def current_runtime(self) -> DeviceRuntime | None:
        return self.runtimes.get(self.current_key or "")

    def _update_detail_panel(self) -> None:
        runtime = self.current_runtime()
        self.suite_combo.blockSignals(True)
        self.case_combo.blockSignals(True)
        self.custom_profile_combo.blockSignals(True)
        if runtime is None:
            self.selected_label.setText("未选择设备")
            self.version_label.setText("-")
            self._populate_suite_combo(FALLBACK_CASES, "stable_test")
            self._populate_case_combo("stable_test", 1)
            self.case_combo.setCurrentIndex(0)
            self._refresh_custom_profile_combo(None)
            self.progress_bar.setValue(0)
            self.progress_bar.setFormat("")
            self.progress_text.setText("--/--")
            self.status_label.setText("-")
            self.message_label.setText("-")
            self._set_detail_actions_enabled(False)
            self.suite_combo.blockSignals(False)
            self.case_combo.blockSignals(False)
            self.custom_profile_combo.blockSignals(False)
            return

        self.selected_label.setText(
            f"{runtime.iface}  |  PC {runtime.pc_ip}  |  Device {runtime.device_ip}"
        )
        self.version_label.setText(runtime.firmware_version or "-")
        catalog = runtime.catalog or FALLBACK_CASES
        if runtime.suite not in catalog:
            runtime.suite = next(iter(catalog), "stable_test")
            runtime.case_id = 1
        self._populate_suite_combo(catalog, runtime.suite)
        self._populate_case_combo(runtime.suite, runtime.case_id)
        self._refresh_custom_profile_combo(runtime)
        self._set_progress(runtime)
        self.message_label.setText(runtime.last_msg or "-")
        self._set_status_label(runtime.status)
        self.watch_btn.setText("停止监视" if runtime.watch_worker is not None else "开始监视")
        self.record_btn.setText("停止运行" if runtime.record_worker is not None else "开始运行")
        self.device_record_btn.setText(
            "停止录像" if runtime.key in self._manual_recording_keys else "录像"
        )
        self._set_detail_actions_enabled(True)
        self.suite_combo.blockSignals(False)
        self.case_combo.blockSignals(False)
        self.custom_profile_combo.blockSignals(False)

    def _set_detail_actions_enabled(self, enabled: bool) -> None:
        runtime = self.current_runtime()
        enabled = enabled and runtime is not None and runtime.configured
        for button in (
            self.stop_btn,
            self.watch_btn,
            self.record_btn,
            self.status_btn,
            self.records_btn,
            self.report_btn,
            self.device_folder_btn,
            self.reboot_btn,
            self.suite_combo,
            self.case_combo,
            self.custom_profile_combo,
        ):
            button.setEnabled(enabled)
        online = enabled and runtime is not None and runtime.online is True
        self.version_refresh_btn.setEnabled(online)
        self.version_copy_btn.setEnabled(
            enabled and runtime is not None and bool(runtime.firmware_version.strip())
        )
        for control in (
            self.photo_action_btn,
            self.device_record_btn,
            self.ui_page_combo,
            self.switch_ui_page_btn,
            self.gimbal_action_btn,
            self.swipe_screen_btn,
        ):
            control.setEnabled(online)
        descriptor = self._selected_case_descriptor(runtime)
        selectable = descriptor is None or bool(descriptor.get("selectable", True))
        cancellable = descriptor is not None and bool(descriptor.get("cancellable", False))
        self.stop_btn.setEnabled(
            enabled and runtime is not None and cancellable and
            runtime.status in {"queued", "running", "stopping"}
        )
        self.stop_btn.setToolTip("" if cancellable else "当前用例没有可用的取消入口")
        is_custom_c01 = self._is_custom_c01(runtime)
        self.custom_profile_label.setVisible(is_custom_c01)
        self.custom_profile_combo.setVisible(is_custom_c01)
        self.custom_profile_combo.setEnabled(is_custom_c01 and online)
        self.custom_config_btn.setVisible(is_custom_c01)
        self.custom_config_btn.setEnabled(
            is_custom_c01 and online and runtime is not None and
            runtime.status not in {"queued", "running", "stopping"}
        )

    def _refresh_custom_profile_combo(self, runtime: DeviceRuntime | None) -> None:
        selected_id = self.custom_profile_combo.currentData()
        selected_id = selected_id if isinstance(selected_id, str) else ""
        self.custom_profile_combo.clear()
        self.custom_profile_combo.addItem("设备当前生效配置", "")
        if runtime is not None:
            for number, profile in enumerate(self._custom_profiles, start=1):
                self.custom_profile_combo.addItem(
                    CustomConfigDialog._profile_label(profile, number), profile.profile_id,
                )
        selected_index = self.custom_profile_combo.findData(selected_id)
        self.custom_profile_combo.setCurrentIndex(max(selected_index, 0))

    def _selected_custom_profile(self, runtime: DeviceRuntime | None) -> CustomProfile | None:
        if runtime is None:
            return None
        profile_id = self.custom_profile_combo.currentData()
        if not isinstance(profile_id, str) or not profile_id:
            return None
        return next(
            (profile for profile in self._custom_profiles
             if profile.profile_id == profile_id),
            None,
        )

    def _on_custom_profile_changed(self, _index: int) -> None:
        runtime = self.current_runtime()
        profile = self._selected_custom_profile(runtime)
        if runtime is not None and profile is not None:
            runtime.last_msg = (
                f"已选择 PC 方案“{profile.name}”；点击“配置自定义步骤”后可载入或直接覆盖设备当前方案。"
            )
            self.message_label.setText(runtime.last_msg)

    def _populate_case_combo(self, suite: str, case_id: int) -> None:
        runtime = self.current_runtime()
        catalog = runtime.catalog if runtime is not None else FALLBACK_CASES
        cases = catalog.get(suite, FALLBACK_CASES.get(suite, []))
        self.case_combo.clear()
        selected_index = 0
        for index, descriptor in enumerate(cases):
            current_id = int(descriptor["case_id"])
            self.case_combo.addItem(
                f"{current_id} - {descriptor['title']}", current_id,
            )
            if current_id == case_id:
                selected_index = index
        if cases:
            self.case_combo.setCurrentIndex(selected_index)

    def _populate_suite_combo(
        self, catalog: Dict[str, list[Dict[str, Any]]], selected_suite: str,
    ) -> None:
        self.suite_combo.clear()
        ordered_suites = sorted(
            catalog,
            key=lambda suite: (suite not in SUITE_LABELS, suite),
        )
        for suite in ordered_suites:
            self.suite_combo.addItem(SUITE_LABELS.get(suite, suite), suite)
        selected_index = self.suite_combo.findData(selected_suite)
        self.suite_combo.setCurrentIndex(max(selected_index, 0))

    def _on_suite_changed(self, index: int) -> None:
        runtime = self.current_runtime()
        if runtime is None:
            return
        suite = self.suite_combo.itemData(index)
        if not isinstance(suite, str) or suite not in runtime.catalog:
            return
        runtime.suite = suite
        runtime.case_id = 1
        self.case_combo.blockSignals(True)
        self._populate_case_combo(runtime.suite, runtime.case_id)
        self.case_combo.blockSignals(False)
        self._set_detail_actions_enabled(True)
        self._schedule_ui_sync()

    def _on_case_changed(self, index: int) -> None:
        runtime = self.current_runtime()
        if runtime is None:
            return
        selected_case_id = self.case_combo.itemData(index)
        if isinstance(selected_case_id, int) and selected_case_id > 0:
            runtime.case_id = selected_case_id
        self._set_detail_actions_enabled(True)
        self._schedule_ui_sync()

    # --- One-shot command handling --------------------------------------------------
    def _probe_runtime(self, runtime: DeviceRuntime) -> None:
        if not self._device_test_page_active():
            return
        if self._command_thread_alive(runtime):
            return
        if self._active_probe_count >= self._max_active_probes:
            return
        self._active_probe_count += 1
        self._start_command(
            runtime,
            {"cmd": "agent_info", "include_catalog": False},
            purpose="probe",
            quiet=True,
            timeout=FAST_PROBE_TIMEOUT,
        )

    def _command_thread_alive(self, runtime: DeviceRuntime) -> bool:
        """Return True only when the device has a genuinely running command.

        A finished thread whose reference was not cleared (the ``finished``
        signal can be lost when the thread object is deleted before its queued
        cleanup runs) is treated as not running, and the stale reference is
        cleared in place so later requests are not wrongly rejected.
        """
        thread = runtime.command_thread
        if thread is None:
            return False
        try:
            if thread.isRunning():
                return True
        except RuntimeError:
            pass
        _APP_LOG.warning("command_thread_stale key=%s", runtime.key)
        runtime.command_thread = None
        runtime.command_worker = None
        return False

    def _start_command(
        self,
        runtime: DeviceRuntime,
        payload: Dict[str, Any],
        *,
        purpose: str,
        quiet: bool = False,
        timeout: float = 3.0,
    ) -> bool:
        if self._closing:
            return False
        if self._command_thread_alive(runtime):
            self._add_log(runtime, "命令仍在执行，已忽略新请求", "error")
            return False

        thread = QThread(self)
        worker = CommandWorker(
            runtime.device_ip,
            runtime.port,
            runtime.pc_ip,
            payload,
            runtime.link,
            timeout=timeout,
        )
        runtime.command_thread = thread
        runtime.command_worker = worker
        worker.moveToThread(thread)
        thread.started.connect(worker.run)
        worker.result.connect(
            lambda reply, key=runtime.key, expected_runtime=runtime,
            cmd_purpose=purpose, is_quiet=quiet:
            self._command_result_ready.emit(
                key, cmd_purpose, is_quiet, reply, expected_runtime,
            ),
        )
        worker.error.connect(
            lambda message, key=runtime.key, expected_runtime=runtime,
            cmd_purpose=purpose, is_quiet=quiet:
            self._command_error_ready.emit(
                key, cmd_purpose, is_quiet, message, expected_runtime,
            ),
        )
        worker.finished.connect(worker.deleteLater)
        worker.finished.connect(thread.quit)
        thread.finished.connect(thread.deleteLater)
        thread.finished.connect(
            lambda key=runtime.key, t=thread, expected_runtime=runtime:
            self._emit_command_thread_finished(key, t, expected_runtime),
            Qt.QueuedConnection,
        )
        _APP_LOG.info(
            "command_thread_starting key=%s purpose=%s thread=%s",
            runtime.key, purpose, threading.current_thread().name,
        )
        thread.start()
        _APP_LOG.info(
            "command_thread_started key=%s purpose=%s thread=%s",
            runtime.key, purpose, threading.current_thread().name,
        )
        return True

    def _emit_command_thread_finished(
        self, key: str, thread: QThread, expected_runtime: DeviceRuntime,
    ) -> None:
        _APP_LOG.info("thread_finished_signal key=%s", key)
        self._command_thread_finished.emit(key, thread, expected_runtime)

    def _on_command_result(
        self,
        key: str,
        purpose: str,
        quiet: bool,
        reply: Dict[str, Any],
        expected_runtime: DeviceRuntime,
    ) -> None:
        callback_start = time.monotonic()
        _APP_LOG.info(
            "command_result_callback_enter key=%s purpose=%s code=%s quiet=%s "
            "configured=%s online=%s watch_present=%s callback_thread=%s",
            key,
            purpose,
            reply.get("code"),
            quiet,
            expected_runtime.configured,
            expected_runtime.online,
            expected_runtime.watch_worker is not None,
            threading.current_thread().name,
        )
        runtime = self.runtimes.get(key)
        if runtime is not expected_runtime:
            _APP_LOG.info("command_result_callback_skip_runtime_mismatch key=%s", key)
            return
        runtime.online = True
        runtime.transport_error_streak = 0
        if purpose in {
            "custom_capabilities", "custom_config_load", "custom_config_save",
            "custom_config_verify_save",
        }:
            _APP_LOG.info(
                "custom_protocol_reply key=%s device_ip=%s firmware_version=%s purpose=%s "
                "expected_config_version=%s expected_policy_version=%s diagnostics=%s",
                key,
                runtime.device_ip,
                runtime.firmware_version or "<unknown>",
                purpose,
                CUSTOM_CONFIG_VERSION,
                CUSTOM_POLICY_VERSION,
                _custom_capability_diagnostics(reply),
            )
        if purpose in {"record_start", "record_stop"}:
            self._manual_record_commands_inflight.discard(key)
        if purpose in {"probe", "catalog_refresh"} and reply.get("code") == 0:
            firmware_version = reply.get("firmware_version", "")
            runtime.firmware_version = firmware_version.strip() if isinstance(firmware_version, str) else ""
            catalog_included = reply.get("catalog_included", True) is True
            catalog_complete = reply.get("catalog_complete", False) is True
            if catalog_included and catalog_complete:
                runtime.catalog = catalog_from_agent_info(reply)
                runtime.catalog_firmware_version = runtime.firmware_version
                runtime.catalog_loaded = True
            elif purpose == "probe" and reply.get("ui_bridge_available") is True and (
                not runtime.catalog_loaded or
                runtime.catalog_firmware_version != runtime.firmware_version
            ):
                self._catalog_refresh_after_probe.add(key)
            if purpose == "probe" and "recording" in reply:
                if reply.get("recording") is True:
                    self._manual_recording_keys.add(key)
                    runtime.recording_start_pending = False
                elif not runtime.recording_start_pending:
                    self._manual_recording_keys.discard(key)
            if catalog_included and catalog_complete and runtime.suite not in runtime.catalog:
                runtime.suite = next(iter(runtime.catalog), "stable_test")
                runtime.case_id = 1
            _APP_LOG.info(
                "command_result_probe_processed key=%s purpose=%s catalog_included=%s "
                "catalog_complete=%s catalog_loaded=%s",
                key, purpose, catalog_included, catalog_complete, runtime.catalog_loaded,
            )
        if purpose == "run" and reply.get("code") == 0:
            runtime.status = "queued"
            runtime.last_msg = "已发送运行命令"
        elif purpose == "stop" and reply.get("code") == 0:
            runtime.status = "stopping"
            runtime.last_msg = "已发送停止命令"
        elif purpose == "reboot" and reply.get("code") == 0:
            self._manual_recording_keys.discard(key)
            runtime.recording_start_pending = False
            runtime.last_msg = "设备即将重启"
        elif purpose == "record_start" and reply.get("code") == 0:
            self._manual_recording_keys.add(key)
            runtime.recording_start_pending = True
            runtime.last_msg = "设备已请求开始录像"
            if not self._closing:
                QTimer.singleShot(
                    3000,
                    lambda record_key=key, expected_runtime=runtime:
                    self._reconcile_pending_record_start(record_key, expected_runtime),
                )
        elif purpose == "record_stop" and reply.get("code") == 0:
            self._manual_recording_keys.discard(key)
            runtime.recording_start_pending = False
            runtime.last_msg = "设备已请求停止录像"
        elif purpose == "capture_photo" and reply.get("code") == 0:
            runtime.last_msg = "设备已请求拍照"
        elif purpose == "switch_ui_page" and reply.get("code") == 0:
            current_page = reply.get("current_page", reply.get("page", ""))
            runtime.last_msg = f"设备 UI 已切换到 {current_page}"
        elif purpose == "gimbal_move" and reply.get("code") == 0:
            runtime.last_msg = "设备云台已执行转动"
        elif purpose == "swipe_screen" and reply.get("code") == 0:
            runtime.last_msg = "设备已执行屏幕滑动"
        elif purpose == "custom_capabilities":
            try:
                capabilities = CustomCapabilities.from_reply(reply)
            except CustomConfigError as exc:
                _APP_LOG.error(
                    "custom_capabilities_parse_failed key=%s device_ip=%s firmware_version=%s "
                    "error=%s diagnostics=%s",
                    key,
                    runtime.device_ip,
                    runtime.firmware_version or "<unknown>",
                    str(exc),
                    _custom_capability_diagnostics(reply),
                )
                self._custom_operation_failed(runtime, purpose, str(exc), structured=reply)
            else:
                _APP_LOG.info(
                    "custom_capabilities_parsed key=%s device_ip=%s firmware_version=%s "
                    "config_version=%s policy_version=%s media_manifest_supported=%s "
                    "cleanup_supported=%s cleanup_effective=%s active_revision=%s active_crc=%s",
                    key,
                    runtime.device_ip,
                    runtime.firmware_version or "<unknown>",
                    capabilities.config_version,
                    capabilities.policy_version,
                    capabilities.media_manifest_supported,
                    capabilities.cleanup_supported,
                    capabilities.media_manifest_supported and capabilities.cleanup_supported,
                    capabilities.active_revision,
                    capabilities.active_crc,
                )
                self._custom_capabilities_by_key[key] = capabilities
                self._custom_profiles = CustomProfileStore(custom_profiles_path()).load()
                profile = self._custom_profile_load_after_capabilities.pop(key, None)
                if profile is None:
                    self._custom_load_after_capabilities.add(key)
                    runtime.last_msg = "已读取自定义测试能力，正在读取设备当前配置"
                else:
                    try:
                        validate_config(profile.config, capabilities)
                    except CustomConfigError as exc:
                        self._custom_operation_failed(runtime, purpose, str(exc))
                        _APP_LOG.exception(
                            "custom_profile_load_rejected key=%s profile=%s rev=%s crc=%s",  # noqa: TRY400
                            key,
                            profile.name,
                            capabilities.active_revision,
                            capabilities.active_crc,
                        )
                        if self._is_custom_c01(runtime):
                            chosen = QMessageBox.question(
                                self,
                                "方案无法载入",
                                f"PC 方案“{profile.name}”与当前设备能力不兼容：\n\n"
                                + custom_config_error_text(str(exc))
                                + "\n\n是否按当前设备能力新建一个空方案？"
                                "（原方案文件不会被修改。）",
                                QMessageBox.Yes | QMessageBox.No,
                                QMessageBox.No,
                            )
                            if chosen == QMessageBox.Yes:
                                try:
                                    draft = self._new_custom_config(capabilities)
                                except CustomConfigError as draft_exc:
                                    draft_message = custom_config_error_text(str(draft_exc))
                                    runtime.last_msg = draft_message
                                    self._add_log(runtime, draft_message, "error")
                                else:
                                    self._show_custom_config_dialog(
                                        runtime, capabilities, draft,
                                        capabilities.active_revision,
                                    )
                                    runtime.last_msg = (
                                        f"已按当前设备能力新建有效草稿，请配置后保存"
                                        f"（原“{profile.name}”未改动）。"
                                    )
                    else:
                        self._show_custom_config_dialog(
                            runtime, capabilities, profile.config, capabilities.active_revision,
                        )
                        runtime.last_msg = f"已载入 PC 方案“{profile.name}”，请保存到设备后运行。"
        elif purpose == "custom_config_load":
            capabilities = self._custom_capabilities_by_key.get(key)
            if capabilities is None:
                self._custom_operation_failed(runtime, purpose, "custom capability context is missing")
            else:
                try:
                    config, revision, crc, estimated_runtime_ms = saved_config_from_reply(reply, capabilities)
                except CustomConfigError as exc:
                    self._custom_operation_failed(runtime, purpose, str(exc), structured=reply)
                else:
                    _APP_LOG.info(
                        "custom_config_loaded key=%s revision=%s crc=%s estimated_runtime_ms=%s",
                        key, revision, crc, estimated_runtime_ms,
                    )
                    runtime.last_msg = f"已读取自定义配置，修订版 {revision}"
                    self._custom_saved_revision_by_key[key] = revision
                    runtime.custom_config_revision = revision
                    runtime.custom_config_crc = crc
                    runtime.custom_estimated_runtime_ms = estimated_runtime_ms
                    runtime.custom_config_snapshot = self._make_custom_config_snapshot(
                        config, capabilities, revision, crc, estimated_runtime_ms,
                        runtime.firmware_version, runtime.device_ip,
                    )
                    self._show_custom_config_dialog(runtime, capabilities, config, revision)
        elif purpose == "custom_config_refresh_revision":
            capabilities = self._custom_capabilities_by_key.get(key)
            dialog = self.custom_config_dialog
            if capabilities is None or dialog is None or self._custom_dialog_key != key:
                self._custom_operation_failed(
                    runtime, purpose, "custom revision refresh context is missing",
                )
            else:
                try:
                    config, revision, crc, estimated_runtime_ms = saved_config_from_reply(reply, capabilities)
                except CustomConfigError as exc:
                    self._custom_operation_failed(runtime, purpose, str(exc), structured=reply)
                else:
                    runtime.last_msg = f"已刷新设备方案修订版 {revision}，保留当前草稿。"
                    self._custom_saved_revision_by_key[key] = revision
                    runtime.custom_config_revision = revision
                    runtime.custom_config_crc = crc
                    runtime.custom_estimated_runtime_ms = estimated_runtime_ms
                    runtime.custom_config_snapshot = self._make_custom_config_snapshot(
                        config, capabilities, revision, crc, estimated_runtime_ms,
                        runtime.firmware_version, runtime.device_ip,
                    )
                    dialog.refresh_device_revision(revision, crc, estimated_runtime_ms)
        elif purpose == "custom_config_save":
            if reply.get("code") != 0:
                self._custom_operation_failed(
                    runtime, purpose, str(reply.get("msg", "set_custom_config failed")),
                    structured=reply,
                )
            else:
                revision = reply.get("config_revision")
                crc = reply.get("config_crc")
                if not isinstance(revision, int) or revision <= 0 or not isinstance(crc, int):
                    self._custom_operation_failed(
                        runtime, purpose, "device save reply is missing config_revision or config_crc",
                    )
                else:
                    self._custom_verify_after_save[key] = (revision, crc)
                    runtime.last_msg = f"设备已确认保存修订版 {revision}，正在回读校验"
        elif purpose == "custom_config_verify_save":
            capabilities = self._custom_capabilities_by_key.get(key)
            expected = self._custom_verify_after_save.pop(key, None)
            if capabilities is None or expected is None:
                self._custom_operation_failed(runtime, purpose, "custom save verification context is missing")
            else:
                try:
                    config, revision, crc, estimated_runtime_ms = saved_config_from_reply(reply, capabilities)
                    if (revision, crc) != expected:
                        raise CustomConfigError(
                            "device readback revision/CRC does not match the saved configuration"
                        )
                except CustomConfigError as exc:
                    self._custom_operation_failed(runtime, purpose, str(exc), structured=reply)
                else:
                    runtime.last_msg = f"自定义配置已回读确认（修订版 {revision}）"
                    self._custom_saved_revision_by_key[key] = revision
                    runtime.custom_config_revision = revision
                    runtime.custom_config_crc = crc
                    runtime.custom_estimated_runtime_ms = estimated_runtime_ms
                    runtime.custom_config_snapshot = self._make_custom_config_snapshot(
                        config, capabilities, revision, crc, estimated_runtime_ms,
                        runtime.firmware_version, runtime.device_ip,
                    )
                    if key == self.current_key:
                        self.custom_profile_combo.setCurrentIndex(0)
                    dialog = self.custom_config_dialog
                    if dialog is not None and self._custom_dialog_key == key:
                        dialog.save_verified(config, revision, crc, estimated_runtime_ms)
                    if self._custom_save_run_after.pop(key, False):
                        self._start_saved_custom_case_after_config(runtime)
        elif purpose != "probe":
            self._apply_event(runtime, reply)
        if not quiet:
            self._add_log(runtime, json.dumps(reply, ensure_ascii=False, separators=(",", ":")))
        _APP_LOG.info("command_result_before_ui_sync key=%s purpose=%s", key, purpose)
        self._schedule_ui_sync()
        _APP_LOG.info("command_result_after_ui_sync key=%s purpose=%s", key, purpose)
        if key == self.current_key:
            _APP_LOG.info("command_result_before_detail_panel key=%s purpose=%s", key, purpose)
            self._update_detail_panel()
            _APP_LOG.info("command_result_after_detail_panel key=%s purpose=%s", key, purpose)
        # 手动操作（拍照/录像/切换画面/云台）命令完成后立即恢复按钮，
        # 不等待 _update_detail_panel 排到主线程事件队列。
        if purpose in _MANUAL_ACTION_PURPOSES and reply.get("code") == 0 and key == self.current_key:
            self._set_detail_actions_enabled(True)
        if purpose == "probe":
            self._active_probe_count = max(0, self._active_probe_count - 1)
        # Auto-start watch for online configured devices
        should_start_watch = runtime.configured and runtime.online and runtime.watch_worker is None
        _APP_LOG.info(
            "command_result_watch_decision key=%s purpose=%s configured=%s online=%s "
            "watch_present=%s should_start=%s",
            key,
            purpose,
            runtime.configured,
            runtime.online,
            runtime.watch_worker is not None,
            should_start_watch,
        )
        if should_start_watch:
            _APP_LOG.info("command_result_before_ensure_watch key=%s", key)
            self._ensure_watch(runtime)
            _APP_LOG.info(
                "command_result_after_ensure_watch key=%s watch_present=%s elapsed=%.3fs",
                key, runtime.watch_worker is not None, time.monotonic() - callback_start,
            )
        _APP_LOG.info(
            "command_result_callback_exit key=%s purpose=%s elapsed=%.3fs",
            key, purpose, time.monotonic() - callback_start,
        )

    def _on_command_error(
        self,
        key: str,
        purpose: str,
        quiet: bool,
        message: str,
        expected_runtime: DeviceRuntime,
    ) -> None:
        runtime = self.runtimes.get(key)
        if runtime is not expected_runtime:
            return
        is_custom_operation = purpose.startswith("custom_config") or purpose == "custom_capabilities"
        # This callback is reserved for transport failures. A user-requested
        # cancellation is consumed by CommandWorker without emitting an
        # error. A valid non-zero device reply is routed through
        # _on_command_result so
        # it can keep the device marked online and show the business reason.
        runtime.online = False
        runtime.transport_error_streak += 1
        if "10049" in message or "地址无效" in message:
            runtime.online = False
            runtime.link_invalid = True
            runtime.last_msg = "设备网络已断开（source_ip 失效），已暂停探活，请刷新设备"
            self._add_log(runtime, runtime.last_msg, "error")
            self._schedule_ui_sync()
            if key == self.current_key:
                self._update_detail_panel()
            return
        if purpose == "probe":
            reset_link_route_cache(runtime.link)
            runtime.last_msg = f"获取设备用例目录失败: {message}"
            self._add_log(runtime, runtime.last_msg, "error")
        if is_custom_operation:
            self._custom_operation_failed(runtime, purpose, message)
        elif purpose != "probe":
            runtime.last_msg = message
            self._add_log(runtime, message, "error")
        if purpose in {"record_start", "record_stop"}:
            self._manual_record_commands_inflight.discard(key)
        if purpose == "record_start":
            runtime.recording_start_pending = False
            self._manual_recording_keys.discard(key)
        self._schedule_ui_sync()
        if key == self.current_key:
            self._update_detail_panel()
        if purpose == "probe":
            self._active_probe_count = max(0, self._active_probe_count - 1)

    def _reconcile_pending_record_start(
        self, key: str, expected_runtime: DeviceRuntime,
    ) -> None:
        runtime = self.runtimes.get(key)
        if runtime is not expected_runtime or not runtime.recording_start_pending:
            return
        runtime.recording_start_pending = False
        self._probe_runtime(runtime)

    def _thread_cleanup(
        self, key: str, thread: QThread, expected_runtime: DeviceRuntime,
    ) -> None:
        _APP_LOG.info("thread_cleanup_enter key=%s", key)
        runtime = self.runtimes.get(key)
        if runtime is not expected_runtime:
            retired = next(
                (candidate for candidate in self._retired_runtimes if candidate is expected_runtime),
                None,
            )
            if retired is None or retired.command_thread is not thread:
                _APP_LOG.info(
                    "thread_cleanup_skip_runtime_mismatch key=%s retired_found=%s",
                    key, retired is not None,
                )
                return
            retired.command_thread = None
            retired.command_worker = None
            _APP_LOG.info("thread_cleanup_retired_cleared key=%s", key)
            return
        if runtime.command_thread is thread:
            runtime.command_thread = None
            runtime.command_worker = None
            _APP_LOG.info("thread_cleanup_cleared key=%s", key)
        else:
            _APP_LOG.info(
                "thread_cleanup_noop key=%s current_thread_is_not_this",
                key,
            )
        next_command: tuple[Dict[str, Any], str, bool, float] | None = None
        if key in self._catalog_refresh_after_probe:
            self._catalog_refresh_after_probe.discard(key)
            if runtime.online is True:
                next_command = (
                    {"cmd": "agent_info", "include_catalog": True},
                    "catalog_refresh", True, CATALOG_PROBE_TIMEOUT,
                )
        elif key in self._custom_load_after_capabilities:
            self._custom_load_after_capabilities.discard(key)
            _APP_LOG.info("custom_config_load_requested key=%s", key)
            next_command = (make_get_custom_config_payload(), "custom_config_load", False, 5.0)
        elif key in self._custom_revision_refresh_after_conflict:
            self._custom_revision_refresh_after_conflict.discard(key)
            _APP_LOG.info("custom_config_revision_refresh_requested key=%s", key)
            next_command = (
                make_get_custom_config_payload(), "custom_config_refresh_revision", False, 5.0,
            )
        elif key in self._custom_verify_after_save:
            dialog = self.custom_config_dialog
            if dialog is not None and self._custom_dialog_key == key:
                # Saving and readback are two bounded requests.  Restart the
                # editor safeguard for the readback phase so a slow save does
                # not re-enable controls while verification is still running.
                dialog.set_busy(True, "配置已保存，正在回读校验…")
            next_command = (make_get_custom_config_payload(), "custom_config_verify_save", False, 5.0)

        if next_command is not None and not self._closing:
            payload, purpose, quiet, timeout = next_command
            self._start_command(runtime, payload, purpose=purpose, quiet=quiet, timeout=timeout)
        self._schedule_ui_sync()
        if key == self.current_key:
            self._update_detail_panel()

    # --- Selected device actions ----------------------------------------------------
    @staticmethod
    def _is_custom_c01(runtime: DeviceRuntime | None) -> bool:
        return runtime is not None and runtime.suite == "custom_test" and runtime.case_id == 1

    @staticmethod
    def _new_custom_config(capabilities: CustomCapabilities) -> CustomConfig:
        """Create the smallest valid draft exclusively from device-provided choices."""
        step: CustomStep | None = None
        for action in capabilities.actions:
            if action == ACTION_NAV and capabilities.safe_pages:
                step = CustomStep(action, page=next(iter(capabilities.safe_pages)))
            elif action == ACTION_NAV_TARGET and capabilities.page_targets:
                step = CustomStep(action, page=next(iter(capabilities.page_targets)))
            elif action == ACTION_MODE and capabilities.mode_options:
                step = CustomStep(action, arg0=next(iter(capabilities.mode_options)))
            elif action == ACTION_VIDEO and capabilities.video_presets:
                step = CustomStep(action, arg0=next(iter(capabilities.video_presets)))
            elif action == ACTION_VERIFY and capabilities.verify_options:
                step = CustomStep(action, arg0=next(iter(capabilities.verify_options)))
            elif action in (ACTION_CLICK, ACTION_SWIPE, ACTION_SLIDER):
                for policy_id, policy in capabilities.policy_steps.items():
                    if policy["action"] == action:
                        step = CustomStep(action, page=int(policy.get("page", 0)), arg0=policy_id)
                        break
            elif action == ACTION_VIDEO_RECORD:
                for profile in capabilities.video_profiles.values():
                    if profile.fps:
                        step = CustomStep(
                            action, arg0=profile.resolution_id,
                            arg1=next(iter(profile.fps)),
                            arg2=min(10, capabilities.record_seconds_range[1]),
                            video_canvas=profile.canvas,
                        )
                        break
            elif action == ACTION_SCREEN_ROTATE:
                step = CustomStep(action, arg0=0)
            elif action == ACTION_PHOTO_CAPTURE:
                step = CustomStep(action)
            if step is not None:
                break
        if step is None:
            raise CustomConfigError("device did not expose an action that can form a valid C01 draft")
        return CustomConfig(
            cycles=capabilities.cycles_range[0],
            steps=[step],
            page_settle_ms=capabilities.interval_options["page_settle_ms"][0],
            step_interval_ms=capabilities.interval_options["step_interval_ms"][0],
            media_interval_ms=custom_c01_compatibility_media_interval_ms(capabilities),
            cycle_interval_ms=capabilities.interval_options["cycle_interval_ms"][0],
        )

    def open_custom_config_selected(self) -> None:
        runtime = self.current_runtime()
        if not self._is_custom_c01(runtime):
            QMessageBox.information(self, "自定义测试", "请选择 custom_test 的 C01 用例后再配置。")
            return
        if runtime is None or not runtime.configured or runtime.online is not True:
            QMessageBox.warning(self, "自定义测试", "请先选择一台已配置且在线的设备。")
            return
        dialog = self.custom_config_dialog
        if dialog is not None and dialog.isVisible():
            if self._custom_dialog_key == runtime.key:
                dialog.raise_()
                dialog.activateWindow()
            else:
                QMessageBox.information(self, "自定义测试", "请先关闭当前设备的 C01 配置窗口，再配置另一台设备。")
            return
        if runtime.status in {"queued", "running", "stopping"}:
            QMessageBox.warning(self, "自定义测试", "设备正在运行用例，不能修改 C01 配置。")
            return
        profile = self._selected_custom_profile(runtime)
        self._custom_profile_load_after_capabilities.pop(runtime.key, None)
        if profile is not None:
            self._custom_profile_load_after_capabilities[runtime.key] = profile
        _APP_LOG.info(
            "custom_capabilities_request key=%s device_ip=%s firmware_version=%s "
            "expected_config_version=%s expected_policy_version=%s "
            "supports_playback_damage_check=%s selected_profile=%s",
            runtime.key,
            runtime.device_ip,
            runtime.firmware_version or "<unknown>",
            CUSTOM_CONFIG_VERSION,
            CUSTOM_POLICY_VERSION,
            True,
            profile.name if profile is not None else "<none>",
        )
        self._start_command(
            runtime, {"cmd": "get_custom_capabilities", "supports_playback_damage_check": True},
            purpose="custom_capabilities", timeout=5.0,
        )

    def _show_custom_config_dialog(
        self,
        runtime: DeviceRuntime,
        capabilities: CustomCapabilities,
        config: CustomConfig,
        revision: int,
    ) -> None:
        _APP_LOG.info("custom_config_dialog_open key=%s revision=%s", runtime.key, revision)
        previous = self.custom_config_dialog
        if previous is not None:
            previous.close()
        dialog = CustomConfigDialog(
            capabilities, config, revision, runtime.firmware_version, self,
        )
        self.custom_config_dialog = dialog
        self._custom_dialog_key = runtime.key
        dialog.save_requested.connect(self._save_custom_config_requested)
        dialog.profiles_changed.connect(
            lambda profiles, profile_key=runtime.key: self._on_custom_profiles_changed(
                profile_key, profiles,
            ),
        )
        dialog.finished.connect(
            lambda _result, closing_dialog=dialog: self._custom_dialog_closed(closing_dialog),
            Qt.DirectConnection,
        )
        dialog.finished.connect(dialog.deleteLater)
        dialog.show()
        dialog.raise_()
        dialog.activateWindow()
        _APP_LOG.info("custom_config_dialog_ready key=%s revision=%s", runtime.key, revision)

    def _custom_dialog_closed(self, dialog: CustomConfigDialog) -> None:
        if self.custom_config_dialog is dialog:
            self.custom_config_dialog = None
            self._custom_dialog_key = None

    def _on_custom_profiles_changed(self, key: str, profiles: object) -> None:
        if not isinstance(profiles, list) or not all(isinstance(item, CustomProfile) for item in profiles):
            return
        self._custom_profiles = list(profiles)
        runtime = self.runtimes.get(key)
        if runtime is not None and key == self.current_key:
            self._refresh_custom_profile_combo(runtime)

    def _save_custom_config_requested(
        self, config: CustomConfig, base_revision: int, run_after: bool,
    ) -> None:
        key = self._custom_dialog_key
        runtime = self.runtimes.get(key) if key is not None else None
        capabilities = self._custom_capabilities_by_key.get(key) if key is not None else None
        dialog = self.custom_config_dialog
        if runtime is None or capabilities is None or dialog is None:
            return
        if not self._is_custom_c01(runtime) or runtime.online is not True:
            dialog.set_busy(False, "当前设备已离线，无法配置 C01。")
            return
        if runtime.status in {"queued", "running", "stopping"} or self._command_thread_alive(runtime):
            dialog.set_busy(False, "设备正在忙，未修改 C01 配置。")
            return
        try:
            payload = make_set_custom_config_payload(config, capabilities, base_revision)
        except CustomConfigError as exc:
            dialog.set_busy(False, custom_config_error_text(str(exc)))
            return
        _APP_LOG.info(
            "custom_config_save_requested key=%s steps=%s cycles=%s media=%s",
            key,
            [step.as_payload() for step in config.steps],
            config.cycles,
            {
                "photo_check": (config.photo_check_mode, config.photo_check_every_cycles),
                "video_check": (config.video_check_mode, config.video_check_every_cycles),
                "photo_cleanup": config.photo_cleanup_every_cycles,
                "video_cleanup": config.video_cleanup_every_cycles,
            },
        )
        _APP_LOG.info(
            "custom_config_save_capabilities key=%s device_ip=%s firmware_version=%s "
            "config_version=%s policy_version=%s media_manifest_supported=%s "
            "cleanup_supported=%s active_revision=%s active_crc=%s base_revision=%s",
            key,
            runtime.device_ip,
            runtime.firmware_version or "<unknown>",
            capabilities.config_version,
            capabilities.policy_version,
            capabilities.media_manifest_supported,
            capabilities.cleanup_supported,
            capabilities.active_revision,
            capabilities.active_crc,
            base_revision,
        )
        self._custom_save_run_after[key] = run_after
        self._start_command(runtime, payload, purpose="custom_config_save", timeout=10.0)

    def _custom_operation_failed(
        self, runtime: DeviceRuntime, purpose: str, message: str,
        structured: Mapping[str, Any] | None = None,
    ) -> None:
        friendly = custom_config_error_text(message, structured=structured)
        runtime.last_msg = friendly
        self._add_log(runtime, friendly, "error")
        capabilities = self._custom_capabilities_by_key.get(runtime.key)
        _APP_LOG.error(
            "custom_operation_failed_detail key=%s device_ip=%s firmware_version=%s "
            "purpose=%s raw_message=%s friendly_message=%s capability_snapshot=%s",
            runtime.key,
            runtime.device_ip,
            runtime.firmware_version or "<unknown>",
            purpose,
            message,
            friendly,
            {
                "config_version": capabilities.config_version if capabilities else None,
                "policy_version": capabilities.policy_version if capabilities else None,
                "media_manifest_supported": (
                    capabilities.media_manifest_supported if capabilities else None
                ),
                "cleanup_supported": capabilities.cleanup_supported if capabilities else None,
            },
        )
        if structured:
            _APP_LOG.info(
                "custom_operation_failed key=%s purpose=%s reason_code=%s step_index=%s field=%s actual=%s allowed=%s",
                runtime.key,
                purpose,
                structured.get("reason_code"),
                structured.get("step_index"),
                structured.get("field"),
                structured.get("actual"),
                structured.get("allowed"),
            )
        key = runtime.key
        if purpose == "custom_capabilities":
            self._custom_profile_load_after_capabilities.pop(key, None)
            self._custom_load_after_capabilities.discard(key)
            self._custom_revision_refresh_after_conflict.discard(key)
        if purpose == "custom_config_load" and (
            "no saved configuration" in message.lower() or "config_not_saved" in message.lower()
        ):
            self._custom_saved_revision_by_key.pop(key, None)
            runtime.custom_config_revision = 0
            runtime.custom_config_crc = None
            runtime.custom_estimated_runtime_ms = None
            runtime.custom_config_snapshot = None
            capabilities = self._custom_capabilities_by_key.get(key)
            if capabilities is not None:
                try:
                    self._show_custom_config_dialog(
                        runtime, capabilities, self._new_custom_config(capabilities), 0,
                    )
                    runtime.last_msg = "设备尚无已保存配置，已打开新的 C01 草稿。"
                    return
                except CustomConfigError as exc:
                    friendly = custom_config_error_text(str(exc))
                    runtime.last_msg = friendly
                    self._add_log(runtime, friendly, "error")
        if purpose == "custom_config_refresh_revision":
            dialog = self.custom_config_dialog
            if "no saved configuration" in message.lower() or "config_not_saved" in message.lower():
                self._custom_saved_revision_by_key.pop(key, None)
                runtime.custom_config_revision = 0
                runtime.custom_config_crc = None
                runtime.custom_estimated_runtime_ms = None
                runtime.custom_config_snapshot = None
                if dialog is not None and self._custom_dialog_key == key:
                    dialog.refresh_device_revision(0, None, None)
                    runtime.last_msg = "设备当前 C01 方案已被删除，保留当前草稿，可再次保存。"
                    return
            if dialog is not None and self._custom_dialog_key == key:
                dialog.save_failed(friendly)
            return
        if purpose in {"custom_config_save", "custom_config_verify_save"}:
            self._custom_save_run_after.pop(key, None)
            self._custom_verify_after_save.pop(key, None)
            dialog = self.custom_config_dialog
            is_revision_conflict = (
                purpose == "custom_config_save" and is_config_revision_conflict(structured)
            )
            if is_revision_conflict and dialog is not None and self._custom_dialog_key == key:
                refresh = QMessageBox.question(
                    self,
                    "设备方案已更新",
                    "设备端 C01 方案已被其它窗口更新。\n\n"
                    "是否读取最新修订版？当前编辑器草稿会保留，不会被设备方案覆盖。"
                    "读取完成后请再次确认覆盖保存。",
                    QMessageBox.Yes | QMessageBox.No,
                    QMessageBox.Yes,
                )
                if refresh == QMessageBox.Yes:
                    self._custom_revision_refresh_after_conflict.add(key)
                    runtime.last_msg = "正在刷新设备 C01 修订版，当前草稿已保留。"
                    dialog.set_busy(True, "正在刷新设备方案修订版，当前草稿已保留…")
                    return
            if dialog is not None and self._custom_dialog_key == key:
                dialog.save_failed(friendly)

    def _start_saved_custom_case_after_config(self, runtime: DeviceRuntime) -> None:
        if not self._is_custom_c01(runtime):
            self._add_log(runtime, "C01 保存成功，但当前选择已变化，未自动运行。", "error")
            return
        confirm_risk = self._confirm_risk(runtime, self._selected_case_descriptor(runtime))
        if confirm_risk is not None:
            self._start_recording(runtime, confirm_risk)

    @staticmethod
    def _make_custom_config_snapshot(
        config: CustomConfig,
        capabilities: CustomCapabilities,
        revision: int,
        crc: int | None,
        estimated_runtime_ms: int | None,
        firmware_version: str = "",
        device_ip: str = "",
    ) -> Dict[str, Any]:
        """Capture the device-confirmed C01 state before the worker starts."""
        return {
            "canonical_config": config.as_payload(),
            "config_version": capabilities.config_version,
            "policy_version": capabilities.policy_version,
            "media_manifest_supported": capabilities.media_manifest_supported,
            "cleanup_supported": capabilities.cleanup_supported,
            "firmware_version": firmware_version,
            "device_ip": device_ip,
            "config_revision": revision,
            "config_crc": crc,
            "estimated_runtime_ms": estimated_runtime_ms,
        }

    def run_selected(self) -> None:
        runtime = self.current_runtime()
        if runtime is None:
            return
        profile = self._selected_custom_profile(runtime)
        if self._is_custom_c01(runtime) and profile is not None:
            QMessageBox.information(
                self,
                "自定义测试",
                f"已选择 PC 方案“{profile.name}”。请先点击“配置自定义步骤”，确认后可“覆盖设备当前方案”或“覆盖并开始测试”。",
            )
            return
        if self._is_custom_c01(runtime) and runtime.key not in self._custom_saved_revision_by_key:
            QMessageBox.information(
                self,
                "自定义测试",
                "C01 需要先从设备读取并确认已保存配置；将打开配置窗口进行检查。",
            )
            self.open_custom_config_selected()
            return
        descriptor = self._selected_case_descriptor(runtime)
        if descriptor is not None and not descriptor.get("selectable", True):
            QMessageBox.warning(self, "运行测试", "当前用例尚未实现，不能运行")
            return
        confirm_risk = self._confirm_risk(runtime, descriptor)
        if confirm_risk is None:
            return
        self._start_recording(runtime, confirm_risk)

    def stop_selected(self) -> None:
        runtime = self.current_runtime()
        if runtime is None:
            return
        self._start_command(
            runtime,
            {"cmd": "stop_case", "suite": runtime.suite, "case_id": runtime.case_id},
            purpose="stop",
        )

    def status_selected(self) -> None:
        runtime = self.current_runtime()
        if runtime is None:
            return
        self._start_command(runtime, {"cmd": "get_case_status"}, purpose="status")

    def refresh_version_selected(self) -> None:
        runtime = self.current_runtime()
        if runtime is None or runtime.online is not True:
            QMessageBox.warning(self, "刷新版本", "请先选择一个在线的设备")
            return
        if self._command_thread_alive(runtime):
            self._add_log(runtime, "设备命令仍在执行，版本刷新已忽略", "error")
            return
        self._add_log(runtime, "正在刷新设备版本和能力信息", "info")
        self._probe_runtime(runtime)

    def copy_version_selected(self) -> None:
        runtime = self.current_runtime()
        version = runtime.firmware_version.strip() if runtime is not None else ""
        if not version:
            QMessageBox.information(self, "复制版本", "当前没有可复制的设备版本")
            return
        QApplication.clipboard().setText(version)
        self._add_log(runtime, f"设备版本已复制: {version}", "info")

    def reboot_selected(self) -> None:
        runtime = self.current_runtime()
        if runtime is None:
            return
        self._start_command(runtime, {"cmd": "reboot"}, purpose="reboot")

    def _manual_device_action_runtime(
        self, title: str, *, allow_while_running: bool = False,
        allow_command_busy: bool = False,
    ) -> DeviceRuntime | None:
        runtime = self.current_runtime()
        if runtime is None or runtime.online is not True:
            QMessageBox.warning(self, title, "请先选择一个在线的设备")
            return None
        blocked_states = {"stopping"}
        if not allow_while_running:
            blocked_states.update({"queued", "running"})
        if runtime.status in blocked_states:
            QMessageBox.warning(self, title, "设备正在运行用例，请停止用例后再执行设备操作")
            return None
        if self._command_thread_alive(runtime):
            if allow_command_busy:
                return runtime
            self._add_log(runtime, "设备命令仍在执行，已忽略重复操作", "error")
            return None
        return runtime

    def capture_photo_selected(self) -> None:
        runtime = self._manual_device_action_runtime("拍照")
        if runtime is None:
            return
        self._start_command(runtime, {"cmd": "capture_photo"}, purpose="capture_photo", timeout=5.0)

    def toggle_device_record_selected(self) -> None:
        runtime = self._manual_device_action_runtime(
            "录像", allow_while_running=True, allow_command_busy=True,
        )
        if runtime is None:
            return
        pending = self._pending_manual_record_commands.get(runtime.key)
        if pending is not None or runtime.key in self._manual_record_commands_inflight:
            self._add_log(runtime, "录像命令仍在执行或等待发送，请稍候", "error")
            return
        starting = runtime.key not in self._manual_recording_keys
        command = "record_start" if starting else "record_stop"
        title = "开始录像" if starting else "停止录像"
        if not self._confirm_action(title, f"将通过设备正式拍摄流程{title}，确定继续吗？"):
            return
        if self._command_thread_alive(runtime):
            self._queue_manual_record_command(runtime, command)
            return
        self._manual_record_commands_inflight.add(runtime.key)
        if not self._start_command(runtime, {"cmd": command}, purpose=command, timeout=5.0):
            self._manual_record_commands_inflight.discard(runtime.key)

    def _queue_manual_record_command(self, runtime: DeviceRuntime, command: str) -> None:
        """Send a manual recording command after a refresh/probe command releases the link."""
        deadline = time.monotonic() + 30.0
        self._pending_manual_record_commands[runtime.key] = (runtime, command, deadline)
        label = "开始录像" if command == "record_start" else "停止录像"
        self._add_log(runtime, f"设备命令仍在执行，{label}将在当前命令完成后发送", "info")
        QTimer.singleShot(
            100,
            lambda key=runtime.key, expected_runtime=runtime:
            self._flush_pending_manual_record_command(key, expected_runtime),
        )

    def _flush_pending_manual_record_command(
        self, key: str, expected_runtime: DeviceRuntime,
    ) -> None:
        pending = self._pending_manual_record_commands.get(key)
        if pending is None or pending[0] is not expected_runtime:
            return
        runtime, command, deadline = pending
        if self._closing or self.runtimes.get(key) is not runtime or runtime.online is not True:
            self._pending_manual_record_commands.pop(key, None)
            return
        if runtime.status == "stopping":
            self._pending_manual_record_commands.pop(key, None)
            self._add_log(runtime, "设备正在停止用例，录像命令未发送", "error")
            return
        if time.monotonic() >= deadline:
            self._pending_manual_record_commands.pop(key, None)
            self._add_log(runtime, "录像命令等待设备命令通道超时，未发送", "error")
            return
        if self._command_thread_alive(runtime):
            QTimer.singleShot(
                100,
                lambda pending_key=key, pending_runtime=runtime:
                self._flush_pending_manual_record_command(pending_key, pending_runtime),
            )
            return
        self._pending_manual_record_commands.pop(key, None)
        self._manual_record_commands_inflight.add(key)
        if not self._start_command(runtime, {"cmd": command}, purpose=command, timeout=5.0):
            self._manual_record_commands_inflight.discard(key)

    def switch_ui_page_selected(self) -> None:
        runtime = self._manual_device_action_runtime("切换画面")
        if runtime is None:
            return
        page = self.ui_page_combo.currentData()
        if not isinstance(page, str) or not page:
            return
        self._start_command(
            runtime,
            {"cmd": "switch_ui_page", "page": page},
            purpose="switch_ui_page",
            timeout=5.0,
        )

    def gimbal_move_selected(self) -> None:
        runtime = self._manual_device_action_runtime("云台转动")
        if runtime is None:
            return
        labels = {"上": "up", "下": "down", "左": "left", "右": "right"}
        direction_label, accepted = QInputDialog.getItem(
            self, "云台转动", "选择方向", list(labels), 0, False,
        )
        if not accepted:
            return
        direction = labels[direction_label]
        self._start_command(
            runtime,
            {"cmd": "gimbal_move", "direction": direction, "duration_ms": 300},
            purpose="gimbal_move",
            timeout=5.0,
        )

    def swipe_screen_selected(self) -> None:
        runtime = self._manual_device_action_runtime("滑动屏幕")
        if runtime is None:
            return
        labels = {"上": "up", "下": "down", "左": "left", "右": "right"}
        direction_label, accepted = QInputDialog.getItem(
            self, "滑动屏幕", "选择方向", list(labels), 0, False,
        )
        if not accepted:
            return
        direction = labels[direction_label]
        self._start_command(
            runtime,
            {"cmd": "swipe_screen", "direction": direction},
            purpose="swipe_screen",
            timeout=5.0,
        )

    # --- Screen capture -------------------------------------------------------------
    def screenshot_selected(self) -> None:
        runtime = self.current_runtime()
        if runtime is None or runtime.online is not True:
            QMessageBox.warning(self, "截屏", "请先选择一个在线的设备")
            return
        self._preview_active = False
        self.preview_btn.setText("屏幕预览")
        self._start_screen_session(runtime, preview=False)

    def toggle_preview_selected(self) -> None:
        if self._preview_active:
            self._preview_active = False
            self.preview_btn.setText("屏幕预览")
            return

        runtime = self.current_runtime()
        if runtime is None or runtime.online is not True:
            QMessageBox.warning(self, "屏幕预览", "请先选择一个在线的设备")
            return
        if runtime.status in {"queued", "stopping"}:
            QMessageBox.warning(self, "屏幕预览", "设备正在运行用例，请停止用例后再开启屏幕预览")
            return

        self._preview_active = True
        self.preview_btn.setText("停止预览")
        self._start_screen_session(runtime, preview=True)

    def _start_screen_session(self, runtime: DeviceRuntime, preview: bool) -> None:
        self._screenshot_key = runtime.key
        self._screenshot_type = str(self.screen_combo.currentData())
        if self._screenshot_dialog is None:
            self._screenshot_dialog = ScreenshotDialog(self)
            self._screenshot_dialog.closed.connect(self._on_screenshot_dialog_closed)
        mode = "连续预览" if preview else "截屏"
        self._screenshot_dialog.setWindowTitle(f"设备屏幕 - {runtime.device_ip} - {mode}")
        self._screenshot_dialog.show()
        self._screenshot_dialog.raise_()
        self._screenshot_dialog.activateWindow()
        self._add_log(runtime, f"开始{mode}: {self._screenshot_type}")
        self._request_screenshot_frame()

    def _request_screenshot_frame(self) -> None:
        if self._closing or self._screenshot_worker is not None or self._screenshot_key is None:
            return
        runtime = self.runtimes.get(self._screenshot_key)
        if runtime is None or runtime.online is not True:
            self._stop_screen_preview("设备已离线")
            return

        worker = ScreenshotWorker(
            runtime.device_ip, runtime.port, runtime.pc_ip, self._screenshot_type,
            int(runtime.link.get("if_index", 0) or 0) or None,
        )
        self._screenshot_worker = worker
        self.screenshot_btn.setEnabled(False)
        worker.start()

    def _on_screenshot_result(self, metadata: Dict[str, Any], raw: bytes) -> None:
        image = self._frame_to_qimage(metadata, raw)
        if image.isNull():
            self._on_screenshot_error("不支持的像素格式或画面数据不完整")
            return
        image = image.transformed(QTransform().rotate(180))
        if self._screenshot_dialog is not None:
            self._screenshot_dialog.set_frame(image, metadata)

    def _on_screenshot_error(self, message: str) -> None:
        runtime = self.runtimes.get(self._screenshot_key or "")
        if runtime is not None:
            self._add_log(runtime, f"获取屏幕失败: {message}", "error")
        if self._screenshot_dialog is not None:
            self._screenshot_dialog.set_error(message)
        self._preview_active = False
        self.preview_btn.setText("屏幕预览")

    def _on_screenshot_finished(self, worker: ScreenshotWorker) -> None:
        if self._screenshot_worker is worker:
            self._screenshot_worker = None
        self.screenshot_btn.setEnabled(True)
        if not self._closing and self._preview_active and self._screenshot_dialog is not None:
            QTimer.singleShot(750, self._request_screenshot_frame)

    def _on_screenshot_dialog_closed(self) -> None:
        self._preview_active = False
        self.preview_btn.setText("屏幕预览")
        self._screenshot_dialog = None

    def export_crash_selected(self) -> None:
        runtime = self.current_runtime()
        if runtime is None or self._crash_export_worker is not None:
            return
        if not self._confirm_action(
            "导出崩溃日志",
            "将通过 FTP 只读导出 /tmp/core.* 和 UI 程序文件到本机，不会修改设备。\n\n确定要导出吗？",
        ):
            return
        worker = CrashExportWorker(
            runtime.key, runtime.device_ip, runtime.pc_ip, runtime.suite, runtime.case_id,
            int(runtime.link.get("if_index", 0) or 0) or None,
        )
        self._crash_export_worker = worker
        self.export_crash_btn.setEnabled(False)
        worker.start()
        self._add_log(runtime, "开始导出崩溃日志")

    def _drain_crash_export_queue(self, worker: CrashExportWorker) -> None:
        runtime = self.runtimes.get(worker.key)
        try:
            while True:
                item = worker.queue.get_nowait()
                if item[0] == "progress" and runtime is not None:
                    self._add_log(runtime, item[1])
                elif item[0] == "result" and runtime is not None:
                    result = item[1]
                    self._add_log(runtime, f"崩溃日志已导出: {result['directory']}")
                    if result["missing"]:
                        self._add_log(runtime, f"部分文件未导出: {len(result['missing'])}", "error")
                elif item[0] == "error" and runtime is not None:
                    self._add_log(runtime, f"导出崩溃日志失败: {item[1]}", "error")
                elif item[0] == "finished":
                    if self._crash_export_worker is worker:
                        self._crash_export_worker = None
                        self.export_crash_btn.setEnabled(True)
        except queue.Empty:
            pass

    def _stop_screen_preview(self, message: str) -> None:
        self._preview_active = False
        self.preview_btn.setText("屏幕预览")
        if self._screenshot_dialog is not None:
            self._screenshot_dialog.set_error(message)

    # --- Worker queue polling (thread-safe, no QObject signals from threads) -----
    def _poll_workers(self) -> None:
        for runtime in list(self.runtimes.values()):
            # 主动清理已结束但引用未清的 command_thread，防止按钮长期置灰/请求被误拒
            self._command_thread_alive(runtime)
            w = runtime.watch_worker
            if w is not None:
                self._drain_watch_queue(w)
            r = runtime.record_worker
            if r is not None:
                self._drain_record_queue(r)
        s = self._screenshot_worker
        if s is not None:
            self._drain_screenshot_queue(s)
        c = self._crash_export_worker
        if c is not None:
            self._drain_crash_export_queue(c)
        o = self._ota_worker
        if o is not None:
            self._drain_ota_queue(o)
        p = self._ota_post_worker
        if p is not None:
            self._drain_ota_post_queue(p)
        b = self._batch_ota_worker
        if b is not None:
            self._drain_batch_ota_queue(b)
        s = self._sd_clean_worker
        if s is not None:
            self._drain_sd_clean_queue(s)
        self._reap_retired_runtimes()
        purge_stale_hubs()

    def _drain_watch_queue(self, w: WatchWorker) -> None:
        for _ in range(MAX_QUEUE_EVENTS_PER_TICK):
            try:
                item = w.queue.get_nowait()
                t = item[0]
                if t == "event":
                    self._on_watch_event(item[1], item[2])
                elif t == "error":
                    self._on_watch_error(item[1], item[2])
                elif t == "finished":
                    self._clear_watch(item[1])
            except queue.Empty:
                return

    def _drain_record_queue(self, r: RecordWorker) -> None:
        for _ in range(MAX_QUEUE_EVENTS_PER_TICK):
            try:
                item = r.queue.get_nowait()
                t = item[0]
                if t == "event":
                    self._on_record_event(item[1], item[2])
                elif t == "saved":
                    self._on_record_saved(item[1], item[2])
                elif t == "error":
                    self._on_record_error(item[1], item[2])
                elif t == "watch_error":
                    runtime = self.runtimes.get(item[1])
                    if runtime is not None and runtime.watch_worker is None:
                        self._on_watch_error(item[1], item[2])
                elif t == "finished":
                    self._clear_record(item[1])
            except queue.Empty:
                return

    def _drain_screenshot_queue(self, s: ScreenshotWorker) -> None:
        try:
            while True:
                item = s.queue.get_nowait()
                t = item[0]
                if t == "result":
                    self._on_screenshot_result(item[1], item[2])
                elif t == "error":
                    self._on_screenshot_error(item[1])
                elif t == "finished":
                    self._on_screenshot_finished(item[1])
        except queue.Empty:
            pass

    @staticmethod
    def _safe_frame_int(metadata: Dict[str, Any], key: str, max_value: int = 4096) -> int:
        try:
            value = int(metadata.get(key, 0))
            if value < 0 or value > max_value:
                return 0
            return value
        except (ValueError, TypeError):
            return 0

    @staticmethod
    def _frame_to_qimage(metadata: Dict[str, Any], raw: bytes) -> QImage:
        width = MainWindow._safe_frame_int(metadata, "w", 4096)
        height = MainWindow._safe_frame_int(metadata, "h", 4096)
        line_length = MainWindow._safe_frame_int(metadata, "line_length", 16384)
        if width <= 0 or height <= 0 or line_length <= 0 or len(raw) < line_length * height:
            return QImage()
        bpp = MainWindow._safe_frame_int(metadata, "bpp", 64)
        red_offset = MainWindow._safe_frame_int(metadata, "red_offset", 256)
        red_length = MainWindow._safe_frame_int(metadata, "red_length", 32)
        green_offset = MainWindow._safe_frame_int(metadata, "green_offset", 256)
        green_length = MainWindow._safe_frame_int(metadata, "green_length", 32)
        blue_offset = MainWindow._safe_frame_int(metadata, "blue_offset", 256)
        blue_length = MainWindow._safe_frame_int(metadata, "blue_length", 32)
        if bpp == 32 and (red_offset, green_offset, blue_offset) == (16, 8, 0):
            return QImage(raw, width, height, line_length, QImage.Format_ARGB32).copy()
        if bpp == 24 and (red_offset, green_offset, blue_offset) == (16, 8, 0) and (
            red_length, green_length, blue_length
        ) == (8, 8, 8):
            return QImage(raw, width, height, line_length, QImage.Format_BGR888).copy()
        if bpp == 24 and (red_offset, green_offset, blue_offset) == (0, 8, 16) and (
            red_length, green_length, blue_length
        ) == (8, 8, 8):
            return QImage(raw, width, height, line_length, QImage.Format_RGB888).copy()

        bytes_per_pixel = (bpp + 7) // 8
        if bytes_per_pixel <= 0 or line_length < width * bytes_per_pixel:
            return QImage()
        rgb = bytearray(width * height * 3)

        def component(pixel: int, offset: int, length: int) -> int:
            if length <= 0:
                return 0
            value = (pixel >> offset) & ((1 << length) - 1)
            return value * 255 // ((1 << length) - 1)

        out = 0
        for y in range(height):
            row = y * line_length
            for x in range(width):
                start = row + x * bytes_per_pixel
                pixel = int.from_bytes(raw[start:start + bytes_per_pixel], "little")
                rgb[out] = component(pixel, red_offset, red_length)
                rgb[out + 1] = component(pixel, green_offset, green_length)
                rgb[out + 2] = component(pixel, blue_offset, blue_length)
                out += 3
        return QImage(bytes(rgb), width, height, width * 3, QImage.Format_RGB888).copy()

    # --- Independent watchers -------------------------------------------------------
    def _confirm_action(self, title: str, message: str) -> bool:
        box = QMessageBox(self)
        box.setIcon(QMessageBox.Question)
        box.setWindowTitle(title)
        box.setText(message)
        box.setStandardButtons(QMessageBox.Ok | QMessageBox.Cancel)
        box.button(QMessageBox.Ok).setText("确定")
        box.button(QMessageBox.Cancel).setText("取消")
        box.setDefaultButton(QMessageBox.Cancel)
        return box.exec() == QMessageBox.Ok

    def toggle_watch_selected(self) -> None:
        runtime = self.current_runtime()
        if runtime is None:
            return
        if runtime.watch_worker is None:
            if not self._confirm_action(
                "开始监视",
                "开始监视只接收当前设备的用例状态，不会启动或停止设备用例，也不会保存测试记录。\n\n确定要开始监视吗？",
            ):
                return
            self._ensure_watch(runtime)
        else:
            if not self._confirm_action(
                "停止监视",
                "停止监视将不再接收该设备的用例状态，不会停止设备当前正在运行的用例。\n\n确定要停止监视吗？",
            ):
                return
            self._stop_watch(runtime)

    def _ensure_watch(self, runtime: DeviceRuntime) -> None:
        start = time.monotonic()
        _APP_LOG.info(
            "ensure_watch_enter key=%s host=%s source_ip=%s watch_present=%s thread=%s",
            runtime.key,
            runtime.device_ip,
            runtime.pc_ip,
            runtime.watch_worker is not None,
            threading.current_thread().name,
        )
        if runtime.watch_worker is not None:
            if runtime.watch_worker.is_running():
                _APP_LOG.info("ensure_watch_already_running key=%s", runtime.key)
                return  # already watching
            # Previous worker was stopped; clear stale reference
            runtime.watch_worker = None
            _APP_LOG.info("ensure_watch_cleared_stale_worker key=%s", runtime.key)
        worker = WatchWorker(
            runtime.key, runtime.device_ip, runtime.port, runtime.pc_ip,
            int(runtime.link.get("if_index", 0) or 0) or None,
        )
        runtime.watch_worker = worker
        _APP_LOG.info("ensure_watch_before_worker_start key=%s", runtime.key)
        worker.start()
        _APP_LOG.info(
            "ensure_watch_after_worker_start key=%s elapsed=%.3fs",
            runtime.key, time.monotonic() - start,
        )
        self._add_log(runtime, "开始监视")
        self._schedule_ui_sync()
        if runtime.key == self.current_key:
            self._update_detail_panel()

    def _stop_watch(self, runtime: DeviceRuntime) -> None:
        if runtime.watch_worker is not None:
            runtime.watch_worker.stop()
            runtime.watch_worker = None
            self._add_log(runtime, "监视已停止")

    def _clear_watch(self, key: str) -> None:
        runtime = self.runtimes.get(key)
        if runtime is None:
            return
        runtime.watch_worker = None
        self._schedule_ui_sync()
        if key == self.current_key:
            self._update_detail_panel()

    def _on_watch_event(self, key: str, event: Dict[str, Any]) -> None:
        runtime = self.runtimes.get(key)
        if runtime is None:
            return

        # [testagent-monitor] handle event_alert pushed by daemon
        if event.get("cmd") == "event_alert":
            try:
                alerts = event.get("alerts", [])
                if not isinstance(alerts, list):
                    _APP_LOG.warning("event_alert alerts is not a list: %s", type(alerts).__name__)
                    return
                for alert in alerts:
                    if not isinstance(alert, dict):
                        continue
                    msg = alert.get("msg", "unknown alert")
                    sev = alert.get("severity", "info")
                    self._add_log(runtime, f"[{sev.upper()}] {msg}")
            except Exception as exc:
                _APP_LOG.warning("event_alert processing error: %s", exc)
            return

        before = self._runtime_status_fingerprint(runtime)
        runtime.online = True
        runtime.transport_error_streak = 0
        if not self._handle_ui_status_availability(runtime, event):
            if before != self._runtime_status_fingerprint(runtime):
                self._schedule_status_refresh()
            return
        self._apply_event(runtime, event)
        if before != self._runtime_status_fingerprint(runtime):
            self._log_status_event(runtime, event)
            self._schedule_status_refresh()

    def _on_watch_error(self, key: str, message: str) -> None:
        _APP_LOG.error("watch_error key=%s message=%s", key, message)
        runtime = self.runtimes.get(key)
        if runtime is None:
            return
        before = self._runtime_status_fingerprint(runtime)
        runtime.online = False
        runtime.last_msg = message
        self._add_log(runtime, f"监视连接失败: {message}", "error")
        if before != self._runtime_status_fingerprint(runtime):
            self._schedule_status_refresh()

    # --- Independent recording ------------------------------------------------------
    def toggle_record_selected(self) -> None:
        runtime = self.current_runtime()
        if runtime is None:
            return
        if runtime.record_worker is not None:
            if not self._confirm_action(
                "停止运行",
                "停止运行将停止 PC 端的状态监听并保存当前测试记录，不会停止设备当前正在运行的用例。\n\n确定要停止运行吗？",
            ):
                return
            self._stop_record(runtime)
            return

        descriptor = self._selected_case_descriptor(runtime)
        if descriptor is not None and not descriptor.get("selectable", True):
            QMessageBox.warning(self, "开始运行", "当前用例尚未实现，不能运行")
            return
        confirm_risk = self._confirm_risk(runtime, descriptor)
        if confirm_risk is None:
            return

        if not self._confirm_action(
            "开始运行",
            "开始运行会启动当前选中的设备用例，并在 PC 端持续记录状态；结束后会保存测试记录和报告。\n\n确定要开始运行吗？",
        ):
            return

        self._start_recording(runtime, confirm_risk)

    def _start_recording(self, runtime: DeviceRuntime, confirm_risk: bool) -> None:
        if runtime.record_worker is not None:
            self._add_log(runtime, "测试记录正在进行，已忽略重复启动", "error")
            return

        if runtime.watch_worker is not None:
            self._add_log(runtime, "录制将复用当前监视状态连接，不会增加设备轮询")

        record_dir = defects_dir(runtime.device_ip)
        wait_timeout: float | None = None
        if self._is_custom_c01(runtime):
            wait_timeout = custom_c01_monitor_timeout_seconds(runtime.custom_estimated_runtime_ms)
            if runtime.custom_estimated_runtime_ms is None:
                self._add_log(
                    runtime,
                    "C01 未返回设备预计耗时；PC 将按设备 30 天最大时长继续监控，不使用默认一小时超时。",
                    "error",
                )
            _APP_LOG.info(
                "custom_c01_monitor_timeout key=%s revision=%s crc=%s estimate_ms=%s timeout_seconds=%.3f",
                runtime.key,
                runtime.custom_config_revision,
                runtime.custom_config_crc,
                runtime.custom_estimated_runtime_ms,
                wait_timeout,
            )
            _APP_LOG.info(
                "custom_c01_run_start key=%s device_ip=%s source_ip=%s firmware_version=%s "
                "expected_config_version=%s expected_policy_version=%s snapshot=%s",
                runtime.key,
                runtime.device_ip,
                runtime.pc_ip,
                runtime.firmware_version or "<unknown>",
                CUSTOM_CONFIG_VERSION,
                CUSTOM_POLICY_VERSION,
                _custom_snapshot_diagnostics(runtime.custom_config_snapshot),
            )
        worker = RecordWorker(
            runtime.key, runtime.device_ip, runtime.port, runtime.pc_ip, runtime.suite,
            runtime.case_id, record_dir, confirm_risk=confirm_risk,
            source_if_index=int(runtime.link.get("if_index", 0) or 0) or None,
            **({"wait_timeout": wait_timeout} if wait_timeout is not None else {}),
            **({"custom_config_snapshot": runtime.custom_config_snapshot}
               if self._is_custom_c01(runtime) else {}),
        )
        runtime.record_worker = worker
        runtime.progress_current = 0
        runtime.progress_total = 0
        runtime.status = "queued"
        runtime.last_msg = "准备运行并记录"
        worker.start()
        self._add_log(runtime, f"开始运行并记录，结果将保存到: {record_dir}")
        self._schedule_ui_sync()
        self._update_detail_panel()

    def _stop_record(self, runtime: DeviceRuntime) -> None:
        if runtime.record_worker is not None:
            runtime.record_worker.stop()
            self._add_log(runtime, "录制已停止，正在保存...")

    def _clear_record(self, key: str) -> None:
        runtime = self.runtimes.get(key)
        if runtime is None:
            return
        runtime.record_worker = None
        self._schedule_ui_sync()
        if key == self.current_key:
            self._update_detail_panel()

    def _on_record_event(self, key: str, event: Dict[str, Any]) -> None:
        runtime = self.runtimes.get(key)
        if runtime is None:
            return
        if runtime.watch_worker is not None:
            return
        before = self._runtime_status_fingerprint(runtime)
        runtime.online = True
        runtime.transport_error_streak = 0
        if not self._handle_ui_status_availability(runtime, event):
            if before != self._runtime_status_fingerprint(runtime):
                self._schedule_status_refresh()
            return
        self._apply_event(runtime, event)
        if before != self._runtime_status_fingerprint(runtime):
            self._log_status_event(runtime, event)
            self._schedule_status_refresh()

    def _schedule_status_refresh(self) -> None:
        self._status_dirty = True

    @staticmethod
    def _runtime_status_fingerprint(runtime: DeviceRuntime) -> tuple[object, ...]:
        return (
            runtime.online,
            runtime.suite,
            runtime.case_id,
            runtime.status,
            runtime.progress_current,
            runtime.progress_total,
            runtime.error_code,
            runtime.started_at_ms,
            runtime.finished_at_ms,
            runtime.ui_status_unavailable,
            runtime.last_msg,
        )

    def _handle_ui_status_availability(self, runtime: DeviceRuntime, event: Dict[str, Any]) -> bool:
        if event.get("cmd") != "case_status_event":
            return True
        status = event.get("status") if event.get("cmd") == "case_status_event" else event
        if not isinstance(status, dict):
            return True
        if status.get("code") == -10:
            if not runtime.ui_status_unavailable:
                runtime.ui_status_unavailable = True
                runtime.last_msg = "UI bridge unavailable"
                self._add_log(runtime, "设备 UI 状态暂不可用，正在等待恢复")
            return False
        if runtime.ui_status_unavailable:
            runtime.ui_status_unavailable = False
            self._add_log(runtime, "设备 UI 状态已恢复")
        return True

    def _flush_status_updates(self) -> None:
        if not self._status_dirty:
            return
        self._status_dirty = False
        self._schedule_ui_sync()
        if self.current_key is not None:
            self._update_detail_panel()

    def _on_record_saved(self, key: str, path: str) -> None:
        runtime = self.runtimes.get(key)
        if runtime is not None:
            self._add_log(runtime, f"录制已保存: {path}")

    def _on_record_error(self, key: str, message: str) -> None:
        runtime = self.runtimes.get(key)
        if runtime is not None:
            self._add_log(runtime, f"录制失败: {message}", "error")
            if self._is_custom_c01(runtime):
                _APP_LOG.error(
                    "custom_record_error key=%s device_ip=%s firmware_version=%s "
                    "message=%s snapshot=%s",
                    runtime.key,
                    runtime.device_ip,
                    runtime.firmware_version or "<unknown>",
                    message,
                    _custom_snapshot_diagnostics(runtime.custom_config_snapshot),
                )

    def _log_status_event(self, runtime: DeviceRuntime, event: Dict[str, Any]) -> None:
        status = event.get("status") if event.get("cmd") == "case_status_event" else event
        if isinstance(status, dict) and status.get("code") == -10:
            return
        self._add_log(runtime, json.dumps(event, ensure_ascii=False, separators=(",", ":")))
        if not self._is_custom_c01(runtime) or not isinstance(status, dict):
            return
        state = str(status.get("status", ""))
        error_code = status.get("error_code", status.get("last_error", status.get("code")))
        if state not in {"failed", "error"} and error_code in (None, 0):
            return
        _APP_LOG.error(
            "custom_case_status_failure key=%s device_ip=%s source_ip=%s firmware_version=%s "
            "suite=%s current_case_id=%s last_case_id=%s status=%s error_code=%s "
            "last_msg=%s current=%s total=%s expected_config_version=%s "
            "expected_policy_version=%s snapshot=%s event=%s",
            runtime.key,
            runtime.device_ip,
            runtime.pc_ip,
            runtime.firmware_version or "<unknown>",
            runtime.suite,
            status.get("current_case_id"),
            status.get("last_case_id"),
            state,
            error_code,
            status.get("last_msg", status.get("msg")),
            status.get("current"),
            status.get("total"),
            CUSTOM_CONFIG_VERSION,
            CUSTOM_POLICY_VERSION,
            _custom_snapshot_diagnostics(runtime.custom_config_snapshot),
            event,
        )

    # --- Event/status mapping -------------------------------------------------------
    def _apply_event(self, runtime: DeviceRuntime, event: Dict[str, Any]) -> None:
        status = event.get("status") if event.get("cmd") == "case_status_event" else event
        if not isinstance(status, dict):
            return
        state = status.get("status")
        if state:
            runtime.status = str(state)
        reported_suite = status.get("current_suite") if status.get("current_case_id") else status.get("last_suite")
        if reported_suite in runtime.catalog:
            runtime.suite = str(reported_suite)
        reported_case_id = status.get("current_case_id") or status.get("last_case_id")
        if isinstance(reported_case_id, int) and reported_case_id > 0:
            runtime.case_id = reported_case_id
        message = status.get("last_msg", status.get("msg"))
        if message:
            runtime.last_msg = str(message)
        current, total = extract_progress(status)
        has_explicit_progress = isinstance(status.get("current"), int) and isinstance(
            status.get("total"), int,
        )
        if has_explicit_progress or total > 0:
            runtime.progress_current = current
            runtime.progress_total = total
        error_code = status.get("error_code", status.get("last_error"))
        if isinstance(error_code, int):
            runtime.error_code = error_code
        started_at_ms = status.get("started_at_ms")
        if isinstance(started_at_ms, int):
            runtime.started_at_ms = started_at_ms
        finished_at_ms = status.get("finished_at_ms")
        if isinstance(finished_at_ms, int):
            runtime.finished_at_ms = finished_at_ms

    def _set_progress(self, runtime: DeviceRuntime) -> None:
        if runtime.progress_total > 0:
            value = min(int(runtime.progress_current * 100 / runtime.progress_total), 100)
            text = f"{runtime.progress_current}/{runtime.progress_total}"
            self.progress_bar.setValue(value)
            self.progress_bar.setFormat(text)
            self.progress_text.setText(text)
        else:
            self.progress_bar.setValue(0)
            self.progress_bar.setFormat("")
            self.progress_text.setText("--/--")

    def _set_status_label(self, status: str) -> None:
        color, _ = STATUS_COLORS.get(status, ("#64748b", "#f8fafc"))
        self.status_label.setText(status or "-")
        self.status_label.setStyleSheet(f"font-size:13px; font-weight:700; color:{color};")

    # --- Logs ----------------------------------------------------------------------
    def _add_log(self, runtime: DeviceRuntime, text: str, level: str = "info") -> None:
        self.log_entries.append(
            {
                "time": time.strftime("%H:%M:%S"),
                "key": runtime.key,
                "device": runtime.device_ip,
                "level": level,
                "text": text,
            }
        )
        if len(self.log_entries) > MAX_LOG_ENTRIES:
            self.log_entries = self.log_entries[-MAX_LOG_ENTRIES:]
        self.render_logs()

    def render_logs(self) -> None:
        if self._log_pending:
            return
        self._log_pending = True
        QTimer.singleShot(250, self._flush_logs)

    _FLUSH_LOG_WARNED = False

    def _flush_logs(self) -> None:
        self._log_pending = False
        try:
            mode = self.log_filter.currentText()
            lines: list[str] = []
            for entry in self.log_entries:
                if mode == "当前设备" and entry["key"] != self.current_key:
                    continue
                if mode == "仅错误" and entry["level"] != "error":
                    continue
                prefix = f"{entry['time']} [{entry['device']}]"
                lines.append(f"{prefix} {entry['text']}")
            snapshot = lines[-1000:]
            if mode == self._log_last_mode:
                prev_len = len(self._log_last_snapshot)
                if prev_len > 0 and len(snapshot) > prev_len:
                    self.log_view.appendPlainText("\n".join(snapshot[prev_len:]))
                    return
            self.log_view.setPlainText("\n".join(snapshot))
            self._log_last_mode = mode
            self._log_last_snapshot = snapshot
        except RuntimeError:
            if not MainWindow._FLUSH_LOG_WARNED:
                MainWindow._FLUSH_LOG_WARNED = True
                print("[testagent_gui] log flush suppressed after window close", file=sys.stderr)
        finally:
            try:
                scrollbar = self.log_view.verticalScrollBar()
                scrollbar.setValue(scrollbar.maximum())
            except RuntimeError:
                pass

    def toggle_logs(self) -> None:
        if self.log_panel.isVisible():
            self.log_panel.hide()
            self.log_toggle_btn.setText("展开日志 ▾")
        else:
            self.log_panel.show()
            self.log_toggle_btn.setText("收起日志 ▾")
            total = self.splitter.height()
            self.splitter.setSizes([max(total - 200, 200), 200])

    # --- Network config -------------------------------------------------------------
    def open_network_config(self) -> None:
        if self.network_dialog is None:
            self.network_dialog = NetworkConfigDialog(self)
            self.network_dialog.configured.connect(self._on_network_configured)
        else:
            self.network_dialog.refresh()
        self.network_dialog.show()
        self.network_dialog.raise_()
        self.network_dialog.activateWindow()

    def _on_network_configured(self, payload: Dict[str, Any], *, refresh: bool = True) -> None:
        device = payload.get("device", {})
        result = payload.get("result", {})
        pc_ip = str(device.get("pc_ip", ""))
        if not pc_ip:
            return
        link_key = str(device.get("link_id") or device.get("adapter_id") or pc_ip)
        runtime = self.runtimes.get(link_key)
        if runtime is None:
            runtime = DeviceRuntime(
                iface=str(device.get("iface", "RNDIS")),
                pc_ip=pc_ip,
                device_ip=str(device.get("device_ip", DEFAULT_DEVICE_IP)),
                link=dict(device),
                configured=bool(result.get("success", False)),
                notes=get_device_note(device.get("adapter_id", "")),
            )
            self.runtimes[runtime.key] = runtime
        else:
            runtime.iface = str(device.get("iface", runtime.iface))
            runtime.link = dict(device)
            runtime.configured = bool(result.get("success", False))
        runtime.device_ip = str(result.get("target_ip") or device.get("device_ip", runtime.device_ip))
        runtime.link["device_ip"] = runtime.device_ip
        verified = bool(result.get("success", False))
        runtime.link["configured"] = verified
        runtime.configured = verified
        if self.current_key is None:
            self.current_key = runtime.key
        runtime.last_msg = (
            f"网络配置: {runtime.device_ip} (已验证)" if verified else
            f"网络配置失败: {result.get('error', '未知错误')}"
        )
        runtime.online = None
        self._add_log(runtime, runtime.last_msg)
        self._schedule_ui_sync()
        self._select_current_row()
        if not self._closing:
            QTimer.singleShot(500, lambda key=runtime.key: self._probe_configured_runtime(key))
        if refresh and not self._closing:
            QTimer.singleShot(1500, self.refresh_devices)

    def _probe_configured_runtime(self, key: str) -> None:
        runtime = self.runtimes.get(key)
        if runtime is not None and runtime.configured:
            self._probe_runtime(runtime)

    # --- OTA upgrade ----------------------------------------------------------------
    def start_ota(self) -> None:
        if self._ota_worker is not None:
            QMessageBox.warning(self, "OTA", "OTA 升级正在进行中")
            return

        runtime = self.current_runtime()
        if runtime is None or runtime.online is not True:
            QMessageBox.warning(self, "OTA", "请先选择一个在线的设备")
            return

        fw_path, _ = QFileDialog.getOpenFileName(
            self, "选择固件文件", "", "固件文件 (*.bin *.zip);;所有文件 (*)",
        )
        if not fw_path:
            return

        fw_name = os.path.basename(fw_path)
        if fw_name.lower().endswith(".bin") and "kd" not in fw_name.lower():
            reply = QMessageBox.question(
                self,
                "非测试固件警告",
                "所选 .bin 文件名不含 kd，可能不是测试固件。\n\n"
                "升级后此设备将无法继续使用 Pocket TestAgent 应用。\n\n"
                "仍要继续升级吗？",
                QMessageBox.Yes | QMessageBox.No,
                QMessageBox.No,
            )
            if reply != QMessageBox.Yes:
                return

        hint = (
            f"即将执行 OTA 升级：\n\n"
            f"固件: {fw_name}\n"
            f"设备: {runtime.device_ip}\n\n"
            f"升级后将自动等待 Jenkins 镜像中的 TestAgent 恢复并重新连接。\n"
            f"确认升级？"
        )
        reply = QMessageBox.question(self, "确认 OTA 升级", hint, QMessageBox.Yes | QMessageBox.No)
        if reply != QMessageBox.Yes:
            return

        self.ota_btn.setEnabled(False)
        self._add_log(runtime, f"开始 OTA 升级: {fw_name}", "info")
        worker = OTAUpgradeWorker(fw_path, runtime.device_ip, runtime.link)
        self._ota_worker = worker
        worker.start()

    def _select_online_devices(self, title: str) -> tuple[list[Dict[str, Any]], bool]:
        """Let the user pick a subset of online devices.

        Returns (selected_links, has_any_online).  Empty list means the user
        cancelled or nothing was checked; callers should check has_any_online
        to decide whether to warn about having no online devices at all.
        """
        online = [
            (runtime.link, f"{runtime.device_ip} ({runtime.iface})")
            for runtime in self.runtimes.values()
            if runtime.configured and runtime.online is True
        ]
        has_any = bool(online)
        if not online:
            return [], False
        if len(online) == 1:
            return [online[0][0]], True

        dialog = QDialog(self)
        dialog.setWindowTitle(title)
        dialog.setMinimumWidth(360)

        label = QLabel("选择设备（默认全选）：")
        checks: list[tuple[QCheckBox, Dict[str, Any]]] = []
        list_widget = QWidget()
        check_layout = QVBoxLayout(list_widget)
        for link, display in online:
            cb = QCheckBox(display)
            cb.setChecked(True)
            check_layout.addWidget(cb)
            checks.append((cb, link))

        scroll = QScrollArea()
        scroll.setWidget(list_widget)
        scroll.setWidgetResizable(True)
        scroll.setMaximumHeight(250)

        btn_layout = QHBoxLayout()
        select_all = QPushButton("全选")
        deselect_all = QPushButton("取消全选")
        confirm = QPushButton("确定")
        cancel = QPushButton("取消")
        select_all.clicked.connect(lambda: [c[0].setChecked(True) for c in checks])
        deselect_all.clicked.connect(lambda: [c[0].setChecked(False) for c in checks])
        confirm.clicked.connect(dialog.accept)
        cancel.clicked.connect(dialog.reject)
        btn_layout.addWidget(select_all)
        btn_layout.addWidget(deselect_all)
        btn_layout.addStretch(1)
        btn_layout.addWidget(confirm)
        btn_layout.addWidget(cancel)

        root = QVBoxLayout(dialog)
        root.addWidget(label)
        root.addWidget(scroll)
        root.addLayout(btn_layout)

        if not dialog.exec():
            return [], True
        return [link for cb, link in checks if cb.isChecked()], True

    def start_batch_ota(self) -> None:
        if self._batch_ota_worker is not None:
            QMessageBox.warning(self, "一键OTA", "一键 OTA 升级正在进行中")
            return
        if self._ota_worker is not None:
            QMessageBox.warning(self, "一键OTA", "OTA 升级正在进行中，请等待完成后再执行批量升级")
            return

        selected, has_online = self._select_online_devices("选择 OTA 升级设备")
        if not selected:
            if not has_online:
                QMessageBox.warning(self, "一键OTA", "没有可用的在线设备")
            return
        online_links = selected

        fw_path, _ = QFileDialog.getOpenFileName(
            self, "选择固件文件", "", "固件文件 (*.bin *.zip);;所有文件 (*)",
        )
        if not fw_path:
            return

        fw_name = os.path.basename(fw_path)
        if fw_name.lower().endswith(".bin") and "kd" not in fw_name.lower():
            reply = QMessageBox.question(
                self,
                "非测试固件警告",
                "所选 .bin 文件名不含 kd，可能不是测试固件。\n\n"
                "升级后这些设备将无法继续使用 Pocket TestAgent 应用。\n\n"
                "仍要继续升级吗？",
                QMessageBox.Yes | QMessageBox.No,
                QMessageBox.No,
            )
            if reply != QMessageBox.Yes:
                return

        hint = (
            f"即将对 {len(online_links)} 台设备执行一键 OTA 升级：\n\n"
            f"固件: {fw_name}\n"
            f"设备: {', '.join(str(link.get('device_ip', '?')) for link in online_links)}\n\n"
            f"将逐台删除旧固件并上传新固件，全部设备同时重启。\n"
            f"确认升级？"
        )
        reply = QMessageBox.question(self, "确认一键 OTA 升级", hint, QMessageBox.Yes | QMessageBox.No)
        if reply != QMessageBox.Yes:
            return

        self.batch_ota_btn.setEnabled(False)
        self.ota_btn.setEnabled(False)
        self._add_log(
            self.current_runtime() or DeviceRuntime("batch", "", ""),
            f"开始一键 OTA 升级: {fw_name} → {len(online_links)} 台设备", "info",
        )
        worker = OTABatchWorker(fw_path, online_links)
        self._batch_ota_worker = worker
        worker.start()

    def _drain_batch_ota_queue(self, w: OTABatchWorker) -> None:
        try:
            while True:
                item = w.queue.get_nowait()
                t = item[0]
                if t == "step":
                    self._add_log(
                        self.current_runtime() or DeviceRuntime("batch", "", ""),
                        item[1], item[2],
                    )
                elif t == "error":
                    self._add_log(
                        self.current_runtime() or DeviceRuntime("batch", "", ""),
                        item[1], "error",
                    )
                elif t == "summary_fail":
                    failed_ips = item[1]
                    success_count = item[2]
                    self._add_log(
                        self.current_runtime() or DeviceRuntime("batch", "", ""),
                        f"OTA 升级：{success_count} 台成功，{len(failed_ips)} 台失败 — {', '.join(failed_ips)}",
                        "error",
                    )
                    box = QMessageBox(self)
                    box.setIcon(QMessageBox.Warning)
                    box.setWindowTitle("一键 OTA 升级")
                    box.setText(
                        f"升级完成：{success_count} 台成功，{len(failed_ips)} 台失败。\n\n"
                        f"失败设备：{', '.join(failed_ips)}\n\n"
                        f"请手动检查并对失败设备重新执行 OTA 升级。"
                    )
                    box.setModal(False)
                    box.show()
                elif t == "finished":
                    self._batch_ota_worker_done(w)
        except queue.Empty:
            pass

    def _batch_ota_worker_done(self, worker: OTABatchWorker) -> None:
        if self._batch_ota_worker is worker:
            self._batch_ota_worker = None
        self.batch_ota_btn.setEnabled(True)
        self.ota_btn.setEnabled(True)

    def start_sd_clean(self) -> None:
        if self._sd_clean_worker is not None:
            QMessageBox.warning(self, "清理SD卡", "SD 卡清理正在进行中")
            return

        selected, has_online = self._select_online_devices("选择清理 SD 卡设备")
        if not selected:
            if not has_online:
                QMessageBox.warning(self, "清理SD卡", "没有可用的在线设备")
            return
        online_links = selected

        hint = (
            f"将对 {len(online_links)} 台设备清理 SD 卡：\n"
            f"设备: {', '.join(str(link.get('device_ip', '?')) for link in online_links)}\n\n"
            f"将彻底删除 /sdcard 下的所有文件（含 firmware）。\n"
            f"确认清理？"
        )
        reply = QMessageBox.question(self, "确认清理 SD 卡", hint, QMessageBox.Yes | QMessageBox.No)
        if reply != QMessageBox.Yes:
            return

        self.sd_clean_btn.setEnabled(False)
        self._add_log(
            self.current_runtime() or DeviceRuntime("batch", "", ""),
            f"开始清理 SD 卡: {len(online_links)} 台设备", "info",
        )
        worker = SDSDCleanWorker(online_links)
        self._sd_clean_worker = worker
        worker.start()

    def _drain_sd_clean_queue(self, w: SDSDCleanWorker) -> None:
        try:
            while True:
                item = w.queue.get_nowait()
                t = item[0]
                if t == "step":
                    self._add_log(
                        self.current_runtime() or DeviceRuntime("batch", "", ""),
                        item[1], item[2],
                    )
                elif t == "error":
                    self._add_log(
                        self.current_runtime() or DeviceRuntime("batch", "", ""),
                        item[1], "error",
                    )
                elif t == "summary_fail":
                    failed_ips = item[1]
                    success_count = item[2]
                    self._add_log(
                        self.current_runtime() or DeviceRuntime("batch", "", ""),
                        f"SD卡清理：{success_count} 台成功，{len(failed_ips)} 台失败 — {', '.join(failed_ips)}",
                        "error",
                    )
                    box = QMessageBox(self)
                    box.setIcon(QMessageBox.Warning)
                    box.setWindowTitle("清理 SD 卡")
                    box.setText(
                        f"清理完成：{success_count} 台成功，{len(failed_ips)} 台失败。\n\n"
                        f"失败设备：{', '.join(failed_ips)}\n\n"
                        f"请手动检查失败设备的 FTP 连接后重试。"
                    )
                    box.setModal(False)
                    box.show()
                elif t == "finished":
                    self._sd_clean_worker_done(w)
        except queue.Empty:
            pass

    def _sd_clean_worker_done(self, worker: SDSDCleanWorker) -> None:
        if self._sd_clean_worker is worker:
            self._sd_clean_worker = None
        self.sd_clean_btn.setEnabled(True)

    def _drain_ota_queue(self, w: OTAUpgradeWorker) -> None:
        try:
            while True:
                item = w.queue.get_nowait()
                t = item[0]
                if t == "step":
                    self._add_log(self.current_runtime() or DeviceRuntime("", "", DEPLOY_FTP_IP), item[1], item[2])
                elif t == "error":
                    self._add_log(self.current_runtime() or DeviceRuntime("", "", DEPLOY_FTP_IP), f"OTA 升级失败: {item[1]}", "error")
                elif t == "need_reboot":
                    self._on_ota_need_reboot(item[1])
                elif t == "bin_conflict":
                    self._on_ota_bin_conflict(item[1], item[2])
                elif t == "finished":
                    self._ota_worker_done(w)
        except queue.Empty:
            pass

    def _on_ota_bin_conflict(self, existing_bins, firmware_path: str) -> None:
        bin_list = ", ".join(existing_bins) if existing_bins else ""
        msg = (
            f"检测到设备 /sdcard/firmware 下存在 {len(existing_bins)} 个旧 .bin 固件：\n"
            f"{bin_list}\n\n"
            f"为避免升级冲突，本次 OTA 升级已中止。\n"
            f"请删除上述旧固件后，重新执行 OTA 升级。"
        )
        box = QMessageBox(self)
        box.setIcon(QMessageBox.Warning)
        box.setWindowTitle("OTA 升级已中止")
        box.setText(msg)
        open_btn = box.addButton("打开文件夹", QMessageBox.ActionRole)
        box.addButton("确定", QMessageBox.AcceptRole)
        box.setDefaultButton(open_btn)
        box.setModal(False)
        local_dir = os.path.dirname(firmware_path)
        box.buttonClicked.connect(
            lambda btn, _d=local_dir: QDesktopServices.openUrl(QUrl.fromLocalFile(_d))
            if btn == open_btn else None
        )
        box.show()
        self._add_log(
            self.current_runtime() or DeviceRuntime("", "", DEPLOY_FTP_IP),
            f"OTA 升级已中止：检测到旧 .bin 固件 {bin_list}，请删除后重新升级",
            "error",
        )

    def _drain_ota_post_queue(self, w: OTAPostWorker) -> None:
        try:
            while True:
                item = w.queue.get_nowait()
                t = item[0]
                if t == "step":
                    self._add_log(self.current_runtime() or DeviceRuntime("", "", DEPLOY_FTP_IP), item[1], item[2])
                elif t == "error":
                    self._add_log(self.current_runtime() or DeviceRuntime("", "", DEPLOY_FTP_IP), f"OTA 后配置失败: {item[1]}", "error")
                elif t == "result":
                    self._on_ota_post_result(item[1])
                elif t == "finished_post":
                    self._ota_post_worker_done(w)
        except queue.Empty:
            pass

    def _on_ota_need_reboot(self, link: Dict[str, Any]) -> None:
        expected_ip = str(link.get("device_ip", ""))
        self._add_log(
            self.current_runtime() or DeviceRuntime("", "", expected_ip),
            "等待设备自动重启，并自动恢复网络配置", "info",
        )
        self._ota_post_worker = OTAPostWorker(link)
        self._ota_post_worker.start()

    def _on_ota_post_result(self, result: Dict[str, Any]) -> None:
        QMessageBox.information(
            self, "设备就绪",
            f"设备已就绪: {result.get('target_ip', 'unknown')}:19099\n\n"
            "设备列表即将刷新。",
        )
        if not self._closing:
            QTimer.singleShot(1000, self.refresh_devices)

    def _ota_worker_done(self, worker: OTAUpgradeWorker) -> None:
        if self._ota_worker is worker:
            self._ota_worker = None
        if self._ota_post_worker is None:
            self.ota_btn.setEnabled(True)

    def _ota_post_worker_done(self, worker: OTAPostWorker) -> None:
        if self._ota_post_worker is worker:
            self._ota_post_worker = None
        self.ota_btn.setEnabled(True)

    # --- Records/report -------------------------------------------------------------
    def list_records(self) -> None:
        runtime = self.current_runtime()
        if runtime is None:
            return
        record_dir = defects_dir(runtime.device_ip)
        if not os.path.isdir(record_dir):
            self._add_log(runtime, f"没有记录目录: {record_dir}")
            return
        for path in sorted(
            (os.path.join(record_dir, name) for name in os.listdir(record_dir) if name.endswith(".json")),
            reverse=True,
        )[:20]:
            try:
                self._add_log(runtime, json.dumps(summarize_record(path), ensure_ascii=False, separators=(",", ":")))
            except Exception as exc:
                self._add_log(runtime, f"读取记录失败: {exc}", "error")

    def generate_report_selected(self) -> None:
        runtime = self.current_runtime()
        if runtime is None:
            return
        record_dir = defects_dir(runtime.device_ip)
        path, _ = QFileDialog.getOpenFileName(self, "选择录制文件", record_dir, "JSON (*.json)")
        if not path:
            return
        try:
            report_path = generate_report(path)
            import webbrowser

            webbrowser.open(f"file://{os.path.abspath(report_path)}")
            self._add_log(runtime, f"报告已生成: {report_path}")
        except Exception as exc:
            self._add_log(runtime, f"报告生成失败: {exc}", "error")

    def open_device_folder(self) -> None:
        runtime = self.current_runtime()
        if runtime is None:
            return
        try:
            ipaddress.ip_address(runtime.device_ip)
        except ValueError:
            self._add_log(runtime, f"设备 IP 无效，无法打开 FTP 文件夹: {runtime.device_ip}", "error")
            return

        ftp_url = f"ftp://{runtime.device_ip}/"
        # Windows 上用资源管理器打开 FTP 文件夹视图。QDesktopServices.openUrl
        # 对 ftp:// 会走系统默认 handler（现代 Windows 默认是浏览器），不是用户
        # 想要的文件夹视图。用 explorer.exe 直接指定，DETACHED_PROCESS 让其完全
        # 脱离父进程，避免继承句柄或触发 Shell COM 阻塞主线程。
        if os.name == "nt":
            try:
                subprocess.Popen(
                    ["explorer.exe", ftp_url],
                    creationflags=getattr(subprocess, "DETACHED_PROCESS", 0),
                    close_fds=True,
                )
            except OSError as exc:
                self._add_log(runtime, f"无法打开设备 FTP 文件夹: {exc}", "error")
            else:
                self._add_log(runtime, f"已在资源管理器中打开设备 FTP 文件夹: {ftp_url}")
            return

        url = QUrl(ftp_url)
        if QDesktopServices.openUrl(url):
            self._add_log(runtime, f"已打开设备 FTP 文件夹: {url.toString()}")
        else:
            self._add_log(
                runtime,
                f"无法打开设备 FTP 文件夹，请手动复制链接: {url.toString()}", "error",
            )

    # --- Presentation ---------------------------------------------------------------
    def _connection_text(self, runtime: DeviceRuntime) -> str:
        if not runtime.configured:
            return "未配置"
        if runtime.online is True:
            return "在线"
        if runtime.online is False:
            return "离线"
        return "检测中"

    def _progress_text(self, runtime: DeviceRuntime) -> str:
        if runtime.progress_total > 0:
            return f"{runtime.progress_current}/{runtime.progress_total}"
        return "--/--"

    @staticmethod
    def _selected_case_descriptor(runtime: DeviceRuntime | None) -> Dict[str, Any] | None:
        if runtime is None:
            return None
        return case_descriptor(runtime.catalog, runtime.suite, runtime.case_id)

    def _confirm_risk(
        self, runtime: DeviceRuntime, descriptor: Dict[str, Any] | None,
    ) -> bool | None:
        if descriptor is None or descriptor.get("risk") != "R4":
            return False
        reply = QMessageBox.question(
            self,
            "确认 R4 高风险测试",
            f"{runtime.suite} Case {runtime.case_id}\n{descriptor.get('title', '')}\n\n"
            "该测试可能修改媒体、存储、设置或重启设备，确认继续？",
            QMessageBox.Yes | QMessageBox.No,
        )
        return True if reply == QMessageBox.Yes else None

    def _case_short_title(self, runtime: DeviceRuntime) -> str:
        descriptor = self._selected_case_descriptor(runtime)
        if descriptor is None:
            return f"{runtime.suite}:{runtime.case_id}"
        prefix = {
            "stable_test": "Stable",
            "bug_test": "Bug",
            "stress_test": "压力",
            "custom_test": "自定义",
        }.get(runtime.suite, runtime.suite)
        return f"{prefix} {runtime.case_id}"

    def _row_color(self, runtime: DeviceRuntime) -> QColor | None:
        if runtime.online is False:
            return QColor("#fef2f2")    # red
        if runtime.online is True:
            return QColor("#dcfce7")    # green
        if runtime.status in STATUS_COLORS:
            return QColor(STATUS_COLORS[runtime.status][1])
        return None

    def _update_summary(self) -> None:
        runtimes = list(self.runtimes.values())
        online = sum(runtime.online is True for runtime in runtimes)
        running = sum(runtime.status in ("queued", "running", "stopping") for runtime in runtimes)
        errors = sum(runtime.status in ("failed", "error") for runtime in runtimes)
        self.summary_label.setText(f"设备 {len(runtimes)}  |  在线 {online}  |  运行 {running}  |  异常 {errors}")

    def _stop_runtime(self, runtime: DeviceRuntime) -> bool:
        self._request_runtime_stop(runtime)
        watch_worker = runtime.watch_worker
        record_worker = runtime.record_worker
        if watch_worker is not None and not watch_worker.join(5000 / 1000):
            return False
        if record_worker is not None and not record_worker.join(5000 / 1000):
            return False
        try:
            command_thread = runtime.command_thread
            if command_thread is not None and not command_thread.wait(5000):
                return False
        except RuntimeError:
            runtime.command_thread = None
        self._release_runtime(runtime)
        return True

    @staticmethod
    def _request_runtime_stop(runtime: DeviceRuntime) -> None:
        watch_worker = runtime.watch_worker
        record_worker = runtime.record_worker
        if watch_worker is not None:
            watch_worker.stop()
        if record_worker is not None:
            record_worker.stop()
        command_worker = runtime.command_worker
        if isinstance(command_worker, CommandWorker):
            command_worker.cancel()
        try:
            command_thread = runtime.command_thread
            if command_thread is not None:
                command_thread.quit()
        except RuntimeError:
            runtime.command_thread = None

    @staticmethod
    def _runtime_stopped(runtime: DeviceRuntime) -> bool:
        watch_worker = runtime.watch_worker
        record_worker = runtime.record_worker
        if watch_worker is not None and not watch_worker.join(0):
            return False
        if record_worker is not None and not record_worker.join(0):
            return False
        try:
            command_thread = runtime.command_thread
            if command_thread is not None and not command_thread.wait(0):
                return False
        except RuntimeError:
            runtime.command_thread = None
            return True
        return True

    @staticmethod
    def _release_runtime(runtime: DeviceRuntime) -> None:
        runtime.watch_worker = None
        runtime.record_worker = None
        runtime.command_thread = None
        runtime.command_worker = None

    def _retire_runtime(self, runtime: DeviceRuntime) -> None:
        self._request_runtime_stop(runtime)
        if self._runtime_stopped(runtime):
            self._release_runtime(runtime)
            return
        self._retired_runtimes.append(runtime)
        _APP_LOG.info("runtime_retired key=%s awaiting_worker_shutdown", runtime.key)

    def _reap_retired_runtimes(self) -> None:
        if not self._retired_runtimes:
            return
        pending: list[DeviceRuntime] = []
        for runtime in self._retired_runtimes:
            self._request_runtime_stop(runtime)
            if self._runtime_stopped(runtime):
                self._release_runtime(runtime)
                _APP_LOG.info("runtime_retired_reaped key=%s", runtime.key)
            else:
                pending.append(runtime)
        self._retired_runtimes = pending

    def _device_test_page_active(self) -> bool:
        return self.stack.currentIndex() == 0

    def _on_nav_changed(self, index: int) -> None:
        self.stack.setCurrentIndex(index)
        if index != 0:
            self._probe_timer.stop()
            return

        self._probe_timer.start(60000)
        self.refresh_devices()

    def _active_background_tasks(self) -> list[str]:
        tasks: list[str] = []
        if self.app_test_page.is_busy():
            tasks.append("App 测试")
        if self.otg_page.is_running():
            tasks.append("OTG 传输")
        if self._screenshot_worker is not None and self._screenshot_worker.is_running():
            tasks.append("屏幕截图")
        if self._crash_export_worker is not None and self._crash_export_worker.is_running():
            tasks.append("崩溃日志导出")
        if self._ota_worker is not None and self._ota_worker.is_running():
            tasks.append("OTA 升级")
        if self._ota_post_worker is not None and self._ota_post_worker.is_running():
            tasks.append("OTA 重连")
        if self._batch_ota_worker is not None and self._batch_ota_worker.is_running():
            tasks.append("一键 OTA")
        if self._sd_clean_worker is not None and self._sd_clean_worker.is_running():
            tasks.append("SD卡清理")
        if self._auto_config_thread is not None and self._auto_config_thread.isRunning():
            tasks.append("自动网络配置")
        return tasks

    def closeEvent(self, event) -> None:  # noqa: N802 - Qt API
        self._closing = True
        self._preview_active = False
        self._worker_poll_timer.stop()
        active_tasks = self._active_background_tasks()
        if active_tasks:
            reply = QMessageBox.question(
                self, "退出",
                f"后台任务仍在执行：{', '.join(active_tasks)}。\n\n强制退出？",
                QMessageBox.Yes | QMessageBox.No,
                QMessageBox.No,
            )
            if reply != QMessageBox.Yes:
                self._closing = False
                self._worker_poll_timer.start(50)
                event.ignore()
                return
        self._worker_poll_timer.start(50)
        if self.network_dialog is not None:
            if not self.network_dialog.shutdown():
                _APP_LOG.warning("close_blocked network_dialog_shared_thread_active")
                self._closing = False
                self._worker_poll_timer.start(50)
                event.ignore()
                QMessageBox.warning(self, "退出未完成", "网络配置线程仍在运行，请稍后再次关闭窗口。")
                return
            self.network_dialog.close()
        auto_config_thread = self._auto_config_thread
        if auto_config_thread is not None and auto_config_thread.isRunning():
            auto_config_thread.quit()
            if not auto_config_thread.wait(5000):
                _APP_LOG.warning("close_blocked auto_config_thread_active")
                self._closing = False
                self._worker_poll_timer.start(50)
                event.ignore()
                QMessageBox.warning(self, "退出未完成", "自动网络配置仍在进行，请稍后再次关闭窗口。")
                return
            self._auto_config_worker = None
            self._auto_config_thread = None
        pending_runtimes: list[DeviceRuntime] = []
        for runtime in list(self.runtimes.values()) + list(self._retired_runtimes):
            if not self._stop_runtime(runtime):
                pending_runtimes.append(runtime)
        if pending_runtimes:
            _APP_LOG.error(
                "close_pending_runtimes keys=%s",
                [runtime.key for runtime in pending_runtimes],
            )
            self._retired_runtimes = pending_runtimes
            event.ignore()
            self._closing = False
            self._worker_poll_timer.start(50)
            QMessageBox.warning(
                self,
                "退出未完成",
                "仍有设备命令线程未结束，已保留运行对象。请稍后再次关闭窗口。",
            )
            return
        self.app_test_page.shutdown()
        self.otg_page.shutdown()
        super().closeEvent(event)


def main() -> int:
    import multiprocessing

    multiprocessing.freeze_support()
    enable_crash_diagnostics()
    app = QApplication(sys.argv)
    icon_path = resource_path("source", "title_photo.svg")
    if os.path.exists(icon_path):
        app.setWindowIcon(QIcon(icon_path))
    window = MainWindow()
    window.show()
    return app.exec()


if __name__ == "__main__":
    raise SystemExit(main())
