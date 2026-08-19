from __future__ import annotations

from datetime import datetime
from pathlib import Path
import re


_INVALID_NAME = re.compile(r'[<>:"/\\|?*\x00-\x1f]+')


def _safe_report_name(value: str | None) -> str:
    text = _INVALID_NAME.sub("_", (value or "").strip())
    text = text.replace("..", "_").strip(" ._")
    return text[:80]


def make_run_dir(root: str | Path, report_name: str | None = None) -> Path:
    timestamp = datetime.now().strftime("%Y%m%d_%H%M%S_%f")
    safe_name = _safe_report_name(report_name)
    suffix = f"_{safe_name}" if safe_name else ""
    root_path = Path(root).resolve()
    root_path.mkdir(parents=True, exist_ok=True)
    run_dir = root_path / f"{timestamp}{suffix}"
    collision = 0
    while True:
        candidate = run_dir if collision == 0 else root_path / f"{run_dir.name}_{collision}"
        try:
            candidate.mkdir(parents=False, exist_ok=False)
            run_dir = candidate
            break
        except FileExistsError:
            collision += 1
    return run_dir
