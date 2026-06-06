"""Tests for _auto_commit in mtor.worker.translocase."""

import subprocess

from unittest.mock import MagicMock, patch

from mtor.worker.git_ops import _status_paths_for_auto_commit
from mtor.worker.translocase import _auto_commit, _mode_allows_auto_commit


def test_commits_dirty_tree():
    """Dirty working tree: stages changes and commits, returns True."""
    # _auto_commit first resolves the branch (refuses on main/master), so the
    # mock sequence leads with a rev-parse result on a feature branch.
    branch_ok = MagicMock(stdout="ribosome-wf-123\n", returncode=0)
    status_ok = MagicMock(stdout=" M file.py\n", returncode=0)
    add_ok = MagicMock(returncode=0)
    diff_dirty = MagicMock(returncode=1)  # --quiet exits 1 when diff exists
    commit_ok = MagicMock(returncode=0)

    with patch("mtor.worker.translocase._subprocess.run") as mock_run:
        mock_run.side_effect = [branch_ok, status_ok, add_ok, diff_dirty, commit_ok]

        result = _auto_commit("/repo", "wf-123")

    assert result is True
    calls = mock_run.call_args_list
    # 5-step sequence: branch check, status, add, diff --cached --quiet, commit.
    # Commands run through _run_worker_command, which injects an env= kwarg for
    # git, so assert on the command vector + key kwargs rather than full call().
    assert [c.args[0] for c in calls] == [
        ["git", "rev-parse", "--abbrev-ref", "HEAD"],
        ["git", "status", "--porcelain"],
        ["git", "add", "--", "file.py"],
        ["git", "diff", "--cached", "--quiet"],
        ["git", "commit", "--no-verify", "-m", "ribosome: wf-123"],
    ]
    # Every git call is pinned to the target repo.
    assert all(c.kwargs["cwd"] == "/repo" for c in calls)
    # add + commit are check=True (raise on failure); commit gets the long timeout.
    assert calls[2].kwargs.get("check") is True
    assert calls[4].kwargs.get("check") is True
    assert calls[4].kwargs.get("timeout") == 30


def test_noop_clean_tree():
    """Clean working tree: no add/commit attempted, returns False."""
    branch_ok = MagicMock(stdout="ribosome-wf-456\n", returncode=0)
    status_clean = MagicMock(stdout="", returncode=0)

    with patch("mtor.worker.translocase._subprocess.run") as mock_run:
        mock_run.side_effect = [branch_ok, status_clean]

        result = _auto_commit("/repo", "wf-456")

    assert result is False
    assert mock_run.call_count == 2  # branch check + status check, then bail


def test_noop_empty_staged():
    """Porcelain shows output but staged diff is empty: returns False."""
    branch_ok = MagicMock(stdout="ribosome-wf-789\n", returncode=0)
    status_dirty = MagicMock(stdout=" M file.py\n", returncode=0)
    add_ok = MagicMock(returncode=0)
    diff_clean = MagicMock(returncode=0)  # --quiet exits 0 when no diff

    with patch("mtor.worker.translocase._subprocess.run") as mock_run:
        mock_run.side_effect = [branch_ok, status_dirty, add_ok, diff_clean]

        result = _auto_commit("/repo", "wf-789")

    assert result is False
    assert mock_run.call_count == 4  # branch + status + add + diff — no commit


def test_refuses_to_commit_on_main():
    """Branch safety: never auto-commit when HEAD is on main/master."""
    branch_main = MagicMock(stdout="main\n", returncode=0)

    with patch("mtor.worker.translocase._subprocess.run") as mock_run:
        mock_run.side_effect = [branch_main]

        result = _auto_commit("/repo", "wf-main")

    assert result is False
    assert mock_run.call_count == 1  # branch check only, then bail before status


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
    branch_ok = MagicMock(stdout="ribosome-wf-runtime\n", returncode=0)
    status_dirty = MagicMock(
        stdout=" M loci/ribosome-runs.jsonl\n?? loci/ribosome-dossiers/task.json\n",
        returncode=0,
    )

    with patch("mtor.worker.translocase._subprocess.run") as mock_run:
        mock_run.side_effect = [branch_ok, status_dirty]

        result = _auto_commit("/repo", "wf-runtime")

    assert result is False
    # branch + status; all paths are runtime artifacts → filtered out → no add.
    assert mock_run.call_count == 2


def test_read_only_modes_do_not_allow_auto_commit():
    """Scout/research runs execute in the main repo and must stay read-only."""
    assert _mode_allows_auto_commit("build") is True
    assert _mode_allows_auto_commit("scout") is False
    assert _mode_allows_auto_commit("research") is False
