from __future__ import annotations

import csv
import json
import html
import re
import threading
from dataclasses import dataclass, asdict
from datetime import datetime
from pathlib import Path
from statistics import mean
from typing import Any

from apptest.core.logging_utils import get_logger
from apptest.core.execution import emit_case_event
from apptest.core.events import CASE_EVENT, FAILURE, METRIC


@dataclass
class MetricRecord:
    timestamp: str
    scenario: str
    step: str
    method: str
    url: str
    status_code: int
    elapsed_ms: float
    bytes_sent: int
    bytes_received: int
    ok: bool
    error: str
    integrity: str


class MetricsRecorder:
    def __init__(self, report_dir: str | Path) -> None:
        self.logger = get_logger("pocket_app_automation.metrics")
        self.report_dir = Path(report_dir)
        self.metrics_path = self.report_dir / "metrics.csv"
        self.failures_path = self.report_dir / "failures.jsonl"
        self.events_path = self.report_dir / "events.jsonl"
        self._lock = threading.Lock()
        self._started_at = datetime.now().isoformat()
        self._ensure_headers()

    def _ensure_headers(self) -> None:
        if not self.metrics_path.exists():
            with self.metrics_path.open("w", newline="", encoding="utf-8") as handle:
                writer = csv.DictWriter(handle, fieldnames=list(MetricRecord.__annotations__.keys()))
                writer.writeheader()

    def record_metric(
        self,
        scenario: str,
        step: str,
        method: str,
        url: str,
        status_code: int,
        elapsed_ms: float,
        bytes_sent: int = 0,
        bytes_received: int = 0,
        ok: bool = True,
        error: str = "",
        integrity: str = "",
    ) -> None:
        record = MetricRecord(
            timestamp=datetime.now().isoformat(),
            scenario=scenario,
            step=step,
            method=method,
            url=url,
            status_code=status_code,
            elapsed_ms=elapsed_ms,
            bytes_sent=bytes_sent,
            bytes_received=bytes_received,
            ok=ok,
            error=error,
            integrity=integrity,
        )
        with self._lock:
            with self.metrics_path.open("a", newline="", encoding="utf-8") as handle:
                writer = csv.DictWriter(handle, fieldnames=list(MetricRecord.__annotations__.keys()))
                writer.writerow(asdict(record))
        self.logger.info(
            "metric scenario=%s step=%s method=%s status=%s elapsed_ms=%.3f ok=%s bytes_received=%s integrity=%s",
            scenario,
            step,
            method,
            status_code,
            elapsed_ms,
            ok,
            bytes_received,
            integrity,
        )
        emit_case_event(METRIC, asdict(record))

    def record_failure(self, scenario: str, step: str, details: dict[str, Any]) -> None:
        payload = {
            "timestamp": datetime.now().isoformat(),
            "scenario": scenario,
            "step": step,
            "details": details,
        }
        with self._lock:
            with self.failures_path.open("a", encoding="utf-8") as handle:
                handle.write(json.dumps(payload, ensure_ascii=False) + "\n")
        self.logger.error("failure scenario=%s step=%s details=%s", scenario, step, json.dumps(details, ensure_ascii=False))
        emit_case_event(FAILURE, payload)

    def record_event(self, event_name: str, payload: dict[str, Any]) -> None:
        event = {
            "timestamp": datetime.now().isoformat(),
            "event": event_name,
            "payload": payload,
        }
        with self._lock:
            with self.events_path.open("a", encoding="utf-8") as handle:
                handle.write(json.dumps(event, ensure_ascii=False) + "\n")
        self.logger.info("event name=%s payload=%s", event_name, json.dumps(payload, ensure_ascii=False))
        emit_case_event(CASE_EVENT, event)

    def build_summary(self, scenario: str, exit_code: int) -> dict[str, Any]:
        with self._lock:
            rows = _read_metric_rows(self.metrics_path)

        requests_total = len(rows)
        requests_failed = sum(1 for row in rows if not _as_bool(row.get("ok")))
        latencies = [_as_float(row.get("elapsed_ms")) for row in rows]
        bytes_downloaded = sum(_as_int(row.get("bytes_received")) or 0 for row in rows)
        bytes_uploaded = sum(_as_int(row.get("bytes_sent")) or 0 for row in rows)
        integrity_statuses = sorted({str(row.get("integrity") or "").strip() for row in rows if str(row.get("integrity") or "").strip()})

        finished_at = datetime.now()
        try:
            elapsed_seconds = max((finished_at - datetime.fromisoformat(self._started_at)).total_seconds(), 0.0)
        except ValueError:
            elapsed_seconds = 0.0

        summary = {
            "scenario": scenario,
            "started_at": self._started_at,
            "finished_at": finished_at.isoformat(),
            "exit_code": exit_code,
            "requests_total": requests_total,
            "requests_failed": requests_failed,
            "success_rate": round(((requests_total - requests_failed) / requests_total), 4) if requests_total else 0.0,
            "latency_avg_ms": round(mean(latencies), 3) if latencies else 0.0,
            "latency_p50_ms": round(_percentile(latencies, 0.50), 3) if latencies else 0.0,
            "latency_p95_ms": round(_percentile(latencies, 0.95), 3) if latencies else 0.0,
            "latency_p99_ms": round(_percentile(latencies, 0.99), 3) if latencies else 0.0,
            "throughput_rps": round(requests_total / elapsed_seconds, 3) if elapsed_seconds > 0 else 0.0,
            "bytes_downloaded": bytes_downloaded,
            "bytes_uploaded": bytes_uploaded,
            "integrity_statuses": integrity_statuses,
        }
        summary_path = self.report_dir / "summary.json"
        summary_path.write_text(json.dumps(summary, ensure_ascii=False, indent=2), encoding="utf-8")
        self.logger.info("summary written to %s", summary_path)
        return summary


