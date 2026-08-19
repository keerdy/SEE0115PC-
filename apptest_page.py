#!/usr/bin/env python3
"""App 测试页：在 PC 侧直接执行 apptest 后端用例并展示实时进度与日志。

后端采用“拷贝并重命名”的 apptest 包，运行/停止走 QThread + QObject worker，
进度回调与日志经 Qt Signal 回到 GUI 线程，OTG 监控作为可折叠区块挂在此页。
"""

from __future__ import annotations

import dataclasses
import logging
import multiprocessing
import os
import queue
import subprocess
import threading
import time
from pathlib import Path

from PySide6.QtCore import QObject, QThread, QTimer, Qt, QUrl, Signal, Slot
from PySide6.QtGui import QDesktopServices
from PySide6.QtWidgets import (
    QComboBox,
    QFileDialog,
    QGridLayout,
    QGroupBox,
    QHBoxLayout,
    QLabel,
    QLineEdit,
    QMessageBox,
    QPlainTextEdit,
    QProgressBar,
    QPushButton,
    QSpinBox,
    QTableWidget,
    QTableWidgetItem,
    QVBoxLayout,
    QWidget,
)

from apptest.core.config import validate_config
from apptest.core.config import apply_device_overrides, load_config, validate_config_for_requested_case
from apptest.clients.p2p_client import P2PClient
from apptest.core.logging_utils import LOG_FORMAT, DATE_FORMAT
from apptest.services import CaseRunRequest, OtgMonitorConfig, OtgMonitorService, list_cases, run_case
from testagent.app_paths import resource_path, runtime_data_dir
from testagent.device import auto_discover_devices
from testagent.protocol import TestAgentClient

STATUS_KINDS = {
    "idle": ("#475569", "#f1f5f9"),
    "running": ("#0369a1", "#e0f2fe"),
    "passed": ("#166534", "#dcfce7"),
    "failed": ("#dc2626", "#fef2f2"),
    "cancelled": ("#c2410c", "#fff7ed"),
}

MONKEY_GROUP_LABELS = {
    "首页": "首页",
    "相册": "相册",
    "设置": "设置",
    "新手教程": "新手教程",
    "激活连接": "激活连接",
}
MONKEY_ACTION_LABELS = {
    "click": "随机点击",
    "swipe": "随机滑动",
    "back": "返回键",
    "connect": "激活/连接设备",
}
MONKEY_DEFAULT_PLAN = [
    {"group": "首页", "action": "click", "percent": 30},
    {"group": "首页", "action": "swipe", "percent": 5},
    {"group": "相册", "action": "click", "percent": 20},
    {"group": "相册", "action": "swipe", "percent": 5},
    {"group": "设置", "action": "click", "percent": 20},
    {"group": "设置", "action": "swipe", "percent": 5},
    {"group": "新手教程", "action": "click", "percent": 5},
    {"group": "激活连接", "action": "connect", "percent": 10},
]


def _button_style(color: str) -> str:
    return (
        f"QPushButton {{ background:{color}; color:white; border:none; border-radius:5px; "
        "padding:6px 14px; font-size:12px; font-weight:600; }"
        f"QPushButton:hover {{ background:{color}dd; }}"
        "QPushButton:disabled { background:#94a3b8; }"
    )


def _make_button(text: str, color: str) -> QPushButton:
    button = QPushButton(text)
    button.setStyleSheet(_button_style(color))
    return button


def _make_label(text: str, width: int = 76) -> QLabel:
    label = QLabel(text)
    label.setFixedWidth(width)
    label.setStyleSheet("font-weight:600; color:#475569;")
    return label


def _status_style(kind: str) -> str:
    fg, bg = STATUS_KINDS.get(kind, STATUS_KINDS["idle"])
    return f"background:{bg}; color:{fg}; font-weight:700; font-size:13px; border-radius:10px; padding:3px 12px;"


class _LogEmitter(QObject):
    """Bridges logging thread output into the GUI thread via a queued signal."""

    new_message = Signal(str)


class QtLogHandler(logging.Handler):
    """Attach to the apptest logger; survives setup_logging (not *_owned*)."""

    def __init__(self, emitter: _LogEmitter) -> None:
        super().__init__(level=logging.INFO)
        self._emitter = emitter
        self.setFormatter(logging.Formatter(LOG_FORMAT, DATE_FORMAT))

    def emit(self, record: logging.LogRecord) -> None:
        try:
            message = self.format(record)
        except Exception:  # noqa: BLE001
            message = record.getMessage()
        self._emitter.new_message.emit(message)


class CaseWorker(QObject):
    """Runs one apptest case inside a dedicated QThread."""

    progress = Signal(str, object)
    result = Signal(object)
    error = Signal(str)
    finished = Signal()

    def __init__(self, request: CaseRunRequest, cancel_event: threading.Event) -> None:
        super().__init__()
        self._request = request
        self._cancel_event = cancel_event

    def cancel(self) -> None:
        self._cancel_event.set()

    @Slot()
    def run(self) -> None:
        try:
            request = dataclasses.replace(
                self._request,
                progress_callback=self._on_progress,
                cancellation_token=self._cancel_event,
            )
            result = run_case(request)
            self.result.emit(result)
        except Exception as exc:  # noqa: BLE001
            self.error.emit(str(exc))
        finally:
            self.finished.emit()

    def _on_progress(self, event_name: str, payload: dict) -> None:
        self.progress.emit(event_name, payload)


