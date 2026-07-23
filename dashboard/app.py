"""Flask + SocketIO dashboard over the proxy's metrics CSV.

Read-only: it never writes to the metrics file, so it can be started,
stopped and restarted independently of the proxy. A background task
re-reads the CSV on an interval and pushes a snapshot to connected
clients; the HTTP endpoint exists so the page renders on first load and
so the data is scriptable without a websocket.
"""

import argparse
import sys
from pathlib import Path

from flask import Flask, jsonify, render_template
from flask_socketio import SocketIO

# Allow `python dashboard/app.py` as well as `python -m dashboard.app`.
if __package__ in (None, ""):
    sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from dashboard.metrics_reader import snapshot_dict  # noqa: E402
from proxy_server import paths  # noqa: E402

DEFAULT_REFRESH_SECONDS = 2.0

app = Flask(__name__)
socketio = SocketIO(app, cors_allowed_origins="*", async_mode="threading")

# Set by main(); module-level so the background task and routes share it.
app.config["METRICS_PATH"] = str(paths.DEFAULT_METRICS)
app.config["REFRESH_SECONDS"] = DEFAULT_REFRESH_SECONDS

_broadcaster_started = False


@app.route("/")
def index():
    return render_template("index.html", refresh_seconds=app.config["REFRESH_SECONDS"])


@app.route("/api/metrics")
def api_metrics():
    """Current snapshot as JSON. Also the fallback if websockets fail."""
    return jsonify(snapshot_dict(app.config["METRICS_PATH"]))


def _broadcast_loop() -> None:
    """Push a fresh snapshot to all clients on an interval."""
    while True:
        socketio.sleep(app.config["REFRESH_SECONDS"])
        try:
            socketio.emit("metrics_update", snapshot_dict(app.config["METRICS_PATH"]))
        except Exception as exc:  # keep the loop alive across transient errors
            app.logger.warning("metrics broadcast failed: %s", exc)


@socketio.on("connect")
def on_connect():
    """Start the broadcaster lazily, and seed the new client immediately."""
    global _broadcaster_started
    if not _broadcaster_started:
        _broadcaster_started = True
        socketio.start_background_task(_broadcast_loop)
    socketio.emit("metrics_update", snapshot_dict(app.config["METRICS_PATH"]))


def parse_args(argv=None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Proxy metrics dashboard")
    parser.add_argument("--host", default="127.0.0.1", help="Bind address")
    parser.add_argument("--port", type=int, default=5000, help="Bind port")
    parser.add_argument(
        "--metrics",
        default=str(paths.DEFAULT_METRICS),
        help="Path to the proxy's metrics CSV",
    )
    parser.add_argument(
        "--refresh",
        type=float,
        default=DEFAULT_REFRESH_SECONDS,
        help="Seconds between dashboard refreshes",
    )
    return parser.parse_args(argv)


def main() -> None:
    args = parse_args()
    app.config["METRICS_PATH"] = args.metrics
    app.config["REFRESH_SECONDS"] = args.refresh

    print(f"Dashboard on http://{args.host}:{args.port}")
    print(f"Reading metrics from {args.metrics}")
    socketio.run(app, host=args.host, port=args.port, allow_unsafe_werkzeug=True)


if __name__ == "__main__":
    main()
