"""Entry point for the HTTP proxy server (async by default)."""

import argparse
import sys
from pathlib import Path

# Support both `python proxy_server/main.py` and `python -m proxy_server.main`.
# Running the file directly leaves the package off sys.path, so put it back
# before importing anything from the package.
if __package__ in (None, ""):
    sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from proxy_server import paths  # noqa: E402
from proxy_server.cache import DEFAULT_REDIS_URL  # noqa: E402
from proxy_server.server import ProxyServer  # noqa: E402


def parse_args(argv=None) -> argparse.Namespace:
    """Parse CLI arguments for proxy runtime configuration."""
    parser = argparse.ArgumentParser(
        description="HTTP/HTTPS forward proxy with filtering, caching and metrics"
    )
    parser.add_argument(
        "--host", default="0.0.0.0", help="IP address to listen on (default: 0.0.0.0)"
    )
    parser.add_argument(
        "--port", type=int, default=8080, help="Port to listen on (default: 8080)"
    )
    parser.add_argument(
        "--blocked-domains",
        default=str(paths.DEFAULT_BLOCKED_DOMAINS),
        help="Path to blocked domains file",
    )
    parser.add_argument(
        "--metrics",
        default=str(paths.DEFAULT_METRICS),
        help="Path to CSV metrics file",
    )
    parser.add_argument(
        "--access-log",
        default=str(paths.DEFAULT_ACCESS_LOG),
        help="Path to access log file",
    )
    parser.add_argument(
        "--error-log",
        default=str(paths.DEFAULT_ERROR_LOG),
        help="Path to error log file",
    )
    parser.add_argument(
        "--mode",
        choices=("async", "threaded"),
        default="async",
        help=(
            "Concurrency model. 'async' is the current implementation; "
            "'threaded' is the legacy build, kept so the two can be "
            "benchmarked under identical conditions (default: async)"
        ),
    )
    parser.add_argument(
        "--max-concurrent",
        type=int,
        default=256,
        help="Async mode: requests served upstream at once (default: 256)",
    )
    parser.add_argument(
        "--max-queued",
        type=int,
        default=512,
        help="Async mode: requests allowed to wait for a slot (default: 512)",
    )
    parser.add_argument(
        "--redis-url",
        default=DEFAULT_REDIS_URL,
        help=(
            "Redis URL for the response cache (default: %(default)s). "
            "The default deliberately avoids database 0, which is what "
            "any other project sharing a local Redis will be using"
        ),
    )
    parser.add_argument(
        "--metrics-port",
        type=int,
        default=9100,
        help="Port for the Prometheus /metrics endpoint (0 disables)",
    )
    parser.add_argument(
        "--rate-limit-requests",
        type=int,
        default=200,
        help="Per client+host requests per minute; 0 disables (default: 200)",
    )
    parser.add_argument(
        "--no-adaptive-rate-limit",
        action="store_true",
        help="Disable the adaptive rate controller (used for benchmarking)",
    )
    parser.add_argument(
        "--health-port",
        type=int,
        default=8081,
        help="Port for /health and /ready (0 disables)",
    )
    parser.add_argument(
        "--no-cache",
        action="store_true",
        help="Disable the response cache entirely",
    )
    return parser.parse_args(argv)


def run_async(args) -> None:
    """Run the asyncio implementation."""
    import asyncio

    from proxy_server.async_server import AsyncProxyServer

    server = AsyncProxyServer(
        host=args.host,
        port=args.port,
        blocked_domains_path=args.blocked_domains,
        metrics_path=args.metrics,
        access_log_path=args.access_log,
        error_log_path=args.error_log,
        max_concurrent=args.max_concurrent,
        max_queued=args.max_queued,
        cache_enabled=not args.no_cache,
        redis_url=args.redis_url,
        metrics_port=args.metrics_port or None,
        health_port=args.health_port or None,
        rate_limit_requests=args.rate_limit_requests,
        adaptive_rate_limit=not args.no_adaptive_rate_limit,
    )

    async def _main() -> None:
        try:
            await server.serve_forever()
        finally:
            await server.stop()

    try:
        asyncio.run(_main())
    except KeyboardInterrupt:
        print("\nShutting down.")


def run_threaded(args) -> None:
    """Run the legacy thread-pool implementation."""
    server = ProxyServer(
        host=args.host,
        port=args.port,
        blocked_domains_path=args.blocked_domains,
        metrics_path=args.metrics,
        access_log_path=args.access_log,
        error_log_path=args.error_log,
        rate_limit_requests=args.rate_limit_requests,
        adaptive_rate_limit=not args.no_adaptive_rate_limit,
    )
    try:
        server.start()
    except KeyboardInterrupt:
        print("\nShutting down.")
        server.stop()


def main() -> None:
    """Start the proxy server and print quick operator hints."""
    args = parse_args()

    print(f"Proxy running on {args.host}:{args.port} ({args.mode} mode)")
    print(f"Metrics CSV: {args.metrics}")

    if args.mode == "async":
        run_async(args)
    else:
        run_threaded(args)


if __name__ == "__main__":
    main()
