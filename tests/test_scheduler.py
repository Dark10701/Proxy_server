"""Unit tests for request prioritisation and the bounded scheduler."""

import threading
import time

from proxy_server.scheduler import (
    PRIORITY_BULK,
    PRIORITY_INTERACTIVE,
    PRIORITY_NORMAL,
    RequestScheduler,
    estimate_priority,
)


def test_bodyless_get_is_interactive():
    assert estimate_priority("GET", {}) == PRIORITY_INTERACTIVE


def test_connect_is_bulk():
    """A tunnel is open-ended and must not sit ahead of short requests."""
    assert estimate_priority("CONNECT", {}) == PRIORITY_BULK


def test_content_length_lookup_is_case_insensitive():
    """The defect in PR #22: it looked up 'content-length' while the
    parser stores whatever casing the client sent, so real requests
    carrying 'Content-Length' always fell through to the default."""
    big = {"Content-Length": str(200 * 1024)}

    assert estimate_priority("POST", big) == PRIORITY_BULK
    assert estimate_priority("POST", {"content-length": str(200 * 1024)}) == PRIORITY_BULK
    assert estimate_priority("POST", {"CONTENT-LENGTH": "10"}) == PRIORITY_INTERACTIVE


def test_body_size_maps_to_classes():
    assert estimate_priority("POST", {"Content-Length": "500"}) == PRIORITY_INTERACTIVE
    assert estimate_priority("POST", {"Content-Length": "50000"}) == PRIORITY_NORMAL
    assert estimate_priority("POST", {"Content-Length": "5000000"}) == PRIORITY_BULK


def test_unparseable_content_length_falls_back_to_normal():
    assert estimate_priority("POST", {"Content-Length": "banana"}) == PRIORITY_NORMAL


def test_intake_rejects_when_full():
    scheduler = RequestScheduler(intake_size=2)

    assert scheduler.submit_connection("a") is True
    assert scheduler.submit_connection("b") is True
    assert scheduler.submit_connection("c") is False
    assert scheduler.stats()["rejected_intake"] == 1


def test_higher_priority_is_served_first():
    """Queue work while no worker can drain it, then let one run."""
    scheduler = RequestScheduler(reader_count=1, worker_count=1)
    served = []
    release = threading.Event()

    def read(connection):
        # Hold the first item back so the rest queue up behind it.
        return connection

    def serve(payload):
        release.wait(timeout=5)
        served.append(payload)

    scheduler.start(read_callback=read, serve_callback=serve)

    # Submit bulk first, interactive last; priority should reorder them.
    for item in [
        (PRIORITY_BULK, "bulk"),
        (PRIORITY_NORMAL, "normal"),
        (PRIORITY_INTERACTIVE, "interactive"),
    ]:
        scheduler.submit_connection(item)
        time.sleep(0.05)

    release.set()
    deadline = time.time() + 5
    while len(served) < 3 and time.time() < deadline:
        time.sleep(0.02)
    scheduler.stop()

    assert len(served) == 3
    # The first item was already in flight when the others arrived; the
    # two that queued behind it must come out in priority order.
    assert served[1:] == ["interactive", "normal"]


def test_reader_returning_none_drops_the_connection():
    scheduler = RequestScheduler(reader_count=1, worker_count=1)
    served = []

    scheduler.start(
        read_callback=lambda connection: None,
        serve_callback=served.append,
    )
    scheduler.submit_connection("ignored")
    time.sleep(0.3)
    scheduler.stop()

    assert served == []


def test_reader_exception_does_not_kill_the_pool():
    scheduler = RequestScheduler(reader_count=1, worker_count=1)
    served = []

    def read(connection):
        if connection == "boom":
            raise RuntimeError("bad connection")
        return (PRIORITY_NORMAL, connection)

    scheduler.start(read_callback=read, serve_callback=served.append)
    scheduler.submit_connection("boom")
    time.sleep(0.1)
    scheduler.submit_connection("good")

    deadline = time.time() + 5
    while not served and time.time() < deadline:
        time.sleep(0.02)
    scheduler.stop()

    assert served == ["good"]


def test_overload_callback_fires_when_ready_queue_is_full():
    scheduler = RequestScheduler(reader_count=1, worker_count=1, queue_size=1)
    refused = []
    block = threading.Event()

    def serve(payload):
        block.wait(timeout=5)

    scheduler.start(
        read_callback=lambda connection: (PRIORITY_NORMAL, connection),
        serve_callback=serve,
        on_overload=refused.append,
    )

    for index in range(8):
        scheduler.submit_connection(f"conn-{index}")
    time.sleep(0.6)
    block.set()
    scheduler.stop()

    assert refused, "expected at least one connection to be refused"
    assert scheduler.stats()["rejected_ready"] == len(refused)
