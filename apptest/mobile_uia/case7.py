# -*- coding: utf-8 -*-
"""Case7 monkey 随机测试。

设计原则（控件大改后只需调整常量，不改执行逻辑）：
  - 底部 tab / 子页入口的文案匹配规则集中在 TAB_LABELS / SUBPAGE_ENTRY_LABELS
  - 危险控件黑名单集中在 DANGEROUS_PATTERNS
  - swipe 不再写死坐标，运行时从当前页 dump 动态找 scrollable 节点
  - _do_connect 连接流程保持原样（稳定，不在本次重构范围内）
"""
from __future__ import annotations

import html
import json
import random
import re
import subprocess
import time
import xml.etree.ElementTree as ET
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Iterable

import uiautomator2 as u2

from apptest.core.android_logcat import AndroidAppLogcatCapture
from apptest.core.config import AppConfig
from apptest.core.execution import CaseCancelled, cancellable_sleep, emit_case_event, raise_if_cancelled
from apptest.core.events import ITERATION_PROGRESS
from apptest.core.logging_utils import get_logger
from apptest.core.reporting import MetricsRecorder
from apptest.core.uia_artifacts import prepare_uia_case_artifacts
from apptest.mobile_uia.case1 import (
    _dump_ui,
    _dump_nodes,
    _parse_bounds,
    _record_ui_metric,
    _save_screenshot,
    UiNode,
)

logger = get_logger("pocket_app_automation.uia_case7")

# 页面分组常量（与 GUI 百分比表格中的 group 字段对应）
GROUP_HOME = "首页"
GROUP_ALBUM = "相册"
GROUP_SETTINGS = "设置"
GROUP_BEGINNER = "新手教程"
GROUP_CONNECT = "激活连接"

# 动作常量
ACTION_CLICK = "click"
ACTION_SWIPE = "swipe"
ACTION_BACK = "back"
ACTION_CONNECT = "connect"

DEFAULT_MONKEY_PLAN: list[dict] = [
    {"group": GROUP_HOME, "action": ACTION_CLICK, "percent": 30},
    {"group": GROUP_HOME, "action": ACTION_SWIPE, "percent": 5},
    {"group": GROUP_ALBUM, "action": ACTION_CLICK, "percent": 20},
    {"group": GROUP_ALBUM, "action": ACTION_SWIPE, "percent": 5},
    {"group": GROUP_SETTINGS, "action": ACTION_CLICK, "percent": 20},
    {"group": GROUP_SETTINGS, "action": ACTION_SWIPE, "percent": 5},
    {"group": GROUP_BEGINNER, "action": ACTION_CLICK, "percent": 5},
    {"group": GROUP_CONNECT, "action": ACTION_CONNECT, "percent": 10},
]

STATUS_BAR_CUTOFF_Y = 72
# SWIPE_AREA 仅供 _do_connect 流程内的 _swipe_device_card 使用；monkey 的随机 swipe
# 已改为运行时从当前页 dump 动态获取 scrollable 节点 bounds，不再依赖此常量。
SWIPE_AREA = (108, 945, 972, 1800)
# _do_connect 流程内的 tab 点击仍用固定坐标（连接流程稳定，不在此重构范围内）。
TAB_SETTINGS_X = 900
TAB_SETTINGS_Y = 2184
TAB_ALBUM_X = 540
TAB_ALBUM_Y = 2184
TAB_HOME_X = 180
TAB_HOME_Y = 2184
LAUNCHER_FOCUS = "com.android.launcher3"

# === 框架级可配置：控件改版后只需调整以下常量，无需改执行逻辑 ===

# 底部主导航 tab 识别：group → 在 desc/text 里匹配的文案。
TAB_LABELS: dict[str, str] = {
    GROUP_HOME: "首页",
    GROUP_ALBUM: "相册",
    GROUP_SETTINGS: "设置",
}

# 子页入口识别：group → 入口控件的 desc/text 匹配文案（入口默认在首页）。
SUBPAGE_ENTRY_LABELS: dict[str, str] = {
    GROUP_BEGINNER: "新手教程",
}

# 危险控件黑名单：按 desc/text 正则匹配，命中则从 monkey 随机点击池剔除。
# 这些控件要么会污染其他用例状态（如"重置新手指引"影响 case1），
# 要么会拉起外部浏览器/应用商店导致 App 失焦。
DANGEROUS_PATTERNS: list[str] = [
    r"重置.*新手",
    r"查看.*版本",
    r"用户协议",
    r"使用条款",
    r"个人信息收集",
    r"第三方信息",
    r"隐私.*政策",
]

