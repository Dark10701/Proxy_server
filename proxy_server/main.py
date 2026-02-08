"""Entry point for the multi-threaded HTTP proxy server."""

import argparse

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
        "--metrics-path",
        default="logs/metrics.csv",
        help="Path to CSV metrics log",
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
    server = ProxyServer(
        host=args.host,
        port=args.port,
        blocked_domains_path=args.blocked_domains,
        metrics_path=args.metrics_path,
        access_log_path=args.access_log,
        error_log_path=args.error_log,
    )
    server.start()


if __name__ == "__main__":
    main()
