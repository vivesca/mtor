"""Tests for HEAD-moved fallback commit detection.

When _git_snapshot misses commits (ribosome committed on main, main..HEAD empty,
or git lock contention caused silent exception), the translate function falls back
to comparing pre_sha vs current HEAD to detect commits.

Runs via: cd ~/code/mtor && uv run pytest assays/test_head_moved_fallback.py -v
"""

from __future__ import annotations

import asyncio
import subprocess
from unittest.mock import patch


def _run(coro):
    return asyncio.run(coro)


import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from mtor.worker.translocase import chaperone


class TestChaperoneHeadMovedFallback:
    """Chaperone should NOT flag no_commit_on_success when head_moved_fallback is set."""

    def test_head_moved_fallback_not_flagged_no_commit(self):
        """When post_diff has head_moved_fallback=True, no_commit_on_success should not fire."""
        result = {
            "success": True,
            "exit_code": 0,
            "stdout": "Done. Committed changes.",
            "stderr": "",
            "task": "Implement feature X",
            "provider": "zhipu",
            "post_diff": {
                "stat": "",
                "numstat": "",
                "commits": [],
                "commit_count": 3,
                "head_moved_fallback": True,
            },
            "pre_diff": {"stat": "", "numstat": ""},
            "branch_name": "",
            "cost_info": "",
        }
        review = _run(chaperone(result))
        assert "no_commit_on_success" not in review["flags"]

    def test_zero_commits_no_fallback_still_flagged(self):
        """Without head_moved_fallback, zero commits on success is still flagged."""
        result = {
            "success": True,
            "exit_code": 0,
            "stdout": "Done.",
            "stderr": "",
            "task": "Implement feature X",
            "provider": "zhipu",
            "post_diff": {
                "stat": "",
                "numstat": "",
                "commits": [],
                "commit_count": 0,
            },
            "pre_diff": {"stat": "", "numstat": ""},
            "branch_name": "",
            "cost_info": "",
        }
        review = _run(chaperone(result))
        assert "no_commit_on_success" in review["flags"]


class TestWorktreeRetry:
    """_create_worktree retries on git lock contention."""

    def test_succeeds_on_second_attempt(self, tmp_path):
        """Worktree creation should retry and succeed after transient failure."""
        from mtor.worker.translocase import _create_worktree

        # Set up a real git repo
        subprocess.run(["git", "init", str(tmp_path)], capture_output=True)
        subprocess.run(
            ["git", "commit", "--allow-empty", "-m", "init"],
            capture_output=True, cwd=str(tmp_path),
        )

        call_count = 0
        original_run = subprocess.run

        def mock_run(cmd, **kwargs):
            nonlocal call_count
            if cmd[:3] == ["git", "worktree", "add"]:
                call_count += 1
                if call_count == 1:
                    return subprocess.CompletedProcess(
                        cmd, returncode=128,
                        stdout="", stderr="fatal: index.lock: File exists",
                    )
            return original_run(cmd, **kwargs)

        with patch("mtor.worker.translocase._subprocess.run", side_effect=mock_run):
            with patch("mtor.worker.translocase._time.sleep"):
                result = _create_worktree(str(tmp_path), "test-branch", retries=3)

        assert result.endswith("test-branch")
        assert call_count == 2

    def test_raises_after_all_retries_exhausted(self, tmp_path):
        """Worktree creation should raise after exhausting all retries."""
        from mtor.worker.translocase import _create_worktree

        subprocess.run(["git", "init", str(tmp_path)], capture_output=True)
        subprocess.run(
            ["git", "commit", "--allow-empty", "-m", "init"],
            capture_output=True, cwd=str(tmp_path),
        )

        original_run = subprocess.run

        def always_fail(cmd, **kwargs):
            if cmd[:3] == ["git", "worktree", "add"]:
                return subprocess.CompletedProcess(
                    cmd, returncode=128,
                    stdout="", stderr="fatal: index.lock: File exists",
                )
            return original_run(cmd, **kwargs)

        with patch("mtor.worker.translocase._subprocess.run", side_effect=always_fail):
            with patch("mtor.worker.translocase._time.sleep"):
                try:
                    _create_worktree(str(tmp_path), "test-branch", retries=2)
                    assert False, "Should have raised RuntimeError"
                except RuntimeError as exc:
                    assert "2 attempts" in str(exc)
                    assert "index.lock" in str(exc)


class TestGitSnapshotLogging:
    """_git_snapshot logs exceptions instead of silently swallowing them."""

    def test_exception_logged_to_stderr(self, capsys):
        """When _git_snapshot catches an exception, it should print a warning."""
        from mtor.worker.translocase import _git_snapshot

        # Call with a nonexistent directory to trigger an exception
        result = _git_snapshot("/nonexistent/path/that/does/not/exist")
        assert result["commit_count"] == 0
        assert result["stat"] == ""
        captured = capsys.readouterr()
        assert "WARNING" in captured.err or "failed" in captured.err.lower()