# 崩溃/ANR 对话框文案（中英文）。命中即记 failure 并尝试 dismiss。
CRASH_PATTERNS: list[str] = [
    "has stopped",
    "Unfortunately",
    "isn't responding",
    "ANR",
    "无响应",
    "应用无响应",
    "应用已停止",
    "已停止运行",
]

# dismiss 崩溃/ANR 对话框时尝试点击的按钮文案
DISMISS_BUTTON_LABELS: list[str] = [
    "关闭",
    "确定",
    "OK",
    "CLOSE",
    "DISMISS",
]

# 底部 tab 区域识别：y 坐标大于屏幕高度 * (1 - BOTTOM_TAB_RATIO) 视为底部 tab 条
# 0.15 比 0.1 更宽松，控件改版后 tab 位置小幅上移仍可识别
BOTTOM_TAB_RATIO = 0.15

# tab 在主点击池里的抽样权重（0.0–1.0），越小越倾向于点功能控件而非切 tab
TAB_CLICK_PROBABILITY = 0.2

_TIMELINE: list[dict] = []


def _mark(device: u2.Device, step: str, detail: str = "") -> None:
    """Log a step marker to Python log, ADB logcat, and in-memory timeline."""
    msg = f"step={step}"
    if detail:
        msg += f" {detail}"
    device.shell(["log", "-t", "UIA_CASE7", msg])
    entry = {"ts": time.time(), "step": step, "detail": detail}
    _TIMELINE.append(entry)
    logger.info("[%s] %s", step, detail if detail else step)


def _current_package(device: u2.Device) -> str:
    try:
        return device.app_current().get("package") or ""
    except Exception:  # noqa: BLE001
        return ""


def _tap_tab(device: u2.Device, x: int, y: int, tab_name: str) -> None:
    device.click(x, y)
    cancellable_sleep(1.2)


def _bring_app_to_front(device: u2.Device, package_name: str) -> None:
    current = _current_package(device)
    if current == package_name:
        return
    _mark(device, "reopen", f"app not in foreground ({current}), relaunching {package_name}")
    device.app_start(package_name)
    cancellable_sleep(2)


def _clickable_nodes(device: u2.Device) -> list[UiNode]:
    """Return clickable nodes below the status bar from a live dump."""
    nodes = []
    for node in _dump_nodes(device):
        if not node.clickable:
            continue
        left, top, right, bottom = node.bounds
        if top < STATUS_BAR_CUTOFF_Y or bottom <= top or right <= left:
            continue
        if node.class_name.endswith("ScrollView") or node.class_name.endswith("ViewPager"):
            continue
        nodes.append(node)
    return nodes


# === 新增：可滚动区动态解析 ===


@dataclass
class _Scrollable:
    bounds: tuple[int, int, int, int]
    class_name: str
    direction: str  # "vertical" | "horizontal"


def _dump_scrollables(device: u2.Device) -> list[_Scrollable]:
    """运行时解析当前页所有 scrollable 节点，按 class 名启发式判断方向。

    case1.UiNode 没有 scrollable 字段，故 case7 内部独立解析，不污染 case1。
    """
    xml = device.dump_hierarchy(compressed=False)
    root = ET.fromstring(xml)
    result: list[_Scrollable] = []
    for raw in root.iter("node"):
        if raw.attrib.get("scrollable", "false") != "true":
            continue
        cls = raw.attrib.get("class", "")
        bounds = _parse_bounds(raw.attrib.get("bounds", "[0,0][0,0]"))
        left, top, right, bottom = bounds
        if top < STATUS_BAR_CUTOFF_Y or bottom <= top or right <= left:
            continue
        direction = "horizontal" if (
            "HorizontalScrollView" in cls or "ViewPager" in cls
        ) else "vertical"
        result.append(_Scrollable(bounds=bounds, class_name=cls, direction=direction))
    return result


def _screen_height(device: u2.Device) -> int:
    try:
        return int(device.info.get("displayHeight") or 0)
    except Exception:  # noqa: BLE001
        return 0


def _is_bottom_tab(node: UiNode, screen_h: int) -> bool:
    """启发式判断节点是否属于底部主导航 tab。"""
    if not node.class_name.endswith("ImageView"):
        return False
    left, top, right, bottom = node.bounds
    if screen_h > 0 and top < screen_h * (1 - BOTTOM_TAB_RATIO):
        return False
    content = node.desc or node.text or ""
    return any(label in content for label in TAB_LABELS.values()) or "标签" in content


