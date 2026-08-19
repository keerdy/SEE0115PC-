from __future__ import annotations

from dataclasses import dataclass

import pytest

from apptest.clients.p2p_client import P2PClient


@dataclass
class _FakeResponse:
    lines: list[str]
    status_code: int = 200

    def __enter__(self) -> "_FakeResponse":
        return self

    def __exit__(self, *args) -> None:
        return None

    def raise_for_status(self) -> None:
        return None

    def iter_lines(self):
        yield from self.lines


class _FakeStreamClient:
    def __init__(self, responses: list[_FakeResponse]) -> None:
        self.responses = iter(responses)
        self.headers: list[dict[str, str]] = []

    def stream(self, method: str, url: str, *, headers, timeout):
        self.headers.append(headers)
        return next(self.responses)


class _FakeLogger:
    def info(self, *args, **kwargs) -> None:
        return None

    def warning(self, *args, **kwargs) -> None:
        return None


def test_business_sse_reconnects_with_last_event_id_and_stops_on_power_event(monkeypatch) -> None:
    client = P2PClient.__new__(P2PClient)
    client.base_url = "http://device"
    client.timeout_seconds = 30
    client.sse_read_timeout_seconds = 60
    client.logger = _FakeLogger()
    stream_client = _FakeStreamClient(
        [
            _FakeResponse(
                [
                    "id: 10",
                    "event: device_recording_state_changed",
                    'data: {"recording": 1}',
                    "",
                ]
            ),
            _FakeResponse(
                [
                    "id: 11",
                    "event: device_power_state_changing",
                    'data: {"action": "reboot"}',
                    "",
                ]
            ),
        ]
    )
    client.client = stream_client
    monkeypatch.setattr("apptest.clients.p2p_client.time.sleep", lambda _: None)

    events = list(client.open_device_events("session-token", max_reconnect_attempts=1))

    assert [event.event for event in events] == ["device_recording_state_changed"]
    assert stream_client.headers[0] == {"Authorization": "Bearer session-token"}
    assert stream_client.headers[1]["Last-Event-ID"] == "10"


def test_business_sse_reconnect_attempts_are_bounded(monkeypatch) -> None:
    client = P2PClient.__new__(P2PClient)
    client.base_url = "http://device"
    client.timeout_seconds = 30
    client.sse_read_timeout_seconds = 60
    client.logger = _FakeLogger()
    stream_client = _FakeStreamClient([_FakeResponse([]), _FakeResponse([])])
    client.client = stream_client
    monkeypatch.setattr("apptest.clients.p2p_client.time.sleep", lambda _: None)

    with pytest.raises(ConnectionError, match="reconnect attempts exhausted"):
        list(client.open_device_events("session-token", max_reconnect_attempts=1))

    assert len(stream_client.headers) == 2
