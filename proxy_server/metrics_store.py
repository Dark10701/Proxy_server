"""Thread-safe in-memory metrics store for proxy monitoring."""

import threading
import time
from collections import defaultdict, deque
from typing import Deque, Dict, List


class MetricsStore:
    """Collect and expose real-time proxy metrics in memory."""

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self.total_requests = 0
        self.blocked_requests = 0
        self.total_bytes_sent = 0
        self.total_bytes_received = 0
        self.active_connections = 0
        self.cumulative_latency = 0.0
        self.recent_requests: Deque[Dict[str, object]] = deque(maxlen=1000)
        self._request_timestamps: Deque[float] = deque()
        self._arrival_timestamps: Deque[float] = deque(maxlen=5000)
        self._latency_by_minute: Dict[int, List[float]] = defaultdict(list)
        self._domain_counts: Dict[str, int] = defaultdict(int)

    def increment_active_connections(self) -> None:
        with self._lock:
            self.active_connections += 1

    def decrement_active_connections(self) -> None:
        with self._lock:
            if self.active_connections > 0:
                self.active_connections -= 1

    def record_request(
        self,
        client_ip: str,
        method: str,
        host: str,
        status_code: int,
        latency_ms: int,
        bytes_sent: int,
        bytes_received: int,
        blocked: bool,
    ) -> None:
        now = time.time()
        request_record = {
            "timestamp": time.strftime("%Y-%m-%d %H:%M:%S", time.localtime(now)),
            "client_ip": client_ip,
            "method": method,
            "host": host,
            "status_code": status_code,
            "latency_ms": latency_ms,
            "bytes_sent": bytes_sent,
            "bytes_received": bytes_received,
            "blocked": blocked,
        }

        minute_bucket = int(now // 60)
        with self._lock:
            self.total_requests += 1
            if blocked:
                self.blocked_requests += 1
            self.total_bytes_sent += bytes_sent
            self.total_bytes_received += bytes_received
            self.cumulative_latency += latency_ms
            self.recent_requests.append(request_record)
            self._request_timestamps.append(now)
            self._arrival_timestamps.append(now)
            self._latency_by_minute[minute_bucket].append(latency_ms)
            if host:
                self._domain_counts[host] += 1

            # Keep rolling window to avoid unbounded growth (last 24 hours).
            cutoff = now - (24 * 60 * 60)
            while self._request_timestamps and self._request_timestamps[0] < cutoff:
                old_ts = self._request_timestamps.popleft()
                old_bucket = int(old_ts // 60)
                self._latency_by_minute.pop(old_bucket, None)

    def get_summary(self) -> Dict[str, float]:
        with self._lock:
            total_requests = self.total_requests
            blocked_requests = self.blocked_requests
            active_connections = self.active_connections
            cumulative_latency = self.cumulative_latency
            total_bandwidth = self.total_bytes_sent + self.total_bytes_received

        average_latency = (cumulative_latency / total_requests) if total_requests else 0.0
        return {
            "total_requests": total_requests,
            "blocked_requests": blocked_requests,
            "active_connections": active_connections,
            "average_latency_ms": round(average_latency, 2),
            "total_bandwidth_bytes": total_bandwidth,
        }

    def get_time_series(self) -> Dict[str, List[float]]:
        with self._lock:
            timestamps = list(self._request_timestamps)
            latency_by_minute = {
                minute: values[:] for minute, values in self._latency_by_minute.items()
            }

        request_counts: Dict[int, int] = defaultdict(int)
        for ts in timestamps:
            request_counts[int(ts // 60)] += 1

        minute_buckets = sorted(request_counts.keys())
        formatted_timestamps = [
            time.strftime("%H:%M", time.localtime(minute * 60)) for minute in minute_buckets
        ]
        requests_per_minute = [request_counts[minute] for minute in minute_buckets]
        average_latency = [
            round(
                sum(latency_by_minute.get(minute, []))
                / max(len(latency_by_minute.get(minute, [])), 1),
                2,
            )
            for minute in minute_buckets
        ]

        return {
            "timestamps": formatted_timestamps,
            "requests_per_minute": requests_per_minute,
            "average_latency": average_latency,
        }

    def get_top_domains(self, limit: int = 10) -> Dict[str, List[object]]:
        with self._lock:
            sorted_domains = sorted(
                self._domain_counts.items(), key=lambda item: item[1], reverse=True
            )[:limit]

        return {
            "domains": [domain for domain, _ in sorted_domains],
            "counts": [count for _, count in sorted_domains],
        }

    def get_recent_requests(self, limit: int = 20) -> List[Dict[str, object]]:
        with self._lock:
            return list(self.recent_requests)[-limit:][::-1]

    def get_traffic_patterns(self) -> Dict[str, object]:
        """Return traffic dynamics to support CN-focused dashboard analysis."""
        with self._lock:
            arrivals = list(self._arrival_timestamps)

        if not arrivals:
            return {
                "requests_per_second": [],
                "traffic_type": "steady",
                "avg_inter_arrival_time": 0.0,
            }

        second_counts: Dict[int, int] = defaultdict(int)
        for ts in arrivals:
            second_counts[int(ts)] += 1

        sorted_seconds = sorted(second_counts.keys())
        requests_per_second = [second_counts[sec] for sec in sorted_seconds[-60:]]

        intervals: List[float] = []
        for idx in range(1, len(arrivals)):
            delta = arrivals[idx] - arrivals[idx - 1]
            if delta >= 0:
                intervals.append(delta)

        if intervals:
            avg_inter_arrival = round(sum(intervals) / len(intervals), 4)
            mean = sum(intervals) / len(intervals)
            variance = sum((value - mean) ** 2 for value in intervals) / len(intervals)
        else:
            avg_inter_arrival = 0.0
            variance = 0.0

        traffic_type = "steady"
        if len(requests_per_second) >= 5:
            recent_window = requests_per_second[-5:]
            baseline = (
                sum(requests_per_second[:-1]) / max(len(requests_per_second[:-1]), 1)
                if len(requests_per_second) > 1
                else recent_window[-1]
            )
            if baseline > 0 and recent_window[-1] >= baseline * 2.5:
                traffic_type = "spike"
            elif variance > 0.1:
                traffic_type = "burst"

        return {
            "requests_per_second": requests_per_second,
            "traffic_type": traffic_type,
            "avg_inter_arrival_time": avg_inter_arrival,
        }
