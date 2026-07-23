"""Read the proxy's metrics CSV and aggregate it for display.

Kept free of Flask imports so the aggregation can be unit-tested on its
own. The proxy appends to the CSV and never rewrites it, so reading is
a plain sequential scan; a partially-written trailing row is skipped
rather than treated as an error.
"""

import csv
from dataclasses import dataclass, field
from pathlib import Path
from typing import Dict, List, Optional

# How many of the most recent requests feed the latency chart and table.
RECENT_WINDOW = 100
TOP_DOMAIN_COUNT = 8


@dataclass
class RequestRecord:
    timestamp: str
    client_ip: str
    method: str
    url: str
    host: str
    latency_ms: float
    request_bytes: int
    response_bytes: int
    blocked: bool
    cache: str


@dataclass
class MetricsSnapshot:
    """Everything the dashboard renders in one pass."""

    total_requests: int = 0
    blocked_requests: int = 0
    allowed_requests: int = 0
    block_rate_pct: float = 0.0
    avg_latency_ms: float = 0.0
    p50_latency_ms: float = 0.0
    p95_latency_ms: float = 0.0
    total_request_bytes: int = 0
    total_response_bytes: int = 0
    cache_hits: int = 0
    cache_misses: int = 0
    cache_stores: int = 0
    cache_hit_ratio_pct: float = 0.0
    top_domains: List[Dict[str, object]] = field(default_factory=list)
    latency_series: List[Dict[str, object]] = field(default_factory=list)
    recent: List[Dict[str, object]] = field(default_factory=list)
    metrics_path: str = ""
    available: bool = True
    message: str = ""

    def to_dict(self) -> Dict[str, object]:
        return self.__dict__.copy()


def _percentile(sorted_values: List[float], pct: float) -> float:
    """Nearest-rank percentile. Empty input yields 0.0."""
    if not sorted_values:
        return 0.0
    rank = max(1, int(round(pct / 100.0 * len(sorted_values))))
    return sorted_values[min(rank, len(sorted_values)) - 1]


def _coerce_int(value: str) -> int:
    try:
        return int(float(value))
    except (TypeError, ValueError):
        return 0


def _coerce_float(value: str) -> float:
    try:
        return float(value)
    except (TypeError, ValueError):
        return 0.0


def read_records(metrics_path: Path) -> List[RequestRecord]:
    """Parse the CSV into records, skipping rows the proxy hasn't finished."""
    records: List[RequestRecord] = []
    with metrics_path.open("r", newline="") as csv_file:
        for row in csv.DictReader(csv_file):
            # A row still being appended can be missing trailing columns.
            if row.get("timestamp") is None or row.get("host") is None:
                continue
            records.append(
                RequestRecord(
                    timestamp=row.get("timestamp", ""),
                    client_ip=row.get("client_ip", ""),
                    method=row.get("method", ""),
                    url=row.get("url", ""),
                    host=row.get("host", ""),
                    latency_ms=_coerce_float(row.get("latency_ms", "0")),
                    request_bytes=_coerce_int(row.get("request_bytes", "0")),
                    response_bytes=_coerce_int(row.get("response_bytes", "0")),
                    blocked=_coerce_int(row.get("blocked", "0")) == 1,
                    cache=(row.get("cache") or "").strip().lower(),
                )
            )
    return records


def build_snapshot(metrics_path: str) -> MetricsSnapshot:
    """Aggregate the metrics CSV into a single renderable snapshot."""
    path = Path(metrics_path)
    snapshot = MetricsSnapshot(metrics_path=str(path))

    if not path.exists():
        snapshot.available = False
        snapshot.message = (
            "No metrics file yet. Start the proxy and send a request through it."
        )
        return snapshot

    try:
        records = read_records(path)
    except OSError as exc:
        snapshot.available = False
        snapshot.message = f"Could not read metrics file: {exc}"
        return snapshot

    if not records:
        snapshot.message = "Metrics file is empty. No requests recorded yet."
        return snapshot

    snapshot.total_requests = len(records)
    snapshot.blocked_requests = sum(1 for r in records if r.blocked)
    snapshot.allowed_requests = snapshot.total_requests - snapshot.blocked_requests
    snapshot.block_rate_pct = round(
        snapshot.blocked_requests / snapshot.total_requests * 100, 1
    )
    snapshot.total_request_bytes = sum(r.request_bytes for r in records)
    snapshot.total_response_bytes = sum(r.response_bytes for r in records)

    # Latency stats come from allowed requests only: a 403 is answered
    # locally in microseconds and would flatter the numbers.
    latencies = sorted(r.latency_ms for r in records if not r.blocked)
    if latencies:
        snapshot.avg_latency_ms = round(sum(latencies) / len(latencies), 1)
        snapshot.p50_latency_ms = round(_percentile(latencies, 50), 1)
        snapshot.p95_latency_ms = round(_percentile(latencies, 95), 1)

    # Cache outcomes. Only requests that consulted the cache count
    # towards the ratio: blocked and bypassed requests never did, and
    # including them would understate it.
    snapshot.cache_hits = sum(1 for r in records if r.cache == "hit")
    snapshot.cache_misses = sum(1 for r in records if r.cache in ("miss", "store"))
    snapshot.cache_stores = sum(1 for r in records if r.cache == "store")
    consulted = snapshot.cache_hits + snapshot.cache_misses
    if consulted:
        snapshot.cache_hit_ratio_pct = round(
            snapshot.cache_hits / consulted * 100, 1
        )

    counts: Dict[str, Dict[str, int]] = {}
    for record in records:
        if not record.host:
            continue
        entry = counts.setdefault(record.host, {"requests": 0, "blocked": 0, "bytes": 0})
        entry["requests"] += 1
        entry["bytes"] += record.response_bytes
        if record.blocked:
            entry["blocked"] += 1

    snapshot.top_domains = [
        {
            "host": host,
            "requests": data["requests"],
            "blocked": data["blocked"],
            "bytes": data["bytes"],
        }
        for host, data in sorted(
            counts.items(), key=lambda item: item[1]["requests"], reverse=True
        )[:TOP_DOMAIN_COUNT]
    ]

    recent = records[-RECENT_WINDOW:]
    snapshot.latency_series = [
        {"t": r.timestamp, "latency_ms": r.latency_ms, "blocked": r.blocked}
        for r in recent
    ]
    snapshot.recent = [
        {
            "timestamp": r.timestamp,
            "client_ip": r.client_ip,
            "method": r.method,
            "host": r.host,
            "url": r.url,
            "latency_ms": r.latency_ms,
            "response_bytes": r.response_bytes,
            "blocked": r.blocked,
            "cache": r.cache,
        }
        for r in reversed(recent)
    ][:25]

    return snapshot


def snapshot_dict(metrics_path: str) -> Dict[str, object]:
    return build_snapshot(metrics_path).to_dict()
