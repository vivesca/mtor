"""Tests for circadian dispatch curve — time-based dispatch modulation.

Runs via: cd ~/code/mtor && uv run pytest assays/test_circadian.py -x
"""

from __future__ import annotations

from pathlib import Path

import sys

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))


class TestDispatchRatePeaksOvernight:
    """Overnight hours should have the highest dispatch rate."""

    def test_overnight_hours_at_peak(self):
        from mtor.watch import circadian_dispatch_rate

        # Hours 22-05 are overnight — rate should be 1.0
        for hour in [22, 23, 0, 1, 2, 3, 4, 5]:
            rate = circadian_dispatch_rate(hour)
            assert rate == 1.0, f"hour {hour}: expected peak rate 1.0, got {rate}"

    def test_daytime_lower_than_overnight(self):
        from mtor.watch import circadian_dispatch_rate

        overnight_rate = circadian_dispatch_rate(2)
        for hour in [9, 12, 15, 17]:
            assert circadian_dispatch_rate(hour) < overnight_rate, f"hour {hour}"

    def test_rate_always_between_zero_and_one(self):
        from mtor.watch import circadian_dispatch_rate

        for hour in range(24):
            rate = circadian_dispatch_rate(hour)
            assert 0.0 <= rate <= 1.0, f"hour {hour}: rate {rate} out of [0, 1]"


class TestDispatchTapersBeforeMorning:
    """Dispatch rate should decrease (taper) during the pre-morning hours."""

    def test_monotonic_decrease_from_overnight_to_daytime(self):
        from mtor.watch import circadian_dispatch_rate

        # Hour 5 is still overnight (peak), hours 6-7 taper, hour 8 is daytime
        rate_5 = circadian_dispatch_rate(5)
        rate_6 = circadian_dispatch_rate(6)
        rate_7 = circadian_dispatch_rate(7)
        rate_8 = circadian_dispatch_rate(8)

        assert rate_5 == 1.0, "hour 5 should still be at overnight peak"
        assert rate_6 > rate_7, f"taper: rate_6 ({rate_6}) should exceed rate_7 ({rate_7})"
        assert rate_7 > rate_8, f"taper: rate_7 ({rate_7}) should exceed rate_8 ({rate_8})"

    def test_morning_taper_reaches_daytime_floor(self):
        from mtor.watch import circadian_dispatch_rate

        daytime_rate = circadian_dispatch_rate(12)
        morning_entry_rate = circadian_dispatch_rate(8)

        # Hour 8 should have reached the daytime floor
        assert morning_entry_rate == daytime_rate, (
            f"hour 8 ({morning_entry_rate}) should match daytime floor ({daytime_rate})"
        )

    def test_no_overnight_rate_jump_at_six(self):
        from mtor.watch import circadian_dispatch_rate

        # Hour 6 should not jump up — it should start tapering from the peak
        rate_5 = circadian_dispatch_rate(5)
        rate_6 = circadian_dispatch_rate(6)

        assert rate_6 <= rate_5, "hour 6 must not exceed overnight peak"
