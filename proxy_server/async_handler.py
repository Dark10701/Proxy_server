"""Asyncio request handler: forwarding, filtering and CONNECT tunnelling.

Differences from the threaded handler that are deliberate:

- Backpressure is real. Every relay awaits ``writer.drain()``, so a slow
  client throttles the read from upstream instead of the proxy buffering
  an unbounded amount on its behalf.
- The sliding-window rate limit uses a plain dict with no mutex. One
  event loop thread touches it, so a lock would guard against a
  concurrency that does not exist.
- Exceptions are handled by type and logged. Nothing is swallowed by a
  bare ``except: pass``; the broadest handler still logs with a stack
  trace and answers the client.
"""

import asyncio
import time
from typing import Dict, List, Optional, Tuple
from urllib.parse import urlsplit

from proxy_server.client_handler import (
    MAX_HEADER_BYTES,
    RATE_LIMIT_REQUESTS,
    RATE_LIMIT_WINDOW_SECONDS,
    TUNNEL_IDLE_TIMEOUT_SECONDS,
    is_whitelisted,
)
from proxy_server.cache import (
    cache_key,
    request_allows_cache,
    response_ttl,
    split_response,
)
from proxy_server.http_parser import (
    build_forward_request,
    parse_http_request,
    parse_target_from_request,
)
from proxy_server.observability import (
    OUTCOME_BLOCKED,
    OUTCOME_CACHE_HIT,
    OUTCOME_CLIENT_ERROR,
    OUTCOME_FORWARDED,
    OUTCOME_PROXY_ERROR,
    OUTCOME_RATE_LIMITED,
    OUTCOME_SHED,
    OUTCOME_TUNNELLED,
    OUTCOME_UPSTREAM_ERROR,
)
from proxy_server.scheduler import estimate_priority

CLIENT_READ_TIMEOUT_SECONDS = 10.0
UPSTREAM_CONNECT_TIMEOUT_SECONDS = 10.0
UPSTREAM_READ_TIMEOUT_SECONDS = 30.0
RELAY_CHUNK_BYTES = 65536


class ClientDisconnected(Exception):
    """The client went away; there is nobody left to answer."""


class AsyncRateLimiter:
    """Sliding-window per (client, host) limit. No lock: single loop thread."""

    def __init__(
        self,
        limit: int = RATE_LIMIT_REQUESTS,
        window_seconds: int = RATE_LIMIT_WINDOW_SECONDS,
    ) -> None:
        self._limit = limit
        self._window = window_seconds
        self._hits: Dict[Tuple[str, str], List[float]] = {}

    def is_limited(self, client_ip: str, host: str) -> bool:
        normalised = (host or "").strip().lower().strip(".")
        if not normalised or is_whitelisted(normalised):
            return False

        now = time.monotonic()
        window_start = now - self._window
        key = (client_ip, normalised)

        timestamps = [ts for ts in self._hits.get(key, []) if ts >= window_start]
        if len(timestamps) >= self._limit:
            self._hits[key] = timestamps
            return True

        timestamps.append(now)
        self._hits[key] = timestamps
        return False

    def prune(self) -> None:
        """Drop entries that have aged out, so the dict cannot grow forever."""
        cutoff = time.monotonic() - self._window
        self._hits = {
            key: [ts for ts in values if ts >= cutoff]
            for key, values in self._hits.items()
            if any(ts >= cutoff for ts in values)
        }


