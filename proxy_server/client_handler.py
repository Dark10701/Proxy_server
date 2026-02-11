"""Handle a single client connection and proxy HTTP requests."""

import select
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
from metrics import MetricsLogger


class ClientHandler:
    """Handles an individual client connection in its own thread."""

    def __init__(
        self,
        client_socket: socket.socket,
        client_address: Tuple[str, int],
        filter_engine: FilterEngine,
        metrics_logger: MetricsLogger,
        logger: ProxyLogger,
    ) -> None:
        self.client_socket = client_socket
        self.client_address = client_address
        self.filter_engine = filter_engine
        self.metrics_logger = metrics_logger
        self.logger = logger

    def handle(self) -> None:
        """Main entry point for processing a client request."""
        self.client_socket.settimeout(10)
        try:
            request_data = self._recv_http_request()
            if not request_data:
                return

            request_line, headers, body = parse_http_request(request_data)
            if not request_line:
                self._send_bad_request()
                return

            method, url, version = request_line

            if method.upper() == "CONNECT":
                self._handle_connect(request_data, method, url)
                return

            target_host, target_port, path = parse_target_from_request(url, headers)
            if not target_host:
                self._send_bad_request()
                return

            if self.filter_engine.is_blocked(target_host, url):
                self.logger.info(
                    "Blocked request from %s to %s",
                    self.client_address[0],
                    url,
                )
                self._send_forbidden(target_host)
                self._log_blocked_request(method=method, url=url, host=target_host)
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
                url=url,
            )
        except socket.timeout:
            self.logger.error("Timeout from client %s", self.client_address[0])
        except Exception as exc:
            self.logger.error("Client handling error: %s", exc)
        finally:
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
        url: str,
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
            return

        latency_ms = int((time.time() - start_time) * 1000)
        self.metrics_logger.log(
            client_ip=self.client_address[0],
            method=method,
            url=url,
            host=target_host,
            latency_ms=latency_ms,
            request_bytes=request_size,
            response_bytes=response_size,
            blocked=0,
        )

    def _handle_connect(self, request_bytes: bytes, method: str, url: str) -> None:
        """Handle HTTPS tunneling using HTTP CONNECT."""
        target_host, target_port = self._parse_connect_target(url)
        if not target_host or target_port <= 0:
            self._send_bad_request()
            return

        if self.filter_engine.is_blocked(target_host, url):
            self.logger.info(
                "Blocked request from %s to %s",
                self.client_address[0],
                url,
            )
            self._send_forbidden(target_host)
            self._log_blocked_request(method=method, url=url, host=target_host)
            return

        start_time = time.time()
        request_size = len(request_bytes)
        response_size = 0

        try:
            with socket.create_connection((target_host, target_port), timeout=10) as upstream_socket:
                self.client_socket.sendall(b"HTTP/1.1 200 Connection Established\r\n\r\n")
                self.logger.info(
                    "Established CONNECT tunnel to %s:%s",
                    target_host,
                    target_port,
                )
                response_size = self._tunnel_bidirectional(upstream_socket)
        except Exception as exc:
            self.logger.error("CONNECT upstream error for %s:%s: %s", target_host, target_port, exc)
            self._send_bad_gateway()
            return

        latency_ms = int((time.time() - start_time) * 1000)
        self.metrics_logger.log(
            client_ip=self.client_address[0],
            method=method,
            url=url,
            host=target_host,
            latency_ms=latency_ms,
            request_bytes=request_size,
            response_bytes=response_size,
            blocked=0,
        )

    def _log_blocked_request(self, method: str, url: str, host: str) -> None:
        """Persist blocked request events in metrics."""
        self.metrics_logger.log(
            client_ip=self.client_address[0],
            method=method,
            url=url,
            host=host,
            latency_ms=0,
            request_bytes=0,
            response_bytes=0,
            blocked=1,
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

    def _tunnel_bidirectional(self, upstream_socket: socket.socket) -> int:
        """Tunnel bytes between client and upstream until one side closes."""
        tunneled_from_upstream = 0
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
                    return tunneled_from_upstream
                dst.sendall(data)
                if src is upstream_socket:
                    tunneled_from_upstream += len(data)

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