def _percentile(values: list[float], quantile: float) -> float:
    ordered = sorted(values)
    if not ordered:
        return 0.0
    position = (len(ordered) - 1) * quantile
    lower_index = int(position)
    upper_index = min(lower_index + 1, len(ordered) - 1)
    fraction = position - lower_index
    return ordered[lower_index] + (ordered[upper_index] - ordered[lower_index]) * fraction



CASE_DISPLAY_NAMES = {
    "case3": "用例 3：APK 下载压力测试",
    "case4": "用例 4：固件下载压力测试",
    "case5": "用例 5：设备激活循环测试",
    "uia_case1": "用例 1：App 视频下载与删除",
    "uia_case2": "用例 2：App 视频预览循环",
    "uia_case6": "用例 6：App 设备连接测试",
    "uia_case7": "用例 7：App monkey 随机测试",
}

_DETAIL_LABELS = {
    "error": "错误信息",
    "message": "提示信息",
    "status_code": "状态码",
    "body": "响应内容",
    "video_file": "视频文件",
    "field": "配置项",
    "iteration": "测试轮次",
    "expected": "期望值",
    "actual": "实际值",
}


def write_chinese_report(
    report_dir: str | Path,
    scenario: str,
    summary: dict[str, Any] | None = None,
    exit_code: int = 0,
    fallback_error: str = "",
    original_report: str | Path | None = None,
) -> Path:
    """Generate the user-facing Chinese HTML report from the recorded run artifacts.

    Test cases only write metrics/failure artifacts.  This reporting function keeps the
    presentation layer separate from those cases and aggregates records with the same
    iteration suffix (for example ``download_12``) into one execution result.
    """
    directory = Path(report_dir)
    directory.mkdir(parents=True, exist_ok=True)
    metric_rows = _read_metric_rows(directory / "metrics.csv")
    failure_rows = _read_failure_rows(directory / "failures.jsonl")
    report_summary = summary or _build_report_summary(directory, scenario, exit_code)
    runs = _build_execution_rows(metric_rows, failure_rows, scenario, exit_code, fallback_error)

    success_count = sum(1 for run in runs if run["ok"])
    failure_count = len(runs) - success_count
    execution_total = len(runs)
    execution_rate = success_count / execution_total if execution_total else 0.0
    scenario_name = CASE_DISPLAY_NAMES.get(scenario, scenario)
    failure_items = [run for run in runs if not run["ok"]]

    cards = "".join(
        [
            _summary_card("执行轮次", str(execution_total), "本次报告中的测试轮次", "neutral"),
            _summary_card("成功", str(success_count), "执行完成且没有失败记录", "success"),
            _summary_card("失败", str(failure_count), "包含失败步骤或异常原因", "danger"),
            _summary_card("通过率", f"{execution_rate:.1%}", "按测试轮次统计", "info"),
        ]
    )
    metadata_rows = "".join(
        [
            _metadata_row("测试用例", scenario_name),
            _metadata_row("开始时间", _format_datetime(report_summary.get("started_at"))),
            _metadata_row("结束时间", _format_datetime(report_summary.get("finished_at"))),
            _metadata_row("接口/操作记录", str(report_summary.get("requests_total", len(metric_rows)))),
            _metadata_row("记录失败数", str(report_summary.get("requests_failed", 0))),
            _metadata_row("完整性状态", ", ".join(report_summary.get("integrity_statuses", [])) or "未记录"),
            _metadata_row("平均耗时", f"{_as_float(report_summary.get('latency_avg_ms')):.1f} ms"),
        ]
    )
    failure_section = _build_failure_section(failure_items)
    execution_table = _build_execution_table(runs)
    artifact_section = _build_artifact_section(directory, original_report)

    html_content = f"""<!DOCTYPE html>
<html lang="zh-CN">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>{html.escape(scenario_name)} - 测试报告</title>
<style>
:root {{ color-scheme: light; --ink:#19212e; --muted:#657184; --line:#e6ebf2; --bg:#f5f7fb; --panel:#fff; --success:#138a5b; --success-bg:#e9f8f0; --danger:#c63646; --danger-bg:#fff0f1; --info:#2f6fec; --info-bg:#edf4ff; }}
* {{ box-sizing:border-box; }}
body {{ margin:0; min-width:320px; color:var(--ink); background:var(--bg); font-family:"Microsoft YaHei UI","Microsoft YaHei",system-ui,-apple-system,BlinkMacSystemFont,"Segoe UI",sans-serif; line-height:1.55; }}
.page {{ max-width:1440px; margin:0 auto; padding:32px 24px 48px; }}
.hero {{ padding:30px 32px; border-radius:20px; color:#fff; background:linear-gradient(120deg,#17345e 0%,#2767c7 55%,#3c91d9 100%); box-shadow:0 14px 36px rgba(24,58,109,.19); }}
.hero h1 {{ margin:0 0 7px; font-size:28px; letter-spacing:.2px; }} .hero p {{ margin:0; opacity:.86; font-size:14px; }}
.grid {{ display:grid; grid-template-columns:repeat(4,minmax(0,1fr)); gap:16px; margin:22px 0; }}
.card,.section {{ background:var(--panel); border:1px solid var(--line); border-radius:16px; box-shadow:0 4px 14px rgba(34,51,84,.035); }}
.card {{ padding:19px 20px; border-top:4px solid #a6b2c3; }} .card.success {{ border-top-color:var(--success); }} .card.danger {{ border-top-color:var(--danger); }} .card.info {{ border-top-color:var(--info); }}
.card .label {{ color:var(--muted); font-size:13px; }} .card .value {{ margin:3px 0 4px; font-size:28px; font-weight:700; }} .card .hint {{ color:#8792a4; font-size:12px; }}
.section {{ margin-top:18px; overflow:hidden; }} .section-title {{ display:flex; align-items:center; justify-content:space-between; gap:12px; padding:18px 22px; margin:0; border-bottom:1px solid var(--line); font-size:17px; }} .section-body {{ padding:20px 22px; }}
.meta {{ display:grid; grid-template-columns:repeat(3,minmax(0,1fr)); gap:12px 20px; }} .meta-item {{ padding:11px 0; border-bottom:1px dashed #e8edf4; }} .meta-label {{ display:block; color:var(--muted); font-size:12px; }} .meta-value {{ font-weight:600; overflow-wrap:anywhere; }}
table {{ width:100%; border-collapse:collapse; }} th {{ background:#f7f9fc; color:#526075; text-align:left; font-size:12px; font-weight:700; letter-spacing:.2px; white-space:nowrap; }} th,td {{ padding:12px 14px; border-bottom:1px solid var(--line); vertical-align:top; }} tr:last-child td {{ border-bottom:0; }}
.badge {{ display:inline-flex; align-items:center; gap:5px; padding:4px 9px; border-radius:999px; font-size:12px; font-weight:700; white-space:nowrap; }} .badge.success {{ color:var(--success); background:var(--success-bg); }} .badge.danger {{ color:var(--danger); background:var(--danger-bg); }}
.reason {{ margin:0; padding-left:18px; color:#8f2935; }} .reason li {{ margin:2px 0; overflow-wrap:anywhere; }} .muted {{ color:var(--muted); }} .step-list {{ margin:8px 0 0; padding-left:18px; color:#506076; font-size:12px; }} .step-list li {{ margin:3px 0; overflow-wrap:anywhere; }}
details summary {{ cursor:pointer; color:var(--info); font-size:13px; font-weight:600; }} details[open] summary {{ margin-bottom:8px; }} .empty {{ padding:28px; text-align:center; color:var(--muted); }} .failure-summary {{ border-left:4px solid var(--danger); background:#fff8f8; }}
.artifacts a {{ color:var(--info); text-decoration:none; }} .artifacts a:hover {{ text-decoration:underline; }} .artifact-list {{ margin:0; padding-left:18px; }}
@media (max-width:900px) {{ .grid {{ grid-template-columns:repeat(2,minmax(0,1fr)); }} .meta {{ grid-template-columns:repeat(2,minmax(0,1fr)); }} .table-wrap {{ overflow-x:auto; }} }}
@media (max-width:560px) {{ .page {{ padding:16px 12px 28px; }} .hero {{ padding:24px 20px; border-radius:14px; }} .hero h1 {{ font-size:23px; }} .grid,.meta {{ grid-template-columns:1fr; }} .section-body {{ padding:14px; }} th,td {{ padding:10px; }} }}
</style>
</head>
<body>
<main class="page">
  <header class="hero"><h1>自动化测试报告</h1><p>{html.escape(scenario_name)} · 所有字段均为中文说明 · 生成时间：{html.escape(_format_datetime(datetime.now().isoformat()))}</p></header>
  <section class="grid">{cards}</section>
  <section class="section"><h2 class="section-title">测试概览 <span class="muted">接口与操作性能数据</span></h2><div class="section-body"><div class="meta">{metadata_rows}</div></div></section>
  {failure_section}
  <section class="section"><h2 class="section-title">每次测试执行明细 <span class="muted">共 {execution_total} 次</span></h2><div class="table-wrap">{execution_table}</div></section>
  {artifact_section}
</main>
</body>
</html>"""
    report_path = directory / "report.html"
    report_path.write_text(html_content, encoding="utf-8")
    return report_path


