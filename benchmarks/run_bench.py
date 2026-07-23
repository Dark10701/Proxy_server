"""Run the full benchmark matrix and emit JSON.

Starts an origin, then for each build (threaded, async) sweeps a set of
concurrency levels, restarting the proxy between builds so no state
carries over. Rate limiting is disabled throughout: with the default
policy of 200 requests per client+host per minute, a benchmark would be
measuring the rate limiter rather than the proxy.

Usage:
    python benchmarks/run_bench.py --out benchmarks/results.json
"""

import argparse
import asyncio
import json
import os
import platform
import socket
import subprocess
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from benchmarks import loadgen  # noqa: E402


def free_port() -> int:
    with socket.socket() as probe:
        probe.bind(("127.0.0.1", 0))
        return probe.getsockname()[1]


def wait_for_port(port: int, timeout: float = 20.0) -> bool:
    deadline = time.time() + timeout
    while time.time() < deadline:
        try:
            with socket.create_connection(("127.0.0.1", port), timeout=0.5):
                return True
        except OSError:
            time.sleep(0.1)
    return False


class Process:
    """Subprocess that is always cleaned up."""

    def __init__(self, args, name):
        self.args = args
        self.name = name
        self.proc = None

    def __enter__(self):
        self.proc = subprocess.Popen(
            self.args,
            cwd=str(ROOT),
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
        )
        return self

    def __exit__(self, *exc):
        if self.proc is not None:
            self.proc.terminate()
            try:
                self.proc.wait(timeout=10)
            except subprocess.TimeoutExpired:
                self.proc.kill()


def run_case(**kwargs) -> dict:
    argv = []
    for key, value in kwargs.items():
        flag = "--" + key.replace("_", "-")
        if isinstance(value, bool):
            if value:
                argv.append(flag)
        else:
            argv.extend([flag, str(value)])
    return asyncio.run(loadgen.run(loadgen.parse_args(argv)))


def environment() -> dict:
    return {
        "python": sys.version.split()[0],
        "platform": platform.platform(),
        "processor": platform.processor() or "unknown",
        "cpu_count": os.cpu_count(),
        "timestamp": time.strftime("%Y-%m-%d %H:%M:%S %Z"),
    }


def median(values):
    ordered = sorted(values)
    n = len(ordered)
    if n == 0:
        return 0.0
    mid = n // 2
    if n % 2:
        return ordered[mid]
    return round((ordered[mid - 1] + ordered[mid]) / 2, 2)


