from __future__ import annotations

import json
import random
import shutil
import threading
import time
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Any, Callable

from apptest.core.logging_utils import get_logger


OtgEventCallback = Callable[[str, dict[str, Any]], None]


@dataclass(frozen=True)
class OtgMonitorConfig:
    source_dir: str | Path
    drive_letter: str = "E"
    report_dir: str | Path = "artifacts/otg_transfer"
    poll_interval_seconds: float = 1.0


@dataclass(frozen=True)
class OtgTransferRecord:
    count: int
    timestamp: str
    source: str
    target: str
    size_bytes: int
    elapsed_seconds: float
    speed_bps: float
    drive_root: str


class OtgMonitorService:
    """Monitor a removable drive and copy one random source file per insertion."""

    def __init__(self, config: OtgMonitorConfig, callback: OtgEventCallback | None = None) -> None:
        self.config = config
        self.callback = callback
        self.source_dir = Path(config.source_dir).resolve()
        self.report_dir = Path(config.report_dir).resolve()
        self.drive_letter = self._normalize_drive_letter(config.drive_letter)
        self.drive_root = Path(f"{self.drive_letter}:/")
        self.poll_interval_seconds = max(0.1, float(config.poll_interval_seconds))
        self.logger = get_logger("pocket_app_automation.otg")
        self._stop_event = threading.Event()
        self._thread: threading.Thread | None = None
        self._lock = threading.Lock()
        self._state = "stopped"
        self._last_error = ""
        self._latest_record: OtgTransferRecord | None = None
        self._transfer_count = self._load_previous_count()

    @property
    def running(self) -> bool:
        return self._thread is not None and self._thread.is_alive()

    def start(self) -> None:
        if self.running:
            raise RuntimeError("OTG monitor is already running")
        source_files = self._list_source_files()
        if not source_files:
            raise FileNotFoundError(f"No transferable files found in source directory: {self.source_dir}")

        self.report_dir.mkdir(parents=True, exist_ok=True)
        self._stop_event.clear()
        self._set_state("waiting")
        self._thread = threading.Thread(target=self._monitor_loop, name="otg-monitor", daemon=True)
        self._thread.start()
        self._emit(
            "started",
            {
                "source_dir": str(self.source_dir),
                "source_file_count": len(source_files),
                "drive_root": str(self.drive_root),
            },
        )

    def stop(self, wait: bool = True, timeout: float = 30.0) -> None:
        self._stop_event.set()
        thread = self._thread
        if wait and thread is not None and thread is not threading.current_thread():
            thread.join(timeout=max(0.0, timeout))
        if thread is self._thread and (not wait or not thread.is_alive()):
            self._thread = None

    def get_status(self) -> dict[str, Any]:
        with self._lock:
            return {
                "running": self.running,
                "state": self._state,
                "drive_root": str(self.drive_root),
                "source_dir": str(self.source_dir),
                "report_dir": str(self.report_dir),
                "transfer_count": self._transfer_count,
                "last_error": self._last_error,
                "latest_record": asdict(self._latest_record) if self._latest_record else None,
            }

    def _monitor_loop(self) -> None:
        waiting_for_removal = False
        try:
            while not self._stop_event.is_set():
                drive_exists = self.drive_root.exists()
                if not waiting_for_removal:
                    if not drive_exists:
                        self._set_state("waiting")
                        self._stop_event.wait(self.poll_interval_seconds)
                        continue
                    try:
                        record = self._copy_random_file()
                        waiting_for_removal = True
                        self._set_state("waiting_removal")
                        self._emit("transfer_success", asdict(record))
                    except Exception as exc:  # noqa: BLE001
                        waiting_for_removal = True
                        self._last_error = str(exc)
                        self._set_state("transfer_error")
                        self._emit("transfer_error", {"error": str(exc), "drive_root": str(self.drive_root)})
                elif drive_exists:
                    self._set_state("waiting_removal")
                else:
                    waiting_for_removal = False
                    self._set_state("waiting")
                    self._emit("drive_removed", {"drive_root": str(self.drive_root)})
                self._stop_event.wait(self.poll_interval_seconds)
        finally:
            self._set_state("stopped")
            self._thread = None
            self._emit("stopped", self.get_status())

    def _copy_random_file(self) -> OtgTransferRecord:
        source_files = self._list_source_files()
        if not source_files:
            raise FileNotFoundError(f"No transferable files found in source directory: {self.source_dir}")
        if not self.drive_root.exists():
            raise FileNotFoundError(f"Target drive is not available: {self.drive_root}")

        source_file = random.choice(source_files)
        next_count = self._transfer_count + 1
        target_file = self.drive_root / f"文件传输_{next_count}{source_file.suffix}"
        collision_index = 1
        while target_file.exists():
            target_file = self.drive_root / f"文件传输_{next_count}_{collision_index}{source_file.suffix}"
            collision_index += 1

        started_at = time.perf_counter()
        shutil.copy2(source_file, target_file)
        elapsed_seconds = max(time.perf_counter() - started_at, 0.001)
        if not target_file.exists():
            raise OSError(f"Target file was not created: {target_file}")

        size_bytes = target_file.stat().st_size
        record = OtgTransferRecord(
            count=next_count,
            timestamp=time.strftime("%Y-%m-%d %H:%M:%S"),
            source=str(source_file),
            target=str(target_file),
            size_bytes=size_bytes,
            elapsed_seconds=round(elapsed_seconds, 3),
            speed_bps=round(size_bytes / elapsed_seconds, 2),
            drive_root=str(self.drive_root),
        )
        with self._lock:
            self._transfer_count = next_count
            self._latest_record = record
            self._last_error = ""
        self._write_report(record)
        return record

    def _write_report(self, record: OtgTransferRecord) -> None:
        self.report_dir.mkdir(parents=True, exist_ok=True)
        payload = asdict(record)
        with (self.report_dir / "history.jsonl").open("a", encoding="utf-8") as handle:
            handle.write(json.dumps(payload, ensure_ascii=False) + "\n")
        summary = {
            "status": "transfer_completed_waiting_for_removal",
            "transfer_count": record.count,
            "last_transfer": payload,
        }
        (self.report_dir / "latest_summary.json").write_text(
            json.dumps(summary, ensure_ascii=False, indent=2),
            encoding="utf-8",
        )

    def _list_source_files(self) -> list[Path]:
        if not self.source_dir.is_dir():
            return []
        return [path for path in self.source_dir.rglob("*") if path.is_file()]

    def _load_previous_count(self) -> int:
        summary_path = self.report_dir / "latest_summary.json"
        if not summary_path.exists():
            return 0
        try:
            payload = json.loads(summary_path.read_text(encoding="utf-8"))
            return max(0, int(payload.get("transfer_count", 0)))
        except (OSError, ValueError, TypeError, json.JSONDecodeError):
            return 0

    def _set_state(self, state: str) -> None:
        with self._lock:
            changed = self._state != state
            self._state = state
        if changed:
            self._emit("state_changed", {"state": state, "drive_root": str(self.drive_root)})

    def _emit(self, event_name: str, payload: dict[str, Any]) -> None:
        self.logger.info("otg event=%s payload=%s", event_name, json.dumps(payload, ensure_ascii=False))
        if self.callback is None:
            return
        try:
            self.callback(event_name, payload)
        except Exception:  # noqa: BLE001
            self.logger.exception("OTG callback failed event=%s", event_name)

    @staticmethod
    def _normalize_drive_letter(value: str) -> str:
        text = (value or "E").strip().upper().replace(":", "")
        if not text or not text[0].isalpha():
            raise ValueError(f"Invalid drive letter: {value!r}")
        return text[0]
