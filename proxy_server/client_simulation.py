from __future__ import annotations
import random
import statistics
import threading
import time
from dataclasses import dataclass, field
import requests

PROXY_URL = "http://127.0.0.1:8080"
DEFAULT_CLIENTS = 12
REQUESTS_PER_CLIENT_RANGE = (2, 5)
SLEEP_BETWEEN_REQUESTS_RANGE = (0.2, 1.5)
THREAD_START_STAGGER_RANGE = (0.05, 0.3)
REQUEST_TIMEOUT_SECONDS = 8

TARGET_URLS = [
    "http://example.com/",
    "http://neverssl.com/",
    "http://httpbin.org/get",
    "http://httpbin.org/uuid",
    "http://iana.org/",
]


@dataclass
class GlobalMetrics:
    total_requests: int = 0
    successful_responses: int = 0
    blocked_responses: int = 0
    errors: int = 0
    latencies_ms: list[float] = field(default_factory=list)


metrics = GlobalMetrics()
metrics_lock = threading.Lock()


def record_result(status_code: int | None, latency_ms: float | None, errored: bool) -> None:
    with metrics_lock:
        metrics.total_requests += 1
        if errored:
            metrics.errors += 1
            return

        if status_code == 403:
            metrics.blocked_responses += 1
        elif status_code is not None and 200 <= status_code < 400:
            metrics.successful_responses += 1

        if latency_ms is not None:
            metrics.latencies_ms.append(latency_ms)


def client_worker(client_id: int, proxies: dict[str, str]) -> None:
    session = requests.Session()
    session.proxies.update(proxies)

    request_count = random.randint(*REQUESTS_PER_CLIENT_RANGE)
    for req_num in range(1, request_count + 1):
        target_url = random.choice(TARGET_URLS)
        started = time.perf_counter()

        try:
            response = session.get(target_url, timeout=REQUEST_TIMEOUT_SECONDS)
            latency_ms = (time.perf_counter() - started) * 1000
            record_result(response.status_code, latency_ms, errored=False)
            print(
                f"[client={client_id:02d} req={req_num}] "
                f"{response.status_code} {target_url} {latency_ms:.1f}ms"
            )
        except requests.RequestException as exc:
            record_result(None, None, errored=True)
            print(f"[client={client_id:02d} req={req_num}] ERROR {target_url} ({exc})")

        time.sleep(random.uniform(*SLEEP_BETWEEN_REQUESTS_RANGE))

    session.close()


def print_summary(duration_seconds: float) -> None:
    with metrics_lock:
        latencies = list(metrics.latencies_ms)
        total = metrics.total_requests
        success = metrics.successful_responses
        blocked = metrics.blocked_responses
        errors = metrics.errors

    avg_latency = statistics.mean(latencies) if latencies else 0.0
    p95_latency = (
        statistics.quantiles(latencies, n=20)[18]
        if len(latencies) >= 20
        else max(latencies, default=0.0)
    )

    print("\n" + "=" * 56)
    print("Proxy Client Simulation Summary")
    print("=" * 56)
    print(f"Duration:              {duration_seconds:.2f}s")
    print(f"Total requests:        {total}")
    print(f"Successful responses:  {success}")
    print(f"Blocked responses 403: {blocked}")
    print(f"Errors:                {errors}")
    print(f"Average latency:       {avg_latency:.1f} ms")
    print(f"P95 latency:           {p95_latency:.1f} ms")
    print("=" * 56)


def run_simulation(client_count: int = DEFAULT_CLIENTS) -> None:
    proxies = {"http": PROXY_URL, "https": PROXY_URL}
    threads: list[threading.Thread] = []
    started = time.perf_counter()

    for client_id in range(1, client_count + 1):
        thread = threading.Thread(target=client_worker, args=(client_id, proxies), daemon=False)
        thread.start()
        threads.append(thread)
        time.sleep(random.uniform(*THREAD_START_STAGGER_RANGE))

    for thread in threads:
        thread.join()

    duration = time.perf_counter() - started
    print_summary(duration)


if __name__ == "__main__":
    run_simulation()
