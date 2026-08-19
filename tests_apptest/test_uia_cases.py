from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace

from apptest.core.execution import CaseCancelled
from apptest.services.uia_cases import UIA_RUNNERS, run_uia_case


def test_uia_runner_reports_cooperative_cancellation(monkeypatch, tmp_path) -> None:
    config_path = tmp_path / "target.yaml"
    config_path.write_text("device: {}\n", encoding="utf-8")
    app_config = SimpleNamespace(
        run=SimpleNamespace(
            report_output_root=str(tmp_path / "reports"),
            case1_iterations=1,
            case2_iterations=1,
            case6_iterations=1,
        )
    )

    monkeypatch.setattr("apptest.services.uia_cases.load_config", lambda path, base_dir: app_config)
    monkeypatch.setitem(
        UIA_RUNNERS,
        "uia_case1",
        lambda config, report_dir: (_ for _ in ()).throw(CaseCancelled("测试已停止")),
    )

    result = run_uia_case(config=config_path, case_name="uia_case1", base_dir=tmp_path)

    assert result["exit_code"] == 2
    assert result["error"] == "测试已停止"
    assert Path(result["report_html"]).is_file()