class CaseController(QObject):
    """Owns the QThread/worker lifecycle for a single case run."""

    progress = Signal(str, object)
    result = Signal(object)
    error = Signal(str)
    finished = Signal()

    def __init__(self, parent: QObject | None = None) -> None:
        super().__init__(parent)
        self._thread: QThread | None = None
        self._worker: CaseWorker | None = None
        self._cancel_event = threading.Event()

    @property
    def running(self) -> bool:
        return self._thread is not None and self._thread.isRunning()

    def start(self, request: CaseRunRequest) -> bool:
        if self.running:
            return False
        self._cancel_event = threading.Event()
        thread = QThread(self)
        worker = CaseWorker(request, self._cancel_event)
        self._thread = thread
        self._worker = worker
        worker.moveToThread(thread)
        thread.started.connect(worker.run)
        worker.progress.connect(self.progress)
        worker.result.connect(self.result)
        worker.error.connect(self.error)
        worker.finished.connect(thread.quit)
        worker.finished.connect(worker.deleteLater)
        thread.finished.connect(thread.deleteLater)
        thread.finished.connect(self._on_thread_finished)
        thread.start()
        return True

    def stop(self) -> None:
        if self._worker is not None:
            self._worker.cancel()
        if self._thread is not None:
            self._thread.quit()

    def _on_thread_finished(self) -> None:
        self._thread = None
        self._worker = None
        self.finished.emit()


def _run_cloud_case_process(
    case_name: str,
    config: str,
    base_dir: str,
    iterations: int,
    workers: int,
    device_overrides: dict,
    cancel_event,
    output_queue,
) -> None:
    last_progress_at = 0.0

    def progress(event_name: str, payload: dict) -> None:
        nonlocal last_progress_at
        if event_name != "iteration_progress":
            return
        completed = int(payload.get("completed") or 0)
        total = int(payload.get("total") or 0)
        now = time.monotonic()
        if completed < total and now - last_progress_at < 0.25:
            return
        last_progress_at = now
        output_queue.put(("progress", case_name, event_name, payload))

    result = run_case(CaseRunRequest(
        config=config,
        case_name=case_name,
        base_dir=base_dir,
        iterations=iterations,
        workers=workers,
        device_overrides=device_overrides,
        cancellation_token=cancel_event,
        progress_callback=progress,
    ))
    output_queue.put(("result", case_name, result))


class ConcurrentCloudWorker(QObject):
    progress = Signal(str, object)
    result = Signal(object)
    error = Signal(str)
    finished = Signal()

    def __init__(self, requests: list[CaseRunRequest]) -> None:
        super().__init__()
        self._requests = requests
        self._cancel_event = None
        self._processes = []

    def cancel(self) -> None:
        if self._cancel_event is not None:
            self._cancel_event.set()

    @Slot()
    def run(self) -> None:
        results: dict[str, dict] = {}
        output_queue = None
        try:
            context = multiprocessing.get_context("spawn")
            self._cancel_event = context.Event()
            output_queue = context.Queue()
            for request in self._requests:
                process = context.Process(
                    target=_run_cloud_case_process,
                    args=(
                        request.case_name,
                        str(request.config),
                        str(request.base_dir or ""),
                        request.iterations,
                        request.workers,
                        dict(request.device_overrides or {}),
                        self._cancel_event,
                        output_queue,
                    ),
                    name=f"PocketApp-{request.case_name}",
                )
                process.start()
                self._processes.append(process)

            cancel_deadline = 0.0
            result_deadline = 0.0
            while True:
                alive = any(process.is_alive() for process in self._processes)
                if not alive and not result_deadline:
                    result_deadline = time.monotonic() + 2.0
                try:
                    message = output_queue.get(timeout=0.1)
                except queue.Empty:
                    message = None
                if message is not None:
                    kind, case_name, *payload = message
                    if kind == "progress":
                        self.progress.emit(case_name, {"event": payload[0], "payload": payload[1]})
                    else:
                        results[case_name] = payload[0]
                for _ in range(63):
                    try:
                        kind, case_name, *payload = output_queue.get_nowait()
                    except queue.Empty:
                        break
                    if kind == "progress":
                        self.progress.emit(case_name, {"event": payload[0], "payload": payload[1]})
                    else:
                        results[case_name] = payload[0]
                if not alive and (len(results) == len(self._requests) or time.monotonic() >= result_deadline):
                    break
                if self._cancel_event.is_set():
                    if not cancel_deadline:
                        cancel_deadline = time.monotonic() + 15.0
                    elif time.monotonic() >= cancel_deadline:
                        for process in self._processes:
                            if process.is_alive():
                                process.terminate()
                        break
            for process in self._processes:
                process.join(timeout=2.0)
            for request in self._requests:
                if request.case_name not in results:
                    results[request.case_name] = {
                        "case": request.case_name,
                        "status": "cancelled" if self._cancel_event.is_set() else "failed",
                        "error": "子进程未返回结果",
                    }
            self.result.emit(results)
        except Exception as exc:  # noqa: BLE001
            self.error.emit(str(exc))
        finally:
            if output_queue is not None:
                output_queue.close()
                output_queue.join_thread()
            self._processes = []
            self.finished.emit()


class ConcurrentCloudController(QObject):
    progress = Signal(str, object)
    result = Signal(object)
    error = Signal(str)
    finished = Signal()

    def __init__(self, parent: QObject | None = None) -> None:
        super().__init__(parent)
        self._thread: QThread | None = None
        self._worker: ConcurrentCloudWorker | None = None

    @property
    def running(self) -> bool:
        return self._thread is not None and self._thread.isRunning()

    def start(self, requests: list[CaseRunRequest]) -> bool:
        if self.running:
            return False
        self._thread = QThread(self)
        self._worker = ConcurrentCloudWorker(requests)
        self._worker.moveToThread(self._thread)
        self._thread.started.connect(self._worker.run)
        self._worker.progress.connect(self.progress)
        self._worker.result.connect(self.result)
        self._worker.error.connect(self.error)
        self._worker.finished.connect(self._thread.quit)
        self._worker.finished.connect(self._worker.deleteLater)
        self._thread.finished.connect(self._thread.deleteLater)
        self._thread.finished.connect(self._on_finished)
        self._thread.start()
        return True

    def stop(self) -> None:
        if self._worker is not None:
            self._worker.cancel()
        if self._thread is not None:
            self._thread.quit()

    def _on_finished(self) -> None:
        self._thread = None
        self._worker = None
        self.finished.emit()


