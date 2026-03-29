# CN Project: Multi-Threaded Proxy Server with Filtering and Monitoring

This repository contains a **Computer Networks (CN) project** that implements a Python-based proxy server using low-level sockets, request parsing, filtering rules, and traffic metrics collection.

It is designed to demonstrate core CN ideas in one end-to-end system:
- TCP socket programming
- HTTP request parsing/forwarding
- Application-layer filtering policies
- Concurrent client handling with threads
- Basic performance monitoring and dashboarding

---

## 1) What this project does

At a high level, the proxy acts as a middle layer between clients (browser/curl) and upstream servers.

### Main capabilities
1. **HTTP proxying**
   - Accepts client TCP connections.
   - Parses HTTP requests and forwards them to target servers.
   - Relays upstream responses back to the client.

2. **HTTPS tunneling (CONNECT)**
   - Supports `CONNECT host:port` flow for HTTPS tunnels.
   - Creates a bidirectional tunnel between client and upstream server.

3. **Content filtering**
   - Blocks requests to domains listed in `blocked_domains.txt`.
   - Blocks URLs containing configured keywords (e.g., `adult`, `malware`, `phishing`).

4. **Metrics and logging**
   - Logs per-request metrics to CSV (`latency`, bytes, blocked flag, etc.).
   - Writes access/error logs.
   - Includes a Flask dashboard app that reads metrics and presents live charts.

---

## 2) Architecture (CN-focused)

## Logical architecture

```text
Client (Browser/curl)
        |
        v
+-----------------------+
| Proxy Listener        |  (server.py)
| - TCP accept loop     |
| - thread per client   |
+-----------------------+
        |
        v
+-----------------------+
| Client Handler        |  (client_handler.py)
| - parse request       |
| - apply filtering     |
| - forward/tunnel      |
| - collect metrics     |
+-----------------------+
   |                 |
   | allowed         | blocked
   v                 v
Upstream Server    403 Response
   |
   v
Response relay back to client

Metrics/Logs side path:
Client Handler -> metrics.py (CSV) + logger.py (logs)
                               |
                               v
                     dashboard/app.py (Flask + SocketIO)
```

### Component-level responsibilities

- **`proxy_server/server.py`**
  - Opens socket, binds host/port, listens for connections.
  - Spawns one handler thread per accepted client.

- **`proxy_server/client_handler.py`**
  - Reads full request bytes from client.
  - Handles regular HTTP forwarding and HTTPS `CONNECT` tunneling.
  - Invokes filter engine before contacting upstream.
  - Logs request metrics after each transaction.

- **`proxy_server/http_parser.py`**
  - Parses request line/headers/body from raw bytes.
  - Extracts target host/port/path from URL + Host header.
  - Rebuilds forward request in origin-form.

- **`proxy_server/filter_engine.py`**
  - Loads blocked domains from file.
  - Checks domain and keyword policy.

- **`proxy_server/metrics.py`**
  - Appends metrics rows to CSV in a thread-safe manner.

- **`proxy_server/logger.py`**
  - Configures access/error logging utilities.

- **`dashboard/app.py`**
  - Reads metrics CSV, computes aggregated stats.
  - Serves dashboard UI and pushes updates via SocketIO.

---

## 3) Request lifecycle (How it works)

### A) Standard HTTP request
1. Client sends request to proxy (e.g., via `curl -x`).
2. Proxy parses request line and headers.
3. Target host/port/path are derived.
4. Filter checks are applied.
5. If allowed, proxy opens upstream TCP connection.
6. Request is forwarded upstream.
7. Response bytes are streamed back to client.
8. Metrics are written to CSV; access logs are updated.

### B) HTTPS CONNECT request
1. Client sends `CONNECT host:443 HTTP/1.1`.
2. Proxy validates and filters target.
3. If allowed, proxy establishes upstream TCP connection.
4. Proxy returns `200 Connection Established`.
5. Proxy tunnels bytes in both directions until closure.
6. Tunnel stats are logged as one request record.

### C) Blocked request behavior
- If a domain/keyword matches policy:
  - Proxy responds with `403 Forbidden`.
  - Marks request as blocked in metrics.

---

## 4) Repository structure

```text
.
├── README.md
├── requirements.txt
├── proxy_server/
│   ├── main.py
│   ├── server.py
│   ├── client_handler.py
│   ├── http_parser.py
│   ├── filter_engine.py
│   ├── metrics.py
│   ├── metrics_store.py
│   ├── logger.py
│   ├── config/
│   │   └── blocked_domains.txt
│   └── README.md
└── dashboard/
    ├── app.py
    └── templates/
        └── index.html
```

---

## 5) Setup and run

## Prerequisites
- Python 3.9+
- pip

## Install dependencies
```bash
pip install -r requirements.txt
```

> Note: `dashboard/app.py` imports `flask_socketio`; if missing in your environment, install it with `pip install flask-socketio`.

## Run proxy server
From `proxy_server/`:
```bash
python main.py --host 0.0.0.0 --port 8080
```

## Run dashboard (optional)
From repository root:
```bash
python dashboard/app.py
```
Then open `http://localhost:5000`.

---

## 6) Quick test commands

### Verify proxy forwarding
```bash
curl -x http://127.0.0.1:8080 http://example.org
```

### Verify blocking
1. Add a domain to `proxy_server/config/blocked_domains.txt`.
2. Retry through proxy:
```bash
curl -i -x http://127.0.0.1:8080 http://blocked-domain.example/
```
Expected: `HTTP/1.1 403 Forbidden`

### Verify metrics/log files
- Check access/error logs (configured paths)
- Check generated metrics CSV rows for latency/bytes/blocked

---

## 7) CN concepts demonstrated

- **Application-layer protocol handling:** HTTP parsing, header management, CONNECT tunneling.
- **Transport-layer behavior:** TCP connection establishment, full-duplex relay for tunnels.
- **Concurrency model:** Multi-threaded server (thread per connection).
- **Traffic analysis:** Throughput, latency, request patterns, top domains.
- **Policy enforcement:** Domain and keyword filtering at proxy layer.

---

## 8) Current implementation notes

This repo currently mixes two monitoring approaches:
1. CSV-based logging (`metrics.py`) used by the dashboard reader.
2. In-memory metrics scaffolding (`metrics_store.py`) referenced by `main.py`.

If you use this for submission/demo, keep your run path consistent (CSV logging + dashboard flow is present and concrete in the current files).

---

## 9) Suggested demo flow for viva/report

1. Start proxy.
2. Send allowed HTTP request (show success).
3. Send blocked request (show 403).
4. Open dashboard and explain latency/bandwidth/top-domain charts.
5. Explain CONNECT tunnel handling for HTTPS.
6. Discuss design trade-offs (thread model, `Connection: close`, basic parser constraints).

---

## 10) Known limitations

- Thread-per-connection model may not scale for very high concurrency.
- Parser is intentionally minimal and not a full RFC-complete implementation.
- Some wiring in `main.py` references modules/attributes not present in this snapshot, so minor integration cleanup may be needed before final production-style run.