def _split_clickable_pool(nodes: list[UiNode], screen_h: int) -> tuple[list[UiNode], list[UiNode]]:
    """把可点击节点分成 (功能控件池, 底部 tab 池)。"""
    tabs: list[UiNode] = []
    features: list[UiNode] = []
    for node in nodes:
        if _is_bottom_tab(node, screen_h):
            tabs.append(node)
        else:
            features.append(node)
    return features, tabs


def _is_dangerous(node: UiNode) -> bool:
    content = node.desc or node.text or ""
    if not content:
        return False
    return any(re.search(pattern, content) for pattern in DANGEROUS_PATTERNS)


def _tap_by_label(device: u2.Device, label: str, timeout: int = 2) -> bool:
    """按文案定位并点击控件。优先 desc 精确 → desc contains → text contains。"""
    for selector in (
        device(description=label),
        device(descriptionContains=label),
        device(text=label),
        device(textContains=label),
    ):
        if selector.exists(timeout=timeout):
            selector.click()
            return True
    return False


def _navigate_to_group(device: u2.Device, group: str, package_name: str) -> str:
    """把 App 切到 group 对应的顶层页面。返回导航结果摘要。

    设计原则：不依赖固定坐标。底部 tab 按 TAB_LABELS 文案定位；
    子页入口按 SUBPAGE_ENTRY_LABELS 文案定位。控件改版后只需改这两个常量。
    导航失败不抛异常（monkey 容错），下次迭代重试。
    """
    _bring_app_to_front(device, package_name)

    # 连接分组：导航由 ACTION_CONNECT / _do_connect 自带，此处不重复
    if group == GROUP_CONNECT:
        return "connect group, navigation handled by ACTION_CONNECT"

    # 顶层 tab 分组：首页 / 相册 / 设置
    if group in TAB_LABELS:
        label = TAB_LABELS[group]
        if _tap_by_label(device, label):
            cancellable_sleep(1.0)
            return f"navigated to tab '{label}'"
        return f"tab '{label}' not found, stay on current page"

    # 子页分组：先回首页，再点子页入口
    if group in SUBPAGE_ENTRY_LABELS:
        home_label = TAB_LABELS.get(GROUP_HOME, "首页")
        _tap_by_label(device, home_label)
        cancellable_sleep(1.0)
        entry_label = SUBPAGE_ENTRY_LABELS[group]
        if _tap_by_label(device, entry_label):
            cancellable_sleep(1.0)
            return f"navigated to subpage '{entry_label}' via home"
        return f"subpage entry '{entry_label}' not found on home"

    # 未知 group：不导航，直接在当前页操作
    return f"unknown group '{group}', no navigation"


def _detect_and_dismiss_crash(device: u2.Device, metrics: MetricsRecorder, iteration: int) -> bool:
    """检测当前是否出现崩溃/ANR 对话框。命中则记 failure 并尝试 dismiss。返回是否命中。"""
    try:
        xml = device.dump_hierarchy(compressed=False)
    except Exception:  # noqa: BLE001
        return False
    matched = next((p for p in CRASH_PATTERNS if p in xml), None)
    if not matched:
        return False

    _mark(device, "crash", f"crash/ANR dialog detected: pattern='{matched}' iter={iteration}")
    metrics.record_failure("uia_case7", f"crash_iter_{iteration}", {"pattern": matched})

    for label in DISMISS_BUTTON_LABELS:
        for selector in (device(text=label), device(description=label)):
            try:
                if selector.exists(timeout=1):
                    selector.click()
                    cancellable_sleep(1.0)
                    _mark(device, "crash", f"dismissed with button '{label}'")
                    return True
            except Exception:  # noqa: BLE001
                continue
    _mark(device, "crash", "no dismiss button found, dialog may persist")
    return True


def _execute_click(device: u2.Device, package_name: str) -> str:
    """随机点击当前页一个可点击节点。

    - 危险控件（DANGEROUS_PATTERNS）剔除
    - 底部 tab 与功能控件分池抽样，TAB_CLICK_PROBABILITY 控制切 tab 概率
    """
    _bring_app_to_front(device, package_name)
    nodes = _clickable_nodes(device)
    if not nodes:
        return "no clickable node found"

    safe_nodes = [n for n in nodes if not _is_dangerous(n)]
    if not safe_nodes:
        return "all clickable nodes are dangerous, click skipped"

    screen_h = _screen_height(device)
    features, tabs = _split_clickable_pool(safe_nodes, screen_h)

    if tabs and (not features or random.random() < TAB_CLICK_PROBABILITY):
        node = random.choice(tabs)
    elif features:
        node = random.choice(features)
    else:
        node = random.choice(tabs)  # 兜底（理论上不会到这）

    cx, cy = node.center
    device.click(cx, cy)
    cancellable_sleep(1.0)
    label = node.desc or node.text or node.class_name
    return f"clicked {label} at {node.bounds}"


