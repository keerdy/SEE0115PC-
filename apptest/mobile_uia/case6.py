# -*- coding: utf-8 -*-
from __future__ import annotations

import html
import json
import re
import time
import xml.etree.ElementTree as ET
from dataclasses import dataclass
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
    _click_by_label,
    _dump_nodes,
    _dump_ui,
    _parse_bounds,
    _record_ui_metric,
    _save_screenshot,
    _tap_bounds,
    UiNode,
)


logger = get_logger("pocket_app_automation.uia_case6")


def _has_desc(device: u2.Device, text: str) -> bool:
    """Check if any node in the hierarchy has the given text in its content-desc."""
    xml = device.dump_hierarchy(compressed=False)
    root = ET.fromstring(xml)
    for node in root.iter("node"):
        desc = node.attrib.get("content-desc", "") or ""
        if text in desc:
            return True
    return False


def _tap_desc(device: u2.Device, desc: str, scenario: str, step: str, metrics: MetricsRecorder, iteration: int) -> None:
    """Find a node by content-desc and click its center. Uses XML parsing for reliability."""
    start = time.perf_counter()
    for _ in range(5):
        xml = device.dump_hierarchy(compressed=False)
        root = ET.fromstring(xml)
        for node in root.iter("node"):
            node_desc = node.attrib.get("content-desc", "") or ""
            if desc in node_desc or node_desc == desc:
                b = node.attrib["bounds"]
                m = re.match(r"\[(\d+),(\d+)\]\[(\d+),(\d+)\]", b)
                if m:
                    cx = (int(m[1]) + int(m[3])) // 2
                    cy = (int(m[2]) + int(m[4])) // 2
                    device.click(cx, cy)
                    _record_ui_metric(metrics, scenario, f"{step}_{iteration}", start, True)
                    return
        cancellable_sleep(1)
    _record_ui_metric(metrics, scenario, f"{step}_{iteration}", start, False, error=f"desc not found: {desc}")
    raise AssertionError(f"UI node not found by description: {desc}")


def _read_device_name(xml: str) -> str | None:
    """Extract the selected device card's first-line name without product-prefix assumptions."""
    root = ET.fromstring(xml)
    for node in root.iter("node"):
        name = (node.attrib.get("content-desc", "") or "").split("\n", 1)[0].strip()
        if name:
            return name
    return None


def _read_page_indicator(xml: str) -> tuple[int, int] | None:
    """Extract current page / total pages from the device card."""
    root = ET.fromstring(xml)
    for node in root.iter("node"):
        desc = node.attrib.get("content-desc", "") or ""
        m = re.search(r"(\d+)\s*/\s*(\d+)", desc)
        if m:
            return int(m.group(1)), int(m.group(2))
    return None


SCROLLABLE_AREA_X1 = 108
SCROLLABLE_AREA_X2 = 972
SETTINGS_TAB_X = 900  # center of [720,2112][1080,2256]
SETTINGS_TAB_Y = 2184
SCROLLABLE_AREA_Y1 = 945
SCROLLABLE_AREA_Y2 = 1800


def _swipe_device_card(device: u2.Device, direction: str) -> None:
    """Swipe within the scrollable device content area [108,945][972,1800]."""
    area_cx = (SCROLLABLE_AREA_X1 + SCROLLABLE_AREA_X2) // 2  # 540
    area_cy = (SCROLLABLE_AREA_Y1 + SCROLLABLE_AREA_Y2) // 2  # 1372
    offset = int((SCROLLABLE_AREA_X2 - SCROLLABLE_AREA_X1) * 0.3)  # ~259
    if direction == "next":
        device.swipe(area_cx + offset, area_cy, area_cx - offset, area_cy, 0.15)
    else:
        device.swipe(area_cx - offset, area_cy, area_cx + offset, area_cy, 0.15)


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


def _find_connect_button(device: u2.Device) -> tuple[int, int] | None:
    """Find the 连接 button on the device card page."""
    xml = device.dump_hierarchy(compressed=False)
    root = ET.fromstring(xml)
    for node in root.iter("node"):
        desc = node.attrib.get("content-desc", "") or ""
        if desc.strip() == "\u8fde\u63a5":  # 连接
            clickable = node.attrib.get("clickable", "false") == "true"
            if clickable:
                b = node.attrib["bounds"]
                m = re.match(r"\[(\d+),(\d+)\]\[(\d+),(\d+)\]", b)
                if m:
                    cx = (int(m[1]) + int(m[3])) // 2
                    cy = (int(m[2]) + int(m[4])) // 2
                    return (cx, cy)
    return None


def _adb_log(device: u2.Device, msg: str) -> None:
    device.shell(["log", "-t", "UIA_CASE6", msg])


