"""Closed-loop HTTP load generator.

Written because wrk and ab are not available on the machine these
numbers were produced on. It is deliberately simple and reports its own
overhead so the results can be judged honestly:

- Closed loop: C workers each issue a request, wait for the full
  response, then issue the next. Throughput is therefore an outcome of
  latency, not an independently applied rate.
- One request per connection. The proxy sends `Connection: close` and
  closes the client socket after each request, so keep-alive is not
  available to measure. Connection setup cost is inside every number
  here, for both builds equally.
- Run it against the origin directly (--direct) to establish the
  generator's own ceiling. Any proxy result near that ceiling is
  measuring this script, not the proxy.
- Timing uses time.perf_counter(), not time.monotonic(). On Windows
  monotonic() is backed by GetTickCount64 with a 15.6 ms tick, which
  quantises sub-millisecond proxy latencies into meaningless steps.

Latencies are wall-clock from just before connect() to the last byte of
the response.
"""

import argparse
import asyncio
import json
import time
from typing import Dict, List, Optional


class Results:
    def __init__(self) -> None:
        self.latencies: List[float] = []
        self.statuses: Dict[str, int] = {}
        self.errors: Dict[str, int] = {}
        self.bytes_read = 0

    def record(self, latency: float, status: str, size: int) -> None:
        self.latencies.append(latency)
        self.statuses[status] = self.statuses.get(status, 0) + 1
        self.bytes_read += size

    def record_error(self, kind: str) -> None:
        self.errors[kind] = self.errors.get(kind, 0) + 1


def percentile(values: List[float], pct: float) -> float:
    if not values:
        return 0.0
    ordered = sorted(values)
    rank = max(1, int(round(pct / 100.0 * len(ordered))))
    return ordered[min(rank, len(ordered)) - 1]


async def worker(
    host: str,
    port: int,
    request: bytes,
    deadline: float,
    results: Results,
    connect_timeout: float,
    read_timeout: float,
) -> None:
    while time.perf_counter() < deadline:
        started = time.perf_counter()
        writer = None
        try:
            reader, writer = await asyncio.wait_for(
                asyncio.open_connection(host, port), timeout=connect_timeout
            )
            writer.write(request)
            await writer.drain()

            payload = bytearray()
            while True:
                chunk = await asyncio.wait_for(
                    reader.read(65536), timeout=read_timeout
                )
                if not chunk:
                    break
                payload.extend(chunk)

            elapsed = time.perf_counter() - started
            if payload.startswith(b"HTTP/"):
                status = payload.split(b" ")[1].decode("ascii", "replace")
            else:
                status = "malformed"
            results.record(elapsed, status, len(payload))
        except asyncio.TimeoutError:
            results.record_error("timeout")
        except ConnectionRefusedError:
            results.record_error("refused")
        except ConnectionResetError:
            results.record_error("reset")
        except OSError as exc:
            results.record_error(type(exc).__name__)
        finally:
            if writer is not None:
                try:
                    writer.close()
                except OSError:
                    pass


async def run(args) -> dict:
    if args.direct:
        target_host, target_port = args.origin_host, args.origin_port
        request = (
            f"GET {args.path} HTTP/1.1\r\n"
            f"Host: {args.origin_host}:{args.origin_port}\r\n"
            "Connection: close\r\n\r\n"
        ).encode()
    else:
        target_host, target_port = args.proxy_host, args.proxy_port
        # Absolute-form request line: this is what a forward proxy expects.
        request = (
            f"GET http://{args.origin_host}:{args.origin_port}{args.path} HTTP/1.1\r\n"
            f"Host: {args.origin_host}:{args.origin_port}\r\n"
            "Connection: close\r\n\r\n"
        ).encode()

    results = Results()

    # Warm-up excluded from the measurement: first requests pay import,
    # JIT-free interpreter warm paths, and cache population.
    if args.warmup > 0:
        warm_deadline = time.perf_counter() + args.warmup
        warm = Results()
        await asyncio.gather(
            *[
                worker(
                    target_host, target_port, request, warm_deadline, warm,
                    args.connect_timeout, args.read_timeout,
                )
                for _ in range(min(args.concurrency, 32))
            ]
        )

    started = time.perf_counter()
    deadline = started + args.duration
    await asyncio.gather(
        *[
            worker(
                target_host, target_port, request, deadline, results,
                args.connect_timeout, args.read_timeout,
            )
            for _ in range(args.concurrency)
        ]
    )
    wall = time.perf_counter() - started

    total = len(results.latencies)
    errors = sum(results.errors.values())
    ok = results.statuses.get("200", 0)

    return {
        "label": args.label,
        "target": "origin-direct" if args.direct else "proxy",
        "concurrency": args.concurrency,
        "duration_s": round(wall, 2),
        "requests": total,
        "ok_200": ok,
        "non_200": total - ok,
        "errors": errors,
        "error_kinds": results.errors,
        "statuses": results.statuses,
        "rps": round(total / wall, 1) if wall > 0 else 0.0,
        "ok_rps": round(ok / wall, 1) if wall > 0 else 0.0,
        "throughput_mib_s": round(results.bytes_read / wall / (1024 * 1024), 2)
        if wall > 0
        else 0.0,
        "latency_ms": {
            "mean": round(
                sum(results.latencies) / total * 1000, 2
            ) if total else 0.0,
            "p50": round(percentile(results.latencies, 50) * 1000, 2),
            "p95": round(percentile(results.latencies, 95) * 1000, 2),
            "p99": round(percentile(results.latencies, 99) * 1000, 2),
            "max": round(max(results.latencies) * 1000, 2) if total else 0.0,
        },
    }


def parse_args(argv: Optional[List[str]] = None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--proxy-host", default="127.0.0.1")
    parser.add_argument("--proxy-port", type=int, default=8080)
    parser.add_argument("--origin-host", default="127.0.0.1")
    parser.add_argument("--origin-port", type=int, default=8000)
    parser.add_argument("--path", default="/")
    parser.add_argument("--concurrency", type=int, default=50)
    parser.add_argument("--duration", type=float, default=10.0)
    parser.add_argument("--warmup", type=float, default=2.0)
    parser.add_argument("--connect-timeout", type=float, default=10.0)
    parser.add_argument("--read-timeout", type=float, default=30.0)
    parser.add_argument("--label", default="run")
    parser.add_argument(
        "--direct",
        action="store_true",
        help="Bypass the proxy to measure this generator's own ceiling",
    )
    return parser.parse_args(argv)


if __name__ == "__main__":
    print(json.dumps(asyncio.run(run(parse_args())), indent=2))
