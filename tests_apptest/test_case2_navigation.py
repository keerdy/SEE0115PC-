from __future__ import annotations

from apptest.mobile_uia.case2 import NEXT_ACTION, PREV_ACTION, _navigation_action


def test_case2_moves_right_to_end_then_left_to_start() -> None:
    actions = [_navigation_action(iteration, video_count=4) for iteration in range(1, 13)]
    assert actions == [
        NEXT_ACTION,
        NEXT_ACTION,
        NEXT_ACTION,
        PREV_ACTION,
        PREV_ACTION,
        PREV_ACTION,
        NEXT_ACTION,
        NEXT_ACTION,
        NEXT_ACTION,
        PREV_ACTION,
        PREV_ACTION,
        PREV_ACTION,
    ]
