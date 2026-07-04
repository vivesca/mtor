"""Assays for _checkpoint_worktree — salvage dirty ribosome worktrees before
destructive cleanup (SIGKILL / abnormal-exit path).

The ribosome wrapper's own stash-checkpoint only runs on clean nonzero exits;
when the wrapper dies by signal the harness used to reset or remove the
worktree with no salvage (2026-07-04: exit_code=-9 destroyed three
uncommitted test files).

Runs via: cd ~/code/mtor && uv run pytest assays/test_worktree_checkpoint.py -q
"""

from __future__ import annotations

import json
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


def _make_repo(tmp_path):
    repo = tmp_path / "repo"
    repo.mkdir()
    _git(repo, "init", "-q", "-b", "main")
    _git(repo, "config", "user.email", "assay@example.com")
    _git(repo, "config", "user.name", "assay")
    (repo / "tracked.py").write_text("x = 1\n")
    _git(repo, "add", "tracked.py")
    _git(repo, "commit", "-q", "-m", "base")
    return repo


def test_checkpoint_worktree_stashes_untracked_and_modified(tmp_path, monkeypatch):
    """Modified + untracked files land in a stash commit, an anchor ref, a
    patch file, and a checkpoint JSON that `mtor checkpoints` can list."""
    repo = _make_repo(tmp_path)
    cp_dir = tmp_path / "cps"
    monkeypatch.setattr(git_ops, "_CHECKPOINT_DIR", cp_dir)
    (repo / "tracked.py").write_text("x = 2\n")
    (repo / "assays_new.py").write_text("y = 3\n")

    ref = git_ops._checkpoint_worktree(
        str(repo), "wf-test-123", task="fix things", provider="zhipu", exit_code=-9
    )

    assert ref
    entry = json.loads((cp_dir / "wf-test-123.json").read_text())
    assert entry["workflow_id"] == "wf-test-123"
    assert entry["exit_code"] == -9
    assert entry["stash_ref"]
    diff = _git(
        repo, "diff", "--name-only", f"{entry['stash_ref']}~1", entry["stash_ref"]
    )
    assert "assays_new.py" in diff.stdout
    ref_check = _git(repo, "show-ref", "refs/ribosome-checkpoints/wf-test-123")
    assert ref_check.returncode == 0
    patch_text = (cp_dir / "wf-test-123.patch").read_text()
    assert "x = 2" in patch_text


def test_checkpoint_worktree_returns_none_when_clean(tmp_path, monkeypatch):
    """A clean tree produces no checkpoint artifacts."""
    repo = _make_repo(tmp_path)
    monkeypatch.setattr(git_ops, "_CHECKPOINT_DIR", tmp_path / "cps")
    assert git_ops._checkpoint_worktree(str(repo), "wf-clean") is None
    assert not (tmp_path / "cps" / "wf-clean.json").exists()


def test_checkpoint_worktree_survives_stale_index_lock(tmp_path, monkeypatch):
    """A stale index.lock from a killed git process does not block salvage."""
    repo = _make_repo(tmp_path)
    cp_dir = tmp_path / "cps"
    monkeypatch.setattr(git_ops, "_CHECKPOINT_DIR", cp_dir)
    (repo / "tracked.py").write_text("x = 99\n")
    (repo / ".git" / "index.lock").write_text("")

    ref = git_ops._checkpoint_worktree(str(repo), "wf-lock", exit_code=-9)

    assert ref
    assert (cp_dir / "wf-lock.json").exists()


def test_checkpoint_worktree_skips_runtime_denylist_paths(tmp_path, monkeypatch):
    """Runtime bookkeeping junk alone does not produce a checkpoint."""
    repo = _make_repo(tmp_path)
    monkeypatch.setattr(git_ops, "_CHECKPOINT_DIR", tmp_path / "cps")
    junk = repo / "loci" / "ribosome-outputs"
    junk.mkdir(parents=True)
    (junk / "run.txt").write_text("noise")
    assert git_ops._checkpoint_worktree(str(repo), "wf-junk") is None


def test_gc_worktrees_checkpoints_dirty_worktree_before_removal(tmp_path, monkeypatch):
    """gc salvages dirty orphaned worktrees before force-removing them."""
    repo = _make_repo(tmp_path)
    cp_dir = tmp_path / "cps"
    monkeypatch.setattr(git_ops, "_CHECKPOINT_DIR", cp_dir)
    wt = repo / ".worktrees" / "ribosome-gc-1"
    _git(repo, "worktree", "add", "-q", str(wt), "-b", "ribosome-gc-1")
    (wt / "uncommitted.py").write_text("z = 1\n")
    old = time.time() - 3 * 3600
    os.utime(wt, (old, old))

    git_ops._gc_worktrees(str(repo))

    assert not wt.exists()
    assert (cp_dir / "ribosome-gc-1.json").exists()


def test_translocase_checkpoints_before_cleanup_on_abnormal_exit():
    """Source-pattern gate: the rc != 0 cleanup path must checkpoint first."""
    import inspect

    from mtor.worker import translocase

    src = inspect.getsource(translocase)
    cleanup_idx = src.index("_cleanup_worktree(str(worktree_path))")
    checkpoint_idx = src.index("_checkpoint_worktree(")
    assert checkpoint_idx < cleanup_idx
