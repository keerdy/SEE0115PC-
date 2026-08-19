from __future__ import annotations

import time
from pathlib import Path

from apptest.core.auth_flow import authenticate_device
from apptest.core.download_utils import ensure_parent
from apptest.core.execution import emit_case_event, raise_if_cancelled
from apptest.core.events import ITERATION_PROGRESS
from apptest.core.integrity import IntegrityMetadataError, UNVERIFIED, resolve_integrity_expectation
from apptest.core.logging_utils import get_logger
from apptest.core.pressure import run_download_pressure


def _parse_successful_protected_response(response, endpoint: str) -> dict:
    """Validate the common response envelope for an authenticated P2P call."""
    if getattr(response, "status_code", None) != 200:
        raise AssertionError(
            f"{endpoint} returned HTTP {getattr(response, 'status_code', 'unknown')}"
        )
    try:
        payload = response.json()
    except Exception as exc:
        raise AssertionError(f"{endpoint} returned invalid JSON") from exc
    if not isinstance(payload, dict) or payload.get("code") != 0:
        raise AssertionError(f"{endpoint} returned unsuccessful response: {payload!r}")
    data = payload.get("data")
    if not isinstance(data, dict):
        raise AssertionError(f"{endpoint} response missing object data: {payload!r}")
    return data


def run_case3(app_config, cloud_client, metrics, report_dir: Path, iterations: int, workers: int) -> None:
    logger = get_logger("pocket_app_automation.case3")
    logger.info("case3 started")
    meta_start = time.perf_counter()
    response = cloud_client.get_current_app_package(
        app_config.cloud.app_package_current_url,
        method=app_config.cloud.app_package_method,
        auth_token=app_config.cloud.app_package_auth_token,
    )
    elapsed_ms = (time.perf_counter() - meta_start) * 1000
    metrics.record_metric(
        "case3",
        "app_package_metadata",
        app_config.cloud.app_package_method.upper(),
        app_config.cloud.app_package_current_url,
        response.status_code,
        elapsed_ms,
        ok=response.is_success,
        error="" if response.is_success else response.text[:200],
    )
    response.raise_for_status()
    payload = response.json()
    package_data = payload.get("data", payload)
    download_url = package_data.get("download_url")
    if not download_url:
        raise KeyError(f"response missing 'download_url': {payload}")
    try:
        integrity = resolve_integrity_expectation(
            package_data,
            configured_md5=app_config.cloud.apk_expected_md5,
            configured_sha256=app_config.cloud.apk_expected_sha256,
        )
    except IntegrityMetadataError as exc:
        raise AssertionError(f"case3 invalid integrity metadata: {exc}") from exc
    if integrity.status == UNVERIFIED:
        logger.warning(
            "case3 package metadata has no MD5/SHA-256; continuing with file-size/download checks only"
        )
    metrics.record_event(
        "case3_integrity_expectation",
        {
            "status": integrity.status,
            "algorithm": integrity.algorithm or None,
            "source": integrity.source,
            "expected_file_size": integrity.expected_file_size,
        },
    )

    run_download_pressure(
        scenario="case3",
        step_prefix="apk_download",
        download_url=download_url,
        iterations=iterations,
        workers=workers,
        expected_md5=integrity.expected_md5,
        expected_sha256=integrity.expected_sha256,
        expected_file_size=integrity.expected_file_size,
        output=ensure_parent(report_dir / "downloads" / "case3_latest.apk"),
        cloud_client=cloud_client,
        metrics=metrics,
        logger=logger,
        stop_on_first_failure=app_config.run.stop_on_first_failure,
    )
    logger.info("case3 completed successfully")


def run_case4(app_config, cloud_client, metrics, report_dir: Path, iterations: int, workers: int) -> None:
    logger = get_logger("pocket_app_automation.case4")
    logger.info("case4 started")
    meta_start = time.perf_counter()
    response = cloud_client.check_firmware(
        app_config.cloud.firmware_check_url,
        device_sn=app_config.device.device_id,
        method=app_config.cloud.firmware_check_method,
        auth_token=app_config.cloud.firmware_check_auth_token,
    )
    elapsed_ms = (time.perf_counter() - meta_start) * 1000
    metrics.record_metric(
        "case4",
        "firmware_metadata",
        app_config.cloud.firmware_check_method.upper(),
        app_config.cloud.firmware_check_url,
        response.status_code,
        elapsed_ms,
        ok=response.is_success,
        error="" if response.is_success else response.text[:200],
    )
    response.raise_for_status()
    payload = response.json()
    firmware_data = payload.get("data", payload)
    download_url = firmware_data["firmware_url"]
    try:
        integrity = resolve_integrity_expectation(
            firmware_data,
            configured_md5=app_config.cloud.firmware_expected_md5,
            configured_sha256=app_config.cloud.firmware_expected_sha256,
            md5_keys=("md5", "firmware_md5", "file_md5"),
            sha256_keys=("sha256", "firmware_sha256", "file_sha256"),
        )
    except IntegrityMetadataError as exc:
        raise AssertionError(f"case4 invalid integrity metadata: {exc}") from exc
    if integrity.status == UNVERIFIED:
        logger.warning(
            "case4 firmware metadata has no MD5/SHA-256; allowing download test only, upgrade is prohibited"
        )
    metrics.record_event(
        "case4_integrity_expectation",
        {
            "status": integrity.status,
            "algorithm": integrity.algorithm or None,
            "source": integrity.source,
            "expected_file_size": integrity.expected_file_size,
            "upgrade_allowed": integrity.status in {"EXPECTED_MD5", "EXPECTED_SHA256"},
        },
    )

    run_download_pressure(
        scenario="case4",
        step_prefix="firmware_download",
        download_url=download_url,
        iterations=iterations,
        workers=workers,
        expected_md5=integrity.expected_md5,
        expected_sha256=integrity.expected_sha256,
        expected_file_size=integrity.expected_file_size,
        output=ensure_parent(report_dir / "downloads" / "case4_latest.bin"),
        cloud_client=cloud_client,
        metrics=metrics,
        logger=logger,
        stop_on_first_failure=app_config.run.stop_on_first_failure,
    )
    logger.info("case4 completed successfully")


