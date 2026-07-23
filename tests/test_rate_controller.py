"""Unit tests for the adaptive rate controller."""

import pytest

from proxy_server.rate_controller import RateController


def test_burst_is_capped_by_the_current_rate():
    rc = RateController(initial_rate=5.0, min_rate=1.0)

    allowed = sum(1 for _ in range(20) if rc.allow_request())

    # A full bucket permits one burst of `initial_rate`, not 20 requests.
    assert allowed == pytest.approx(5, abs=1)


def test_fast_upstream_raises_the_rate():
    rc = RateController(initial_rate=10.0, up_step=2.0, fast_latency_ms=100.0)

    for _ in range(5):
        rc.record_latency(10.0)

    assert rc.get_current_rate() == pytest.approx(20.0)


def test_slow_upstream_lowers_the_rate():
    rc = RateController(initial_rate=100.0, down_factor=0.5, slow_latency_ms=500.0)

    rc.record_latency(900.0)

    assert rc.get_current_rate() == pytest.approx(50.0)


def test_rate_never_falls_below_the_floor():
    rc = RateController(initial_rate=10.0, min_rate=4.0, down_factor=0.5)

    for _ in range(50):
        rc.record_latency(5000.0)

    assert rc.get_current_rate() == pytest.approx(4.0)


def test_rate_never_exceeds_the_ceiling():
    rc = RateController(initial_rate=10.0, max_rate=12.0, up_step=5.0)

    for _ in range(20):
        rc.record_latency(1.0)

    assert rc.get_current_rate() == pytest.approx(12.0)


def test_single_slow_outlier_does_not_collapse_the_rate():
    """The defect this port fixes: mean-based adaptation over-reacted.

    Forty-nine fast requests and one very slow one is a healthy upstream
    with one big download, not an overloaded one. The mean of that set
    lands past the 500 ms back-off threshold; the median does not.
    """
    rc = RateController(initial_rate=50.0, up_step=0.0, down_factor=0.5)

    for _ in range(49):
        rc.record_latency(20.0)
    rc.record_latency(30000.0)

    assert rc.get_current_rate() == pytest.approx(50.0)


def test_sustained_slowness_still_backs_off():
    """Robustness to outliers must not mean ignoring a real slowdown."""
    rc = RateController(initial_rate=50.0, min_rate=1.0, down_factor=0.5)

    for _ in range(30):
        rc.record_latency(2000.0)

    assert rc.get_current_rate() < 50.0


def test_stats_report_rejections():
    rc = RateController(initial_rate=2.0)

    for _ in range(10):
        rc.allow_request()

    stats = rc.stats()
    assert stats["allowed"] + stats["rejected"] == 10
    assert stats["rejected"] > 0


@pytest.mark.parametrize(
    "kwargs",
    [
        {"min_rate": 0},
        {"min_rate": 10, "max_rate": 5},
        {"down_factor": 1.5},
        {"down_factor": 0},
    ],
)
def test_invalid_configuration_is_rejected(kwargs):
    with pytest.raises(ValueError):
        RateController(**kwargs)