def _execute_swipe(device: u2.Device, package_name: str) -> str:
    """在当前页随机选一个 scrollable 节点滑动。没有可滚动区则跳过。

    方向按 scrollable 节点的 class 启发式判断：
      HorizontalScrollView / ViewPager → 水平
      其他（ScrollView/RecyclerView/ListView）→ 垂直
    不再依赖写死的 SWIPE_AREA，控件改版后自动适配。
    """
    _bring_app_to_front(device, package_name)
    scrollables = _dump_scrollables(device)
    if not scrollables:
        return "no scrollable node on current page, swipe skipped"

    target = random.choice(scrollables)
    left, top, right, bottom = target.bounds
    cx = (left + right) // 2
    cy = (top + bottom) // 2
    span_x = max(20, int((right - left) * 0.3))
    span_y = max(20, int((bottom - top) * 0.3))
    duration = random.uniform(0.2, 0.6)

    if target.direction == "horizontal":
        direction = random.choice(["left", "right"])
        if direction == "left":
            device.swipe(cx + span_x, cy, cx - span_x, cy, duration)
        else:
            device.swipe(cx - span_x, cy, cx + span_x, cy, duration)
    else:
        direction = random.choice(["up", "down"])
        if direction == "up":
            device.swipe(cx, cy + span_y, cx, cy - span_y, duration)
        else:
            device.swipe(cx, cy - span_y, cx, cy + span_y, duration)

    cancellable_sleep(1.0)
    return f"swipe {direction} on {target.class_name} at {target.bounds}"


def _execute_back(device: u2.Device, package_name: str) -> str:
    device.press("back")
    cancellable_sleep(1.2)
    _bring_app_to_front(device, package_name)
    return "pressed back"


def _has_desc(device: u2.Device, text: str) -> bool:
    xml = device.dump_hierarchy(compressed=False)
    return text in xml


def _find_connect_card(device: u2.Device) -> bool:
    xml = device.dump_hierarchy(compressed=False)
    root = ET.fromstring(xml)
    for node in root.iter("node"):
        desc = node.attrib.get("content-desc", "") or ""
        if "点击连接设备" in desc:
            b = node.attrib["bounds"]
            m = re.match(r"\[(\d+),(\d+)\]\[(\d+),(\d+)\]", b)
            if m:
                cx = (int(m[1]) + int(m[3])) // 2
                cy = (int(m[2]) + int(m[4])) // 2
                device.click(cx, cy)
                return True
    return False


def _find_device_by_name(device: u2.Device, target_name: str, max_swipes: int = 50) -> bool:
    """Swipe through device cards until target device is found. Returns True if found."""
    expected = target_name.strip().casefold()
    for i in range(max_swipes):
        root = ET.fromstring(device.dump_hierarchy(compressed=False))
        for node in root.iter("node"):
            name = (node.attrib.get("content-desc", "") or "").split("\n", 1)[0].strip()
            if name and name.casefold() == expected:
                logger.info("Found target device '%s' after %d swipes", target_name, i)
                return True
        _swipe_device_card(device, "next")
        cancellable_sleep(1.5)
    return False


def _read_device_name(xml: str) -> str | None:
    root = ET.fromstring(xml)
    for node in root.iter("node"):
        name = (node.attrib.get("content-desc", "") or "").split("\n", 1)[0].strip()
        if name:
            return name
    return None


def _swipe_device_card(device: u2.Device, direction: str) -> None:
    x1, y1, x2, y2 = SWIPE_AREA
    cx = (x1 + x2) // 2
    cy = (y1 + y2) // 2
    offset = int((x2 - x1) * 0.3)
    if direction == "next":
        device.swipe(cx + offset, cy, cx - offset, cy, 0.15)
    else:
        device.swipe(cx - offset, cy, cx + offset, cy, 0.15)


def _find_connect_button(device: u2.Device) -> tuple[int, int] | None:
    xml = device.dump_hierarchy(compressed=False)
    root = ET.fromstring(xml)
    for node in root.iter("node"):
        desc = node.attrib.get("content-desc", "") or ""
        if desc.strip() == "连接":
            clickable = node.attrib.get("clickable", "false") == "true"
            if clickable:
                b = node.attrib["bounds"]
                m = re.match(r"\[(\d+),(\d+)\]\[(\d+),(\d+)\]", b)
                if m:
                    cx = (int(m[1]) + int(m[3])) // 2
                    cy = (int(m[2]) + int(m[4])) // 2
                    return (cx, cy)
    return None


