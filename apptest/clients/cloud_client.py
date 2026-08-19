from __future__ import annotations

from collections.abc import Callable
import time

import httpx

from apptest.core.logging_utils import get_logger


class CloudClient:
    def __init__(self, timeout_seconds: int = 30) -> None:
        self.client = httpx.Client(timeout=timeout_seconds, follow_redirects=True)
        self.logger = get_logger("pocket_app_automation.cloud")
        self.retry_attempts = 3
        self.retry_backoff_seconds = 1.0

    def close(self) -> None:
        self.client.close()

    def _should_retry_status(self, status_code: int) -> bool:
        return status_code in {502, 503, 504}

    def _sleep_before_retry(self, attempt: int) -> None:
        delay = self.retry_backoff_seconds * attempt
        self.logger.info("cloud retry sleeping attempt=%s delay_seconds=%s", attempt, delay)
        time.sleep(delay)

    def _request_with_retry(self, method: str, url: str, **kwargs) -> httpx.Response:
        last_error: Exception | None = None
        response: httpx.Response | None = None
        for attempt in range(1, self.retry_attempts + 1):
            try:
                response = self.client.request(method.upper(), url, **kwargs)
                if not self._should_retry_status(response.status_code) or attempt == self.retry_attempts:
                    return response
                self.logger.warning(
                    "cloud request got retryable status=%s method=%s url=%s attempt=%s/%s",
                    response.status_code,
                    method.upper(),
                    url,
                    attempt,
                    self.retry_attempts,
                )
            except httpx.RequestError as exc:
                last_error = exc
                self.logger.warning(
                    "cloud request failed method=%s url=%s attempt=%s/%s error=%s",
                    method.upper(),
                    url,
                    attempt,
                    self.retry_attempts,
                    exc,
                )
                if attempt == self.retry_attempts:
                    raise
            self._sleep_before_retry(attempt)

        if response is not None:
            return response
        assert last_error is not None
        raise last_error

    def get_current_app_package(self, url: str, method: str = "POST", auth_token: str = "") -> httpx.Response:
        headers = {}
        if auth_token:
            headers["Authorization"] = f"Bearer {auth_token}"
        self.logger.info("cloud request app package method=%s url=%s", method.upper(), url)
        response = self._request_with_retry(method.upper(), url, headers=headers)
        self.logger.info("cloud app package response status=%s", response.status_code)
        return response

    def check_firmware(self, url: str, device_sn: str, method: str = "GET", auth_token: str = "") -> httpx.Response:
        headers = {"X-Device-SN": device_sn}
        if auth_token:
            headers["Authorization"] = f"Bearer {auth_token}"
        self.logger.info("cloud request firmware check method=%s url=%s device_sn=%s", method.upper(), url, device_sn)
        response = self._request_with_retry(method.upper(), url, headers=headers)
        self.logger.info("cloud firmware check response status=%s", response.status_code)
        return response

    def download_file(
        self,
        url: str,
        auth_token: str = "",
        progress_callback: Callable[[int, int | None], None] | None = None,
    ) -> httpx.Response:
        headers = {}
        if auth_token:
            headers["Authorization"] = f"Bearer {auth_token}"
        self.logger.info("cloud download url=%s", url)
        if progress_callback is None:
            response = self._request_with_retry("GET", url, headers=headers)
            self.logger.info("cloud download response status=%s bytes=%s", response.status_code, len(response.content))
            return response

        response = None
        for attempt in range(1, self.retry_attempts + 1):
            try:
                request = self.client.build_request("GET", url, headers=headers)
                response = self.client.send(request, stream=True)
            except httpx.RequestError as exc:
                self.logger.warning(
                    "cloud streamed download failed url=%s attempt=%s/%s error=%s",
                    url, attempt, self.retry_attempts, exc,
                )
                response = None
                if attempt == self.retry_attempts:
                    raise
                self._sleep_before_retry(attempt)
                continue
            if not self._should_retry_status(response.status_code) or attempt == self.retry_attempts:
                break
            self.logger.warning(
                "cloud streamed download got retryable status=%s url=%s attempt=%s/%s",
                response.status_code,
                url,
                attempt,
                self.retry_attempts,
            )
            response.close()
            response = None
            self._sleep_before_retry(attempt)

        assert response is not None
        content = bytearray()
        try:
            total_bytes = response.headers.get("Content-Length")
            total = int(total_bytes) if total_bytes and total_bytes.isdigit() else None
            downloaded = 0
            for chunk in response.iter_bytes():
                if not chunk:
                    continue
                content.extend(chunk)
                downloaded += len(chunk)
                progress_callback(downloaded, total)
            wrapped = httpx.Response(
                status_code=response.status_code,
                headers=response.headers,
                content=bytes(content),
                request=response.request,
                history=response.history,
                extensions=response.extensions,
            )
            self.logger.info("cloud download response status=%s bytes=%s", wrapped.status_code, len(wrapped.content))
            return wrapped
        finally:
            response.close()

    def activate_device(self, url: str, sn: str, activation_code: str) -> httpx.Response:
        payload = {"sn": sn, "activation_code": activation_code}
        self.logger.info("cloud activate url=%s sn=%s", url, sn)
        response = self._request_with_retry("POST", url, json=payload)
        self.logger.info("cloud activate response status=%s", response.status_code)
        return response
