"""Tests for spec status reconciliation (feedback loop).

Runs via: cd ~/code/mtor && uv run pytest assays/test_reconcile.py -x
"""

from __future__ import annotations

import os
import sys
from pathlib import Path
from unittest.mock import MagicMock

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from mtor.spec import update_spec_status
from mtor.reconcile import check_code_exists, reconcile_all, reconcile_spec

integration = pytest.mark.skipif(
    not os.environ.get("MTOR_INTEGRATION"),
    reason="integration: check_code_exists resolves against ~/code/mtor, absent on a clean runner. Set MTOR_INTEGRATION=1 to run.",
)


class TestReconcileCheckCodeExists:
    """check_code_exists correctly detects existing files and functions."""

    @integration
    def test_file_exists_returns_true(self):
        """Existing file → returns True."""
        assert check_code_exists("mtor/reconcile.py") is True

    def test_file_missing_returns_false(self):
        """Missing file → returns False."""
        assert check_code_exists("mtor/nonexistent_file.py") is False

    @integration
    def test_function_exists_in_file_returns_true(self):
        """Existing function in file → returns True."""
        assert check_code_exists("mtor/reconcile.py:check_code_exists") is True

    def test_function_missing_in_file_returns_false(self):
        """Missing function in existing file → returns False."""
        assert check_code_exists("mtor/reconcile.py:missing_function") is False

    def test_backtick_wrapped_absolute_file_path_returns_true(self, tmp_path: Path):
        target = tmp_path / "receptor.md"
        target.write_text("# Receptor\n")

        assert check_code_exists(f"`{target}`", repo_root=tmp_path) is True

    def test_backtick_wrapped_absolute_function_path_returns_true(self, tmp_path: Path):
        target = tmp_path / "module.py"
        target.write_text("def present_function():\n    return True\n")

        assert (
            check_code_exists(f"`{target}:present_function`", repo_root=tmp_path)
            is True
        )

    def test_worker_home_path_maps_to_local_home(self, tmp_path: Path, monkeypatch):
        local_file = (
            tmp_path / "germline" / "membrane" / "receptors" / "tecton" / "SKILL.md"
        )
        local_file.parent.mkdir(parents=True)
        local_file.write_text("# Tecton\n")
        monkeypatch.setattr(Path, "home", lambda: tmp_path)

        assert (
            check_code_exists(
                "`/home/vivesca/germline/membrane/receptors/tecton/SKILL.md`",
                repo_root=tmp_path,
            )
            is True
        )