_TIMELINE: list[dict] = []


def _mark(device: u2.Device, step: str, detail: str = "") -> None:
    """Log a step marker to Python log, ADB logcat, and in-memory timeline."""
    msg = f"step={step}"
    if detail:
        msg += f" {detail}"
    device.shell(["log", "-t", "UIA_CASE6", msg])
    entry = {"ts": time.time(), "step": step, "detail": detail}
    _TIMELINE.append(entry)
    logger.info("[%s] %s", step, detail if detail else step)


def _wait_for_connection(device: u2.Device, timeout: int = 30) -> bool:
    start = time.time()
    while time.time() - start < timeout:
        xml = device.dump_hierarchy(compressed=False)
        if "\u8fde\u63a5\u6210\u529f" in xml:
            elapsed = time.time() - start
            logger.info("Connection succeeded (连接成功 toast) after %.1fs", elapsed)
            return True
        root = ET.fromstring(xml)
        for node in root.iter("node"):
            desc = node.attrib.get("content-desc", "") or ""
            if "\u5df2\u8fde\u63a5" in desc:
                b = node.attrib.get("bounds", "")
                m = re.match(r"\[(\d+),(\d+)\]\[(\d+),(\d+)\]", b)
                if m and int(m[2]) < 72:
                    continue
                elapsed = time.time() - start
                logger.info("Connection established (已连接 tag) after %.1fs", elapsed)
                return True
            if "\u8fde\u63a5\u5931\u8d25" in desc or "\u8fde\u63a5\u8d85\u65f6" in desc:
                logger.warning("Connection failed: %s", desc)
                return False
        cancellable_sleep(1)
    logger.warning("Connection timed out after %ds", timeout)
    return False


def _find_connected_tag(device: u2.Device) -> tuple[int, int] | None:
    """Find the 已连接 tag outside the status bar."""
    xml = device.dump_hierarchy(compressed=False)
    root = ET.fromstring(xml)
    for node in root.iter("node"):
        desc = node.attrib.get("content-desc", "") or ""
        if "\u5df2\u8fde\u63a5" in desc:
            b = node.attrib["bounds"]
            m = re.match(r"\[(\d+),(\d+)\]\[(\d+),(\d+)\]", b)
            if m:
                if int(m[2]) < 72:
                    continue  # skip status bar
                cx = (int(m[1]) + int(m[3])) // 2
                cy = (int(m[2]) + int(m[4])) // 2
                logger.info("已连接 tag found at bounds=%s", b)
                return (cx, cy)
    return None


def _find_confirm_in_dialog(device: u2.Device) -> tuple[int, int] | None:
    """Find the 确认 button in the disconnect confirmation dialog."""
    xml = device.dump_hierarchy(compressed=False)
    root = ET.fromstring(xml)
    for node in root.iter("node"):
        desc = node.attrib.get("content-desc", "") or ""
        if "\u786e\u8ba4" in desc:
            b = node.attrib["bounds"]
            m = re.match(r"\[(\d+),(\d+)\]\[(\d+),(\d+)\]", b)
            if m:
                if int(m[2]) < 72:
                    continue  # skip status bar
                cx = (int(m[1]) + int(m[3])) // 2
                cy = (int(m[2]) + int(m[4])) // 2
                logger.info("确认 button found in disconnect dialog at bounds=%s", b)
                return (cx, cy)
    return None


def _is_connected(xml: str) -> bool:
    """Check if any device is currently connected."""
    root = ET.fromstring(xml)
    for node in root.iter("node"):
        desc = node.attrib.get("content-desc", "") or ""
        if "\u5df2\u8fde\u63a5" in desc:
            b = node.attrib.get("bounds", "")
            m = re.match(r"\[(\d+),(\d+)\]\[(\d+),(\d+)\]", b)
            if m and int(m[2]) < 72:
                continue
            return True
    return False


