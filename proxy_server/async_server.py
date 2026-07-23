"""Asyncio accept loop, replacing the thread-per-connection listener."""

import asyncio
from typing import Optional

from proxy_server.async_handler import (
    AsyncClientHandler,
    AsyncRateLimiter,
)
from proxy_server.async_metrics import AsyncMetricsLogger
from proxy_server.async_scheduler import AsyncScheduler
from proxy_server.client_handler import MAX_HEADER_BYTES
from proxy_server.filter_engine import FilterEngine
from proxy_server.logger import ProxyLogger
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
    ) -> None:
        self.host = host
        self.port = port
        self.filter_engine = FilterEngine(blocked_domains_path)
        self.logger = ProxyLogger(access_log_path, error_log_path)
        self.metrics_path = metrics_path

        self.metrics_logger: Optional[AsyncMetricsLogger] = None
        self.rate_limiter = AsyncRateLimiter()
        self.scheduler = AsyncScheduler(
            max_concurrent=max_concurrent, max_queued=max_queued
        )
        self.rate_controller = RateController() if adaptive_rate_limit else None

        self._server: Optional[asyncio.AbstractServer] = None
        self._prune_task: Optional[asyncio.Task] = None
        self.active_connections = 0
        self.peak_connections = 0

    async def _on_client(
        self, reader: asyncio.StreamReader, writer: asyncio.StreamWriter
    ) -> None:
        self.active_connections += 1
        self.peak_connections = max(self.peak_connections, self.active_connections)
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
            )
            await handler.handle()
        finally:
            self.active_connections -= 1

    async def _prune_loop(self) -> None:
        """Keep the rate-limit table from growing without bound."""
        while True:
            await asyncio.sleep(PRUNE_INTERVAL_SECONDS)
            self.rate_limiter.prune()

    async def start(self) -> None:
        """Bind the listener and begin serving."""
        self.metrics_logger = AsyncMetricsLogger(self.metrics_path)
        self.metrics_logger.start()

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
        self.logger.info(
            "Async proxy server listening on %s:%s", self.host, self.port
        )

    async def serve_forever(self) -> None:
        if self._server is None:
            await self.start()
        async with self._server:
            await self._server.serve_forever()

    async def stop(self) -> None:
        if self._prune_task is not None:
            self._prune_task.cancel()
            try:
                await self._prune_task
            except asyncio.CancelledError:
                pass
            self._prune_task = None

        if self._server is not None:
            self._server.close()
            await self._server.wait_closed()
            self._server = None

        if self.metrics_logger is not None:
            await self.metrics_logger.close()
            self.metrics_logger = None

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
