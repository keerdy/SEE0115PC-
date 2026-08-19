from __future__ import annotations

import random

from apptest.mobile_uia.case7 import (
    GROUP_CONNECT,
    GROUP_HOME,
    _normalize_plan,
    _weighted_pick,
)


def test_normalize_plan_defaults_when_empty() -> None:
    plan = _normalize_plan(None)
    assert plan
    assert all(isinstance(item["percent"], int) for item in plan)


def test_normalize_plan_sanitizes_garbage() -> None:
    raw = [
        {"group": "首页", "action": "click", "percent": "abc"},
        {"group": None, "action": "explode", "percent": -5},
        {"group": "激活连接", "action": "connect", "percent": 10},
    ]
    plan = _normalize_plan(raw)
    assert plan[0]["percent"] == 0
    assert plan[1]["group"] == GROUP_HOME
    assert plan[1]["action"] == "click"
    assert plan[2]["group"] == GROUP_CONNECT
    assert plan[2]["action"] == "connect"


def test_weighted_pick_respects_percent_weights() -> None:
    random.seed(2026)
    plan = _normalize_plan(
        [
            {"group": "首页", "action": "click", "percent": 90},
            {"group": "激活连接", "action": "connect", "percent": 10},
        ]
    )
    picks = [_weighted_pick(plan)["group"] for _ in range(5000)]
    assert picks.count(GROUP_HOME) > picks.count(GROUP_CONNECT) * 5


def test_weighted_pick_handles_all_zero() -> None:
    plan = _normalize_plan([{"group": "首页", "action": "click", "percent": 0}])
    pick = _weighted_pick(plan)
    assert pick["group"] == GROUP_HOME
