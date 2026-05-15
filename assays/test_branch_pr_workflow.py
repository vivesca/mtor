"""Tests for branch-to-PR workflow wiring."""

from __future__ import annotations

import inspect
import subprocess
from types import SimpleNamespace
from unittest.mock import patch

from mtor.worker import workflow
from mtor.worker.translocase import _create_pr_impl


def test_workflow_creates_pr_instead_of_auto_merging():
    """Approved branch results should open PRs, not merge directly to main."""
    source = inspect.getsource(workflow.TranslationWorkflow._execute_one)

    assert "create_pr" in source
    assert "pr_url" in source
    assert "merged\": False" in source
    assert "merge_approved" not in source


def test_worker_registers_create_pr_activity():
    """Temporal worker exposes the PR activity used by TranslationWorkflow."""
    import mtor.worker.translocase as translocase

    source = inspect.getsource(translocase.main)

    assert "create_pr" in source
    assert "activities=[translate, chaperone, merge_approved, create_pr, watch_cycle]" in source


def test_create_pr_impl_pushes_branch_and_invokes_gh(tmp_path):
    """The PR helper pushes the branch before calling gh pr create."""
    calls: list[list[str]] = []

    def fake_run(cmd, **kwargs):
        calls.append(cmd)
        if cmd[:3] == ["git", "log", "--oneline"]:
            return SimpleNamespace(returncode=0, stdout="abc123 change\n", stderr="")
        if cmd[:3] == ["git", "push", "origin"]:
            return SimpleNamespace(returncode=0, stdout="", stderr="")
        if cmd[:3] == ["gh", "pr", "create"]:
            return SimpleNamespace(
                returncode=0,
                stdout="https://github.com/vivesca/mtor/pull/42\n",
                stderr="",
            )
        raise AssertionError(f"unexpected command: {cmd}")

    with patch("mtor.worker.translocase._subprocess.run", side_effect=fake_run):
        result = _create_pr_impl(
            str(tmp_path),
            "ribosome-test",
            title="Implement thing",
            body="Dispatched by mtor",
        )

    assert result["created"] is True
    assert result["pr_url"] == "https://github.com/vivesca/mtor/pull/42"
    assert result["pr_number"] == 42
    assert calls[1] == ["git", "push", "origin", "ribosome-test"]
    assert calls[2][:3] == ["gh", "pr", "create"]
    assert "--base" in calls[2]
    assert "main" in calls[2]


def test_create_pr_impl_skips_empty_branch(tmp_path):
    """No PR is created when the branch has no commits ahead of main."""
    def fake_run(cmd, **kwargs):
        if cmd[:3] == ["git", "log", "--oneline"]:
            return subprocess.CompletedProcess(cmd, 0, stdout="", stderr="")
        raise AssertionError(f"unexpected command: {cmd}")

    with patch("mtor.worker.translocase._subprocess.run", side_effect=fake_run):
        result = _create_pr_impl(str(tmp_path), "ribosome-empty")

    assert result["created"] is False
    assert result["skipped"] is True
    assert result["pr_url"] == ""
