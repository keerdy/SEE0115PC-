from __future__ import annotations

import html
import re
import time
from pathlib import Path

import uiautomator2 as u2

from apptest.core.android_logcat import AndroidAppLogcatCapture
from apptest.core.config import AppConfig
from apptest.core.execution import CaseCancelled, cancellable_sleep, emit_case_event, raise_if_cancelled
from apptest.core.events import ITERATION_PROGRESS
from apptest.core.logging_utils import get_logger
from apptest.core.reporting import MetricsRecorder
from apptest.core.uia_artifacts import prepare_uia_case_artifacts
from apptest.mobile_uia.case1 import (
    UiNode,
    _click_by_label,
    _dump_nodes,
    _dump_ui,
    _record_ui_metric,
    _save_screenshot,
    _tap_bounds,
    _tap_desc,
    _extract_total_count,
)


LABEL_DEVICE = "\u8bbe\u5907"
LABEL_VIDEO = "\u89c6\u9891"
PREVIEW_TITLE = "\u4f4e\u6e05\u9884\u89c8"
SEEKBAR_CLASS = "android.widget.SeekBar"
NEXT_ACTION = "next"
PREV_ACTION = "prev"
MIN_VIDEO_CARDS = 2
SEEKBAR_PERCENT_PATTERN = re.compile(r"(\d+)%")
MIN_PREVIEW_SECONDS = 1.5
PLAYBACK_COMPLETE_PERCENT = 100