class TestReconcileSpecs:
    """reconcile_specs scans dispatched specs and updates status from Temporal."""

    def _workflow(self, status: str, verdict: str = ""):
        wf = MagicMock()
        wf.status = MagicMock()
        wf.status.name = status
        wf.search_attributes = {"mtor_verdict": [verdict]} if verdict else {}
        return wf

    def test_completed_accepted_becomes_done(self, tmp_path: Path):
        """COMPLETED + accepted verdict → status: done."""
        spec = tmp_path / "test-spec.md"
        spec.write_text("---\nstatus: dispatched\nworkflow_id: wf-123\n---\nBody\n")

        # Simulate what reconcile should do
        update_spec_status(spec, "done", verdict="accepted")

        text = spec.read_text()
        assert "status: done" in text
        assert "verdict: accepted" in text

    def test_dispatched_no_workflows_becomes_ready(self, tmp_path: Path):
        """dispatched + no workflows → status: ready, clear workflow_id."""
        spec_path = tmp_path / "test-dispatched-gone.md"
        spec_path.write_text("""---
name: test-dispatched-gone
status: dispatched
workflow_id: wf-12345
---
# Test Spec
""")

        spec_dict = {
            "name": "test-dispatched-gone",
            "path": str(spec_path),
            "status": "dispatched",
            "workflow_id": "wf-12345",
            "body": "# Test Spec",
        }

        # dry_run = True to avoid actual change
        result = reconcile_spec(spec_dict, dry_run=True)
        assert result["changed"] is True
        assert result["was"] == "dispatched"
        assert result["now"] == "ready"

    def test_completed_accepted_workflow_becomes_done(self, tmp_path: Path):
        """dispatched + workflow_id + accepted Temporal verdict → done."""
        spec_path = tmp_path / "accepted.md"
        spec_path.write_text(
            "---\nstatus: dispatched\nworkflow_id: wf-accepted\n---\nBody\n"
        )
        spec_dict = {
            "name": "accepted",
            "path": str(spec_path),
            "status": "dispatched",
            "workflow_id": "wf-accepted",
            "body": "Body",
        }

        result = reconcile_spec(
            spec_dict,
            workflow_description=self._workflow("COMPLETED", "accepted"),
        )

        assert result["changed"] is True
        assert result["now"] == "done"
        text = spec_path.read_text()
        assert "status: done" in text

    def test_completed_approved_workflow_becomes_done(self, tmp_path: Path):
        """dispatched + workflow_id + approved Temporal verdict → done."""
        spec_path = tmp_path / "approved.md"
        spec_path.write_text(
            "---\nstatus: dispatched\nworkflow_id: wf-approved\n---\nBody\n"
        )
        spec_dict = {
            "name": "approved",
            "path": str(spec_path),
            "status": "dispatched",
            "workflow_id": "wf-approved",
            "body": "Body",
        }

        result = reconcile_spec(
            spec_dict,
            workflow_description=self._workflow("COMPLETED", "approved"),
        )

        assert result["changed"] is True
        assert result["now"] == "done"
        assert result["reason"] == "workflow completed with approved verdict"
        assert "status: done" in spec_path.read_text()

    def test_completed_approved_with_flags_workflow_becomes_done(self, tmp_path: Path):
        """approved_with_flags is a policy-approved terminal verdict."""
        spec_path = tmp_path / "approved-with-flags.md"
        spec_path.write_text(
            "---\nstatus: dispatched\nworkflow_id: wf-approved-flags\n---\nBody\n"
        )
        spec_dict = {
            "name": "approved-with-flags",
            "path": str(spec_path),
            "status": "dispatched",
            "workflow_id": "wf-approved-flags",
            "body": "Body",
        }

        result = reconcile_spec(
            spec_dict,
            workflow_description=self._workflow("COMPLETED", "approved_with_flags"),
        )

        assert result["changed"] is True
        assert result["now"] == "done"
        assert "status: done" in spec_path.read_text()

    def test_completed_rejected_workflow_becomes_failed(self, tmp_path: Path):
        """dispatched + workflow_id + rejected Temporal verdict → failed."""
        spec_path = tmp_path / "rejected.md"
        spec_path.write_text(
            "---\nstatus: dispatched\nworkflow_id: wf-rejected\n---\nBody\n"
        )
        spec_dict = {
            "name": "rejected",
            "path": str(spec_path),
            "status": "dispatched",
            "workflow_id": "wf-rejected",
            "body": "Body",
        }

        result = reconcile_spec(
            spec_dict,
            workflow_description=self._workflow("COMPLETED", "rejected"),
        )

        assert result["changed"] is True
        assert result["now"] == "failed"
        assert "status: failed" in spec_path.read_text()

    def test_running_workflow_stays_dispatched(self, tmp_path: Path):
        """RUNNING workflow leaves the spec dispatched."""
        spec_path = tmp_path / "running.md"
        spec_path.write_text(
            "---\nstatus: dispatched\nworkflow_id: wf-running\n---\nBody\n"
        )
        spec_dict = {
            "name": "running",
            "path": str(spec_path),
            "status": "dispatched",
            "workflow_id": "wf-running",
            "body": "Body",
        }

        result = reconcile_spec(
            spec_dict,
            workflow_description=self._workflow("RUNNING"),
        )

        assert result["changed"] is False
        assert result["now"] == "dispatched"
        assert "status: dispatched" in spec_path.read_text()

    def test_missing_workflow_clears_workflow_id(self, tmp_path: Path):
        """Missing workflow returns spec to ready and clears stale workflow_id."""
        spec_path = tmp_path / "missing.md"
        spec_path.write_text(
            "---\nstatus: dispatched\nworkflow_id: wf-missing\n---\nBody\n"
        )
        spec_dict = {
            "name": "missing",
            "path": str(spec_path),
            "status": "dispatched",
            "workflow_id": "wf-missing",
            "body": "Body",
        }

        result = reconcile_spec(spec_dict)

        text = spec_path.read_text()
        assert result["changed"] is True
        assert result["now"] == "ready"
        assert "status: ready" in text
        assert "workflow_id:" not in text

    def test_reconcile_all_uses_known_workflow_descriptions(self, tmp_path: Path):
        """Opportunistic list reconciliation updates only workflows already listed."""
        matched = tmp_path / "matched.md"
        unmatched = tmp_path / "unmatched.md"
        matched.write_text(
            "---\nstatus: dispatched\nworkflow_id: wf-matched\n---\nBody\n"
        )
        unmatched.write_text(
            "---\nstatus: dispatched\nworkflow_id: wf-unmatched\n---\nBody\n"
        )

        result = reconcile_all(
            tmp_path,
            workflow_descriptions={
                "wf-matched": self._workflow("COMPLETED", "accepted")
            },
        )

        assert result["fixed"] == [
            {
                "name": "matched",
                "was": "dispatched",
                "now": "done",
                "reason": "workflow completed with accepted verdict",
            }
        ]
        assert "status: done" in matched.read_text()
        assert "status: dispatched" in unmatched.read_text()

    @integration
    def test_done_with_existing_code_no_warning(self, tmp_path: Path):
        """done + files exist → correct, no warning."""
        spec_path = tmp_path / "test-done-code-exists.md"
        spec_path.write_text("""---
name: test-done-code-exists
status: done
---
## Files to edit
- mtor/reconcile.py:check_code_exists
- mtor/cli.py
""")

        spec_dict = {
            "name": "test-done-code-exists",
            "path": str(spec_path),
            "status": "done",
            "body": spec_path.read_text(),
        }

        result = reconcile_spec(spec_dict, dry_run=True)
        assert result["changed"] is False
        assert result["warning"] is None

    def test_done_with_missing_code_has_warning(self, tmp_path: Path):
        """done + missing files → warning generated."""
        spec_path = tmp_path / "test-done-missing.md"
        spec_path.write_text("""---
name: test-done-missing
status: done
---
## Files to edit
- mtor/nonexistent.py
- mtor/reconcile.py:missing_function
""")

        spec_dict = {
            "name": "test-done-missing",
            "path": str(spec_path),
            "status": "done",
            "body": spec_path.read_text(),
        }

        result = reconcile_spec(spec_dict, dry_run=True)
        assert result["changed"] is False
        assert result["warning"] is not None
        assert "nonexistent.py" in result["warning"]
        assert "missing_function" in result["warning"]

    def test_done_with_backtick_absolute_file_has_no_warning(self, tmp_path: Path):
        """done + markdown-wrapped absolute Files to edit entry does not warn."""
        target = tmp_path / "done_target.py"
        target.write_text("VALUE = 1\n")
        spec_path = tmp_path / "test-done-backtick.md"
        spec_path.write_text(f"""---
name: test-done-backtick
status: done
---
## Files to edit
- `{target}`
""")

        spec_dict = {
            "name": "test-done-backtick",
            "path": str(spec_path),
            "status": "done",
            "body": spec_path.read_text(),
        }

        result = reconcile_spec(spec_dict, dry_run=True)
        assert result["changed"] is False
        assert result["warning"] is None

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
