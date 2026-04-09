"""Tests for worktree fallback behavior — fail, don't fall back to main.

Runs via: cd ~/code/mtor && uv run pytest assays/test_worktree_fallback.py -x
"""

from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))


class TestWorktreeFallback:
    """Build tasks must fail if worktree creation fails, not run on main."""

    def test_worktree_failure_raises_not_falls_back(self):
        """When _create_worktree raises, translate should propagate the error."""
        # Will verify that RuntimeError propagates instead of silently
        # falling back to repo_root
        pass

    def test_scout_mode_still_runs_on_main(self):
        """Scout mode (read-only) should still run on main without worktree."""
        pass
