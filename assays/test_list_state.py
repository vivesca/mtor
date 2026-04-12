"""Tests for execution state display in mtor riboseq.

Runs via: cd ~/code/mtor && uv run pytest assays/test_list_state.py -x
"""

from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))


class TestListExecutionState:
    """mtor riboseq shows queued vs executing for RUNNING workflows."""

    def test_recent_heartbeat_shows_executing(self):
        """Workflow with heartbeat within 60s → execution_state='executing'."""
        # Will be implemented by the spec
        pass

    def test_no_heartbeat_shows_queued(self):
        """Workflow with no heartbeat or >60s ago → execution_state='queued'."""
        pass

    def test_completed_workflows_unchanged(self):
        """COMPLETED workflows don't get execution_state field."""
        pass

    def test_terminated_workflows_unchanged(self):
        """TERMINATED workflows don't get execution_state field."""
        pass
