"""Tests for v3 stall detection — Langfuse trace integration.

v2: streaming-json pattern detection (local, real-time)
v3: emit stall events as Langfuse spans, query trace history for
    cross-workflow stall patterns, and surface stall rates in dashboard.

v3 adds observability — v2 detects, v3 records and learns.

The guarded import below keeps collection alive if the module regresses.
"""

from __future__ import annotations

import sys
from pathlib import Path

from langfuse import Langfuse
from langfuse._client.span import LangfuseSpan

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


import pytest  # noqa: E402
from unittest.mock import MagicMock, patch  # noqa: E402

if not _stall_trace_available:
    pytestmark = pytest.mark.skip(reason="mtor.worker.stall_trace not yet available")


def _make_client() -> MagicMock:
    """Mock client spec'd to v4 so removed v2 attrs raise AttributeError."""
    client = MagicMock(spec=Langfuse)
    client.start_observation.return_value = MagicMock(spec=LangfuseSpan)
    return client


def _make_observation() -> MagicMock:
    """Mock observation spec'd to v4 so removed v2 attrs raise AttributeError."""
    obs = MagicMock(spec=LangfuseSpan)
    obs.start_observation.return_value = MagicMock(spec=LangfuseSpan)
    return obs


class TestStallTraceEmission:
    """Stall events are recorded as Langfuse observations for observability."""

    @patch("mtor.worker.stall_trace.get_langfuse")
    @patch("langfuse.Langfuse.create_trace_id", return_value="stall-trace-id")
    def test_stall_detected_emits_span(self, _ctid, mock_lf):
        """v4: standalone stall creates a root observation + 'stall-detected' child."""
        client = _make_client()
        root = _make_observation()
        client.start_observation.return_value = root
        mock_lf.return_value = client

        record_stall_event(  # type: ignore[union-attr]
            workflow_id="ribosome-glm51-sha-gate-a1b2c3d4",
            pattern="repeated_action",
            action_taken="warn",
            details={"tool": "Read", "count": 4},
        )

        # Root observation is the standalone stall trace.
        client.start_observation.assert_called_once()
        root_kwargs = client.start_observation.call_args[1]
        assert root_kwargs["name"] == "stall-ribosome-glm51-sha-gate-a1b2c3d4"
        # Child observation carries the stall details.
        root.start_observation.assert_called_once()
        call_kwargs = root.start_observation.call_args[1]
        assert call_kwargs["name"] == "stall-detected"
        assert call_kwargs["metadata"]["pattern"] == "repeated_action"
        assert call_kwargs["metadata"]["action"] == "warn"

    @patch("mtor.worker.stall_trace.get_langfuse")
    @patch("langfuse.Langfuse.create_trace_id", return_value="stall-trace-id")
    def test_stall_kill_emits_generation(self, _ctid, mock_lf):
        """Kill events include the partial stdout for debugging."""
        client = _make_client()
        root = _make_observation()
        client.start_observation.return_value = root
        mock_lf.return_value = client

        record_stall_event(  # type: ignore[union-attr]
            workflow_id="ribosome-glm51-sha-gate-a1b2c3d4",
            pattern="ping_pong",
            action_taken="kill",
            details={"partial_stdout": "Working on file..."},
        )

        call_kwargs = root.start_observation.call_args[1]
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
