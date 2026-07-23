"""Cache policy and Redis-degradation tests."""

import asyncio
import email.utils
import time

import pytest

from proxy_server.cache import (
    HTTPCache,
    cache_key,
    parse_cache_control,
    request_allows_cache,
    response_ttl,
    split_response,
)


def run(coro):
    loop = asyncio.new_event_loop()
    try:
        return loop.run_until_complete(coro)
    finally:
        loop.close()


# --- directive parsing -------------------------------------------------


def test_parse_cache_control_handles_values_and_flags():
    parsed = parse_cache_control("public, max-age=600, no-transform")

    assert parsed["max-age"] == "600"
    assert "public" in parsed
    assert "no-transform" in parsed


def test_parse_cache_control_is_case_insensitive_and_strips_quotes():
    assert parse_cache_control('Max-Age="30"')["max-age"] == "30"


# --- request side ------------------------------------------------------


@pytest.mark.parametrize("method", ["POST", "PUT", "DELETE", "CONNECT", "PATCH"])
def test_only_get_and_head_are_cacheable(method):
    assert request_allows_cache(method, {}) == (False, False)


def test_get_is_cacheable_by_default():
    assert request_allows_cache("GET", {}) == (True, True)


def test_request_no_store_forbids_both_directions():
    assert request_allows_cache("GET", {"Cache-Control": "no-store"}) == (False, False)


def test_request_no_cache_skips_lookup_but_still_stores():
    """no-cache means 'do not give me a stored copy', not 'never store'."""
    assert request_allows_cache("GET", {"Cache-Control": "no-cache"}) == (False, True)


# --- response side -----------------------------------------------------


def test_max_age_becomes_the_ttl():
    assert response_ttl(200, {"Cache-Control": "max-age=120"}) == 120


def test_s_maxage_wins_for_a_shared_cache():
    ttl = response_ttl(200, {"Cache-Control": "max-age=60, s-maxage=600"})
    assert ttl == 600


@pytest.mark.parametrize(
    "directive", ["no-store", "no-cache", "private", "must-revalidate"]
)
def test_directives_that_prevent_storage(directive):
    assert response_ttl(200, {"Cache-Control": f"max-age=600, {directive}"}) is None


def test_zero_and_negative_max_age_are_not_cacheable():
    assert response_ttl(200, {"Cache-Control": "max-age=0"}) is None


def test_expires_is_used_when_no_max_age():
    now = time.time()
    headers = {
        "Date": email.utils.formatdate(now, usegmt=True),
        "Expires": email.utils.formatdate(now + 300, usegmt=True),
    }
    ttl = response_ttl(200, headers, now=now)

    assert ttl is not None
    assert 290 <= ttl <= 300


def test_past_expires_is_not_cacheable():
    now = time.time()
    headers = {
        "Date": email.utils.formatdate(now, usegmt=True),
        "Expires": email.utils.formatdate(now - 300, usegmt=True),
    }
    assert response_ttl(200, headers, now=now) is None


def test_no_freshness_information_means_no_caching():
    """Heuristic freshness is deliberately not implemented."""
    assert response_ttl(200, {"Content-Type": "text/html"}) is None


@pytest.mark.parametrize("status", [200, 203, 204, 301, 308, 404, 410])
def test_cacheable_status_codes(status):
    assert response_ttl(status, {"Cache-Control": "max-age=60"}) == 60


@pytest.mark.parametrize("status", [201, 302, 401, 403, 500, 502, 503])
def test_uncacheable_status_codes(status):
    assert response_ttl(status, {"Cache-Control": "max-age=60"}) is None


def test_vary_responses_are_not_cached():
    headers = {"Cache-Control": "max-age=600", "Vary": "Accept-Encoding"}
    assert response_ttl(200, headers) is None


# --- response parsing --------------------------------------------------


