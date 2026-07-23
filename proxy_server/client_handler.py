"""Handle a single client connection and proxy HTTP requests."""

import select
import socket
import threading
import time
from typing import Dict, Tuple
from urllib.parse import urlsplit

from proxy_server.filter_engine import FilterEngine
from proxy_server.http_parser import (
    build_forward_request,
    parse_http_request,
    parse_target_from_request,
)
from proxy_server.logger import ProxyLogger
from proxy_server.metrics import MetricsLogger

RATE_LIMIT_REQUESTS = 200
RATE_LIMIT_WINDOW_SECONDS = 60
RATE_LIMIT_WHITELIST = (
    "google.com",
    "youtube.com",
    "googlevideo.com",
    "ytimg.com",
    "amazon.in",
    "amazon.com",
)
_rate_limit_by_client_host: Dict[Tuple[str, str], list] = {}
_rate_limit_lock = threading.Lock()


def is_whitelisted(host: str) -> bool:
    host = (host or "").strip().lower().strip(".")
    if not host:
        return False
    return any(host == domain or host.endswith(f".{domain}") for domain in RATE_LIMIT_WHITELIST)


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
        self.client_ip, self.client_port = client_address
        self.client_id = f"{self.client_ip}:{self.client_port}"
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
            request_start_time = time.time()

            request_line, headers, body = parse_http_request(request_data)
            if not request_line:
                self._send_bad_request()
                return

            method, url, version = request_line
            if method.upper() == "CONNECT":
                rate_limit_host = url.split(":", 1)[0].strip("[]")
            else:
                rate_limit_host, _, _ = parse_target_from_request(url, headers)

            if method.upper() != "CONNECT" and self._is_rate_limited(self.client_ip, rate_limit_host):
                reason = "rate_limited"
                if method.upper() == "CONNECT":
                    target_host = url.split(":", 1)[0].strip("[]")
                else:
                    target_host, _, _ = parse_target_from_request(url, headers)
                latency_ms = int((time.time() - request_start_time) * 1000)
                response_size = self._send_too_many_requests()
                self._log_blocked_request(
                    reason=reason,
                    method=method,
                    url=url,
                    host=rate_limit_host,
                    latency_ms=latency_ms,
                    request_bytes=len(request_data),
                    response_bytes=response_size,
                )
                return

            if method.upper() == "CONNECT":
                self._handle_connect(request_data, method, url)
                return

            target_host, target_port, path = parse_target_from_request(url, headers)
            if not target_host:
                self._send_bad_request()
                return

            if self.filter_engine.is_blocked(target_host, url):
                reason = "blocked_domain"
                self.logger.info(
                    "Blocked request_type=%s client_id=%s host=%s url=%s",
                    method,
                    self.client_id,
                    target_host,
                    url,
                )
                latency_ms = int((time.time() - request_start_time) * 1000)
                response_size = self._send_forbidden(target_host)
                self._log_blocked_request(
                    reason=reason,
                    method=method,
                    url=url,
                    host=target_host,
                    latency_ms=latency_ms,
                    request_bytes=len(request_data),
                    response_bytes=response_size,
                )
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
            self.logger.error(
                "Timeout request_type=UNKNOWN client_id=%s host=UNKNOWN",
                self.client_id,
            )
        except Exception as exc:
            self.logger.error("Client handling error: %s", exc)
        finally:
            self.client_socket.close()

    def _is_rate_limited(self, client_ip: str, host: str) -> bool:
        """Return True when a client exceeds the configured request rate for a host."""
        normalized_host = (host or "").strip().lower().strip(".")
        if not normalized_host or is_whitelisted(normalized_host):
            return False
        now = time.time()
        window_start = now - RATE_LIMIT_WINDOW_SECONDS
        key = (client_ip, normalized_host)
        with _rate_limit_lock:
            timestamps = _rate_limit_by_client_host.get(key, [])
            timestamps = [ts for ts in timestamps if ts >= window_start]
            if len(timestamps) >= RATE_LIMIT_REQUESTS:
                _rate_limit_by_client_host[key] = timestamps
                return True
            timestamps.append(now)
            _rate_limit_by_client_host[key] = timestamps
            return False

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
                    "Forwarded request_type=%s client_id=%s host=%s:%s",
                    method,
                    self.client_id,
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
            client_ip=self.client_ip,
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
        start_time = time.time()
        target_host, target_port = self._parse_connect_target(url)
        if not target_host or target_port <= 0:
            self._send_bad_request()
            return

        if self.filter_engine.is_blocked(target_host, url):
            reason = "blocked_domain"
            self.logger.info(
                "Blocked request_type=%s client_id=%s host=%s url=%s",
                method,
                self.client_id,
                target_host,
                url,
            )
            latency_ms = int((time.time() - start_time) * 1000)
            response_size = self._send_forbidden(target_host)
            self._log_blocked_request(
                reason=reason,
                method=method,
                url=url,
                host=target_host,
                latency_ms=latency_ms,
                request_bytes=len(request_bytes),
                response_bytes=response_size,
            )
            return

        request_size = len(request_bytes)
        response_size = 0

        try:
            with socket.create_connection((target_host, target_port), timeout=10) as upstream_socket:
                self.client_socket.sendall(b"HTTP/1.1 200 Connection Established\r\n\r\n")
                self.logger.info(
                    "Established request_type=CONNECT client_id=%s host=%s:%s",
                    self.client_id,
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
            client_ip=self.client_ip,
            method=method,
            url=url,
            host=target_host,
            latency_ms=latency_ms,
            request_bytes=request_size,
            response_bytes=response_size,
            blocked=0,
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

    def _send_forbidden(self, host: str) -> int:
        response = (
            "HTTP/1.1 403 Forbidden\r\n"
            "Content-Type: text/plain\r\n"
            "Connection: close\r\n"
            "\r\n"
            f"Access to {host} is blocked by proxy policy."
        )
        response_bytes = response.encode("utf-8")
        self.client_socket.sendall(response_bytes)
        return len(response_bytes)

    def _send_too_many_requests(self) -> int:
        response = (
            "HTTP/1.1 429 Too Many Requests\r\n"
            "Content-Type: text/plain\r\n"
            "Connection: close\r\n"
            "Retry-After: 60\r\n"
            "\r\n"
            "Too many requests. Please try again later."
        )
        response_bytes = response.encode("utf-8")
        self.client_socket.sendall(response_bytes)
        return len(response_bytes)

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

    def _log_blocked_request(
        self,
        reason: str,
        method: str,
        url: str,
        host: str,
        latency_ms: int,
        request_bytes: int,
        response_bytes: int,
    ) -> None:
        """Log blocked request to metrics."""
        self.logger.info(
            "Blocked request_type=%s reason=%s client_id=%s host=%s url=%s",
            method,
            reason,
            self.client_id,
            host,
            url,
        )
        self.metrics_logger.log(
            client_ip=self.client_ip,
            method=method,
            url=url,
            host=host,
            latency_ms=latency_ms,
            request_bytes=request_bytes,
            response_bytes=response_bytes,
            blocked=1,
        )
