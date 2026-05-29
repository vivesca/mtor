"""Tests for mtor/workflow_gate.py — trigger classification and ledger writing."""

from __future__ import annotations

from pathlib import Path

import pytest

from mtor.workflow_gate import assess_workflow_trigger, write_workflow_ledger


def test_assess_workflow_trigger_recommends_for_repo_wide_security_audit() -> None:
    """Repo-wide security audit prompt routes to 'workflow' with security_audit and adversarial_review triggers."""
    result = assess_workflow_trigger(
        "Run a repo-wide security audit and adversarial review before merge"
    )
    assert result["route"] == "workflow"
    assert "security_audit" in result["triggers"]
    assert "adversarial_review" in result["triggers"]
    assert result["required_controls"] == [
        "resumable_ledger",
        "independent_verification",
        "explicit_cost_visibility",
        "human_or_admin_enablement",
    ]


def test_assess_workflow_trigger_keeps_small_tasks_single_lane() -> None:
    """Routine small work like fixing a typo routes to 'single' with no triggers or controls."""
    result = assess_workflow_trigger("Fix the typo in README")
    assert result["route"] == "single"
    assert result["triggers"] == []
    assert result["required_controls"] == []


def test_write_workflow_ledger_creates_resumable_markdown(tmp_path: Path) -> None:
    """Ledger creation writes a Markdown file with task, triggers, controls, and all required sections."""
    result = write_workflow_ledger(
        "Run a repo-wide security audit",
        tmp_path,
    )
    assert result["route"] == "workflow"
    assert "security_audit" in result["triggers"]

    ledger_path = Path(result["path"])
    assert ledger_path.exists()

    content = ledger_path.read_text()
    assert "# Workflow Ledger" in content
    assert "## Task" in content
    assert "Run a repo-wide security audit" in content
    assert "## Trigger Assessment" in content
    assert "## Subtasks" in content
    assert "## Evidence" in content
    assert "## Verification" in content
    assert "## Decisions" in content
    assert "## Open Loops" in content
    assert "resumable_ledger" in content
    assert "security_audit" in content


def test_write_workflow_ledger_refuses_to_overwrite(tmp_path: Path) -> None:
    """Ledger creation raises FileExistsError when the target file already exists."""
    prompt = "Security audit and hardening"
    result = write_workflow_ledger(prompt, tmp_path)
    ledger_path = Path(result["path"])

    # The ledger file now exists; writing again with same dir should fail
    # because the deterministic filename collides within the same second.
    # To guarantee collision, write the same file explicitly.
    ledger_path.write_text("existing content")

    with pytest.raises(FileExistsError):
        write_workflow_ledger(prompt, tmp_path)
