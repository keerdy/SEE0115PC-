from __future__ import annotations

import html
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


COUNT_PATTERN = re.compile(r"共\s*(\d+)\s*个作品")
ALLOW_BUTTON_LABELS = [
    "允许",
    "始终允许",
    "仅在使用中允许",
    "仅在使用该应用时允许",
    "使用期间允许",
]


@dataclass
class UiNode:
    text: str
    desc: str
    class_name: str
    clickable: bool
    long_clickable: bool
    selected: bool
    bounds: tuple[int, int, int, int]

    @property
    def center(self) -> tuple[int, int]:
        left, top, right, bottom = self.bounds
        return ((left + right) // 2, (top + bottom) // 2)


def run_uia_case1(app_config: AppConfig, report_dir: str | Path) -> dict:
    artifacts = prepare_uia_case_artifacts(report_dir, "uia_case1")
    logger = get_logger("pocket_app_automation.uia_case1")
    metrics = MetricsRecorder(artifacts.report_dir)
    iterations = app_config.run.case1_iterations
    logger.info("uia_case1 started")
    logger.info("uia_case1 iterations=%s", iterations)

    exit_code = 0
    error_message = ""
    logcat: AndroidAppLogcatCapture | None = None
    try:
        serial = app_config.mobile.android_serial.strip()
        device = u2.connect(serial) if serial else u2.connect()
        package_name = app_config.mobile.android_package_name.strip() or device.app_current().get("package") or device.info.get("currentPackageName", "")
        if not package_name:
            raise AssertionError("Unable to determine Android package name for UIA case1.")

        logcat = AndroidAppLogcatCapture(
            serial=device.serial,
            package_name=package_name,
            output_file=artifacts.app_logcat_file,
        )
        logcat.start()

        for index in range(iterations):
            raise_if_cancelled()
            iteration = index + 1
            logger.info("uia_case1 iteration=%s/%s started", iteration, iterations)
            _run_single_iteration(device, app_config, metrics, artifacts, iteration, package_name)
            logger.info("uia_case1 iteration=%s/%s completed successfully", iteration, iterations)
            emit_case_event(
                ITERATION_PROGRESS,
                {"case": "case1", "iteration": iteration, "completed": iteration, "total": iterations, "ok": True},
            )
    except CaseCancelled as exc:
        exit_code = 2
        error_message = str(exc)
        metrics.record_event("uia_case1_cancelled", {"error": error_message})
        logger.info("uia_case1 cancelled")
    except Exception as exc:  # noqa: BLE001
        exit_code = 1
        error_message = str(exc)
        metrics.record_failure("uia_case1", "execution", {"error": error_message})
        logger.exception("uia_case1 failed")
    finally:
        if logcat is not None:
            logcat.stop()

    summary = metrics.build_summary("uia_case1", exit_code)
    report_html = _write_uia_case_report(artifacts, summary, error_message)
    return {
        "case": "uia_case1",
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


def _run_single_iteration(
    device: u2.Device,
    app_config: AppConfig,
    metrics: MetricsRecorder,
    artifacts,
    iteration: int,
    package_name: str,
) -> None:
    settle = max(0.5, float(app_config.mobile.ui_settle_seconds))
    _open_album_tab(device, metrics, iteration)
    _tap_desc(device, "本地", "uia_case1", "open_local_tab_before_download", metrics, iteration)
    baseline_local_count = _extract_total_count(device)
    metrics.record_event("uia_case1_local_baseline", {"iteration": iteration, "baseline_local_count": baseline_local_count})
    _tap_desc(device, "设备", "uia_case1", "open_device_tab", metrics, iteration)
    disconnected = device(description="设备未连接")
    if disconnected.exists(timeout=2):
        raise AssertionError('Device tab shows "设备未连接" — camera is not connected. Please connect the device WiFi and retry.')
    _tap_desc(device, "视频", "uia_case1", "open_video_filter", metrics, iteration)

    _dump_ui(device, artifacts.dumps_dir / f"iteration_{iteration:04d}_device_video.xml")
    first_video_download_icon = _find_first_video_download_icon(device)
    _tap_bounds(device, first_video_download_icon.bounds, "uia_case1", "download_first_video", metrics, iteration)
    cancellable_sleep(settle)

    _dump_ui(device, artifacts.dumps_dir / f"iteration_{iteration:04d}_download_started.xml")

    _tap_desc(device, "本地", "uia_case1", "switch_to_local_after_download", metrics, iteration)
    local_count_before_delete = _wait_for_local_video_download(
        device,
        baseline_local_count,
        app_config.mobile.ui_wait_timeout_seconds,
        artifacts,
        iteration,
    )
    _tap_top_right_action(device, "uia_case1", "enter_local_multi_select", metrics, iteration)
    cancellable_sleep(settle)

    checkbox = _find_first_local_checkbox(device)
    _tap_bounds(device, checkbox.bounds, "uia_case1", "select_first_local_video", metrics, iteration)
    cancellable_sleep(settle)

    selected_count = _extract_selected_count(device)
    if selected_count != 1:
        raise AssertionError(f"Expected 1 selected local video before delete, got {selected_count}.")

    delete_icon = _find_bottom_right_delete_icon(device)
    _tap_bounds(device, delete_icon.bounds, "uia_case1", "tap_delete_icon", metrics, iteration)
    cancellable_sleep(settle)
    _confirm_delete_if_present(device, app_config.mobile.delete_confirm_text, metrics, iteration)
    _allow_system_popup_if_present(device, metrics, iteration)

    if not _wait_for_delete_success(device, baseline_local_count, app_config.mobile.ui_wait_timeout_seconds):
        _dump_ui(device, artifacts.dumps_dir / f"iteration_{iteration:04d}_delete_failed.xml")
        _save_screenshot(device, artifacts.screenshots_dir / f"iteration_{iteration:04d}_delete_failed.png")
        raise AssertionError(
            "Local delete verification failed. "
            f"Expected local count to return to {baseline_local_count} after delete."
        )

    metrics.record_event(
        "uia_case1_delete_verified",
        {
            "iteration": iteration,
            "baseline_local_count": baseline_local_count,
            "local_count_before_delete": local_count_before_delete,
        },
    )


def _open_album_tab(device: u2.Device, metrics: MetricsRecorder, iteration: int) -> None:
    start = time.perf_counter()
    if _is_tab_selected(device, "相册"):
        _record_ui_metric(metrics, "uia_case1", f"open_album_tab_{iteration}", start, True)
        return
    if _click_by_label(device, "相册", timeout=8):
        _record_ui_metric(metrics, "uia_case1", f"open_album_tab_{iteration}", start, True)
        return
    _record_ui_metric(
        metrics,
        "uia_case1",
        f"open_album_tab_{iteration}",
        start,
        False,
        error="album tab not found",
    )
    raise AssertionError("未找到“相册”入口，已停止执行，避免误点到其他 App。")


def _tap_desc(device: u2.Device, desc: str, scenario: str, step: str, metrics: MetricsRecorder, iteration: int) -> None:
    start = time.perf_counter()
    ok = _click_by_label(device, desc, timeout=5)
    if not ok:
        _record_ui_metric(metrics, scenario, f"{step}_{iteration}", start, False, error=f"desc not found: {desc}")
        raise AssertionError(f"UI node not found by description: {desc}")
    _record_ui_metric(metrics, scenario, f"{step}_{iteration}", start, True)


def _tap_bounds(
    device: u2.Device,
    bounds: tuple[int, int, int, int],
    scenario: str,
    step: str,
    metrics: MetricsRecorder,
    iteration: int,
) -> None:
    start = time.perf_counter()
    left, top, right, bottom = bounds
    device.click((left + right) // 2, (top + bottom) // 2)
    _record_ui_metric(metrics, scenario, f"{step}_{iteration}", start, True)


def _tap_top_right_action(device: u2.Device, scenario: str, step: str, metrics: MetricsRecorder, iteration: int) -> None:
    xml = device.dump_hierarchy(compressed=False)
    root = ET.fromstring(xml)
    for node in root.iter("node"):
        cls = node.attrib.get("class", "")
        clickable = node.attrib.get("clickable", "false") == "true"
        desc = node.attrib.get("content-desc", "") or ""
        naf = node.attrib.get("NAF", "false") == "true"
        if clickable and cls.endswith("ImageView") and not desc:
            _tap_bounds(device, _parse_bounds(node.attrib.get("bounds", "[0,0][0,0]")), scenario, step, metrics, iteration)
            return
    raise AssertionError("Unable to find top-right action icon.")


def _find_first_video_download_icon(device: u2.Device) -> UiNode:
    xml = device.dump_hierarchy(compressed=False)
    root = ET.fromstring(xml)

    def _to_node(elem) -> UiNode:
        return UiNode(
            text=elem.attrib.get("text", ""),
            desc=elem.attrib.get("content-desc", ""),
            class_name=elem.attrib.get("class", ""),
            clickable=elem.attrib.get("clickable", "false") == "true",
            long_clickable=elem.attrib.get("long-clickable", "false") == "true",
            selected=elem.attrib.get("selected", "false") == "true",
            bounds=_parse_bounds(elem.attrib.get("bounds", "[0,0][0,0]")),
        )

    def _walk(parent_elem):
        for child in parent_elem:
            cls = child.attrib.get("class", "")
            long_clickable = child.attrib.get("long-clickable", "false") == "true"
            if long_clickable and cls == "android.view.View":
                for grandchild in child:
                    gcls = grandchild.attrib.get("class", "")
                    gclickable = grandchild.attrib.get("clickable", "false") == "true"
                    if gcls.endswith("ImageView") and gclickable:
                        return _to_node(grandchild)
            result = _walk(child)
            if result is not None:
                return result
        return None

    icon = _walk(root)
    if icon is None:
        raise AssertionError("Unable to find download icon on device video.")
    return icon


def _click_by_label(device: u2.Device, label: str, timeout: int = 5) -> bool:
    selectors = [
        device(description=label),
        device(text=label),
        device(descriptionContains=label),
        device(textContains=label),
    ]
    for selector in selectors:
        if selector.exists(timeout=timeout):
            selector.click()
            return True
    return False


def _is_tab_selected(device: u2.Device, label: str) -> bool:
    for node in _dump_nodes(device):
        content = node.desc or node.text
        if label in content and node.selected:
            return True
    return False


def _find_first_local_checkbox(device: u2.Device) -> UiNode:
    xml = device.dump_hierarchy(compressed=False)
    root = ET.fromstring(xml)
    for node in root.iter("node"):
        cls = node.attrib.get("class", "")
        naf = node.attrib.get("NAF", "false") == "true"
        clickable = node.attrib.get("clickable", "false") == "true"
        if naf and clickable and cls.endswith("ImageView"):
            return UiNode(
                text=node.attrib.get("text", ""),
                desc=node.attrib.get("content-desc", ""),
                class_name=cls,
                clickable=True,
                long_clickable=node.attrib.get("long-clickable", "false") == "true",
                selected=node.attrib.get("selected", "false") == "true",
                bounds=_parse_bounds(node.attrib.get("bounds", "[0,0][0,0]")),
            )
    raise AssertionError("Unable to find the first local selection checkbox.")


def _find_bottom_right_delete_icon(device: u2.Device) -> UiNode:
    xml = device.dump_hierarchy(compressed=False)
    root = ET.fromstring(xml)
    candidate = None
    for node in root.iter("node"):
        cls = node.attrib.get("class", "")
        if not cls.endswith("ImageView"):
            continue
        if node.attrib.get("NAF", "false") != "true":
            continue
        if node.attrib.get("clickable", "false") != "true":
            continue
        desc = node.attrib.get("content-desc", "") or ""
        if desc:
            continue
        candidate = node
    if candidate is None:
        raise AssertionError("Unable to find bottom-right delete icon.")
    return UiNode(
        text=candidate.attrib.get("text", ""),
        desc=candidate.attrib.get("content-desc", ""),
        class_name=candidate.attrib.get("class", ""),
        clickable=True,
        long_clickable=candidate.attrib.get("long-clickable", "false") == "true",
        selected=candidate.attrib.get("selected", "false") == "true",
        bounds=_parse_bounds(candidate.attrib["bounds"]),
    )


def _confirm_delete_if_present(device: u2.Device, confirm_text: str, metrics: MetricsRecorder, iteration: int) -> None:
    start = time.perf_counter()
    button = device(text=confirm_text)
    if button.exists(timeout=3):
        button.click()
        _record_ui_metric(metrics, "uia_case1", f"confirm_delete_{iteration}", start, True)
        return
    desc_button = device(description=confirm_text)
    if desc_button.exists(timeout=1):
        desc_button.click()
        _record_ui_metric(metrics, "uia_case1", f"confirm_delete_{iteration}", start, True)
        return
    _record_ui_metric(metrics, "uia_case1", f"confirm_delete_{iteration}", start, True)


def _allow_system_popup_if_present(device: u2.Device, metrics: MetricsRecorder, iteration: int) -> None:
    start = time.perf_counter()
    deadline = time.time() + 3
    clicked_label = ""
    while time.time() < deadline and not clicked_label:
        for label in ALLOW_BUTTON_LABELS:
            if _click_by_label(device, label, timeout=1):
                clicked_label = label
                break
        if not clicked_label:
            cancellable_sleep(0.3)

    if clicked_label:
        metrics.record_event(
            "uia_case1_system_allow_clicked",
            {"iteration": iteration, "button": clicked_label},
        )
    _record_ui_metric(metrics, "uia_case1", f"optional_allow_popup_{iteration}", start, True)


def _wait_for_local_video_download(
    device: u2.Device,
    baseline_local_count: int,
    timeout_seconds: int,
    artifacts,
    iteration: int,
) -> int:
    deadline = time.time() + max(5, timeout_seconds)
    while time.time() < deadline:
        count = _extract_total_count(device)
        if count > baseline_local_count:
            return count
        cancellable_sleep(1)
    _dump_ui(device, artifacts.dumps_dir / f"iteration_{iteration:04d}_wait_local_timeout.xml")
    _save_screenshot(device, artifacts.screenshots_dir / f"iteration_{iteration:04d}_wait_local_timeout.png")
    raise AssertionError(
        "Downloaded local video did not appear. "
        f"Baseline count={baseline_local_count}, latest count={_extract_total_count(device)}."
    )


def _wait_for_delete_success(device: u2.Device, expected_count_after_delete: int, timeout_seconds: int) -> bool:
    deadline = time.time() + max(5, timeout_seconds)
    while time.time() < deadline:
        total = _extract_total_count(device)
        selected = _extract_selected_count(device)
        if total == expected_count_after_delete and selected == 0:
            return True
        cancellable_sleep(1)
    return False


def _extract_total_count(device: u2.Device) -> int:
    for node in _dump_nodes(device):
        text = node.desc or node.text
        match = COUNT_PATTERN.search(text)
        if match:
            return int(match.group(1))
    return 0


def _extract_selected_count(device: u2.Device) -> int:
    for node in _dump_nodes(device):
        text = node.desc or node.text
        if text.startswith("已选") and text.endswith("项"):
            digits = re.findall(r"\d+", text)
            if digits:
                return int(digits[0])
    return 0


def _dump_ui(device: u2.Device, path: Path) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(device.dump_hierarchy(compressed=False), encoding="utf-8")


def _save_screenshot(device: u2.Device, path: Path) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    device.screenshot(str(path))


def _dump_nodes(device: u2.Device) -> list[UiNode]:
    xml = device.dump_hierarchy(compressed=False)
    root = ET.fromstring(xml)
    nodes: list[UiNode] = []
    for raw in root.iter("node"):
        nodes.append(
            UiNode(
                text=raw.attrib.get("text", ""),
                desc=raw.attrib.get("content-desc", ""),
                class_name=raw.attrib.get("class", ""),
                clickable=raw.attrib.get("clickable", "false") == "true",
                long_clickable=raw.attrib.get("long-clickable", "false") == "true",
                selected=raw.attrib.get("selected", "false") == "true",
                bounds=_parse_bounds(raw.attrib.get("bounds", "[0,0][0,0]")),
            )
        )
    return nodes


def _parse_bounds(raw: str) -> tuple[int, int, int, int]:
    digits = [int(part) for part in re.findall(r"\d+", raw)]
    if len(digits) != 4:
        return (0, 0, 0, 0)
    return tuple(digits)  # type: ignore[return-value]


def _record_ui_metric(
    metrics: MetricsRecorder,
    scenario: str,
    step: str,
    started_at: float,
    ok: bool,
    error: str = "",
) -> None:
    elapsed = (time.perf_counter() - started_at) * 1000
    metrics.record_metric(
        scenario,
        step,
        "UI",
        step,
        0 if ok else 1,
        elapsed,
        ok=ok,
        error=error,
    )


def _write_uia_case_report(artifacts, summary: dict, error_message: str) -> Path:
    report_path = artifacts.report_dir / "report.html"
    screenshots = sorted(path.name for path in artifacts.screenshots_dir.glob("*"))
    dumps = sorted(path.name for path in artifacts.dumps_dir.glob("*"))
    summary_rows = "".join(
        f"<tr><th>{html.escape(str(key))}</th><td>{html.escape(str(value))}</td></tr>"
        for key, value in summary.items()
    )
    screenshot_rows = "".join(f"<li>{html.escape(item)}</li>" for item in screenshots) or "<li>无</li>"
    dump_rows = "".join(f"<li>{html.escape(item)}</li>" for item in dumps) or "<li>无</li>"
    error_block = (
        f'<div class="card error"><h2>错误信息</h2><pre>{html.escape(error_message)}</pre></div>'
        if error_message
        else ""
    )
    report_html = f"""<!doctype html>
<html lang="zh-CN">
<head>
  <meta charset="utf-8">
  <title>UIA Case1 测试报告</title>
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
  <div class="card">
    <h1>UIA Case1 测试报告</h1>
    <table>{summary_rows}</table>
  </div>
  {error_block}
  <div class="card">
    <h2>日志与产物</h2>
    <table>
      <tr><th>脚本日志</th><td><code>{html.escape(str(artifacts.script_log_file))}</code></td></tr>
      <tr><th>App 日志</th><td><code>{html.escape(str(artifacts.app_logcat_file))}</code></td></tr>
      <tr><th>UI Dump 目录</th><td><code>{html.escape(str(artifacts.dumps_dir))}</code></td></tr>
      <tr><th>截图目录</th><td><code>{html.escape(str(artifacts.screenshots_dir))}</code></td></tr>
    </table>
  </div>
  <div class="card">
    <h2>截图列表</h2>
    <ul>{screenshot_rows}</ul>
  </div>
  <div class="card">
    <h2>UI Dump 列表</h2>
    <ul>{dump_rows}</ul>
  </div>
</body>
</html>"""
    report_path.write_text(report_html, encoding="utf-8")
    return report_path
