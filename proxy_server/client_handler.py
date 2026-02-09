import socket
import time

from http_parser import parse_http_request
from metrics import MetricsLogger

METRICS_LOGGER = MetricsLogger("logs/metrics.csv")

def handle_client(client_socket):
    try:
        request = client_socket.recv(8192).decode(errors="ignore")
        if not request:
            client_socket.close()
            return

        method, url, host, port = parse_http_request(request)

        if not host:
            client_socket.close()
            return

        start_time = time.monotonic()
        response_bytes = 0

        # CONNECT TO REAL DESTINATION (use Host header-derived target only)
        upstream_host = host
        upstream_port = port
        server_socket = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        server_socket.connect((upstream_host, upstream_port))

        server_socket.sendall(request.encode())

        while True:
            response = server_socket.recv(8192)
            if not response:
                break
            response_bytes += len(response)
            client_socket.sendall(response)

        # Metrics recorded after final response byte is sent to the client.
        latency_ms = int((time.monotonic() - start_time) * 1000)
        client_ip = ""
        try:
            client_ip = client_socket.getpeername()[0]
        except OSError:
            client_ip = ""
        METRICS_LOGGER.log(
            client_ip=client_ip,
            host=host,
            latency_ms=latency_ms,
            response_bytes=response_bytes,
        )

        server_socket.close()
        client_socket.close()
    except Exception as e:
        print("Upstream error:", e)
        client_socket.close()
