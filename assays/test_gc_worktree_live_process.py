"""Assays for _worktree_has_live_process and the _gc_worktrees live-process
guard.

A still-running ribosome can have its worktree removed out from under it
because the GC age threshold (7200s) equals the activity/wall-clock
timeout. The guard skips worktrees whose cwd is held by a live process.

Runs via: cd ~/code/mtor && uv run pytest assays/test_gc_worktree_live_process.py -q
"""

from __future__ import annotations

import os
import sys
import time
from pathlib import Path
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from mtor.worker import git_ops


def test_worktree_has_live_process_true_when_proc_cwd_matches(tmp_path):
    worktree_dir = tmp_path / "worktree"
    worktree_dir.mkdir()
    proc_root = tmp_path / "proc"
    pid_dir = proc_root / "123"
    pid_dir.mkdir(parents=True)
    (pid_dir / "cwd").symlink_to(worktree_dir)

    assert (
        git_ops._worktree_has_live_process(str(worktree_dir), proc_root=proc_root)
        is True
    )


def test_worktree_has_live_process_false_when_no_match(tmp_path):
    worktree_dir = tmp_path / "worktree"
    worktree_dir.mkdir()
    elsewhere = tmp_path / "elsewhere"
    elsewhere.mkdir()
    proc_root = tmp_path / "proc"
    pid_dir = proc_root / "123"
    pid_dir.mkdir(parents=True)
    (pid_dir / "cwd").symlink_to(elsewhere)

    assert (
        git_ops._worktree_has_live_process(str(worktree_dir), proc_root=proc_root)
        is False
    )


def test_gc_worktrees_skips_worktree_with_live_process(tmp_path):
    wt_base = tmp_path / ".worktrees"
    wt_dir = wt_base / "ribosome-abc123"
    wt_dir.mkdir(parents=True)

    old_time = time.time() - 100000
    os.utime(wt_dir, (old_time, old_time))

    with (
        patch("mtor.worker.git_ops._worktree_has_live_process", return_value=True),
        patch("mtor.worker.git_ops._run_worker_command") as mock_run,
    ):
        git_ops._gc_worktrees(str(tmp_path))

    mock_run.assert_not_called()
    assert wt_dir.exists()
