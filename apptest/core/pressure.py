from __future__ import annotations

import hashlib
import time
from concurrent.futures import FIRST_COMPLETED, Future, ThreadPoolExecutor, wait
from contextvars import copy_context
from pathlib import Path
from typing import Any

from apptest.core.execution import CaseCancelled, emit_case_event, is_cancelled, raise_if_cancelled
from apptest.core.events import DOWNLOAD_PROGRESS, ITERATION_PROGRESS, PRESSURE_FINISHED, PRESSURE_STARTED
from apptest.core.integrity import MISMATCH, UNVERIFIED


def run_download_pressure(
    *,
    scenario: str,
    step_prefix: str,
    download_url: str,
    iterations: int,
    workers: int,
    expected_md5: str,
    expected_sha256: str = "",
    expected_file_size: int | None = None,
    output: Path,
    cloud_client: Any,
    metrics: Any,
    logger: Any,
    stop_on_first_failure: bool,
) -> None:
    total = max(1, int(iterations))
    worker_count = max(1, min(int(workers), total))
    expected = expected_md5.strip().lower()
    expected_sha = expected_sha256.strip().lower()
    if expected and expected_sha:
        raise ValueError("expected_md5 and expected_sha256 are mutually exclusive")
    if expected_file_size is not None and int(expected_file_size) <= 0:
        raise ValueError("expected_file_size must be positive")
    completed = 0
    next_iteration = 1
    failures: list[str] = []
    last_successful_content: bytes | None = None

    logger.info("%s pressure start iterations=%s workers=%s", scenario, total, worker_count)
    emit_case_event(
        PRESSURE_STARTED,
        {"case": scenario, "iterations": total, "workers": worker_count, "url": download_url},
    )

    def download_one(iteration: int) -> bytes:
        raise_if_cancelled()
        progress_state = {"last_percent": -1}

        def on_progress(downloaded: int, content_length: int | None) -> None:
            raise_if_cancelled()
            if content_length and content_length > 0:
                percent = min(int(downloaded * 100 / content_length), 100)
                if percent != 100 and percent - progress_state["last_percent"] < 10:
                    return
                progress_state["last_percent"] = percent
            else:
                percent = None
            emit_case_event(
                DOWNLOAD_PROGRESS,
                {
                    "case": scenario,
                    "iteration": iteration,
                    "total": total,
                    "downloaded_bytes": downloaded,
                    "content_length": content_length,
                    "percent": percent,
                },
            )

        started = time.perf_counter()
        response = None
        integrity_status = UNVERIFIED
        ok = False
        error = ""
        try:
            response = cloud_client.download_file(download_url, progress_callback=on_progress)
            content = response.content
            ok = response.is_success
            error = "" if ok else response.text[:200]
            response.raise_for_status()
            if not content:
                raise AssertionError("downloaded file is empty")
            if expected_file_size is not None and len(content) != int(expected_file_size):
                integrity_status = MISMATCH
                raise AssertionError(
                    f"file size mismatch: expected={int(expected_file_size)}, actual={len(content)}"
                )
            if expected:
                actual_md5 = hashlib.md5(content).hexdigest()
                if actual_md5.lower() != expected:
                    integrity_status = MISMATCH
                    raise AssertionError(f"MD5 mismatch: expected={expected}, actual={actual_md5}")
                integrity_status = "VERIFIED_MD5"
            elif expected_sha:
                actual_sha256 = hashlib.sha256(content).hexdigest()
                if actual_sha256.lower() != expected_sha:
                    integrity_status = MISMATCH
                    raise AssertionError(f"SHA-256 mismatch: expected={expected_sha}, actual={actual_sha256}")
                integrity_status = "VERIFIED_SHA256"
            return content
        except Exception as exc:  # noqa: BLE001
            ok = False
            error = str(exc)
            raise
        finally:
            elapsed_ms = (time.perf_counter() - started) * 1000
            metrics.record_metric(
                scenario,
                f"{step_prefix}_{iteration}",
                "GET",
                download_url,
                response.status_code if response is not None else 0,
                elapsed_ms,
                bytes_received=len(response.content) if response is not None else 0,
                ok=ok if response is not None else False,
                error=error,
                integrity=integrity_status,
            )

    with ThreadPoolExecutor(max_workers=worker_count, thread_name_prefix=f"{scenario}-download") as executor:
        pending: dict[Future[bytes], int] = {}

        def submit_one(iteration: int) -> None:
            context = copy_context()
            pending[executor.submit(context.run, download_one, iteration)] = iteration

        while next_iteration <= total and len(pending) < worker_count:
            submit_one(next_iteration)
            next_iteration += 1

        while pending:
            done, _ = wait(pending, return_when=FIRST_COMPLETED)
            for future in done:
                iteration = pending.pop(future)
                iteration_ok = False
                try:
                    last_successful_content = future.result(timeout=300)
                    iteration_ok = True
                    logger.info("%s iteration=%s/%s completed", scenario, iteration, total)
                except CaseCancelled:
                    pass
                except Exception as exc:  # noqa: BLE001
                    message = f"iteration={iteration}: {exc}"
                    failures.append(message)
                    metrics.record_failure(scenario, f"{step_prefix}_{iteration}", {"error": str(exc)})
                    logger.exception("%s download failed iteration=%s/%s", scenario, iteration, total)
                completed += 1
                emit_case_event(
                    ITERATION_PROGRESS,
                    {
                        "case": scenario,
                        "iteration": iteration,
                        "completed": completed,
                        "total": total,
                        "ok": iteration_ok,
                    },
                )

            should_stop = is_cancelled() or (stop_on_first_failure and bool(failures))
            if should_stop:
                for future in pending:
                    future.cancel()
                break

            while next_iteration <= total and len(pending) < worker_count:
                submit_one(next_iteration)
                next_iteration += 1

    raise_if_cancelled()
    if failures:
        raise AssertionError(f"{len(failures)} download iteration(s) failed; first error: {failures[0]}")
    if last_successful_content is None:
        raise AssertionError("No successful download result was produced")
    output.parent.mkdir(parents=True, exist_ok=True)
    output.write_bytes(last_successful_content)
    emit_case_event(
        PRESSURE_FINISHED,
        {"case": scenario, "completed": completed, "total": total, "workers": worker_count},
    )
