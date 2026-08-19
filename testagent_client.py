#!/usr/bin/env python3
"""PC-side client for Pocket TestAgent.

Protocol: 4-byte little-endian length prefix followed by a JSON body.
"""

from __future__ import annotations

import argparse
import datetime as _dt
import glob
import html as _html
import json
import os
import posixpath
import secrets
import socket
import sys
import time
from typing import Any, Dict, Optional

from testagent.catalog import FALLBACK_CASES, case_titles
from testagent.device import (
    DEFAULT_DEVICE_IP,
    auto_discover_devices,
    configure_device,
    device_ip_for_pc,
    probe_device_with_reconnect,
    reboot_device,
    request_device,
    set_device_ip,
)
from testagent.protocol import (
    DEFAULT_HOST,
    DEFAULT_PORT,
    DEFAULT_TIMEOUT,
    TestAgentClient,
    TestAgentError,
)
from testagent.status import extract_progress, is_terminal_status
from testagent.app_paths import defects_dir
from testagent.crash_export import export_crash_files


CASE_TITLES_BY_SUITE: Dict[str, list[str]] = {
    suite: case_titles(FALLBACK_CASES, suite) for suite in FALLBACK_CASES
}
CASE_TITLES_BY_SUITE["stress_test"] = ["1 - 随机 UI 压力测试 x5000"]
STABLE_CASE_TITLES = CASE_TITLES_BY_SUITE["stable_test"]
BUG_CASE_TITLES = CASE_TITLES_BY_SUITE["bug_test"]
CASE_TITLES = STABLE_CASE_TITLES
START_CONFIRM_TIMEOUT = 10.0

DEFECT_LABELS: Dict[str, str] = {
    "none": "无",
    "connection_lost": "连接丢失",
    "watch_timeout": "轮询超时",
    "ui_bridge_unavailable": "UI 桥接不可用",
    "command_rejected": "命令被拒绝",
    "case_failed": "用例失败",
    "case_error": "用例异常",
    "crash": "崩溃",
    "reboot_or_disconnect": "重启或断开",
}

STATUS_LABELS: Dict[str, str] = {
    "unknown": "未知",
    "running": "运行中",
    "waiting": "等待中",
    "finished": "已完成",
    "failed": "失败",
    "error": "错误",
}


def print_json(data: Dict[str, Any]) -> None:
    print(json.dumps(data, ensure_ascii=False, separators=(",", ":")))


def make_run_command(
    suite: str, case_id: int, *, confirm_risk: bool = False,
) -> Dict[str, Any]:
    return {
        "cmd": "run_case",
        "suite": suite,
        "case_id": case_id,
        "confirm_risk": confirm_risk,
    }


def is_ambiguous_start_reply(reply: Dict[str, Any]) -> bool:
    """A timed-out start may still have reached the device UI queue."""
    return reply.get("cmd") == "run_case" and reply.get("code") == -10


def is_current_case_running(status: Dict[str, Any], case_id: int, suite: str) -> bool:
    return (
        status.get("status") == "running"
        and status.get("current_case_id") == case_id
        and status.get("current_suite") == suite
    )


def resolve_device_host(
    device: str | None, host: str | None, recorded_host: str | None = None,
) -> str:
    return device or host or recorded_host or DEFAULT_HOST


def wait_for_terminal_state(
    client: TestAgentClient, case_id: int, timeout: float, suite: str = "stable_test",
) -> Dict[str, Any]:
    deadline = time.monotonic() + timeout
    last_status: Dict[str, Any] = {}
    consecutive_errors = 0
    while time.monotonic() < deadline:
        try:
            status = client.request({"cmd": "get_case_status"})
            consecutive_errors = 0
        except (OSError, TestAgentError) as exc:
            consecutive_errors += 1
            if consecutive_errors >= 3:
                raise TestAgentError(
                    f"case {case_id} polling failed after {consecutive_errors} errors: {exc}; last={last_status}"
                ) from exc
            time.sleep(0.5)
            continue
        last_status = status
        if is_terminal_status(status, case_id, suite):
            return status
        time.sleep(0.5)
    raise TestAgentError(f"case {case_id} did not reach terminal state before timeout; last={last_status}")


def make_record_path(
    record_dir: str, case_id: int, final_status: str, suite: str = "stable_test",
) -> str:
    ts = _dt.datetime.now().strftime("%Y%m%d_%H%M%S_%f")
    os.makedirs(record_dir, exist_ok=True)
    return os.path.join(record_dir, f"{suite}_case{case_id:02d}_{final_status}_{ts}.json")


