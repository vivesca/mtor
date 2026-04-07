"""Tests for v3 stall detection — Langfuse trace integration.

v2: streaming-json pattern detection (local, real-time)
v3: emit stall events as Langfuse spans, query trace history for
    cross-workflow stall patterns, and surface stall rates in dashboard.

v3 adds observability — v2 detects, v3 records and learns.

NOTE: stall_trace module is not yet in mtor/worker — tests are stubs pending migration.
"""

from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "mtor" / "worker"))

_stall_trace_available = False
try:
    from mtor.worker.stall_trace import (
        most_common_stall_pattern,
        record_stall_event,
        stall_rate,
    )

    _stall_trace_available = True
except ImportError:
    record_stall_event = None  # type: ignore[assignment, misc]
    stall_rate = None  # type: ignore[assignment, misc]
    most_common_stall_pattern = None  # type: ignore[assignment, misc]


import pytest
from unittest.mock import MagicMock, patch

if not _stall_trace_available:
    pytestmark = pytest.mark.skip(reason="mtor.worker.stall_trace not yet available")


class TestStallTraceEmission:
    """Stall events are recorded as Langfuse spans for observability."""

    @patch("mtor.worker.stall_trace.get_langfuse")
    def test_stall_detected_emits_span(self, mock_lf):
        """When a stall is detected, a span is added to the current trace."""
        mock_trace = MagicMock()
        mock_lf.return_value.trace.return_value = mock_trace

        record_stall_event(  # type: ignore[union-attr]
            workflow_id="ribosome-glm51-sha-gate-a1b2c3d4",
            pattern="repeated_action",
            action_taken="warn",
            details={"tool": "Read", "count": 4},
        )

        mock_trace.span.assert_called_once()
        call_kwargs = mock_trace.span.call_args[1]
        assert call_kwargs["name"] == "stall-detected"
        assert call_kwargs["metadata"]["pattern"] == "repeated_action"
        assert call_kwargs["metadata"]["action"] == "warn"

    @patch("mtor.worker.stall_trace.get_langfuse")
    def test_stall_kill_emits_generation(self, mock_lf):
        """Kill events include the partial stdout for debugging."""
        mock_trace = MagicMock()
        mock_lf.return_value.trace.return_value = mock_trace

        record_stall_event(  # type: ignore[union-attr]
            workflow_id="ribosome-glm51-sha-gate-a1b2c3d4",
            pattern="ping_pong",
            action_taken="kill",
            details={"partial_stdout": "Working on file..."},
        )

        call_kwargs = mock_trace.span.call_args[1]
        assert call_kwargs["metadata"]["action"] == "kill"
        assert "partial_stdout" in call_kwargs["metadata"]

    @patch("mtor.worker.stall_trace.get_langfuse")
    def test_no_langfuse_graceful_noop(self, mock_lf):
        """If Langfuse is unavailable, stall recording is a silent no-op."""
        mock_lf.return_value = None

        # Should not raise
        record_stall_event(  # type: ignore[union-attr]
            workflow_id="test",
            pattern="repeated_action",
            action_taken="warn",
            details={},
        )


class TestStallRateQuery:
    """Query Langfuse for cross-workflow stall patterns."""

    @patch("mtor.worker.stall_trace.get_langfuse")
    def test_stall_rate_returns_percentage(self, mock_lf):
        """stall_rate() returns fraction of recent workflows that hit stalls."""
        rate = stall_rate(window_hours=24)  # type: ignore[union-attr]
        assert isinstance(rate, float)
        assert 0.0 <= rate <= 1.0

    @patch("mtor.worker.stall_trace.get_langfuse")
    def test_most_common_pattern(self, mock_lf):
        """most_common_stall_pattern() returns the dominant pattern name."""
        pattern = most_common_stall_pattern(window_hours=24)  # type: ignore[union-attr]
        assert pattern is None or isinstance(pattern, str)
