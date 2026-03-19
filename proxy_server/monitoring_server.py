"""Flask-based monitoring server for real-time proxy dashboard."""

import os
import threading
from typing import Optional

from flask import Flask, jsonify, send_file

from metrics_store import MetricsStore
from rate_controller import RateController
from scheduler import RequestScheduler


def create_monitoring_app(
    metrics_store: MetricsStore,
    rate_controller: Optional[RateController] = None,
    scheduler: Optional[RequestScheduler] = None,
) -> Flask:
    app = Flask(__name__)

    @app.get("/api/summary")
    def summary() -> tuple:
        summary_data = metrics_store.get_summary()
        summary_data["current_rate"] = (
            rate_controller.get_current_rate() if rate_controller else 0.0
        )
        summary_data["scheduler_queue_size"] = (
            scheduler.get_queue_size() if scheduler else 0
        )
        return jsonify(summary_data), 200

    @app.get("/api/time-series")
    def time_series() -> tuple:
        return jsonify(metrics_store.get_time_series()), 200

    @app.get("/api/top-domains")
    def top_domains() -> tuple:
        return jsonify(metrics_store.get_top_domains()), 200

    @app.get("/api/recent-requests")
    def recent_requests() -> tuple:
        return jsonify(metrics_store.get_recent_requests(limit=20)), 200

    @app.get("/api/traffic-patterns")
    def traffic_patterns() -> tuple:
        return jsonify(metrics_store.get_traffic_patterns()), 200

    @app.get("/")
    def dashboard() -> object:
        template_path = os.path.join(os.path.dirname(__file__), "dashboard.html")
        return send_file(template_path)

    return app


def start_monitoring_server(
    metrics_store: MetricsStore,
    rate_controller: Optional[RateController] = None,
    scheduler: Optional[RequestScheduler] = None,
    host: str = "0.0.0.0",
    port: int = 9090,
) -> threading.Thread:
    app = create_monitoring_app(
        metrics_store=metrics_store,
        rate_controller=rate_controller,
        scheduler=scheduler,
    )

    thread = threading.Thread(
        target=lambda: app.run(host=host, port=port, debug=False, use_reloader=False),
        daemon=True,
    )
    thread.start()
    return thread
