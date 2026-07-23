# proxy_server

Core proxy implementation. See the [root README](../README.md) for
architecture, design decisions and benchmarks.

## Async build (default)

| File | Role |
|---|---|
| `main.py` | CLI entry point; selects the concurrency model |
| `async_server.py` | `asyncio.start_server` accept loop and lifecycle |
| `async_handler.py` | Per-connection: parse, filter, cache, forward, tunnel |
| `async_scheduler.py` | Bounded admission control, priority-ordered waiters |
| `async_metrics.py` | Queue-backed CSV writer, single writer task |

## Shared

| File | Role |
|---|---|
| `http_parser.py` | Request line, headers, origin-form rewriting |
| `filter_engine.py` | Domain and keyword blocklist |
| `rate_controller.py` | Adaptive token bucket |
| `scheduler.py` | Request priority classification |
| `cache.py` | HTTP caching policy and Redis storage |
| `observability.py` | Prometheus collectors |
| `health.py` | Liveness and readiness endpoints |
| `netutil.py` | Socket options (TCP_NODELAY) |
| `logger.py`, `metrics.py`, `paths.py` | Logging, CSV metrics, path resolution |

## Legacy threaded build

`server.py` and `client_handler.py` implement the original
thread-per-connection design, reachable with `--mode threaded`. It is
retained so the two concurrency models can be benchmarked under
identical conditions, not because it is the recommended path.

`client_simulation.py` is a small threaded load-generator script; the
maintained benchmark harness lives in [`../benchmarks/`](../benchmarks).
