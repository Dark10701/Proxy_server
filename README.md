# Multi-Threaded HTTP Proxy Server with Content Filtering and Performance Monitoring

## Project Overview
This project implements a raw TCP, multi-threaded HTTP/1.1 proxy server in Python using only standard libraries. It accepts browser connections, parses HTTP requests manually, forwards them to destination servers, and relays responses back to the client. Content filtering is enforced via a domain blacklist and URL keyword checks, while performance metrics are recorded to a CSV file for analysis.

## Architecture
**Key modules**:
- `main.py`: CLI entry point and configuration parsing.
- `server.py`: TCP listener that spawns a thread per client connection.
- `client_handler.py`: Parses requests, applies filtering, forwards data, and logs metrics.
- `http_parser.py`: Minimal HTTP parsing and request reconstruction utilities.
- `filter_engine.py`: Domain and keyword filtering logic.
- `metrics.py`: CSV metrics logger for latency and bandwidth tracking.
- `logger.py`: Access/error logging configuration.

**Data flow**:
1. Client connects to the proxy listener.
2. Request is parsed from raw bytes (request line + headers + body).
3. Destination host/port is derived from the absolute URL or `Host` header.
4. Filter engine blocks or forwards the request.
5. Response is relayed back while collecting timing and size metrics.

## How to Run
1. Ensure Python 3 is installed.
2. Start the proxy server from the `proxy_server/` directory:
   ```bash
   python main.py --host 0.0.0.0 --port 8080
   ```
3. Configure your browser or HTTP client to use the proxy at the chosen host/port.

## How to Test
- Use `curl` with a proxy:
  ```bash
  curl -x http://127.0.0.1:8080 http://example.org
  ```
- Add domains to `config/blocked_domains.txt` and confirm a `403 Forbidden` response.
- Observe `logs/access.log` and `logs/metrics.csv` for logging and metrics.

## Limitations
- HTTPS CONNECT tunneling is not implemented; only standard HTTP proxying is supported.
- Chunked transfer encoding is not explicitly parsed for requests (responses are streamed).
- The proxy uses `Connection: close` for simplicity, which disables keep-alive.

## Notes
This code is intentionally verbose and heavily commented for educational clarity, emphasizing raw socket use and HTTP parsing logic.
