"""Tests for _auto_commit in mtor.worker.translocase."""

import subprocess

from unittest.mock import MagicMock, call, patch

from mtor.worker.git_ops import _status_paths_for_auto_commit
from mtor.worker.translocase import _auto_commit, _mode_allows_auto_commit


def test_commits_dirty_tree():
    """Dirty working tree: stages changes and commits, returns True."""
    status_ok = MagicMock(stdout=" M file.py\n", returncode=0)
    add_ok = MagicMock(returncode=0)
    diff_dirty = MagicMock(returncode=1)  # --quiet exits 1 when diff exists
    commit_ok = MagicMock(returncode=0)

    with patch("mtor.worker.translocase._subprocess.run") as mock_run:
        mock_run.side_effect = [status_ok, add_ok, diff_dirty, commit_ok]

        result = _auto_commit("/repo", "wf-123")

    assert result is True
    # Verify the 4-step sequence: status, add, diff --cached --quiet, commit
    calls = mock_run.call_args_list
    assert len(calls) == 4
    assert calls[0] == call(
        ["git", "status", "--porcelain"],
        cwd="/repo", capture_output=True, text=True, timeout=10,
    )
    assert calls[1] == call(["git", "add", "--", "file.py"], cwd="/repo", check=True, timeout=10)
    assert calls[2] == call(
        ["git", "diff", "--cached", "--quiet"], cwd="/repo", timeout=10,
    )
    assert calls[3] == call(
        ["git", "commit", "--no-verify", "-m", "ribosome: wf-123"],
        cwd="/repo", check=True, timeout=30,
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
    assert mock_run.call_count == 3  # status + add + diff — no commit


def test_timeout_returns_false():
    """subprocess.TimeoutExpired is caught and returns False (no corruption)."""
    with patch("mtor.worker.translocase._subprocess.run") as mock_run:
        mock_run.side_effect = subprocess.TimeoutExpired(cmd="git", timeout=30)

        result = _auto_commit("/repo", "wf-timeout")

    # Implementation catches all exceptions and returns False
    assert result is False


def test_status_paths_exclude_ribosome_runtime_artifacts():
    """Auto-commit stages explicit paths but skips ribosome bookkeeping."""
    status = "\n".join(
        [
            " M mtor/worker/git_ops.py",
            "?? assays/test_auto_commit.py",
            " M loci/ribosome-runs.jsonl",
            "?? loci/ribosome-dossiers/task.json",
            " D loci/ribosome-heartbeats/1234",
        ]
    )

    assert _status_paths_for_auto_commit(status) == [
        "mtor/worker/git_ops.py",
        "assays/test_auto_commit.py",
    ]


def test_noop_when_only_runtime_artifacts_changed():
    """Dirty runtime-only tree is intentionally not committed."""
    status_dirty = MagicMock(
        stdout=" M loci/ribosome-runs.jsonl\n?? loci/ribosome-dossiers/task.json\n",
        returncode=0,
    )

    with patch("mtor.worker.translocase._subprocess.run") as mock_run:
        mock_run.return_value = status_dirty

        result = _auto_commit("/repo", "wf-runtime")

    assert result is False
    assert mock_run.call_count == 1


def test_read_only_modes_do_not_allow_auto_commit():
    """Scout/research runs execute in the main repo and must stay read-only."""
    assert _mode_allows_auto_commit("build") is True
    assert _mode_allows_auto_commit("scout") is False
    assert _mode_allows_auto_commit("research") is False
