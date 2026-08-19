from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path


@dataclass
class UiaCaseArtifacts:
    report_dir: Path
    logs_dir: Path
    dumps_dir: Path
    screenshots_dir: Path
    script_log_file: Path
    app_logcat_file: Path


def prepare_uia_case_artifacts(report_dir: str | Path, case_name: str) -> UiaCaseArtifacts:
    root = Path(report_dir)
    logs_dir = root / "logs"
    dumps_dir = root / "ui-dumps" / case_name
    screenshots_dir = root / "screenshots" / case_name

    logs_dir.mkdir(parents=True, exist_ok=True)
    dumps_dir.mkdir(parents=True, exist_ok=True)
    screenshots_dir.mkdir(parents=True, exist_ok=True)

    return UiaCaseArtifacts(
        report_dir=root,
        logs_dir=logs_dir,
        dumps_dir=dumps_dir,
        screenshots_dir=screenshots_dir,
        script_log_file=logs_dir / f"{case_name}.log",
        app_logcat_file=logs_dir / f"{case_name}.app.logcat.txt",
    )