def _read_metric_rows(path: Path) -> list[dict[str, Any]]:
    if not path.exists() or path.stat().st_size == 0:
        return []
    with path.open("r", newline="", encoding="utf-8-sig") as handle:
        return [dict(row) for row in csv.DictReader(handle)]


def _read_failure_rows(path: Path) -> list[dict[str, Any]]:
    if not path.exists():
        return []
    rows: list[dict[str, Any]] = []
    for line in path.read_text(encoding="utf-8-sig").splitlines():
        if not line.strip():
            continue
        try:
            payload = json.loads(line)
        except json.JSONDecodeError:
            payload = {"timestamp": "", "scenario": "", "step": "", "details": {"error": line}}
        if isinstance(payload, dict):
            rows.append(payload)
    return rows


def _build_report_summary(directory: Path, scenario: str, exit_code: int) -> dict[str, Any]:
    return MetricsRecorder(directory).build_summary(scenario, exit_code)


def _build_execution_rows(
    metric_rows: list[dict[str, Any]],
    failure_rows: list[dict[str, Any]],
    scenario: str,
    exit_code: int,
    fallback_error: str,
) -> list[dict[str, Any]]:
    grouped: dict[tuple[str, int | None], dict[str, Any]] = {}
    order: list[tuple[str, int | None]] = []

    def get_group(case: str, iteration: int | None) -> dict[str, Any]:
        key = (case or scenario, iteration)
        if key not in grouped:
            grouped[key] = {"scenario": key[0], "iteration": iteration, "records": [], "reasons": [], "first_timestamp": ""}
            order.append(key)
        return grouped[key]

    for record in metric_rows:
        case = str(record.get("scenario") or scenario)
        iteration = _extract_iteration(str(record.get("step") or ""))
        group = get_group(case, iteration)
        group["records"].append(record)
        group["first_timestamp"] = group["first_timestamp"] or str(record.get("timestamp") or "")
        if not _as_bool(record.get("ok")):
            group["reasons"].append(_metric_failure_reason(record))

    for failure in failure_rows:
        case = str(failure.get("scenario") or scenario)
        details = failure.get("details") if isinstance(failure.get("details"), dict) else {"error": failure.get("details", "")}
        iteration = _as_int(details.get("iteration")) if isinstance(details, dict) else None
        group = _latest_iteration_group(grouped, order, case) if iteration is None else None
        if group is None:
            group = get_group(case, iteration)
        group["first_timestamp"] = group["first_timestamp"] or str(failure.get("timestamp") or "")
        group["reasons"].append(_failure_details_reason(str(failure.get("step") or "执行异常"), details))

    if exit_code and not any(group["reasons"] for group in grouped.values()):
        group = get_group(scenario, None)
        group["reasons"].append(fallback_error.strip() or f"测试进程异常结束，退出码为 {exit_code}。")

    rows: list[dict[str, Any]] = []
    for key in order:
        group = grouped[key]
        records = group["records"]
        reasons = _deduplicate(group["reasons"])
        rows.append(
            {
                **group,
                "reasons": reasons,
                "ok": not reasons and bool(records),
                "elapsed_ms": sum(_as_float(item.get("elapsed_ms")) for item in records),
                "timestamp": group["first_timestamp"],
            }
        )

    if not rows:
        rows.append(
            {
                "scenario": scenario,
                "iteration": None,
                "records": [],
                "reasons": [fallback_error.strip() or f"测试未产生可展示的执行记录，退出码为 {exit_code}。"] if exit_code else [],
                "first_timestamp": "",
                "timestamp": "",
                "ok": exit_code == 0,
                "elapsed_ms": 0.0,
            }
        )
    return rows


