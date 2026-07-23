"""One-command local demo: proxy + dashboard + live traffic.

Replaces the old three-terminal dance (start proxy, curl some traffic,
start dashboard) with a single command:

    python scripts/run_local.py

It starts, wires together and later cleans up:

  * a bundled origin server (so there is something to forward to offline)
  * a bundled minimal Redis (so cache hits actually appear without you
    installing Redis)
  * the proxy, pointed at both
  * the Flask dashboard, reading the same metrics file
  * a gentle stream of demo traffic -- allowed, cacheable and blocked --
    so the dashboard has something to show the moment it opens

then opens the dashboard in your browser. Press Ctrl-C to stop
everything.

Everything is loopback-only and needs no internet: the "allowed" traffic
goes to the bundled origin, and the "blocked" traffic is refused by the
filter before any upstream is contacted.
"""

import argparse
import atexit
import socket
import subprocess
import sys
import threading
import time
import urllib.error
import urllib.request
import webbrowser
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
PYTHON = sys.executable

_procs = []
_stop = threading.Event()


def free_port() -> int:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


def wait_for_port(port: int, timeout: float = 20.0) -> bool:
    deadline = time.time() + timeout
    while time.time() < deadline:
        with socket.socket() as s:
            s.settimeout(0.5)
            if s.connect_ex(("127.0.0.1", port)) == 0:
                return True
        time.sleep(0.15)
    return False


def spawn(name: str, args, quiet: bool = True):
    out = subprocess.DEVNULL if quiet else None
    proc = subprocess.Popen(args, cwd=str(ROOT), stdout=out, stderr=out)
    _procs.append((name, proc))
    return proc


def shutdown():
    _stop.set()
    for name, proc in reversed(_procs):
        if proc.poll() is None:
            proc.terminate()
    for name, proc in reversed(_procs):
        try:
            proc.wait(timeout=8)
        except subprocess.TimeoutExpired:
            proc.kill()
    _procs.clear()


def demo_traffic(proxy_port: int, origin_port: int):
    """Send a small, steady mix of requests through the proxy.

    Allowed + cacheable goes to the bundled origin; blocked goes to a
    domain the default policy refuses (answered locally, no network).
    """
    proxy = urllib.request.ProxyHandler(
        {"http": f"http://127.0.0.1:{proxy_port}"}
    )
    opener = urllib.request.build_opener(proxy)
    targets = [
        f"http://127.0.0.1:{origin_port}/cacheable",   # allowed, cacheable
        f"http://127.0.0.1:{origin_port}/cacheable",   # again -> cache hit
        f"http://127.0.0.1:{origin_port}/",            # allowed, no-store
        "http://facebook.com/",                        # blocked by policy
    ]
    i = 0
    while not _stop.is_set():
        url = targets[i % len(targets)]
        i += 1
        try:
            opener.open(url, timeout=5).read()
        except urllib.error.HTTPError:
            pass  # 403 for the blocked domain is expected
        except Exception:
            pass  # never let the demo generator crash the launcher
        _stop.wait(0.8)


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--proxy-port", type=int, default=8080)
    parser.add_argument("--dashboard-port", type=int, default=5000)
    parser.add_argument("--no-browser", action="store_true",
                        help="Do not open a web browser")
    parser.add_argument("--no-demo-traffic", action="store_true",
                        help="Do not generate demo traffic")
    parser.add_argument("--run-seconds", type=int, default=0,
                        help="Exit automatically after N seconds (0 = run "
                             "until Ctrl-C; used by the test)")
    args = parser.parse_args()

    atexit.register(shutdown)

    metrics = ROOT / "logs" / "demo_metrics.csv"
    metrics.parent.mkdir(parents=True, exist_ok=True)
    if metrics.exists():
        metrics.unlink()

    origin_port = free_port()
    redis_port = free_port()

    print("Starting local proxy demo...")

    # 1. bundled origin and minimal Redis (both loopback, no internet)
    spawn("origin", [PYTHON, "benchmarks/origin_server.py",
                     "--port", str(origin_port)])
    spawn("redis", [PYTHON, "benchmarks/mini_redis.py",
                    "--port", str(redis_port)])
    if not wait_for_port(origin_port) or not wait_for_port(redis_port):
        print("ERROR: bundled origin or redis did not start")
        shutdown()
        sys.exit(1)

    # 2. the proxy, pointed at both, metrics to the demo file
    spawn("proxy", [
        PYTHON, "-m", "proxy_server.main",
        "--host", "127.0.0.1", "--port", str(args.proxy_port),
        "--redis-url", f"redis://127.0.0.1:{redis_port}/0",
        "--metrics", str(metrics),
        "--access-log", str(ROOT / "logs" / "demo_access.log"),
        "--error-log", str(ROOT / "logs" / "demo_error.log"),
        # Keep the demo to one listener to avoid clashing with anything
        # else already using the health/metrics ports.
        "--metrics-port", "0", "--health-port", "0",
    ])
    if not wait_for_port(args.proxy_port):
        print("ERROR: proxy did not start")
        shutdown()
        sys.exit(1)

    # 3. the dashboard, reading the same metrics file
    spawn("dashboard", [
        PYTHON, "dashboard/app.py",
        "--host", "127.0.0.1", "--port", str(args.dashboard_port),
        "--metrics", str(metrics),
    ])
    if not wait_for_port(args.dashboard_port):
        print("ERROR: dashboard did not start")
        shutdown()
        sys.exit(1)

    # 4. demo traffic so the dashboard is alive on first open
    if not args.no_demo_traffic:
        threading.Thread(
            target=demo_traffic, args=(args.proxy_port, origin_port),
            daemon=True,
        ).start()

    url = f"http://localhost:{args.dashboard_port}"
    print(
        "\n  Proxy      : http://127.0.0.1:%s"
        "  (curl -x http://127.0.0.1:%s http://httpforever.com/)"
        % (args.proxy_port, args.proxy_port)
    )
    print(f"  Dashboard  : {url}")
    print("\nPress Ctrl-C to stop.\n")

    if not args.no_browser:
        try:
            webbrowser.open(url)
        except Exception:
            pass

    try:
        if args.run_seconds > 0:
            time.sleep(args.run_seconds)
        else:
            while True:
                time.sleep(1)
                # If any child died, surface it rather than hanging.
                for name, proc in _procs:
                    if proc.poll() is not None:
                        print(f"\n{name} exited unexpectedly; shutting down.")
                        return
    except KeyboardInterrupt:
        print("\nStopping.")
    finally:
        shutdown()


if __name__ == "__main__":
    main()
