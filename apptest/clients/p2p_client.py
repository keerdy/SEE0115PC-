from __future__ import annotations

import json
from contextlib import contextmanager
from pathlib import Path
import time
from dataclasses import dataclass
from typing import Any, Iterator

import httpx

from apptest.core.hash_utils import sha256_text
from apptest.core.logging_utils import get_logger


@dataclass
class SseEvent:
    event: str
    data: dict[str, Any]
    raw: str
    event_id: str | None = None
    retry_ms: int | None = None


class P2PClient:
    def __init__(self, base_url: str, timeout_seconds: int = 30) -> None:
        self.base_url = base_url.rstrip("/")
        self.timeout_seconds = timeout_seconds
        # SSE is long-lived. Keep ordinary request timeout semantics while
        # giving the authorization stream enough read time for its configured
        # confirmation window.
        self.sse_read_timeout_seconds = max(float(timeout_seconds), 60.0)
        self.client = httpx.Client(timeout=timeout_seconds)
        self.logger = get_logger("pocket_app_automation.p2p")

    def close(self) -> None:
        self.client.close()

    def _auth_headers(self, token: str) -> dict[str, str]:
        return {"Authorization": f"Bearer {token}"}

    def get_challenge(self) -> httpx.Response:
        url = f"{self.base_url}/api/v1/device/challenge"
        self.logger.info("GET challenge url=%s", url)
        response = self.client.get(url)
        self.logger.info("challenge response status=%s", response.status_code)
        return response

    def post_auth(self, auth_session_id: str, device_id: str, challenge_code: str, default_secret_key: str, app_version: str) -> httpx.Response:
        signature = sha256_text(f"{device_id}{challenge_code}{default_secret_key}")
        payload = {
            "device_id": device_id,
            "auth_session_id": auth_session_id,
            "signature": signature,
            "app_version": app_version,
        }
        url = f"{self.base_url}/api/v1/device/auth"
        self.logger.info("POST auth url=%s auth_session_id=%s device_id=%s", url, auth_session_id, device_id)
        response = self.client.post(url, json=payload)
        self.logger.info("auth response status=%s", response.status_code)
        return response

    def cancel_auth(self, auth_session_id: str, device_id: str) -> httpx.Response:
        payload = {"device_id": device_id, "auth_session_id": auth_session_id}
        url = f"{self.base_url}/api/v1/device/auth/cancel"
        self.logger.info("POST auth cancel url=%s auth_session_id=%s", url, auth_session_id)
        response = self.client.post(url, json=payload)
        self.logger.info("auth cancel response status=%s", response.status_code)
        return response

    def heartbeat(self, token: str) -> httpx.Response:
        url = f"{self.base_url}/api/v1/device/heartbeat"
        self.logger.info("GET heartbeat url=%s", url)
        response = self.client.get(url, headers=self._auth_headers(token))
        self.logger.info("heartbeat response status=%s", response.status_code)
        return response

    def get_device_status(self, token: str) -> httpx.Response:
        url = f"{self.base_url}/api/v1/device/status"
        self.logger.info("GET device status url=%s", url)
        response = self.client.get(url, headers=self._auth_headers(token))
        self.logger.info("device status response status=%s", response.status_code)
        return response

    def get_recording_state(self, token: str) -> httpx.Response:
        url = f"{self.base_url}/api/v1/device/recording/state"
        self.logger.info("GET recording state url=%s", url)
        response = self.client.get(url, headers=self._auth_headers(token))
        self.logger.info("recording state response status=%s", response.status_code)
        return response

    def get_album_files(self, token: str, page: int = 1, size: int = 20, media_type: str = "video") -> httpx.Response:
        params = {"page": page, "size": size, "type": media_type}
        url = f"{self.base_url}/api/v1/album/files"
        self.logger.info("GET album files url=%s page=%s size=%s type=%s", url, page, size, media_type)
        response = self.client.get(url, params=params, headers=self._auth_headers(token))
        self.logger.info("album files response status=%s", response.status_code)
        return response

    def delete_album_files(self, token: str, file_paths: list[str]) -> httpx.Response:
        url = f"{self.base_url}/api/v1/album/delete"
        self.logger.info("POST album delete url=%s file_paths=%s", url, file_paths)
        response = self.client.post(
            url,
            json={"file_paths": file_paths},
            headers=self._auth_headers(token),
        )
        self.logger.info("album delete response status=%s", response.status_code)
        return response

    def download_album_file(self, token: str, file_path: str) -> httpx.Response:
        url = f"{self.base_url}/api/v1/album/download"
        self.logger.info("GET album download url=%s file_path=%s", url, file_path)
        response = self.client.get(
            url,
            params={"file_path": file_path},
            headers=self._auth_headers(token),
        )
        self.logger.info("album download response status=%s", response.status_code)
        return response

    @contextmanager
    def open_album_download(
        self,
        token: str,
        file_path: str,
        range_header: str | None = None,
    ) -> Iterator[httpx.Response]:
        """Open an authenticated album download without buffering its body."""
        headers = self._auth_headers(token)
        if range_header:
            headers["Range"] = range_header
        url = f"{self.base_url}/api/v1/album/download"
        self.logger.info(
            "GET album download stream url=%s file_path=%s range=%s",
            url,
            file_path,
            range_header or "",
        )
        with self.client.stream(
            "GET",
            url,
            params={"file_path": file_path},
            headers=headers,
            timeout=self._sse_timeout(self.timeout_seconds),
        ) as response:
            self.logger.info("album download stream response status=%s", response.status_code)
            yield response

    @contextmanager
    def open_video_stream(
        self,
        token: str,
        file_path: str,
        range_header: str | None = None,
    ) -> Iterator[httpx.Response]:
        """Open a Range-capable MP4 stream without loading it into memory."""
        headers = self._auth_headers(token)
        if range_header:
            headers["Range"] = range_header
        url = f"{self.base_url}/api/v1/stream/live"
        self.logger.info(
            "GET video stream url=%s file_path=%s range=%s",
            url,
            file_path,
            range_header or "",
        )
        with self.client.stream(
            "GET",
            url,
            params={"file_path": file_path},
            headers=headers,
            timeout=self._sse_timeout(self.timeout_seconds),
        ) as response:
            self.logger.info("video stream response status=%s", response.status_code)
            yield response

    @staticmethod
    def _write_stream_to_file(
        response: httpx.Response,
        output_path: str | Path,
        chunk_size: int = 1024 * 1024,
    ) -> int:
        if chunk_size <= 0:
            raise ValueError("chunk_size must be > 0")
        destination = Path(output_path)
        destination.parent.mkdir(parents=True, exist_ok=True)
        partial = destination.with_name(destination.name + ".part")
        bytes_written = 0
        try:
            response.raise_for_status()
            with partial.open("wb") as handle:
                for chunk in response.iter_bytes(chunk_size=chunk_size):
                    if not chunk:
                        continue
                    handle.write(chunk)
                    bytes_written += len(chunk)
            partial.replace(destination)
            return bytes_written
        except Exception:
            partial.unlink(missing_ok=True)
            raise

    def download_album_file_to(
        self,
        token: str,
        file_path: str,
        output_path: str | Path,
        range_header: str | None = None,
        chunk_size: int = 1024 * 1024,
    ) -> int:
        with self.open_album_download(token, file_path, range_header) as response:
            if range_header and response.status_code != 206:
                raise ValueError(
                    "album download Range request expected HTTP 206, "
                    f"got {response.status_code}"
                )
            return self._write_stream_to_file(response, output_path, chunk_size)

    def download_video_file_to(
        self,
        token: str,
        file_path: str,
        output_path: str | Path,
        range_header: str | None = None,
        chunk_size: int = 1024 * 1024,
    ) -> int:
        with self.open_video_stream(token, file_path, range_header) as response:
            if range_header and response.status_code != 206:
                raise ValueError(
                    "video stream Range request expected HTTP 206, "
                    f"got {response.status_code}"
                )
            return self._write_stream_to_file(response, output_path, chunk_size)

    def stream_video_file(self, token: str, file_path: str, range_header: str | None = None) -> httpx.Response:
        headers = self._auth_headers(token)
        if range_header:
            headers["Range"] = range_header
        url = f"{self.base_url}/api/v1/stream/live"
        self.logger.info("GET stream live url=%s file_path=%s range=%s", url, file_path, range_header or "")
        response = self.client.get(
            url,
            params={"file_path": file_path},
            headers=headers,
        )
        self.logger.info("stream live response status=%s", response.status_code)
        return response

    def _sse_timeout(self, read_timeout_seconds: float | None = None) -> httpx.Timeout:
        connect_timeout = max(float(self.timeout_seconds), 1.0)
        read_timeout = max(
            float(read_timeout_seconds or self.sse_read_timeout_seconds), 1.0
        )
        return httpx.Timeout(
            connect=connect_timeout,
            read=read_timeout,
            write=connect_timeout,
            pool=connect_timeout,
        )

    def open_auth_sse(
        self, auth_session_id: str, timeout_seconds: int | None = None
    ) -> Iterator[SseEvent]:
        with self.client.stream(
            "GET",
            f"{self.base_url}/api/v1/events",
            params={"auth_session_id": auth_session_id},
            timeout=self._sse_timeout(timeout_seconds),
        ) as response:
            response.raise_for_status()
            yield from self._iter_sse(response)

    def open_device_events(
        self, token: str, max_reconnect_attempts: int = 3
    ) -> Iterator[SseEvent]:
        """Consume business events and reconnect after a dropped SSE stream.

        A new connection receives the device's current snapshot. ``Last-Event-ID``
        is only an SSE transport cursor; it is never exposed as a business
        request ID. Power-transition events terminate the iterator so callers
        can stop heartbeat and other device requests before reboot/shutdown.
        """
        if max_reconnect_attempts < 0:
            raise ValueError("max_reconnect_attempts must be >= 0")

        last_event_id: str | None = None
        retry_delay_seconds = 3.0
        reconnect_attempts = 0
        last_error: Exception | None = None

        while True:
            headers = self._auth_headers(token)
            if last_event_id:
                headers["Last-Event-ID"] = last_event_id

            try:
                with self.client.stream(
                    "GET",
                    f"{self.base_url}/api/v1/device/events",
                    headers=headers,
                    timeout=self._sse_timeout(),
                ) as response:
                    response.raise_for_status()
                    last_error = None
                    for event in self._iter_sse(response):
                        if event.event_id:
                            last_event_id = event.event_id
                        if event.retry_ms is not None:
                            retry_delay_seconds = min(
                                max(event.retry_ms / 1000.0, 0.0), 60.0
                            )
                        if event.event == "device_power_state_changing":
                            self.logger.info(
                                "device power transition received; stop business SSE reconnect"
                            )
                            return
                        yield event
            except httpx.HTTPStatusError as exc:
                # A stale/invalid Bearer token needs a fresh auth flow, not a
                # reconnect loop that repeatedly hammers the device.
                if exc.response is not None and exc.response.status_code == 401:
                    raise
                raise
            except httpx.RequestError as exc:
                last_error = exc
                self.logger.warning(
                    "device SSE disconnected attempt=%s error=%s",
                    reconnect_attempts + 1,
                    exc,
                )

            reconnect_attempts += 1
            if reconnect_attempts > max_reconnect_attempts:
                if last_error is not None:
                    raise ConnectionError(
                        "device business SSE reconnect attempts exhausted"
                    ) from last_error
                raise ConnectionError(
                    "device business SSE closed and reconnect attempts exhausted"
                )
            time.sleep(retry_delay_seconds)

    def wait_for_auth_confirmation(self, auth_session_id: str, timeout_seconds: int) -> SseEvent:
        start = time.monotonic()
        deadline = start + max(float(timeout_seconds), 0.0)
        self.logger.info("waiting for auth confirmation auth_session_id=%s timeout=%s", auth_session_id, timeout_seconds)
        try:
            for event in self.open_auth_sse(auth_session_id, timeout_seconds=timeout_seconds):
                if event.event in {"auth_confirmed", "auth_rejected"}:
                    self.logger.info("received auth event event=%s data=%s", event.event, event.data)
                    return event
                if time.monotonic() >= deadline:
                    break
        except httpx.TimeoutException as exc:
            raise TimeoutError(
                f"Timed out waiting for auth confirmation for session {auth_session_id}"
            ) from exc
        raise TimeoutError(f"Timed out waiting for auth confirmation for session {auth_session_id}")

    def _iter_sse(self, response: httpx.Response) -> Iterator[SseEvent]:
        buffer: list[str] = []
        for line in response.iter_lines():
            if line == "":
                event = self._parse_sse_chunk(buffer)
                buffer = []
                if event:
                    yield event
                continue
            buffer.append(line)
            if len(buffer) > 1000:
                self.logger.error("sse buffer overflow lines=%s; discarding", len(buffer))
                buffer = []

    @staticmethod
    def _parse_sse_chunk(lines: list[str]) -> SseEvent | None:
        if not lines:
            return None
        event_name = ""
        event_id: str | None = None
        retry_ms: int | None = None
        data_lines: list[str] = []
        has_sse_field = False
        for line in lines:
            if line.startswith("event:"):
                has_sse_field = True
                event_name = line.split(":", 1)[1].strip()
            elif line.startswith("data:"):
                has_sse_field = True
                data_lines.append(line.split(":", 1)[1].lstrip())
            elif line.startswith("id:"):
                has_sse_field = True
                value = line.split(":", 1)[1].strip()
                if "\x00" not in value:
                    event_id = value
            elif line.startswith("retry:"):
                has_sse_field = True
                value = line.split(":", 1)[1].strip()
                try:
                    parsed_retry = int(value)
                except ValueError:
                    parsed_retry = -1
                if parsed_retry >= 0:
                    retry_ms = parsed_retry
        if not has_sse_field:
            return None
        data = "\n".join(data_lines)
        try:
            parsed = json.loads(data) if data else {}
        except json.JSONDecodeError:
            parsed = {}
        if not isinstance(parsed, dict):
            parsed = {}
        return SseEvent(
            event=event_name,
            data=parsed,
            raw="\n".join(lines),
            event_id=event_id,
            retry_ms=retry_ms,
        )
