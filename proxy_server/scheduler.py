"""QoS request scheduler for proxy connections."""

import queue
import socket
import threading
from typing import Dict, Optional, Tuple


class RequestScheduler:
    """Priority-based scheduler that dispatches client requests to handlers."""

    def __init__(self) -> None:
        self._queue: queue.PriorityQueue = queue.PriorityQueue()
        self._shutdown_event = threading.Event()
        self._worker_thread: Optional[threading.Thread] = None
        self._sequence = 0
        self._seq_lock = threading.Lock()

    def _next_sequence(self) -> int:
        with self._seq_lock:
            self._sequence += 1
            return self._sequence

    def add_request(self, client_socket: socket.socket, parsed_request: Dict[str, object]) -> None:
        """Queue a request with QoS priority derived from request size."""
        priority = self._estimate_priority(parsed_request)
        sequence = self._next_sequence()
        payload = (priority, sequence, client_socket, parsed_request)
        self._queue.put(payload)

    def get_next_request(self, timeout: float = 0.2) -> Optional[Tuple[int, int, socket.socket, Dict[str, object]]]:
        try:
            return self._queue.get(timeout=timeout)
        except queue.Empty:
            return None

    def start(self, dispatch_callback) -> None:
        """Start scheduler worker loop."""
        if self._worker_thread and self._worker_thread.is_alive():
            return

        self._shutdown_event.clear()

        def _worker() -> None:
            while not self._shutdown_event.is_set():
                item = self.get_next_request(timeout=0.2)
                if item is None:
                    continue
                _, _, client_socket, parsed_request = item
                try:
                    dispatch_callback(client_socket, parsed_request)
                finally:
                    self._queue.task_done()

        self._worker_thread = threading.Thread(target=_worker, daemon=True)
        self._worker_thread.start()

    def stop(self) -> None:
        self._shutdown_event.set()
        if self._worker_thread and self._worker_thread.is_alive():
            self._worker_thread.join(timeout=1.0)

    def get_queue_size(self) -> int:
        return self._queue.qsize()

    @staticmethod
    def _estimate_priority(parsed_request: Dict[str, object]) -> int:
        """Estimate request size and map it to QoS priority classes.

        1 = small/fast, 2 = normal, 3 = large/heavy
        """
        headers = parsed_request.get("headers", {})
        if not isinstance(headers, dict):
            return 2

        content_length = headers.get("content-length")
        if content_length is None:
            return 2

        try:
            size = int(content_length)
        except (TypeError, ValueError):
            return 2

        if size <= 1024:
            return 1
        if size <= 100 * 1024:
            return 2
        return 3