def _find_connected_tag(device: u2.Device) -> tuple[int, int] | None:
    xml = device.dump_hierarchy(compressed=False)
    root = ET.fromstring(xml)
    for node in root.iter("node"):
        desc = node.attrib.get("content-desc", "") or ""
        if "已连接" in desc:
            b = node.attrib["bounds"]
            m = re.match(r"\[(\d+),(\d+)\]\[(\d+),(\d+)\]", b)
            if m:
                if int(m[2]) < STATUS_BAR_CUTOFF_Y:
                    continue
                cx = (int(m[1]) + int(m[3])) // 2
                cy = (int(m[2]) + int(m[4])) // 2
                return (cx, cy)
    return None


def _is_connected(device: u2.Device) -> bool:
    xml = device.dump_hierarchy(compressed=False)
    root = ET.fromstring(xml)
    for node in root.iter("node"):
        desc = node.attrib.get("content-desc", "") or ""
        if "已连接" in desc:
            b = node.attrib.get("bounds", "")
            m = re.match(r"\[(\d+),(\d+)\]\[(\d+),(\d+)\]", b)
            if m and int(m[2]) < STATUS_BAR_CUTOFF_Y:
                continue
            return True
    return False


def _find_confirm_in_dialog(device: u2.Device) -> tuple[int, int] | None:
    xml = device.dump_hierarchy(compressed=False)
    root = ET.fromstring(xml)
    for node in root.iter("node"):
        desc = node.attrib.get("content-desc", "") or ""
        if "确认" in desc:
            b = node.attrib["bounds"]
            m = re.match(r"\[(\d+),(\d+)\]\[(\d+),(\d+)\]", b)
            if m:
                if int(m[2]) < STATUS_BAR_CUTOFF_Y:
                    continue
                cx = (int(m[1]) + int(m[3])) // 2
                cy = (int(m[2]) + int(m[4])) // 2
                return (cx, cy)
    return None


def _disconnect_if_connected(device: u2.Device) -> bool:
    """If a device is connected, disconnect it first (like case6)."""
    tag = _find_connected_tag(device)
    if tag is None:
        return False
    _mark(device, "disconnect", "already connected, disconnecting first")
    device.click(tag[0], tag[1])
    cancellable_sleep(2)
    confirm = _find_confirm_in_dialog(device)
    if confirm:
        device.click(confirm[0], confirm[1])
        _mark(device, "disconnect", "existing connection disconnected")
        cancellable_sleep(2)
    return True


def _wait_for_connection(device: u2.Device, timeout: int = 30) -> bool:
    start = time.time()
    while time.time() - start < timeout:
        xml = device.dump_hierarchy(compressed=False)
        if "连接成功" in xml:
            logger.info("Connection succeeded (连接成功 toast) after %.1fs", time.time() - start)
            return True
        if _is_connected(device):
            logger.info("Connection established (已连接 tag) after %.1fs", time.time() - start)
            return True
        root = ET.fromstring(xml)
        for node in root.iter("node"):
            desc = node.attrib.get("content-desc", "") or ""
            if "连接失败" in desc or "连接超时" in desc:
                logger.warning("Connection failed: %s", desc)
                return False
        cancellable_sleep(1)
    logger.warning("Connection timed out after %ds", timeout)
    return False


def _do_connect(device: u2.Device, target_name: str, retry: bool = True) -> bool:
    """前置条件 A：进入设置 → 连接设备卡片 → 按 ble_exact_name 找到设备 → 连接。"""
    _mark(device, "connect", f"start connect flow for {target_name}")
    _tap_tab(device, TAB_SETTINGS_X, TAB_SETTINGS_Y, "设置")
    if not _find_connect_card(device):
        _mark(device, "connect", "connect card not found on settings page")
        return False
    cancellable_sleep(2)

    if not _find_device_by_name(device, target_name):
        _mark(device, "connect", f"target device {target_name} not found after swiping")
        return False

    connect_btn = _find_connect_button(device)
    if connect_btn is None:
        _mark(device, "connect", "连接 button not found")
        return False
    cx, cy = connect_btn
    device.click(cx, cy)
    cancellable_sleep(1.5)

    ok = _wait_for_connection(device, timeout=30)
    if not ok and retry:
        _mark(device, "connect", "first connect attempt failed, retrying once")
        cancellable_sleep(5)
        _tap_tab(device, TAB_SETTINGS_X, TAB_SETTINGS_Y, "设置")
        if _find_connect_card(device):
            cancellable_sleep(2)
            if _find_device_by_name(device, target_name):
                btn2 = _find_connect_button(device)
                if btn2:
                    device.click(btn2[0], btn2[1])
                    cancellable_sleep(1.5)
                    ok = _wait_for_connection(device, timeout=30)
    return ok


