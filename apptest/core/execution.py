from __future__ import annotations

import logging
import threading
from contextlib import contextmanager
from contextvars import ContextVar
from datetime import datetime
from typing import Any, Callable, Iterator, Protocol


ProgressCallback = Callable[[str, dict[str, Any]], None]


class CancellationToken(Protocol):
    def is_set(self) -> bool: ...


class CaseCancelled(RuntimeError):
    """Raised when the caller requests cooperative cancellation."""


class _NeverCancelled:
    def is_set(self) -> bool:
        return False


_callback_var: ContextVar[ProgressCallback | None] = ContextVar("case_progress_callback", default=None)
_cancel_var: ContextVar[CancellationToken] = ContextVar("case_cancellation_token", default=_NeverCancelled())


@contextmanager
def case_execution_context(
    callback: ProgressCallback | None = None,
    cancellation_token: CancellationToken | None = None,
) -> Iterator[None]:
    callback_token = _callback_var.set(callback)
    cancel_token = _cancel_var.set(cancellation_token or _NeverCancelled())
    try:
        yield
    finally:
        _cancel_var.reset(cancel_token)
        _callback_var.reset(callback_token)


def emit_case_event(event_name: str, payload: dict[str, Any] | None = None) -> None:
    callback = _callback_var.get()
    if callback is None:
        return
    event_payload = {
        "timestamp": datetime.now().isoformat(),
        **(payload or {}),
    }
    try:
        callback(event_name, event_payload)
    except Exception:  # noqa: BLE001
        logging.getLogger("pocket_app_automation.execution").exception(
            "case progress callback failed event=%s", event_name
        )


def is_cancelled() -> bool:
    return bool(_cancel_var.get().is_set())


def raise_if_cancelled() -> None:
    if is_cancelled():
        raise CaseCancelled("测试已由调用方停止")


def cancellable_sleep(seconds: float, poll_interval: float = 0.2) -> None:
    remaining = max(0.0, float(seconds))
    while remaining > 0:
        raise_if_cancelled()
        wait_time = min(remaining, poll_interval)
        threading.Event().wait(wait_time)
        remaining -= wait_time