class AsyncClientHandler:
    """Serves one client connection on the event loop."""

    def __init__(
        self,
        reader: asyncio.StreamReader,
        writer: asyncio.StreamWriter,
        filter_engine,
        metrics_logger,
        logger,
        rate_limiter: AsyncRateLimiter,
        scheduler=None,
        rate_controller=None,
        cache=None,
        telemetry=None,
    ) -> None:
        self.cache = cache
        self.telemetry = telemetry
        self.reader = reader
        self.writer = writer
        self.filter_engine = filter_engine
        self.metrics_logger = metrics_logger
        self.logger = logger
        self.rate_limiter = rate_limiter
        self.scheduler = scheduler
        self.rate_controller = rate_controller

        peer = writer.get_extra_info("peername") or ("unknown", 0)
        self.client_ip, self.client_port = peer[0], peer[1]
        self.client_id = f"{self.client_ip}:{self.client_port}"

    # ------------------------------------------------------------------
    # Entry point
    # ------------------------------------------------------------------

    async def handle(self) -> None:
        try:
            await self._handle_inner()
        except ClientDisconnected:
            self.logger.info("Client disconnected client_id=%s", self.client_id)
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            self.logger.error(
                "Unhandled error client_id=%s: %r", self.client_id, exc, exc_info=True
            )
            await self._try_send(self._response(500, "Internal proxy error."))
            if self.telemetry is not None:
                self.telemetry.record_request("UNKNOWN", OUTCOME_PROXY_ERROR, 0.0)
        finally:
            await self._close()

    async def _handle_inner(self) -> None:
        request_data = await self._read_request()
        if request_data is None:
            return

        received_at = time.time()
        request_line, headers, body = parse_http_request(request_data)
        if not request_line:
            await self._send(self._response(400, "Malformed request received by proxy."))
            self._telemetry_request("UNKNOWN", OUTCOME_CLIENT_ERROR, received_at)
            return

        method, url, version = request_line
        method_upper = method.upper()

        if method_upper == "CONNECT":
            policy_host = url.split(":", 1)[0].strip("[]")
        else:
            policy_host, _, _ = parse_target_from_request(url, headers)

        if self.rate_limiter.is_limited(self.client_ip, policy_host):
            size = await self._send(
                self._response(
                    429,
                    "Too many requests. Please try again later.",
                    extra="Retry-After: 60\r\n",
                )
            )
            self._record(
                method, url, policy_host, received_at, len(request_data), size, blocked=1
            )
            self._telemetry_request(method, OUTCOME_RATE_LIMITED, received_at)
            return

        priority = estimate_priority(method, headers)
        if self.scheduler is not None:
            admitted = await self.scheduler.acquire(priority)
            if not admitted:
                size = await self._send(
                    self._response(
                        503,
                        "Proxy is at capacity. Please retry shortly.",
                        extra="Retry-After: 1\r\n",
                    )
                )
                self._record(
                    method, url, policy_host, received_at,
                    len(request_data), size, blocked=1,
                )
                self._telemetry_request(method, OUTCOME_SHED, received_at)
                return
        try:
            if self.rate_controller is not None and not self.rate_controller.allow_request():
                size = await self._send(
                    self._response(
                        503,
                        "Proxy is shedding load. Please retry shortly.",
                        extra="Retry-After: 1\r\n",
                    )
                )
                self._record(
                    method, url, policy_host, received_at,
                    len(request_data), size, blocked=1,
                )
                self._telemetry_request(method, OUTCOME_SHED, received_at)
                return

            if method_upper == "CONNECT":
                await self._handle_connect(request_data, method, url, received_at)
                return

            await self._handle_forward(
                request_data, method, url, version, headers, body, received_at
            )
        finally:
            if self.scheduler is not None:
                self.scheduler.release()

    # ------------------------------------------------------------------
    # Reading
    # ------------------------------------------------------------------

    async def _read_request(self) -> Optional[bytes]:
        """Read request head plus body. None means the connection is done."""
        try:
            head = await asyncio.wait_for(
                self.reader.readuntil(b"\r\n\r\n"),
                timeout=CLIENT_READ_TIMEOUT_SECONDS,
            )
        except asyncio.IncompleteReadError:
            # Client closed before sending a complete request head.
            return None
        except asyncio.LimitOverrunError:
            self.logger.error(
                "Header block exceeded %s bytes client_id=%s",
                MAX_HEADER_BYTES,
                self.client_id,
            )
            await self._send(self._response(431, "Request header block too large."))
            return None
        except asyncio.TimeoutError:
            self.logger.error("Timeout reading request client_id=%s", self.client_id)
            await self._try_send(self._response(408, "Request timed out."))
            return None
        except (ConnectionResetError, BrokenPipeError) as exc:
            raise ClientDisconnected(str(exc)) from exc

        content_length = 0
        for line in head.decode("iso-8859-1", errors="replace").split("\r\n")[1:]:
            if line.lower().startswith("content-length:"):
                try:
                    content_length = int(line.split(":", 1)[1].strip())
                except ValueError:
                    content_length = 0
                break

        body = b""
        if content_length > 0:
            try:
                body = await asyncio.wait_for(
                    self.reader.readexactly(content_length),
                    timeout=CLIENT_READ_TIMEOUT_SECONDS,
                )
            except asyncio.IncompleteReadError as exc:
                # Take what arrived; the upstream will judge the request.
                body = exc.partial
            except asyncio.TimeoutError:
                self.logger.error(
                    "Timeout reading body client_id=%s", self.client_id
                )
                await self._try_send(self._response(408, "Request body timed out."))
                return None

        return head + body

    # ------------------------------------------------------------------
    # Plain HTTP forwarding
    # ------------------------------------------------------------------

    async def _handle_forward(
        self, request_data, method, url, version, headers, body, received_at
    ) -> None:
        target_host, target_port, path = parse_target_from_request(url, headers)
        if not target_host:
            await self._send(self._response(400, "Malformed request received by proxy."))
            return

        if self.filter_engine.is_blocked(target_host, url):
            self.logger.info(
                "Blocked request_type=%s client_id=%s host=%s url=%s",
                method, self.client_id, target_host, url,
            )
            size = await self._send(
                self._response(
                    403, f"Access to {target_host} is blocked by proxy policy."
                )
            )
            self._record(
                method, url, target_host, received_at,
                len(request_data), size, blocked=1,
            )
            self._telemetry_request(method, OUTCOME_BLOCKED, received_at)
            return

        if path.lower().startswith(("http://", "https://")):
            parsed = urlsplit(path)
            path = parsed.path or "/"
            if parsed.query:
                path = f"{path}?{parsed.query}"

        # --- cache lookup -------------------------------------------------
        may_read, may_write = (False, False)
        key = None
        if self.cache is not None:
            may_read, may_write = request_allows_cache(method, headers)
            if may_read or may_write:
                key = cache_key(method, url)

        if key is not None and may_read:
            cached = await self.cache.get(key)
            if cached is not None:
                start = time.time()
                try:
                    self.writer.write(cached)
                    await self.writer.drain()
                except (ConnectionResetError, BrokenPipeError) as exc:
                    raise ClientDisconnected(str(exc)) from exc
                self.logger.info(
                    "Cache hit request_type=%s client_id=%s url=%s",
                    method, self.client_id, url,
                )
                self.metrics_logger.log(
                    client_ip=self.client_ip,
                    method=method,
                    url=url,
                    host=target_host,
                    latency_ms=int((time.time() - start) * 1000),
                    request_bytes=len(request_data),
                    response_bytes=len(cached),
                    blocked=0,
                    cache="hit",
                )
                if self.telemetry is not None:
                    self.telemetry.record_cache("hit")
                    self.telemetry.record_bytes(to_client=len(cached))
                self._telemetry_request(method, OUTCOME_CACHE_HIT, received_at)
                return

        forward_bytes = build_forward_request(
            method=method, path=path, version=version, headers=headers, body=body
        )

        start = time.time()
        try:
            upstream_reader, upstream_writer = await asyncio.wait_for(
                asyncio.open_connection(target_host, target_port),
                timeout=UPSTREAM_CONNECT_TIMEOUT_SECONDS,
            )
        except asyncio.TimeoutError:
            self.logger.error(
                "Upstream connect timeout host=%s:%s client_id=%s",
                target_host, target_port, self.client_id,
            )
            await self._try_send(self._response(504, "Upstream timed out."))
            if self.telemetry is not None:
                self.telemetry.record_upstream_error("connect_timeout")
            self._telemetry_request(method, OUTCOME_UPSTREAM_ERROR, received_at)
            return
        except OSError as exc:
            self.logger.error(
                "Upstream connect failed host=%s:%s: %s", target_host, target_port, exc
            )
            await self._try_send(
                self._response(502, "Proxy could not reach the upstream server.")
            )
            if self.telemetry is not None:
                self.telemetry.record_upstream_error("connect_failed")
            self._telemetry_request(method, OUTCOME_UPSTREAM_ERROR, received_at)
            return

        response_bytes = 0
        # Buffer only while the response is still small enough to store;
        # past the limit we drop the buffer and keep streaming.
        capture = bytearray() if (key is not None and may_write) else None
        complete = False
        try:
            upstream_writer.write(forward_bytes)
            await upstream_writer.drain()
            self.logger.info(
                "Forwarded request_type=%s client_id=%s host=%s:%s",
                method, self.client_id, target_host, target_port,
            )

            while True:
                try:
                    chunk = await asyncio.wait_for(
                        upstream_reader.read(RELAY_CHUNK_BYTES),
                        timeout=UPSTREAM_READ_TIMEOUT_SECONDS,
                    )
                except asyncio.TimeoutError:
                    self.logger.error(
                        "Upstream read timeout host=%s client_id=%s",
                        target_host, self.client_id,
                    )
                    break
                if not chunk:
                    complete = True
                    break
                response_bytes += len(chunk)
                if capture is not None:
                    capture.extend(chunk)
                    if len(capture) > self.cache.max_entry_bytes:
                        capture = None
                self.writer.write(chunk)
                # Backpressure: a slow client slows the upstream read
                # rather than growing an unbounded buffer here.
                await self.writer.drain()
        except (ConnectionResetError, BrokenPipeError) as exc:
            self.logger.info(
                "Client disconnected mid-transfer client_id=%s host=%s: %s",
                self.client_id, target_host, exc,
            )
        except OSError as exc:
            self.logger.error("Relay error host=%s: %s", target_host, exc)
        finally:
            await self._close_writer(upstream_writer)

        latency_ms = int((time.time() - start) * 1000)
        if self.rate_controller is not None:
            self.rate_controller.record_latency(latency_ms)

        # Only a response we received in full is safe to replay later.
        cache_status = "miss" if key is not None and may_read else "bypass"
        if capture is not None and complete and key is not None and may_write:
            parsed_response = split_response(bytes(capture))
            if parsed_response is not None:
                status, response_headers = parsed_response
                ttl = response_ttl(status, response_headers)
                if ttl and await self.cache.set(key, bytes(capture), ttl):
                    cache_status = "store"

        self.metrics_logger.log(
            client_ip=self.client_ip,
            method=method,
            url=url,
            host=target_host,
            latency_ms=latency_ms,
            request_bytes=len(forward_bytes),
            response_bytes=response_bytes,
            blocked=0,
            cache=cache_status,
        )
        if self.telemetry is not None:
            self.telemetry.record_cache(cache_status)
            self.telemetry.record_bytes(
                to_client=response_bytes, to_upstream=len(forward_bytes)
            )
        self._telemetry_request(method, OUTCOME_FORWARDED, received_at)

    # ------------------------------------------------------------------
    # CONNECT tunnelling
    # ------------------------------------------------------------------

    async def _handle_connect(self, request_data, method, url, received_at) -> None:
        target_host, target_port = _parse_connect_target(url)
        if not target_host or target_port <= 0:
            await self._send(self._response(400, "Malformed request received by proxy."))
            return

        if self.filter_engine.is_blocked(target_host, url):
            self.logger.info(
                "Blocked request_type=CONNECT client_id=%s host=%s", self.client_id, target_host
            )
            size = await self._send(
                self._response(
                    403, f"Access to {target_host} is blocked by proxy policy."
                )
            )
            self._record(
                method, url, target_host, received_at,
                len(request_data), size, blocked=1,
            )
            self._telemetry_request(method, OUTCOME_BLOCKED, received_at)
            return

        start = time.time()
        try:
            upstream_reader, upstream_writer = await asyncio.wait_for(
                asyncio.open_connection(target_host, target_port),
                timeout=UPSTREAM_CONNECT_TIMEOUT_SECONDS,
            )
        except asyncio.TimeoutError:
            self.logger.error(
                "CONNECT timeout host=%s:%s client_id=%s",
                target_host, target_port, self.client_id,
            )
            await self._try_send(self._response(504, "Upstream timed out."))
            if self.telemetry is not None:
                self.telemetry.record_upstream_error("connect_timeout")
            self._telemetry_request(method, OUTCOME_UPSTREAM_ERROR, received_at)
            return
        except OSError as exc:
            self.logger.error(
                "CONNECT failed host=%s:%s: %s", target_host, target_port, exc
            )
            await self._try_send(
                self._response(502, "Proxy could not reach the upstream server.")
            )
            if self.telemetry is not None:
                self.telemetry.record_upstream_error("connect_failed")
            self._telemetry_request(method, OUTCOME_UPSTREAM_ERROR, received_at)
            return

        relayed = 0
        try:
            self.writer.write(b"HTTP/1.1 200 Connection Established\r\n\r\n")
            await self.writer.drain()
            self.logger.info(
                "Established request_type=CONNECT client_id=%s host=%s:%s",
                self.client_id, target_host, target_port,
            )
            relayed = await self._relay_bidirectional(upstream_reader, upstream_writer)
        except (ConnectionResetError, BrokenPipeError) as exc:
            self.logger.info(
                "Tunnel closed by peer client_id=%s: %s", self.client_id, exc
            )
        finally:
            await self._close_writer(upstream_writer)

        latency_ms = int((time.time() - start) * 1000)
        self.metrics_logger.log(
            client_ip=self.client_ip,
            method=method,
            url=url,
            host=target_host,
            latency_ms=latency_ms,
            request_bytes=len(request_data),
            response_bytes=relayed,
            blocked=0,
        )
        if self.telemetry is not None:
            self.telemetry.record_bytes(to_client=relayed)
        self._telemetry_request(method, OUTCOME_TUNNELLED, received_at)

    async def _relay_bidirectional(self, upstream_reader, upstream_writer) -> int:
        """Pump both directions until either side closes or goes idle."""
        counter = {"from_upstream": 0}

        async def pump(reader, writer, count: bool) -> None:
            while True:
                try:
                    chunk = await asyncio.wait_for(
                        reader.read(RELAY_CHUNK_BYTES),
                        timeout=TUNNEL_IDLE_TIMEOUT_SECONDS,
                    )
                except asyncio.TimeoutError:
                    self.logger.info(
                        "Tunnel idle timeout client_id=%s after %ss",
                        self.client_id, TUNNEL_IDLE_TIMEOUT_SECONDS,
                    )
                    return
                if not chunk:
                    return
                if count:
                    counter["from_upstream"] += len(chunk)
                writer.write(chunk)
                await writer.drain()

        client_to_upstream = asyncio.create_task(
            pump(self.reader, upstream_writer, count=False)
        )
        upstream_to_client = asyncio.create_task(
            pump(upstream_reader, self.writer, count=True)
        )

        done, pending = await asyncio.wait(
            {client_to_upstream, upstream_to_client},
            return_when=asyncio.FIRST_COMPLETED,
        )

        # One direction ended, so the tunnel is finished. Stop the other
        # rather than leaving a task pumping into a closed socket.
        for task in pending:
            task.cancel()
        if pending:
            await asyncio.wait(pending)

        for task in done:
            exc = task.exception()
            if exc and not isinstance(
                exc, (ConnectionResetError, BrokenPipeError, asyncio.CancelledError)
            ):
                self.logger.error(
                    "Tunnel relay error client_id=%s: %r", self.client_id, exc
                )

        return counter["from_upstream"]

    # ------------------------------------------------------------------
    # Responses and teardown
    # ------------------------------------------------------------------

    @staticmethod
    def _response(status: int, body: str, extra: str = "") -> bytes:
        reasons = {
            400: "Bad Request",
            403: "Forbidden",
            408: "Request Timeout",
            429: "Too Many Requests",
            431: "Request Header Fields Too Large",
            500: "Internal Server Error",
            502: "Bad Gateway",
            503: "Service Unavailable",
            504: "Gateway Timeout",
        }
        payload = body.encode("utf-8")
        head = (
            f"HTTP/1.1 {status} {reasons.get(status, 'Error')}\r\n"
            "Content-Type: text/plain\r\n"
            f"Content-Length: {len(payload)}\r\n"
            "Connection: close\r\n"
            f"{extra}"
            "\r\n"
        ).encode("utf-8")
        return head + payload

    async def _send(self, payload: bytes) -> int:
        try:
            self.writer.write(payload)
            await self.writer.drain()
            return len(payload)
        except (ConnectionResetError, BrokenPipeError) as exc:
            raise ClientDisconnected(str(exc)) from exc

    async def _try_send(self, payload: bytes) -> int:
        """Send, tolerating a client that has already gone."""
        try:
            return await self._send(payload)
        except ClientDisconnected:
            return 0

    def _telemetry_request(self, method, outcome, started_at) -> None:
        if self.telemetry is not None:
            self.telemetry.record_request(
                method, outcome, max(time.time() - started_at, 0.0)
            )

    def _record(
        self, method, url, host, received_at, request_bytes, response_bytes, blocked
    ) -> None:
        self.metrics_logger.log(
            client_ip=self.client_ip,
            method=method,
            url=url,
            host=host,
            latency_ms=int((time.time() - received_at) * 1000),
            request_bytes=request_bytes,
            response_bytes=response_bytes,
            blocked=blocked,
        )

    async def _close(self) -> None:
        await self._close_writer(self.writer)

    @staticmethod
    async def _close_writer(writer: asyncio.StreamWriter) -> None:
        if writer.is_closing():
            return
        try:
            writer.close()
            await writer.wait_closed()
        except (ConnectionResetError, BrokenPipeError, OSError):
            # The peer is gone; the socket is closed either way.
            pass


def _parse_connect_target(authority: str) -> Tuple[str, int]:
    """Parse CONNECT authority-form (host:port), including IPv6 literals."""
    value = (authority or "").strip()
    if not value:
        return "", 0

    if value.startswith("["):
        end = value.find("]")
        if end == -1:
            return "", 0
        host = value[1:end]
        remainder = value[end + 1:]
        if not remainder.startswith(":"):
            return "", 0
        port_str = remainder[1:]
    else:
        if ":" not in value:
            return "", 0
        host, port_str = value.rsplit(":", 1)

    try:
        port = int(port_str)
    except ValueError:
        return "", 0
    return host.strip(), port
