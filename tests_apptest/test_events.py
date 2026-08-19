from __future__ import annotations

from apptest.core.events import (
    CASE_EVENT,
    CASE_FINISHED,
    CASE_STARTED,
    DOWNLOAD_PROGRESS,
    FAILURE,
    ITERATION_PROGRESS,
    METRIC,
    PRESSURE_FINISHED,
    PRESSURE_STARTED,
    PROTOCOL_CASE_COMPLETED,
)


def test_event_constants_match_previous_magic_strings() -> None:
    assert CASE_STARTED == "case_started"
    assert CASE_FINISHED == "case_finished"
    assert METRIC == "metric"
    assert CASE_EVENT == "case_event"
    assert FAILURE == "failure"
    assert ITERATION_PROGRESS == "iteration_progress"
    assert PROTOCOL_CASE_COMPLETED == "protocol_case_completed"
    assert PRESSURE_STARTED == "pressure_started"
    assert PRESSURE_FINISHED == "pressure_finished"
    assert DOWNLOAD_PROGRESS == "download_progress"


def test_event_constants_are_all_unique() -> None:
    constants = [
        CASE_STARTED,
        CASE_FINISHED,
        METRIC,
        CASE_EVENT,
        FAILURE,
        ITERATION_PROGRESS,
        PROTOCOL_CASE_COMPLETED,
        PRESSURE_STARTED,
        PRESSURE_FINISHED,
        DOWNLOAD_PROGRESS,
    ]
    assert len(constants) == len(set(constants))
