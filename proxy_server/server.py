"""Proxy server that accepts connections and delegates to client handlers."""

import socket
import threading
from typing import Dict, Tuple

from client_handler import ClientHandler
from filter_engine import FilterEngine
from logger import ProxyLogger
from metrics_store import MetricsStore
from rate_controller import RateController
from scheduler import RequestScheduler


class ProxyServer:
    """TCP listener that routes incoming client connections through QoS scheduling."""

    def __init__(
        self,
        host: str,
        port: int,
        blocked_domains_path: str,
        metrics_store: MetricsStore,
        access_log_path: str,
        error_log_path: str,
    ) -> None:
        self.host = host
        self.port = port
        self.blocked_domains_path = blocked_domains_path
        self.access_log_path = access_log_path
        self.error_log_path = error_log_path
        self.metrics_store = metrics_store
        self._shutdown_event = threading.Event()
        self.filter_engine = FilterEngine(self.blocked_domains_path)
        self.logger = ProxyLogger(self.access_log_path, self.error_log_path)
        self.rate_controller = RateController()
        self.scheduler = RequestScheduler()

    def start(self) -> None:
        """Start the TCP listener and accept clients forever."""
        self.scheduler.start(self._dispatch_scheduled_request)
        with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as server_socket:
            server_socket.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
            server_socket.bind((self.host, self.port))
            server_socket.listen(100)
            self.logger.info(
                "Proxy server listening on %s:%s", self.host, self.port
            )

            while not self._shutdown_event.is_set():
                try:
                    client_socket, client_addr = server_socket.accept()
                except OSError as exc:
                    self.logger.error("Accept failed: %s", exc)
                    continue

                parsed_request = self._parse_minimal_request_info(client_socket, client_addr)
                self.scheduler.add_request(
                    client_socket=client_socket,
                    parsed_request=parsed_request,
                )
        self.scheduler.stop()

    def stop(self) -> None:
        self._shutdown_event.set()
        self.scheduler.stop()

    def _dispatch_scheduled_request(
        self,
        client_socket: socket.socket,
        parsed_request: Dict[str, object],
    ) -> None:
        """Invoke existing client handler logic for scheduled requests."""
        client_address = parsed_request.get("client_address", ("unknown", 0))
        handler = ClientHandler(
            client_socket=client_socket,
            client_address=client_address,
            filter_engine=self.filter_engine,
            metrics_store=self.metrics_store,
            logger=self.logger,
            rate_controller=self.rate_controller,
        )
        handler.handle()

    def _parse_minimal_request_info(
        self,
        client_socket: socket.socket,
        client_addr: Tuple[str, int],
    ) -> Dict[str, object]:
        """Peek request headers without consuming bytes for QoS classification."""
        parsed: Dict[str, object] = {
            "client_address": client_addr,
            "headers": {},
        }
        try:
            client_socket.settimeout(1.0)
            peek_data = client_socket.recv(2048, socket.MSG_PEEK)
            if not peek_data:
                return parsed

            head_bytes, _, _ = peek_data.partition(b"\r\n\r\n")
            head_text = head_bytes.decode("iso-8859-1", errors="replace")
            lines = head_text.split("\r\n")
            if lines:
                parsed["request_line"] = lines[0]
            headers: Dict[str, str] = {}
            for line in lines[1:]:
                if ":" not in line:
                    continue
                key, value = line.split(":", 1)
                headers[key.strip().lower()] = value.strip()
            parsed["headers"] = headers
        except (socket.timeout, OSError):
            return parsed
        finally:
            client_socket.settimeout(10)

        return parsed

    @staticmethod
    def format_address(address: Tuple[str, int]) -> str:
        return f"{address[0]}:{address[1]}"
