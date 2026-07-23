"""Asyncio accept loop, replacing the thread-per-connection listener."""

import asyncio
from typing import Optional

from proxy_server.async_handler import (
    AsyncClientHandler,
    AsyncRateLimiter,
)
from proxy_server.async_metrics import AsyncMetricsLogger
from proxy_server.async_scheduler import AsyncScheduler
from proxy_server.cache import HTTPCache
from proxy_server.client_handler import MAX_HEADER_BYTES
from proxy_server.filter_engine import FilterEngine
from proxy_server.health import HealthServer
from proxy_server.logger import ProxyLogger
from proxy_server.observability import Telemetry
from proxy_server.rate_controller import RateController

PRUNE_INTERVAL_SECONDS = 60.0


class AsyncProxyServer:
    """Event-loop proxy: one task per connection, no threads per client."""

    def __init__(
        self,
        host: str,
        port: int,
        blocked_domains_path: str,
        metrics_path: str,
        access_log_path: str,
        error_log_path: str,
        max_concurrent: int = 256,
        max_queued: int = 512,
        adaptive_rate_limit: bool = True,
        cache_enabled: bool = True,
        redis_url: str = "redis://127.0.0.1:6379/0",
        metrics_port: Optional[int] = 9100,
        health_port: Optional[int] = 8081,
        rate_limit_requests: int = 200,
    ) -> None:
        self.host = host
        self.port = port
        self.filter_engine = FilterEngine(blocked_domains_path)
        self.logger = ProxyLogger(access_log_path, error_log_path)
        self.metrics_path = metrics_path

        self.metrics_logger: Optional[AsyncMetricsLogger] = None
        self.rate_limiter = AsyncRateLimiter(limit=rate_limit_requests)
        self.scheduler = AsyncScheduler(
            max_concurrent=max_concurrent, max_queued=max_queued
        )
        self.rate_controller = RateController() if adaptive_rate_limit else None
        self.cache = HTTPCache(
            url=redis_url, enabled=cache_enabled, logger=self.logger
        )

        self.telemetry = Telemetry()
        self.metrics_port = metrics_port
        self.health_port = health_port
        self.health: Optional[HealthServer] = None

        self._server: Optional[asyncio.AbstractServer] = None
        self._gauge_task: Optional[asyncio.Task] = None
        self._prune_task: Optional[asyncio.Task] = None
        self.active_connections = 0
        self.peak_connections = 0

    async def _on_client(
        self, reader: asyncio.StreamReader, writer: asyncio.StreamWriter
    ) -> None:
        self.active_connections += 1
        self.peak_connections = max(self.peak_connections, self.active_connections)
        self.telemetry.connection_opened()
        try:
            handler = AsyncClientHandler(
                reader=reader,
                writer=writer,
                filter_engine=self.filter_engine,
                metrics_logger=self.metrics_logger,
                logger=self.logger,
                rate_limiter=self.rate_limiter,
                scheduler=self.scheduler,
                rate_controller=self.rate_controller,
                cache=self.cache,
                telemetry=self.telemetry,
            )
            await handler.handle()
        finally:
            self.active_connections -= 1
            self.telemetry.connection_closed()

    async def _gauge_loop(self) -> None:
        """Sample state-shaped values that no single event updates."""
        while True:
            await asyncio.sleep(5.0)
            self.telemetry.observe_gauges(
                queued=self.scheduler.stats()["queued"],
                rate_limit=(
                    self.rate_controller.get_current_rate()
                    if self.rate_controller is not None
                    else 0.0
                ),
            )

    async def _prune_loop(self) -> None:
        """Keep the rate-limit table from growing without bound."""
        while True:
            await asyncio.sleep(PRUNE_INTERVAL_SECONDS)
            self.rate_limiter.prune()

    async def start(self) -> None:
        """Bind the listener and begin serving."""
        self.metrics_logger = AsyncMetricsLogger(self.metrics_path)
        self.metrics_logger.start()

        # A missing or unreachable Redis is not fatal: the proxy runs
        # without a cache rather than refusing to start.
        await self.cache.connect()

        self._server = await asyncio.start_server(
            self._on_client,
            host=self.host,
            port=self.port,
            # Bound the per-connection read buffer so readuntil() raises
            # LimitOverrunError instead of buffering a huge header block.
            limit=MAX_HEADER_BYTES,
            backlog=512,
            reuse_address=True,
        )
        self._prune_task = asyncio.create_task(self._prune_loop())
        self._gauge_task = asyncio.create_task(self._gauge_loop())

        if self.health_port:
            self.health = HealthServer(
                port=self.health_port, status_provider=self.stats
            )
            await self.health.start()
            self.logger.info(
                "Health endpoint on http://0.0.0.0:%s/health", self.health_port
            )

        if self.metrics_port:
            try:
                self.telemetry.start_server(self.metrics_port)
                self.logger.info(
                    "Prometheus metrics on http://0.0.0.0:%s/metrics",
                    self.metrics_port,
                )
            except OSError as exc:
                # Losing the exporter must not stop the proxy serving.
                self.logger.error(
                    "Could not bind metrics port %s: %s", self.metrics_port, exc
                )

        self.logger.info(
            "Async proxy server listening on %s:%s", self.host, self.port
        )

    async def serve_forever(self) -> None:
        if self._server is None:
            await self.start()
        async with self._server:
            await self._server.serve_forever()

    async def stop(self) -> None:
        # Report not-ready before closing the listener, so a load
        # balancer drains this instance instead of racing it.
        if self.health is not None:
            self.health.begin_draining()
            await asyncio.sleep(0)

        for attr in ("_prune_task", "_gauge_task"):
            task = getattr(self, attr, None)
            if task is not None:
                task.cancel()
                try:
                    await task
                except asyncio.CancelledError:
                    pass
                setattr(self, attr, None)

        if self._server is not None:
            self._server.close()
            await self._server.wait_closed()
            self._server = None

        if self.metrics_logger is not None:
            await self.metrics_logger.close()
            self.metrics_logger = None

        await self.cache.close()

        if self.health is not None:
            await self.health.stop()
            self.health = None

    @property
    def port_in_use(self) -> int:
        """Actual bound port, for tests that ask the OS to pick one."""
        if self._server is None or not self._server.sockets:
            return self.port
        return self._server.sockets[0].getsockname()[1]

    def stats(self) -> dict:
        data = {
            "active_connections": self.active_connections,
            "peak_connections": self.peak_connections,
            "scheduler": self.scheduler.stats(),
            "cache": self.cache.stats(),
        }
        if self.rate_controller is not None:
            data["rate_controller"] = self.rate_controller.stats()
        if self.metrics_logger is not None:
            data["metrics_dropped_rows"] = self.metrics_logger.dropped_rows
        return data


async def run(server: AsyncProxyServer) -> None:
    """Serve until cancelled, shutting down cleanly."""
    try:
        await server.serve_forever()
    except asyncio.CancelledError:
        raise
    finally:
        await server.stop()
