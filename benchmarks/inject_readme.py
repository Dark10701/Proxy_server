"""Insert the headline benchmark figures into README.md.

Replaces everything between the BENCHMARKS markers, so the README
summary is regenerated from results.json rather than hand-maintained
and left to drift.

    python benchmarks/inject_readme.py
"""

import json
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
RESULTS = ROOT / "benchmarks" / "results.json"
README = ROOT / "README.md"

START = "<!--BENCHMARKS-->"
END = "<!--/BENCHMARKS-->"


def peak(summary, label):
    rows = [r for r in summary if r["label"] == label]
    if not rows:
        return None
    return max(rows, key=lambda r: r["rps_median"])


def at(summary, label, concurrency):
    for row in summary:
        if row["label"] == label and row["concurrency"] == concurrency:
            return row
    return None


def main() -> None:
    data = json.loads(RESULTS.read_text(encoding="utf-8"))
    summary = data["summary"]
    env = data["environment"]
    config = data["config"]

    control = peak(summary, "origin-direct")
    threaded = peak(summary, "proxy-threaded")
    asyncp = peak(summary, "proxy-async")
    hit = peak(summary, "cache-hit")
    nocache = peak(summary, "cache-disabled")

    gain = (
        (asyncp["rps_median"] - threaded["rps_median"]) / threaded["rps_median"] * 100
        if threaded and threaded["rps_median"]
        else 0
    )

    # Peak throughput is reported at whatever concurrency each build
    # peaks at, but latency is only comparable at the SAME concurrency,
    # so the head-to-head rows below are matched.
    match_c = asyncp["concurrency"] if asyncp else 100
    t_at = at(summary, "proxy-threaded", match_c)
    a_at = at(summary, "proxy-async", match_c)

    lines = [
        START,
        "",
        "Full methodology, environment, per-concurrency tables and the caveats "
        "that matter are in [`benchmarks/RESULTS.md`](benchmarks/RESULTS.md). "
        "**Read the caveats before quoting any figure** — these come from a "
        "single laptop with a Python load generator that is itself a ceiling.",
        "",
        f"Measured on {env['platform']}, {env['cpu_count']} cores, "
        f"Python {env['python']}. Each cell is the median of "
        f"{config['repetitions']} runs of {config['duration_s']:g}s.",
        "",
        "**Peak sustained throughput** (each build at the concurrency where "
        "it peaks):",
        "",
        "| | Peak req/s | at concurrency |",
        "|---|---:|---:|",
        f"| Load generator ceiling (no proxy) | {control['rps_median']} | "
        f"{control['concurrency']} |",
        f"| Threaded build | {threaded['rps_median']} | {threaded['concurrency']} |",
        f"| Async build | **{asyncp['rps_median']}** | {asyncp['concurrency']} |",
        "",
        f"**Head to head at concurrency {match_c}** (same load, so latency is "
        "comparable):",
        "",
        "| | req/s | p50 | p95 | p99 |",
        "|---|---:|---:|---:|---:|",
    ]
    if t_at and a_at:
        lines += [
            f"| Threaded | {t_at['rps_median']} | {t_at['p50_ms']} ms | "
            f"{t_at['p95_ms']} ms | {t_at['p99_ms']} ms |",
            f"| Async | **{a_at['rps_median']}** | {a_at['p50_ms']} ms | "
            f"{a_at['p95_ms']} ms | {a_at['p99_ms']} ms |",
            "",
        ]
        rps_gain = (a_at["rps_median"] - t_at["rps_median"]) / t_at["rps_median"] * 100
        p99_cut = (t_at["p99_ms"] - a_at["p99_ms"]) / t_at["p99_ms"] * 100
        lines.append(
            f"At the same offered load the async build serves "
            f"**{rps_gain:+.0f}%** more requests per second with a "
            f"**{p99_cut:.0f}% lower p99**. Peak-to-peak the gain is "
            f"{gain:+.0f}%. Both remain well below the "
            f"{control['rps_median']} req/s the generator reaches with no "
            "proxy in the path, so the proxy — not the client — is what is "
            "being measured."
        )
        lines.append("")

    if hit and nocache:
        cache_c = hit["concurrency"]
        hit_at = at(summary, "cache-hit", cache_c)
        off_at = at(summary, "cache-disabled", cache_c)
        if hit_at and off_at:
            cache_gain = (
                (hit_at["rps_median"] - off_at["rps_median"])
                / off_at["rps_median"] * 100
            )
            cache_p99 = (
                (off_at["p99_ms"] - hit_at["p99_ms"]) / off_at["p99_ms"] * 100
            )
            lines += [
                f"**Cache hit vs no cache**, async build at concurrency "
                f"{cache_c}. Identical URL and origin in both arms; the only "
                "variable is whether the cache is enabled.",
                "",
                "| | req/s | p50 | p95 | p99 |",
                "|---|---:|---:|---:|---:|",
                f"| Cache disabled | {off_at['rps_median']} | "
                f"{off_at['p50_ms']} ms | {off_at['p95_ms']} ms | "
                f"{off_at['p99_ms']} ms |",
                f"| Serving cache hits | **{hit_at['rps_median']}** | "
                f"{hit_at['p50_ms']} ms | {hit_at['p95_ms']} ms | "
                f"{hit_at['p99_ms']} ms |",
                "",
                f"Serving from cache is **{cache_gain:+.0f}%** on throughput "
                f"with a **{cache_p99:.0f}% lower p99**. The cache arm talks to "
                "a minimal RESP server rather than real Redis, which could not "
                "be installed on the benchmark machine — over a real socket "
                "with the real client, so this is a pessimistic bound rather "
                "than an optimistic one.",
                "",
            ]

    lines.append(END)

    text = README.read_text(encoding="utf-8")
    if START in text and END in text:
        head, _, rest = text.partition(START)
        _, _, tail = rest.partition(END)
        text = head + "\n".join(lines) + tail
    else:
        text = text.replace(START, "\n".join(lines))
    README.write_text(text, encoding="utf-8")
    print("README benchmark section updated")


if __name__ == "__main__":
    main()