def run_case5(app_config, p2p_client, cloud_client, metrics, iterations: int) -> None:
    logger = get_logger("pocket_app_automation.case5")
    logger.info("case5 started")
    activation_code = app_config.cloud.activation_code.strip()
    if not activation_code:
        raise AssertionError("case5 requires cloud.activation_code in target.yaml.")
    if not app_config.device.device_id:
        raise AssertionError("device.device_id is required for case5")
    if not app_config.device.default_secret_key:
        raise AssertionError("device.default_secret_key is required for case5")
    if not app_config.cloud.activate_url:
        raise AssertionError("cloud.activate_url is required for case5")

    for index in range(iterations):
        raise_if_cancelled()
        iteration = index + 1
        device_id = app_config.device.device_id
        logger.info("case5 iteration=%s/%s start real activation flow", iteration, iterations)

        started = time.perf_counter()
        response = cloud_client.activate_device(app_config.cloud.activate_url, sn=device_id, activation_code=activation_code)
        elapsed_ms = (time.perf_counter() - started) * 1000
        metrics.record_metric(
            "case5",
            f"activation_{iteration}",
            "POST",
            app_config.cloud.activate_url,
            response.status_code,
            elapsed_ms,
            ok=response.is_success,
            error="" if response.is_success else response.text[:200],
        )
        response.raise_for_status()
        payload = response.json()
        activated = payload.get("activated")
        if activated is None and isinstance(payload.get("data"), dict):
            activated = payload["data"].get("activated")
        message = str(payload.get("msg") or payload.get("message") or "")
        passed = activated in (1, True) or "already" in message.lower() or "activated" in message.lower()
        if not passed:
            raise AssertionError(f"Unexpected activation payload: {payload}")
        metrics.record_event(
            "case5_activation_verified",
            {"iteration": iteration, "activated": activated, "message": message},
        )

        raise_if_cancelled()
        started = time.perf_counter()
        auth = authenticate_device(
            p2p_client=p2p_client,
            device_id=device_id,
            default_secret_key=app_config.device.default_secret_key,
            app_version=app_config.device.app_version,
            auth_wait_timeout_seconds=app_config.device.auth_wait_timeout_seconds,
        )
        elapsed_ms = (time.perf_counter() - started) * 1000
        metrics.record_metric(
            "case5",
            f"challenge_auth_{iteration}",
            "P2P",
            "/api/v1/device/challenge+/api/v1/device/auth",
            200,
            elapsed_ms,
            ok=True,
        )
        metrics.record_event(
            "case5_auth_completed",
            {"iteration": iteration, "auth_session_id": auth["auth_session_id"], "device_id": auth["device_id"]},
        )

        # A token is not sufficient proof of a working P2P session.  Exercise
        # one protected endpoint and validate the protocol envelope before the
        # iteration is reported as passed.
        protected_endpoint = "/api/v1/device/heartbeat"
        protected_started = time.perf_counter()
        protected_response = None
        try:
            protected_response = p2p_client.heartbeat(auth["token"])
            heartbeat_data = _parse_successful_protected_response(
                protected_response, protected_endpoint
            )
            online = heartbeat_data.get("online")
            if online not in (0, 1, True, False):
                raise AssertionError(
                    f"{protected_endpoint} response has invalid online field: {online!r}"
                )
        except Exception as exc:
            protected_elapsed_ms = (time.perf_counter() - protected_started) * 1000
            metrics.record_metric(
                "case5",
                f"protected_request_{iteration}",
                "GET",
                protected_endpoint,
                getattr(protected_response, "status_code", 0),
                protected_elapsed_ms,
                ok=False,
                error=str(exc)[:200],
            )
            metrics.record_event(
                "case5_protected_request_failed",
                {
                    "iteration": iteration,
                    "endpoint": protected_endpoint,
                    "error": str(exc)[:200],
                },
            )
            raise AssertionError(
                f"case5 authenticated session failed protected request: {exc}"
            ) from exc

        protected_elapsed_ms = (time.perf_counter() - protected_started) * 1000
        metrics.record_metric(
            "case5",
            f"protected_request_{iteration}",
            "GET",
            protected_endpoint,
            protected_response.status_code,
            protected_elapsed_ms,
            ok=True,
        )
        metrics.record_event(
            "case5_protected_request_verified",
            {
                "iteration": iteration,
                "endpoint": protected_endpoint,
                "online": heartbeat_data.get("online"),
            },
        )
        emit_case_event(
            ITERATION_PROGRESS,
            {"case": "case5", "iteration": iteration, "completed": iteration, "total": iterations, "ok": True},
        )
    logger.info("case5 completed successfully")