def _ensure_connected_final(device: u2.Device, target_name: str) -> bool:
    """前置条件 B：迭代结束后必须保持连接，未连接则主动连接一次。"""
    if _is_connected(device):
        _mark(device, "final", "connection already established at end of run")
        return True
    _mark(device, "final", "NOT connected at end of run, attempting final connect")
    return _do_connect(device, target_name, retry=False)


def _weighted_pick(monkey_plan: list[dict]) -> dict:
    """Pick one plan entry using weighted random based on percent fields."""
    entries = [item for item in monkey_plan if int(item.get("percent", 0)) > 0]
    if not entries:
        entries = list(monkey_plan)
    weights = [max(0, int(item.get("percent", 1))) for item in entries]
    if sum(weights) <= 0:
        weights = [1] * len(entries)
    return random.choices(entries, weights=weights, k=1)[0]


def _normalize_plan(raw_plan: Iterable[dict] | None) -> list[dict]:
    """Normalize an untrusted plan (may come from GUI options)."""
    if not raw_plan:
        return list(DEFAULT_MONKEY_PLAN)
    plan = []
    for item in raw_plan:
        group = item.get("group") if isinstance(item, dict) else None
        group = str(group).strip() if group else GROUP_HOME
        action = str(item.get("action", ACTION_CLICK)).strip()
        if action not in (ACTION_CLICK, ACTION_SWIPE, ACTION_BACK, ACTION_CONNECT):
            action = ACTION_CLICK
        try:
            percent = max(0, int(item.get("percent", 0)))
        except (TypeError, ValueError):
            percent = 0
        plan.append({"group": group, "action": action, "percent": percent})
    return plan


def _dump_recent_logcat(serial: str, output: Path, lines: int = 400) -> None:
    """Capture a recent adb logcat snippet for developer debugging (crash slices)."""
    try:
        output.parent.mkdir(parents=True, exist_ok=True)
        cmd = ["adb"]
        if serial:
            cmd.extend(["-s", serial])
        cmd.extend(["logcat", "-d", "-t", str(lines)])
        result = subprocess.run(cmd, capture_output=True, text=True, encoding="utf-8", errors="replace", timeout=20)
        output.write_text(result.stdout or result.stderr or "", encoding="utf-8")
    except Exception as exc:  # noqa: BLE001
        logger.warning("failed to capture recent logcat snippet: %s", exc)