def _latest_iteration_group(
    grouped: dict[tuple[str, int | None], dict[str, Any]],
    order: list[tuple[str, int | None]],
    scenario: str,
) -> dict[str, Any] | None:
    """Attach a generic execution exception to the latest recorded iteration.

    UI automation code records a final ``execution`` failure after the step that
    failed.  It does not always include an iteration number, so the latest iteration
    of the same scenario is the most useful place to show the reason to the user.
    """
    for key in reversed(order):
        case, iteration = key
        if case == scenario and iteration is not None:
            return grouped[key]
    return None


def _build_execution_table(runs: list[dict[str, Any]]) -> str:
    body: list[str] = []
    for index, run in enumerate(runs, start=1):
        status = '<span class="badge success">● 成功</span>' if run["ok"] else '<span class="badge danger">● 失败</span>'
        iteration = f"第 {run['iteration']} 次" if run.get("iteration") is not None else "未标记轮次"
        reasons = "<span class=\"muted\">—</span>" if run["ok"] else "<ul class=\"reason\">" + "".join(f"<li>{html.escape(reason)}</li>" for reason in run["reasons"]) + "</ul>"
        body.append(
            "<tr>"
            f"<td>{index}</td>"
            f"<td><strong>{html.escape(CASE_DISPLAY_NAMES.get(run['scenario'], str(run['scenario'])))}</strong><br><span class=\"muted\">{html.escape(iteration)}</span></td>"
            f"<td>{status}</td>"
            f"<td>{html.escape(_format_datetime(run.get('timestamp')))}</td>"
            f"<td>{run['elapsed_ms']:.1f} ms</td>"
            f"<td>{reasons}</td>"
            f"<td>{_build_steps_details(run['records'])}</td>"
            "</tr>"
        )
    return (
        "<table><thead><tr><th>#</th><th>测试用例 / 轮次</th><th>结果</th><th>开始时间</th>"
        "<th>耗时</th><th>失败原因</th><th>步骤明细</th></tr></thead><tbody>"
        + "".join(body)
        + "</tbody></table>"
    )


