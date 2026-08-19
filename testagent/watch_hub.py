"""One shared case-status watch connection per device in one PC process."""

from __future__ import annotations

import time as _time
from collections import deque
from contextlib import contextmanager
import queue
import socket
import threading
from dataclasses import dataclass
from typing import Any, Dict

from .device import request_case_status
from .protocol import TestAgentClient, TestAgentError, init_com_mta
from .app_logging import get_logger


_LOG = get_logger()


def _is_critical_event(event: Dict[str, Any]) -> bool:
    if event.get("cmd") == "event_alert":
        return True
    status = event.get("status") if event.get("cmd") == "case_status_event" else event
    if not isinstance(status, dict):
        return True
    if status.get("code") not in (None, 0):
        return True
    return status.get("status") in {"finished", "failed", "stopped", "error"}


class SubscriptionQueue:
    """Bounded queue that keeps terminal/error events when a consumer is slow."""

    def __init__(self, maxsize: int = 256):
        self._maxsize = maxsize
        self._items: deque[tuple[Any, ...]] = deque()
        self._condition = threading.Condition()

    def put(self, item: tuple[Any, ...]) -> None:
        critical = item[0] != "event" or _is_critical_event(item[2])
        with self._condition:
            if len(self._items) >= self._maxsize:
                removable = next(
                    (index for index, old in enumerate(self._items)
                     if old[0] == "event" and not _is_critical_event(old[2])),
                    None,
                )
                if removable is not None:
                    del self._items[removable]
                elif not critical:
                    return
                else:
                    self._items.popleft()
            self._items.append(item)
            self._condition.notify()

    def get_nowait(self) -> tuple[Any, ...]:
        with self._condition:
            if not self._items:
                raise queue.Empty
            return self._items.popleft()

    def get(self, timeout: float | None = None) -> tuple[Any, ...]:
        with self._condition:
            if not self._items:
                if not self._condition.wait(timeout):
                    raise queue.Empty
            if not self._items:
                raise queue.Empty
            return self._items.popleft()


@dataclass
class WatchSubscription:
    _hub: "WatchHub"
    _id: int
    key: str
    queue: SubscriptionQueue

    def close(self) -> None:
        self._hub.unsubscribe(self)

    def snapshot_status(self) -> Dict[str, Any]:
        return self._hub.snapshot_status()


