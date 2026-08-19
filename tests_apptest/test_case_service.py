from __future__ import annotations

import threading

from apptest.services.case_service import CaseRunRequest, list_cases, run_case


def test_list_cases_exposes_exactly_seven_canonical_cases() -> None:
    cases = list_cases()
    assert [item["name"] for item in cases] == [f"case{index}" for index in range(1, 8)]
    assert {item["backend"] for item in cases} == {"uia", "protocol"}


def test_run_case_normalizes_protocol_result_and_emits_events(monkeypatch) -> None:
    events: list[tuple[str, dict]] = []

    def fake_run(**kwargs):
        assert kwargs["case_name"] == "case3"
        assert kwargs["base_dir"] == "host-data"
        return {"exit_code": 0, "report_html": "report.html", "summary": {"requests_total": 3}}

    monkeypatch.setattr("apptest.services.protocol_cases.run_protocol_case", fake_run)
    result = run_case(
        CaseRunRequest(
            config="unused.yaml",
            case_name="case3",
            base_dir="host-data",
            iterations=3,
            workers=2,
            progress_callback=lambda name, payload: events.append((name, payload)),
        )
    )

    assert result["status"] == "passed"
    assert result["backend"] == "protocol"
    assert result["backend_case"] == "case3"
    assert result["error"] == ""
    assert result["events_jsonl"] == ""
    assert [name for name, _ in events] == ["case_started", "case_finished"]


def test_run_case_passes_device_overrides_to_backend(monkeypatch) -> None:
    def fake_run(**kwargs):
        assert kwargs["device_overrides"] == {"host": "192.168.1.101"}
        return {"exit_code": 0}

    monkeypatch.setattr("apptest.services.protocol_cases.run_protocol_case", fake_run)
    result = run_case(CaseRunRequest(
        config="unused.yaml", case_name="case3", device_overrides={"host": "192.168.1.101"},
    ))
    assert result["status"] == "passed"


def test_run_case_returns_cancelled_before_start(monkeypatch) -> None:
    called = False

    def fake_run(**kwargs):
        nonlocal called
        called = True
        return {"exit_code": 0}

    monkeypatch.setattr("apptest.services.uia_cases.run_uia_case", fake_run)
    cancel_event = threading.Event()
    cancel_event.set()
    result = run_case(CaseRunRequest(config="unused.yaml", case_name="case1", cancellation_token=cancel_event))

    assert called is False
    assert result["status"] == "cancelled"
    assert result["exit_code"] == 2
    assert result["error"]


def test_run_case_normalizes_startup_error(monkeypatch) -> None:
    def fake_run(**kwargs):
        raise FileNotFoundError("missing config")

    monkeypatch.setattr("apptest.services.protocol_cases.run_protocol_case", fake_run)
    result = run_case(CaseRunRequest(config="missing.yaml", case_name="case4"))

    assert result["status"] == "failed"
    assert result["exit_code"] == 1
    assert result["error"] == "missing config"
    assert set(result) == {
        "case",
        "backend",
        "backend_case",
        "status",
        "exit_code",
        "error",
        "report_dir",
        "report_html",
        "summary_json",
        "metrics_csv",
        "failures_jsonl",
        "events_jsonl",
        "log_file",
        "app_logcat_file",
        "summary",
    }
