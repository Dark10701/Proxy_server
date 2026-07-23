# HTTP/HTTPS Forward Proxy

An asyncio forward proxy with domain filtering, adaptive load shedding, a Redis-backed response cache, and Prometheus metrics — deployable as a load-balanced multi-instance stack.

[![CI](https://github.com/Dark10701/Proxy_server/actions/workflows/ci.yml/badge.svg)](https://github.com/Dark10701/Proxy_server/actions/workflows/ci.yml)

---

## Architecture

```text
                      ┌──────────────────────────────────────┐
   clients ──────────▶│ nginx (stream / layer 4)             │
                      │ least_conn, passive health checks    │
                      └───────────────┬──────────────────────┘
                          ┌───────────┼───────────┐
                          ▼           ▼           ▼
                     ┌─────────┐ ┌─────────┐ ┌─────────┐
                     │ proxy 1 │ │ proxy 2 │ │ proxy 3 │   asyncio, one task
                     └────┬────┘ └────┬────┘ └────┬────┘   per connection
                          └───────────┼───────────┘
                            ┌─────────┴─────────┐
                            ▼                   ▼
                      ┌───────────┐      ┌─────────────┐
                      │   Redis   │      │  upstream   │
                      │  (cache)  │      │   servers   │
                      └───────────┘      └─────────────┘

   each instance also exposes:  :9100/metrics ──▶ Prometheus ──▶ Grafana
                                :8081/health, /ready ──▶ healthchecks, nginx
```

Inside one instance, a request travels:

```text
accept ──▶ read + parse head ──▶ per-client rate limit ──▶ admission control
                                                                   │
                    ┌──────────────────────────────────────────────┘
                    ▼
              domain filter ──▶ cache lookup ──▶ upstream fetch ──▶ relay to client
                    │                  │                                  │
                  403                 hit ─────────────────────────▶ store if cacheable
```

`CONNECT` skips the cache entirely and becomes two `StreamReader`/`StreamWriter` pumps relaying bytes in both directions.

| Module | Responsibility |
|---|---|
| `async_server.py` | `asyncio.start_server` accept loop, lifecycle, health/metrics listeners |
| `async_handler.py` | Per-connection handling: parse, filter, cache, forward, tunnel |
| `async_scheduler.py` | Bounded admission control, waiters released by priority |
| `rate_controller.py` | Adaptive token bucket driven by observed upstream latency |
| `cache.py` | Cache-Control/Expires policy and Redis storage |
| `observability.py` | Prometheus collectors |
| `health.py` | `/health` (liveness) and `/ready` (readiness) |
| `filter_engine.py` | Domain and keyword blocklist |
| `http_parser.py` | Request-line/header parsing, origin-form rewriting |
| `server.py`, `client_handler.py` | Legacy threaded build, retained for benchmarking |

## Quickstart

```bash
pip install -r requirements.txt
```

Run the proxy:

```bash
python -m proxy_server.main --host 0.0.0.0 --port 8080
```

Send traffic through it:

```bash
curl -x http://127.0.0.1:8080 http://example.org
```

The full stack — 3 proxy instances behind nginx, plus Redis, Prometheus and Grafana:

```bash
docker compose up
```

| Endpoint | Purpose |
|---|---|
| `http://localhost:8080` | Proxy, load balanced across instances |
| `http://localhost:9090` | Prometheus |
| `http://localhost:3000` | Grafana (admin/admin, dashboard pre-provisioned) |
| `http://localhost:8081/health` | Liveness |
| `http://localhost:8081/ready` | Readiness; reports draining during shutdown |

To verify the stack actually behaves as described — filtering and CONNECT through nginx, load distributed across instances, the cache shared between them, Prometheus targets up, and no traffic dropped when an instance goes down:

```bash
docker compose --profile verify up -d --build && ./scripts/verify_stack.sh
```

The `verify` profile adds a small origin that returns a cacheable response, which is what makes the shared-cache check meaningful. It is not started by a plain `docker compose up`.

Optional CSV-backed dashboard, independent of the Prometheus stack:

```bash
python dashboard/app.py
```

Tests:

```bash
python -m pytest
```

## Benchmarks

<!--BENCHMARKS-->

Full methodology, environment, per-concurrency tables and the caveats that matter are in [`benchmarks/RESULTS.md`](benchmarks/RESULTS.md). **Read the caveats before quoting any figure** — these come from a single laptop with a Python load generator that is itself a ceiling.

Measured on Windows-10-10.0.26200-SP0, 8 cores, Python 3.11.4. Each cell is the median of 3 runs of 10s.

**Peak sustained throughput** (each build at the concurrency where it peaks):

| | Peak req/s | at concurrency |
|---|---:|---:|
| Load generator ceiling (no proxy) | 621.7 | 400 |
| Threaded build | 233.7 | 100 |
| Async build | **311.6** | 50 |

**Head to head at concurrency 50** (same load, so latency is comparable):

| | req/s | p50 | p95 | p99 |
|---|---:|---:|---:|---:|
| Threaded | 217.7 | 226.02 ms | 321.98 ms | 369.0 ms |
| Async | **311.6** | 151.85 ms | 188.34 ms | 221.47 ms |

At the same offered load the async build serves **+43%** more requests per second with a **40% lower p99**. Peak-to-peak the gain is +33%. Both remain well below the 621.7 req/s the generator reaches with no proxy in the path, so the proxy — not the client — is what is being measured.

**Cache hit vs no cache**, async build at concurrency 100. Identical URL and origin in both arms; the only variable is whether the cache is enabled.

| | req/s | p50 | p95 | p99 |
|---|---:|---:|---:|---:|
| Cache disabled | 280.1 | 326.34 ms | 508.69 ms | 588.36 ms |
| Serving cache hits | **417.2** | 214.04 ms | 340.66 ms | 382.18 ms |

Serving from cache is **+49%** on throughput with a **35% lower p99**. The cache arm talks to a minimal RESP server rather than real Redis, which could not be installed on the benchmark machine — over a real socket with the real client, so this is a pessimistic bound rather than an optimistic one.

<!--/BENCHMARKS-->

## Design decisions

**asyncio over threads.** The original design allocated a thread per connection. A proxy is almost entirely I/O wait — it holds a client socket open while waiting on an upstream — so those threads were spending their lives blocked, each carrying a stack and a scheduling slot. Under an event loop the same waiting costs a suspended coroutine. The threaded build is still present behind `--mode threaded` rather than deleted, because a performance claim you cannot re-run is not evidence; both builds ship from one codebase so they can be measured under identical conditions.

**A bounded scheduler, not unbounded acceptance.** Accepting everything and hoping turns overload into memory growth and uniformly degraded latency for everyone. Both builds cap in-flight work and answer `503` past the queue limit, so overload is an explicit, fast refusal. Queued requests are released by priority, so a short interactive `GET` is not stuck behind a bulk upload.

**What the rate limiter adapts to.** There are two, doing different jobs. The fixed sliding window is *policy*: no client gets more than N requests per minute to one host. The `RateController` is *protection*: a token bucket whose fill rate tracks observed upstream latency, so when an origin slows down the proxy stops adding load to something already struggling. It adapts on the **median** of recent latencies, not the mean — the mean lets one large download drag the average past the back-off threshold and clamp the entire proxy, which is a self-inflicted outage. `CONNECT` durations are deliberately excluded from the signal: a tunnel's lifetime measures how long the client kept it open, not how fast the upstream is.

**The default Redis URL avoids database 0.** A developer machine very often already runs a Redis on 6379 for some other project, and that project is using db 0. Defaulting there means merely starting this proxy writes cache entries into someone else's keyspace, consumes their memory, and — under the `allkeys-lru` policy most local Redis setups use — can evict their data. The default is `redis://127.0.0.1:6379/11` so it works out of the box against a local Redis without colliding with anything. Inside `docker compose` the Redis is dedicated, so db 0 is passed explicitly there.

**Redis for the cache.** The cache has to be shared. With N instances behind a load balancer, a per-process cache means the hit ratio falls roughly by a factor of N — each instance independently misses on content its siblings already hold — and invalidation has no single place to happen. Redis gives one shared keyspace with native TTL expiry, which is exactly the primitive an HTTP cache needs, and it is out-of-process, so restarting a proxy instance does not empty the cache. The dependency is optional in the strong sense: unreachable Redis logs once, backs off, and every lookup reports a miss.

**Nagle disabled.** A proxy writes a complete message and then waits for the peer. Nagle's algorithm holds the trailing segment until the previous one is acknowledged, and the peer's delayed-ACK timer holds that acknowledgement — the two interact to add a fixed stall to every exchange, with nothing to coalesce. This was measurable as a hard latency floor at concurrency 1 before `TCP_NODELAY` was set.

**Metrics off the hot path.** The threaded build took a mutex and `fsync`'d on every request. The async build hands rows to an `asyncio.Queue` drained by a single writer task, with the blocking write on a worker thread. The trade is explicit: an abrupt kill can lose up to one flush interval of rows. These are observability records, not transactions, and paying an `fsync` per request would dominate the very latency it is trying to measure.

**Prometheus on a separate port.** The proxy port speaks proxy protocol — absolute-form request lines and `CONNECT`. An origin-form `GET /metrics` arriving there is ambiguous at best and a filtering bypass at worst. Outcome labels are a closed set; URLs and hosts are deliberately not labels, since unbounded cardinality is the standard way to melt a Prometheus server.

**nginx at layer 4, not layer 7.** A forward proxy receives absolute-form request lines and `CONNECT`, neither of which survives an HTTP reverse proxy that wants to parse, rewrite and re-target them. TCP pass-through hands the bytes through untouched. `least_conn` rather than round robin, because `CONNECT` tunnels are long-lived and counting live sessions reflects real load far better than counting hand-offs.

**What the parser deliberately does not implement.** It handles the request line, headers, and a `Content-Length` body — enough to route, filter, and forward. It does **not** implement: chunked request bodies (responses are relayed as raw bytes, so chunked *responses* pass through untouched); keep-alive or pipelining, since `Connection: close` is forced upstream and the client socket closes after one exchange; `Transfer-Encoding` negotiation; header folding; or HTTP/2. This is a deliberate boundary — a fully RFC-conformant parser is a large project on its own, and the interesting problems here are concurrency, caching, and load management. The limits are enforced rather than assumed: an oversized header block is refused with `431` instead of being silently truncated.

## Known scope boundaries

- **No keep-alive.** One request per client connection. Connection setup cost is therefore inside every latency number reported above, and throughput is lower than a keep-alive proxy would achieve.
- **Cache does not revalidate.** There is no `If-None-Match`/`If-Modified-Since` handling, so `no-cache` and `must-revalidate` mean "do not serve from cache" rather than "revalidate, then serve". A stale entry is never served. Heuristic freshness from `Last-Modified` is not implemented — no explicit lifetime means no caching. Responses carrying `Vary` are not cached at all rather than cached against the wrong key.
- **HTTPS is tunnelled, not inspected.** `CONNECT` traffic is opaque, so filtering applies to the target host only, and the cache never sees it. There is no TLS interception.
- **Filtering is exact-domain and substring-keyword.** No regex, no wildcards beyond subdomain matching, no PAC files.
- **No HTTP/2 or HTTP/3**, upstream or downstream.
- **The async accept loop shares the event loop with request handling.** Under a burst of short-lived connections the listen backlog can fill faster than it drains, and clients see connection refusals — measured at concurrency 400 in the benchmark sweep. The threaded build has a dedicated accept thread and does not show this. Moving accept to its own loop or process would fix it; that work has not been done.
- **Benchmarked on a single machine** with a Python load generator, which is itself a measurable ceiling. See the methodology notes in `benchmarks/RESULTS.md` before quoting any figure.
- **The compose stack is a local development topology.** Grafana ships with default credentials and Redis has no auth; neither is fit to expose beyond localhost as configured.

## Repository layout

```text
proxy_server/      proxy core (async build + retained threaded build)
dashboard/         Flask + SocketIO view over the metrics CSV
tests/             pytest suite
benchmarks/        load generator, origin, harness, results
grafana/           dashboard JSON and provisioning
prometheus/        scrape config
nginx/             layer 4 load balancer config
```

## License

No license is currently declared, which means default copyright applies.
