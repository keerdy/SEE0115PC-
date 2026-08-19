from __future__ import annotations

from pathlib import Path


def ensure_parent(path: str | Path) -> Path:
    output = Path(path)
    output.parent.mkdir(parents=True, exist_ok=True)
    return output


def validate_non_empty_file(path: str | Path) -> None:
    file_path = Path(path)
    if not file_path.exists():
        raise AssertionError(f"File does not exist: {file_path}")
    if file_path.stat().st_size <= 0:
        raise AssertionError(f"File is empty: {file_path}")
