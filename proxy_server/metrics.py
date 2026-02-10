"""Metrics logging for proxy performance monitoring."""

import csv
import threading
import time
from pathlib import Path


class MetricsLogger:
    """Append per-request metrics to a CSV file."""

    def __init__(self, metrics_path: str) -> None:
        self.metrics_path = Path(metrics_path)
        self.metrics_path.parent.mkdir(parents=True, exist_ok=True)
        self._lock = threading.Lock()
        self._ensure_header()

    def _ensure_header(self) -> None:
        if self.metrics_path.exists():
            return
        with self._lock:
            if self.metrics_path.exists():
                return
            with self.metrics_path.open("w", newline="") as csv_file:
                writer = csv.writer(csv_file)
                writer.writerow(
                    [
                        "timestamp",
                        "client_ip",
                        "host",
                        "latency_ms",
                        "response_bytes",
                    ]
                )
        with self.metrics_path.open("w", newline="") as csv_file:
            writer = csv.writer(csv_file)
            writer.writerow(
                [
                    "timestamp",
                    "client_ip",
                    "method",
                    "url",
                    "host",
                    "latency_ms",
                    "request_bytes",
                    "response_bytes",
                ]
            )

    def log(
        self,
        client_ip: str,
        host: str,
        latency_ms: int,
        response_bytes: int,
    ) -> None:
        timestamp = time.strftime("%Y-%m-%d %H:%M:%S", time.localtime())
        with self._lock:
            with self.metrics_path.open("a", newline="") as csv_file:
                writer = csv.writer(csv_file)
                writer.writerow(
                    [
                        timestamp,
                        client_ip,
                        host,
                        latency_ms,
                        response_bytes,
                    ]
                )
        method: str,
        url: str,
        host: str,
        latency_ms: int,
        request_bytes: int,
        response_bytes: int,
    ) -> None:
        timestamp = time.strftime("%Y-%m-%d %H:%M:%S", time.localtime())
        with self.metrics_path.open("a", newline="") as csv_file:
            writer = csv.writer(csv_file)
            writer.writerow(
                [
                    timestamp,
                    client_ip,
                    method,
                    url,
                    host,
                    latency_ms,
                    request_bytes,
                    response_bytes,
                ]
            )