def classify_defect(final_status: Dict[str, Any]) -> str:
    status = final_status.get("status")
    code = final_status.get("code")
    msg = str(final_status.get("last_msg", final_status.get("msg", ""))).lower()
    if code == -20:
        return "connection_lost"
    if code == -21:
        return "watch_timeout"
    if code == -10:
        return "ui_bridge_unavailable"
    if isinstance(code, int) and code < 0:
        return "command_rejected"
    if status == "failed":
        return "case_failed"
    if status == "error":
        return "case_error"
    if "crash" in msg:
        return "crash"
    if "reboot" in msg or "disconnect" in msg:
        return "reboot_or_disconnect"
    return "none"


def run_record(args: argparse.Namespace) -> Dict[str, Any]:
    events = []
    started_at = _dt.datetime.now().isoformat(timespec="seconds")
    final_status: Dict[str, Any] = {}
    deadline = time.monotonic() + args.wait_timeout
    start_confirmation_deadline = 0.0
    host = resolve_device_host(args.device, args.host)

    try:
        probe_device_with_reconnect(host, args.pc_ip, port=args.port, timeout=args.timeout)
        with TestAgentClient(host, args.port, args.timeout, source_host=args.pc_ip) as client:
            run_reply = client.request(make_run_command(
                args.suite, args.case_id, confirm_risk=args.confirm_risk,
            ))
            if is_ambiguous_start_reply(run_reply):
                start_confirmation_deadline = time.monotonic() + START_CONFIRM_TIMEOUT
            else:
                events.append({"time": _dt.datetime.now().isoformat(timespec="milliseconds"), "event": run_reply})
                print_json(run_reply)
            code = run_reply.get("code")
            if isinstance(code, int) and code != 0 and not is_ambiguous_start_reply(run_reply):
                final_status = run_reply

        if not final_status:
            with TestAgentClient(host, args.port, args.timeout, source_host=args.pc_ip) as watcher:
                watcher.send({"cmd": "watch_case_status", "interval_ms": args.interval_ms})
                assert watcher.sock is not None, "watcher socket not connected"
                watcher.sock.settimeout(1.0)
                code_minus10_count = 0
                while time.monotonic() < deadline:
                    if start_confirmation_deadline and time.monotonic() >= start_confirmation_deadline:
                        final_status = {
                            "cmd": "run_case",
                            "code": -10,
                            "status": "error",
                            "suite": args.suite,
                            "case_id": args.case_id,
                            "last_msg": f"case start was not confirmed within {START_CONFIRM_TIMEOUT:.0f} seconds",
                        }
                        events.append({"time": _dt.datetime.now().isoformat(timespec="milliseconds"), "event": final_status})
                        print_json(final_status)
                        break
                    try:
                        event = watcher.recv()
                    except socket.timeout:
                        continue
                    events.append({"time": _dt.datetime.now().isoformat(timespec="milliseconds"), "event": event})
                    print_json(event)
                    if not isinstance(event, dict):
                        continue
                    status = event.get("status") if event.get("cmd") == "case_status_event" else event
                    if isinstance(status, dict):
                        if start_confirmation_deadline and is_current_case_running(
                            status, args.case_id, args.suite,
                        ):
                            start_confirmation_deadline = 0.0
                        if status.get("code") == -10:
                            code_minus10_count += 1
                            if code_minus10_count > 60:
                                final_status = status
                                final_status["_code_minus10_exhausted"] = True
                                break
                            continue
                        code_minus10_count = 0
                        if is_terminal_status(status, args.case_id, args.suite):
                            final_status = status
                            break
    except (OSError, TestAgentError, json.JSONDecodeError) as exc:
        final_status = {
            "cmd": "get_case_status",
            "code": -20,
            "status": "error",
            "last_suite": args.suite,
            "last_case_id": args.case_id,
            "last_msg": f"connection lost: {exc}",
        }
        events.append({"time": _dt.datetime.now().isoformat(timespec="milliseconds"), "event": final_status})

    if not final_status:
        with TestAgentClient(host, args.port, args.timeout, source_host=args.pc_ip) as client:
            try:
                final_status = client.request({"cmd": "get_case_status"})
            except (OSError, TestAgentError, json.JSONDecodeError) as exc:
                final_status = {
                    "cmd": "get_case_status",
                    "code": -21,
                    "status": "error",
                    "last_suite": args.suite,
                    "last_case_id": args.case_id,
                    "last_msg": f"watch ended; device unreachable during final status check: {exc}",
                }

    defect_kind = classify_defect(final_status)

    record = {
        "version": 1,
        "started_at": started_at,
        "host": host,
        "port": args.port,
        "suite": args.suite,
        "case_id": args.case_id,
        "command": make_run_command(
            args.suite, args.case_id, confirm_risk=args.confirm_risk,
        ),
        "final_status": final_status,
        "defect_kind": defect_kind,
        "events": events,
    }
    final_name = defect_kind if defect_kind != "none" else str(final_status.get("status", "unknown"))
    path = make_record_path(args.record_dir, args.case_id, final_name, args.suite)
    with open(path, "w", encoding="utf-8") as fp:
        json.dump(record, fp, ensure_ascii=False, indent=2)
        fp.write("\n")
    print(f"record: {path}")
    return record


