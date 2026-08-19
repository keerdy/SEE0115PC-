from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace

from apptest.services.protocol_cases import run_protocol_case


class FakeClient:
    def __init__(self, *args, **kwargs) -> None:
        self.closed = False

    def close(self) -> None:
        self.closed = True


def _fake_config(report_root: Path):
    return SimpleNamespace(
        device=SimpleNamespace(base_url="http://192.0.2.1:8080", request_timeout_seconds=1),
        run=SimpleNamespace(
            report_output_root=str(report_root),
            pressure_workers=4,
            case3_iterations=3,
            case4_iterations=3,
            case5_iterations=3,
        ),
    )


def test_protocol_runner_executes_without_pytest_and_writes_report(monkeypatch, tmp_path) -> None:
    config_path = tmp_path / "target.yaml"
    config_path.write_text("device: {}\n", encoding="utf-8")
    host_root = tmp_path / "host"
    clients: list[FakeClient] = []
    calls: list[tuple[int, int]] = []

    def make_client(*args, **kwargs):
        client = FakeClient()
        clients.append(client)
        return client

    def fake_load_config(path, base_dir):
        assert Path(path) == config_path.resolve()
        assert Path(base_dir) == host_root.resolve()
        return _fake_config(host_root / "reports")

    def fake_case3(app_config, cloud_client, metrics, report_dir, iterations, workers):
        calls.append((iterations, workers))
        metrics.record_event("fake_case3", {"report_dir": str(report_dir)})

    monkeypatch.setattr("apptest.services.protocol_cases.load_config", fake_load_config)
    monkeypatch.setattr("apptest.services.protocol_cases.CloudClient", make_client)
    monkeypatch.setattr("apptest.services.protocol_cases.P2PClient", make_client)
    monkeypatch.setattr("apptest.services.protocol_cases.run_case3", fake_case3)

    result = run_protocol_case(
        config=config_path,
        case_name="case3",
        report_name="integration",
        iterations=7,
        workers=2,
        base_dir=host_root,
    )

    assert result["exit_code"] == 0
    assert calls == [(7, 2)]
    assert Path(result["report_html"]).is_file()
    assert Path(result["summary_json"]).is_file()
    assert Path(result["events_jsonl"]).is_file()
    assert all(client.closed for client in clients)


def test_protocol_runner_normalizes_scenario_failure_with_artifacts(monkeypatch, tmp_path) -> None:
    config_path = tmp_path / "target.yaml"
    config_path.write_text("device: {}\n", encoding="utf-8")

    monkeypatch.setattr(
        "apptest.services.protocol_cases.load_config",
        lambda path, base_dir: _fake_config(tmp_path / "reports"),
    )
    monkeypatch.setattr("apptest.services.protocol_cases.CloudClient", FakeClient)
    monkeypatch.setattr("apptest.services.protocol_cases.P2PClient", FakeClient)
    monkeypatch.setattr(
        "apptest.services.protocol_cases.run_case3",
        lambda *args, **kwargs: (_ for _ in ()).throw(RuntimeError("scenario failed")),
    )

    result = run_protocol_case(config=config_path, case_name="case3")

    assert result["exit_code"] == 1
    assert result["error"] == "scenario failed"
    assert Path(result["report_html"]).is_file()
    assert Path(result["failures_jsonl"]).is_file()
