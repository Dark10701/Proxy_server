"""Tests for the dashboard's HTTP surface.

The reader had unit tests but the Flask routes did not, so a template
written in the wrong encoding took the page down with a 500 while every
test stayed green. These exercise the routes themselves.
"""

import pytest

from dashboard.app import app
from proxy_server.metrics import MetricsLogger


@pytest.fixture
def client(tmp_path):
    metrics = tmp_path / "metrics.csv"
    logger = MetricsLogger(str(metrics))
    logger.log("10.0.0.1", "GET", "http://a.test/", "a.test", 12, 100, 900,
               blocked=0, cache="hit")
    logger.log("10.0.0.1", "GET", "http://a.test/2", "a.test", 40, 100, 800,
               blocked=0, cache="miss")
    logger.log("10.0.0.2", "GET", "http://b.test/", "b.test", 0, 90, 60,
               blocked=1)

    app.config["METRICS_PATH"] = str(metrics)
    app.config["REFRESH_SECONDS"] = 2.0
    app.config["TESTING"] = True
    return app.test_client()


def test_index_renders(client):
    """Catches a template that cannot be decoded or parsed."""
    response = client.get("/")

    assert response.status_code == 200
    body = response.get_data(as_text=True)
    assert "Proxy Metrics" in body
    assert "Cache hit ratio" in body
    # The Jinja variable must actually have been substituted.
    assert "REFRESH_MS = 2000" in body


def test_template_is_valid_utf8():
    """A cp1252-encoded template renders fine locally and 500s in Flask."""
    from pathlib import Path

    template = Path(app.root_path) / "templates" / "index.html"
    template.read_bytes().decode("utf-8")


def test_api_metrics_returns_aggregates(client):
    response = client.get("/api/metrics")

    assert response.status_code == 200
    data = response.get_json()
    assert data["total_requests"] == 3
    assert data["blocked_requests"] == 1
    assert data["cache_hits"] == 1
    assert data["cache_misses"] == 1
    assert data["cache_hit_ratio_pct"] == 50.0


def test_api_metrics_when_file_is_missing(client, tmp_path):
    app.config["METRICS_PATH"] = str(tmp_path / "nothing.csv")

    response = client.get("/api/metrics")

    assert response.status_code == 200
    data = response.get_json()
    assert data["available"] is False
    assert data["total_requests"] == 0


def test_counter_elements_have_no_nested_id_children(client):
    """Regression: render() sets textContent on #blocked, which wipes its
    children. A sibling like #blockRate nested inside it gets destroyed,
    and the next line's getElementById returns null and throws -- silently
    caught and mislabelled as "dashboard unreachable". Keep id-bearing
    counters and their adornments as siblings, never parent/child."""
    import re
    html = client.get("/").get_data(as_text=True)
    # The blocked count must be its own span, with blockRate as a sibling.
    assert '<span id="blocked">' in html
    # blockRate must not sit inside the element whose textContent is overwritten.
    m = re.search(r'id="blocked"[^>]*>(.*?)</span>', html)
    assert m is not None
    assert 'id="blockRate"' not in m.group(1)
