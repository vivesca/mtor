"""Tests for _auto_commit wiring in translate activity.

Runs via: cd ~/code/mtor && uv run pytest assays/test_auto_commit_wiring.py -x
"""

from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from mtor.worker.translocase import _auto_commit


class TestAutoCommitWiring:
    """_auto_commit is called after successful ribosome exit."""

    def test_auto_commit_creates_commit_on_dirty_worktree(self, tmp_path: Path):
        """_auto_commit stages and commits when there are changes."""
        import subprocess

        # Set up a git repo with a dirty file
        subprocess.run(["git", "init"], cwd=tmp_path, check=True, capture_output=True)
        subprocess.run(["git", "config", "user.email", "test@test.com"], cwd=tmp_path, check=True, capture_output=True)
        subprocess.run(["git", "config", "user.name", "Test"], cwd=tmp_path, check=True, capture_output=True)
        (tmp_path / "file.py").write_text("print('hello')")
        subprocess.run(["git", "add", "."], cwd=tmp_path, check=True, capture_output=True)
        subprocess.run(["git", "commit", "-m", "initial"], cwd=tmp_path, check=True, capture_output=True)

        # Make a change
        (tmp_path / "file.py").write_text("print('changed')")

        result = _auto_commit(str(tmp_path), "wf-test-123")
        assert result is True

        # Verify commit exists
        log = subprocess.run(["git", "log", "--oneline", "-1"], cwd=tmp_path, capture_output=True, text=True)
        assert "auto-commit" in log.stdout.lower() or "wf-test-123" in log.stdout

    def test_auto_commit_noop_on_clean_worktree(self, tmp_path: Path):
        """_auto_commit returns False when nothing to commit."""
        import subprocess

        subprocess.run(["git", "init"], cwd=tmp_path, check=True, capture_output=True)
        subprocess.run(["git", "config", "user.email", "test@test.com"], cwd=tmp_path, check=True, capture_output=True)
        subprocess.run(["git", "config", "user.name", "Test"], cwd=tmp_path, check=True, capture_output=True)
        (tmp_path / "file.py").write_text("print('hello')")
        subprocess.run(["git", "add", "."], cwd=tmp_path, check=True, capture_output=True)
        subprocess.run(["git", "commit", "-m", "initial"], cwd=tmp_path, check=True, capture_output=True)

        result = _auto_commit(str(tmp_path), "wf-test-456")
        assert result is False
