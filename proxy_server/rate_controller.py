"""Adaptive admission control driven by observed upstream latency.

A token bucket whose fill rate moves with how the upstream is actually
behaving: when responses come back quickly the proxy allows more
concurrency, and when they slow down it backs off rather than piling
more load onto something already struggling.

This complements, and does not replace, the fixed per-client/per-host
limit in client_handler. That one enforces a policy ("no client gets
more than N requests a minute to one host"). This one protects the
proxy and the upstream from overload regardless of who is asking.

Adapted from the RateController proposed in PR #22, with three changes:

- The original adapted on the *mean* of the last 50 latencies, so one
  large download could drag the average past the back-off threshold and
  clamp the whole proxy to its floor. This uses the median, which is
  unmoved by a small number of slow outliers.
- The original hard-coded its 100 ms / 500 ms thresholds and a floor of
  2 req/s. A global floor that low means a single slow upstream can
  throttle every client. Both are constructor arguments now, and the
  floor defaults higher.
- Rejections are counted, so throttling is visible on the dashboard
  instead of silently shaping traffic.
"""

import statistics
import threading
import time
from collections import deque
from typing import Deque, Dict


class RateController:
    """Token bucket with a fill rate that adapts to upstream latency."""

    def __init__(
        self,
        initial_rate: float = 20.0,
        min_rate: float = 5.0,
        max_rate: float = 200.0,
        up_step: float = 1.0,
        down_factor: float = 0.8,
        fast_latency_ms: float = 100.0,
        slow_latency_ms: float = 500.0,
        window: int = 50,
    ) -> None:
        if min_rate <= 0:
            raise ValueError("min_rate must be positive")
        if max_rate < min_rate:
            raise ValueError("max_rate must be >= min_rate")
        if not 0 < down_factor < 1:
            raise ValueError("down_factor must be between 0 and 1")

        self._lock = threading.Lock()
        self._latencies: Deque[float] = deque(maxlen=window)
        self._allowed_rate = max(min(initial_rate, max_rate), min_rate)
        self._min_rate = min_rate
        self._max_rate = max_rate
        self._up_step = up_step
        self._down_factor = down_factor
        self._fast_latency_ms = fast_latency_ms
        self._slow_latency_ms = slow_latency_ms

        self._tokens = self._allowed_rate
        self._last_refill = time.monotonic()
        self._allowed_count = 0
        self._rejected_count = 0

    def record_latency(self, latency_ms: float) -> None:
        """Feed an observed upstream latency in and re-tune the fill rate."""
        with self._lock:
            self._latencies.append(latency_ms)
            typical = statistics.median(self._latencies)

            if typical < self._fast_latency_ms:
                self._allowed_rate = min(
                    self._allowed_rate + self._up_step, self._max_rate
                )
            elif typical > self._slow_latency_ms:
                self._allowed_rate = max(
                    self._allowed_rate * self._down_factor, self._min_rate
                )

            # Never let a saved-up burst exceed the current rate.
            self._tokens = min(self._tokens, self._allowed_rate)

    def allow_request(self) -> bool:
        """Consume a token if one is available."""
        with self._lock:
            now = time.monotonic()
            elapsed = max(now - self._last_refill, 0.0)
            self._last_refill = now

            self._tokens = min(
                self._allowed_rate, self._tokens + elapsed * self._allowed_rate
            )

            if self._tokens >= 1.0:
                self._tokens -= 1.0
                self._allowed_count += 1
                return True

            self._rejected_count += 1
            return False

    def get_current_rate(self) -> float:
        with self._lock:
            return round(self._allowed_rate, 2)

    def stats(self) -> Dict[str, float]:
        """Snapshot for the dashboard and later Prometheus export."""
        with self._lock:
            return {
                "current_rate": round(self._allowed_rate, 2),
                "tokens": round(self._tokens, 2),
                "allowed": self._allowed_count,
                "rejected": self._rejected_count,
                "median_latency_ms": (
                    round(statistics.median(self._latencies), 1)
                    if self._latencies
                    else 0.0
                ),
                "samples": len(self._latencies),
            }
