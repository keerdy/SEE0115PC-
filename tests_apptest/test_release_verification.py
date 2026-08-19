from pathlib import Path

from packaging.verify_release import find_release_violations


def test_release_verification_rejects_stale_protocol_code(tmp_path: Path) -> None:
    internal = tmp_path / "_internal"
    client = internal / "apptest" / "clients"
    client.mkdir(parents=True)
    (client / "p2p_client.py").write_text(
        "def control_device_action(): pass\n", encoding="utf-8"
    )

    violations = find_release_violations(internal)

    assert any("device/control" in violation for violation in violations)
    assert any("open_album_download" in violation for violation in violations)


def test_release_verification_accepts_current_protocol_markers(tmp_path: Path) -> None:
    internal = tmp_path / "_internal"
    client = internal / "apptest" / "clients"
    services = internal / "apptest" / "services"
    client.mkdir(parents=True)
    services.mkdir(parents=True)
    (client / "p2p_client.py").write_text(
        "open_album_download open_video_stream\n", encoding="utf-8"
    )
    (services / "protocol_scenarios.py").write_text(
        "case5_protected_request_verified\n", encoding="utf-8"
    )

    assert find_release_violations(internal) == []
