"""End-to-end caching through the running proxy.

Uses an in-process stand-in for redis.asyncio so the behaviour is
exercised without requiring a Redis server in CI. The origin counts how
many times it is actually contacted, which is the real assertion: a
cache hit must not reach the origin at all.
"""

import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

import pytest

from tests.test_async_smoke import AsyncProxyFixture, send_via_proxy

HITS = {"count": 0}


class _CachingOrigin(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"

    def do_GET(self):  # noqa: N802
        HITS["count"] += 1
        if self.path.startswith("/nocache"):
            cache_control = "no-store"
        elif self.path.startswith("/private"):
            cache_control = "private, max-age=600"
        else:
            cache_control = "max-age=600"

        body = f"origin-response-{HITS['count']}".encode()
        self.send_response(200)
        self.send_header("Content-Type", "text/plain")
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", cache_control)
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, *args):
        pass


class _FakeRedis:
    def __init__(self):
        self.store = {}

    async def get(self, key):
        return self.store.get(key)

    async def set(self, key, value, ex=None):
        self.store[key] = value

    async def ping(self):
        return True

    async def aclose(self):
        pass


@pytest.fixture
def caching_origin():
    HITS["count"] = 0
    server = ThreadingHTTPServer(("127.0.0.1", 0), _CachingOrigin)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    yield server.server_address
    server.shutdown()
    server.server_close()


@pytest.fixture
def cached_proxy(tmp_path_factory):
    config = tmp_path_factory.mktemp("cacheconf") / "blocked_domains.txt"
    config.write_text("# none\n")
    log_dir = tmp_path_factory.mktemp("cachelogs")

    fixture = AsyncProxyFixture(
        str(config), str(log_dir / "metrics.csv"), log_dir
    )
    port = fixture.start()
    # Swap in the in-process backend after start(), so the real connect
    # attempt (which fails, and must fail harmlessly) has already run.
    fixture.server.cache._client = _FakeRedis()
    fixture.server.cache._unavailable_until = 0.0
    yield port, fixture.server.cache
    fixture.stop()


def get_through(port, host, origin_port, path="/", extra=""):
    return send_via_proxy(
        port,
        (
            f"GET http://{host}:{origin_port}{path} HTTP/1.1\r\n"
            f"Host: {host}:{origin_port}\r\n{extra}\r\n"
        ).encode(),
    )


def test_second_request_is_served_from_cache(cached_proxy, caching_origin):
    port, cache = cached_proxy
    host, origin_port = caching_origin

    first = get_through(port, host, origin_port)
    second = get_through(port, host, origin_port)

    assert first.startswith(b"HTTP/1.1 200")
    assert second.startswith(b"HTTP/1.1 200")
    # The origin was contacted exactly once: the second was a hit.
    assert HITS["count"] == 1
    assert b"origin-response-1" in second
    assert cache.stats()["hits"] == 1
    assert cache.stats()["stores"] == 1


def test_no_store_response_is_never_cached(cached_proxy, caching_origin):
    port, cache = cached_proxy
    host, origin_port = caching_origin

    get_through(port, host, origin_port, "/nocache")
    get_through(port, host, origin_port, "/nocache")

    assert HITS["count"] == 2
    assert cache.stats()["stores"] == 0


def test_private_response_is_not_cached_by_a_shared_proxy(
    cached_proxy, caching_origin
):
    port, cache = cached_proxy
    host, origin_port = caching_origin

    get_through(port, host, origin_port, "/private")
    get_through(port, host, origin_port, "/private")

    assert HITS["count"] == 2
    assert cache.stats()["stores"] == 0


def test_request_no_cache_bypasses_the_stored_copy(cached_proxy, caching_origin):
    port, cache = cached_proxy
    host, origin_port = caching_origin

    get_through(port, host, origin_port)  # populates the cache
    assert HITS["count"] == 1

    fresh = get_through(
        port, host, origin_port, extra="Cache-Control: no-cache\r\n"
    )

    # The client demanded a fresh copy, so the origin was contacted again.
    assert HITS["count"] == 2
    assert b"origin-response-2" in fresh


def test_distinct_urls_do_not_collide(cached_proxy, caching_origin):
    port, _ = cached_proxy
    host, origin_port = caching_origin

    get_through(port, host, origin_port, "/one")
    get_through(port, host, origin_port, "/two")

    assert HITS["count"] == 2


def test_proxy_still_serves_when_the_cache_backend_dies(
    cached_proxy, caching_origin
):
    """The whole point of degrading gracefully."""
    port, cache = cached_proxy
    host, origin_port = caching_origin

    class _DeadRedis:
        async def get(self, key):
            raise ConnectionError("redis is gone")

        async def set(self, key, value, ex=None):
            raise ConnectionError("redis is gone")

        async def aclose(self):
            pass

    cache._client = _DeadRedis()

    response = get_through(port, host, origin_port)

    assert response.startswith(b"HTTP/1.1 200")
    assert HITS["count"] == 1
