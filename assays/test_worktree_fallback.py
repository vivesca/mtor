"""Tests for worktree fallback behavior — fail, don't fall back to main.

Runs via: cd ~/code/mtor && uv run pytest assays/test_worktree_fallback.py -x
"""

from __future__ import annotations

import asyncio
import subprocess
import sys
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))


def _run(coro):
    return asyncio.run(coro)


def _mock_pre_worktree_run(cmd, **kwargs):
    if cmd[0] == "pgrep":
        return subprocess.CompletedProcess(cmd, returncode=0, stdout="0\n", stderr="")
    if cmd[0] == "bash" and "-n" in cmd:
        return subprocess.CompletedProcess(cmd, returncode=0, stdout="", stderr="")
    raise AssertionError(f"unexpected subprocess before worktree gate: {cmd}")


class TestWorktreeFallback:
    """Build tasks must fail if worktree creation fails, not run on main."""

    def test_worktree_failure_raises_not_falls_back(self, tmp_path):
        """When _create_worktree raises, translate should propagate the error."""
        from mtor.worker.translocase import translate

        mock_info = MagicMock()
        mock_info.workflow_id = "test-worktree-failure"

        with patch("mtor.worker.translocase._subprocess.run", side_effect=_mock_pre_worktree_run), \
             patch("mtor.worker.translocase.activity.info", return_value=mock_info), \
             patch("mtor.worker.translocase.create_task_trace", return_value=None), \
             patch("mtor.worker.translocase._create_worktree", side_effect=RuntimeError("index.lock")):
            with pytest.raises(RuntimeError, match="Build tasks must not run on main"):
                _run(translate("Make a small safe edit", "zhipu", "build", str(tmp_path)))

    def test_scout_mode_still_runs_on_main(self, tmp_path):
        """Scout mode (read-only) should still run on main without worktree."""
        from mtor.worker.translocase import translate

        mock_info = MagicMock()
        mock_info.workflow_id = "test-scout"

        with patch("mtor.worker.translocase._subprocess.run", side_effect=_mock_pre_worktree_run), \
             patch("mtor.worker.translocase.activity.info", return_value=mock_info), \
             patch("mtor.worker.translocase.create_task_trace", return_value=None), \
             patch("mtor.worker.translocase._create_worktree") as create_worktree, \
             patch("mtor.worker.translocase._detect_prior_commits", side_effect=RuntimeError("scout reached repo root")):
            with pytest.raises(RuntimeError, match="scout reached repo root"):
                _run(translate("Inspect the repository", "zhipu", "scout", str(tmp_path)))

        create_worktree.assert_not_called()
