"""HTTP response cache backed by Redis.

What is implemented
-------------------
- Keyed on method + full request URL.
- Only GET and HEAD are cacheable; CONNECT never is (a tunnel is opaque
  bytes, and caching it would be meaningless and unsafe).
- Request directives honoured: ``no-store`` (do not read or write),
  ``no-cache`` (skip the lookup, still store the fresh response).
- Response directives honoured: ``no-store``, ``no-cache``, ``private``,
  ``max-age``, ``s-maxage`` (which wins for a shared cache), and
  ``Expires`` when no max-age is present.
- Only status codes whose responses are safe to replay are stored.
- Responses without any freshness information are not cached.
- Entries are stored as the raw upstream byte stream and replayed
  verbatim, so chunked framing survives a round trip untouched.

Deliberately out of scope
-------------------------
- Revalidation. There is no ``If-None-Match``/``If-Modified-Since``
  handling, so ``no-cache`` and ``must-revalidate`` are treated as "do
  not serve from cache" rather than "revalidate then serve". A stale
  entry is never served.
- Heuristic freshness. RFC 9111 permits guessing a lifetime from
  ``Last-Modified``; this does not. No explicit lifetime means no cache.
- ``Vary``. Responses that vary on request headers are not cached at
  all, rather than cached against the wrong key.
- Range requests, cache invalidation on unsafe methods, and stale-while-
  revalidate.

Redis is optional. Every operation degrades to a miss when the server
is unreachable, and the proxy keeps serving traffic.
"""

import email.utils
import time
from typing import Dict, Optional, Tuple

CACHEABLE_METHODS = frozenset({"GET", "HEAD"})

# Status codes safe to store and replay without revalidation.
CACHEABLE_STATUS = frozenset({200, 203, 204, 301, 308, 404, 410})

MAX_CACHEABLE_BYTES = 1024 * 1024  # 1 MiB
DEFAULT_NAMESPACE = "proxycache:v1"
UNAVAILABLE_RETRY_SECONDS = 5.0

# Deliberately not database 0. A developer machine very often already has
# a Redis on 6379 belonging to some other project, and db 0 is what that
# project will be using. Defaulting there means simply starting this
# proxy writes cache entries into someone else's keyspace, consumes
# their memory, and — under the usual allkeys-lru policy — can evict
# their data. A dedicated index makes the default collision-free while
# still working out of the box against a local Redis.
DEFAULT_REDIS_DB = 11
DEFAULT_REDIS_URL = f"redis://127.0.0.1:6379/{DEFAULT_REDIS_DB}"


def parse_cache_control(value: str) -> Dict[str, Optional[str]]:
    """Parse a Cache-Control header into a directive dict."""
    directives: Dict[str, Optional[str]] = {}
    if not value:
        return directives
    for part in value.split(","):
        part = part.strip()
        if not part:
            continue
        if "=" in part:
            name, _, raw = part.partition("=")
            directives[name.strip().lower()] = raw.strip().strip('"')
        else:
            directives[part.lower()] = None
    return directives


def header_lookup(headers: Dict[str, str], name: str) -> Optional[str]:
    """Case-insensitive header fetch."""
    target = name.lower()
    for key, value in (headers or {}).items():
        if key.lower() == target:
            return value
    return None


def cache_key(method: str, url: str, namespace: str = DEFAULT_NAMESPACE) -> str:
    return f"{namespace}:{method.upper()}:{url}"


def request_allows_cache(method: str, headers: Dict[str, str]) -> Tuple[bool, bool]:
    """Return (may_read, may_write) for this request.

    ``no-store`` forbids both. ``no-cache`` forbids reading a stored
    response but still permits storing the fresh one.
    """
    if method.upper() not in CACHEABLE_METHODS:
        return False, False

    directives = parse_cache_control(header_lookup(headers, "Cache-Control") or "")
    if "no-store" in directives:
        return False, False
    if "no-cache" in directives:
        return False, True
    return True, True


