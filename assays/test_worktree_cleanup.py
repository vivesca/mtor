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


def test_auto_commit_precedes_cleanup():
    source = Path(translocase.__file__).read_text()

    # _auto_commit must appear before _cleanup_worktree in translate()
    auto_commit_pos = source.index("auto_committed = _auto_commit(")
    cleanup_pos = source.index("_cleanup_worktree(str(worktree_path))")
    assert auto_commit_pos < cleanup_pos, (
        "_auto_commit must execute before _cleanup_worktree"
    )

    # Cleanup must be guarded by `not auto_committed`
    assert "not auto_committed" in source


def test_cleanup_skipped_on_success():
    source = Path(translocase.__file__).read_text()

    assert "if rc != 0 and worktree_path and not auto_committed:" in source
    assert "_cleanup_worktree" in source
