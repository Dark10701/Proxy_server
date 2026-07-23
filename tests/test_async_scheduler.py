"""Unit tests for the async admission gate."""

import asyncio

import pytest

from proxy_server.async_scheduler import AsyncScheduler
from proxy_server.scheduler import (
    PRIORITY_BULK,
    PRIORITY_INTERACTIVE,
    PRIORITY_NORMAL,
)


def run(coro):
    loop = asyncio.new_event_loop()
    try:
        return loop.run_until_complete(coro)
    finally:
        loop.close()


def test_admits_up_to_capacity():
    async def scenario():
        scheduler = AsyncScheduler(max_concurrent=3, max_queued=0)
        results = [await scheduler.acquire(PRIORITY_NORMAL) for _ in range(3)]
        # Capacity is full and nothing may queue, so the next is refused.
        results.append(await scheduler.acquire(PRIORITY_NORMAL))
        return results, scheduler.stats()

    results, stats = run(scenario())
    assert results == [True, True, True, False]
    assert stats["active"] == 3
    assert stats["refused"] == 1


def test_release_frees_a_slot():
    async def scenario():
        scheduler = AsyncScheduler(max_concurrent=1, max_queued=0)
        assert await scheduler.acquire(PRIORITY_NORMAL) is True
        assert await scheduler.acquire(PRIORITY_NORMAL) is False
        scheduler.release()
        return await scheduler.acquire(PRIORITY_NORMAL)

    assert run(scenario()) is True


def test_waiters_are_woken_in_priority_order():
    async def scenario():
        scheduler = AsyncScheduler(max_concurrent=1, max_queued=10)
        assert await scheduler.acquire(PRIORITY_NORMAL) is True

        order = []

        async def contender(priority, label):
            await scheduler.acquire(priority)
            order.append(label)

        tasks = [
            asyncio.create_task(contender(PRIORITY_BULK, "bulk")),
            asyncio.create_task(contender(PRIORITY_NORMAL, "normal")),
            asyncio.create_task(contender(PRIORITY_INTERACTIVE, "interactive")),
        ]
        await asyncio.sleep(0.05)  # let all three queue up

        for _ in range(3):
            scheduler.release()
            await asyncio.sleep(0.02)

        await asyncio.gather(*tasks)
        return order

    assert run(scenario()) == ["interactive", "normal", "bulk"]


def test_queue_limit_refuses_rather_than_growing():
    async def scenario():
        scheduler = AsyncScheduler(max_concurrent=1, max_queued=2)
        await scheduler.acquire(PRIORITY_NORMAL)

        waiters = [
            asyncio.create_task(scheduler.acquire(PRIORITY_NORMAL)) for _ in range(2)
        ]
        await asyncio.sleep(0.05)

        refused = await scheduler.acquire(PRIORITY_NORMAL)

        for _ in range(3):
            scheduler.release()
            await asyncio.sleep(0.01)
        await asyncio.gather(*waiters)
        return refused, scheduler.stats()

    refused, stats = run(scenario())
    assert refused is False
    assert stats["refused"] == 1


def test_cancelled_waiter_does_not_consume_a_slot():
    """A client that gives up while queued must not strand capacity."""

    async def scenario():
        scheduler = AsyncScheduler(max_concurrent=1, max_queued=5)
        await scheduler.acquire(PRIORITY_NORMAL)

        abandoned = asyncio.create_task(scheduler.acquire(PRIORITY_INTERACTIVE))
        await asyncio.sleep(0.05)
        abandoned.cancel()
        try:
            await abandoned
        except asyncio.CancelledError:
            pass

        later = asyncio.create_task(scheduler.acquire(PRIORITY_NORMAL))
        await asyncio.sleep(0.02)
        scheduler.release()
        return await asyncio.wait_for(later, timeout=2)

    assert run(scenario()) is True


@pytest.mark.parametrize("kwargs", [{"max_concurrent": 0}, {"max_queued": -1}])
def test_invalid_configuration_is_rejected(kwargs):
    with pytest.raises(ValueError):
        AsyncScheduler(**kwargs)
