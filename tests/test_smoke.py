"""End-to-end smoke tests for the proxy.

Three behaviours are the contract every later refactor must preserve:
forward a plain HTTP request, refuse a blocked domain with 403, and
complete a CONNECT tunnel.
"""

import socket

from tests.conftest import BLOCKED_DOMAIN, ORIGIN_BODY


def send_via_proxy(proxy_port: int, payload: bytes, read_timeout: float = 10.0) -> bytes:
    """Send raw bytes to the proxy and read until it closes the connection."""
    with socket.create_connection(("127.0.0.1", proxy_port), timeout=read_timeout) as sock:
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


def test_forwards_plain_http_request(proxy, origin_server):
    """An allowed absolute-form GET reaches the origin and comes back."""
    host, port = origin_server
    request = (
        f"GET http://{host}:{port}/ HTTP/1.1\r\n"
        f"Host: {host}:{port}\r\n"
        "\r\n"
    ).encode()

    response = send_via_proxy(proxy, request)

    assert response.startswith(b"HTTP/1.1 200"), response[:120]
    assert ORIGIN_BODY in response


def test_blocked_domain_returns_403(proxy):
    """A domain listed in the policy file is refused before any upstream call."""
    request = (
        f"GET http://{BLOCKED_DOMAIN}/ HTTP/1.1\r\n"
        f"Host: {BLOCKED_DOMAIN}\r\n"
        "\r\n"
    ).encode()

    response = send_via_proxy(proxy, request)

    assert response.startswith(b"HTTP/1.1 403"), response[:120]
    assert BLOCKED_DOMAIN.encode() in response


def test_connect_tunnel_relays_bytes(proxy, echo_server):
    """CONNECT is answered with 200, then bytes flow both ways untouched."""
    host, port = echo_server

    with socket.create_connection(("127.0.0.1", proxy), timeout=10) as sock:
        sock.settimeout(10)
        sock.sendall(
            f"CONNECT {host}:{port} HTTP/1.1\r\nHost: {host}:{port}\r\n\r\n".encode()
        )

        established = sock.recv(4096)
        assert established.startswith(b"HTTP/1.1 200"), established[:120]

        # The tunnel is opaque: whatever we write should come straight back
        # from the echo server on the far side.
        secret = b"tunnelled-payload-42"
        sock.sendall(secret)

        echoed = b""
        while len(echoed) < len(secret):
            chunk = sock.recv(4096)
            if not chunk:
                break
            echoed += chunk

    assert echoed == secret
