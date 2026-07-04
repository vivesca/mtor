"""Tests for GitHub Actions cost controls."""

from __future__ import annotations

from pathlib import Path


WORKFLOWS = Path(__file__).resolve().parents[1] / ".github" / "workflows"


def test_only_deterministic_ci_workflow_remains():
    """The repo should not spend Actions minutes on non-gating AI review."""
    workflows = sorted(path.name for path in WORKFLOWS.glob("*.yml"))

    assert workflows == ["ci.yml"]


def test_ci_skips_redundant_and_superseded_runs():
    """CI should keep safety gates while trimming duplicated GitHub minutes."""
    workflow = (WORKFLOWS / "ci.yml").read_text()

    assert "cancel-in-progress: true" in workflow
    assert "startsWith(github.event.head_commit.message, 'Merge pull request ')" in workflow
    assert "paths-ignore:" in workflow
    assert "pull_request:" in workflow
    assert "push:" in workflow
    assert "qodo-ai/pr-agent" not in workflow
    assert "OPENAI_KEY" not in workflow
