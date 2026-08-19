from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import yaml

from apptest.core.integrity import validate_config_digest


@dataclass
class DeviceConfig:
    host: str
    port: int
    ble_name_prefix: str = "pocket-"
    ble_exact_name: str = ""
    default_secret_key: str = ""
    device_id: str = ""
    app_version: str = "2.0.1"
    auth_wait_timeout_seconds: int = 60
    connect_timeout_seconds: int = 10
    request_timeout_seconds: int = 30
    sample_video_file_path: str = ""
    sample_delete_file_paths: list[str] = field(default_factory=list)

    @property
    def base_url(self) -> str:
        return f"http://{self.host}:{self.port}"


@dataclass
class CloudConfig:
    app_package_current_url: str
    app_package_method: str = "POST"
    app_package_auth_token: str = ""
    firmware_check_url: str = ""
    firmware_check_method: str = "GET"
    firmware_check_auth_token: str = ""
    activate_url: str = ""
    activation_code: str = ""
    firmware_expected_md5: str = ""
    apk_expected_md5: str = ""
    firmware_expected_sha256: str = ""
    apk_expected_sha256: str = ""


@dataclass
class RunConfig:
    report_output_root: str
    download_dir: str
    case1_iterations: int = 1
    case2_iterations: int = 1000
    case3_iterations: int = 5000
    case4_iterations: int = 5000
    case5_iterations: int = 1000
    case6_iterations: int = 1000
    case7_iterations: int = 1000
    pressure_workers: int = 8
    preview_range_header: str = "bytes=0-1048575"
    stop_on_first_failure: bool = False


@dataclass
class MobileConfig:
    android_serial: str = ""
    android_package_name: str = ""
    android_app_activity: str = ""
    ui_wait_timeout_seconds: int = 20
    ui_settle_seconds: float = 1.5
    delete_confirm_text: str = "删除"


@dataclass
class AppConfig:
    device: DeviceConfig
    cloud: CloudConfig
    run: RunConfig
    mobile: MobileConfig


def _load_yaml(path: Path) -> dict[str, Any]:
    with path.open("r", encoding="utf-8") as handle:
        return yaml.safe_load(handle) or {}


def _resolve_path_value(base_dir: Path, value: str) -> str:
    if not value:
        return value
    path = Path(value)
    if path.is_absolute():
        return str(path)
    return str((base_dir / path).resolve())


def load_config(path: str | Path, base_dir: str | Path | None = None) -> AppConfig:
    config_path = Path(path).resolve()
    payload = _load_yaml(config_path)
    resolved_base_dir = Path(base_dir).resolve() if base_dir is not None else config_path.parent

    device_payload = dict(payload.get("device", {}))
    cloud_payload = dict(payload.get("cloud", {}))
    run_payload = dict(payload.get("run", {}))
    mobile_payload = dict(payload.get("mobile", {}))

    if "sample_video_file_path" in device_payload:
        device_payload["sample_video_file_path"] = _resolve_path_value(resolved_base_dir, device_payload.get("sample_video_file_path", ""))
    if "sample_delete_file_paths" in device_payload:
        device_payload["sample_delete_file_paths"] = [
            _resolve_path_value(resolved_base_dir, item) for item in device_payload.get("sample_delete_file_paths", [])
        ]
    if "report_output_root" in run_payload:
        run_payload["report_output_root"] = _resolve_path_value(resolved_base_dir, run_payload.get("report_output_root", "artifacts"))
    if "download_dir" in run_payload:
        run_payload["download_dir"] = _resolve_path_value(resolved_base_dir, run_payload.get("download_dir", "artifacts/downloads"))

    return AppConfig(
        device=DeviceConfig(**device_payload),
        cloud=CloudConfig(**cloud_payload),
        run=RunConfig(**run_payload),
        mobile=MobileConfig(**mobile_payload),
    )


def apply_device_overrides(app_config: AppConfig, overrides: dict | None) -> AppConfig:
    """Apply transient GUI/CLI overrides without changing target.yaml."""
    values = {key: str(value).strip() for key, value in (overrides or {}).items() if str(value).strip()}
    if "host" in values:
        app_config.device.host = values["host"]
    if "ble_exact_name" in values:
        app_config.device.ble_exact_name = values["ble_exact_name"]
    if "device_id" in values:
        app_config.device.device_id = values["device_id"]
    if "activation_code" in values:
        app_config.cloud.activation_code = values["activation_code"]
    if "android_serial" in values:
        app_config.mobile.android_serial = values["android_serial"]
    return app_config


