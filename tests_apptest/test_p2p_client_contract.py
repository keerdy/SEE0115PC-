from pathlib import Path


CLIENT_SOURCE = Path(__file__).parents[1] / "apptest" / "clients" / "p2p_client.py"


def test_removed_device_control_route_is_not_exposed() -> None:
    source = CLIENT_SOURCE.read_text(encoding="utf-8")

    assert "def control_device_action" not in source
    assert "/api/v1/device/control" not in source
