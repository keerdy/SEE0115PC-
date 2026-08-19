from __future__ import annotations

from dataclasses import dataclass

from apptest.clients.p2p_client import P2PClient


@dataclass
class _FakeResponse:
    chunks: list[bytes]
    status_code: int = 206

    def __enter__(self) -> "_FakeResponse":
        return self

    def __exit__(self, *args) -> None:
        return None

    def raise_for_status(self) -> None:
        return None

    def iter_bytes(self, chunk_size: int):
        assert chunk_size > 0
        yield from self.chunks


class _FakeStreamClient:
    def __init__(self, response: _FakeResponse) -> None:
        self.response = response
        self.calls: list[dict] = []

    def stream(self, method: str, url: str, **kwargs):
        self.calls.append({"method": method, "url": url, **kwargs})
        return self.response


class _FakeLogger:
    def info(self, *args, **kwargs) -> None:
        return None


def _client(response: _FakeResponse) -> tuple[P2PClient, _FakeStreamClient]:
    client = P2PClient.__new__(P2PClient)
    client.base_url = "http://device"
    client.timeout_seconds = 30
    client.sse_read_timeout_seconds = 60
    client.logger = _FakeLogger()
    stream_client = _FakeStreamClient(response)
    client.client = stream_client
    return client, stream_client


def test_album_download_is_streamed_to_atomic_output_with_range(tmp_path) -> None:
    client, stream_client = _client(_FakeResponse([b"first", b"second"]))
    output = tmp_path / "recording.mp4"

    written = client.download_album_file_to(
        "session-token",
        "/sdcard/recording.mp4",
        output,
        range_header="bytes=0-10",
        chunk_size=3,
    )

    assert written == 11
    assert output.read_bytes() == b"firstsecond"
    assert not output.with_name(output.name + ".part").exists()
    assert stream_client.calls[0]["headers"] == {
        "Authorization": "Bearer session-token",
        "Range": "bytes=0-10",
    }


def test_video_stream_uses_same_streaming_path(tmp_path) -> None:
    client, stream_client = _client(_FakeResponse([b"video"]))
    output = tmp_path / "video.mp4"

    assert client.download_video_file_to(
        "session-token", "/sdcard/video.mp4", output
    ) == 5
    assert output.read_bytes() == b"video"
    assert stream_client.calls[0]["url"].endswith("/api/v1/stream/live")


def test_album_range_does_not_accept_full_file_fallback(tmp_path) -> None:
    client, _ = _client(_FakeResponse([b"full"], status_code=200))

    try:
        client.download_album_file_to(
            "session-token",
            "/sdcard/video.mp4",
            tmp_path / "video.mp4",
            range_header="bytes=0-3",
        )
    except ValueError as exc:
        assert "expected HTTP 206" in str(exc)
    else:
        raise AssertionError("full-file fallback was accepted for a Range request")
