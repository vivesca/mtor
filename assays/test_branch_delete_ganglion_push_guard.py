"""Assays for the ganglion-push guard on the two `git branch -D` reaper sites in
mtor/worker/git_ops.py.

A committed-but-unpushed branch tip used to be force-deleted outright by
`_create_worktree` (stale-branch reuse) and `_gc_worktrees` (orphan cleanup),
becoming a dangling object `git gc` reaps and that autophagy.salvage can't
see (it only fetches/walks pushed ganglion/<branch> refs). The guard pushes
to `ganglion` first when the branch carries commits main doesn't have.

Runs via: cd ~/code/mtor && uv run pytest assays/test_branch_delete_ganglion_push_guard.py -q
"""

from __future__ import annotations

import os
import subprocess
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from mtor.worker import git_ops


def _git(repo, *args):
    return subprocess.run(
        ["git", "-C", str(repo), *args], capture_output=True, text=True, check=False
    )


def _make_repo_with_ganglion_remote(tmp_path):
    bare = tmp_path / "ganglion.git"
    _git(tmp_path, "init", "-q", "--bare", str(bare))

    repo = tmp_path / "repo"
    repo.mkdir()
    _git(repo, "init", "-q", "-b", "main")
    _git(repo, "config", "user.email", "assay@example.com")
    _git(repo, "config", "user.name", "assay")
    (repo / "tracked.py").write_text("x = 1\n")
    _git(repo, "add", "tracked.py")
    _git(repo, "commit", "-q", "-m", "base")
    _git(repo, "remote", "add", "ganglion", str(bare))
    return repo, bare


def test_gc_worktrees_pushes_unmerged_tip_to_ganglion_before_delete(tmp_path):
    repo, bare = _make_repo_with_ganglion_remote(tmp_path)
    branch = "ribosome-gc-tip"
    wt = repo / ".worktrees" / branch
    add = _git(repo, "worktree", "add", "-q", str(wt), "-b", branch)
    assert add.returncode == 0

    (wt / "committed_work.py").write_text("z = 1\n")
    _git(wt, "add", "committed_work.py")
    commit = _git(wt, "commit", "-q", "-m", "real work, never pushed")
    assert commit.returncode == 0

    old = time.time() - 3 * 3600
    os.utime(wt, (old, old))

    git_ops._gc_worktrees(str(repo))

    assert not wt.exists()
    on_ganglion = subprocess.run(
        ["git", "--git-dir", str(bare), "log", "--oneline", branch],
        capture_output=True,
        text=True,
    )
    assert on_ganglion.returncode == 0
    assert "real work, never pushed" in on_ganglion.stdout


def test_create_worktree_pushes_unmerged_tip_before_reusing_branch_name(tmp_path):
    repo, bare = _make_repo_with_ganglion_remote(tmp_path)
    branch = "ribosome-reuse-tip"

    first_path = git_ops._create_worktree(str(repo), branch)
    (Path(first_path) / "committed_work.py").write_text("y = 1\n")
    _git(Path(first_path), "add", "committed_work.py")
    commit = _git(Path(first_path), "commit", "-q", "-m", "unpushed reuse tip")
    assert commit.returncode == 0

    git_ops._create_worktree(str(repo), branch)

    on_ganglion = subprocess.run(
        ["git", "--git-dir", str(bare), "log", "--oneline", branch],
        capture_output=True,
        text=True,
    )
    assert on_ganglion.returncode == 0
    assert "unpushed reuse tip" in on_ganglion.stdout