def _run_single_iteration(
    device: u2.Device,
    app_config: AppConfig,
    metrics: MetricsRecorder,
    artifacts,
    iteration: int,
    target_device: str,
    package_name: str,
) -> None:
    settle = max(0.5, float(app_config.mobile.ui_settle_seconds))

    # Step 1: Navigate to the 设置 (Settings) tab where the connect card lives
    _mark(device, "1", "navigate to settings tab")
    for _ in range(5):
        if _has_desc(device, "\u8bbe\u7f6e"):
            break
        device.press("back")
        cancellable_sleep(0.8)
    device.click(SETTINGS_TAB_X, SETTINGS_TAB_Y)
    cancellable_sleep(settle)

    # Step 2: Handle already-connected state - disconnect first if needed
    if not _has_desc(device, "\u70b9\u51fb\u8fde\u63a5\u8bbe\u5907"):
        _mark(device, "2", "check if already connected")
        conn_tag = _find_connected_tag(device)
        if conn_tag is not None:
            _mark(device, "2", "already connected, disconnecting first")
            device.click(conn_tag[0], conn_tag[1])
            cancellable_sleep(2)
            confirm = _find_confirm_in_dialog(device)
            if confirm:
                device.click(confirm[0], confirm[1])
                _mark(device, "2", "existing connection disconnected")
                cancellable_sleep(2)
            device.click(SETTINGS_TAB_X, SETTINGS_TAB_Y)
            cancellable_sleep(settle)

    # Step 3: Click the connect device card to open the device list
    _mark(device, "3", "click connect card")
    _tap_desc(device, "\u70b9\u51fb\u8fde\u63a5\u8bbe\u5907", "uia_case6", "click_connect_card", metrics, iteration)
    cancellable_sleep(2)

    _dump_ui(device, artifacts.dumps_dir / f"iteration_{iteration:04d}_device_card.xml")

    # Step 4: Find the target device by swiping
    _mark(device, "4", f"find target device {target_device}")
    found = _find_device_by_name(device, target_device)
    if not found:
        _mark(device, "4", "FAILED - target not found")
        raise AssertionError(f"Target device '{target_device}' not found after swiping through all devices.")

    # Step 5: Click 连接 button at the bottom of the device card
    _mark(device, "5", "click connect button")
    connect_btn = _find_connect_button(device)
    if connect_btn is None:
        _mark(device, "5", "FAILED - connect button not found")
        raise AssertionError("Could not find 连接 button.")
    cx, cy = connect_btn
    device.click(cx, cy)
    _record_ui_metric(metrics, "uia_case6", f"click_connect_{iteration}", time.perf_counter(), True)
    cancellable_sleep(settle)

    # Step 6: Wait for connection
    _mark(device, "6", "wait for BLE connection")
    connected = _wait_for_connection(device, timeout=30)
    if not connected:
        _dump_ui(device, artifacts.dumps_dir / f"iteration_{iteration:04d}_connect_fail.xml")
        _save_screenshot(device, artifacts.screenshots_dir / f"iteration_{iteration:04d}_connect_fail.png")
        _mark(device, "6", "FAILED - connection timeout")
        raise AssertionError("Device connection failed or timed out.")

    _mark(device, "6", "connection established")
    metrics.record_event("uia_case6_connected", {"iteration": iteration, "device": target_device})

    # Step 7: Click 已连接 tag in upper-right to open disconnect dialog
    _mark(device, "7", "click connected tag")
    cancellable_sleep(1)
    conn_tag = _find_connected_tag(device)
    if conn_tag is None:
        _dump_ui(device, artifacts.dumps_dir / f"iteration_{iteration:04d}_no_connected_tag.xml")
        _save_screenshot(device, artifacts.screenshots_dir / f"iteration_{iteration:04d}_no_connected_tag.png")
        _mark(device, "7", "FAILED - connected tag not found")
        raise AssertionError("Could not find 已连接 tag after connection.")
    cx, cy = conn_tag
    device.click(cx, cy)
    _record_ui_metric(metrics, "uia_case6", f"click_connected_tag_{iteration}", time.perf_counter(), True)
    cancellable_sleep(2)

    # Step 8: Click 确认 in the disconnect dialog to confirm
    _mark(device, "8", "click confirm in disconnect dialog")
    confirm_btn = _find_confirm_in_dialog(device)
    if confirm_btn is None:
        _dump_ui(device, artifacts.dumps_dir / f"iteration_{iteration:04d}_no_confirm_btn.xml")
        _save_screenshot(device, artifacts.screenshots_dir / f"iteration_{iteration:04d}_no_confirm_btn.png")
        _mark(device, "8", "FAILED - confirm button not found")
        raise AssertionError("Could not find 确认 button in disconnect dialog.")
    dcx, dcy = confirm_btn
    device.click(dcx, dcy)
    _record_ui_metric(metrics, "uia_case6", f"click_disconnect_confirm_{iteration}", time.perf_counter(), True)
    cancellable_sleep(2)

    # Verify disconnected
    _mark(device, "9", "verify disconnection")
    verify_xml = device.dump_hierarchy(compressed=False)
    if "\u70b9\u51fb\u8fde\u63a5\u8bbe\u5907" in verify_xml:
        _mark(device, "9", "disconnect confirmed, back to connect card")
    else:
        _mark(device, "9", "WARNING - disconnect may have failed")
        _dump_ui(device, artifacts.dumps_dir / f"iteration_{iteration:04d}_disconnect_maybe_fail.xml")

    metrics.record_event("uia_case6_disconnected", {"iteration": iteration, "device": target_device})
    _mark(device, "done", f"iteration {iteration} completed")


