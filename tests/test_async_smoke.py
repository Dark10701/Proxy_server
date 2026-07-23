"""The Phase 0 smoke tests, re-run against the asyncio implementation.

Both concurrency models must satisfy the same contract, so these mirror
tests/test_smoke.py. Extra cases cover the failure modes Phase 2 calls
for: upstream timeouts, client disconnects mid-transfer, and refusal
under saturation.
"""

import asyncio
import socket
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

import pytest

from proxy_server.async_server import AsyncProxyServer
from tests.conftest import BLOCKED_DOMAIN, ORIGIN_BODY


class _Origin(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"

    def do_GET(self):  # noqa: N802
        self.send_response(200)
        self.send_header("Content-Type", "text/plain")
        self.send_header("Content-Length", str(len(ORIGIN_BODY)))
        self.end_headers()
        self.wfile.write(ORIGIN_BODY)

    def log_message(self, *args):
        pass


def run_async(coro):
    """Run a coroutine on a fresh loop, so tests need no asyncio plugin."""
    return asyncio.new_event_loop().run_until_complete(coro)


class AsyncProxyFixture:
    """Starts the async proxy on its own loop in a background thread."""

    def __init__(
        self,
        blocked_domains_path,
        metrics_path,
        log_dir,
        adaptive_rate_limit=False,
        max_concurrent=64,
        max_queued=128,
    ):
        self.server = AsyncProxyServer(
            host="127.0.0.1",
            port=0,
            blocked_domains_path=blocked_domains_path,
            metrics_path=metrics_path,
            access_log_path=str(log_dir / "access.log"),
            error_log_path=str(log_dir / "error.log"),
            max_concurrent=max_concurrent,
            max_queued=max_queued,
            # Off by default here so these tests measure the proxy path
            # rather than admission control. Shedding has its own test.
            adaptive_rate_limit=adaptive_rate_limit,
            # No exporter: a fixed port would collide between fixtures.
            # Telemetry still records into its own isolated registry.
            metrics_port=None,
        )
        self.loop = asyncio.new_event_loop()
        self.port = None
        self._ready = threading.Event()
        self._thread = threading.Thread(target=self._run, daemon=True)

    def _run(self):
        asyncio.set_event_loop(self.loop)
        self.loop.run_until_complete(self.server.start())
        self.port = self.server.port_in_use
        self._ready.set()
        self.loop.run_forever()

    def start(self):
        self._thread.start()
        assert self._ready.wait(timeout=10), "async proxy failed to start"
        return self.port

    def stop(self):
        asyncio.run_coroutine_threadsafe(self.server.stop(), self.loop).result(10)
        self.loop.call_soon_threadsafe(self.loop.stop)
        self._thread.join(timeout=5)


@pytest.fixture(scope="module")
def async_proxy(tmp_path_factory):
    config = tmp_path_factory.mktemp("aconfig") / "blocked_domains.txt"
    config.write_text(f"# test policy\n{BLOCKED_DOMAIN}\n")
    log_dir = tmp_path_factory.mktemp("alogs")

    fixture = AsyncProxyFixture(str(config), str(log_dir / "metrics.csv"), log_dir)
    port = fixture.start()
    yield port
    fixture.stop()


@pytest.fixture(scope="module")
def origin():
    server = ThreadingHTTPServer(("127.0.0.1", 0), _Origin)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    yield server.server_address
    server.shutdown()
    server.server_close()


def send_via_proxy(port, payload, read_timeout=10.0):
    with socket.create_connection(("127.0.0.1", port), timeout=read_timeout) as sock:
        sock.settimeout(read_timeout)
        sock.sendall(payload)
        chunks = []
        while True:
            try:
                data = sock.recv(4096)
            except socket.timeout:
                break
            if not data:
                break
            chunks.append(data)
    return b"".join(chunks)


def test_async_forwards_plain_http_request(async_proxy, origin):
    host, port = origin
    response = send_via_proxy(
        async_proxy,
        f"GET http://{host}:{port}/ HTTP/1.1\r\nHost: {host}:{port}\r\n\r\n".encode(),
    )

    assert response.startswith(b"HTTP/1.1 200"), response[:120]
    assert ORIGIN_BODY in response


def test_async_blocked_domain_returns_403(async_proxy):
    response = send_via_proxy(
        async_proxy,
        f"GET http://{BLOCKED_DOMAIN}/ HTTP/1.1\r\nHost: {BLOCKED_DOMAIN}\r\n\r\n".encode(),
    )

    assert response.startswith(b"HTTP/1.1 403"), response[:120]


def test_async_connect_tunnel_relays_bytes(async_proxy, echo_server):
    host, port = echo_server

    with socket.create_connection(("127.0.0.1", async_proxy), timeout=10) as sock:
        sock.settimeout(10)
        sock.sendall(
            f"CONNECT {host}:{port} HTTP/1.1\r\nHost: {host}:{port}\r\n\r\n".encode()
        )
        established = sock.recv(4096)
        assert established.startswith(b"HTTP/1.1 200"), established[:120]

        secret = b"async-tunnelled-payload"
        sock.sendall(secret)
        echoed = b""
        while len(echoed) < len(secret):
            chunk = sock.recv(4096)
            if not chunk:
                break
            echoed += chunk

    assert echoed == secret


def test_async_unreachable_upstream_returns_502_not_a_hang(async_proxy):
    """A closed port must produce a clean 502, not a swallowed exception."""
    with socket.socket() as probe:
        probe.bind(("127.0.0.1", 0))
        dead_port = probe.getsockname()[1]
    # Port is now closed again.

    response = send_via_proxy(
        async_proxy,
        f"GET http://127.0.0.1:{dead_port}/ HTTP/1.1\r\n"
        f"Host: 127.0.0.1:{dead_port}\r\n\r\n".encode(),
    )

    assert response.startswith(b"HTTP/1.1 502"), response[:120]


def test_async_malformed_request_returns_400(async_proxy):
    response = send_via_proxy(async_proxy, b"NOT-A-VALID-REQUEST-LINE\r\n\r\n")

    assert response.startswith(b"HTTP/1.1 400"), response[:120]


def test_async_oversized_header_block_returns_431(async_proxy):
    """Must be refused, not silently truncated and forwarded."""
    padding = "x" * 200
    headers = "".join(f"X-Pad-{i}: {padding}\r\n" for i in range(600))
    payload = (
        f"GET http://127.0.0.1:1/ HTTP/1.1\r\nHost: 127.0.0.1\r\n{headers}\r\n"
    ).encode()

    response = send_via_proxy(async_proxy, payload)

    assert response.startswith(b"HTTP/1.1 431"), response[:120]


def test_async_client_disconnect_mid_transfer_does_not_crash_the_server(
    async_proxy, origin
):
    """Hang up early, then confirm the proxy still serves the next client."""
    host, port = origin
    sock = socket.create_connection(("127.0.0.1", async_proxy), timeout=10)
    sock.sendall(
        f"GET http://{host}:{port}/ HTTP/1.1\r\nHost: {host}:{port}\r\n\r\n".encode()
    )
    sock.close()  # Disconnect without reading the response.

    response = send_via_proxy(
        async_proxy,
        f"GET http://{host}:{port}/ HTTP/1.1\r\nHost: {host}:{port}\r\n\r\n".encode(),
    )
    assert response.startswith(b"HTTP/1.1 200"), response[:120]


def test_async_handles_many_concurrent_clients(async_proxy, origin):
    """The point of the rewrite: many simultaneous connections, no threads."""
    host, port = origin
    request = f"GET http://{host}:{port}/ HTTP/1.1\r\nHost: {host}:{port}\r\n\r\n".encode()
    results = []
    lock = threading.Lock()

    def one():
        try:
            body = send_via_proxy(async_proxy, request)
            ok = body.startswith(b"HTTP/1.1 200")
        except OSError:
            ok = False
        with lock:
            results.append(ok)

    threads = [threading.Thread(target=one) for _ in range(60)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join(timeout=30)

    assert len(results) == 60
    assert sum(results) >= 57, f"only {sum(results)}/60 succeeded"


def test_async_rate_controller_sheds_load_with_503(tmp_path_factory, origin):
    """With adaptive limiting on, excess load is refused rather than queued.

    This is the behaviour that made the concurrency test above look like
    a failure: a burst larger than the token bucket gets a definitive
    503, not a hang and not a dropped connection.
    """
    config = tmp_path_factory.mktemp("shed") / "blocked_domains.txt"
    config.write_text("# none\n")
    log_dir = tmp_path_factory.mktemp("shedlogs")

    fixture = AsyncProxyFixture(
        str(config),
        str(log_dir / "metrics.csv"),
        log_dir,
        adaptive_rate_limit=True,
    )
    port = fixture.start()
    host, origin_port = origin
    request = (
        f"GET http://{host}:{origin_port}/ HTTP/1.1\r\n"
        f"Host: {host}:{origin_port}\r\n\r\n"
    ).encode()

    statuses = []
    lock = threading.Lock()

    def one():
        try:
            body = send_via_proxy(port, request)
            code = body.split(b" ")[1] if body.startswith(b"HTTP/1.1") else b"none"
        except OSError:
            code = b"error"
        with lock:
            statuses.append(code)

    try:
        threads = [threading.Thread(target=one) for _ in range(60)]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join(timeout=30)
    finally:
        fixture.stop()

    assert len(statuses) == 60
    # Every client got a real HTTP response: none hung, none was dropped.
    assert all(code in (b"200", b"503") for code in statuses), set(statuses)
    assert b"503" in statuses, "expected the controller to shed some load"
    assert b"200" in statuses, "expected some requests to get through"
