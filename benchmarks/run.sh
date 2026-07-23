#!/usr/bin/env bash
#
# Benchmark the threaded and async builds under identical conditions.
#
# Uses wrk when it is available, and falls back to the bundled Python
# load generator when it is not. The results in RESULTS.md were produced
# by the fallback path, because wrk is not available on Windows; the wrk
# path is here so the same comparison is reproducible on a Linux box
# where wrk gives a more trustworthy client.
#
#   ./benchmarks/run.sh                 # full sweep
#   DURATION=30 CONNS="50 200" ./benchmarks/run.sh
#
set -euo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$ROOT"

DURATION="${DURATION:-10}"
WARMUP="${WARMUP:-2}"
REPEAT="${REPEAT:-3}"
CONNS="${CONNS:-1 10 50 100 200 400}"
THREADS="${THREADS:-4}"
ORIGIN_PORT="${ORIGIN_PORT:-8000}"
PROXY_PORT="${PROXY_PORT:-8080}"
PYTHON="${PYTHON:-python3}"
OUT_DIR="$ROOT/benchmarks/_out"

mkdir -p "$OUT_DIR"

cleanup() {
  [[ -n "${ORIGIN_PID:-}" ]] && kill "$ORIGIN_PID" 2>/dev/null || true
  [[ -n "${PROXY_PID:-}" ]] && kill "$PROXY_PID" 2>/dev/null || true
}
trap cleanup EXIT

wait_for_port() {
  local port="$1" tries=0
  until "$PYTHON" -c "import socket,sys; s=socket.socket(); s.settimeout(0.5); sys.exit(0 if s.connect_ex(('127.0.0.1',$port))==0 else 1)" 2>/dev/null; do
    tries=$((tries + 1))
    if [[ $tries -gt 100 ]]; then
      echo "port $port never opened" >&2
      exit 1
    fi
    sleep 0.2
  done
}

start_proxy() {
  local mode="$1"; shift
  "$PYTHON" -m proxy_server.main \
    --host 127.0.0.1 --port "$PROXY_PORT" --mode "$mode" \
    --metrics "$OUT_DIR/metrics-$mode.csv" \
    --access-log "$OUT_DIR/access-$mode.log" \
    --error-log "$OUT_DIR/error-$mode.log" \
    --rate-limit-requests 0 --no-adaptive-rate-limit \
    --metrics-port 0 --health-port 0 \
    "$@" >/dev/null 2>&1 &
  PROXY_PID=$!
  wait_for_port "$PROXY_PORT"
}

stop_proxy() {
  [[ -n "${PROXY_PID:-}" ]] && kill "$PROXY_PID" 2>/dev/null || true
  PROXY_PID=""
  sleep 2   # let the port leave TIME_WAIT
}

# ---------------------------------------------------------------------
# Rate limiting is disabled above on purpose. The shipped default is 200
# requests per client+host per minute, so a benchmark left at defaults
# would measure the rate limiter rather than the proxy.
# ---------------------------------------------------------------------

if command -v wrk >/dev/null 2>&1; then
  echo "wrk found: $(wrk --version 2>&1 | head -1)"

  "$PYTHON" benchmarks/origin_server.py --port "$ORIGIN_PORT" >/dev/null 2>&1 &
  ORIGIN_PID=$!
  wait_for_port "$ORIGIN_PORT"

  for mode in threaded async; do
    start_proxy "$mode"
    for c in $CONNS; do
      for rep in $(seq 1 "$REPEAT"); do
        echo "=== $mode c=$c rep=$rep ==="
        ORIGIN="http://127.0.0.1:$ORIGIN_PORT" \
          wrk -t"$THREADS" -c"$c" -d"${DURATION}s" --latency \
              -s benchmarks/proxy.lua "http://127.0.0.1:$PROXY_PORT" \
          | tee "$OUT_DIR/wrk-$mode-c$c-r$rep.txt"
      done
    done
    stop_proxy
  done

  echo "raw wrk output in $OUT_DIR"
else
  echo "wrk not found; using the bundled Python load generator."
  echo "Numbers will be client-limited -- see RESULTS.md methodology."
  exec "$PYTHON" benchmarks/run_bench.py \
    --duration "$DURATION" \
    --warmup "$WARMUP" \
    --repeat "$REPEAT" \
    --concurrency "$(echo $CONNS | tr ' ' ',')" \
    --out "$ROOT/benchmarks/results.json"
fi
