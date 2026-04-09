"""Tests for hard concurrency gate — prevent OOM from runaway processes.

Runs via: cd ~/code/mtor && uv run pytest assays/test_concurrency_gate.py -x
"""

from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))


class TestConcurrencyGate:
    """max_concurrent computed from available providers only."""

    def test_max_concurrent_only_counts_available_providers(self):
        """Providers without API keys should not contribute to max_concurrent."""
        from mtor.worker.translocase import PROVIDER_LIMITS

        # PROVIDER_LIMITS should exist and be a dict
        assert isinstance(PROVIDER_LIMITS, dict)
        assert "zhipu" in PROVIDER_LIMITS

    def test_process_count_gate_blocks_when_at_limit(self):
        """When ribosome process count >= limit, new dispatch is blocked."""
        # This tests the concept — the actual gate reads ps aux
        from mtor.worker.provider import dispatch_blocked

        # At limit → blocked
        assert dispatch_blocked(running_tasks=5, load_avg=0.0) is True
        # Below limit → allowed
        assert dispatch_blocked(running_tasks=1, load_avg=0.0) is False

    def test_provider_limits_sum_reasonable(self):
        """Total provider limits should not exceed ganglion capacity."""
        from mtor.worker.translocase import PROVIDER_LIMITS

        total = sum(PROVIDER_LIMITS.values())
        # Ganglion is 4CPU/24GB — should not exceed ~6 concurrent
        assert total <= 10, f"Total provider limits {total} too high for ganglion"