def run_uia_case7(app_config: AppConfig, report_dir: str | Path, options: dict | None = None) -> dict:
    """Monkey 测试：按页面/动作权重随机操作 App，结束后必须保持连接。

    框架特性（本次重构）：
      - 每次 click/swipe/back 前先按 entry["group"] 导航到对应页面（TAB_LABELS /
        SUBPAGE_ENTRY_LABELS 文案定位，控件改版后只改这两个常量）
      - swipe 运行时从当前页 dump 动态找 scrollable 节点，按 class 启发式判断方向
      - click 危险控件黑名单过滤（DANGEROUS_PATTERNS），底部 tab 与功能控件分池抽样
      - 每次动作前检测崩溃/ANR 对话框（CRASH_PATTERNS），命中记 failure 并尝试 dismiss

    前置条件 A：随机到"激活连接"动作时，按 device.ble_exact_name 找到设备并连接。
    前置条件 B：全部迭代结束后必须已连接，否则整体失败。
    全程抓取 adb logcat；故障时刻额外存档 logcat 片段便于开发定位。

    注：connect 动作单次最多 ~65s，权重建议 ≤ 3%，靠 _ensure_connected_final 兜底。
    """
    artifacts = prepare_uia_case_artifacts(report_dir, "uia_case7")
    logger = get_logger("pocket_app_automation.uia_case7")
    metrics = MetricsRecorder(artifacts.report_dir)
    iterations = app_config.run.case7_iterations
    plan = _normalize_plan((options or {}).get("monkey_plan"))
    _TIMELINE.clear()
    logger.info("uia_case7 started iterations=%s plan=%s", iterations, json.dumps(plan, ensure_ascii=False))

    target_device = app_config.device.ble_exact_name.strip()
    if not target_device:
        raise AssertionError("No target device name configured (device.ble_exact_name in config).")

    exit_code = 0
    error_message = ""
    logcat: AndroidAppLogcatCapture | None = None
    try:
        serial = app_config.mobile.android_serial.strip()
        device = u2.connect(serial) if serial else u2.connect()
        package_name = app_config.mobile.android_package_name.strip() or device.app_current().get("package") or device.info.get("currentPackageName", "")
        if not package_name:
            raise AssertionError("Unable to determine Android package name for UIA case7.")

        logcat = AndroidAppLogcatCapture(
            serial=device.serial,
            package_name=package_name,
            output_file=artifacts.app_logcat_file,
        )
        logcat.start()

        _mark(device, "start", f"monkey test start iterations={iterations}")
        for index in range(iterations):
            raise_if_cancelled()
            iteration = index + 1
            entry = _weighted_pick(plan)
            action = entry["action"]
            group = entry.get("group", GROUP_HOME)
            started = time.perf_counter()
            try:
                # 导航前置：connect 自带导航，其余动作先切到 group 对应页面
                if action != ACTION_CONNECT:
                    nav_detail = _navigate_to_group(device, group, package_name)
                    _mark(device, "navigate", f"iter {iteration} group={group} {nav_detail}")

                # 崩溃/ANR 检测（动作前）
                if _detect_and_dismiss_crash(device, metrics, iteration):
                    detail = f"crash detected before {action}, dismissed"
                    _record_ui_metric(metrics, "uia_case7", f"{action}_{iteration}", started, False, error=detail)
                    _mark(device, "iteration", f"{iteration}/{iterations} {action} {detail}")
                    emit_case_event(
                        ITERATION_PROGRESS,
                        {"case": "case7", "iteration": iteration, "completed": iteration, "total": iterations, "ok": False},
                    )
                    continue

                if action == ACTION_CLICK:
                    detail = _execute_click(device, package_name)
                elif action == ACTION_SWIPE:
                    detail = _execute_swipe(device, package_name)
                elif action == ACTION_BACK:
                    detail = _execute_back(device, package_name)
                else:  # ACTION_CONNECT → 前置条件 A
                    detail = f"connect -> {_do_connect(device, target_device)}"
                ok = True
            except Exception as exc:  # noqa: BLE001
                ok = False
                detail = f"{action} failed: {exc}"
                _dump_ui(device, artifacts.dumps_dir / f"iteration_{iteration:04d}_{action}_fail.xml")
                _save_screenshot(device, artifacts.screenshots_dir / f"iteration_{iteration:04d}_{action}_fail.png")
                _dump_recent_logcat(device.serial, artifacts.logs_dir / f"iteration_{iteration:04d}_{action}_fail.logcat")
                _record_ui_metric(metrics, "uia_case7", f"{action}_{iteration}", started, False, error=detail)
                raise_if_cancelled()
                raise

            _mark(device, "iteration", f"{iteration}/{iterations} {action} {detail}")
            _record_ui_metric(metrics, "uia_case7", f"{action}_{iteration}", started, True)
            if action == ACTION_CONNECT:
                metrics.record_event("uia_case7_connect", {"iteration": iteration, "device": target_device})
            emit_case_event(
                ITERATION_PROGRESS,
                {"case": "case7", "iteration": iteration, "completed": iteration, "total": iterations, "ok": ok},
            )

        # 前置条件 B：迭代结束后必须保持连接
        _mark(device, "final", "verifying connection at end of run")
        connected = _ensure_connected_final(device, target_device)
        if not connected:
            _dump_ui(device, artifacts.dumps_dir / "final_not_connected.xml")
            _save_screenshot(device, artifacts.screenshots_dir / "final_not_connected.png")
            _dump_recent_logcat(device.serial, artifacts.logs_dir / "final_not_connected.logcat")
            error_message = f"Monkey completed {iterations} iterations but device was NOT connected at the end."
            logger.error(error_message)
            metrics.record_failure("uia_case7", "final_connected", {"error": error_message})
            exit_code = 1
        else:
            metrics.record_event("uia_case7_final_connected", {"iterations": iterations, "device": target_device})
            logger.info("uia_case7 passed: connection maintained after %s iterations", iterations)
    except CaseCancelled as exc:
        exit_code = 2
        error_message = str(exc)
        metrics.record_event("uia_case7_cancelled", {"error": error_message})
        logger.info("uia_case7 cancelled")
    except Exception as exc:  # noqa: BLE001
        exit_code = 1
        error_message = str(exc)
        metrics.record_failure("uia_case7", "execution", {"error": error_message})
        logger.exception("uia_case7 failed")
    finally:
        if logcat is not None:
            logcat.stop()

    summary = metrics.build_summary("uia_case7", exit_code)
    report_html = _write_uia_case7_report(artifacts, summary, error_message)

    timeline_path = artifacts.report_dir / "timeline.jsonl"
    with timeline_path.open("w", encoding="utf-8") as f:
        for entry in _TIMELINE:
            f.write(json.dumps(entry, ensure_ascii=False) + "\n")
    logger.info("Timeline written to %s", timeline_path)

    return {
        "case": "uia_case7",
        "exit_code": exit_code,
        "report_dir": str(artifacts.report_dir),
        "report_html": str(report_html),
        "summary_json": str(artifacts.report_dir / "summary.json"),
        "metrics_csv": str(artifacts.report_dir / "metrics.csv"),
        "failures_jsonl": str(artifacts.report_dir / "failures.jsonl"),
        "events_jsonl": str(artifacts.report_dir / "events.jsonl"),
        "log_file": str(artifacts.script_log_file),
        "app_logcat_file": str(artifacts.app_logcat_file),
        "summary": summary,
        "error": error_message,
    }