class WatchHub:
    def __init__(self, host: str, port: int, source_host: str, source_if_index: int | None = None):
        self.host = host
        self.port = port
        self.source_host = source_host
        self.source_if_index = source_if_index
        self._lock = threading.Lock()
        self._subscriptions: dict[int, tuple[WatchSubscription, int]] = {}
        self._next_id = 1
        self._thread: threading.Thread | None = None
        self._stop_event = threading.Event()
        self._reconfigure_event = threading.Event()
        self._client: TestAgentClient | None = None
        self._last_status: Dict[str, Any] = {}
        self._suspend_count = 0
        self._suspend_event = threading.Event()

    def subscribe(self, key: str, interval_ms: int) -> WatchSubscription:
        interval_ms = max(200, min(interval_ms, 5000))
        lock_start = _time.monotonic()
        _LOG.info(
            "watch_hub_before_lock key=%s host=%s source_ip=%s thread=%s",
            key, self.host, self.source_host, threading.current_thread().name,
        )
        with self._lock:
            _LOG.info(
                "watch_hub_after_lock key=%s host=%s source_ip=%s elapsed=%.3fs",
                key, self.host, self.source_host, _time.monotonic() - lock_start,
            )
            before = self._interval_locked()
            subscription = WatchSubscription(self, self._next_id, key, SubscriptionQueue())
            self._next_id += 1
            self._subscriptions[subscription._id] = (subscription, interval_ms)
            after = self._interval_locked()
            if self._thread is None or not self._thread.is_alive():
                self._stop_event.clear()
                self._thread = threading.Thread(target=self._run, daemon=True)
                self._thread.start()
                _LOG.info("watch_hub_started host=%s source_ip=%s key=%s",
                          self.host, self.source_host, key)
            elif before != after:
                self._request_reconnect_locked()
            return subscription

    def unsubscribe(self, subscription: WatchSubscription) -> None:
        remove_hub = False
        with self._lock:
            entry = self._subscriptions.pop(subscription._id, None)
            if entry is None:
                return
            before = self._interval_locked(entry[1])
            after = self._interval_locked()
            subscription.queue.put(("finished", subscription.key))
            if not self._subscriptions:
                self._stop_event.set()
                self._close_client_locked()
                remove_hub = True
            elif before != after:
                self._request_reconnect_locked()
        if remove_hub:
            _LOG.info("watch_hub_stopped host=%s source_ip=%s", self.host, self.source_host)
            _remove_hub(self)

    def snapshot_status(self) -> Dict[str, Any]:
        with self._lock:
            return dict(self._last_status)

    def _interval_locked(self, removed_interval: int | None = None) -> int:
        intervals = [interval for _, interval in self._subscriptions.values()]
        if removed_interval is not None:
            intervals.append(removed_interval)
        return min(intervals) if intervals else 1000

    def _request_reconnect_locked(self) -> None:
        self._reconfigure_event.set()
        self._close_client_locked()

    def _close_client_locked(self) -> None:
        if self._client is not None:
            self._client.close()
            self._client = None

    def suspend(self) -> None:
        with self._lock:
            self._suspend_count += 1
            self._suspend_event.set()
            self._reconfigure_event.set()
            self._close_client_locked()

    def resume(self) -> None:
        with self._lock:
            self._suspend_count = max(0, self._suspend_count - 1)
            if self._suspend_count == 0:
                self._suspend_event.clear()
                self._reconfigure_event.clear()

    def _broadcast(self, kind: str, payload: Any) -> None:
        with self._lock:
            subscribers = list(self._subscriptions.values())
        for subscription, _ in subscribers:
            if kind == "event":
                subscription.queue.put(("event", subscription.key, payload))
            elif kind == "error":
                subscription.queue.put(("error", subscription.key, payload))

    def _watch_interval(self) -> int:
        with self._lock:
            return self._interval_locked()

    def _set_last_status(self, event: Dict[str, Any]) -> None:
        status = event.get("status") if event.get("cmd") == "case_status_event" else event
        if isinstance(status, dict):
            with self._lock:
                self._last_status = dict(status)

    @staticmethod
    def _sleep_until(stop_event: threading.Event, seconds: float) -> None:
        """Sleep without COM re-entrancy.

        threading.Event.wait() → Condition → Semaphore → WaitForSingleObject
        is intercepted by COM STA on Windows, causing 0x8001010d when multiple
        threads sleep simultaneously.  time.sleep() uses kernel32 Sleep() which
        does not pump COM messages.
        """
        deadline = _time.monotonic() + seconds
        while _time.monotonic() < deadline:
            if stop_event.is_set():
                return
            _time.sleep(min(0.1, deadline - _time.monotonic()))

    def _run(self) -> None:
        init_com_mta()
        error_reported = False
        retry_delay = 1.0
        poll_cycle = 0
        while not self._stop_event.is_set():
            if self._suspend_event.is_set():
                self._sleep_until(self._stop_event, 0.2)
                continue
            poll_cycle += 1
            loop_start = _time.monotonic()
            try:
                # 短请求轮询设备状态（受 _request_lock 保护，与探活串行）。
                # 不再使用长连接持续 recv——多设备并行长连接 recv 会触发
                # Windows Winsock 堆损坏 (0xc0000374)，跑 20 分钟必崩。
                _LOG.info(
                    "watch_request_before host=%s source_ip=%s cycle=%s thread=%s",
                    self.host, self.source_host, poll_cycle, threading.current_thread().name,
                )
                reply = request_case_status(
                    self.host, self.source_host, port=self.port, timeout=3.0,
                    source_if_index=self.source_if_index,
                )
                _LOG.info(
                    "watch_request_after host=%s source_ip=%s cycle=%s code=%s elapsed=%.3fs",
                    self.host, self.source_host, poll_cycle, reply.get("code"),
                    _time.monotonic() - loop_start,
                )
                if reply.get("code") == -10:
                    # Keep the latest confirmed status while the device UI bridge recovers.
                    error_reported = False
                    retry_delay = 1.0
                elif reply.get("code") != 0:
                    raise TestAgentError(str(reply.get("msg", "get_case_status failed")))
                else:
                    reply["_connection_mode"] = "poll"
                    self._set_last_status(reply)
                    self._broadcast("event", reply)
                    if error_reported:
                        error_reported = False
                        retry_delay = 1.0
            except Exception as exc:
                _LOG.exception("watch_loop_error host=%s source_ip=%s", self.host, self.source_host)
                if self._reconfigure_event.is_set():
                    self._reconfigure_event.clear()
                    continue
                if not error_reported and not self._stop_event.is_set():
                    self._broadcast("error", f"{exc}; 正在自动重连")
                    error_reported = True
                self._sleep_until(self._stop_event, retry_delay)
                retry_delay = min(30.0, retry_delay * 2.0)
            # 等待下次轮询（订阅者设定的 interval_ms）
            interval = max(1.0, self._watch_interval() / 1000.0)
            elapsed = _time.monotonic() - loop_start
            if poll_cycle % 30 == 0:
                late = max(0.0, elapsed - interval - 0.5)
                if late > 0.1:
                    _LOG.warning("watch_loop_late host=%s source_ip=%s cycle=%s elapsed=%.1fs interval=%.1fs",
                                 self.host, self.source_host, poll_cycle, elapsed, interval)
            self._sleep_until(self._stop_event, interval)


