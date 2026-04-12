"""Tests for _auto_commit in mtor.worker.translocase."""

import subprocess

from unittest.mock import MagicMock, patch

import pytest

from mtor.worker.translocase import _auto_commit


def test_commits_dirty_tree():
    """Dirty working tree: stages changes and commits, returns True."""
    status_ok = MagicMock(stdout="M file.py\n", returncode=0)
    add_ok = MagicMock(returncode=0)
    diff_dirty = MagicMock(returncode=1)  # --quiet exits 1 when diff exists
    commit_ok = MagicMock(returncode=0)

    with patch("mtor.worker.translocase._subprocess.run") as mock_run:
        mock_run.side_effect = [status_ok, add_ok, diff_dirty, commit_ok]

        result = _auto_commit("/repo", "wf-123")

    assert result is True
    mock_run.assert_any_call(
        ["git", "add", "-A"], capture_output=True, cwd="/repo", timeout=10,
    )
    mock_run.assert_any_call(
        ["git", "commit", "-m", "auto-commit: wf-123 (safety net)"],
        capture_output=True, text=True, cwd="/repo", timeout=30,
    )


def test_noop_clean_tree():
    """Clean working tree: no add/commit attempted, returns False."""
    status_clean = MagicMock(stdout="", returncode=0)

    with patch("mtor.worker.translocase._subprocess.run") as mock_run:
        mock_run.return_value = status_clean

        result = _auto_commit("/repo", "wf-456")

    assert result is False
    assert mock_run.call_count == 1  # only the status check


def test_noop_empty_staged():
    """Porcelain shows output but staged diff is empty: returns False."""
    status_dirty = MagicMock(stdout="M file.py\n", returncode=0)
    add_ok = MagicMock(returncode=0)
    diff_clean = MagicMock(returncode=0)  # --quiet exits 0 when no diff

    with patch("mtor.worker.translocase._subprocess.run") as mock_run:
        mock_run.side_effect = [status_dirty, add_ok, diff_clean]

        result = _auto_commit("/repo", "wf-789")

    assert result is False
    # git add called, but git commit should NOT be called
    mock_run.assert_any_call(
        ["git", "add", "-A"], capture_output=True, cwd="/repo", timeout=10,
    )
    assert mock_run.call_count == 3  # status + add + diff — no commit


def test_timeout_safe():
    """subprocess.TimeoutExpired propagates without corrupting state."""
    with patch("mtor.worker.translocase._subprocess.run") as mock_run:
        mock_run.side_effect = subprocess.TimeoutExpired(cmd="git", timeout=30)

        with pytest.raises(subprocess.TimeoutExpired):
            _auto_commit("/repo", "wf-timeout")
