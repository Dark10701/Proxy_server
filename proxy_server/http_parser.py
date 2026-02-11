"""Minimal HTTP parsing utilities for a raw TCP proxy."""

from typing import Dict, Tuple
from urllib.parse import urlsplit


def parse_http_request(request_bytes: bytes) -> Tuple[Tuple[str, str, str], Dict[str, str], bytes]:
    """Parse the request line and headers from raw bytes."""
    try:
        header_part, _, body = request_bytes.partition(b"\r\n\r\n")
        lines = header_part.decode("iso-8859-1", errors="replace").split("\r\n")
        request_line = lines[0].split(" ")
        if len(request_line) != 3:
            return (), {}, b""
        method, url, version = request_line
        headers = {}
        for line in lines[1:]:
            if ":" in line:
                key, value = line.split(":", 1)
                headers[key.strip()] = value.strip()
        return (method, url, version), headers, body
    except Exception:
        return (), {}, b""


def parse_target_from_request(url: str, headers: Dict[str, str]) -> Tuple[str, int, str]:
    """Extract target host, port, and path from request URL and headers."""
    if url.startswith("http://") or url.startswith("https://"):
        parsed = urlsplit(url)
        host = parsed.hostname or ""
        port = parsed.port or (443 if parsed.scheme == "https" else 80)
        path = parsed.path or "/"
        if parsed.query:
            path = f"{path}?{parsed.query}"
        return host, port, path

    host_header = headers.get("Host", "")
    if not host_header:
        return "", 0, ""
    if ":" in host_header:
        host, port_str = host_header.split(":", 1)
        try:
            port = int(port_str)
        except ValueError:
            port = 80
    else:
        host = host_header
        port = 80
    path = url or "/"
    return host, port, path


def build_forward_request(
    method: str,
    path: str,
    version: str,
    headers: Dict[str, str],
    body: bytes,
) -> bytes:
    """Build an origin-form HTTP/1.1 request to send to the upstream server."""
    forward_headers = {
        key: value for key, value in headers.items() if key.lower() not in {"proxy-connection"}
    }
    forward_headers["Connection"] = "close"
    request_line = f"{method} {path} {version}\r\n"
    header_lines = "".join(f"{key}: {value}\r\n" for key, value in forward_headers.items())
    return (request_line + header_lines + "\r\n").encode("iso-8859-1") + body
