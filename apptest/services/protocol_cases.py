from __future__ import annotations

import json
from pathlib import Path

from apptest.clients.cloud_client import CloudClient
from apptest.clients.p2p_client import P2PClient
from apptest.core.artifacts import make_run_dir
from apptest.core.config import apply_device_overrides, load_config, validate_config_for_requested_case
from apptest.core.execution import CaseCancelled, emit_case_event, raise_if_cancelled
from apptest.core.events import PROTOCOL_CASE_COMPLETED
from apptest.core.logging_utils import get_logger, setup_logging
from apptest.core.reporting import MetricsRecorder, write_chinese_report
from apptest.services._environment import serialized_case_execution
from apptest.services.protocol_scenarios import run_case3, run_case4, run_case5


PROJECT_ROOT = Path(__file__).resolve().parents[2]
PROTOCOL_CASES = ("case3", "case4", "case5")


def run_protocol_case(
    config: str | Path,
    case_name: str,
    report_name: str = "",
    iterations: int = 0,
    workers: int = 0,
    base_dir: str | Path | None = None,
    device_overrides: dict | None = None,
) -> dict:
    if case_name not in (*PROTOCOL_CASES, "all"):
        raise ValueError(f"Unsupported protocol case: {case_name}")
    config_path = Path(config).resolve()
    if not config_path.exists():
        raise FileNotFoundError(f"Config file not found: {config_path}")

    with serialized_case_execution():
        raise_if_cancelled()
        config_base_dir = Path(base_dir).resolve() if base_dir is not None else PROJECT_ROOT
        app_config = apply_device_overrides(load_config(config_path, base_dir=config_base_dir), device_overrides)
        selected_cases = PROTOCOL_CASES if case_name == "all" else (case_name,)
        problems = [problem for selected in selected_cases
                    for problem in validate_config_for_requested_case(app_config, selected)]
        if problems:
            raise ValueError("; ".join(dict.fromkeys(problems)))
        run_dir = make_run_dir(app_config.run.report_output_root, report_name or case_name)
        logging_context = setup_logging(run_dir / "logs", case_name)
        logger = get_logger("pocket_app_automation.protocol_runner")
        metrics = MetricsRecorder(run_dir)
        exit_code = 0
        error_message = ""
        cloud_client = CloudClient(timeout_seconds=app_config.device.request_timeout_seconds)
        p2p_client = P2PClient(app_config.device.base_url, timeout_seconds=app_config.device.request_timeout_seconds)
        try:
            for selected in selected_cases:
                raise_if_cancelled()
                selected_iterations = iterations if iterations > 0 and case_name != "all" else getattr(
                    app_config.run, f"{selected}_iterations"
                )
                selected_workers = workers if workers > 0 else app_config.run.pressure_workers
                logger.info(
                    "protocol scenario start case=%s config=%s iterations=%s workers=%s",
                    selected,
                    config_path,
                    selected_iterations,
                    selected_workers,
                )
                if selected == "case3":
                    run_case3(app_config, cloud_client, metrics, run_dir, selected_iterations, selected_workers)
                elif selected == "case4":
                    run_case4(app_config, cloud_client, metrics, run_dir, selected_iterations, selected_workers)
                else:
                    run_case5(app_config, p2p_client, cloud_client, metrics, selected_iterations)
        except CaseCancelled as exc:
            exit_code = 2
            error_message = str(exc)
            metrics.record_event("protocol_case_cancelled", {"case": case_name, "error": error_message})
            logger.info("protocol run cancelled case=%s", case_name)
        except Exception as exc:  # noqa: BLE001
            exit_code = 1
            error_message = str(exc)
            metrics.record_failure(case_name, "execution", {"error": error_message})
            logger.exception("protocol run failed case=%s", case_name)
        finally:
            p2p_client.close()
            cloud_client.close()

        summary = metrics.build_summary(case_name, exit_code)
        report_html = write_chinese_report(
            report_dir=run_dir,
            scenario=case_name,
            summary=summary,
            exit_code=exit_code,
            fallback_error=error_message,
        )
        result = {
            "case": case_name,
            "exit_code": exit_code,
            "report_dir": str(run_dir),
            "report_html": str(report_html),
            "summary_json": str(run_dir / "summary.json"),
            "metrics_csv": str(run_dir / "metrics.csv"),
            "failures_jsonl": str(run_dir / "failures.jsonl"),
            "events_jsonl": str(run_dir / "events.jsonl"),
            "log_file": str(logging_context.log_file),
            "app_logcat_file": "",
            "error": error_message,
            "summary": summary,
        }
        emit_case_event(PROTOCOL_CASE_COMPLETED, {"case": case_name, "exit_code": exit_code})
        logger.info("protocol run complete result=%s", json.dumps(result, ensure_ascii=False))
        return result
