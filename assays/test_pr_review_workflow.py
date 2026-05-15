"""Tests for GitHub PR review workflow configuration."""

from __future__ import annotations

from pathlib import Path


def test_pr_review_workflow_uses_current_pr_agent_action():
    """AI review workflow should use the maintained PR-Agent action."""
    workflow = (Path(__file__).resolve().parents[1] / ".github" / "workflows" / "pr-review.yml").read_text()

    assert "uses: qodo-ai/pr-agent@main" in workflow
    assert "OPENAI_KEY:" in workflow
    assert "pull-requests: write" in workflow
    assert "pull_request:" in workflow
    assert "pull_request_target" not in workflow