def _build_steps_details(records: list[dict[str, Any]]) -> str:
    if not records:
        return '<span class="muted">无步骤指标</span>'
    items = []
    for record in records:
        status = "成功" if _as_bool(record.get("ok")) else "失败"
        status_code = str(record.get("status_code") or "-")
        items.append(
            "<li>"
            f"<strong>{html.escape(status)}</strong> · {html.escape(str(record.get('step') or '未命名步骤'))}"
            f" · {html.escape(str(record.get('method') or '-'))} · 状态码 {html.escape(status_code)}"
            f" · {_as_float(record.get('elapsed_ms')):.1f} ms"
            f" · 完整性 {html.escape(str(record.get('integrity') or '未记录'))}"
            "</li>"
        )
    return f"<details><summary>查看 {len(records)} 个步骤</summary><ul class=\"step-list\">{''.join(items)}</ul></details>"


def _build_failure_section(failures: list[dict[str, Any]]) -> str:
    if not failures:
        return (
            '<section class="section"><h2 class="section-title">失败原因汇总 <span class="badge success">● 未发现失败</span></h2>'
            '<div class="section-body"><p class="muted">本次执行没有记录到失败步骤或异常。</p></div></section>'
        )
    items = []
    for run in failures:
        iteration = f"第 {run['iteration']} 次" if run.get("iteration") is not None else "未标记轮次"
        reason_html = "".join(f"<li>{html.escape(reason)}</li>" for reason in run["reasons"])
        items.append(
            f"<div class=\"failure-summary\" style=\"padding:13px 15px;margin-bottom:12px;border-radius:8px\"><strong>{html.escape(CASE_DISPLAY_NAMES.get(run['scenario'], str(run['scenario'])))} · {html.escape(iteration)}</strong><ul class=\"reason\">{reason_html}</ul></div>"
        )
    return f'<section class="section"><h2 class="section-title">失败原因汇总 <span class="badge danger">● {len(failures)} 次失败</span></h2><div class="section-body">{"".join(items)}</div></section>'


