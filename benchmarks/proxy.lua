-- wrk script for benchmarking a forward proxy.
--
-- wrk connects to the proxy but must send an ABSOLUTE-form request line
-- ("GET http://origin/path HTTP/1.1"), which is what a forward proxy
-- expects and what wrk will not produce on its own. Point wrk at the
-- proxy and set ORIGIN to the upstream:
--
--   ORIGIN=http://127.0.0.1:8000 wrk -t4 -c100 -d30s \
--     -s benchmarks/proxy.lua http://127.0.0.1:8080
--
-- PROXY_PATH selects the origin path (default "/").

local origin = os.getenv("ORIGIN") or "http://127.0.0.1:8000"
local path = os.getenv("PROXY_PATH") or "/"
local host = origin:gsub("^https?://", "")

request = function()
   -- Connection: close matches how the proxy behaves. It closes the
   -- client socket after each request, so measuring with keep-alive
   -- would measure a code path that does not exist.
   return wrk.format("GET", origin .. path, {
      ["Host"] = host,
      ["Connection"] = "close",
   })
end

done = function(summary, latency, requests)
   io.write("----------------------------------------\n")
   io.write(string.format("requests    : %d\n", summary.requests))
   io.write(string.format("duration    : %.2fs\n", summary.duration / 1e6))
   io.write(string.format("rps         : %.1f\n",
            summary.requests / (summary.duration / 1e6)))
   io.write(string.format("socket errs : connect %d, read %d, write %d, timeout %d\n",
            summary.errors.connect, summary.errors.read,
            summary.errors.write, summary.errors.timeout))
   io.write(string.format("non-2xx/3xx : %d\n", summary.errors.status))
   io.write(string.format("p50         : %.2fms\n", latency:percentile(50) / 1000))
   io.write(string.format("p95         : %.2fms\n", latency:percentile(95) / 1000))
   io.write(string.format("p99         : %.2fms\n", latency:percentile(99) / 1000))
   io.write(string.format("max         : %.2fms\n", latency.max / 1000))
   io.write("----------------------------------------\n")
end