def run_uia_case6(app_config: AppConfig, report_dir: str | Path) -> dict:
    artifacts = prepare_uia_case_artifacts(report_dir, "uia_case6")
    logger = get_logger("pocket_app_automation.uia_case6")
    metrics = MetricsRecorder(artifacts.report_dir)
    iterations = app_config.run.case6_iterations
    _TIMELINE.clear()
    logger.info("uia_case6 started")
    logger.info("uia_case6 iterations=%s", iterations)

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
            raise AssertionError("Unable to determine Android package name for UIA case6.")

        logcat = AndroidAppLogcatCapture(
            serial=device.serial,
            package_name=package_name,
            output_file=artifacts.app_logcat_file,
        )
        logcat.start()

        for index in range(iterations):
            raise_if_cancelled()
            iteration = index + 1
            logger.info("uia_case6 iteration=%s/%s target=%s", iteration, iterations, target_device)
            _run_single_iteration(device, app_config, metrics, artifacts, iteration, target_device, package_name)
            logger.info("uia_case6 iteration=%s/%s completed successfully", iteration, iterations)
            emit_case_event(
                ITERATION_PROGRESS,
                {"case": "case6", "iteration": iteration, "completed": iteration, "total": iterations, "ok": True},
            )
    except CaseCancelled as exc:
        exit_code = 2
        error_message = str(exc)
        metrics.record_event("uia_case6_cancelled", {"error": error_message})
        logger.info("uia_case6 cancelled")
    except Exception as exc:
        exit_code = 1
        error_message = str(exc)
        metrics.record_failure("uia_case6", "execution", {"error": error_message})
        logger.exception("uia_case6 failed")
    finally:
        if logcat is not None:
            logcat.stop()

    summary = metrics.build_summary("uia_case6", exit_code)
    report_html = _write_uia_case6_report(artifacts, summary, error_message)

    timeline_path = artifacts.report_dir / "timeline.jsonl"
    with timeline_path.open("w", encoding="utf-8") as f:
        for entry in _TIMELINE:
            f.write(json.dumps(entry, ensure_ascii=False) + "\n")
    logger.info("Timeline written to %s", timeline_path)

    return {
        "case": "uia_case6",
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


def _write_uia_case6_report(artifacts, summary: dict, error_message: str) -> Path:
    report_path = artifacts.report_dir / "report.html"
    html_content = f"""<!DOCTYPE html>
<html lang="zh-CN">
<head><meta charset="utf-8"><title>UIA Case6 Report</title>
<style>
body {{ font-family: 'Microsoft YaHei UI', sans-serif; margin: 20px; background: #f5f5f5; }}
h1 {{ color: #333; }}
table {{ border-collapse: collapse; width: 100%; background: #fff; }}
th, td {{ padding: 8px 12px; text-align: left; border-bottom: 1px solid #ddd; }}
th {{ background: #f0f0f0; }}
.error {{ color: #d32f2f; background: #ffebee; padding: 12px; border-radius: 4px; }}
</style></head>
<body>
<h1>UIA Case6 - Device Connection Test</h1>
{_build_summary_table_html(summary)}
{_build_error_html(error_message)}
{_build_artifacts_html(artifacts)}
</body></html>"""
    report_path.write_text(html_content, encoding="utf-8")
    return report_path


def _build_summary_table_html(summary: dict) -> str:
    rows = ""
    for k, v in summary.items():
        rows += f"<tr><td>{html.escape(str(k))}</td><td>{html.escape(str(v))}</td></tr>"
    return f"<table>{rows}</table>"


def _build_error_html(error_message: str) -> str:
    if not error_message:
        return ""
    return f'<div class="error"><strong>Error:</strong> {html.escape(error_message)}</div>'


def _build_artifacts_html(artifacts) -> str:
    dumps = list(artifacts.dumps_dir.glob("*.xml")) if artifacts.dumps_dir.exists() else []
    screenshots = list(artifacts.screenshots_dir.glob("*.png")) if artifacts.screenshots_dir.exists() else []
    parts = []
    if dumps:
        parts.append("<h3>UI Dumps</h3><ul>")
        for d in sorted(dumps):
            parts.append(f'<li><a href="{d.as_uri()}">{d.name}</a></li>')
        parts.append("</ul>")
    if screenshots:
        parts.append("<h3>Screenshots</h3>")
        for s in sorted(screenshots):
            parts.append(f'<img src="{s.as_uri()}" style="max-width:300px;margin:4px">')
    return "".join(parts)
