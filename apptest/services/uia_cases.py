from __future__ import annotations

import json
from pathlib import Path

from apptest.core.artifacts import make_run_dir
from apptest.core.config import apply_device_overrides, load_config, validate_config_for_requested_case
from apptest.core.execution import CaseCancelled, raise_if_cancelled
from apptest.core.logging_utils import get_logger, setup_logging
from apptest.core.reporting import MetricsRecorder, write_chinese_report
from apptest.mobile_uia.case1 import run_uia_case1
from apptest.mobile_uia.case2 import run_uia_case2
from apptest.mobile_uia.case6 import run_uia_case6
from apptest.mobile_uia.case7 import run_uia_case7
from apptest.services._environment import serialized_case_execution


PROJECT_ROOT = Path(__file__).resolve().parents[2]
UIA_CASES = ("uia_case1", "uia_case2", "uia_case6", "uia_case7")
UIA_RUNNERS = {
    "uia_case1": run_uia_case1,
    "uia_case2": run_uia_case2,
    "uia_case6": run_uia_case6,
    "uia_case7": run_uia_case7,
}


def run_uia_case(
    config: str | Path,
    case_name: str,
    report_name: str = "",
    iterations: int = 0,
    base_dir: str | Path | None = None,
    options: dict | None = None,
    device_overrides: dict | None = None,
) -> dict:
    if case_name not in UIA_RUNNERS:
        raise ValueError(f"Unsupported UIA case: {case_name}")

    config_path = Path(config).resolve()
    if not config_path.exists():
        raise FileNotFoundError(f"Config file not found: {config_path}")

    with serialized_case_execution():
        raise_if_cancelled()
        config_base_dir = Path(base_dir).resolve() if base_dir is not None else PROJECT_ROOT
        app_config = apply_device_overrides(load_config(config_path, base_dir=config_base_dir), device_overrides)
        problems = validate_config_for_requested_case(app_config, case_name)
        if problems:
            raise ValueError("; ".join(problems))
        if iterations > 0:
            iteration_field = {
                "uia_case1": "case1_iterations",
                "uia_case2": "case2_iterations",
                "uia_case6": "case6_iterations",
                "uia_case7": "case7_iterations",
            }[case_name]
            setattr(app_config.run, iteration_field, iterations)
        run_dir = make_run_dir(Path(app_config.run.report_output_root), report_name or case_name)
        logging_context = setup_logging(run_dir / "logs", case_name)
        logger = get_logger("pocket_app_automation.uia_runner")
        logger.info(
            "uia run start case=%s config=%s report_dir=%s iterations_override=%s",
            case_name,
            config_path,
            run_dir,
            iterations,
        )

        try:
            runner = UIA_RUNNERS[case_name]
            if case_name == "uia_case7":
                result = runner(app_config, run_dir, options=options)
            else:
                result = runner(app_config, run_dir)
        except CaseCancelled as exc:
            logger.info("uia run cancelled case=%s", case_name)
            result = {
                "case": case_name,
                "exit_code": 2,
                "report_dir": str(run_dir),
                "report_html": "",
                "summary_json": "",
                "metrics_csv": "",
                "failures_jsonl": "",
                "events_jsonl": "",
                "log_file": str(logging_context.log_file),
                "app_logcat_file": "",
                "summary": {},
                "error": str(exc),
            }
        except Exception as exc:  # noqa: BLE001
            logger.exception("uia run failed case=%s", case_name)
            result = {
                "case": case_name,
                "exit_code": 1,
                "report_dir": str(run_dir),
                "report_html": "",
                "summary_json": "",
                "metrics_csv": "",
                "failures_jsonl": "",
                "events_jsonl": "",
                "log_file": str(logging_context.log_file),
                "app_logcat_file": "",
                "summary": {},
                "error": str(exc),
            }

        if not result.get("log_file"):
            result["log_file"] = str(logging_context.log_file)

        existing_report = run_dir / "report.html"
        case_report = run_dir / "case_report.html"
        if existing_report.exists():
            if case_report.exists():
                case_report.unlink()
            existing_report.replace(case_report)

        summary = result.get("summary") or MetricsRecorder(run_dir).build_summary(
            case_name,
            int(result.get("exit_code", 1)),
        )
        report_html = write_chinese_report(
            report_dir=run_dir,
            scenario=case_name,
            summary=summary,
            exit_code=int(result.get("exit_code", 1)),
            fallback_error=str(result.get("error") or result.get("message") or ""),
            original_report=case_report if case_report.exists() else None,
        )
        result["summary"] = summary
        result["report_html"] = str(report_html)
        logger.info("uia run complete result=%s", json.dumps(result, ensure_ascii=False))
        return result
