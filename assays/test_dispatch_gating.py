"""Tests for wiring dispatch gating into the dispatch path.

Runs via: cd ~/code/mtor && uv run pytest assays/test_dispatch_gating.py -x
"""

from __future__ import annotations

import sys
from pathlib import Path
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from mtor.watch import check_ganglion_load
from mtor.worker.provider import (
    dispatch_blocked,
    feedback_dispatch_blocked,
    ProviderFeedbackTracker,
)


class TestDispatchGatingWired:
    """Verify dispatch gating functions are called from the dispatch path."""

    def test_feedback_dispatch_blocked_combines_load_and_feedback(self):
        """feedback_dispatch_blocked blocks on load OR feedback."""
        # Blocked by load
        assert feedback_dispatch_blocked(running_tasks=10, load_avg=0.0) is True

        # Blocked by feedback
        tracker = ProviderFeedbackTracker(window_size=4, rejection_threshold=0.5)
        for _ in range(3):
            tracker.record(True)  # 3/3 = 100% rejection
        assert feedback_dispatch_blocked(
            running_tasks=0, load_avg=0.0, feedback=tracker
        ) is True

        # Neither blocked
        clean_tracker = ProviderFeedbackTracker()
        assert feedback_dispatch_blocked(
            running_tasks=0, load_avg=0.0, feedback=clean_tracker
        ) is False

    def test_dispatch_blocked_threshold(self):
        """dispatch_blocked returns True when thresholds exceeded."""
        assert dispatch_blocked(running_tasks=5, load_avg=0.0) is True
        assert dispatch_blocked(running_tasks=0, load_avg=4.0) is True
        assert dispatch_blocked(running_tasks=1, load_avg=0.5) is False

    def test_check_ganglion_load_returns_dataclass(self):
        """check_ganglion_load returns GanglionLoad with correct fields."""
        load = check_ganglion_load(running_tasks=2, load_avg=1.5)
        assert load.running_tasks == 2
        assert load.load_avg == 1.5
        assert load.load_level == "low"

    def test_feedback_tracker_recovery(self):
        """Tracker recovers when rejections slide out of window."""
        tracker = ProviderFeedbackTracker(window_size=4, rejection_threshold=0.5)
        # Fill with rejections
        for _ in range(4):
            tracker.record(True)
        assert tracker.should_throttle() is True

        # Add successes to push rejections out
        for _ in range(4):
            tracker.record(False)
        assert tracker.should_throttle() is False

    def test_watch_skips_when_dispatch_blocked(self, tmp_path):
        """run_watch records a blocked cycle instead of syncing when gate blocks."""
        from mtor.watch import run_watch

        repo = tmp_path / "repo"
        repo.mkdir()
        with patch("mtor.watch.feedback_dispatch_blocked", return_value=True), \
             patch("mtor.watch.sync_from_ganglion") as sync:
            stats = run_watch(str(repo), once=True)

        sync.assert_not_called()
        assert stats.cycles == 1
        assert stats.total_errors == 1
        assert stats.cycles_run[0].error == "dispatch_blocked"
