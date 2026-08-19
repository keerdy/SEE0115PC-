from __future__ import annotations

import threading
from contextlib import contextmanager
from typing import Iterator


_CASE_EXECUTION_LOCK = threading.RLock()


@contextmanager
def serialized_case_execution() -> Iterator[None]:
    """Serialize runs that share logging, ADB, Pocket, and report resources."""
    with _CASE_EXECUTION_LOCK:
        yield
