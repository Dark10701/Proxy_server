"""Priority admission control for the asyncio proxy.

The threaded build needed explicit thread pools because a thread per
connection does not scale. Under an event loop concurrency is nearly
free, so the pools are gone -- but admission control is not, and for
the same reason as before: unbounded acceptance turns overload into
memory growth and uniformly bad latency instead of an honest refusal.

Two limits:

- ``max_concurrent`` caps requests being served upstream at once.
- ``max_queued`` caps how many may wait for a slot. Beyond that,
  ``acquire`` returns False and the caller answers 503.

Waiters are released in priority order rather than arrival order, using
the same classification as the threaded scheduler, so a queued
interactive GET is not stuck behind a queued bulk upload.
"""

import asyncio
import heapq
from typing import Dict, List, Optional, Tuple

from proxy_server.scheduler import estimate_priority  # noqa: F401  (re-exported)


class AsyncScheduler:
    """Bounded, priority-ordered admission gate."""

    def __init__(self, max_concurrent: int = 256, max_queued: int = 512) -> None:
        if max_concurrent < 1:
            raise ValueError("max_concurrent must be >= 1")
        if max_queued < 0:
            raise ValueError("max_queued must be >= 0")

        self._max_concurrent = max_concurrent
        self._max_queued = max_queued
        self._active = 0
        self._sequence = 0
        self._waiters: List[Tuple[int, int, asyncio.Future]] = []

        self._admitted = 0
        self._refused = 0
        self._peak_active = 0

    async def acquire(self, priority: int) -> bool:
        """Wait for a slot. False means the queue is full; answer 503."""
        if self._active < self._max_concurrent:
            self._take_slot()
            return True

        if len(self._waiters) >= self._max_queued:
            self._refused += 1
            return False

        loop = asyncio.get_running_loop()
        future = loop.create_future()
        self._sequence += 1
        heapq.heappush(self._waiters, (priority, self._sequence, future))

        try:
            await future
        except asyncio.CancelledError:
            # The client went away while queued. Drop the future so
            # release() does not hand a slot to nobody.
            self._discard(future)
            raise

        self._take_slot()
        return True

    def _take_slot(self) -> None:
        self._active += 1
        self._admitted += 1
        self._peak_active = max(self._peak_active, self._active)

    def _discard(self, future: asyncio.Future) -> None:
        self._waiters = [item for item in self._waiters if item[2] is not future]
        heapq.heapify(self._waiters)

    def release(self) -> None:
        """Give up a slot and wake the highest-priority waiter."""
        if self._active > 0:
            self._active -= 1

        while self._waiters:
            _, _, future = heapq.heappop(self._waiters)
            if not future.done():
                future.set_result(True)
                return

    def stats(self) -> Dict[str, int]:
        return {
            "active": self._active,
            "queued": len(self._waiters),
            "admitted": self._admitted,
            "refused": self._refused,
            "peak_active": self._peak_active,
            "max_concurrent": self._max_concurrent,
            "max_queued": self._max_queued,
        }


class _Slot:
    """Async context manager returned by ``AsyncScheduler.slot``."""

    def __init__(self, scheduler: AsyncScheduler, priority: int) -> None:
        self._scheduler = scheduler
        self._priority = priority
        self.admitted = False

    async def __aenter__(self) -> "_Slot":
        self.admitted = await self._scheduler.acquire(self._priority)
        return self

    async def __aexit__(self, exc_type, exc, tb) -> Optional[bool]:
        if self.admitted:
            self._scheduler.release()
        return None


def slot(scheduler: AsyncScheduler, priority: int) -> _Slot:
    """`async with slot(scheduler, priority) as s:` then check `s.admitted`."""
    return _Slot(scheduler, priority)