def aggregate(runs):
    """Median across repetitions, plus the spread, so noise is visible."""
    rps = [r["rps"] for r in runs]
    return {
        "label": runs[0]["label"],
        "concurrency": runs[0]["concurrency"],
        "repetitions": len(runs),
        "rps_median": median(rps),
        "rps_min": min(rps),
        "rps_max": max(rps),
        "p50_ms": median([r["latency_ms"]["p50"] for r in runs]),
        "p95_ms": median([r["latency_ms"]["p95"] for r in runs]),
        "p99_ms": median([r["latency_ms"]["p99"] for r in runs]),
        "errors_total": sum(r["errors"] for r in runs),
        "requests_total": sum(r["requests"] for r in runs),
        "non_200_total": sum(r["non_200"] for r in runs),
    }


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--out", default=str(ROOT / "benchmarks" / "results.json"))
    parser.add_argument("--duration", type=float, default=10.0)
    parser.add_argument("--warmup", type=float, default=2.0)
    parser.add_argument("--repeat", type=int, default=3)
    parser.add_argument(
        "--concurrency",
        default="1,10,50,100,200,400",
        help="Comma-separated concurrency levels",
    )
    args = parser.parse_args()
    levels = [int(x) for x in args.concurrency.split(",")]

    origin_port = free_port()
    proxy_port = free_port()
    log_dir = ROOT / "benchmarks" / "_logs"
    log_dir.mkdir(parents=True, exist_ok=True)

    report = {
        "environment": environment(),
        "config": {
            "duration_s": args.duration,
            "warmup_s": args.warmup,
            "repetitions": args.repeat,
            "concurrency_levels": levels,
            "body_bytes": 1024,
        },
        "runs": [],
        "summary": [],
    }

    def measure(label, **case):
        """Repeat a case and record both raw runs and the aggregate."""
        repeats = []
        for _ in range(args.repeat):
            result = run_case(label=label, duration=args.duration,
                              warmup=args.warmup, **case)
            report["runs"].append(result)
            repeats.append(result)
        agg = aggregate(repeats)
        report["summary"].append(agg)
        print(
            f"  c={agg['concurrency']:<4} rps={agg['rps_median']:<8} "
            f"(min {agg['rps_min']} max {agg['rps_max']})  "
            f"p50={agg['p50_ms']}ms p95={agg['p95_ms']}ms p99={agg['p99_ms']}ms "
            f"err={agg['errors_total']}",
            flush=True,
        )
        return agg

    origin_cmd = [
        sys.executable, "benchmarks/origin_server.py",
        "--port", str(origin_port),
    ]

    with Process(origin_cmd, "origin"):
        if not wait_for_port(origin_port):
            raise SystemExit("origin failed to start")

        # Control: the generator's own ceiling, with no proxy involved.
        print("== control: origin direct (load generator ceiling) ==", flush=True)
        for level in levels:
            measure(
                "origin-direct",
                origin_host="127.0.0.1", origin_port=origin_port,
                concurrency=level, direct=True,
            )

        for mode in ("threaded", "async"):
            print(f"== proxy: {mode} ==", flush=True)
            proxy_cmd = [
                sys.executable, "-m", "proxy_server.main",
                "--host", "127.0.0.1",
                "--port", str(proxy_port),
                "--mode", mode,
                "--metrics", str(log_dir / f"metrics-{mode}.csv"),
                "--access-log", str(log_dir / f"access-{mode}.log"),
                "--error-log", str(log_dir / f"error-{mode}.log"),
                # Measure the proxy, not its policies.
                "--rate-limit-requests", "0",
                "--no-adaptive-rate-limit",
                "--no-cache",
                "--metrics-port", "0",
                "--health-port", "0",
            ]
            with Process(proxy_cmd, mode):
                if not wait_for_port(proxy_port):
                    raise SystemExit(f"{mode} proxy failed to start")
                for level in levels:
                    measure(
                        f"proxy-{mode}",
                        proxy_host="127.0.0.1", proxy_port=proxy_port,
                        origin_host="127.0.0.1", origin_port=origin_port,
                        concurrency=level,
                    )
            # Ports linger in TIME_WAIT; give the OS a moment.
            time.sleep(2)

        # --- cache hit vs miss ------------------------------------
        # Identical URL and origin in both arms. The only variable is
        # whether the cache is enabled, which isolates its effect.
        redis_port = free_port()
        redis_cmd = [
            sys.executable, "benchmarks/mini_redis.py", "--port", str(redis_port)
        ]
        cache_levels = [level for level in levels if level in (10, 50, 100)] or [50]

        with Process(redis_cmd, "mini-redis"):
            if not wait_for_port(redis_port):
                raise SystemExit("mini-redis failed to start")

            for arm, extra in (
                ("cache-hit", ["--redis-url", f"redis://127.0.0.1:{redis_port}/0"]),
                ("cache-disabled", ["--no-cache"]),
            ):
                print(f"== async proxy: {arm} ==", flush=True)
                cmd = [
                    sys.executable, "-m", "proxy_server.main",
                    "--host", "127.0.0.1", "--port", str(proxy_port),
                    "--mode", "async",
                    "--metrics", str(log_dir / f"metrics-{arm}.csv"),
                    "--access-log", str(log_dir / f"access-{arm}.log"),
                    "--error-log", str(log_dir / f"error-{arm}.log"),
                    "--rate-limit-requests", "0",
                    "--no-adaptive-rate-limit",
                    "--metrics-port", "0", "--health-port", "0",
                ] + extra
                with Process(cmd, arm):
                    if not wait_for_port(proxy_port):
                        raise SystemExit(f"{arm} proxy failed to start")
                    for level in cache_levels:
                        measure(
                            arm,
                            proxy_host="127.0.0.1", proxy_port=proxy_port,
                            origin_host="127.0.0.1", origin_port=origin_port,
                            concurrency=level, path="/cacheable",
                        )
                time.sleep(2)

    Path(args.out).write_text(json.dumps(report, indent=2))
    print(f"\nwrote {args.out}")


if __name__ == "__main__":
    main()
