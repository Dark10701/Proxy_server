"""Minimal HTTP parsing utilities for a raw TCP proxy."""


def parse_http_request(request: str):
    lines = request.split("\r\n")
    method, url, protocol = lines[0].split()

    host = None
    port = 80

    for line in lines:
        if line.lower().startswith("host:"):
            host_value = line.split(":", 1)[1].strip()
            if ":" in host_value:
                host, port = host_value.split(":")
                port = int(port)
            else:
                host = host_value

    return method, url, host, port
