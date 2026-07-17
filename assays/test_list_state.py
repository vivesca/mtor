"""Tests for execution state display in mtor riboseq.

Runs via: cd ~/code/mtor && uv run pytest assays/test_list_state.py -x
"""

from __future__ import annotations

import io
import json
import sys
from datetime import UTC, datetime, timedelta
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from mtor.cli import app


def invoke(args: list[str]) -> tuple[int, dict]:
    captured = io.StringIO()
    old_stdout = sys.stdout
    exit_code = 0
    try:
        sys.stdout = captured
        app(args)
    except SystemExit as exc:
        exit_code = exc.code if isinstance(exc.code, int) else 1
    finally:
        sys.stdout = old_stdout
    return exit_code, json.loads(captured.getvalue())


def _execution(workflow_id: str, status: str = "RUNNING"):
    execution = MagicMock()
    execution.id = workflow_id
    execution.status = MagicMock()
    execution.status.name = status
    execution.start_time = datetime(2026, 5, 15, 4, 0, tzinfo=UTC)
    execution.close_time = None if status == "RUNNING" else datetime(2026, 5, 15, 4, 5, tzinfo=UTC)
    execution.search_attributes = {}
    return execution


def _client_for(*executions, heartbeat_by_id=None):
    client = MagicMock()
    heartbeat_by_id = heartbeat_by_id or {}

    async def _list(query=None):
        for execution in executions:
            yield execution

    def _handle(workflow_id):
        desc = MagicMock()
        desc.pending_activities = []
        heartbeat_time = heartbeat_by_id.get(workflow_id)
        if heartbeat_time is not None:
            activity = MagicMock()
            activity.last_heartbeat_time = heartbeat_time
            desc.pending_activities = [activity]
        handle = MagicMock()
        handle.describe = AsyncMock(return_value=desc)
        return handle

    client.list_workflows = _list
    client.get_workflow_handle = MagicMock(side_effect=_handle)
    return client


class TestListExecutionState:
    """mtor riboseq shows queued vs executing for RUNNING workflows."""

    def test_recent_heartbeat_shows_executing(self):
        """Workflow with heartbeat within 60s → execution_state='executing'."""
        now = datetime.now(UTC)
        client = _client_for(
            _execution("wf-running"),
            heartbeat_by_id={"wf-running": now - timedelta(seconds=30)},
        )

        with patch("mtor.cli._get_client", return_value=(client, None)), \
             patch("mtor.cli.load_triage", return_value={}):
            exit_code, data = invoke(["riboseq"])

        assert exit_code == 0
        workflow = data["result"]["workflows"][0]
        assert workflow["execution_state"] == "executing"
        assert workflow["heartbeat_age_seconds"] <= 60
        assert workflow["heartbeat_stale"] is False
        assert "last_heartbeat_iso" in workflow

    def test_old_heartbeat_shows_stale_queued(self):
        """Workflow with heartbeat older than 15m is queued and stale."""
        now = datetime.now(UTC)
        client = _client_for(
            _execution("wf-stale"),
            heartbeat_by_id={"wf-stale": now - timedelta(minutes=47)},
        )

        with patch("mtor.cli._get_client", return_value=(client, None)), \
             patch("mtor.cli.load_triage", return_value={}):
            exit_code, data = invoke(["riboseq"])

        assert exit_code == 0
        workflow = data["result"]["workflows"][0]
        assert workflow["execution_state"] == "queued"
        assert workflow["heartbeat_age_seconds"] > 15 * 60
        assert workflow["heartbeat_stale"] is True

    def test_no_heartbeat_shows_queued(self):
        """Workflow with no heartbeat or >60s ago → execution_state='queued'."""
        client = _client_for(_execution("wf-running"))

        with patch("mtor.cli._get_client", return_value=(client, None)), \
             patch("mtor.cli.load_triage", return_value={}):
            exit_code, data = invoke(["riboseq"])

        assert exit_code == 0
        workflow = data["result"]["workflows"][0]
        assert workflow["execution_state"] == "queued"
        assert "heartbeat_age_seconds" not in workflow

    def test_completed_workflows_unchanged(self):
        """COMPLETED workflows don't get execution_state field."""
        execution = _execution("wf-complete", "COMPLETED")
        execution.search_attributes = {"mtor_verdict": ["rejected"]}
        client = _client_for(execution)

        with patch("mtor.cli._get_client", return_value=(client, None)), \
             patch("mtor.cli.load_triage", return_value={}):
            exit_code, data = invoke(["riboseq"])

        assert exit_code == 0
        workflow = data["result"]["workflows"][0]
        assert "execution_state" not in workflow
        assert workflow["operator_state"] == "failed_review"

    def test_terminated_workflows_unchanged(self):
        """TERMINATED workflows don't get execution_state field."""
        client = _client_for(_execution("wf-terminated", "TERMINATED"))

        with patch("mtor.cli._get_client", return_value=(client, None)), \
             patch("mtor.cli.load_triage", return_value={}):
            exit_code, data = invoke(["riboseq"])

        assert exit_code == 0
        workflow = data["result"]["workflows"][0]
        assert "execution_state" not in workflow
        assert workflow["operator_state"] == "terminated"