def safe_local_name(name: str) -> str:
    base = posixpath.basename(name.replace("\\", "/"))
    if not base or base in (".", ".."):
        raise TestAgentError(f"invalid file name from device: {name!r}")
    return base


def replay_record(args: argparse.Namespace) -> None:
    with open(args.record, "r", encoding="utf-8") as fp:
        record = json.load(fp)
    case_id = int(record["case_id"])
    suite = str(record.get("suite", "stable_test"))
    host = resolve_device_host(args.device, args.host, record.get("host"))
    port = args.port or record.get("port", DEFAULT_PORT)
    print_json(request_device(
        host,
        make_run_command(suite, case_id, confirm_risk=args.confirm_risk),
        args.pc_ip,
        port=port,
        timeout=args.timeout,
    ))


def summarize_record(path: str) -> Dict[str, Any]:
    with open(path, "r", encoding="utf-8") as fp:
        record = json.load(fp)
    final_status = record.get("final_status", {})
    return {
        "path": path,
        "started_at": record.get("started_at"),
        "case_id": record.get("case_id"),
        "defect_kind": record.get("defect_kind"),
        "status": final_status.get("status"),
        "last_msg": final_status.get("last_msg", final_status.get("msg")),
        "events": len(record.get("events", [])),
    }


def list_records(args: argparse.Namespace) -> None:
    pattern = os.path.join(args.record_dir, "*.json")
    paths = sorted(glob.glob(pattern), reverse=True)
    if args.limit > 0:
        paths = paths[: args.limit]
    for path in paths:
        try:
            print_json(summarize_record(path))
        except (json.JSONDecodeError, OSError, ValueError) as exc:
            print_json({"path": path, "error": str(exc)})


def show_record(args: argparse.Namespace) -> None:
    print_json(summarize_record(args.record))


def _status_color(s: str) -> str:
    if s in ("finished",):
        return "#166534"
    if s in ("failed", "error"):
        return "#dc2626"
    if s in ("stopped",):
        return "#ea580c"
    if s in ("running",):
        return "#0ea5e9"
    if s in ("queued",):
        return "#8b5cf6"
    return "#64748b"


def _status_bg(s: str) -> str:
    if s in ("finished",):
        return "#dcfce7"
    if s in ("failed", "error"):
        return "#fef2f2"
    if s in ("stopped",):
        return "#fff7ed"
    if s in ("running",):
        return "#e0f2fe"
    if s in ("queued",):
        return "#f3e8ff"
    return "#f1f5f9"


def _is_pass_record(record: Dict[str, Any]) -> bool:
    fs = record.get("final_status", {})
    if not isinstance(fs, dict):
        fs = {}
    return str(fs.get("status")) == "finished" and str(record.get("defect_kind", "none")) == "none"


def _empty_box(text: str) -> str:
    return f"<div class='empty-box'>{text}</div>"


def _suite_stats_html(record_dir: str) -> str:
    groups: Dict[str, list[Dict[str, Any]]] = {}
    if os.path.isdir(record_dir):
        for name in sorted(os.listdir(record_dir)):
            if not name.endswith(".json") or "_case" not in name:
                continue
            path = os.path.join(record_dir, name)
            try:
                with open(path, "r", encoding="utf-8") as fp:
                    rec = json.load(fp)
            except (OSError, json.JSONDecodeError):
                continue
            if isinstance(rec, dict):
                groups.setdefault(str(rec.get("suite", "stable_test")), []).append(rec)

    if not groups:
        return _empty_box("尚未生成任何记录：运行 run-record 后再生成报告，此处将展示各套件统计与失败用例图例。")

    blocks = ""
    for suite in sorted(groups):
        recs = groups[suite]
        passed = [r for r in recs if _is_pass_record(r)]
        failed = [r for r in recs if not _is_pass_record(r)]
        titles = CASE_TITLES_BY_SUITE.get(suite, [])
        legend = ""
        if failed:
            rows = ""
            for r in sorted(failed, key=lambda x: int(x.get("case_id", 0))):
                cid = int(r.get("case_id", 0))
                title = _html.escape(titles[cid - 1] if 1 <= cid <= len(titles) else f"Case {cid}")
                defect = _html.escape(DEFECT_LABELS.get(str(r.get("defect_kind", "none")), "未知"))
                rows += f"<li>{cid} &middot; {title} &middot; <b style='color:#dc2626'>{defect}</b></li>"
            legend = f"<div class='legend'><b>失败用例</b><ul>{rows}</ul></div>"
        blocks += f"""
      <div class="suite">
        <div class="suite-head">
          <span class="suite-name">{_html.escape(suite)}</span>
          <span class="suite-meta">共 {len(recs)} 条</span>
        </div>
        <div class="suite-grid">
          <div class="stat"><span class="stat-num" style="color:#166534">{len(passed)}</span><span class="stat-label">通过</span></div>
          <div class="stat"><span class="stat-num" style="color:#dc2626">{len(failed)}</span><span class="stat-label">失败/异常</span></div>
        </div>
        {legend}
      </div>"""
    return blocks


