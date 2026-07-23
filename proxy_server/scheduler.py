"""Two-stage QoS scheduler sitting between the accept loop and the handlers.

The proxy previously spawned one unbounded thread per accepted socket,
so load was whatever clients decided to send. This replaces that with a
fixed pool and a bounded queue, which caps concurrency and makes
overload visible as backpressure instead of thread exhaustion.

Why two stages
--------------
Prioritising a request means knowing something about it, and nothing is
knowable at accept() time -- the bytes have not arrived yet. PR #22
enqueued at accept and prioritised on a `parsed_request` dict, but its
lookup was `headers.get("content-length")` while the parser stores
headers in their original case (`Content-Length`), so the lookup never
matched and every request took the default priority. Its QoS scheduler
was FIFO in practice.

So reading is split from serving:

    accept -> intake queue -> reader pool  (reads and parses the head)
                           -> priority queue -> worker pool (forwards)

Readers only touch the client socket, so a slow client occupies a
reader and not an upstream connection. By the time an item reaches the
priority queue its headers are known, so the ordering is real.
"""

import queue
import threading
from typing import Callable, Dict, Optional, Tuple

# Priority classes; lower value is served first.
PRIORITY_INTERACTIVE = 1
PRIORITY_NORMAL = 2
PRIORITY_BULK = 3

SMALL_BODY_BYTES = 1024
LARGE_BODY_BYTES = 100 * 1024


def estimate_priority(method: str, headers: Dict[str, str]) -> int:
    """Classify a request by how much work it is likely to be.

    Header lookup is case-insensitive: HTTP header names are
    case-insensitive per RFC 9110, and the parser preserves whatever
    casing the client sent.
    """
    normalised = {key.lower(): value for key, value in (headers or {}).items()}

    # A tunnel is open-ended, so it must never sit ahead of short requests.
    if (method or "").upper() == "CONNECT":
        return PRIORITY_BULK

    raw_length = normalised.get("content-length")
    if raw_length is None:
        # No body: a plain GET/HEAD, which is the interactive case.
        return PRIORITY_INTERACTIVE

    try:
        size = int(raw_length)
    except (TypeError, ValueError):
        return PRIORITY_NORMAL

    if size <= SMALL_BODY_BYTES:
        return PRIORITY_INTERACTIVE
    if size <= LARGE_BODY_BYTES:
        return PRIORITY_NORMAL
    return PRIORITY_BULK


class RequestScheduler:
    """Bounded, priority-ordered dispatch across a fixed pool of threads."""

    def __init__(
        self,
        reader_count: int = 8,
        worker_count: int = 16,
        intake_size: int = 256,
        queue_size: int = 256,
    ) -> None:
        self._intake: queue.Queue = queue.Queue(maxsize=intake_size)
        self._ready: queue.PriorityQueue = queue.PriorityQueue(maxsize=queue_size)
        self._shutdown = threading.Event()
        self._threads = []
        self._reader_count = max(reader_count, 1)
        self._worker_count = max(worker_count, 1)

        self._sequence = 0
        self._seq_lock = threading.Lock()

        self._stats_lock = threading.Lock()
        self._accepted = 0
        self._rejected_intake = 0
        self._rejected_ready = 0
        self._completed = 0

    def _next_sequence(self) -> int:
        """Tie-breaker that keeps equal priorities in arrival order."""
        with self._seq_lock:
            self._sequence += 1
            return self._sequence

    def submit_connection(self, connection) -> bool:
        """Offer a freshly accepted connection. False means saturated."""
        try:
            self._intake.put_nowait(connection)
        except queue.Full:
            with self._stats_lock:
                self._rejected_intake += 1
            return False
        with self._stats_lock:
            self._accepted += 1
        return True

    def _submit_ready(self, priority: int, payload) -> bool:
        try:
            self._ready.put_nowait((priority, self._next_sequence(), payload))
        except queue.Full:
            with self._stats_lock:
                self._rejected_ready += 1
            return False
        return True

    def start(
        self,
        read_callback: Callable[[object], Optional[Tuple[int, object]]],
        serve_callback: Callable[[object], None],
        on_overload: Optional[Callable[[object], None]] = None,
    ) -> None:
        """Spin up both pools.

        ``read_callback`` receives an accepted connection and returns
        ``(priority, payload)``, or None if the connection is finished
        with (bad request, client hung up). ``serve_callback`` receives
        the payload once it is scheduled. ``on_overload`` is called with
        a payload that could not be queued, so the caller can answer 503.
        """
        if self._threads:
            return
        self._shutdown.clear()

        for _ in range(self._reader_count):
            self._spawn(self._reader_loop, read_callback, on_overload)
        for _ in range(self._worker_count):
            self._spawn(self._worker_loop, serve_callback)

    def _spawn(self, target, *args) -> None:
        thread = threading.Thread(target=target, args=args, daemon=True)
        thread.start()
        self._threads.append(thread)

    def _reader_loop(self, read_callback, on_overload) -> None:
        while not self._shutdown.is_set():
            try:
                connection = self._intake.get(timeout=0.2)
            except queue.Empty:
                continue
            try:
                result = read_callback(connection)
                if result is None:
                    continue
                priority, payload = result
                if not self._submit_ready(priority, payload) and on_overload:
                    on_overload(payload)
            except Exception:
                # A single bad connection must not kill a reader thread.
                # read_callback is responsible for its own logging.
                pass
            finally:
                self._intake.task_done()

    def _worker_loop(self, serve_callback) -> None:
        while not self._shutdown.is_set():
            try:
                _, _, payload = self._ready.get(timeout=0.2)
            except queue.Empty:
                continue
            try:
                serve_callback(payload)
            except Exception:
                pass
            finally:
                with self._stats_lock:
                    self._completed += 1
                self._ready.task_done()

    def stop(self) -> None:
        self._shutdown.set()
        for thread in self._threads:
            thread.join(timeout=1.0)
        self._threads = []

    def stats(self) -> Dict[str, int]:
        with self._stats_lock:
            return {
                "accepted": self._accepted,
                "completed": self._completed,
                "rejected_intake": self._rejected_intake,
                "rejected_ready": self._rejected_ready,
                "intake_depth": self._intake.qsize(),
                "ready_depth": self._ready.qsize(),
                "readers": self._reader_count,
                "workers": self._worker_count,
            }
