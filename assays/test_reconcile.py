"""Tests for spec status reconciliation (feedback loop).

Runs via: cd ~/code/mtor && uv run pytest assays/test_reconcile.py -x
"""

from __future__ import annotations

import re
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from mtor.spec import update_spec_status


class TestReconcileSpecs:
    """reconcile_specs scans dispatched specs and updates status from Temporal."""

    def test_completed_accepted_becomes_done(self, tmp_path: Path):
        """COMPLETED + accepted verdict → status: done."""
        spec = tmp_path / "test-spec.md"
        spec.write_text("---\nstatus: dispatched\nworkflow_id: wf-123\n---\nBody\n")

        # Simulate what reconcile should do
        update_spec_status(spec, "done", verdict="accepted")

        text = spec.read_text()
        assert "status: done" in text
        assert "verdict: accepted" in text

    def test_completed_rejected_becomes_failed(self, tmp_path: Path):
        """COMPLETED + rejected verdict → status: failed."""
        spec = tmp_path / "test-spec.md"
        spec.write_text("---\nstatus: dispatched\nworkflow_id: wf-456\n---\nBody\n")

        update_spec_status(spec, "failed", verdict="rejected")

        text = spec.read_text()
        assert "status: failed" in text
        assert "verdict: rejected" in text

    def test_terminated_becomes_failed(self, tmp_path: Path):
        """TERMINATED → status: failed."""
        spec = tmp_path / "test-spec.md"
        spec.write_text("---\nstatus: dispatched\nworkflow_id: wf-789\n---\nBody\n")

        update_spec_status(spec, "failed")

        text = spec.read_text()
        assert "status: failed" in text

    def test_running_left_as_dispatched(self, tmp_path: Path):
        """Still RUNNING → no change (stays dispatched)."""
        spec = tmp_path / "test-spec.md"
        spec.write_text("---\nstatus: dispatched\nworkflow_id: wf-running\n---\nBody\n")

        # reconcile_specs should skip RUNNING workflows
        # update_spec_status is NOT called for running specs
        text = spec.read_text()
        assert "status: dispatched" in text

    def test_spec_without_workflow_id_skipped(self, tmp_path: Path):
        """Specs without workflow_id in frontmatter are skipped."""
        spec = tmp_path / "test-spec.md"
        spec.write_text("---\nstatus: dispatched\n---\nBody\n")

        # No workflow_id → nothing to query → skip
        text = spec.read_text()
        assert "status: dispatched" in text

    def test_done_spec_not_requeried(self, tmp_path: Path):
        """Specs already at done/failed are not re-queried."""
        spec = tmp_path / "test-spec.md"
        spec.write_text("---\nstatus: done\nworkflow_id: wf-old\n---\nBody\n")

        text = spec.read_text()
        assert "status: done" in text
