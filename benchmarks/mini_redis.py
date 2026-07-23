"""A minimal RESP server, standing in for Redis during benchmarking.

Redis could not be installed on the machine these numbers came from
(no passwordless sudo, no Docker daemon). Rather than swap in an
in-process dict and quietly drop the network from the measurement, this
speaks enough of the RESP protocol for the real redis-py client to talk
to it over a real TCP socket.

What that means for the numbers:

- Included: redis-py client overhead, RESP encode/decode, loopback TCP
  round trip, and the proxy's full cache code path.
- Not included: real Redis's performance characteristics. Redis is
  written in C and is almost certainly faster than this at serving a
  GET, so cache-hit latency measured here is a pessimistic bound, not
  an optimistic one.

Implements PING, GET, SET (with EX), DEL and a stubbed CONFIG/INFO.
Nothing else. This is a benchmark fixture, not a Redis.
"""

import argparse
import asyncio
import time
from typing import Dict, List, Optional, Tuple


class Store:
    def __init__(self) -> None:
        self.data: Dict[bytes, Tuple[bytes, Optional[float]]] = {}

    def get(self, key: bytes) -> Optional[bytes]:
        entry = self.data.get(key)
        if entry is None:
            return None
        value, expires = entry
        if expires is not None and time.monotonic() > expires:
            self.data.pop(key, None)
            return None
        return value

    def set(self, key: bytes, value: bytes, ttl: Optional[float]) -> None:
        self.data[key] = (value, time.monotonic() + ttl if ttl else None)


async def read_command(reader: asyncio.StreamReader) -> Optional[List[bytes]]:
    """Read one RESP array command. Returns None at EOF."""
    line = await reader.readline()
    if not line:
        return None
    if not line.startswith(b"*"):
        # Inline command, e.g. a raw "PING\r\n" from a probe.
        return line.strip().split()

    count = int(line[1:].strip())
    parts: List[bytes] = []
    for _ in range(count):
        header = await reader.readline()
        if not header.startswith(b"$"):
            return None
        length = int(header[1:].strip())
        if length == -1:
            parts.append(b"")
            continue
        payload = await reader.readexactly(length)
        await reader.readexactly(2)  # trailing CRLF
        parts.append(payload)
    return parts


def bulk(value: Optional[bytes]) -> bytes:
    if value is None:
        return b"$-1\r\n"
    return b"$" + str(len(value)).encode() + b"\r\n" + value + b"\r\n"


async def handle(reader, writer, store: Store) -> None:
    try:
        while True:
            command = await read_command(reader)
            if command is None:
                return
            if not command:
                continue

            name = command[0].upper()
            if name == b"PING":
                writer.write(b"+PONG\r\n")
            elif name == b"GET":
                writer.write(bulk(store.get(command[1])))
            elif name == b"SET":
                ttl = None
                for index, token in enumerate(command):
                    if token.upper() == b"EX" and index + 1 < len(command):
                        ttl = float(command[index + 1])
                store.set(command[1], command[2], ttl)
                writer.write(b"+OK\r\n")
            elif name == b"DEL":
                removed = sum(1 for key in command[1:] if store.data.pop(key, None))
                writer.write(b":" + str(removed).encode() + b"\r\n")
            elif name in (b"CONFIG", b"INFO", b"CLIENT", b"HELLO"):
                writer.write(b"$-1\r\n")
            else:
                writer.write(b"-ERR unsupported command\r\n")
            await writer.drain()
    except (asyncio.IncompleteReadError, ConnectionResetError, BrokenPipeError):
        pass
    finally:
        try:
            writer.close()
            await writer.wait_closed()
        except (OSError, ConnectionResetError):
            pass


async def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--port", type=int, default=6379)
    args = parser.parse_args()

    store = Store()
    server = await asyncio.start_server(
        lambda r, w: handle(r, w, store), args.host, args.port, reuse_address=True
    )
    print(f"mini-redis listening on {args.host}:{args.port}", flush=True)
    async with server:
        await server.serve_forever()


if __name__ == "__main__":
    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        pass