def _attachments_html(record_dir: str) -> str:
    entries = []
    if os.path.isdir(record_dir):
        for name in sorted(os.listdir(record_dir)):
            path = os.path.join(record_dir, name)
            if os.path.isfile(path) and not (name.endswith(".json") or name.endswith("_report.html")):
                try:
                    entries.append((name, os.path.getsize(path), os.path.getmtime(path)))
                except OSError:
                    continue
    if not entries:
        return _empty_box("无附件（崩溃导出、日志或截图将显示在此处）。")
    rows = ""
    for name, size, mtime in sorted(entries, key=lambda e: e[2], reverse=True):
        stamp = _dt.datetime.fromtimestamp(mtime).strftime("%Y-%m-%d %H:%M:%S")
        rows += f"<li>{_html.escape(name)} &middot; <span class='muted'>{stamp} &middot; {size} B</span></li>"
    return f"<ul class='attachments'>{rows}</ul>"


def generate_report(record_path: str, output_path: str | None = None) -> str:
    with open(record_path, "r", encoding="utf-8") as fp:
        record = json.load(fp)

    try:
        case_id = int(record.get("case_id", 0))
    except (ValueError, TypeError):
        case_id = 0
    suite_raw = str(record.get("suite", "stable_test"))
    suite = _html.escape(suite_raw)
    suite_titles = CASE_TITLES_BY_SUITE.get(suite_raw, [])
    case_title = _html.escape(
        suite_titles[case_id - 1] if 1 <= case_id <= len(suite_titles) else f"Case {case_id}",
    )
    device_host = _html.escape(str(record.get("device_host", record.get("host", ""))))
    final_status = record.get("final_status", {})
    if not isinstance(final_status, dict):
        final_status = {}
    status_raw = str(final_status.get("status", "unknown"))
    status = _html.escape(STATUS_LABELS.get(status_raw, status_raw))
    defect_raw = str(record.get("defect_kind", "none"))
    defect_kind = _html.escape(DEFECT_LABELS.get(defect_raw, defect_raw))
    last_msg = _html.escape(str(final_status.get("last_msg", final_status.get("msg", ""))))
    started_at = _html.escape(str(record.get("started_at", "")))
    events = record.get("events", [])

    is_pass = status_raw == "finished" and defect_raw == "none"
    status_icon = "通过" if is_pass else "失败"

    msg_style = "color:#dc2626;font-weight:bold" if not is_pass and last_msg else "color:#666"

    # --- Status Timeline: every status update as a row ---
    timeline_rows = ""
    for ev in events:
        ts = _html.escape(str(ev.get("time", "")))
        event_data = ev.get("event", {})
        if not isinstance(event_data, dict):
            event_data = {}

        # The event might be a direct status or wrapped in case_status_event
        inner = event_data.get("status") if event_data.get("cmd") == "case_status_event" else event_data
        if not isinstance(inner, dict):
            inner = {}

        st_raw = str(inner.get("status", ""))
        st = _html.escape(STATUS_LABELS.get(st_raw, st_raw))
        code = inner.get("error_code", inner.get("last_error", inner.get("code", 0)))
        msg = _html.escape(str(inner.get("last_msg", inner.get("msg", ""))))
        case = inner.get("last_case_id", inner.get("case_id", ""))

        color = _status_color(st_raw)
        bg = _status_bg(st_raw)
        is_err = (isinstance(code, (int, float)) and code < 0) or st_raw in ("failed", "error")
        badge_bg = "#dcfce7" if st_raw == "finished" else ("#fef2f2" if is_err else "#f1f5f9")
        badge_color = "#166534" if st_raw == "finished" else ("#dc2626" if is_err else "#475569")

        timeline_rows += f"""
          <tr style="background:{bg}">
            <td class="time">{ts}</td>
            <td style="text-align:center;font-weight:600;color:{color}">{st}</td>
            <td style="text-align:center"><span style="display:inline-block;padding:2px 8px;border-radius:10px;background:{badge_bg};color:{badge_color};font-size:11px;font-weight:600">{'异常' if is_err else '正常'}</span></td>
            <td style="color:{'#dc2626' if is_err else '#334155'};font-weight:{'600' if is_err else '400'}">{msg if msg else '-'}</td>
          </tr>"""

    if not timeline_rows:
        timeline_rows = "<tr><td colspan='4' style='color:#94a3b8;text-align:center;padding:20px'>未记录状态事件</td></tr>"

    # --- Full raw event log (collapsible) ---
    raw_rows = ""
    for ev in events:
        ts = _html.escape(str(ev.get("time", "")))
        event_data = ev.get("event", {})
        if not isinstance(event_data, dict):
            event_data = {}
        inner = event_data.get("status") if event_data.get("cmd") == "case_status_event" else event_data
        if not isinstance(inner, dict):
            inner = {}
        st = _html.escape(str(inner.get("status", "")))
        code = inner.get("error_code", inner.get("last_error", inner.get("code", 0)))
        is_err = (isinstance(code, (int, float)) and code < 0) or st in ("failed", "error")
        row_class = "row-err" if is_err else ""
        event_json = _html.escape(json.dumps(event_data, ensure_ascii=False, indent=2))
        raw_rows += f"""
          <tr class="{row_class}">
            <td class="time">{ts}</td>
            <td><pre>{event_json}</pre></td>
          </tr>"""

    raw_table = raw_rows if raw_rows else "<tr><td colspan='2' style='color:#94a3b8;text-align:center'>未记录事件</td></tr>"

    suite_stats = _suite_stats_html(os.path.dirname(record_path))
    attachments = _attachments_html(os.path.dirname(record_path))
    # The report contains an intentional inline collapse script.  Give it a
    # per-report nonce so a future escaping regression cannot turn arbitrary
    # device text into executable inline JavaScript.
    script_nonce = secrets.token_urlsafe(18)

    if output_path is None:
        base = os.path.splitext(record_path)[0]
        output_path = base + "_report.html"

    html = f"""<!DOCTYPE html>
<html lang="zh-CN">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<meta http-equiv="Content-Security-Policy" content="default-src 'none'; style-src 'unsafe-inline'; script-src 'nonce-{script_nonce}'; object-src 'none'; base-uri 'none'; form-action 'none'">
<title>测试报告 - {case_title}</title>
<style>
  * {{ margin:0; padding:0; box-sizing:border-box; }}
  body {{ font-family:-apple-system,BlinkMacSystemFont,"Segoe UI",Roboto,sans-serif; background:#f1f5f9; color:#1e293b; padding:24px; }}
  .container {{ max-width:960px; margin:0 auto; }}
  .header {{ background:#fff; border-radius:12px; box-shadow:0 1px 3px rgba(0,0,0,.08); padding:28px 32px; margin-bottom:20px; }}
  .header h1 {{ font-size:20px; margin-bottom:4px; }}
  .header .subtitle {{ color:#64748b; font-size:12px; }}
  .badge {{ display:inline-block; padding:4px 14px; border-radius:20px; font-size:13px; font-weight:700; }}
  .badge-pass {{ background:#dcfce7; color:#166534; }}
  .badge-fail {{ background:#fef2f2; color:#991b1b; }}
  .meta-grid {{ display:grid; grid-template-columns:auto 1fr auto 1fr; gap:4px 16px; margin-top:12px; font-size:13px; }}
  .meta-grid .label {{ color:#64748b; }}
  .meta-grid .value {{ color:#1e293b; font-weight:500; }}
  .facts {{ display:grid; grid-template-columns:repeat(4,1fr); gap:10px; margin-top:14px; }}
  .fact {{ background:#f8fafc; border:1px solid #e2e8f0; border-radius:8px; padding:10px 12px; }}
  .fact-label {{ display:block; font-size:11px; color:#94a3b8; margin-bottom:4px; }}
  .fact-value {{ font-size:13px; font-weight:600; color:#1e293b; word-break:break-all; }}
  .suite {{ border:1px solid #e2e8f0; border-radius:8px; padding:12px 14px; margin-bottom:10px; }}
  .suite-head {{ display:flex; justify-content:space-between; align-items:center; margin-bottom:8px; }}
  .suite-name {{ font-weight:600; font-size:13px; }}
  .suite-meta {{ color:#94a3b8; font-size:11px; }}
  .suite-grid {{ display:grid; grid-template-columns:repeat(2,1fr); gap:8px; }}
  .stat {{ background:#f8fafc; border-radius:6px; padding:8px 12px; text-align:center; }}
  .stat-num {{ display:block; font-size:20px; font-weight:700; }}
  .stat-label {{ font-size:11px; color:#64748b; }}
  .legend {{ margin-top:10px; font-size:12px; }}
  .legend ul {{ margin:6px 0 0 16px; }}
  .legend li {{ margin-bottom:3px; }}
  .attachments {{ list-style:none; }}
  .attachments li {{ padding:7px 0; border-bottom:1px solid #f1f5f9; font-size:12px; }}
  .attachments .muted {{ color:#94a3b8; font-size:11px; }}
  .empty-box {{ color:#94a3b8; font-size:12px; text-align:center; padding:16px; border:1px dashed #e2e8f0; border-radius:8px; }}
  .msg-box {{ margin-top:12px; padding:10px 14px; border-left:4px solid #dc2626; border-radius:6px; {msg_style} font-size:13px; background:#fef2f2; }}
  .msg-box.pass {{ background:#f0fdf4; border-color:#16a34a; color:#166534; font-weight:normal; }}
  .section {{ background:#fff; border-radius:12px; box-shadow:0 1px 3px rgba(0,0,0,.08); padding:20px 24px; margin-bottom:16px; }}
  .section h2 {{ font-size:14px; font-weight:600; color:#334155; margin-bottom:10px; display:flex; align-items:center; gap:6px; cursor:pointer; user-select:none; }}
  .section h2 .count {{ font-size:11px; color:#94a3b8; font-weight:400; }}
  .section h2 .arrow {{ transition:transform .15s; display:inline-block; font-size:11px; }}
  .section h2 .arrow.collapsed {{ transform:rotate(-90deg); }}
  table {{ width:100%; border-collapse:collapse; font-size:12px; }}
  th, td {{ text-align:left; padding:5px 8px; border-bottom:1px solid #e2e8f0; vertical-align:middle; }}
  th {{ color:#64748b; font-weight:600; white-space:nowrap; font-size:11px; text-transform:uppercase; letter-spacing:.5px; }}
  td.time {{ white-space:nowrap; color:#94a3b8; font-family:monospace; font-size:11px; }}
  pre {{ background:#f8fafc; border:1px solid #e2e8f0; border-radius:4px; padding:6px 8px; font-size:11px; overflow-x:auto; margin:0; white-space:pre-wrap; word-break:break-all; }}
  .row-err {{ background:#fef2f2; }}
  .row-err pre {{ background:#fff5f5; border-color:#fecaca; }}
  .collapsible {{ overflow:hidden; }}
  .collapsible.hidden {{ display:none; }}
</style>
</head>
<body>
<div class="container">

<div class="header">
  <div style="display:flex;align-items:center;justify-content:space-between;flex-wrap:wrap;gap:8px;">
    <div>
      <h1>{case_title}</h1>
      <div class="subtitle">{started_at} &middot; 设备: {device_host}</div>
    </div>
    <span class="badge badge-{'pass' if is_pass else 'fail'}">{status_icon}</span>
  </div>
  <div class="meta-grid">
    <span class="label">套件</span><span class="value">{suite}</span>
    <span class="label">用例 ID</span><span class="value">{case_id}</span>
    <span class="label">事件数</span><span class="value">{len(events)}</span>
    <span class="label">状态</span><span class="value">{status}</span>
  </div>
  <div class="facts">
    <div class="fact"><span class="fact-label">时间</span><span class="fact-value">{started_at}</span></div>
    <div class="fact"><span class="fact-label">设备</span><span class="fact-value">{device_host}</span></div>
    <div class="fact"><span class="fact-label">用例</span><span class="fact-value">{case_title}</span></div>
    <div class="fact"><span class="fact-label">缺陷</span><span class="fact-value" style="color:{'#166534' if is_pass else '#dc2626'};font-weight:600">{defect_kind}</span></div>
  </div>
  <div class="msg-box{' pass' if is_pass else ''}">{last_msg if last_msg else '无消息'}</div>
</div>

<div class="section">
  <h2><span class="arrow">&#9660;</span> 状态时间线 <span class="count">({len(events)} 次变更)</span></h2>
  <div class="collapsible">
  <table>
    <thead><tr><th>时间</th><th>状态</th><th>结果</th><th>消息</th></tr></thead>
    <tbody>{timeline_rows}</tbody>
  </table>
  </div>
</div>

<div class="section">
  <h2><span class="arrow">&#9660;</span> 套件统计 <span class="count">(同目录记录)</span></h2>
  <div class="collapsible">{suite_stats}</div>
</div>

<div class="section">
  <h2><span class="arrow">&#9660;</span> 附件 <span class="count">(崩溃导出 / 日志 / 截图)</span></h2>
  <div class="collapsible">{attachments}</div>
</div>

<div class="section">
  <h2><span class="arrow collapsed">&#9660;</span> 原始事件日志 <span class="count">(点击展开)</span></h2>
  <div class="collapsible hidden">
  <table>
    <thead><tr><th>时间</th><th>原始 JSON</th></tr></thead>
    <tbody>{raw_table}</tbody>
  </table>
  </div>
</div>

</div>
<script nonce="{script_nonce}">
(function() {{
  document.querySelectorAll('.section h2').forEach(function(h2) {{
    h2.addEventListener('click', function() {{
      var arrow = this.querySelector('.arrow');
      var body = this.nextElementSibling;
      if (body) {{
        body.classList.toggle('hidden');
        if (arrow) arrow.classList.toggle('collapsed');
      }}
    }});
  }});
}})();
</script>
</body>
</html>"""

    with open(output_path, "w", encoding="utf-8") as fp:
        fp.write(html)
    return output_path


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Pocket TestAgent PC client")
    parser.add_argument("--host", help="Legacy device IP; --device takes priority")
    parser.add_argument("--device", help="Select device by IP (from auto-discover or manual)")
    parser.add_argument("--port", type=int, default=DEFAULT_PORT)
    parser.add_argument("--timeout", type=float, default=DEFAULT_TIMEOUT)
    parser.add_argument("--pc-ip", help="Bind requests to this PC-side RNDIS address")
    parser.add_argument(
        "--token", default=os.getenv("POCKET_TESTAGENT_TOKEN", ""),
        help="Authentication token (or set POCKET_TESTAGENT_TOKEN)",
    )
    sub = parser.add_subparsers(dest="command", required=True)

    dev = sub.add_parser("device", help="Multi-device management")
    dev_sub = dev.add_subparsers(dest="device_command", required=True)
    dev_sub.add_parser("list", help="List discovered devices")
    dev_configure = dev_sub.add_parser("configure", help="Configure device network")
    dev_configure.add_argument("pc_ip", help="PC-side IP to configure from")

    sub.add_parser("ping")
    sub.add_parser("info")
    sub.add_parser("status")
    sub.add_parser("reset")
    sub.add_parser("reboot")

    run = sub.add_parser("run")
    run.add_argument("case_id", type=int)
    run.add_argument("--suite", choices=CASE_TITLES_BY_SUITE, default="stable_test")
    run.add_argument("--confirm-risk", action="store_true", help="Confirm R4 test risk")

    stop = sub.add_parser("stop")
    stop.add_argument("case_id", type=int)
    stop.add_argument("--suite", choices=CASE_TITLES_BY_SUITE, default="stable_test")

    watch = sub.add_parser("watch")
    watch.add_argument("--interval-ms", type=int, default=500)

    run_watch = sub.add_parser("run-watch")
    run_watch.add_argument("case_id", type=int)
    run_watch.add_argument("--suite", choices=CASE_TITLES_BY_SUITE, default="stable_test")
    run_watch.add_argument("--interval-ms", type=int, default=500)
    run_watch.add_argument("--confirm-risk", action="store_true", help="Confirm R4 test risk")

    run_stop = sub.add_parser("run-stop")
    run_stop.add_argument("case_id", type=int)
    run_stop.add_argument("--suite", choices=CASE_TITLES_BY_SUITE, default="stable_test")
    run_stop.add_argument("--delay", type=float, default=1.0)
    run_stop.add_argument("--wait-timeout", type=float, default=10.0)
    run_stop.add_argument("--confirm-risk", action="store_true", help="Confirm R4 test risk")

    run_record_parser = sub.add_parser("run-record")
    run_record_parser.add_argument("case_id", type=int)
    run_record_parser.add_argument("--suite", choices=CASE_TITLES_BY_SUITE, default="stable_test")
    run_record_parser.add_argument("--interval-ms", type=int, default=500)
    run_record_parser.add_argument("--wait-timeout", type=float, default=3600.0)
    run_record_parser.add_argument("--record-dir", default=defects_dir())
    run_record_parser.add_argument("--confirm-risk", action="store_true", help="Confirm R4 test risk")

    export_crash = sub.add_parser("export-crash", help="Export core files and UI binaries over FTP")
    export_crash.add_argument("device_ip")
    export_crash.add_argument("--suite", choices=CASE_TITLES_BY_SUITE, default="stable_test")
    export_crash.add_argument("--case-id", type=int, default=0)

    replay = sub.add_parser("replay")
    replay.add_argument("record")
    replay.add_argument("--confirm-risk", action="store_true", help="Confirm R4 test risk")

    list_parser = sub.add_parser("list-records")
    list_parser.add_argument("--record-dir", default=defects_dir())
    list_parser.add_argument("--limit", type=int, default=20)

    show_parser = sub.add_parser("show-record")
    show_parser.add_argument("record")

    gen_report = sub.add_parser("generate-report")
    gen_report.add_argument("record")
    gen_report.add_argument("-o", "--output", help="Output HTML path")
    gen_report.add_argument("--open", action="store_true", help="Open report in browser")

    get_file_parser = sub.add_parser("get-file", help="Pull a file from device")
    get_file_parser.add_argument("remote_path", help="Path on device (e.g. /sdcard/testagent/...)")
    get_file_parser.add_argument("-o", "--output", help="Local save path (default: basename of remote)")

    return parser


