from __future__ import annotations

from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Iterable

from apptest.core.execution import (
    CaseCancelled,
    CancellationToken,
    ProgressCallback,
    case_execution_context,
    emit_case_event,
    is_cancelled,
)
from apptest.core.events import CASE_FINISHED, CASE_STARTED, ITERATION_PROGRESS


@dataclass(frozen=True)
class CaseDefinition:
    name: str
    backend: str
    backend_case: str
    title: str
    description: str


CASE_DEFINITIONS = {
    "case1": CaseDefinition("case1", "uia", "uia_case1", "视频下载与删除", "通过 Android App 下载 Pocket 视频并删除本地副本。"),
    "case2": CaseDefinition("case2", "uia", "uia_case2", "视频预览循环", "通过 Android App 轮询预览 Pocket 视频并检查播放异常。"),
    "case3": CaseDefinition("case3", "protocol", "case3", "APK 下载压测", "获取 APK 信息，循环下载并校验文件和 MD5。"),
    "case4": CaseDefinition("case4", "protocol", "case4", "固件下载压测", "获取固件信息，循环下载并校验文件和 MD5。"),
    "case5": CaseDefinition("case5", "protocol", "case5", "设备激活测试", "调用云端激活接口，再执行 Pocket challenge/auth 验证。"),
    "case6": CaseDefinition("case6", "uia", "uia_case6", "设备连接测试", "通过 Android App 发现、连接并断开 Pocket 设备。"),
    "case7": CaseDefinition("case7", "uia", "uia_case7", "monkey 测试", "按页面/动作权重随机操作 App，全程抓 logcat，结束时必须保持连接。"),
}


@dataclass(frozen=True)
class CaseRunRequest:
    config: str | Path
    case_name: str
    base_dir: str | Path | None = None
    report_name: str = ""
    iterations: int = 0
    workers: int = 0
    progress_callback: ProgressCallback | None = None
    cancellation_token: CancellationToken | None = None
    options: dict | None = None
    device_overrides: dict | None = None


def list_cases() -> list[dict]:
    """Return serializable metadata for the six supported application cases."""
    return [asdict(CASE_DEFINITIONS[name]) for name in CASE_DEFINITIONS]


def run_case(request: CaseRunRequest) -> dict:
    """Run one canonical case and return paths/status for GUI or CLI consumers."""
    definition = CASE_DEFINITIONS.get(request.case_name)
    if definition is None:
        raise ValueError(f"Unsupported case: {request.case_name}")

    with case_execution_context(request.progress_callback, request.cancellation_token):
        emit_case_event(
            CASE_STARTED,
            {
                "case": definition.name,
                "backend": definition.backend,
                "backend_case": definition.backend_case,
                "iterations": request.iterations,
                "workers": request.workers,
            },
        )
        try:
            if is_cancelled():
                result = {"exit_code": 2, "error": "测试在启动前已被停止"}
            elif definition.backend == "protocol":
                from apptest.services.protocol_cases import run_protocol_case

                result = run_protocol_case(
                    config=request.config,
                    case_name=definition.backend_case,
                    report_name=request.report_name,
                    iterations=request.iterations,
                    workers=request.workers,
                    base_dir=request.base_dir,
                    device_overrides=request.device_overrides,
                )
            else:
                from apptest.services.uia_cases import run_uia_case

                result = run_uia_case(
                    config=request.config,
                    case_name=definition.backend_case,
                    report_name=request.report_name,
                    iterations=request.iterations,
                    base_dir=request.base_dir,
                    options=request.options,
                    device_overrides=request.device_overrides,
                )
        except CaseCancelled as exc:
            result = {"exit_code": 2, "error": str(exc)}
        except Exception as exc:  # noqa: BLE001
            result = {"exit_code": 1, "error": str(exc)}

        normalized = _normalize_result(result, definition)
        emit_case_event(CASE_FINISHED, normalized.copy())
        return normalized


def run_cases(requests: Iterable[CaseRunRequest]) -> list[dict]:
    """Run multiple cases sequentially and return one result per request."""
    return [run_case(request) for request in requests]


def _normalize_result(result: dict, definition: CaseDefinition) -> dict:
    exit_code = int(result.get("exit_code", 1))
    if exit_code == 0:
        status = "passed"
    elif exit_code == 2:
        status = "cancelled"
    else:
        status = "failed"
    return {
        "case": definition.name,
        "backend": definition.backend,
        "backend_case": definition.backend_case,
        "status": status,
        "exit_code": exit_code,
        "error": str(result.get("error") or result.get("message") or ""),
        "report_dir": str(result.get("report_dir") or ""),
        "report_html": str(result.get("report_html") or ""),
        "summary_json": str(result.get("summary_json") or ""),
        "metrics_csv": str(result.get("metrics_csv") or ""),
        "failures_jsonl": str(result.get("failures_jsonl") or ""),
        "events_jsonl": str(result.get("events_jsonl") or ""),
        "log_file": str(result.get("log_file") or ""),
        "app_logcat_file": str(result.get("app_logcat_file") or ""),
        "summary": result.get("summary") or {},
    }
