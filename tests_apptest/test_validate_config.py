from __future__ import annotations

from apptest.core.config import (
    AppConfig,
    CloudConfig,
    DeviceConfig,
    MobileConfig,
    RunConfig,
    apply_device_overrides,
    validate_config,
    validate_config_for_requested_case,
)


def make_config() -> AppConfig:
    return AppConfig(
        device=DeviceConfig(
            host="192.168.4.1",
            port=8080,
            device_id="SN1234567890X",
            default_secret_key="default_secret_key",
            ble_exact_name="pocket-test",
        ),
        cloud=CloudConfig(
            app_package_current_url="https://example.test/app",
            firmware_check_url="https://example.test/firmware",
            activate_url="https://example.test/activate",
            activation_code="code",
            apk_expected_md5="a" * 32,
            firmware_expected_md5="b" * 32,
        ),
        run=RunConfig(
            report_output_root="artifacts",
            download_dir="artifacts/downloads",
            case1_iterations=1,
            case2_iterations=2,
            case3_iterations=3,
            case4_iterations=4,
            case5_iterations=5,
            case6_iterations=6,
        ),
        mobile=MobileConfig(android_serial="abc", android_package_name="com.example.app"),
    )


def test_validate_config_accepts_complete_config() -> None:
    assert validate_config(make_config()) == []


def test_validate_config_reports_missing_required_keys() -> None:
    config = make_config()
    config.device.host = ""
    config.cloud.activation_code = ""
    config.device.ble_exact_name = ""
    config.run.case5_iterations = 0

    problems = validate_config(config)
    joined = "\n".join(problems)
    assert "device.host" in joined
    assert "cloud.activation_code" in joined
    assert "device.ble_exact_name" in joined
    assert "run.case5_iterations" in joined


def test_device_overrides_take_precedence_for_requested_case() -> None:
    config = make_config()
    applied = apply_device_overrides(config, {
        "host": "192.168.1.101",
        "android_serial": "adb-2",
        "ble_exact_name": "Gimbal Camera-123456",
        "device_id": "SN-2",
        "activation_code": "code-2",
    })
    assert applied.device.host == "192.168.1.101"
    assert applied.mobile.android_serial == "adb-2"
    assert applied.device.ble_exact_name == "Gimbal Camera-123456"
    assert applied.device.device_id == "SN-2"
    assert applied.cloud.activation_code == "code-2"
    assert validate_config_for_requested_case(applied, "uia_case6") == []
