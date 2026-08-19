from __future__ import annotations

from pathlib import Path

from apptest.core.android_logcat import AndroidAppLogcatCapture


def test_android_logcat_builds_serialized_adb_command(tmp_path: Path) -> None:
    capture = AndroidAppLogcatCapture(
        serial="ce1705bc",
        package_name="com.sx.pocket.cameraapp",
        output_file=tmp_path / "case1.app.logcat.txt",
    )

    cmd = capture._build_adb_command("logcat", "--pid", "1234", "-v", "threadtime")
    assert cmd == ["adb", "-s", "ce1705bc", "logcat", "--pid", "1234", "-v", "threadtime"]
