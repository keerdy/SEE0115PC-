from __future__ import annotations

from apptest.core.artifacts import make_run_dir


def test_make_run_dir_is_unique_for_immediate_calls(tmp_path) -> None:
    first = make_run_dir(tmp_path, "case3")
    second = make_run_dir(tmp_path, "case3")
    assert first != second
    assert first.is_dir()
    assert second.is_dir()


def test_make_run_dir_sanitizes_unsafe_report_name(tmp_path) -> None:
    run_dir = make_run_dir(tmp_path, "../../outside\\bad:name")
    assert run_dir.parent == tmp_path.resolve()
    assert ".." not in run_dir.name
    assert ":" not in run_dir.name
    assert "\\" not in run_dir.name
