"""Small socket helpers shared by both concurrency models."""

import socket


def set_nodelay(sock) -> None:
    """Disable Nagle's algorithm.

    A proxy writes a complete request or response and then waits for the
    peer. Nagle holds a small trailing segment until the previous one is
    acknowledged, and the peer's delayed-ACK timer holds that ack, so the
    two interact to add a fixed stall (~10-40ms) to every exchange. That
    is pure latency on a request/response workload with nothing to
    coalesce, so it is switched off on every socket the proxy owns.
    """
    if sock is None:
        return
    try:
        sock.setsockopt(socket.IPPROTO_TCP, socket.TCP_NODELAY, 1)
    except (OSError, AttributeError):
        # Not fatal: the connection still works, just with Nagle on.
        pass
