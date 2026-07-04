"""Tests for execution-state classification across worker restarts.

Simulates the 2026-07-04 incident: worker restarted mid-activity, retried
attempts executing while Temporal heartbeat data lags or points at the dead
attempt. Runs via: cd ~/code/mtor && uv run pytest assays/test_execution_state_restart.py -x
"""

from __future__ import annotations

import asyncio
import sys
from datetime import UTC, datetime, timedelta
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from mtor.client import workflow_execution_state

NOW = datetime(2026, 7, 4, 8, 30, tzinfo=UTC)


def _client_with_activity(heartbeat_time=None, started_time=None, with_activity=True):
    desc = MagicMock()
    if with_activity:
        activity = MagicMock()
        activity.last_heartbeat_time = heartbeat_time
        activity.last_started_time = started_time
        desc.pending_activities = [activity]
    else:
        desc.pending_activities = []
    handle = MagicMock()
    handle.describe = AsyncMock(return_value=desc)
    client = MagicMock()
    client.get_workflow_handle = MagicMock(return_value=handle)
    return client


def _state(client):
    return asyncio.run(workflow_execution_state(client, "wf-restart", now=NOW))


def test_fresh_start_stale_heartbeat_reports_executing():
    """Retry attempt started 20s ago after worker restart; heartbeat still shows the dead attempt (7m old)."""
    client = _client_with_activity(
        heartbeat_time=NOW - timedelta(minutes=7),
        started_time=NOW - timedelta(seconds=20),
    )
    state = _state(client)
    assert state["execution_state"] == "executing"
    assert state["started_age_seconds"] == 20.0
    assert state["heartbeat_stale"] is False


def test_started_attempt_without_heartbeat_reports_executing():
    """Attempt started 45s ago, first throttled heartbeat not yet recorded."""
    client = _client_with_activity(
        heartbeat_time=None,
        started_time=NOW - timedelta(seconds=45),
    )
    state = _state(client)
    assert state["execution_state"] == "executing"
    assert "heartbeat_age_seconds" not in state
    assert state["started_age_seconds"] == 45.0


def test_throttled_heartbeat_within_lag_reports_executing():
    """Heartbeat 90s old — within SDK throttle lag — must not read as queued."""
    client = _client_with_activity(heartbeat_time=NOW - timedelta(seconds=90))
    state = _state(client)
    assert state["execution_state"] == "executing"
    assert state["heartbeat_age_seconds"] == 90.0


def test_dead_attempt_reports_queued():
    """Attempt started 40m ago with heartbeat 20m old is genuinely dead: queued and stale."""
    client = _client_with_activity(
        heartbeat_time=NOW - timedelta(minutes=20),
        started_time=NOW - timedelta(minutes=40),
    )
    state = _state(client)
    assert state["execution_state"] == "queued"
    assert state["heartbeat_stale"] is True


def test_non_datetime_started_time_ignored():
    """SDK objects without a real last_started_time datetime must not break classification."""
    client = _client_with_activity(
        heartbeat_time=NOW - timedelta(seconds=30),
        started_time=MagicMock(),
    )
    state = _state(client)
    assert state["execution_state"] == "executing"
    assert "started_age_seconds" not in state


def test_no_pending_activities_reports_queued():
    """No pending activities at all stays a bare queued verdict."""
    client = _client_with_activity(with_activity=False)
    state = _state(client)
    assert state == {"execution_state": "queued"}