class AppDeviceDiscoveryWorker(QObject):
    result = Signal(object)
    error = Signal(str)
    finished = Signal()

    @Slot()
    def run(self) -> None:
        try:
            self.result.emit(auto_discover_devices())
        except Exception as exc:  # noqa: BLE001
            self.error.emit(str(exc))
        finally:
            self.finished.emit()


class AdbDiscoveryWorker(QObject):
    result = Signal(object)
    error = Signal(str)
    finished = Signal()

    @Slot()
    def run(self) -> None:
        try:
            completed = subprocess.run(["adb", "devices", "-l"], capture_output=True, text=True, timeout=10)
            if completed.returncode != 0:
                raise RuntimeError((completed.stderr or completed.stdout or "adb devices failed").strip())
            devices = []
            for line in completed.stdout.splitlines()[1:]:
                fields = line.split()
                if len(fields) >= 2 and fields[1] == "device":
                    serial = fields[0]
                    model = next((item.split(":", 1)[1] for item in fields[2:] if item.startswith("model:")), "")
                    devices.append((serial, model))
            self.result.emit(devices)
        except Exception as exc:  # noqa: BLE001
            self.error.emit(str(exc))
        finally:
            self.finished.emit()


class RuntimeSerialWorker(QObject):
    result = Signal(object)
    error = Signal(str)
    finished = Signal()

    def __init__(self, host: str, serial_number: str, test_token: str = "") -> None:
        super().__init__()
        self._host = host
        self._serial_number = serial_number
        self._test_token = test_token

    @Slot()
    def run(self) -> None:
        client: TestAgentClient | None = None
        try:
            client = TestAgentClient(host=self._host, port=19099, timeout=8, token=self._test_token)
            with client:
                payload = client.set_runtime_serial(self._serial_number)
                verify = client.get_runtime_serial()
            if verify.get("serial_number") != self._serial_number:
                raise RuntimeError("设备读回的运行态 SN 与请求值不一致")
            self.result.emit(payload)
        except Exception as exc:  # noqa: BLE001
            self.error.emit(str(exc))
        finally:
            if client is not None:
                client.close()
            self.finished.emit()


