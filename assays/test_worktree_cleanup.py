"""Tests for failed-run worktree cleanup in translocase."""

from __future__ import annotations

from pathlib import Path
from unittest.mock import patch

from mtor.worker import translocase


def test_cleanup_removes_index_lock(tmp_path: Path):
    git_dir = tmp_path / ".git"
    git_dir.mkdir()
    lock = git_dir / "index.lock"
    lock.write_text("stale lock")

    with patch("mtor.worker.translocase._subprocess.run") as run:
        translocase._cleanup_worktree(str(tmp_path))

    assert not lock.exists()
    assert run.called


def test_cleanup_aborts_interrupted_rebase(tmp_path: Path):
    git_dir = tmp_path / ".git"
    (git_dir / "rebase-merge").mkdir(parents=True)

    with patch("mtor.worker.translocase._subprocess.run") as run:
        translocase._cleanup_worktree(str(tmp_path))

    commands = [call.args[0] for call in run.call_args_list]
    assert ["git", "rebase", "--abort"] in commands


def test_cleanup_resets_dirty_tree(tmp_path: Path):
    (tmp_path / ".git").mkdir()

    with patch("mtor.worker.translocase._subprocess.run") as run:
        translocase._cleanup_worktree(str(tmp_path))

    commands = [call.args[0] for call in run.call_args_list]
    assert ["git", "checkout", "--", "."] in commands
    assert ["git", "clean", "-fd"] in commands


def test_cleanup_noop_on_missing_dir(tmp_path: Path):
    missing = tmp_path / "missing"

    with patch("mtor.worker.translocase._subprocess.run") as run:
        translocase._cleanup_worktree(str(missing))

    run.assert_not_called()


def test_cleanup_skipped_on_success():
    source = Path(translocase.__file__).read_text()

    assert "if rc != 0 and worktree_path:" in source
    assert "_cleanup_worktree" in source
