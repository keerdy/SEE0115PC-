"""Status schema helpers shared by monitoring, recording, and reports."""

from __future__ import annotations

import re
from typing import Any, Dict


TERMINAL_STATES = frozenset({"finished", "failed", "stopped", "error"})


def unwrap_status(event: Dict[str, Any]) -> Dict[str, Any]:
    status = event.get("status") if event.get("cmd") == "case_status_event" else event
    return status if isinstance(status, dict) else {}


def is_terminal_status(
    event: Dict[str, Any], case_id: int, suite: str = "stable_test",
) -> bool:
    status = unwrap_status(event)
    return (
        status.get("last_case_id") == case_id
        and status.get("last_suite") == suite
        and status.get("status") in TERMINAL_STATES
    )


def extract_progress(event: Dict[str, Any]) -> tuple[int, int]:
    status = unwrap_status(event)
    current = status.get("current")
    total = status.get("total")
    if (
        isinstance(current, int)
        and isinstance(total, int)
        and current >= 0
        and total >= 0
        and (total == 0 or current <= total)
    ):
        return current, total

    message = str(status.get("last_msg", status.get("msg", "")))
    match = re.search(r"(\d+)\s*/\s*(\d+)", message)
    if match:
        parsed_current = int(match.group(1))
        parsed_total = int(match.group(2))
        if parsed_total > 0 and parsed_current <= parsed_total:
            return parsed_current, parsed_total
    return 0, 0
