from __future__ import annotations

from pathlib import Path

from apptest.core.uia_artifacts import prepare_uia_case_artifacts


def test_prepare_uia_case_artifacts_creates_separate_log_files(tmp_path: Path) -> None:
    artifacts = prepare_uia_case_artifacts(tmp_path, "uia_case1")

    assert artifacts.logs_dir.exists()
    assert artifacts.dumps_dir.exists()
    assert artifacts.screenshots_dir.exists()
    assert artifacts.script_log_file == tmp_path / "logs" / "uia_case1.log"
    assert artifacts.app_logcat_file == tmp_path / "logs" / "uia_case1.app.logcat.txt"
