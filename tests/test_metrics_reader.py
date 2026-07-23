"""Unit tests for the dashboard's CSV aggregation."""

import csv

import pytest

from dashboard.metrics_reader import MetricsSnapshot, build_snapshot
from proxy_server.metrics import MetricsLogger


@pytest.fixture
def metrics_file(tmp_path):
    """A metrics CSV written by the real MetricsLogger, not a hand-rolled one."""
    path = tmp_path / "metrics.csv"
    logger = MetricsLogger(str(path))
    logger.log("10.0.0.1", "GET", "http://a.test/1", "a.test", 100, 200, 5000, blocked=0)
    logger.log("10.0.0.1", "GET", "http://a.test/2", "a.test", 300, 200, 7000, blocked=0)
    logger.log("10.0.0.2", "GET", "http://b.test/", "b.test", 0, 150, 90, blocked=1)
    return str(path)


def test_aggregates_counts_and_bytes(metrics_file):
    snap = build_snapshot(metrics_file)

    assert snap.total_requests == 3
    assert snap.blocked_requests == 1
    assert snap.allowed_requests == 2
    assert snap.block_rate_pct == pytest.approx(33.3)
    assert snap.total_response_bytes == 5000 + 7000 + 90


def test_latency_excludes_blocked_requests(metrics_file):
    """A locally-served 403 must not drag the latency average down."""
    snap = build_snapshot(metrics_file)

    assert snap.avg_latency_ms == pytest.approx(200.0)
    assert snap.p50_latency_ms == pytest.approx(100.0)
    assert snap.p95_latency_ms == pytest.approx(300.0)


def test_top_domains_ranked_by_request_count(metrics_file):
    snap = build_snapshot(metrics_file)

    assert [d["host"] for d in snap.top_domains] == ["a.test", "b.test"]
    assert snap.top_domains[0]["requests"] == 2
    assert snap.top_domains[1]["blocked"] == 1


def test_recent_is_newest_first(metrics_file):
    snap = build_snapshot(metrics_file)

    assert snap.recent[0]["host"] == "b.test"
    assert snap.recent[0]["blocked"] is True


def test_missing_file_is_reported_not_raised(tmp_path):
    snap = build_snapshot(str(tmp_path / "absent.csv"))

    assert isinstance(snap, MetricsSnapshot)
    assert snap.available is False
    assert snap.total_requests == 0
    assert "No metrics file" in snap.message


def test_header_only_file_is_empty_not_an_error(tmp_path):
    path = tmp_path / "metrics.csv"
    with path.open("w", newline="") as handle:
        csv.writer(handle).writerow(MetricsLogger.FIELDNAMES)

    snap = build_snapshot(str(path))

    assert snap.available is True
    assert snap.total_requests == 0


def test_malformed_row_does_not_break_aggregation(tmp_path):
    """A row still being appended must not take the whole dashboard down."""
    path = tmp_path / "metrics.csv"
    logger = MetricsLogger(str(path))
    logger.log("10.0.0.1", "GET", "http://a.test/", "a.test", 120, 200, 4000, blocked=0)
    with path.open("a", newline="") as handle:
        handle.write("2026-01-01 00:00:00,10.0.0.9,GET\n")

    snap = build_snapshot(str(path))

    # The partial row is skipped, not counted and not fatal.
    assert snap.total_requests == 1
    assert snap.avg_latency_ms == pytest.approx(120.0)
