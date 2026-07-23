"""Prometheus instrumentation.

Exposed on its own port rather than on the proxy port. The proxy port
speaks proxy protocol -- absolute-form request lines and CONNECT -- so
an origin-form GET /metrics arriving there is ambiguous at best and a
way to bypass filtering at worst. A separate listener keeps the two
concerns apart, which is also how exporters are normally deployed.

prometheus_client is optional. If it is not installed, every call here
becomes a no-op and the proxy runs without instrumentation rather than
refusing to start.
"""

from typing import Optional

try:
    from prometheus_client import (
        CollectorRegistry,
        Counter,
        Gauge,
        Histogram,
        start_http_server,
    )

    PROMETHEUS_AVAILABLE = True
except ImportError:  # pragma: no cover - exercised by absence, not tests
    PROMETHEUS_AVAILABLE = False
    CollectorRegistry = None

# Outcome label values. Kept to a closed set: an unbounded label space
# is the classic way to melt a Prometheus server.
OUTCOME_FORWARDED = "forwarded"
OUTCOME_CACHE_HIT = "cache_hit"
OUTCOME_BLOCKED = "blocked"
OUTCOME_RATE_LIMITED = "rate_limited"
OUTCOME_SHED = "shed"
OUTCOME_TUNNELLED = "tunnelled"
OUTCOME_UPSTREAM_ERROR = "upstream_error"
OUTCOME_CLIENT_ERROR = "client_error"
OUTCOME_PROXY_ERROR = "proxy_error"

LATENCY_BUCKETS = (
    0.001, 0.005, 0.01, 0.025, 0.05, 0.1, 0.25, 0.5, 1.0, 2.5, 5.0, 10.0, 30.0,
)


class Telemetry:
    """Facade over the Prometheus collectors, inert when unavailable."""

    def __init__(self, registry: Optional["CollectorRegistry"] = None) -> None:
        self.enabled = PROMETHEUS_AVAILABLE
        self._server_port: Optional[int] = None
        if not self.enabled:
            return

        # An explicit registry keeps repeated instantiation (tests,
        # multiple servers in one process) from colliding on the default.
        self.registry = registry if registry is not None else CollectorRegistry()

        self.requests = Counter(
            "proxy_requests_total",
            "Requests handled by the proxy, by method and outcome.",
            ["method", "outcome"],
            registry=self.registry,
        )
        self.latency = Histogram(
            "proxy_request_duration_seconds",
            "Time to serve a request, by outcome.",
            ["outcome"],
            buckets=LATENCY_BUCKETS,
            registry=self.registry,
        )
        self.bytes_transferred = Counter(
            "proxy_bytes_total",
            "Bytes transferred, by direction relative to the client.",
            ["direction"],
            registry=self.registry,
        )
        self.active_connections = Gauge(
            "proxy_active_connections",
            "Client connections currently being served.",
            registry=self.registry,
        )
        self.cache_events = Counter(
            "proxy_cache_events_total",
            "Cache outcomes, by result.",
            ["result"],
            registry=self.registry,
        )
        self.upstream_errors = Counter(
            "proxy_upstream_errors_total",
            "Failures contacting an upstream server, by kind.",
            ["kind"],
            registry=self.registry,
        )
        self.scheduler_queue = Gauge(
            "proxy_scheduler_queued",
            "Requests waiting for an admission slot.",
            registry=self.registry,
        )
        self.rate_limit_current = Gauge(
            "proxy_adaptive_rate_limit",
            "Current adaptive rate limit, in requests per second.",
            registry=self.registry,
        )

    # -- recording ------------------------------------------------------

    def record_request(
        self, method: str, outcome: str, duration_seconds: float
    ) -> None:
        if not self.enabled:
            return
        self.requests.labels(method=method.upper(), outcome=outcome).inc()
        self.latency.labels(outcome=outcome).observe(max(duration_seconds, 0.0))

    def record_bytes(self, to_client: int = 0, to_upstream: int = 0) -> None:
        if not self.enabled:
            return
        if to_client:
            self.bytes_transferred.labels(direction="to_client").inc(to_client)
        if to_upstream:
            self.bytes_transferred.labels(direction="to_upstream").inc(to_upstream)

    def record_cache(self, result: str) -> None:
        if not self.enabled or not result:
            return
        self.cache_events.labels(result=result).inc()

    def record_upstream_error(self, kind: str) -> None:
        if not self.enabled:
            return
        self.upstream_errors.labels(kind=kind).inc()

    def connection_opened(self) -> None:
        if self.enabled:
            self.active_connections.inc()

    def connection_closed(self) -> None:
        if self.enabled:
            self.active_connections.dec()

    def observe_gauges(self, queued: int = 0, rate_limit: float = 0.0) -> None:
        """Sample values that are state rather than events."""
        if not self.enabled:
            return
        self.scheduler_queue.set(queued)
        self.rate_limit_current.set(rate_limit)

    # -- exposition -----------------------------------------------------

    def start_server(self, port: int, addr: str = "0.0.0.0") -> Optional[int]:
        """Serve /metrics. Returns the port, or None if unavailable."""
        if not self.enabled:
            return None
        start_http_server(port, addr=addr, registry=self.registry)
        self._server_port = port
        return port

    def generate(self) -> bytes:
        """Render the current exposition, for tests and debugging."""
        if not self.enabled:
            return b""
        from prometheus_client import generate_latest

        return generate_latest(self.registry)
