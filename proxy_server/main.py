"""Entry point for the multi-threaded HTTP proxy server."""

import argparse
import sys
from pathlib import Path

# Support both `python proxy_server/main.py` and `python -m proxy_server.main`.
# Running the file directly leaves the package off sys.path, so put it back
# before importing anything from the package.
if __package__ in (None, ""):
    sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from proxy_server import paths  # noqa: E402
from proxy_server.server import ProxyServer  # noqa: E402


def parse_args(argv=None) -> argparse.Namespace:
    """Parse CLI arguments for proxy runtime configuration."""
    parser = argparse.ArgumentParser(
        description="Multi-Threaded HTTP Proxy Server with Content Filtering"
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
    return parser.parse_args(argv)


def main() -> None:
    """Start the proxy server and print quick operator hints."""
    args = parse_args()

    server = ProxyServer(
        host=args.host,
        port=args.port,
        blocked_domains_path=args.blocked_domains,
        metrics_path=args.metrics,
        access_log_path=args.access_log,
        error_log_path=args.error_log,
    )

    print(f"Proxy running on {args.host}:{args.port}")
    print(f"Metrics CSV: {args.metrics}")
    try:
        server.start()
    except KeyboardInterrupt:
        print("\nShutting down.")
        server.stop()


if __name__ == "__main__":
    main()
