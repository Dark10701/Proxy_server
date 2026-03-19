"""Entry point for the multi-threaded HTTP proxy server."""

import argparse

from metrics_store import MetricsStore
from monitoring_server import start_monitoring_server
from server import ProxyServer


def parse_args() -> argparse.Namespace:
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
        default="config/blocked_domains.txt",
        help="Path to blocked domains file",
    )
    parser.add_argument(
        "--access-log",
        default="logs/access.log",
        help="Path to access log file",
    )
    parser.add_argument(
        "--error-log",
        default="logs/error.log",
        help="Path to error log file",
    )
    return parser.parse_args()


def main() -> None:
    args = parse_args()
    metrics_store = MetricsStore()
    server = ProxyServer(
        host=args.host,
        port=args.port,
        blocked_domains_path=args.blocked_domains,
        metrics_store=metrics_store,
        access_log_path=args.access_log,
        error_log_path=args.error_log,
    )
    start_monitoring_server(
        metrics_store=metrics_store,
        rate_controller=server.rate_controller,
        scheduler=server.scheduler,
        host="0.0.0.0",
        port=9090,
    )

    print(f"Proxy running on {args.host}:{args.port}")
    print("Monitoring dashboard available at http://localhost:9090")
    server.start()


if __name__ == "__main__":
    main()
