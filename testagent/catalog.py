"""Dynamic test catalog with a local fallback for offline UI startup."""

from __future__ import annotations

from copy import deepcopy
from typing import Any, Dict, Iterable


def _case(case_id: int, title: str, risk: str = "UNKNOWN", *, selectable: bool = True,
          cancellable: bool = True) -> Dict[str, Any]:
    return {
        "case_id": case_id,
        "case_key": "",
        "title": title,
        "risk": risk,
        "implementation": "UNKNOWN",
        "selectable": selectable,
        "cancellable": cancellable,
    }


FALLBACK_CASES: Dict[str, list[Dict[str, Any]]] = {
    "stable_test": [
        _case(1, "USB 文件传输反复测试", "R3"),
        _case(2, "屏幕旋转反复测试", "R3"),
        _case(3, "拍照反复测试", "R3"),
        _case(4, "录像压力测试", "R4"),
        _case(5, "4-mode 云台快速运动压力测试", "R3"),
        _case(6, "电源键拍照与回放 x2000", "R3"),
        _case(7, "USB 网络相机反复测试", "R3"),
        _case(8, "24小时录像压力测试", "R3"),
        _case(9, "电源键连拍压力测试", "R4"),
        _case(10, "USB 环境恢复测试", "R3"),
        _case(11, "App Test", "R3"),
        _case(12, "自适应录像测试", "R4"),
        _case(13, "Aging 测试（可选时长）", "R3", cancellable=False),
        _case(14, "录像期间保持亮屏测试", "R4"),
    ],
    "bug_test": [
        _case(1, "控制中心滑动 x2000"),
        _case(2, "录像70分钟并旋转 x50", "R4"),
        _case(3, "录像10分钟并回放 x1000", "R4"),
        _case(4, "控制中心/旋转/翻转 x2000", "R3"),
        _case(5, "Not Implemented", selectable=False, cancellable=False),
        _case(6, "智能跟踪并旋转 x2000", "R3"),
        _case(7, "四边滑动 x2000", "R3"),
        _case(8, "变焦并长按云台 x1000", "R4"),
        _case(9, "录像与拍照模式切换 x2000", "R3"),
        _case(10, "拍照/录像/回放 x2000", "R4"),
        _case(11, "横屏拍照录像滑动测试", "R4"),
        _case(12, "自拍跟随/人脸丢失/三向摇杆 x2000", "R3"),
        _case(13, "录像并变焦 x500", "R4"),
        _case(14, "旋转/拍照/云台 x2000", "R4"),
        _case(15, "回放放大/云台 x2000", "R4"),
        _case(16, "Not Implemented", selectable=False, cancellable=False),
        _case(17, "锁定横屏并拍照 x2000", "R4"),
        _case(18, "Main UI 变焦滑块下滑 x2000", "R3"),
        _case(19, "摇杆/UI/云台 x2000", "R3"),
        _case(20, "Pro 连续自动对焦并随机点击 x2000", "R3"),
        _case(21, "控制中心/电源返回/点击 x5000", "R3"),
        _case(22, "恢复出厂并重启 x2000", "R4"),
        _case(23, "格式化并检查存储卡 x2000", "R4"),
        _case(24, "录像并多选 x2000", "R4"),
        _case(25, "拍照/录像/Pro/滤镜/回放 x2000", "R4"),
    ],
    "stress_test": [
        _case(1, "随机 UI 压力测试 x5000", "R4"),
    ],
    "custom_test": [
        _case(1, "自定义安全回归 Phase 1/2", "R3"),
    ],
}


def catalog_from_agent_info(info: Dict[str, Any]) -> Dict[str, list[Dict[str, Any]]]:
    raw_catalog = info.get("cases")
    if not isinstance(raw_catalog, dict):
        return deepcopy(FALLBACK_CASES)

    catalog: Dict[str, list[Dict[str, Any]]] = {}
    for suite, raw_cases in raw_catalog.items():
        if not isinstance(suite, str) or not isinstance(raw_cases, list):
            continue
        normalized = [_normalize_case(item) for item in raw_cases]
        catalog[suite] = sorted(
            (item for item in normalized if item is not None),
            key=lambda item: int(item["case_id"]),
        )
    return catalog or deepcopy(FALLBACK_CASES)


def _normalize_case(raw: Any) -> Dict[str, Any] | None:
    if not isinstance(raw, dict):
        return None
    case_id = raw.get("case_id")
    title = raw.get("title")
    if not isinstance(case_id, int) or case_id <= 0 or not isinstance(title, str):
        return None
    return {
        "case_id": case_id,
        "case_key": str(raw.get("case_key", "")),
        "title": title,
        "risk": str(raw.get("risk", "UNKNOWN")),
        "implementation": str(raw.get("implementation", "UNKNOWN")),
        "selectable": bool(raw.get("selectable", True)),
        "cancellable": bool(raw.get("cancellable", True)),
    }


def case_descriptor(
    catalog: Dict[str, list[Dict[str, Any]]], suite: str, case_id: int,
) -> Dict[str, Any] | None:
    return next(
        (item for item in catalog.get(suite, []) if item.get("case_id") == case_id),
        None,
    )


def case_titles(
    catalog: Dict[str, list[Dict[str, Any]]], suite: str,
) -> list[str]:
    return [f"{item['case_id']} - {item['title']}" for item in catalog.get(suite, [])]


def suite_names(catalog: Dict[str, list[Dict[str, Any]]]) -> Iterable[str]:
    return catalog.keys()