def response_ttl(
    status: int, headers: Dict[str, str], now: Optional[float] = None
) -> Optional[int]:
    """Seconds this response may be stored for, or None if not cacheable."""
    if status not in CACHEABLE_STATUS:
        return None

    # Varying responses are not cached at all rather than cached wrongly.
    if header_lookup(headers, "Vary"):
        return None

    directives = parse_cache_control(header_lookup(headers, "Cache-Control") or "")
    if any(key in directives for key in ("no-store", "no-cache", "private")):
        return None
    # No revalidation support, so a response demanding it is not stored.
    if "must-revalidate" in directives or "proxy-revalidate" in directives:
        return None

    # s-maxage is aimed at shared caches and outranks max-age here.
    for directive in ("s-maxage", "max-age"):
        if directive in directives and directives[directive] is not None:
            try:
                seconds = int(directives[directive])
            except (TypeError, ValueError):
                continue
            return seconds if seconds > 0 else None

    expires = header_lookup(headers, "Expires")
    if expires:
        parsed = email.utils.parsedate_to_datetime(expires)
        if parsed is None:
            return None
        reference = now if now is not None else time.time()
        # Date, if present, is the origin's own clock; prefer it so a
        # skewed proxy clock does not distort the lifetime.
        date_header = header_lookup(headers, "Date")
        if date_header:
            parsed_date = email.utils.parsedate_to_datetime(date_header)
            if parsed_date is not None:
                reference = parsed_date.timestamp()
        seconds = int(parsed.timestamp() - reference)
        return seconds if seconds > 0 else None

    # No explicit freshness information: no heuristic caching.
    return None


def split_response(raw: bytes) -> Optional[Tuple[int, Dict[str, str]]]:
    """Pull status code and headers out of a raw HTTP response."""
    head, separator, _ = raw.partition(b"\r\n\r\n")
    if not separator:
        return None
    lines = head.decode("iso-8859-1", errors="replace").split("\r\n")
    if not lines:
        return None
    parts = lines[0].split(" ", 2)
    if len(parts) < 2:
        return None
    try:
        status = int(parts[1])
    except ValueError:
        return None

    headers: Dict[str, str] = {}
    for line in lines[1:]:
        if ":" in line:
            key, _, value = line.partition(":")
            headers[key.strip()] = value.strip()
    return status, headers


class HTTPCache:
    """Redis-backed response cache that fails open."""

    def __init__(
        self,
        url: str = DEFAULT_REDIS_URL,
        namespace: str = DEFAULT_NAMESPACE,
        max_entry_bytes: int = MAX_CACHEABLE_BYTES,
        enabled: bool = True,
        logger=None,
    ) -> None:
        self.url = url
        self.namespace = namespace
        self.max_entry_bytes = max_entry_bytes
        self.enabled = enabled
        self.logger = logger

        self._client = None
        self._unavailable_until = 0.0
        self.hits = 0
        self.misses = 0
        self.stores = 0
        self.errors = 0

    async def connect(self) -> bool:
        """Attempt to connect. False means run without a cache."""
        if not self.enabled:
            return False
        try:
            import redis.asyncio as aioredis
        except ImportError:
            self._log("redis package not installed; running without cache")
            self.enabled = False
            return False

        try:
            self._client = aioredis.from_url(
                self.url, socket_connect_timeout=2, socket_timeout=2
            )
            await self._client.ping()
            self._log(f"cache connected to {self.url}")
            return True
        except Exception as exc:
            self._log(f"cache unavailable at {self.url}: {exc}")
            self._mark_unavailable()
            return False

    def _log(self, message: str) -> None:
        if self.logger is not None:
            self.logger.info("HTTP cache: %s", message)

    def _mark_unavailable(self) -> None:
        """Back off briefly so every request does not retry a dead server."""
        self._unavailable_until = time.monotonic() + UNAVAILABLE_RETRY_SECONDS

    @property
    def available(self) -> bool:
        return (
            self.enabled
            and self._client is not None
            and time.monotonic() >= self._unavailable_until
        )

    async def get(self, key: str) -> Optional[bytes]:
        """Fetch a stored response. Any failure counts as a miss."""
        if not self.available:
            self.misses += 1
            return None
        try:
            payload = await self._client.get(key)
        except Exception as exc:
            self.errors += 1
            self._log(f"read failed, serving without cache: {exc}")
            self._mark_unavailable()
            self.misses += 1
            return None

        if payload is None:
            self.misses += 1
            return None
        self.hits += 1
        return payload

    async def set(self, key: str, payload: bytes, ttl: int) -> bool:
        if not self.available or ttl <= 0:
            return False
        if len(payload) > self.max_entry_bytes:
            return False
        try:
            await self._client.set(key, payload, ex=ttl)
        except Exception as exc:
            self.errors += 1
            self._log(f"write failed: {exc}")
            self._mark_unavailable()
            return False
        self.stores += 1
        return True

    async def close(self) -> None:
        if self._client is not None:
            try:
                await self._client.aclose()
            except AttributeError:
                await self._client.close()
            except Exception:
                pass
            self._client = None

    def stats(self) -> Dict[str, object]:
        total = self.hits + self.misses
        return {
            "enabled": self.enabled,
            "available": self.available,
            "hits": self.hits,
            "misses": self.misses,
            "stores": self.stores,
            "errors": self.errors,
            "hit_ratio_pct": round(self.hits / total * 100, 1) if total else 0.0,
        }
