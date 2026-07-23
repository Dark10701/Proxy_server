"""Prometheus instrumentation tests."""

import socket
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

import pytest

from proxy_server.observability import (
    OUTCOME_BLOCKED,
    OUTCOME_FORWARDED,
    Telemetry,
)
from tests.test_async_smoke import AsyncProxyFixture, send_via_proxy
from tests.conftest import BLOCKED_DOMAIN, ORIGIN_BODY


def sample(text: str, metric: str, **labels) -> float:
    """Pull one sample value out of a Prometheus exposition."""
    if labels:
        rendered = ",".join(f'{k}="{v}"' for k, v in sorted(labels.items()))
        needle = f"{metric}{{{rendered}}}"
    else:
        needle = metric
    for line in text.splitlines():
        if line.startswith("#"):
            continue
        name, _, value = line.rpartition(" ")
        if name.strip() == needle:
            return float(value)
    raise AssertionError(f"{needle} not found in exposition:\n{text}")


def test_request_counter_and_histogram_record_together():
    telemetry = Telemetry()
    telemetry.record_request("GET", OUTCOME_FORWARDED, 0.03)
    telemetry.record_request("GET", OUTCOME_FORWARDED, 0.07)

    text = telemetry.generate().decode()

    assert sample(text, "proxy_requests_total", method="GET", outcome=OUTCOME_FORWARDED) == 2.0
    assert sample(text, "proxy_request_duration_seconds_count", outcome=OUTCOME_FORWARDED) == 2.0


def test_outcomes_are_counted_separately():
    telemetry = Telemetry()
    telemetry.record_request("GET", OUTCOME_FORWARDED, 0.01)
    telemetry.record_request("GET", OUTCOME_BLOCKED, 0.001)

    text = telemetry.generate().decode()

    assert sample(text, "proxy_requests_total", method="GET", outcome=OUTCOME_BLOCKED) == 1.0
    assert sample(text, "proxy_requests_total", method="GET", outcome=OUTCOME_FORWARDED) == 1.0


def test_bytes_counted_by_direction():
    telemetry = Telemetry()
    telemetry.record_bytes(to_client=500, to_upstream=120)

    text = telemetry.generate().decode()

    assert sample(text, "proxy_bytes_total", direction="to_client") == 500.0
    assert sample(text, "proxy_bytes_total", direction="to_upstream") == 120.0


def test_active_connections_gauge_goes_up_and_down():
    telemetry = Telemetry()
    telemetry.connection_opened()
    telemetry.connection_opened()
    telemetry.connection_closed()

    assert sample(telemetry.generate().decode(), "proxy_active_connections") == 1.0


def test_cache_hits_and_misses_counted():
    telemetry = Telemetry()
    telemetry.record_cache("hit")
    telemetry.record_cache("hit")
    telemetry.record_cache("miss")

    text = telemetry.generate().decode()

    assert sample(text, "proxy_cache_events_total", result="hit") == 2.0
    assert sample(text, "proxy_cache_events_total", result="miss") == 1.0


def test_registries_are_isolated():
    """Two servers in one process must not collide on the default registry."""
    first, second = Telemetry(), Telemetry()
    first.record_request("GET", OUTCOME_FORWARDED, 0.01)

    text = second.generate().decode()
    with pytest.raises(AssertionError):
        sample(text, "proxy_requests_total", method="GET", outcome=OUTCOME_FORWARDED)


class _Origin(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"

    def do_GET(self):  # noqa: N802
        self.send_response(200)
        self.send_header("Content-Length", str(len(ORIGIN_BODY)))
        self.end_headers()
        self.wfile.write(ORIGIN_BODY)

    def log_message(self, *args):
        pass


@pytest.fixture
def instrumented_proxy(tmp_path_factory):
    config = tmp_path_factory.mktemp("obsconf") / "blocked_domains.txt"
    config.write_text(f"{BLOCKED_DOMAIN}\n")
    log_dir = tmp_path_factory.mktemp("obslogs")

    fixture = AsyncProxyFixture(str(config), str(log_dir / "metrics.csv"), log_dir)
    port = fixture.start()
    yield port, fixture.server.telemetry
    fixture.stop()


def test_live_traffic_moves_the_counters(instrumented_proxy):
    """The instrumentation is on the real path, not just unit-testable."""
    port, telemetry = instrumented_proxy

    origin = ThreadingHTTPServer(("127.0.0.1", 0), _Origin)
    threading.Thread(target=origin.serve_forever, daemon=True).start()
    host, origin_port = origin.server_address
    try:
        send_via_proxy(
            port,
            f"GET http://{host}:{origin_port}/ HTTP/1.1\r\n"
            f"Host: {host}:{origin_port}\r\n\r\n".encode(),
        )
        send_via_proxy(
            port,
            f"GET http://{BLOCKED_DOMAIN}/ HTTP/1.1\r\nHost: {BLOCKED_DOMAIN}\r\n\r\n".encode(),
        )
    finally:
        origin.shutdown()
        origin.server_close()

    text = telemetry.generate().decode()

    assert sample(text, "proxy_requests_total", method="GET", outcome=OUTCOME_FORWARDED) == 1.0
    assert sample(text, "proxy_requests_total", method="GET", outcome=OUTCOME_BLOCKED) == 1.0
    assert sample(text, "proxy_bytes_total", direction="to_client") > 0


def test_metrics_endpoint_is_served_over_http():
    telemetry = Telemetry()
    telemetry.record_request("GET", OUTCOME_FORWARDED, 0.01)

    with socket.socket() as probe:
        probe.bind(("127.0.0.1", 0))
        port = probe.getsockname()[1]

    assert telemetry.start_server(port, addr="127.0.0.1") == port

    import urllib.request

    with urllib.request.urlopen(f"http://127.0.0.1:{port}/metrics", timeout=10) as resp:
        body = resp.read().decode()

    assert resp.status == 200
    assert "proxy_requests_total" in body