def test_split_response_extracts_status_and_headers():
    raw = b"HTTP/1.1 200 OK\r\nContent-Length: 2\r\nCache-Control: max-age=5\r\n\r\nhi"
    status, headers = split_response(raw)

    assert status == 200
    assert headers["Cache-Control"] == "max-age=5"


def test_split_response_rejects_a_truncated_head():
    assert split_response(b"HTTP/1.1 200 OK\r\nContent-Len") is None


def test_cache_key_includes_method_and_full_url():
    key = cache_key("GET", "http://example.com/a?b=1")

    assert "GET" in key
    assert "http://example.com/a?b=1" in key
    assert cache_key("HEAD", "http://example.com/a?b=1") != key


# --- degradation -------------------------------------------------------


def test_unreachable_redis_degrades_to_no_cache():
    """The proxy must keep serving when Redis is down."""
    cache = HTTPCache(url="redis://127.0.0.1:6399/0")

    connected = run(cache.connect())

    assert connected is False
    assert cache.available is False
    assert run(cache.get("anything")) is None
    assert run(cache.set("anything", b"payload", 60)) is False


def test_disabled_cache_never_connects():
    cache = HTTPCache(enabled=False)

    assert run(cache.connect()) is False
    assert run(cache.get("k")) is None


class _FlakyClient:
    """Stands in for redis.asyncio, failing on demand."""

    def __init__(self):
        self.store = {}
        self.fail = False

    async def get(self, key):
        if self.fail:
            raise ConnectionError("redis went away")
        return self.store.get(key)

    async def set(self, key, value, ex=None):
        if self.fail:
            raise ConnectionError("redis went away")
        self.store[key] = value

    async def aclose(self):
        pass


def test_round_trip_hit_and_miss_counting():
    async def scenario():
        cache = HTTPCache()
        cache._client = _FlakyClient()
        payload = b"HTTP/1.1 200 OK\r\n\r\nbody"

        assert await cache.get("k") is None  # miss
        await cache.set("k", payload, 60)
        assert await cache.get("k") == payload  # hit
        return cache.stats()

    stats = scenario_stats = run(scenario())
    assert scenario_stats["hits"] == 1
    assert scenario_stats["misses"] == 1
    assert scenario_stats["stores"] == 1
    assert stats["hit_ratio_pct"] == 50.0


def test_redis_failing_mid_flight_is_a_miss_not_an_error():
    async def scenario():
        cache = HTTPCache()
        client = _FlakyClient()
        cache._client = client
        await cache.set("k", b"payload", 60)

        client.fail = True
        result = await cache.get("k")
        return result, cache.stats()

    result, stats = run(scenario())
    assert result is None
    assert stats["errors"] == 1
    # And it backs off rather than retrying a dead server every request.
    assert stats["available"] is False


def test_oversized_entries_are_not_stored():
    async def scenario():
        cache = HTTPCache(max_entry_bytes=10)
        cache._client = _FlakyClient()
        return await cache.set("k", b"x" * 100, 60)

    assert run(scenario()) is False


# --- default keyspace safety ---------------------------------------


def test_default_redis_url_avoids_database_zero():
    """A shared local Redis must not be polluted just by starting up.

    Developer machines commonly already run a Redis on 6379 for some
    other project, and that project will be using db 0. Defaulting there
    means this proxy writes into their keyspace, consumes their memory,
    and under allkeys-lru can evict their data.
    """
    from proxy_server.cache import DEFAULT_REDIS_DB, DEFAULT_REDIS_URL

    assert DEFAULT_REDIS_DB != 0
    assert DEFAULT_REDIS_URL.endswith(f"/{DEFAULT_REDIS_DB}")
    assert HTTPCache().url == DEFAULT_REDIS_URL


def test_cli_default_matches_the_safe_url():
    from proxy_server.cache import DEFAULT_REDIS_URL
    from proxy_server.main import parse_args

    assert parse_args([]).redis_url == DEFAULT_REDIS_URL