def main(argv: Optional[list[str]] = None) -> int:
    args = build_parser().parse_args(argv)
    if args.token:
        os.environ["POCKET_TESTAGENT_TOKEN"] = args.token
    try:
        if args.command == "export-crash":
            result = export_crash_files(
                args.device_ip, args.pc_ip, args.suite, args.case_id,
            )
            print_json(result)
            return 0
        if args.command == "run-record":
            run_record(args)
            return 0
        if args.command == "replay":
            replay_record(args)
            return 0
        if args.command == "list-records":
            list_records(args)
            return 0
        if args.command == "show-record":
            show_record(args)
            return 0
        if args.command == "generate-report":
            path = generate_report(args.record, args.output)
            print(f"report: {path}")
            if args.open:
                import webbrowser
                webbrowser.open(f"file://{os.path.abspath(path)}")
            return 0

        if args.command == "device":
            if args.device_command == "list":
                devices = auto_discover_devices()
                if not devices:
                    print("No devices discovered", file=sys.stderr)
                    return 1
                print(f"{'Interface':<20} {'PC IP':<20} {'Device IP':<20}")
                print("-" * 60)
                for d in devices:
                    print(f"{d['iface']:<20} {d['pc_ip']:<20} {d['device_ip']:<20}")
                return 0
            if args.device_command == "configure":
                result = configure_device(args.pc_ip)
                print_json(result)
                return 0 if result.get("success") else 1

        host = resolve_device_host(args.device, args.host)
        probe_device_with_reconnect(host, args.pc_ip, port=args.port, timeout=args.timeout)
        with TestAgentClient(
            host, args.port, args.timeout, source_host=args.pc_ip,
        ) as client:
            if args.command == "ping":
                print_json(client.request({"cmd": "ping"}))
            elif args.command == "info":
                print_json(client.request({"cmd": "agent_info"}))
            elif args.command == "status":
                print_json(client.request({"cmd": "get_case_status"}))
            elif args.command == "reset":
                print_json(client.request({"cmd": "reset_case_status"}))
            elif args.command == "reboot":
                print_json(client.request({"cmd": "reboot"}))
            elif args.command == "run":
                print_json(client.request(make_run_command(
                    args.suite, args.case_id, confirm_risk=args.confirm_risk,
                )))
            elif args.command == "stop":
                print_json(client.request({"cmd": "stop_case", "suite": args.suite, "case_id": args.case_id}))
            elif args.command == "watch":
                for event in client.watch_case_status(args.interval_ms):
                    print_json(event)
            elif args.command == "run-watch":
                run_reply = client.request(make_run_command(
                    args.suite, args.case_id, confirm_risk=args.confirm_risk,
                ))
                print_json(run_reply)
                if run_reply.get("code") != 0:
                    return 1
                for event in client.watch_case_status(args.interval_ms):
                    print_json(event)
            elif args.command == "run-stop":
                run_reply = client.request(make_run_command(
                    args.suite, args.case_id, confirm_risk=args.confirm_risk,
                ))
                print_json(run_reply)
                if run_reply.get("code") != 0:
                    return 1
                time.sleep(args.delay)
                print_json(client.request({"cmd": "stop_case", "suite": args.suite, "case_id": args.case_id}))
                print_json(wait_for_terminal_state(client, args.case_id, args.wait_timeout, args.suite))
            elif args.command == "get-file":
                metadata, data = client.get_file(args.remote_path)
                local_path = args.output or safe_local_name(metadata.get("name") or "")
                with open(local_path, "wb") as f:
                    f.write(data)
                print(f"saved {len(data)} bytes to {local_path}")
            else:
                raise TestAgentError(f"unsupported command: {args.command}")
    except (OSError, TestAgentError, json.JSONDecodeError) as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 1
    except (ValueError, KeyError, AttributeError, IndexError) as exc:
        print(f"error: unexpected data error: {exc}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