def _build_artifact_section(directory: Path, original_report: str | Path | None) -> str:
    links: list[str] = []
    for filename, label in [
        ("summary.json", "原始汇总数据（summary.json）"),
        ("metrics.csv", "操作指标明细（metrics.csv）"),
        ("failures.jsonl", "失败原始记录（failures.jsonl）"),
        ("events.jsonl", "事件原始记录（events.jsonl）"),
    ]:
        path = directory / filename
        if path.exists():
            links.append(f'<li><a href="{html.escape(path.resolve().as_uri())}">{html.escape(label)}</a></li>')
    if original_report:
        path = Path(original_report)
        if path.exists():
            links.append(f'<li><a href="{html.escape(path.resolve().as_uri())}">原始测试框架报告（{html.escape(path.name)}）</a></li>')
    if not links:
        return ""
    return f'<section class="section artifacts"><h2 class="section-title">原始数据与附件</h2><div class="section-body"><ul class="artifact-list">{"".join(links)}</ul></div></section>'


def _summary_card(label: str, value: str, hint: str, tone: str) -> str:
    return f'<div class="card {tone}"><div class="label">{html.escape(label)}</div><div class="value">{html.escape(value)}</div><div class="hint">{html.escape(hint)}</div></div>'


def _metadata_row(label: str, value: str) -> str:
    return f'<div class="meta-item"><span class="meta-label">{html.escape(label)}</span><span class="meta-value">{html.escape(value)}</span></div>'


def _extract_iteration(step: str) -> int | None:
    match = re.search(r"_(\d+)$", step)
    return int(match.group(1)) if match else None


def _metric_failure_reason(record: dict[str, Any]) -> str:
    step = str(record.get("step") or "未命名步骤")
    error = str(record.get("error") or "").strip()
    if error:
        return f"步骤“{step}”失败：{error}"
    status_code = _as_int(record.get("status_code"))
    if status_code:
        return f"步骤“{step}”失败：状态码为 {status_code}。"
    return f"步骤“{step}”执行失败。"


def _failure_details_reason(step: str, details: dict[str, Any]) -> str:
    parts = []
    for key, value in details.items():
        if value in (None, "", [], {}):
            continue
        label = _DETAIL_LABELS.get(str(key), str(key))
        text = json.dumps(value, ensure_ascii=False) if isinstance(value, (dict, list)) else str(value)
        parts.append(f"{label}：{text}")
    detail = "；".join(parts) or "未提供详细原因"
    return f"步骤“{step}”失败：{detail}"


def _deduplicate(values: list[str]) -> list[str]:
    return list(dict.fromkeys(value for value in values if value))


def _as_bool(value: Any) -> bool:
    if isinstance(value, bool):
        return value
    return str(value).strip().lower() in {"1", "true", "yes", "y", "是"}


def _as_int(value: Any) -> int | None:
    try:
        return int(value)
    except (TypeError, ValueError):
        return None


def _as_float(value: Any) -> float:
    try:
        return float(value or 0)
    except (TypeError, ValueError):
        return 0.0


def _format_datetime(value: Any) -> str:
    if not value:
        return "—"
    text = str(value)
    try:
        return datetime.fromisoformat(text.replace("Z", "+00:00")).strftime("%Y-%m-%d %H:%M:%S")
    except ValueError:
        return text.replace("T", " ")
