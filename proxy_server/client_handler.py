"""Handle a single client connection and proxy HTTP requests."""

import select
import ssl
import socket
import time
from typing import Dict, Tuple
from urllib.parse import urlsplit

from filter_engine import FilterEngine
from http_parser import (
    build_forward_request,
    parse_http_request,
    parse_target_from_request,
)
from logger import ProxyLogger
from metrics_store import MetricsStore
from rate_controller import RateController


class ClientHandler:
    """Handles an individual client connection in its own thread."""

    def __init__(
        self,
        client_socket: socket.socket,
        client_address: Tuple[str, int],
        filter_engine: FilterEngine,
        metrics_store: MetricsStore,
        logger: ProxyLogger,
        rate_controller: RateController,
    ) -> None:
        self.client_socket = client_socket
        self.client_address = client_address
        self.filter_engine = filter_engine
        self.metrics_store = metrics_store
        self.logger = logger
        self.rate_controller = rate_controller

    def handle(self) -> None:
        """Main entry point for processing a client request."""
        self.client_socket.settimeout(10)
        self.metrics_store.increment_active_connections()
        try:
            while not self.rate_controller.allow_request():
                time.sleep(0.05)

            request_data = self._recv_http_request()
            if not request_data:
                return

            request_line, headers, body = parse_http_request(request_data)
            if not request_line:
                self._send_bad_request()
                self.metrics_store.record_request(
                    client_ip=self.client_address[0],
                    method="UNKNOWN",
                    host="",
                    status_code=400,
                    latency_ms=0,
                    bytes_sent=0,
                    bytes_received=0,
                    blocked=False,
                )
                return

            method, url, version = request_line

            if method.upper() == "CONNECT":
                self._handle_connect(request_data, method, url)
                return

            target_host, target_port, path = parse_target_from_request(url, headers)
            if not target_host:
                self._send_bad_request()
                self.metrics_store.record_request(
                    client_ip=self.client_address[0],
                    method=method,
                    host="",
                    status_code=400,
                    latency_ms=0,
                    bytes_sent=0,
                    bytes_received=0,
                    blocked=False,
                )
                return

            if self.filter_engine.is_blocked(target_host, url):
                self.logger.info(
                    "Blocked request from %s to %s",
                    self.client_address[0],
                    url,
                )
                self._send_forbidden(target_host)
                self._log_blocked_request(method=method, host=target_host)
                return

            # Convert absolute-form request line to origin-form.
            # Example: "GET http://neverssl.com/ HTTP/1.1" -> "GET / HTTP/1.1"
            if path.lower().startswith("http://") or path.lower().startswith("https://"):
                parsed = urlsplit(path)
                path = parsed.path or "/"
                if parsed.query:
                    path = f"{path}?{parsed.query}"

            forward_bytes = build_forward_request(
                method=method,
                path=path,
                version=version,
                headers=headers,
                body=body,
            )
            self._proxy_request(
                target_host=target_host,
                target_port=target_port,
                request_bytes=forward_bytes,
                method=method,
            )
        except socket.timeout:
            self.logger.error("Timeout from client %s", self.client_address[0])
        except Exception as exc:
            self.logger.error("Client handling error: %s", exc)
        finally:
            self.metrics_store.decrement_active_connections()
            self.client_socket.close()

    def _recv_http_request(self) -> bytes:
        """Receive the full HTTP request from the client socket."""
        buffer = bytearray()
        while b"\r\n\r\n" not in buffer:
            chunk = self.client_socket.recv(4096)
            if not chunk:
                return b""
            buffer.extend(chunk)
            if len(buffer) > 65536:
                break

        header_bytes, _, remaining = buffer.partition(b"\r\n\r\n")
        headers_text = header_bytes.decode("iso-8859-1", errors="replace")
        content_length = 0
        for line in headers_text.split("\r\n")[1:]:
            if line.lower().startswith("content-length:"):
                try:
                    content_length = int(line.split(":", 1)[1].strip())
                except ValueError:
                    content_length = 0
                break

        body = remaining
        while len(body) < content_length:
            chunk = self.client_socket.recv(4096)
            if not chunk:
                break
            body += chunk

        return header_bytes + b"\r\n\r\n" + body

    def _proxy_request(
        self,
        target_host: str,
        target_port: int,
        request_bytes: bytes,
        method: str,
    ) -> None:
        """Forward the HTTP request to the destination server and relay response."""
        start_time = time.time()
        request_size = len(request_bytes)
        response_size = 0

        try:
            with socket.create_connection(
                (target_host, target_port), timeout=10
            ) as upstream_socket:
                upstream_socket.sendall(request_bytes)
                self.logger.info(
                    "Forwarded %s request to %s:%s",
                    method,
                    target_host,
                    target_port,
                )

                while True:
                    data = upstream_socket.recv(4096)
                    if not data:
                        break
                    response_size += len(data)
                    self.client_socket.sendall(data)
        except Exception as exc:
            self.logger.error("Upstream error for %s: %s", target_host, exc)
            self._send_bad_gateway()
            latency_ms = int((time.time() - start_time) * 1000)
            self.rate_controller.record_latency(latency_ms)
            self.metrics_store.record_request(
                client_ip=self.client_address[0],
                method=method,
                host=target_host,
                status_code=502,
                latency_ms=latency_ms,
                bytes_sent=request_size,
                bytes_received=response_size,
                blocked=False,
            )
            return

        latency_ms = int((time.time() - start_time) * 1000)
        self.rate_controller.record_latency(latency_ms)
        self.metrics_store.record_request(
            client_ip=self.client_address[0],
            method=method,
            host=target_host,
            status_code=200,
            latency_ms=latency_ms,
            bytes_sent=request_size,
            bytes_received=response_size,
            blocked=False,
        )

    def _handle_connect(self, request_bytes: bytes, method: str, url: str) -> None:
        """Handle HTTPS tunneling using HTTP CONNECT."""
        target_host, target_port = self._parse_connect_target(url)
        if not target_host or target_port <= 0:
            self._send_bad_request()
            self.metrics_store.record_request(
                client_ip=self.client_address[0],
                method=method,
                host="",
                status_code=400,
                latency_ms=0,
                bytes_sent=0,
                bytes_received=0,
                blocked=False,
            )
            return

        if self.filter_engine.is_blocked(target_host, url):
            self.logger.info(
                "Blocked request from %s to %s",
                self.client_address[0],
                url,
            )
            self._send_forbidden(target_host)
            self._log_blocked_request(method=method, host=target_host)
            return

        start_time = time.time()
        request_size = len(request_bytes)
        bytes_client_to_upstream = 0
        bytes_upstream_to_client = 0

        # Record CONNECT visibility immediately so HTTPS domains appear in dashboard.
        self.metrics_store.record_request(
            client_ip=self.client_address[0],
            method=method,
            host=target_host,
            status_code=200,
            latency_ms=0,
            bytes_sent=0,
            bytes_received=0,
            blocked=False,
        )

        try:
            with socket.create_connection((target_host, target_port), timeout=10) as upstream_socket:
                self.client_socket.sendall(b"HTTP/1.1 200 Connection Established\r\n\r\n")
                self.logger.info(
                    "Established CONNECT tunnel to %s:%s",
                    target_host,
                    target_port,
                )
                if ENABLE_HTTPS_INSPECTION:
                    try:
                        (
                            bytes_client_to_upstream,
                            bytes_upstream_to_client,
                        ) = self._inspect_tls_tunnel(upstream_socket, target_host)
                    except Exception as exc:
                        self.logger.error(
                            "HTTPS inspection failed for %s:%s, falling back to TCP tunnel: %s",
                            target_host,
                            target_port,
                            exc,
                        )
                        (
                            bytes_client_to_upstream,
                            bytes_upstream_to_client,
                        ) = self._tunnel_bidirectional(upstream_socket)
                else:
                    (
                        bytes_client_to_upstream,
                        bytes_upstream_to_client,
                    ) = self._tunnel_bidirectional(upstream_socket)
        except Exception as exc:
            self.logger.error("CONNECT upstream error for %s:%s: %s", target_host, target_port, exc)
            self._send_bad_gateway()
            latency_ms = int((time.time() - start_time) * 1000)
            self.rate_controller.record_latency(latency_ms)
            self.metrics_store.record_request(
                client_ip=self.client_address[0],
                method=method,
                host=target_host,
                status_code=502,
                latency_ms=latency_ms,
                bytes_sent=request_size,
                bytes_received=bytes_upstream_to_client,
                blocked=False,
            )
            return

        latency_ms = int((time.time() - start_time) * 1000)
        self.rate_controller.record_latency(latency_ms)
        self.metrics_store.record_request(
            client_ip=self.client_address[0],
            method=method,
            host=target_host,
            status_code=200,
            latency_ms=latency_ms,
            bytes_sent=bytes_client_to_upstream,
            bytes_received=bytes_upstream_to_client,
            blocked=False,
        )

    def _parse_connect_target(self, authority: str) -> Tuple[str, int]:
        """Parse CONNECT authority-form target (host:port)."""
        value = (authority or "").strip()
        if not value:
            return "", 0

        # IPv6 authority form: [::1]:443
        if value.startswith("["):
            bracket_end = value.find("]")
            if bracket_end == -1:
                return "", 0
            host = value[1:bracket_end]
            remainder = value[bracket_end + 1 :]
            if not remainder.startswith(":"):
                return "", 0
            port_str = remainder[1:]
        else:
            if ":" not in value:
                return "", 0
            host, port_str = value.rsplit(":", 1)

        try:
            port = int(port_str)
        except ValueError:
            return "", 0

        return host.strip(), port

    def _tunnel_bidirectional(self, upstream_socket: socket.socket) -> Tuple[int, int]:
        """Tunnel bytes between client and upstream until one side closes."""
        bytes_client_to_upstream = 0
        bytes_upstream_to_client = 0
        sockets = [self.client_socket, upstream_socket]
        self.client_socket.settimeout(None)
        upstream_socket.settimeout(None)

        while True:
            readable, _, _ = select.select(sockets, [], [], 30)
            if not readable:
                continue

            for src in readable:
                dst = upstream_socket if src is self.client_socket else self.client_socket
                data = src.recv(4096)
                if not data:
                    return bytes_client_to_upstream, bytes_upstream_to_client
                dst.sendall(data)
                if src is self.client_socket:
                    bytes_client_to_upstream += len(data)
                else:
                    bytes_upstream_to_client += len(data)

    def _inspect_tls_tunnel(
        self,
        upstream_socket: socket.socket,
        target_host: str,
    ) -> Tuple[int, int]:
        """Optional educational HTTPS inspection mode (disabled by default)."""
        if not HTTPS_INSPECTION_CERT_FILE or not HTTPS_INSPECTION_KEY_FILE:
            raise RuntimeError("Inspection certificate/key not configured")

        client_context = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
        client_context.load_cert_chain(
            certfile=HTTPS_INSPECTION_CERT_FILE,
            keyfile=HTTPS_INSPECTION_KEY_FILE,
        )
        upstream_context = ssl.create_default_context()

        with client_context.wrap_socket(self.client_socket, server_side=True) as client_ssl, upstream_context.wrap_socket(
            upstream_socket, server_hostname=target_host
        ) as upstream_ssl:
            client_ssl.settimeout(1.0)
            upstream_ssl.settimeout(1.0)
            bytes_client_to_upstream = 0
            bytes_upstream_to_client = 0

            while True:
                try:
                    request_chunk = client_ssl.recv(8192)
                except socket.timeout:
                    continue
                if not request_chunk:
                    break

                bytes_client_to_upstream += len(request_chunk)
                print("HTTPS REQUEST:", request_chunk.decode(errors="ignore"))
                upstream_ssl.sendall(request_chunk)

                idle_cycles = 0
                while idle_cycles < 2:
                    try:
                        response_chunk = upstream_ssl.recv(8192)
                    except socket.timeout:
                        idle_cycles += 1
                        continue
                    if not response_chunk:
                        return bytes_client_to_upstream, bytes_upstream_to_client

                    bytes_upstream_to_client += len(response_chunk)
                    client_ssl.sendall(response_chunk)
                    idle_cycles = 0

            return bytes_client_to_upstream, bytes_upstream_to_client

    def _send_forbidden(self, host: str) -> None:
        response = (
            "HTTP/1.1 403 Forbidden\r\n"
            "Content-Type: text/plain\r\n"
            "Connection: close\r\n"
            "\r\n"
            f"Access to {host} is blocked by proxy policy."
        )
        self.client_socket.sendall(response.encode("utf-8"))

    def _send_bad_request(self) -> None:
        response = (
            "HTTP/1.1 400 Bad Request\r\n"
            "Content-Type: text/plain\r\n"
            "Connection: close\r\n"
            "\r\n"
            "Malformed request received by proxy."
        )
        self.client_socket.sendall(response.encode("utf-8"))

    def _send_bad_gateway(self) -> None:
        response = (
            "HTTP/1.1 502 Bad Gateway\r\n"
            "Content-Type: text/plain\r\n"
            "Connection: close\r\n"
            "\r\n"
            "Proxy could not reach the upstream server."
        )
        self.client_socket.sendall(response.encode("utf-8"))

    def _log_blocked_request(self, method: str, host: str) -> None:
        """Log blocked request in metrics store."""
        self.metrics_store.record_request(
            client_ip=self.client_address[0],
            method=method,
            host=host,
            status_code=403,
            latency_ms=0,
            bytes_sent=0,
            bytes_received=0,
            blocked=True,
        )
