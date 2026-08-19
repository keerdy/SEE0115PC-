from __future__ import annotations

from apptest.clients.p2p_client import P2PClient


def test_sse_parser_preserves_id_retry_and_multiline_data() -> None:
    event = P2PClient._parse_sse_chunk(
        [
            "id: 42",
            "retry: 1500",
            "event: device_recording_state_changed",
            'data: {"recording":',
            'data: 1}',
        ]
    )

    assert event is not None
    assert event.event == "device_recording_state_changed"
    assert event.event_id == "42"
    assert event.retry_ms == 1500
    assert event.data == {"recording": 1}


def test_sse_timeout_for_auth_wait_is_independent_from_http_timeout() -> None:
    client = P2PClient("http://device", timeout_seconds=30)

    timeout = client._sse_timeout(60)

    assert timeout.read == 60
    assert timeout.connect == 30
    client.close()
