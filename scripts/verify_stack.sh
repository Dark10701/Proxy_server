#!/usr/bin/env bash
#
# Verify the docker compose stack end to end.
#
# Checks the claims the README makes about the distributed setup rather
# than asserting them:
#
#   1. health-checked containers reach a healthy state
#   2. filtering works through the nginx entrypoint
#   3. plain HTTP forwarding works through nginx
#   4. CONNECT tunnelling survives the layer 4 load balancer
#   5. requests are distributed across all instances
#   6. the Redis cache is genuinely SHARED -- an instance serves a hit
#      for content it never fetched itself
#   7. Prometheus has every instance up
#   8. Grafana has the dashboard and datasource provisioned
#   9. stopping an instance drops no traffic
#
# Usage:
#   docker compose --profile verify up -d --build
#   ./scripts/verify_stack.sh
#
set -uo pipefail

ENTRY="${ENTRY:-http://127.0.0.1:8080}"
PROM="${PROM:-http://127.0.0.1:9090}"
GRAFANA="${GRAFANA:-http://127.0.0.1:3000}"
GRAFANA_AUTH="${GRAFANA_AUTH:-admin:admin}"
ORIGIN="${ORIGIN:-http://cache-origin:8000/}"
PY="${PY:-python}"

pass=0
fail=0

ok()    { echo "  PASS  $1"; pass=$((pass + 1)); }
bad()   { echo "  FAIL  $1"; fail=$((fail + 1)); }
head1() { echo; echo "== $1 =="; }

code_for() {
  curl -s -o /dev/null -w '%{http_code}' --max-time 15 -x "$ENTRY" "$1"
}

head1 "1. container health"
# Only proxy1-3, redis and nginx declare a HEALTHCHECK. Grafana,
# Prometheus and cache-origin do not, so "Up" is all they can report and
# demanding "healthy" from them would be a false failure.
bad_health=""
for c in proxy1 proxy2 proxy3 \
         $(docker compose ps --format '{{.Name}}' | grep -E 'redis|nginx'); do
  st=$(docker inspect -f '{{.State.Health.Status}}' "$c" 2>/dev/null || echo missing)
  echo "    $c: $st"
  [ "$st" = "healthy" ] || bad_health="$bad_health $c=$st"
done
if [ -z "$bad_health" ]; then
  ok "all health-checked containers healthy"
else
  bad "not healthy:$bad_health"
fi

head1 "2. filtering through nginx"
c=$(code_for "http://example.com/")
[ "$c" = "403" ] && ok "blocked domain -> 403" || bad "blocked domain -> $c (want 403)"

head1 "3. plain HTTP forwarding through nginx"
c=$(code_for "http://httpforever.com/")
[ "$c" = "200" ] && ok "allowed origin -> 200" || bad "allowed origin -> $c (want 200)"

head1 "4. CONNECT tunnel through the layer 4 balancer"
c=$(code_for "https://example.org/")
[ "$c" = "200" ] && ok "https via CONNECT -> 200" || bad "https via CONNECT -> $c (want 200)"

head1 "5. load distribution"
for _ in $(seq 1 12); do curl -s -o /dev/null --max-time 15 -x "$ENTRY" "$ORIGIN"; done
served=0
for c in proxy1 proxy2 proxy3; do
  n=$(docker exec "$c" sh -c 'wc -l < /app/logs/metrics.csv' 2>/dev/null | tr -d '[:space:]')
  echo "    $c: ${n:-0} metrics rows"
  [ "${n:-0}" -gt 1 ] && served=$((served + 1))
done
[ "$served" -ge 2 ] && ok "$served/3 instances served traffic" \
                    || bad "only $served/3 instances served traffic"

head1 "6. shared cache across instances"
# A distinct URL per run, so a previous run's cache entry cannot mask a
# failure here.
uniq_url="${ORIGIN}?run=$$"
bodies=$(for _ in $(seq 1 8); do
           curl -s --max-time 15 -x "$ENTRY" "$uniq_url"; echo
         done | sort -u | grep -c .)
if [ "$bodies" -eq 1 ]; then
  ok "8 requests through the balancer, origin contacted exactly once"
else
  bad "origin produced $bodies distinct responses; cache is not shared"
fi

# Parsed with Python: grepping Prometheus JSON in shell is too fragile.
cache_report=$(curl -s --get "$PROM/api/v1/query" \
  --data-urlencode 'query=proxy_cache_events_total' \
  | "$PY" -c '
import json, sys
rows = {}
for m in json.load(sys.stdin)["data"]["result"]:
    inst = m["metric"].get("instance", "?")
    rows.setdefault(inst, {})[m["metric"].get("result")] = float(m["value"][1])
shared = 0
for inst in sorted(rows):
    d = rows[inst]
    hit, store = d.get("hit", 0.0), d.get("store", 0.0)
    print("    %s: hits=%d stores=%d" % (inst, hit, store))
    if hit > 0 and store == 0:
        shared += 1
print("SHARED=%d" % shared)
')
echo "$cache_report" | grep -v '^SHARED='
shared=$(echo "$cache_report" | sed -n 's/^SHARED=//p')
[ "${shared:-0}" -ge 1 ] \
  && ok "${shared} instance(s) served hits for content they never stored" \
  || bad "no instance served a hit for content it did not fetch"

head1 "7. Prometheus targets"
up=$(curl -s --get "$PROM/api/v1/query" --data-urlencode 'query=up{job="proxy"}' \
     | "$PY" -c 'import json,sys; print(sum(1 for m in json.load(sys.stdin)["data"]["result"] if m["value"][1]=="1"))')
[ "${up:-0}" -eq 3 ] && ok "3/3 proxy targets up" || bad "${up:-0}/3 proxy targets up"

head1 "8. Grafana provisioning"
curl -s -u "$GRAFANA_AUTH" "$GRAFANA/api/search?type=dash-db" | grep -q "HTTP Proxy" \
  && ok "dashboard provisioned" || bad "dashboard missing"
curl -s -u "$GRAFANA_AUTH" "$GRAFANA/api/datasources" | grep -q "prometheus" \
  && ok "datasource provisioned" || bad "datasource missing"

head1 "9. draining: no traffic dropped with an instance down"
docker compose stop proxy2 >/dev/null 2>&1
drops=0
for _ in $(seq 1 20); do
  [ "$(code_for "$ORIGIN")" = "200" ] || drops=$((drops + 1))
done
docker compose start proxy2 >/dev/null 2>&1
[ "$drops" -eq 0 ] && ok "20/20 succeeded with proxy2 stopped" \
                   || bad "$drops/20 failed with proxy2 stopped"

echo
echo "========================================"
echo "  passed: $pass   failed: $fail"
echo "========================================"
[ "$fail" -eq 0 ] || exit 1
