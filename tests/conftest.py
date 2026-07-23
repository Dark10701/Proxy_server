"""Shared fixtures: a real origin server and a real proxy on loopback.

The smoke tests drive the proxy over an actual TCP socket rather than
calling its methods directly, so they exercise the accept loop, the
parser, the filter and the tunnel the same way a client would. That is
what makes them useful as a regression gate across the later rewrites.
"""

import socket
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

import pytest

from proxy_server.server import ProxyServer

ORIGIN_BODY = b"hello from origin"
BLOCKED_DOMAIN = "blocked.example"


def free_port() -> int:
    """Ask the OS for an unused loopback port."""
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as probe:
        probe.bind(("127.0.0.1", 0))
        return probe.getsockname()[1]


class _OriginHandler(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"

    def do_GET(self) -> None:  # noqa: N802 - name fixed by BaseHTTPRequestHandler
        self.send_response(200)
        self.send_header("Content-Type", "text/plain")
        self.send_header("Content-Length", str(len(ORIGIN_BODY)))
        self.end_headers()
        self.wfile.write(ORIGIN_BODY)

    def log_message(self, *args, **kwargs) -> None:
        """Silence the default stderr access log."""


@pytest.fixture(scope="session")
def origin_server():
    """A plain HTTP server for the proxy to forward to."""
    server = ThreadingHTTPServer(("127.0.0.1", 0), _OriginHandler)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    yield server.server_address
    server.shutdown()
    server.server_close()


@pytest.fixture(scope="session")
def echo_server():
    """A raw TCP echo server, used as the far end of a CONNECT tunnel."""
    listener = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    listener.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
    listener.bind(("127.0.0.1", 0))
    listener.listen(5)

    def serve() -> None:
        while True:
            try:
                conn, _ = listener.accept()
            except OSError:
                return
            threading.Thread(target=_echo, args=(conn,), daemon=True).start()

    def _echo(conn: socket.socket) -> None:
        with conn:
            while True:
                data = conn.recv(4096)
                if not data:
                    return
                conn.sendall(data)

    threading.Thread(target=serve, daemon=True).start()
    yield listener.getsockname()
    listener.close()


@pytest.fixture(scope="session")
def blocked_domains_file(tmp_path_factory) -> str:
    path = tmp_path_factory.mktemp("config") / "blocked_domains.txt"
    path.write_text(f"# test policy\n{BLOCKED_DOMAIN}\n")
    return str(path)


@pytest.fixture(scope="session")
def proxy(tmp_path_factory, blocked_domains_file) -> int:
    """Start the proxy on a free loopback port; yield that port."""
    log_dir = tmp_path_factory.mktemp("logs")
    port = free_port()

    server = ProxyServer(
        host="127.0.0.1",
        port=port,
        blocked_domains_path=blocked_domains_file,
        metrics_path=str(log_dir / "metrics.csv"),
        access_log_path=str(log_dir / "access.log"),
        error_log_path=str(log_dir / "error.log"),
    )
    threading.Thread(target=server.start, daemon=True).start()
    _wait_until_listening(port)
    yield port
    server.stop()


def _wait_until_listening(port: int, timeout: float = 5.0) -> None:
    """Block until the accept loop is actually bound, or fail the test."""
    import time

    deadline = time.time() + timeout
    while time.time() < deadline:
        try:
            with socket.create_connection(("127.0.0.1", port), timeout=0.25):
                return
        except OSError:
            time.sleep(0.05)
    raise RuntimeError(f"proxy did not start listening on port {port}")