def validate_config_for_requested_case(app_config: AppConfig, case_name: str) -> list[str]:
    """Validate only fields the selected case consumes."""
    problems: list[str] = []
    if case_name.startswith("uia_") and not app_config.mobile.android_serial.strip():
        problems.append("mobile.android_serial 未配置（多设备时必须指定 adb 设备）")
    if case_name in {"case4", "case5"} and not app_config.device.device_id.strip():
        problems.append("device.device_id 未配置")
    if case_name == "case5":
        if not app_config.device.host.strip():
            problems.append("device.host 未配置")
        if not app_config.cloud.activation_code.strip():
            problems.append("cloud.activation_code 未配置")
    if case_name in {"uia_case6", "uia_case7"} and not app_config.device.ble_exact_name.strip():
        problems.append("device.ble_exact_name 未配置（需要 App 设备名称）")
    return problems


def validate_config(app_config: AppConfig) -> list[str]:
    """Return human-readable (Chinese) problems detected in the loaded config.

    An empty list means the config is usable for every supported case. Checks
    are grouped per case so the GUI can show preflight diagnostics before a run.
    """
    problems: list[str] = []
    device = app_config.device
    cloud = app_config.cloud
    run = app_config.run
    mobile = app_config.mobile

    if not device.host.strip():
        problems.append("device.host 未配置（必须填写设备地址）")
    if device.port <= 0:
        problems.append(f"device.port 无效（当前值 {device.port}）")

    if not cloud.app_package_current_url.strip():
        problems.append("cloud.app_package_current_url 未配置（case3 获取安装包信息需要）")
    if not cloud.firmware_check_url.strip():
        problems.append("cloud.firmware_check_url 未配置（case4 检查固件需要）")
    if not cloud.activate_url.strip():
        problems.append("cloud.activate_url 未配置（case5 设备激活需要）")
    if not cloud.activation_code.strip():
        problems.append("cloud.activation_code 未配置（case5 激活码需要）")
    for field_name, value, algorithm in [
        ("cloud.apk_expected_md5", cloud.apk_expected_md5, "md5"),
        ("cloud.firmware_expected_md5", cloud.firmware_expected_md5, "md5"),
        ("cloud.apk_expected_sha256", cloud.apk_expected_sha256, "sha256"),
        ("cloud.firmware_expected_sha256", cloud.firmware_expected_sha256, "sha256"),
    ]:
        problem = validate_config_digest(value, algorithm, field_name)
        if problem:
            problems.append(problem)

    if cloud.app_package_method.upper() not in {"GET", "POST"}:
        problems.append("cloud.app_package_method 必须是 GET 或 POST")
    if cloud.firmware_check_method.upper() not in {"GET", "POST"}:
        problems.append("cloud.firmware_check_method 必须是 GET 或 POST")
    if "api.example.com" in cloud.firmware_check_url:
        problems.append("cloud.firmware_check_url 仍是协议占位地址，必须替换为真实固件接口")

    if not device.device_id.strip():
        problems.append("device.device_id 未配置（case4/case5 设备序列号需要）")
    if not device.default_secret_key.strip():
        problems.append("device.default_secret_key 未配置（case5 密钥认证需要）")

    if run.case1_iterations <= 0:
        problems.append("run.case1_iterations 必须大于 0")
    if run.case2_iterations <= 0:
        problems.append("run.case2_iterations 必须大于 0")
    if run.case3_iterations <= 0:
        problems.append("run.case3_iterations 必须大于 0")
    if run.case4_iterations <= 0:
        problems.append("run.case4_iterations 必须大于 0")
    if run.case5_iterations <= 0:
        problems.append("run.case5_iterations 必须大于 0")
    if run.case6_iterations <= 0:
        problems.append("run.case6_iterations 必须大于 0")
    if run.case7_iterations <= 0:
        problems.append("run.case7_iterations 必须大于 0")

    if not device.ble_exact_name.strip():
        problems.append("device.ble_exact_name 未配置（case6 多设备 UIA 需要指定目标设备名）")

    if not mobile.android_serial.strip():
        problems.append("mobile.android_serial 未配置（将自动连接唯一设备，多设备时需手动指定）")
    if not mobile.android_package_name.strip():
        problems.append("mobile.android_package_name 未配置（将读取当前前台应用，建议明确指定）")
    if mobile.ui_wait_timeout_seconds <= 0:
        problems.append("mobile.ui_wait_timeout_seconds 必须大于 0")

    return problems
