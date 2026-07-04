"""Tests for _reap_orphaned_worktree_processes in mtor.worker.translocase.

The reaper kills opencode/timeout process groups whose cwd resolves to a
deleted worktree directory. We never touch a real process — every external
call (ps, readlink, isdir, signal) is patched on the translocase module.
"""

from __future__ import annotations

import types
from unittest.mock import patch

import mtor.worker.translocase as translocase


def _ps_output(rows: list[tuple[int, int, str]]) -> str:
    header = "  PID  PGID COMMAND\n"
    lines = [f"{pid:>5} {pgid:>5} {comm}" for pid, pgid, comm in rows]
    return header + "\n".join(lines)


def test_reap_orphaned_worktree_processes_kills_group_when_cwd_deleted() -> None:
    repo_root = "/home/vivesca/fake-repo"
    ps_completed = _ps_output([(4242, 4242, "opencode")])
    fake_run = types.SimpleNamespace(stdout=ps_completed, stderr="", returncode=0)

    def fake_readlink(path: str) -> str:
        return f"{repo_root}/.worktrees/ribosome-foo (deleted)"

    with (
        patch.object(translocase._subprocess, "run", return_value=fake_run),
        patch.object(translocase.os, "readlink", side_effect=fake_readlink),
        patch.object(translocase.os.path, "isdir", return_value=False),
        patch.object(translocase, "_signal_group", return_value=True) as sig,
    ):
        result = translocase._reap_orphaned_worktree_processes(repo_root)

    assert result["scanned"] == 1
    assert 4242 in result["killed_pgids"]
    sig.assert_called_once_with(4242, translocase._signal.SIGKILL)
    assert result["errors"] == []


def test_reap_orphaned_worktree_processes_skips_live_worktree() -> None:
    repo_root = "/home/vivesca/fake-repo"
    ps_completed = _ps_output([(5353, 5353, "opencode")])
    fake_run = types.SimpleNamespace(stdout=ps_completed, stderr="", returncode=0)
    live_path = f"{repo_root}/.worktrees/ribosome-live"

    def fake_readlink(path: str) -> str:
        return live_path

    with (
        patch.object(translocase._subprocess, "run", return_value=fake_run),
        patch.object(translocase.os, "readlink", side_effect=fake_readlink),
        patch.object(translocase.os.path, "isdir", return_value=True),
        patch.object(translocase, "_signal_group", return_value=True) as sig,
    ):
        result = translocase._reap_orphaned_worktree_processes(repo_root)

    assert result["scanned"] == 1
    assert result["killed_pgids"] == []
    sig.assert_not_called()
    assert result["errors"] == []


def test_reap_orphaned_worktree_processes_skips_unrelated_process_names() -> None:
    repo_root = "/home/vivesca/fake-repo"
    ps_completed = _ps_output([(6464, 6464, "bash")])
    fake_run = types.SimpleNamespace(stdout=ps_completed, stderr="", returncode=0)

    def fake_readlink(path: str) -> str:
        return f"{repo_root}/.worktrees/ribosome-foo (deleted)"

    with (
        patch.object(translocase._subprocess, "run", return_value=fake_run),
        patch.object(translocase.os, "readlink", side_effect=fake_readlink),
        patch.object(translocase.os.path, "isdir", return_value=False),
        patch.object(translocase, "_signal_group", return_value=True) as sig,
    ):
        result = translocase._reap_orphaned_worktree_processes(repo_root)

    assert result["scanned"] == 0
    assert result["killed_pgids"] == []
    sig.assert_not_called()
    assert result["errors"] == []


def test_reap_orphaned_worktree_processes_handles_no_matches() -> None:
    repo_root = "/home/vivesca/fake-repo"
    fake_run = types.SimpleNamespace(
        stdout="  PID  PGID COMMAND\n", stderr="", returncode=0
    )

    with (
        patch.object(translocase._subprocess, "run", return_value=fake_run),
        patch.object(translocase.os, "readlink") as readlink,
        patch.object(translocase.os.path, "isdir") as isdir,
        patch.object(translocase, "_signal_group") as sig,
    ):
        result = translocase._reap_orphaned_worktree_processes(repo_root)

    assert result == {"scanned": 0, "killed_pgids": [], "errors": []}
    readlink.assert_not_called()
    isdir.assert_not_called()
    sig.assert_not_called()