_HUBS: dict[tuple[str, int, str, int | None], WatchHub] = {}
_HUBS_LOCK = threading.Lock()


def acquire_watch_subscription(
    key: str, host: str, port: int, source_host: str, interval_ms: int,
    source_if_index: int | None = None,
) -> WatchSubscription:
    hub_key = (host, port, source_host, source_if_index)
    start = _time.monotonic()
    _LOG.info(
        "watch_subscription_before_hubs_lock key=%s host=%s source_ip=%s thread=%s",
        key, host, source_host, threading.current_thread().name,
    )
    with _HUBS_LOCK:
        hub = _HUBS.get(hub_key)
        hub_existing = hub is not None
        if hub is None:
            hub = WatchHub(host, port, source_host, source_if_index)
            _HUBS[hub_key] = hub
        _LOG.info(
            "watch_subscription_after_hubs_lock key=%s hub_existing=%s elapsed=%.3fs",
            key, hub_existing, _time.monotonic() - start,
        )
    _LOG.info("watch_subscription_before_hub_subscribe key=%s", key)
    subscription = hub.subscribe(key, interval_ms)
    _LOG.info(
        "watch_subscription_after_hub_subscribe key=%s elapsed=%.3fs",
        key, _time.monotonic() - start,
    )
    return subscription


@contextmanager
def suspend_watch_hub(
    host: str, port: int, source_host: str, source_if_index: int | None = None,
):
    hub_key = (host, port, source_host, source_if_index)
    with _HUBS_LOCK:
        hub = _HUBS.get(hub_key)
    if hub is None:
        yield
        return
    hub.suspend()
    try:
        yield
    finally:
        hub.resume()


def suspend_all_watch_hubs() -> None:
    """Pause polling on every hub, used around long FTP transfers (OTA, SD clean).

    While a transfer holds the global request lock, watch threads would otherwise
    block on the lock, and that blocking wait (threading.Lock in alertable wait)
    can trigger COM re-entrancy (0x8001010d) on Windows.  Suspending first avoids
    the contention entirely.
    """
    with _HUBS_LOCK:
        hubs = list(_HUBS.values())
    for hub in hubs:
        hub.suspend()


def resume_all_watch_hubs() -> None:
    with _HUBS_LOCK:
        hubs = list(_HUBS.values())
    for hub in hubs:
        hub.resume()


def _remove_hub(hub: WatchHub) -> None:
    hub_key = (hub.host, hub.port, hub.source_host, hub.source_if_index)
    with _HUBS_LOCK:
        if _HUBS.get(hub_key) is hub:
            _HUBS.pop(hub_key, None)


def purge_stale_hubs() -> int:
    """Remove hubs that have no active subscriptions and stopped threads.

    Called periodically by the GUI to prevent thread accumulation over
    long-running sessions.  Returns the number of purged hubs.
    """
    purged = 0
    start = _time.monotonic()
    _LOG.info("purge_stale_hubs_before_lock thread=%s", threading.current_thread().name)
    with _HUBS_LOCK:
        stale = [
            (key, hub) for key, hub in list(_HUBS.items())
            if not hub._subscriptions
        ]
        for key, hub in stale:
            if not hub._stop_event.is_set():
                hub._stop_event.set()
            hub._close_client_locked()
            removed = _HUBS.pop(key, None)
            if removed is not None:
                _LOG.info("purge_stale_hub host=%s port=%s", key[0], key[1])
                purged += 1
    _LOG.info("purge_stale_hubs_after_lock purged=%s elapsed=%.3fs", purged, _time.monotonic() - start)
    return purged
