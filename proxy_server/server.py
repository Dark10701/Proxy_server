"""Proxy server that accepts connections and delegates to client handlers."""

import socket
import threading
from typing import Tuple

from client_handler import ClientHandler
from filter_engine import FilterEngine
from logger import ProxyLogger
from metrics import MetricsLogger


class ProxyServer:
    """TCP listener that spawns a thread per incoming client connection."""

    def __init__(
        self,
        host: str,
        port: int,
        blocked_domains_path: str,
        metrics_path: str,
        access_log_path: str,
        error_log_path: str,
    ) -> None:
        self.host = host
        self.port = port
        self.blocked_domains_path = blocked_domains_path
        self.metrics_path = metrics_path
        self.access_log_path = access_log_path
        self.error_log_path = error_log_path
        self._shutdown_event = threading.Event()
        self.filter_engine = FilterEngine(self.blocked_domains_path)
        self.metrics_logger = MetricsLogger(self.metrics_path)
        self.logger = ProxyLogger(self.access_log_path, self.error_log_path)

    def start(self) -> None:
        """Start the TCP listener and accept clients forever."""
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

                handler = ClientHandler(
                    client_socket=client_socket,
                    client_address=client_addr,
                    filter_engine=self.filter_engine,
                    metrics_logger=self.metrics_logger,
                    logger=self.logger,
                )
                thread = threading.Thread(target=handler.handle, daemon=True)
                thread.start()

    def stop(self) -> None:
        self._shutdown_event.set()

    @staticmethod
    def format_address(address: Tuple[str, int]) -> str:
        return f"{address[0]}:{address[1]}"
