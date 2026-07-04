"""Tests for pending-activity extraction from real temporalio describe results.

temporalio 1.24.0 exposes pending activities only on raw_description as
protobuf records; these tests mirror that shape instead of mocking a
pending_activities attribute that the SDK does not have.
Runs via: cd ~/code/mtor && uv run pytest assays/test_execution_state_proto_describe.py -x
"""

from __future__ import annotations

import asyncio
import sys
from datetime import UTC, datetime, timedelta
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

from google.protobuf.timestamp_pb2 import Timestamp

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from mtor.client import workflow_execution_state

NOW = datetime(2026, 7, 4, 8, 30, tzinfo=UTC)


class _SdkDescribe:
    """Mimics temporalio's WorkflowExecutionDescription: no pending_activities attribute."""

    def __init__(self, raw_activities):
        self.raw_description = SimpleNamespace(pending_activities=raw_activities)


def _proto_ts(when=None):
    ts = Timestamp()
    if when is not None:
        ts.FromDatetime(when)
    return ts


def _client_for(desc):
    handle = MagicMock()
    handle.describe = AsyncMock(return_value=desc)
    client = MagicMock()
    client.get_workflow_handle = MagicMock(return_value=handle)
    return client


def _state(desc):
    client = _client_for(desc)
    return asyncio.run(workflow_execution_state(client, "wf-proto", now=NOW))


def test_raw_describe_fresh_heartbeat_reports_executing():
    """A STARTED proto activity with a 30s-old heartbeat Timestamp classifies as executing."""
    activity = SimpleNamespace(
        last_heartbeat_time=_proto_ts(NOW - timedelta(seconds=30)),
        last_started_time=_proto_ts(NOW - timedelta(minutes=5)),
    )
    state = _state(_SdkDescribe([activity]))
    assert state["execution_state"] == "executing"
    assert state["heartbeat_age_seconds"] == 30.0


def test_raw_describe_started_only_reports_executing():
    """Unset heartbeat Timestamp with a 20s-old start still reads as executing."""
    activity = SimpleNamespace(
        last_heartbeat_time=_proto_ts(),
        last_started_time=_proto_ts(NOW - timedelta(seconds=20)),
    )
    state = _state(_SdkDescribe([activity]))
    assert state["execution_state"] == "executing"
    assert "heartbeat_age_seconds" not in state
    assert state["started_age_seconds"] == 20.0


def test_raw_describe_unset_timestamps_report_queued():
    """All-unset proto Timestamps mean no evidence: bare queued, not epoch-1970 staleness."""
    activity = SimpleNamespace(
        last_heartbeat_time=_proto_ts(),
        last_started_time=_proto_ts(),
    )
    state = _state(_SdkDescribe([activity]))
    assert state == {"execution_state": "queued"}


def test_raw_describe_empty_activities_report_queued():
    """No pending activities on the raw description stays a bare queued verdict."""
    state = _state(_SdkDescribe([]))
    assert state == {"execution_state": "queued"}