def _write_uia_case7_report(artifacts, summary: dict, error_message: str) -> Path:
    report_path = artifacts.report_dir / "report.html"
    dumps = sorted(path.name for path in artifacts.dumps_dir.glob("*.xml"))
    screenshots = sorted(path.name for path in artifacts.screenshots_dir.glob("*.png"))
    logcat_slices = sorted(path.name for path in artifacts.logs_dir.glob("*.logcat"))
    summary_rows = "".join(
        f"<tr><th>{html.escape(str(k))}</th><td>{html.escape(str(v))}</td></tr>"
        for k, v in summary.items()
    )
    error_block = (
        f'<div class="card error"><h2>错误信息</h2><pre>{html.escape(error_message)}</pre></div>'
        if error_message
        else ""
    )
    def _list_html(title: str, items: list[str]) -> str:
        rows = "".join(f"<li>{html.escape(item)}</li>" for item in items) or "<li>无</li>"
        return f"<h2>{title}</h2><ul>{rows}</ul>"

    html_content = f"""<!doctype html>
<html lang="zh-CN">
<head>
  <meta charset="utf-8">
  <title>UIA Case7 monkey 测试报告</title>
  <style>
    body {{ font-family: "Microsoft YaHei UI", sans-serif; background: #f4f7fb; color: #173042; margin: 24px; }}
    .card {{ background: white; border-radius: 14px; padding: 18px 20px; box-shadow: 0 10px 28px rgba(18,52,77,.08); margin-bottom: 18px; }}
    .error {{ border-left: 6px solid #cb4d4d; }}
    h1, h2 {{ margin: 0 0 12px 0; }}
    table {{ width: 100%; border-collapse: collapse; }}
    td, th {{ border-bottom: 1px solid #e4edf6; padding: 10px 8px; text-align: left; vertical-align: top; }}
    ul {{ margin: 0; padding-left: 20px; }}
    pre {{ background: #0d1b2a; color: #d8f3ff; padding: 12px; border-radius: 10px; overflow: auto; }}
    code {{ font-family: Consolas, monospace; }}
  </style>
</head>
<body>
  <div class="card"><h1>UIA Case7 monkey 随机测试</h1><table>{summary_rows}</table></div>
  {error_block}
  <div class="card"><h2>日志与产物</h2>
    <table>
      <tr><th>脚本日志</th><td><code>{html.escape(str(artifacts.script_log_file))}</code></td></tr>
      <tr><th>App logcat</th><td><code>{html.escape(str(artifacts.app_logcat_file))}</code></td></tr>
      <tr><th>故障 logcat 片段目录</th><td><code>{html.escape(str(artifacts.logs_dir))}</code></td></tr>
      <tr><th>UI Dump 目录</th><td><code>{html.escape(str(artifacts.dumps_dir))}</code></td></tr>
      <tr><th>截图目录</th><td><code>{html.escape(str(artifacts.screenshots_dir))}</code></td></tr>
    </table>
  </div>
  <div class="card">{_list_html("故障 logcat 片段（供开发定位）", logcat_slices)}</div>
  <div class="card">{_list_html("截图列表", screenshots)}</div>
  <div class="card">{_list_html("UI Dump 列表", dumps)}</div>
</body>
</html>"""
    report_path.write_text(html_content, encoding="utf-8")
    return report_path
