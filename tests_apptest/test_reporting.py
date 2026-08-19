from __future__ import annotations

from apptest.core.reporting import MetricsRecorder


def test_summary_uses_standard_library_percentiles(tmp_path) -> None:
    recorder = MetricsRecorder(tmp_path)
    for index, latency in enumerate((10.0, 20.0, 30.0, 40.0), start=1):
        recorder.record_metric(
            "case3",
            f"download_{index}",
            "GET",
            "https://example.test/file",
            200 if index < 4 else 500,
            latency,
            bytes_sent=index,
            bytes_received=index * 10,
            ok=index < 4,
        )

    summary = recorder.build_summary("case3", 1)

    assert summary["requests_total"] == 4
    assert summary["requests_failed"] == 1
    assert summary["latency_avg_ms"] == 25.0
    assert summary["latency_p50_ms"] == 25.0
    assert summary["latency_p95_ms"] == 38.5
    assert summary["bytes_downloaded"] == 100
    assert summary["bytes_uploaded"] == 10