class OtgSection(QWidget):
    """OTG transfer monitor driven by apptest OtgMonitorService."""

    status_refreshed = Signal(object)

    def __init__(self, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self._emitter = _LogEmitter(self)
        self._emitter.new_message.connect(self._append_log)
        self._service: OtgMonitorService | None = None
        self._last_record = ""

        outer = QVBoxLayout()
        outer.setSpacing(10)

        top_bar = QHBoxLayout()
        title = QLabel("OTG 文件传输监控")
        title.setStyleSheet("font-size:15px; font-weight:800; color:#0f172a;")
        top_bar.addWidget(title)
        top_bar.addStretch(1)
        self._status_badge = QLabel("已停止")
        self._status_badge.setStyleSheet(_status_style("idle"))
        top_bar.addWidget(self._status_badge)
        outer.addLayout(top_bar)

        config_group = QGroupBox("监控设置")
        config_layout = QGridLayout()
        config_layout.addWidget(_make_label("源目录"), 0, 0)
        self._source_edit = QLineEdit()
        self._source_edit.setPlaceholderText("选择包含待传输文件的源目录")
        browse_btn = _make_button("浏览…", "#64748b")
        browse_btn.clicked.connect(self._browse_source)
        config_layout.addWidget(self._source_edit, 0, 1, 1, 3)
        config_layout.addWidget(browse_btn, 0, 4)
        config_layout.addWidget(_make_label("盘符"), 1, 0)
        self._drive_combo = QComboBox()
        for letter in ("E", "D", "F", "G", "H", "I"):
            self._drive_combo.addItem(letter)
        config_layout.addWidget(self._drive_combo, 1, 1)
        config_layout.addWidget(QLabel("监控运行后，插入对应盘符的可移动存储将自动传输随机源文件"), 1, 2, 1, 3)
        config_group.setLayout(config_layout)
        outer.addWidget(config_group)

        ctrl_row = QHBoxLayout()
        self._start_btn = _make_button("启动监控", "#16a34a")
        self._start_btn.clicked.connect(self._toggle_service)
        ctrl_row.addWidget(self._start_btn)
        self._count_label = QLabel("已传输 0 个文件")
        self._count_label.setStyleSheet("font-weight:700; color:#334155;")
        ctrl_row.addWidget(self._count_label)
        ctrl_row.addStretch(1)
        outer.addLayout(ctrl_row)

        log_group = QGroupBox("传输记录")
        log_layout = QVBoxLayout()
        self._otg_log = QPlainTextEdit()
        self._otg_log.setReadOnly(True)
        self._otg_log.setMaximumBlockCount(500)
        log_layout.addWidget(self._otg_log)
        log_group.setLayout(log_layout)
        outer.addWidget(log_group, 1)

        self.setLayout(outer)

        self._refresh_timer = QTimer(self)
        self._refresh_timer.timeout.connect(self._refresh_status)
        self._refresh_timer.start(1000)

    def is_running(self) -> bool:
        return self._service is not None and self._service.running

    def shutdown(self) -> None:
        if self._service is not None:
            self._service.stop(wait=True, timeout=2.0)
            self._service = None

    def _browse_source(self) -> None:
        directory = QFileDialog.getExistingDirectory(self, "选择 OTG 源目录")
        if directory:
            self._source_edit.setText(directory)

    def _toggle_service(self) -> None:
        if self.is_running():
            self._service.stop(wait=True, timeout=3.0)
            self._service = None
            self._start_btn.setText("启动监控")
            self._start_btn.setStyleSheet(_button_style("#16a34a"))
            self._set_status("idle")
            self._append_log("OTG 监控已停止")
            return
        source = self._source_edit.text().strip()
        if not source:
            QMessageBox.warning(self, "OTG 监控", "请先选择源目录。")
            return
        config = OtgMonitorConfig(
            source_dir=source,
            drive_letter=self._drive_combo.currentText(),
            report_dir=Path(runtime_data_dir()) / "otg_transfer",
        )
        service = OtgMonitorService(config, callback=self._on_otg_event)
        try:
            service.start()
        except Exception as exc:  # noqa: BLE001
            QMessageBox.warning(self, "OTG 监控", str(exc))
            return
        self._service = service
        self._start_btn.setText("停止监控")
        self._start_btn.setStyleSheet(_button_style("#dc2626"))
        self._set_status("running")

    def _on_otg_event(self, event_name: str, payload: dict) -> None:
        self._emitter.new_message.emit(f"[OTG] {event_name}: {payload}")

    def _set_status(self, kind: str) -> None:
        label, style = {
            "idle": ("已停止", "idle"),
            "running": ("监控中", "running"),
        }[kind]
        self._status_badge.setText(label)
        self._status_badge.setStyleSheet(_status_style(style))

    def _refresh_status(self) -> None:
        if self._service is None:
            return
        status = self._service.get_status()
        self._count_label.setText(f"已传输 {status.get('transfer_count', 0)} 个文件")
        record = status.get("latest_record")
        if record and record.get("target") != self._last_record:
            self._last_record = record["target"]
            self._append_log(
                f"[OTG] 完成传输 → {record.get('target')} "
                f"({record.get('size_bytes')} 字节, {record.get('speed_bps')} B/s)"
            )
        self.status_refreshed.emit(status)

    def _append_log(self, message: str) -> None:
        self._otg_log.appendPlainText(message)
        if self._otg_log.blockCount() > 500:
            self._otg_log.clear()


class AppTestPage(QWidget):
    """Runs the six apptest cases on the PC and streams progress/logs to the UI."""

    def __init__(self, parent: QWidget | None = None, main_window: QObject | None = None) -> None:
        super().__init__(parent)
        self._main_window = main_window
        self._cases = list_cases()
        self._controller = CaseController(self)
        self._controller.progress.connect(self._on_progress)
        self._controller.result.connect(self._on_result)
        self._controller.error.connect(self._on_error)
        self._controller.finished.connect(self._on_finished)
        self._cloud_controller = ConcurrentCloudController(self)
        self._cloud_controller.progress.connect(self._on_cloud_progress)
        self._cloud_controller.result.connect(self._on_cloud_result)
        self._cloud_controller.error.connect(self._on_error)
        self._cloud_controller.finished.connect(self._on_cloud_finished)
        self._last_report_html = ""
        self._device_discovery_thread: QThread | None = None
        self._device_discovery_worker: AppDeviceDiscoveryWorker | None = None
        self._adb_discovery_thread: QThread | None = None
        self._adb_discovery_worker: AdbDiscoveryWorker | None = None
        self._runtime_serial_thread: QThread | None = None
        self._runtime_serial_worker: RuntimeSerialWorker | None = None

        self._log_emitter = _LogEmitter(self)
        self._log_emitter.new_message.connect(self._append_log)
        self._log_handler = QtLogHandler(self._log_emitter)
        suite_logger = logging.getLogger("pocket_app_automation")
        if not any(isinstance(handler, QtLogHandler) for handler in suite_logger.handlers):
            suite_logger.addHandler(self._log_handler)

        self._build_ui()
        self._load_default_config_path()

    def is_busy(self) -> bool:
        return self._controller.running or self._cloud_controller.running or any(thread is not None for thread in (
            self._device_discovery_thread, self._adb_discovery_thread, self._runtime_serial_thread,
        ))

    def shutdown(self) -> None:
        self._controller.stop()
        self._cloud_controller.stop()
        for thread in (self._controller._thread, self._cloud_controller._thread):
            if thread is not None and thread.isRunning():
                thread.wait(5000)

    # --- UI construction -----------------------------------------------------
    def _build_ui(self) -> None:
        outer = QVBoxLayout()
        outer.setSpacing(10)

        top_bar = QHBoxLayout()
        title = QLabel("App 测试")
        title.setStyleSheet("font-size:15px; font-weight:800; color:#0f172a;")
        top_bar.addWidget(title)
        top_bar.addStretch(1)
        self._status_badge = QLabel("空闲")
        self._status_badge.setStyleSheet(_status_style("idle"))
        top_bar.addWidget(self._status_badge)
        outer.addLayout(top_bar)

        config_group = QGroupBox("运行配置")
        config_layout = QGridLayout()
        config_layout.addWidget(_make_label("target.yaml"), 0, 0)
        self._config_edit = QLineEdit()
        self._config_edit.setPlaceholderText("选择 apptest 目标配置（含设备/云端/迭代参数）")
        browse_btn = _make_button("浏览…", "#64748b")
        browse_btn.clicked.connect(self._browse_config)
        validate_btn = _make_button("配置预检", "#0f766e")
        validate_btn.clicked.connect(self._validate_config)
        config_layout.addWidget(self._config_edit, 0, 1, 1, 2)
        config_layout.addWidget(browse_btn, 0, 3)
        config_layout.addWidget(validate_btn, 0, 4)
        self._base_dir_label = QLabel("")
        config_layout.addWidget(_make_label("输出目录"), 1, 0)
        config_layout.addWidget(self._base_dir_label, 1, 1, 1, 4)
        config_group.setLayout(config_layout)
        outer.addWidget(config_group)

        device_group = QGroupBox("设备与手机")
        device_layout = QGridLayout()
        device_layout.addWidget(_make_label("设备 IP"), 0, 0)
        self._device_ip_combo = QComboBox()
        self._device_ip_combo.setEditable(True)
        device_layout.addWidget(self._device_ip_combo, 0, 1, 1, 2)
        refresh_device_btn = _make_button("刷新设备", "#64748b")
        refresh_device_btn.clicked.connect(self._refresh_app_devices)
        device_layout.addWidget(refresh_device_btn, 0, 3)
        device_layout.addWidget(_make_label("adb 设备"), 1, 0)
        self._adb_combo = QComboBox()
        self._adb_combo.setEditable(True)
        device_layout.addWidget(self._adb_combo, 1, 1, 1, 2)
        refresh_adb_btn = _make_button("刷新 adb", "#64748b")
        refresh_adb_btn.clicked.connect(self._refresh_adb_devices)
        device_layout.addWidget(refresh_adb_btn, 1, 3)
        device_layout.addWidget(_make_label("App 设备名称"), 2, 0)
        self._app_device_name_edit = QLineEdit()
        self._app_device_name_edit.setPlaceholderText("例如 Gimbal Camera-123456")
        device_layout.addWidget(self._app_device_name_edit, 2, 1, 1, 3)
        device_layout.addWidget(_make_label("设备 SN"), 3, 0)
        self._serial_edit = QLineEdit()
        device_layout.addWidget(self._serial_edit, 3, 1)
        device_layout.addWidget(_make_label("激活码"), 3, 2)
        self._activation_code_edit = QLineEdit()
        device_layout.addWidget(self._activation_code_edit, 3, 3)
        runtime_serial_btn = _make_button("写入临时 SN", "#0f766e")
        runtime_serial_btn.clicked.connect(self._write_runtime_serial)
        device_layout.addWidget(runtime_serial_btn, 4, 1)
        device_group.setLayout(device_layout)
        outer.addWidget(device_group)

        run_group = QGroupBox("用例运行")
        run_layout = QGridLayout()
        run_layout.addWidget(_make_label("用例"), 0, 0)
        self._case_combo = QComboBox()
        for case in self._cases:
            self._case_combo.addItem(f"{case['name']} - {case['title']}", case["name"])
        self._case_combo.currentIndexChanged.connect(self._on_case_changed)
        run_layout.addWidget(self._case_combo, 0, 1, 1, 3)
        run_layout.addWidget(_make_label("迭代"), 1, 0)
        self._iter_spin = QSpinBox()
        self._iter_spin.setRange(0, 100000)
        self._iter_spin.setValue(0)
        self._iter_spin.setToolTip("0 表示使用配置中的迭代数")
        run_layout.addWidget(self._iter_spin, 1, 1)
        run_layout.addWidget(_make_label("并发"), 2, 0)
        self._workers_spin = QSpinBox()
        self._workers_spin.setRange(0, 64)
        self._workers_spin.setValue(0)
        self._workers_spin.setToolTip("0 表示使用配置中的并发数")
        run_layout.addWidget(self._workers_spin, 2, 1)

        self._run_btn = _make_button("开始运行", "#16a34a")
        self._run_btn.clicked.connect(self._start_run)
        self._stop_btn = _make_button("停止", "#dc2626")
        self._stop_btn.setEnabled(False)
        self._stop_btn.clicked.connect(self._stop_run)
        self._report_btn = _make_button("打开报告", "#2563eb")
        self._report_btn.setEnabled(False)
        self._report_btn.clicked.connect(self._open_report)
        run_layout.addWidget(self._run_btn, 3, 0)
        run_layout.addWidget(self._stop_btn, 3, 1)
        run_layout.addWidget(self._report_btn, 3, 2)

        self._progress_bar = QProgressBar()
        self._progress_bar.setRange(0, 100)
        self._progress_bar.setValue(0)
        run_layout.addWidget(_make_label("进度"), 4, 0)
        run_layout.addWidget(self._progress_bar, 4, 1, 1, 3)
        run_group.setLayout(run_layout)
        outer.addWidget(run_group)

        cloud_group = QGroupBox("云端并发压测")
        cloud_layout = QGridLayout()
        cloud_layout.addWidget(QLabel("case3 APK 下载"), 0, 0)
        self._case3_iter_spin = QSpinBox()
        self._case3_iter_spin.setRange(1, 100000)
        self._case3_iter_spin.setValue(5000)
        cloud_layout.addWidget(self._case3_iter_spin, 0, 1)
        self._case3_cloud_status = QLabel("未运行")
        cloud_layout.addWidget(self._case3_cloud_status, 0, 2)
        cloud_layout.addWidget(QLabel("case4 固件下载"), 1, 0)
        self._case4_iter_spin = QSpinBox()
        self._case4_iter_spin.setRange(1, 100000)
        self._case4_iter_spin.setValue(5000)
        cloud_layout.addWidget(self._case4_iter_spin, 1, 1)
        self._case4_cloud_status = QLabel("未运行")
        cloud_layout.addWidget(self._case4_cloud_status, 1, 2)
        cloud_layout.addWidget(_make_label("并发"), 2, 0)
        self._cloud_workers_spin = QSpinBox()
        self._cloud_workers_spin.setRange(1, 64)
        self._cloud_workers_spin.setValue(8)
        cloud_layout.addWidget(self._cloud_workers_spin, 2, 1)
        self._cloud_start_btn = _make_button("开始并发", "#16a34a")
        self._cloud_start_btn.clicked.connect(self._start_cloud_runs)
        self._cloud_stop_btn = _make_button("停止", "#dc2626")
        self._cloud_stop_btn.setEnabled(False)
        self._cloud_stop_btn.clicked.connect(self._stop_cloud_runs)
        cloud_layout.addWidget(self._cloud_start_btn, 3, 0)
        cloud_layout.addWidget(self._cloud_stop_btn, 3, 1)
        cloud_group.setLayout(cloud_layout)
        outer.addWidget(cloud_group)

        self._monkey_group = QGroupBox("monkey 随机参数（页面 / 动作 百分比权重）")
        monkey_layout = QVBoxLayout()
        self._monkey_table = QTableWidget()
        self._monkey_table.setColumnCount(3)
        self._monkey_table.setHorizontalHeaderLabels(["页面分组", "动作", "百分比"])
        self._monkey_table.horizontalHeader().setStretchLastSection(True)
        self._populate_monkey_table(MONKEY_DEFAULT_PLAN)
        monkey_layout.addWidget(self._monkey_table)
        self._monkey_hint = QLabel(
            "百分比=权重（不要求合计 100，monkey 会按权重归一化随机选择）。"
            "“激活连接”动作在随机到时按 ble_exact_name 找到设备并连接；"
            "全部迭代结束后必须保持连接，否则用例判定失败。全程抓取 adb logcat。"
        )
        self._monkey_hint.setStyleSheet("color:#64748b; font-size:12px;")
        self._monkey_hint.setWordWrap(True)
        monkey_layout.addWidget(self._monkey_hint)
        self._monkey_group.setLayout(monkey_layout)
        outer.addWidget(self._monkey_group)
        self._monkey_group.setVisible(self._selected_case() == "case7")

        log_group = QGroupBox("实时日志")
        log_layout = QVBoxLayout()
        self._log_view = QPlainTextEdit()
        self._log_view.setReadOnly(True)
        self._log_view.setMaximumBlockCount(2000)
        log_layout.addWidget(self._log_view)
        log_group.setLayout(log_layout)
        outer.addWidget(log_group, 1)

        self.setLayout(outer)

    def _set_status(self, text: str, kind: str) -> None:
        self._status_badge.setText(text)
        self._status_badge.setStyleSheet(_status_style(kind))

    def _load_default_config_path(self) -> None:
        default = resource_path("configs", "app_test_target.yaml")
        if Path(default).exists():
            self._config_edit.setText(default)
        self._base_dir_label.setText(runtime_data_dir())
        self._base_dir_label.setToolTip("报告/下载/日志输出到此目录")

    # --- config helpers ------------------------------------------------------
    def _browse_config(self) -> None:
        path, _ = QFileDialog.getOpenFileName(
            self, "选择 target.yaml", str(Path(runtime_data_dir())), "YAML 配置 (*.yaml *.yml)"
        )
        if path:
            self._config_edit.setText(path)

    def _selected_case(self) -> str:
        return self._case_combo.currentData()

    def _on_case_changed(self) -> None:
        if hasattr(self, "_monkey_group"):
            self._monkey_group.setVisible(self._selected_case() == "case7")

    def _populate_monkey_table(self, plan: list[dict]) -> None:
        self._monkey_table.setRowCount(len(plan))
        for row, item in enumerate(plan):
            group = str(item.get("group", ""))
            action = str(item.get("action", ""))
            percent = int(item.get("percent", 0))
            self._monkey_table.setItem(row, 0, QTableWidgetItem(group))
            self._monkey_table.setItem(row, 1, QTableWidgetItem(action))
            spin = QSpinBox()
            spin.setRange(0, 100)
            spin.setValue(percent)
            self._monkey_table.setCellWidget(row, 2, spin)

    def _collect_monkey_plan(self) -> list[dict]:
        plan = []
        for row in range(self._monkey_table.rowCount()):
            group_item = self._monkey_table.item(row, 0)
            action_item = self._monkey_table.item(row, 1)
            spin = self._monkey_table.cellWidget(row, 2)
            group = group_item.text().strip() if group_item else ""
            action = action_item.text().strip() if action_item else ""
            percent = spin.value() if isinstance(spin, QSpinBox) else 0
            if group and action:
                plan.append({"group": group, "action": action, "percent": percent})
        return plan

    def _validate_monkey_plan(self, plan: list[dict]) -> str:
        if not plan:
            return "monkey 用例至少需要一个页面/动作项。"
        for item in plan:
            if item["action"] == "connect" and item["group"] == "激活连接" and int(item.get("percent", 0)) > 0:
                return ""
        return "monkey 用例建议配置“激活连接”动作权重（>0），否则不满足结束时保持连接的前置条件。"

    def _validate_config(self) -> None:
        config_path = self._config_edit.text().strip()
        if not config_path:
            QMessageBox.warning(self, "配置预检", "请先选择 target.yaml。")
            return
        try:
            app_config = apply_device_overrides(
                load_config(config_path, base_dir=Path(runtime_data_dir())), self._device_overrides()
            )
        except Exception as exc:  # noqa: BLE001
            QMessageBox.warning(self, "配置预检", f"配置加载失败：{exc}")
            return
        problems = validate_config_for_requested_case(app_config, self._backend_case())
        if problems:
            QMessageBox.information(self, "配置预检", "发现以下问题：\n\n" + "\n".join(problems))
        else:
            QMessageBox.information(self, "配置预检", "配置可用，未发现问题。")

    # --- run lifecycle -------------------------------------------------------
    def _start_run(self) -> None:
        if self._controller.running:
            return
        config_path = self._config_edit.text().strip()
        if not config_path or not Path(config_path).exists():
            QMessageBox.warning(self, "运行", "请先选择有效的 target.yaml。")
            return
        request = CaseRunRequest(
            config=config_path,
            case_name=self._selected_case(),
            base_dir=Path(runtime_data_dir()),
            iterations=self._iter_spin.value(),
            workers=self._workers_spin.value(),
            device_overrides=self._device_overrides(),
        )
        if self._selected_case() == "case7":
            plan = self._collect_monkey_plan()
            problem = self._validate_monkey_plan(plan)
            if problem:
                QMessageBox.warning(self, "monkey 参数", problem)
                return
            request = dataclasses.replace(request, options={"monkey_plan": plan})
        if not self._controller.start(request):
            QMessageBox.warning(self, "运行", "已有用例正在运行。")
            return
        self._run_btn.setEnabled(False)
        self._stop_btn.setEnabled(True)
        self._report_btn.setEnabled(False)
        self._progress_bar.setValue(0)
        self._set_status("正在启动…", "running")
        self._append_log(f"开始运行 {self._selected_case()}  config={config_path}")

    def _stop_run(self) -> None:
        self._controller.stop()
        self._set_status("正在停止…", "cancelled")

    def _start_cloud_runs(self) -> None:
        if self._controller.running or self._cloud_controller.running:
            QMessageBox.warning(self, "云端并发", "已有 App 测试正在运行。")
            return
        config_path = self._config_edit.text().strip()
        if not config_path or not Path(config_path).exists():
            QMessageBox.warning(self, "云端并发", "请先选择有效的 target.yaml。")
            return
        overrides = self._device_overrides()
        requests = [
            CaseRunRequest(config=config_path, case_name="case3", base_dir=Path(runtime_data_dir()),
                           iterations=self._case3_iter_spin.value(), workers=self._cloud_workers_spin.value(),
                           device_overrides=overrides),
            CaseRunRequest(config=config_path, case_name="case4", base_dir=Path(runtime_data_dir()),
                           iterations=self._case4_iter_spin.value(), workers=self._cloud_workers_spin.value(),
                           device_overrides=overrides),
        ]
        if not self._cloud_controller.start(requests):
            return
        self._cloud_start_btn.setEnabled(False)
        self._cloud_stop_btn.setEnabled(True)
        self._case3_cloud_status.setText("运行中")
        self._case4_cloud_status.setText("运行中")
        self._append_log("开始并发执行 case3 与 case4（独立子进程）")

    def _stop_cloud_runs(self) -> None:
        self._cloud_controller.stop()
        self._cloud_stop_btn.setEnabled(False)

    def _on_cloud_progress(self, case_name: str, event: dict) -> None:
        payload = event.get("payload", {})
        if event.get("event") == "iteration_progress":
            self._append_log(f"[{case_name}] {payload.get('completed', 0)}/{payload.get('total', 0)}")

    def _on_cloud_result(self, results: dict) -> None:
        for case_name, result in results.items():
            label = self._case3_cloud_status if case_name == "case3" else self._case4_cloud_status
            label.setText(str(result.get("status", "failed")))
            report_html = str(result.get("report_html") or "")
            if report_html and Path(report_html).exists():
                self._last_report_html = report_html
                self._report_btn.setEnabled(True)
            if result.get("error"):
                self._append_log(f"[{case_name}] {result['error']}")

    def _on_cloud_finished(self) -> None:
        self._cloud_start_btn.setEnabled(True)
        self._cloud_stop_btn.setEnabled(False)

    def _on_progress(self, event_name: str, payload: dict) -> None:
        if event_name == "case_started":
            self._set_status(
                f"运行中：{payload.get('case', '')} ({payload.get('backend', '')})", "running"
            )
        elif event_name == "iteration_progress":
            total = int(payload.get("total") or 0)
            completed = int(payload.get("completed") or 0)
            if total > 0:
                self._progress_bar.setValue(int(completed * 100 / total))
                self._set_status(f"运行中：已完成 {completed}/{total} 迭代", "running")
        elif event_name == "download_progress":
            percent = payload.get("percent")
            if isinstance(percent, (int, float)):
                self._progress_bar.setValue(int(percent))

    def _on_result(self, result: dict) -> None:
        status = result.get("status", "failed")
        case = result.get("case", "")
        self._progress_bar.setValue(100 if status == "passed" else self._progress_bar.value())
        self._set_status(f"{case} 运行结束：{status}", status)
        report_html = str(result.get("report_html") or "")
        if report_html and Path(report_html).exists():
            self._last_report_html = report_html
            self._report_btn.setEnabled(True)
            self._append_log(f"[完成] 报告已生成：{report_html}")
        if result.get("error"):
            self._append_log(f"[错误] {result['error']}")

    def _on_error(self, message: str) -> None:
        self._set_status("运行失败", "failed")
        self._append_log(f"[异常] {message}")

    def _on_finished(self) -> None:
        self._run_btn.setEnabled(True)
        self._stop_btn.setEnabled(False)
        if self._status_badge.text() in ("正在启动…", "正在停止…"):
            self._set_status("空闲", "idle")

    def _open_report(self) -> None:
        if self._last_report_html and Path(self._last_report_html).exists():
            QDesktopServices.openUrl(QUrl.fromLocalFile(self._last_report_html))

    def _backend_case(self) -> str:
        return next(case["backend_case"] for case in self._cases if case["name"] == self._selected_case())

    def _device_overrides(self) -> dict:
        device_ip = self._device_ip_combo.currentData() or self._device_ip_combo.currentText().strip()
        adb_serial = self._adb_combo.currentData() or self._adb_combo.currentText().strip()
        return {
            "host": device_ip,
            "android_serial": adb_serial,
            "ble_exact_name": self._app_device_name_edit.text().strip(),
            "device_id": self._serial_edit.text().strip(),
            "activation_code": self._activation_code_edit.text().strip(),
        }

    def _replace_combo_items(self, combo: QComboBox, entries: list[tuple[str, str]]) -> None:
        current = combo.currentData() or combo.currentText().strip()
        combo.blockSignals(True)
        combo.clear()
        for label, value in entries:
            combo.addItem(label, value)
        if current:
            index = combo.findData(current)
            if index >= 0:
                combo.setCurrentIndex(index)
            else:
                combo.setEditText(current)
        combo.blockSignals(False)

    def _refresh_app_devices(self) -> None:
        if self._device_discovery_thread is not None:
            return
        self._device_discovery_thread = QThread(self)
        self._device_discovery_worker = AppDeviceDiscoveryWorker()
        self._device_discovery_worker.moveToThread(self._device_discovery_thread)
        self._device_discovery_thread.started.connect(self._device_discovery_worker.run)
        self._device_discovery_worker.result.connect(self._on_app_devices_discovered)
        self._device_discovery_worker.error.connect(self._on_app_device_discovery_error)
        self._device_discovery_worker.finished.connect(self._device_discovery_thread.quit)
        self._device_discovery_worker.finished.connect(self._device_discovery_worker.deleteLater)
        self._device_discovery_thread.finished.connect(self._device_discovery_thread.deleteLater)
        self._device_discovery_thread.finished.connect(self._clear_app_device_discovery)
        self._device_discovery_thread.start()

    def _on_app_devices_discovered(self, links: list[dict]) -> None:
        entries = [(f"{item.get('iface', '')}: {item.get('device_ip', '')}", str(item.get("device_ip", "")))
                   for item in links if item.get("device_ip")]
        self._replace_combo_items(self._device_ip_combo, entries)
        self._append_log(f"[设备发现] 发现 {len(entries)} 条 NDIS/RNDIS 链路（未配网、未发送 TestAgent TCP）")

    def _clear_app_device_discovery(self) -> None:
        self._device_discovery_thread = None
        self._device_discovery_worker = None

    @Slot(str)
    def _on_app_device_discovery_error(self, message: str) -> None:
        self._append_log(f"[设备发现] {message}")

    def _refresh_adb_devices(self) -> None:
        if self._adb_discovery_thread is not None:
            return
        self._adb_discovery_thread = QThread(self)
        self._adb_discovery_worker = AdbDiscoveryWorker()
        self._adb_discovery_worker.moveToThread(self._adb_discovery_thread)
        self._adb_discovery_thread.started.connect(self._adb_discovery_worker.run)
        self._adb_discovery_worker.result.connect(self._on_adb_devices_discovered)
        self._adb_discovery_worker.error.connect(self._on_adb_discovery_error)
        self._adb_discovery_worker.finished.connect(self._adb_discovery_thread.quit)
        self._adb_discovery_worker.finished.connect(self._adb_discovery_worker.deleteLater)
        self._adb_discovery_thread.finished.connect(self._adb_discovery_thread.deleteLater)
        self._adb_discovery_thread.finished.connect(self._clear_adb_discovery)
        self._adb_discovery_thread.start()

    def _on_adb_devices_discovered(self, devices: list[tuple[str, str]]) -> None:
        self._replace_combo_items(self._adb_combo, [(f"{serial} ({model})" if model else serial, serial) for serial, model in devices])

    def _clear_adb_discovery(self) -> None:
        self._adb_discovery_thread = None
        self._adb_discovery_worker = None

    @Slot(str)
    def _on_adb_discovery_error(self, message: str) -> None:
        self._append_log(f"[adb 发现] {message}")

    def _write_runtime_serial(self) -> None:
        host = str(self._device_ip_combo.currentData() or self._device_ip_combo.currentText()).strip()
        serial_number = self._serial_edit.text().strip()
        test_token = os.environ.get("POCKET_TESTAGENT_TOKEN", "").strip()
        if not host or not serial_number:
            QMessageBox.warning(self, "写入临时 SN", "请先填写设备 IP 和设备 SN。")
            return
        if self._runtime_serial_thread is not None:
            return
        self._runtime_serial_thread = QThread(self)
        self._runtime_serial_worker = RuntimeSerialWorker(host, serial_number, test_token)
        self._runtime_serial_worker.moveToThread(self._runtime_serial_thread)
        self._runtime_serial_thread.started.connect(self._runtime_serial_worker.run)
        self._runtime_serial_worker.result.connect(self._on_runtime_serial_written)
        self._runtime_serial_worker.error.connect(self._on_runtime_serial_error)
        self._runtime_serial_worker.finished.connect(self._runtime_serial_thread.quit)
        self._runtime_serial_worker.finished.connect(self._runtime_serial_worker.deleteLater)
        self._runtime_serial_thread.finished.connect(self._runtime_serial_thread.deleteLater)
        self._runtime_serial_thread.finished.connect(self._clear_runtime_serial_worker)
        self._runtime_serial_thread.start()

    def _on_runtime_serial_written(self, data: dict) -> None:
        actual = str(data.get("serial_number", ""))
        if actual != self._serial_edit.text().strip():
            QMessageBox.warning(self, "写入临时 SN", "设备返回的 SN 与请求值不一致。")
            return
        QMessageBox.information(self, "写入临时 SN", "运行态 SN 已写入，设备重启后会恢复固件原始 SN。")

    def _clear_runtime_serial_worker(self) -> None:
        self._runtime_serial_thread = None
        self._runtime_serial_worker = None

    @Slot(str)
    def _on_runtime_serial_error(self, message: str) -> None:
        QMessageBox.warning(self, "写入临时 SN", message)

    def _append_log(self, message: str) -> None:
        self._log_view.appendPlainText(message)


if __name__ == "__main__":
    import sys

    from PySide6.QtWidgets import QApplication

    app = QApplication(sys.argv)
    page = AppTestPage()
    page.resize(960, 720)
    page.show()
    sys.exit(app.exec())
