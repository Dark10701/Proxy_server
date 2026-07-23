"""Minimal, fast origin server for benchmarking.

Deliberately hand-rolled on asyncio rather than http.server: the origin
must never be the bottleneck, or the benchmark measures the origin
instead of the proxy. It writes a fixed, pre-built response with no
per-request formatting.

Paths:
  /              non-cacheable body (Cache-Control: no-store)
  /cacheable     Cache-Control: max-age=3600
"""

import argparse
import asyncio
import socket

BODY = b"x" * 1024


def build(cache_control: str) -> bytes:
    return (
        "HTTP/1.1 200 OK\r\n"
        "Content-Type: text/plain\r\n"
        f"Content-Length: {len(BODY)}\r\n"
        f"Cache-Control: {cache_control}\r\n"
        "Connection: close\r\n"
        "\r\n"
    ).encode() + BODY


CACHEABLE = build("max-age=3600")
NON_CACHEABLE = build("no-store")


async def handle(reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
    # The origin must not add Nagle stalls to the measurement either.
    sock = writer.get_extra_info("socket")
    if sock is not None:
        try:
            sock.setsockopt(socket.IPPROTO_TCP, socket.TCP_NODELAY, 1)
        except OSError:
            pass
    try:
        request_line = await reader.readline()
        if not request_line:
            return
        while True:
            line = await reader.readline()
            if line in (b"\r\n", b"\n", b""):
                break
        path = request_line.split(b" ")[1] if b" " in request_line else b"/"
        writer.write(CACHEABLE if b"/cacheable" in path else NON_CACHEABLE)
        await writer.drain()
    except (ConnectionResetError, BrokenPipeError, asyncio.IncompleteReadError):
        pass
    finally:
        try:
            writer.close()
            await writer.wait_closed()
        except (ConnectionResetError, BrokenPipeError, OSError):
            pass


async def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--port", type=int, default=8000)
    args = parser.parse_args()

    server = await asyncio.start_server(
        handle, args.host, args.port, backlog=2048, reuse_address=True
    )
    print(f"origin listening on {args.host}:{args.port}", flush=True)
    async with server:
        await server.serve_forever()


if __name__ == "__main__":
    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        pass
