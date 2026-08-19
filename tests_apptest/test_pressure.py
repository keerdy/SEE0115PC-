from __future__ import annotations

import hashlib
import threading
import time

import pytest

from apptest.core.execution import case_execution_context
from apptest.core.pressure import run_download_pressure
from apptest.core.reporting import MetricsRecorder


class FakeResponse:
    status_code = 200
    is_success = True
    text = ""

    def __init__(self, content: bytes) -> None:
        self.content = content

    def raise_for_status(self) -> None:
        return None


class FakeCloudClient:
    def __init__(self, content: bytes) -> None:
        self.content = content
        self.active = 0
        self.max_active = 0
        self.lock = threading.Lock()

    def download_file(self, url, progress_callback=None):
        with self.lock:
            self.active += 1
            self.max_active = max(self.max_active, self.active)
        try:
            if progress_callback:
                progress_callback(len(self.content), len(self.content))
            time.sleep(0.03)
            return FakeResponse(self.content)
        finally:
            with self.lock:
                self.active -= 1


class NullLogger:
    def info(self, *args, **kwargs) -> None:
        return None

    def exception(self, *args, **kwargs) -> None:
        return None


def test_download_pressure_uses_workers_and_writes_one_final_file(tmp_path) -> None:
    content = b"pocket-package"
    client = FakeCloudClient(content)
    events: list[tuple[str, dict]] = []
    output = tmp_path / "downloads" / "latest.apk"

    with case_execution_context(lambda name, payload: events.append((name, payload))):
        run_download_pressure(
            scenario="case3",
            step_prefix="apk_download",
            download_url="https://example.test/app.apk",
            iterations=6,
            workers=3,
            expected_md5=hashlib.md5(content).hexdigest(),
            expected_file_size=len(content),
            output=output,
            cloud_client=client,
            metrics=MetricsRecorder(tmp_path),
            logger=NullLogger(),
            stop_on_first_failure=False,
        )

    assert client.max_active >= 2
    assert output.read_bytes() == content
    progress_events = [payload for name, payload in events if name == "iteration_progress"]
    assert len(progress_events) == 6
    assert max(item["completed"] for item in progress_events) == 6


def test_download_pressure_records_unverified_when_hash_is_missing(tmp_path) -> None:
    content = b"pocket-package"
    client = FakeCloudClient(content)
    metrics = MetricsRecorder(tmp_path)

    run_download_pressure(
        scenario="case3",
        step_prefix="apk_download",
        download_url="https://example.test/app.apk",
        iterations=1,
        workers=1,
        expected_md5="",
        expected_file_size=len(content),
        output=tmp_path / "downloads" / "latest.apk",
        cloud_client=client,
        metrics=metrics,
        logger=NullLogger(),
        stop_on_first_failure=False,
    )

    rows = (tmp_path / "metrics.csv").read_text(encoding="utf-8").splitlines()
    assert rows[-1].endswith(",UNVERIFIED")


def test_download_pressure_rejects_file_size_mismatch(tmp_path) -> None:
    content = b"pocket-package"
    client = FakeCloudClient(content)

    with pytest.raises(AssertionError, match="file size mismatch"):
        run_download_pressure(
            scenario="case3",
            step_prefix="apk_download",
            download_url="https://example.test/app.apk",
            iterations=1,
            workers=1,
            expected_md5="",
            expected_file_size=len(content) + 1,
            output=tmp_path / "downloads" / "latest.apk",
            cloud_client=client,
            metrics=MetricsRecorder(tmp_path),
            logger=NullLogger(),
            stop_on_first_failure=False,
        )
