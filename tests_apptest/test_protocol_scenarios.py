from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace

import pytest

from apptest.services.protocol_scenarios import run_case3, run_case4, run_case5


class FakeResponse:
    status_code = 200
    is_success = True
    text = ""

    def __init__(self, payload: dict) -> None:
        self._payload = payload

    def raise_for_status(self) -> None:
        return None

    def json(self) -> dict:
        return self._payload


class FakeMetrics:
    def __init__(self) -> None:
        self.events: list[tuple[str, dict]] = []
        self.metrics: list[tuple[tuple, dict]] = []

    def record_metric(self, *args, **kwargs) -> None:
        self.metrics.append((args, kwargs))

    def record_event(self, *args, **kwargs) -> None:
        self.events.append((args[0], args[1]))


def _config(apk_md5: str = "", firmware_md5: str = ""):
    return SimpleNamespace(
        device=SimpleNamespace(device_id="SN-TEST"),
        cloud=SimpleNamespace(
            app_package_current_url="https://example.test/app",
            app_package_method="GET",
            app_package_auth_token="",
            apk_expected_md5=apk_md5,
            apk_expected_sha256="",
            firmware_check_url="https://example.test/firmware",
            firmware_check_method="GET",
            firmware_check_auth_token="",
            firmware_expected_md5=firmware_md5,
            firmware_expected_sha256="",
        ),
        run=SimpleNamespace(stop_on_first_failure=False),
    )


def _case5_config():
    config = _config()
    config.device.default_secret_key = "secret"
    config.device.app_version = "test-version"
    config.device.auth_wait_timeout_seconds = 1
    config.cloud.activation_code = "ACTIVATE"
    config.cloud.activate_url = "https://example.test/activate"
    return config


def test_case3_uses_metadata_md5(monkeypatch, tmp_path: Path) -> None:
    client = SimpleNamespace(
        get_current_app_package=lambda *args, **kwargs: FakeResponse(
            {"data": {"download_url": "https://example.test/app.apk", "md5": "a" * 32, "file_size": 42}}
        )
    )
    captured: dict = {}
    monkeypatch.setattr(
        "apptest.services.protocol_scenarios.run_download_pressure",
        lambda **kwargs: captured.update(kwargs),
    )

    run_case3(_config(), client, FakeMetrics(), tmp_path, iterations=2, workers=1)

    assert captured["expected_md5"] == "a" * 32
    assert captured["expected_file_size"] == 42


def test_case3_allows_missing_md5(monkeypatch, tmp_path: Path) -> None:
    client = SimpleNamespace(
        get_current_app_package=lambda *args, **kwargs: FakeResponse(
            {"data": {"download_url": "https://example.test/app.apk", "file_size": 42}}
        )
    )
    captured: dict = {}
    monkeypatch.setattr(
        "apptest.services.protocol_scenarios.run_download_pressure",
        lambda **kwargs: captured.update(kwargs),
    )

    run_case3(_config(), client, FakeMetrics(), tmp_path, iterations=2, workers=1)

    assert captured["expected_md5"] == ""
    assert captured["expected_file_size"] == 42


def test_case4_allows_missing_md5(monkeypatch, tmp_path: Path) -> None:
    client = SimpleNamespace(
        check_firmware=lambda *args, **kwargs: FakeResponse(
            {"data": {"firmware_url": "https://example.test/firmware.bin", "file_size": 42}}
        )
    )
    captured: dict = {}
    monkeypatch.setattr(
        "apptest.services.protocol_scenarios.run_download_pressure",
        lambda **kwargs: captured.update(kwargs),
    )

    run_case4(_config(), client, FakeMetrics(), tmp_path, iterations=2, workers=1)

    assert captured["expected_md5"] == ""
    assert captured["expected_file_size"] == 42


def test_case5_verifies_authenticated_heartbeat(monkeypatch) -> None:
    class FakeP2P:
        def __init__(self) -> None:
            self.tokens: list[str] = []

        def heartbeat(self, token: str) -> FakeResponse:
            self.tokens.append(token)
            return FakeResponse({"code": 0, "data": {"online": 1}})

    cloud = type(
        "FakeCloud",
        (),
        {"activate_device": lambda self, *args, **kwargs: FakeResponse({"activated": 1})},
    )()
    p2p = FakeP2P()
    metrics = FakeMetrics()
    monkeypatch.setattr(
        "apptest.services.protocol_scenarios.authenticate_device",
        lambda **kwargs: {
            "token": "session-token",
            "auth_session_id": "auth-session",
            "device_id": "SN-TEST",
        },
    )
    monkeypatch.setattr("apptest.services.protocol_scenarios.emit_case_event", lambda *args, **kwargs: None)

    run_case5(_case5_config(), p2p, cloud, metrics, iterations=1)

    assert p2p.tokens == ["session-token"]
    assert any(name == "case5_protected_request_verified" for name, _ in metrics.events)


def test_case5_fails_when_authenticated_heartbeat_is_rejected(monkeypatch) -> None:
    class FakeP2P:
        def heartbeat(self, token: str) -> FakeResponse:
            return FakeResponse({"code": 401, "msg": "Unauthorized", "data": {}})

    cloud = type(
        "FakeCloud",
        (),
        {"activate_device": lambda self, *args, **kwargs: FakeResponse({"activated": 1})},
    )()
    metrics = FakeMetrics()
    monkeypatch.setattr(
        "apptest.services.protocol_scenarios.authenticate_device",
        lambda **kwargs: {
            "token": "session-token",
            "auth_session_id": "auth-session",
            "device_id": "SN-TEST",
        },
    )
    monkeypatch.setattr("apptest.services.protocol_scenarios.emit_case_event", lambda *args, **kwargs: None)

    with pytest.raises(AssertionError, match="protected request"):
        run_case5(_case5_config(), FakeP2P(), cloud, metrics, iterations=1)

    assert any(name == "case5_protected_request_failed" for name, _ in metrics.events)
