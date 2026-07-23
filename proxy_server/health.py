"""Minimal HTTP health endpoint.

Runs on its own port so orchestrators and load balancers have something
to probe that is not the proxy port. Deliberately hand-rolled on
asyncio rather than pulling in a web framework: it answers two paths
and has no reason to be bigger than this.

Readiness is distinct from liveness on purpose:

- ``/health`` is liveness. The event loop is running and answering.
- ``/ready`` is readiness. The proxy is accepting and not draining.
  A container that is shutting down reports not-ready first, so the
  load balancer stops sending it traffic before the listener closes.
"""

import asyncio
import json
from typing import Callable, Optional


class HealthServer:
    """Serves /health and /ready over plain HTTP."""

    def __init__(
        self,
        port: int,
        host: str = "0.0.0.0",
        status_provider: Optional[Callable[[], dict]] = None,
    ) -> None:
        self.port = port
        self.host = host
        self.status_provider = status_provider
        self.ready = False
        self._server: Optional[asyncio.AbstractServer] = None

    async def start(self) -> None:
        self._server = await asyncio.start_server(
            self._handle, host=self.host, port=self.port, reuse_address=True
        )
        self.ready = True

    def begin_draining(self) -> None:
        """Report not-ready so traffic drains before the listener closes."""
        self.ready = False

    async def stop(self) -> None:
        self.ready = False
        if self._server is not None:
            self._server.close()
            await self._server.wait_closed()
            self._server = None

    async def _handle(
        self, reader: asyncio.StreamReader, writer: asyncio.StreamWriter
    ) -> None:
        try:
            request_line = await asyncio.wait_for(reader.readline(), timeout=5)
            parts = request_line.decode("iso-8859-1", errors="replace").split(" ")
            path = parts[1] if len(parts) > 1 else "/"

            # Drain the rest of the request head so the client is not
            # writing into a socket nobody read.
            while True:
                line = await asyncio.wait_for(reader.readline(), timeout=5)
                if line in (b"\r\n", b"\n", b""):
                    break

            if path.startswith("/ready"):
                healthy = self.ready
            elif path.startswith("/health"):
                healthy = True
            else:
                await self._respond(writer, 404, {"error": "not found"})
                return

            body = {"status": "ok" if healthy else "draining"}
            if self.status_provider is not None:
                try:
                    body.update(self.status_provider())
                except Exception as exc:  # never let a probe crash on stats
                    body["stats_error"] = repr(exc)

            await self._respond(writer, 200 if healthy else 503, body)
        except (asyncio.TimeoutError, ConnectionResetError, BrokenPipeError):
            pass
        finally:
            try:
                writer.close()
                await writer.wait_closed()
            except (ConnectionResetError, BrokenPipeError, OSError):
                pass

    @staticmethod
    async def _respond(writer: asyncio.StreamWriter, status: int, body: dict) -> None:
        payload = json.dumps(body).encode()
        reason = {200: "OK", 404: "Not Found", 503: "Service Unavailable"}[status]
        writer.write(
            (
                f"HTTP/1.1 {status} {reason}\r\n"
                "Content-Type: application/json\r\n"
                f"Content-Length: {len(payload)}\r\n"
                "Connection: close\r\n\r\n"
            ).encode()
            + payload
        )
        await writer.drain()
