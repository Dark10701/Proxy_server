"""Flask-based monitoring server for real-time proxy dashboard."""

import os
import threading

from flask import Flask, jsonify, send_file

from metrics_store import MetricsStore


def create_monitoring_app(metrics_store: MetricsStore) -> Flask:
    app = Flask(__name__)

    @app.get("/api/summary")
    def summary() -> tuple:
        return jsonify(metrics_store.get_summary()), 200

    @app.get("/api/time-series")
    def time_series() -> tuple:
        return jsonify(metrics_store.get_time_series()), 200

    @app.get("/api/top-domains")
    def top_domains() -> tuple:
        return jsonify(metrics_store.get_top_domains()), 200

    @app.get("/api/recent-requests")
    def recent_requests() -> tuple:
        return jsonify(metrics_store.get_recent_requests(limit=20)), 200

    @app.get("/")
    def dashboard() -> object:
        template_path = os.path.join(os.path.dirname(__file__), "dashboard.html")
        return send_file(template_path)

    return app


def start_monitoring_server(metrics_store: MetricsStore, host: str = "0.0.0.0", port: int = 9090) -> threading.Thread:
    app = create_monitoring_app(metrics_store)

    thread = threading.Thread(
        target=lambda: app.run(host=host, port=port, debug=False, use_reloader=False),
        daemon=True,
    )
    thread.start()
    return thread