def run_uia_case2(app_config: AppConfig, report_dir: str | Path) -> dict:
    artifacts = prepare_uia_case_artifacts(report_dir, "uia_case2")
    logger = get_logger("pocket_app_automation.uia_case2")
    metrics = MetricsRecorder(artifacts.report_dir)
    iterations = app_config.run.case2_iterations
    logger.info("uia_case2 started")
    logger.info("uia_case2 iterations=%s", iterations)

    exit_code = 0
    error_message = ""
    logcat: AndroidAppLogcatCapture | None = None
    try:
        serial = app_config.mobile.android_serial.strip()
        device = u2.connect(serial) if serial else u2.connect()
        package_name = (
            app_config.mobile.android_package_name.strip()
            or device.app_current().get("package")
            or device.info.get("currentPackageName", "")
        )
        if not package_name:
            raise AssertionError("Unable to determine Android package name for UIA case2.")

        logcat = AndroidAppLogcatCapture(
            serial=device.serial,
            package_name=package_name,
            output_file=artifacts.app_logcat_file,
        )
        logcat.start()

        video_count = _prepare_preview_entry(device, app_config, metrics, artifacts, package_name)

        for index in range(iterations):
            raise_if_cancelled()
            iteration = index + 1
            logger.info("uia_case2 iteration=%s/%s previewing", iteration, iterations)
            _run_single_iteration(device, app_config, metrics, artifacts, iteration, iterations, video_count)
            logger.info("uia_case2 iteration=%s/%s completed successfully", iteration, iterations)
            emit_case_event(
                ITERATION_PROGRESS,
                {"case": "case2", "iteration": iteration, "completed": iteration, "total": iterations, "ok": True},
            )
    except CaseCancelled as exc:
        exit_code = 2
        error_message = str(exc)
        metrics.record_event("uia_case2_cancelled", {"error": error_message})
        logger.info("uia_case2 cancelled")
    except Exception as exc:  # noqa: BLE001
        exit_code = 1
        error_message = str(exc)
        metrics.record_failure("uia_case2", "execution", {"error": error_message})
        logger.exception("uia_case2 failed")
    finally:
        if logcat is not None:
            logcat.stop()

    summary = metrics.build_summary("uia_case2", exit_code)
    report_html = _write_uia_case2_report(artifacts, summary, error_message)
    return {
        "case": "uia_case2",
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


def _prepare_preview_entry(
    device: u2.Device,
    app_config: AppConfig,
    metrics: MetricsRecorder,
    artifacts,
    package_name: str,
) -> int:
    settle = max(0.5, float(app_config.mobile.ui_settle_seconds))
    device.app_start(package_name, stop=False)
    cancellable_sleep(settle)
    _ensure_device_video_list_page(device, metrics, settle)
    _dump_ui(device, artifacts.dumps_dir / "entry_device_video_list.xml")

    cards = _find_visible_video_cards(device)
    if len(cards) < MIN_VIDEO_CARDS:
        raise AssertionError(f"uia_case2 requires at least {MIN_VIDEO_CARDS} visible device videos, found {len(cards)}.")

    total_count = _extract_total_count(device) or len(cards)
    _tap_bounds(device, cards[0].bounds, "uia_case2", "open_first_device_video", metrics, 0)
    cancellable_sleep(max(1.0, settle))
    if not _wait_for_preview_page(device, timeout_seconds=8):
        _dump_ui(device, artifacts.dumps_dir / "entry_preview_failed.xml")
        _save_screenshot(device, artifacts.screenshots_dir / "entry_preview_failed.png")
        raise AssertionError("Failed to enter preview page for UIA case2.")
    _dump_ui(device, artifacts.dumps_dir / "entry_preview.xml")
    _save_screenshot(device, artifacts.screenshots_dir / "entry_preview.png")
    return max(MIN_VIDEO_CARDS, total_count)


def _ensure_device_video_list_page(device: u2.Device, metrics: MetricsRecorder, settle: float) -> None:
    for _ in range(4):
        if _is_preview_page(device):
            _exit_preview_page(device, metrics, 0)
            cancellable_sleep(max(0.3, settle))
        elif _has_album_tab(device):
            _open_album_tab_case2(device, metrics)
            cancellable_sleep(settle)

        if _is_device_disconnected_page(device):
            raise AssertionError("设备当前未连接，无法执行 UIA case2 预览测试。")

        _try_click_label(device, LABEL_DEVICE)
        cancellable_sleep(0.3)
        _try_click_label(device, LABEL_VIDEO)
        cancellable_sleep(settle)

        if _is_device_disconnected_page(device):
            raise AssertionError("设备当前未连接，无法执行 UIA case2 预览测试。")

        if _is_device_video_list_page(device) and _find_visible_video_cards(device):
            return

        cancellable_sleep(settle)

    raise AssertionError("无法进入设备视频列表页，请先确认设备已连接且设备相册中存在视频。")


def _run_single_iteration(
    device: u2.Device,
    app_config: AppConfig,
    metrics: MetricsRecorder,
    artifacts,
    iteration: int,
    total_iterations: int,
    video_count: int,
) -> None:
    wait_timeout = max(5, int(app_config.mobile.ui_wait_timeout_seconds))
    settle = max(0.5, float(app_config.mobile.ui_settle_seconds))
    if not _wait_for_preview_page(device, timeout_seconds=wait_timeout):
        _dump_ui(device, artifacts.dumps_dir / f"iteration_{iteration:04d}_preview_missing.xml")
        _save_screenshot(device, artifacts.screenshots_dir / f"iteration_{iteration:04d}_preview_missing.png")
        raise AssertionError(f"Preview page not ready at iteration {iteration}.")

    action = _navigation_action(iteration, video_count)
    preview_context = _build_preview_context(device, artifacts, iteration, action)
    _assert_preview_watchable(device, metrics, preview_context)
    preview_percent = _wait_for_preview_complete(device, metrics, artifacts, iteration, wait_timeout, action)
    metrics.record_event(
        "uia_case2_preview_verified",
        {
            "iteration": iteration,
            "seekbar_percent": preview_percent,
            "direction": action,
            "capture_file": preview_context["screenshot_file"],
        },
    )
    _record_ui_metric(metrics, "uia_case2", f"preview_verified_{iteration}", time.perf_counter(), True)

    cancellable_sleep(max(0.8, settle))
    if iteration >= total_iterations:
        _exit_preview_page(device, metrics, iteration)
        return

    _navigate_preview(device, metrics, artifacts, iteration, action)
    cancellable_sleep(max(1.0, settle))
    if not _wait_for_preview_page(device, timeout_seconds=wait_timeout):
        _dump_ui(device, artifacts.dumps_dir / f"iteration_{iteration:04d}_preview_lost.xml")
        _save_screenshot(device, artifacts.screenshots_dir / f"iteration_{iteration:04d}_preview_lost.png")
        raise AssertionError(f"Preview page lost after swipe at iteration {iteration}.")


def _find_visible_video_cards(device: u2.Device) -> list[UiNode]:
    nodes = _dump_nodes(device)
    cards = [
        node
        for node in nodes
        if node.clickable
        and node.long_clickable
        and node.bounds[1] > 350
        and node.bounds[1] < 1900
        and node.bounds[2] - node.bounds[0] > 200
    ]
    return sorted(cards, key=lambda item: (item.bounds[1], item.bounds[0]))


def _wait_for_preview_page(device: u2.Device, timeout_seconds: int) -> bool:
    deadline = time.time() + max(3, timeout_seconds)
    while time.time() < deadline:
        raise_if_cancelled()
        if _is_preview_page(device):
            return True
        cancellable_sleep(0.5)
    return False


def _is_preview_page(device: u2.Device) -> bool:
    for node in _dump_nodes(device):
        content = node.desc or node.text
        if PREVIEW_TITLE in content:
            return True
        if node.class_name == SEEKBAR_CLASS:
            return True
    return False


def _is_device_video_list_page(device: u2.Device) -> bool:
    labels = {node.desc or node.text for node in _dump_nodes(device)}
    return LABEL_DEVICE in labels and LABEL_VIDEO in labels


def _has_album_tab(device: u2.Device) -> bool:
    for node in _dump_nodes(device):
        content = node.desc or node.text
        if "相册" in content:
            return True
    return False


def _is_device_disconnected_page(device: u2.Device) -> bool:
    labels = {node.desc or node.text for node in _dump_nodes(device)}
    return "设备未连接" in labels or "连接设备" in labels


def _try_click_label(device: u2.Device, label: str) -> bool:
    return _click_by_label(device, label, timeout=1)


def _open_album_tab_case2(device: u2.Device, metrics: MetricsRecorder) -> None:
    start = time.perf_counter()
    if _is_album_tab_selected(device):
        _record_ui_metric(metrics, "uia_case2", "open_album_tab_0", start, True)
        return
    if _click_by_label(device, "相册", timeout=6):
        _record_ui_metric(metrics, "uia_case2", "open_album_tab_0", start, True)
        return
    _record_ui_metric(metrics, "uia_case2", "open_album_tab_0", start, False, error="album tab not found")
    raise AssertionError("Unable to find the album tab for UIA case2.")


def _is_album_tab_selected(device: u2.Device) -> bool:
    for node in _dump_nodes(device):
        content = node.desc or node.text
        if "相册" in content and node.selected:
            return True
    return False


def _extract_seekbar_percent(device: u2.Device) -> int:
    for node in _dump_nodes(device):
        if node.class_name != SEEKBAR_CLASS:
            continue
        content = node.desc or node.text
        match = SEEKBAR_PERCENT_PATTERN.search(content)
        if match:
            return int(match.group(1))
    return -1


def _build_preview_context(device: u2.Device, artifacts, iteration: int, action: str) -> dict[str, str]:
    screenshot_path = artifacts.screenshots_dir / f"iteration_{iteration:04d}_watch_check.png"
    dump_path = artifacts.dumps_dir / f"iteration_{iteration:04d}_watch_check.xml"
    _save_screenshot(device, screenshot_path)
    _dump_ui(device, dump_path)
    return {
        "iteration": str(iteration),
        "direction": action,
        "screenshot_file": screenshot_path.name,
        "screenshot_path": str(screenshot_path),
        "dump_path": str(dump_path),
    }


def _assert_preview_watchable(device: u2.Device, metrics: MetricsRecorder, context: dict[str, str]) -> None:
    iteration = int(context["iteration"])
    if not _is_preview_page(device):
        details = {
            "iteration": iteration,
            "direction": context["direction"],
            "reason": "预览页已丢失，视频无法观看",
            "screenshot_file": context["screenshot_file"],
            "screenshot_path": context["screenshot_path"],
            "dump_path": context["dump_path"],
        }
        metrics.record_failure("uia_case2", "preview_not_watchable", details)
        raise AssertionError(
            f"视频无法观看，已停止测试。轮次={iteration}，方向={context['direction']}，截图={context['screenshot_file']}，原因=预览页已丢失"
        )

    preview_percent = _extract_seekbar_percent(device)
    if preview_percent < 0:
        details = {
            "iteration": iteration,
            "direction": context["direction"],
            "reason": "未检测到播放进度条，视频可能异常或无法观看",
            "screenshot_file": context["screenshot_file"],
            "screenshot_path": context["screenshot_path"],
            "dump_path": context["dump_path"],
        }
        metrics.record_failure("uia_case2", "preview_not_watchable", details)
        raise AssertionError(
            f"视频无法观看，已停止测试。轮次={iteration}，方向={context['direction']}，截图={context['screenshot_file']}，原因=未检测到播放进度条"
        )

    page_texts = {node.desc or node.text for node in _dump_nodes(device) if (node.desc or node.text)}
    error_keywords = ["播放失败", "加载失败", "视频异常", "无法播放", "绿屏"]
    matched_error = next((word for word in error_keywords if word in page_texts), "")
    if matched_error:
        details = {
            "iteration": iteration,
            "direction": context["direction"],
            "reason": f"页面出现异常提示: {matched_error}",
            "screenshot_file": context["screenshot_file"],
            "screenshot_path": context["screenshot_path"],
            "dump_path": context["dump_path"],
        }
        metrics.record_failure("uia_case2", "preview_not_watchable", details)
        raise AssertionError(
            f"视频无法观看，已停止测试。轮次={iteration}，方向={context['direction']}，截图={context['screenshot_file']}，原因={matched_error}"
        )


def _wait_for_preview_complete(
    device: u2.Device,
    metrics: MetricsRecorder,
    artifacts,
    iteration: int,
    timeout_seconds: int,
    action: str,
) -> int:
    start = time.perf_counter()
    deadline = time.time() + max(3, timeout_seconds)
    first_percent = _extract_seekbar_percent(device)
    latest_percent = first_percent
    missing_count = 0

    if first_percent >= PLAYBACK_COMPLETE_PERCENT:
        cancellable_sleep(MIN_PREVIEW_SECONDS)
        _record_ui_metric(metrics, "uia_case2", f"wait_playback_complete_{iteration}", start, True)
        return first_percent

    while time.time() < deadline:
        raise_if_cancelled()
        latest_percent = _extract_seekbar_percent(device)
        if latest_percent >= 0:
            missing_count = 0
        else:
            missing_count += 1
            if missing_count >= 3:
                context = _build_preview_context(device, artifacts, iteration, action)
                _assert_preview_watchable(device, metrics, context)

        if latest_percent >= PLAYBACK_COMPLETE_PERCENT:
            _record_ui_metric(metrics, "uia_case2", f"wait_playback_complete_{iteration}", start, True)
            return latest_percent
        cancellable_sleep(0.8)

    context = _build_preview_context(device, artifacts, iteration, action)
    _assert_preview_watchable(device, metrics, context)
    _record_ui_metric(
        metrics,
        "uia_case2",
        f"wait_playback_complete_{iteration}",
        start,
        False,
        error=f"seekbar did not reach {PLAYBACK_COMPLETE_PERCENT}% (last={latest_percent})",
    )
    raise AssertionError(f"Preview playback did not complete for iteration {iteration}. last_percent={latest_percent}")


def _navigation_action(iteration: int, video_count: int) -> str:
    if video_count <= 1:
        return NEXT_ACTION
    direction_span = video_count - 1
    cycle_position = (iteration - 1) % (direction_span * 2)
    return NEXT_ACTION if cycle_position < direction_span else PREV_ACTION


def _navigate_preview(device: u2.Device, metrics: MetricsRecorder, artifacts, iteration: int, action: str) -> None:
    width, height = device.window_size()
    center_y = int(height * 0.52)
    start_x = int(width * 0.25)
    end_x = int(width * 0.75)

    if action == NEXT_ACTION:
        # App 实测行为：右滑进入下一个视频。
        swipe_from = (start_x, center_y)
        swipe_to = (end_x, center_y)
        step_name = f"swipe_next_{iteration}"
    else:
        # App 实测行为：左滑返回上一个视频。
        swipe_from = (end_x, center_y)
        swipe_to = (start_x, center_y)
        step_name = f"swipe_prev_{iteration}"

    start = time.perf_counter()
    device.swipe(swipe_from[0], swipe_from[1], swipe_to[0], swipe_to[1], 0.15)
    elapsed_ms = (time.perf_counter() - start) * 1000
    metrics.record_metric("uia_case2", step_name, "UI", action, 0, elapsed_ms, ok=True)
    metrics.record_event(
        "uia_case2_navigation",
        {
            "iteration": iteration,
            "action": action,
        },
    )
    if iteration <= 3:
        _save_screenshot(device, artifacts.screenshots_dir / f"iteration_{iteration:04d}_{action}.png")


def _exit_preview_page(device: u2.Device, metrics: MetricsRecorder, iteration: int) -> None:
    start = time.perf_counter()
    device.press("back")
    cancellable_sleep(0.8)
    if _is_preview_page(device):
        width, height = device.window_size()
        device.click(width // 2, height // 2)
        cancellable_sleep(0.3)
        device.press("back")
        cancellable_sleep(0.8)
    ok = not _is_preview_page(device)
    _record_ui_metric(
        metrics,
        "uia_case2",
        f"exit_preview_{iteration}",
        start,
        ok,
        error="" if ok else "preview page still active after back",
    )
    if not ok:
        raise AssertionError("Unable to exit the preview page for UIA case2.")


def _write_uia_case2_report(artifacts, summary: dict, error_message: str) -> Path:
    report_path = artifacts.report_dir / "report.html"
    screenshots = sorted(path.name for path in artifacts.screenshots_dir.glob("*"))
    dumps = sorted(path.name for path in artifacts.dumps_dir.glob("*"))
    summary_rows = "".join(
        f"<tr><th>{html.escape(str(key))}</th><td>{html.escape(str(value))}</td></tr>"
        for key, value in summary.items()
    )
    screenshot_rows = "".join(f"<li>{html.escape(item)}</li>" for item in screenshots) or "<li>无截图</li>"
    dump_rows = "".join(f"<li>{html.escape(item)}</li>" for item in dumps) or "<li>无 Dump</li>"
    error_block = (
        f'<div class="card error"><h2>错误信息</h2><pre>{html.escape(error_message)}</pre></div>'
        if error_message
        else ""
    )
    report_html = f"""<!doctype html>
<html lang="zh-CN">
<head>
  <meta charset="utf-8">
  <title>UIA Case2 测试报告</title>
  <style>
    body {{ font-family: "Microsoft YaHei", sans-serif; background: #0f172a; color: #e2e8f0; margin: 0; padding: 24px; }}
    h1 {{ margin-top: 0; }}
    .grid {{ display: grid; grid-template-columns: 2fr 1fr; gap: 18px; }}
    .card {{ background: #111827; border: 1px solid #334155; border-radius: 14px; padding: 16px; box-shadow: 0 8px 24px rgba(0,0,0,0.25); }}
    .error {{ border-color: #ef4444; }}
    table {{ width: 100%; border-collapse: collapse; }}
    th, td {{ text-align: left; padding: 8px 10px; border-bottom: 1px solid #1f2937; }}
    th {{ width: 34%; color: #93c5fd; }}
    ul {{ margin: 0; padding-left: 18px; }}
    li {{ line-height: 1.6; }}
    pre {{ white-space: pre-wrap; word-break: break-word; }}
  </style>
</head>
<body>
  <h1>UIA Case2 测试报告</h1>
  <div class="grid">
    <div class="card">
      <h2>摘要</h2>
      <table>
        <tr><th>指标</th><th>值</th></tr>
        {summary_rows}
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
    {error_block}
  </div>
</body>
</html>
"""
    report_path.write_text(report_html, encoding="utf-8")
    return report_path
