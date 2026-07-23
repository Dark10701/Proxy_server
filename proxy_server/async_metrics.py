"""Async-safe metrics writer.

The threaded MetricsLogger takes a mutex per row and fsyncs each one.
Under an event loop both are wrong: the mutex guards against a
concurrency that no longer exists, and a blocking write plus fsync on
the loop thread stalls every other connection while the disk catches up.

This replaces both with a single writer task draining an asyncio.Queue.
Only one coroutine ever touches the file, so no lock is needed at all,
and the actual write happens in a worker thread so the loop keeps
running. Rows are batched and flushed on an interval instead of fsynced
individually.

The durability trade is deliberate and worth stating plainly: an abrupt
kill can lose up to FLUSH_INTERVAL_SECONDS of metrics rows. These are
observability records, not transactions, and paying an fsync per
request to protect them would dominate the very latency they measure.
"""

import asyncio
import csv
from pathlib import Path
from typing import List, Optional

from proxy_server.metrics import MetricsLogger

FLUSH_INTERVAL_SECONDS = 1.0
QUEUE_MAXSIZE = 10000


class AsyncMetricsLogger:
    """Queue-backed CSV metrics writer with a single writer task."""

    FIELDNAMES = MetricsLogger.FIELDNAMES

    def __init__(
        self,
        metrics_path: str,
        flush_interval: float = FLUSH_INTERVAL_SECONDS,
        queue_maxsize: int = QUEUE_MAXSIZE,
    ) -> None:
        self.metrics_path = Path(metrics_path)
        self.metrics_path.parent.mkdir(parents=True, exist_ok=True)
        self._flush_interval = flush_interval
        self._queue: asyncio.Queue = asyncio.Queue(maxsize=queue_maxsize)
        self._task: Optional[asyncio.Task] = None
        self._closing = False
        self.dropped_rows = 0

        # Reuse the threaded logger's header handling so both writers
        # produce byte-identical files and the dashboard needs no changes.
        MetricsLogger(str(self.metrics_path))

    def start(self) -> None:
        if self._task is None:
            self._task = asyncio.create_task(self._writer_loop())

    def log(
        self,
        client_ip: str,
        method: str,
        url: str,
        host: str,
        latency_ms: int,
        request_bytes: int,
        response_bytes: int,
        blocked: int = 0,
    ) -> None:
        """Hand a row to the writer. Never blocks the caller.

        If the queue is full the row is dropped and counted rather than
        applying backpressure: metrics must not become the reason a
        request stalls.
        """
        import time

        row = [
            time.strftime("%Y-%m-%d %H:%M:%S", time.localtime()),
            client_ip,
            method,
            url,
            host,
            latency_ms,
            request_bytes,
            response_bytes,
            blocked,
        ]
        try:
            self._queue.put_nowait(row)
        except asyncio.QueueFull:
            self.dropped_rows += 1

    async def _writer_loop(self) -> None:
        pending: List[list] = []
        try:
            while True:
                try:
                    row = await asyncio.wait_for(
                        self._queue.get(), timeout=self._flush_interval
                    )
                    pending.append(row)
                    # Opportunistically drain whatever else is waiting.
                    while not self._queue.empty() and len(pending) < 500:
                        pending.append(self._queue.get_nowait())
                except asyncio.TimeoutError:
                    pass

                if pending:
                    rows, pending = pending, []
                    await asyncio.to_thread(self._write_rows, rows)

                if self._closing and self._queue.empty():
                    return
        except asyncio.CancelledError:
            if pending:
                # Best effort: get what we have to disk before going away.
                self._write_rows(pending)
            raise

    def _write_rows(self, rows: List[list]) -> None:
        """Blocking write, executed off the event loop."""
        with self.metrics_path.open("a", newline="") as csv_file:
            csv.writer(csv_file).writerows(rows)

    async def close(self) -> None:
        """Drain the queue and stop the writer task."""
        self._closing = True
        if self._task is None:
            return
        try:
            await asyncio.wait_for(self._task, timeout=5.0)
        except asyncio.TimeoutError:
            self._task.cancel()
            try:
                await self._task
            except (asyncio.CancelledError, Exception):
                pass
        finally:
            self._task = None
