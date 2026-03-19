"""Adaptive request rate controller based on recent upstream latency."""

import threading
import time
from collections import deque
from typing import Deque


class RateController:
    """Token-bucket style controller with adaptive fill rate."""

    def __init__(
        self,
        initial_rate: float = 20.0,
        min_rate: float = 2.0,
        max_rate: float = 200.0,
        up_step: float = 1.0,
        down_factor: float = 0.8,
    ) -> None:
        self._lock = threading.Lock()
        self._latencies: Deque[float] = deque(maxlen=50)
        self._allowed_rate = initial_rate
        self._min_rate = min_rate
        self._max_rate = max_rate
        self._up_step = up_step
        self._down_factor = down_factor

        self._tokens = initial_rate
        self._last_refill = time.time()

    def record_latency(self, latency_ms: float) -> None:
        """Record request latency and adapt request rate to network conditions."""
        with self._lock:
            self._latencies.append(latency_ms)
            avg_latency = sum(self._latencies) / len(self._latencies)

            if avg_latency < 100:
                self._allowed_rate = min(self._allowed_rate + self._up_step, self._max_rate)
            elif avg_latency > 500:
                reduced = self._allowed_rate * self._down_factor
                self._allowed_rate = max(reduced, self._min_rate)

            # Keep tokens bounded by current rate to avoid large bursts.
            self._tokens = min(self._tokens, self._allowed_rate)

    def allow_request(self) -> bool:
        """Return True when a request can proceed under the current adaptive rate."""
        with self._lock:
            now = time.time()
            elapsed = max(now - self._last_refill, 0.0)
            self._last_refill = now

            self._tokens = min(
                self._allowed_rate,
                self._tokens + elapsed * self._allowed_rate,
            )
            if self._tokens >= 1.0:
                self._tokens -= 1.0
                return True
            return False

    def get_current_rate(self) -> float:
        with self._lock:
            return round(self._allowed_rate, 2)
