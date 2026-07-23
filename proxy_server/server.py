"""Proxy server that accepts connections and dispatches them via the scheduler."""

import socket
import threading
from typing import Optional, Tuple

from proxy_server.client_handler import ClientHandler
from proxy_server.filter_engine import FilterEngine
from proxy_server.logger import ProxyLogger
from proxy_server.metrics import MetricsLogger
from proxy_server.rate_controller import RateController
from proxy_server.scheduler import RequestScheduler

LISTEN_BACKLOG = 100


class ProxyServer:
    """TCP listener feeding accepted connections into a bounded scheduler.

    The accept loop no longer spawns a thread per connection. It hands
    each socket to the scheduler, which reads it on a reader thread and
    then serves it on a worker thread in priority order. That caps the
    thread count and turns overload into an explicit 503 rather than
    unbounded thread growth.
    """

    def __init__(
        self,
        host: str,
        port: int,
        blocked_domains_path: str,
        metrics_path: str,
        access_log_path: str,
        error_log_path: str,
        reader_count: int = 8,
        worker_count: int = 16,
        queue_size: int = 256,
        adaptive_rate_limit: bool = True,
        rate_limit_requests: int = 200,
    ) -> None:
        self.rate_limit_requests = rate_limit_requests
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
        self.rate_controller = RateController() if adaptive_rate_limit else None
        self.scheduler = RequestScheduler(
            reader_count=reader_count,
            worker_count=worker_count,
            intake_size=queue_size,
            queue_size=queue_size,
        )

    def _build_handler(self, connection) -> ClientHandler:
        client_socket, client_addr = connection
        return ClientHandler(
            client_socket=client_socket,
            client_address=client_addr,
            filter_engine=self.filter_engine,
            metrics_logger=self.metrics_logger,
            logger=self.logger,
            rate_controller=self.rate_controller,
            rate_limit_requests=self.rate_limit_requests,
        )

    def _read_stage(self, connection) -> Optional[Tuple[int, ClientHandler]]:
        """Reader-thread stage: parse the head and classify the request."""
        handler = self._build_handler(connection)
        priority = handler.read_request()
        if priority is None:
            return None
        return priority, handler

    @staticmethod
    def _serve_stage(handler: ClientHandler) -> None:
        handler.serve()

    def _on_overload(self, handler: ClientHandler) -> None:
        """Ready queue is full: refuse rather than queue without bound."""
        self.logger.error(
            "Scheduler saturated, refusing client_id=%s", handler.client_id
        )
        try:
            handler._send_service_unavailable()
        except OSError:
            pass
        finally:
            handler._close()

    def start(self) -> None:
        """Start the TCP listener and accept clients forever."""
        self.scheduler.start(
            read_callback=self._read_stage,
            serve_callback=self._serve_stage,
            on_overload=self._on_overload,
        )

        with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as server_socket:
            server_socket.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
            server_socket.bind((self.host, self.port))
            server_socket.listen(LISTEN_BACKLOG)
            self.logger.info(
                "Proxy server listening on %s:%s", self.host, self.port
            )

            while not self._shutdown_event.is_set():
                try:
                    client_socket, client_addr = server_socket.accept()
                except OSError as exc:
                    if self._shutdown_event.is_set():
                        break
                    self.logger.error("Accept failed: %s", exc)
                    continue

                if not self.scheduler.submit_connection((client_socket, client_addr)):
                    # Intake is full. Closing immediately is the honest
                    # signal; holding the socket would just hide the
                    # overload from the client.
                    self.logger.error(
                        "Intake queue full, dropping connection from %s",
                        client_addr,
                    )
                    try:
                        client_socket.close()
                    except OSError:
                        pass

    def stop(self) -> None:
        self._shutdown_event.set()
        self.scheduler.stop()

    def stats(self) -> dict:
        """Scheduler and rate-controller state, for the dashboard."""
        data = {"scheduler": self.scheduler.stats()}
        if self.rate_controller is not None:
            data["rate_controller"] = self.rate_controller.stats()
        return data

    @staticmethod
    def format_address(address: Tuple[str, int]) -> str:
        return f"{address[0]}:{address[1]}"
