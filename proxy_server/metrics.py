"""Metrics logging for proxy performance monitoring."""

import csv
import threading
import time
from pathlib import Path
from typing import List


class MetricsLogger:
    """Append per-request metrics to a CSV file."""

    FIELDNAMES = [
        "timestamp",
        "client_ip",
        "method",
        "url",
        "host",
        "latency_ms",
        "request_bytes",
        "response_bytes",
        "blocked",
    ]

    def __init__(self, metrics_path: str) -> None:
        self.metrics_path = Path(metrics_path)
        self.metrics_path.parent.mkdir(parents=True, exist_ok=True)
        self._lock = threading.Lock()
        self._ensure_header()

    def _ensure_header(self) -> None:
        with self._lock:
            if not self.metrics_path.exists():
                with self.metrics_path.open("w", newline="") as csv_file:
                    writer = csv.writer(csv_file)
                    writer.writerow(self.FIELDNAMES)
                return

            with self.metrics_path.open("r", newline="") as csv_file:
                rows = list(csv.reader(csv_file))

            existing_header: List[str] = rows[0] if rows else []
            if existing_header == self.FIELDNAMES:
                return

            data_rows = rows[1:] if rows else []
            old_index = {name: idx for idx, name in enumerate(existing_header)}
            normalized_rows = []
            for row in data_rows:
                normalized_row = []
                for field in self.FIELDNAMES:
                    if field in old_index and old_index[field] < len(row):
                        normalized_row.append(row[old_index[field]])
                    elif field == "blocked":
                        normalized_row.append("0")
                    else:
                        normalized_row.append("")
                normalized_rows.append(normalized_row)

            with self.metrics_path.open("w", newline="") as csv_file:
                writer = csv.writer(csv_file)
                writer.writerow(self.FIELDNAMES)
                writer.writerows(normalized_rows)

    def log(
        self,
        client_ip: str,
        method: str,
        url: str,
        host: str,
        latency_ms: int,
        request_bytes: int,
        response_bytes: int,
        blocked: int = 0,
    ) -> None:
        timestamp = time.strftime("%Y-%m-%d %H:%M:%S", time.localtime())
        with self._lock:
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
                        blocked,
                    ]
                )
