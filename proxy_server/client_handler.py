import socket
import time

from http_parser import parse_http_request
from metrics import MetricsLogger

METRICS_LOGGER = MetricsLogger("logs/metrics.csv")


class ClientHandler:
    def __init__(
        self,
        client_socket,
        client_address,
        filter_engine,
        metrics_logger,
        logger,
    ):
        self.client_socket = client_socket
        self.client_address = client_address
        self.filter_engine = filter_engine
        self.metrics_logger = metrics_logger
        self.logger = logger

    def handle(self):
        try:
            request = self.client_socket.recv(8192).decode(errors="ignore")
            if not request:
                self.client_socket.close()
                return

            method, url, host, port = parse_http_request(request)

            if not host:
                self.client_socket.close()
                return

            if self.filter_engine and self.filter_engine.is_blocked(host, url):
                blocked_response = (
                    "HTTP/1.1 403 Forbidden\r\n"
                    "Content-Type: text/plain\r\n"
                    "Connection: close\r\n"
                    "\r\n"
                    "Access blocked by proxy policy."
                )
                self.client_socket.sendall(blocked_response.encode())
                if self.logger:
                    self.logger.info(
                        "Blocked request from %s to %s", self.client_address[0], host
                    )
                self.client_socket.close()
                return

            start_time = time.monotonic()
            response_bytes = 0

            # CONNECT TO REAL DESTINATION (use Host header-derived target only)
            upstream_host = host
            upstream_port = port
            server_socket = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
            server_socket.connect((upstream_host, upstream_port))

            server_socket.sendall(request.encode())

            while True:
                response = server_socket.recv(8192)
                if not response:
                    break
                response_bytes += len(response)
                self.client_socket.sendall(response)

            # Metrics recorded after final response byte is sent to the client.
            latency_ms = int((time.monotonic() - start_time) * 1000)
            client_ip = ""
            try:
                client_ip = self.client_socket.getpeername()[0]
            except OSError:
                client_ip = ""

            metrics = self.metrics_logger or METRICS_LOGGER
            metrics.log(
                client_ip=client_ip,
                host=host,
                latency_ms=latency_ms,
                response_bytes=response_bytes,
            )

            server_socket.close()
            self.client_socket.close()
        except Exception as e:
            if self.logger:
                self.logger.error("Upstream error: %s", e)
            self.client_socket.close()
