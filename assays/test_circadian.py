"""Tests for circadian dispatch curve — time-of-day weighting in watch.py."""

from __future__ import annotations

from mtor.watch import circadian_dispatch_rate


def test_test_dispatch_rate_peaks_overnight():
    """Overnight hours (22–05 UTC) should return peak rate 1.0."""
    for hour in (22, 23, 0, 1, 2, 3, 4, 5):
        assert circadian_dispatch_rate(hour) == 1.0, f"hour={hour} should be peak"


def test_test_dispatch_tapers_before_morning():
    """Rate should decrease during the morning taper (hours 6–7 UTC).

    Hour 6 starts at peak (1.0); hour 7 is midway (0.6).
    By hour 8 we hit the daytime floor (0.2).
    """
    assert circadian_dispatch_rate(6) == 1.0, "hour 6 should still be peak"
    assert circadian_dispatch_rate(7) == 0.6, "hour 7 should taper to 0.6"
    assert circadian_dispatch_rate(8) == 0.2, "hour 8 should reach daytime floor"
