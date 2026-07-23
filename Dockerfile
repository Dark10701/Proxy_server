# syntax=docker/dockerfile:1
FROM python:3.11-slim AS base

# Unbuffered so container logs appear as they happen rather than when a
# buffer fills; no .pyc since the layer is immutable anyway.
ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1

WORKDIR /app

# Dependencies first: this layer is cached unless requirements change,
# so editing source does not reinstall the world.
COPY requirements.txt ./
RUN pip install --no-cache-dir -r requirements.txt

COPY proxy_server/ ./proxy_server/
COPY dashboard/ ./dashboard/

# Run unprivileged. The proxy binds 8080/8081/9100, all above 1024, so
# it never needs root.
RUN useradd --create-home --shell /usr/sbin/nologin proxy \
    && mkdir -p /app/logs \
    && chown -R proxy:proxy /app
USER proxy

# 8080 proxy, 8081 health, 9100 prometheus
EXPOSE 8080 8081 9100

# Probes the readiness endpoint, so a draining instance reports
# unhealthy and stops receiving traffic before it stops listening.
HEALTHCHECK --interval=10s --timeout=3s --start-period=5s --retries=3 \
    CMD python -c "import urllib.request,sys; sys.exit(0 if urllib.request.urlopen('http://127.0.0.1:8081/ready', timeout=2).status==200 else 1)"

ENTRYPOINT ["python", "-m", "proxy_server.main"]
CMD ["--host", "0.0.0.0", "--port", "8080", "--metrics-port", "9100", "--metrics", "/app/logs/metrics.csv", "--access-log", "/app/logs/access.log", "--error-log", "/app/logs/error.log"]
