"""Assays for the source-labeling behavior of `_execution_state_with_fallback`.

Wave 1 of the trace diagnosis refactor split trust between two evidence
sources: live Temporal pending-activity heartbeats (preferred) and worker-log
recency (fallback). These tests pin the `source` label each path produces and
the diagnosis string a fresh heartbeat yields.
"""

from __future__ import annotations

import asyncio
from datetime import UTC, datetime, timedelta
from types import SimpleNamespace
from unittest.mock import MagicMock

from google.protobuf.timestamp_pb2 import Timestamp

from mtor.cli import _execution_state_with_fallback, _trace_diagnosis
from mtor.client import workflow_execution_state


NOW = datetime(2026, 7, 4, 12, 0, tzinfo=UTC)


def _proto_ts(when: datetime) -> Timestamp:
    ts = Timestamp()
    ts.FromDatetime(when)
    return ts


def test_trace_state_uses_pending_activity_heartbeat():
    desc = SimpleNamespace(
        raw_description=SimpleNamespace(
            pending_activities=[
                SimpleNamespace(
                    last_heartbeat_time=_proto_ts(NOW - timedelta(seconds=30)),
                    last_started_time=_proto_ts(NOW - timedelta(minutes=5)),
                )
            ]
        )
    )
    client = MagicMock()
    state = asyncio.run(workflow_execution_state(client, "wf-t", now=NOW, desc=desc))
    assert state["execution_state"] == "executing"
    assert state["source"] == "pending_activities"
    client.get_workflow_handle.assert_not_called()


def test_trace_state_fallback_labels_log_cache():
    result = _execution_state_with_fallback(
        {"execution_state": "queued"}, [{"file": "wf.log"}]
    )
    assert result["execution_state"] == "executing"
    assert result["source"] == "log-cache"
    assert result["active_log_count"] == 1


def test_trace_state_fallback_no_logs_stays_queued():
    result = _execution_state_with_fallback({}, [])
    assert result["execution_state"] == "queued"
    assert result["source"] == "log-cache"


def test_trace_diagnosis_not_stale_with_fresh_heartbeat():
    payload = {
        "operator_state": "running",
        "pending_activities": [{"activity_id": "a1"}],
        "execution_state": {
            "execution_state": "executing",
            "source": "pending_activities",
        },
    }
    assert _trace_diagnosis(payload) == "workflow is currently executing"
