from __future__ import annotations

import argparse
from pathlib import Path


FORBIDDEN_CODE_MARKERS = (
    "/api/v1/test/runtime-serial",
    "X-TestAgent-Token",
    "/api/v1/device/control",
)

FORBIDDEN_CONFIG_MARKERS = ("api.example.com", "seevison.cn")

REQUIRED_MARKERS = (
    "open_album_download",
    "open_video_stream",
    "case5_protected_request_verified",
)

TEXT_SUFFIXES = {".py", ".yaml", ".yml", ".json", ".txt", ".ini", ".cfg"}


def _text_files(root: Path):
    for path in root.rglob("*"):
        if path.is_file() and path.suffix.lower() in TEXT_SUFFIXES:
            yield path


def find_release_violations(release_dir: Path) -> list[str]:
    violations: list[str] = []
    if not release_dir.is_dir():
        return [f"release directory does not exist: {release_dir}"]

    contents: dict[Path, str] = {}
    for path in _text_files(release_dir):
        try:
            contents[path] = path.read_text(encoding="utf-8")
        except UnicodeDecodeError:
            violations.append(f"cannot decode release text file: {path}")

    for path, content in contents.items():
        markers = FORBIDDEN_CODE_MARKERS
        if path.suffix.lower() in {".yaml", ".yml", ".json", ".ini", ".cfg"}:
            markers += FORBIDDEN_CONFIG_MARKERS
        for marker in markers:
            if marker in content:
                violations.append(f"forbidden marker {marker!r} found in {path}")

    combined = "\n".join(contents.values())
    for marker in REQUIRED_MARKERS:
        if marker not in combined:
            violations.append(f"required marker {marker!r} missing from release")
    return violations


def main() -> int:
    parser = argparse.ArgumentParser(description="Verify a generated TestAgent release")
    parser.add_argument(
        "--release-dir",
        type=Path,
        default=Path(__file__).resolve().parents[1]
        / "dist"
        / "SXPocketTestAgent"
        / "_internal",
    )
    args = parser.parse_args()
    violations = find_release_violations(args.release_dir)
    if violations:
        for violation in violations:
            print(f"ERROR: {violation}")
        return 1
    print(f"Release verification passed: {args.release_dir}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
