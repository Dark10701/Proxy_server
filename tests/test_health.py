"""Health and readiness endpoint tests.

These matter for Phase 5: the container healthcheck and nginx's passive
health checking both depend on readiness flipping to 503 *before* the
listener closes, so a restarting instance is drained rather than sent
traffic it will drop.
"""

import asyncio
import json
import socket

from proxy_server.health import HealthServer


def free_port() -> int:
    with socket.socket() as probe:
        probe.bind(("127.0.0.1", 0))
        return probe.getsockname()[1]


def fetch(port: int, path: str):
    """Plain HTTP GET returning (status_code, parsed_body)."""
    with socket.create_connection(("127.0.0.1", port), timeout=5) as sock:
        sock.sendall(f"GET {path} HTTP/1.1\r\nHost: localhost\r\n\r\n".encode())
        chunks = []
        while True:
            data = sock.recv(4096)
            if not data:
                break
            chunks.append(data)
    raw = b"".join(chunks)
    head, _, body = raw.partition(b"\r\n\r\n")
    status = int(head.split(b" ")[1])
    try:
        return status, json.loads(body.decode())
    except ValueError:
        return status, {}


def with_server(coro_fn, status_provider=None):
    """Run a health server on its own loop for the duration of the test."""
    port = free_port()
    server = HealthServer(port=port, host="127.0.0.1", status_provider=status_provider)
    loop = asyncio.new_event_loop()
    loop.run_until_complete(server.start())

    thread_done = []

    import threading

    def spin():
        loop.run_forever()

    thread = threading.Thread(target=spin, daemon=True)
    thread.start()
    try:
        return coro_fn(port, server)
    finally:
        asyncio.run_coroutine_threadsafe(server.stop(), loop).result(5)
        loop.call_soon_threadsafe(loop.stop)
        thread.join(timeout=5)
        thread_done.append(True)


def test_health_reports_ok():
    def scenario(port, server):
        return fetch(port, "/health")

    status, body = with_server(scenario)
    assert status == 200
    assert body["status"] == "ok"


def test_ready_reports_ok_once_started():
    def scenario(port, server):
        return fetch(port, "/ready")

    status, body = with_server(scenario)
    assert status == 200
    assert body["status"] == "ok"


def test_draining_flips_ready_to_503_but_health_stays_200():
    """The drain contract: unhealthy for routing, still alive."""

    def scenario(port, server):
        server.begin_draining()
        return fetch(port, "/ready"), fetch(port, "/health")

    (ready_status, ready_body), (health_status, _) = with_server(scenario)

    assert ready_status == 503
    assert ready_body["status"] == "draining"
    assert health_status == 200


def test_unknown_path_is_404():
    def scenario(port, server):
        return fetch(port, "/nope")

    status, _ = with_server(scenario)
    assert status == 404


def test_status_provider_is_merged_into_the_body():
    def scenario(port, server):
        return fetch(port, "/health")

    status, body = with_server(scenario, status_provider=lambda: {"active": 7})
    assert status == 200
    assert body["active"] == 7


def test_a_failing_status_provider_does_not_break_the_probe():
    """A broken stats call must not make the container look dead."""

    def boom():
        raise RuntimeError("stats exploded")

    def scenario(port, server):
        return fetch(port, "/health")

    status, body = with_server(scenario, status_provider=boom)
    assert status == 200
    assert "stats_error" in body
