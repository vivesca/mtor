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

import pytest

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


def _checkpoint_entries(cp_dir: Path, workflow_id: str) -> list[dict]:
    return [
        json.loads(path.read_text())
        for path in sorted(cp_dir.glob(f"*-{workflow_id}.json"))
    ]


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
    [entry] = _checkpoint_entries(cp_dir, "wf-test-123")
    assert entry["workflow_id"] == "wf-test-123"
    assert entry["exit_code"] == -9
    assert entry["stash_ref"] == ref
    diff = _git(
        repo, "diff", "--name-only", f"{entry['stash_ref']}~1", entry["stash_ref"]
    )
    assert "assays_new.py" in diff.stdout
    ref_check = _git(repo, "show-ref", "--verify", entry["git_ref"])
    assert ref_check.returncode == 0
    patch_text = Path(entry["patch_file"]).read_text()
    assert "x = 2" in patch_text


def test_checkpoint_captures_quoted_untracked_binary_path(tmp_path, monkeypatch):
    """NUL-safe status parsing preserves spaces and binary file content."""
    repo = _make_repo(tmp_path)
    cp_dir = tmp_path / "cps"
    monkeypatch.setattr(git_ops, "_CHECKPOINT_DIR", cp_dir)
    (repo / "tracked.py").write_text("x = 2\n")
    binary = b"\x00valuable\xffcontent\n"
    (repo / "valuable file.bin").write_bytes(binary)

    ref = git_ops._checkpoint_worktree(str(repo), "wf-quoted")

    assert ref
    captured = subprocess.run(
        ["git", "-C", str(repo), "show", f"{ref}:valuable file.bin"],
        capture_output=True,
        check=False,
    )
    assert captured.returncode == 0
    assert captured.stdout == binary


def test_checkpoint_attempts_keep_immutable_refs_and_metadata(tmp_path, monkeypatch):
    """A later retry cannot overwrite an earlier attempt's recovery state."""
    repo = _make_repo(tmp_path)
    cp_dir = tmp_path / "cps"
    monkeypatch.setattr(git_ops, "_CHECKPOINT_DIR", cp_dir)
    (repo / "attempt-one.py").write_text("attempt = 1\n")
    first = git_ops._checkpoint_worktree(str(repo), "wf-attempts", exit_code=-15)
    assert first

    _git(repo, "reset", "--hard", "HEAD")
    _git(repo, "clean", "-fd")
    (repo / "attempt-two.py").write_text("attempt = 2\n")
    second = git_ops._checkpoint_worktree(str(repo), "wf-attempts", exit_code=-9)

    assert second and second != first
    entries = _checkpoint_entries(cp_dir, "wf-attempts")
    assert len(entries) == 2
    assert {entry["stash_ref"] for entry in entries} == {first, second}
    assert _git(repo, "show", f"{first}:attempt-one.py").stdout == "attempt = 1\n"
    assert _git(repo, "show", f"{second}:attempt-two.py").stdout == "attempt = 2\n"
    refs = _git(
        repo,
        "for-each-ref",
        "--format=%(refname)",
        "refs/ribosome-checkpoints-v2/",
    )
    assert len(refs.stdout.splitlines()) == 2


def test_v2_checkpoint_ref_coexists_with_legacy_leaf_ref(tmp_path, monkeypatch):
    repo = _make_repo(tmp_path)
    cp_dir = tmp_path / "cps"
    monkeypatch.setattr(git_ops, "_CHECKPOINT_DIR", cp_dir)
    legacy_ref = "refs/ribosome-checkpoints/wf-legacy"
    assert _git(repo, "update-ref", legacy_ref, "HEAD").returncode == 0
    (repo / "valuable.py").write_text("preserve = True\n")

    saved = git_ops._checkpoint_worktree_or_raise(str(repo), "wf-legacy")

    assert saved
    [entry] = _checkpoint_entries(cp_dir, "wf-legacy")
    assert entry["git_ref"].startswith("refs/ribosome-checkpoints-v2/")
    assert _git(repo, "show-ref", "--verify", legacy_ref).returncode == 0
    assert _git(repo, "show-ref", "--verify", entry["git_ref"]).returncode == 0


def test_checkpoint_guard_fails_when_immutable_ref_cannot_be_created(
    tmp_path, monkeypatch
):
    repo = _make_repo(tmp_path)
    monkeypatch.setattr(git_ops, "_CHECKPOINT_DIR", tmp_path / "cps")
    (repo / "valuable.py").write_text("preserve = True\n")
    real_run = git_ops._run_worker_command

    def fail_update_ref(command, *args, **kwargs):
        if command[:2] == ["git", "update-ref"]:
            return subprocess.CompletedProcess(command, 1, stdout="", stderr="failed")
        return real_run(command, *args, **kwargs)

    monkeypatch.setattr(git_ops, "_run_worker_command", fail_update_ref)

    with pytest.raises(RuntimeError, match="refusing destructive cleanup"):
        git_ops._checkpoint_worktree_or_raise(str(repo), "wf-ref-failure")

    assert (repo / "valuable.py").read_text() == "preserve = True\n"


def test_checkpoint_worktree_returns_none_when_clean(tmp_path, monkeypatch):
    """A clean tree produces no checkpoint artifacts."""
    repo = _make_repo(tmp_path)
    monkeypatch.setattr(git_ops, "_CHECKPOINT_DIR", tmp_path / "cps")
    assert git_ops._checkpoint_worktree(str(repo), "wf-clean") is None
    assert not list((tmp_path / "cps").glob("*-wf-clean.json"))


def test_checkpoint_worktree_refuses_to_unlink_unknown_index_lock(
    tmp_path, monkeypatch
):
    """Lifecycle code preserves an index lock whose ownership is unknown."""
    repo = _make_repo(tmp_path)
    cp_dir = tmp_path / "cps"
    monkeypatch.setattr(git_ops, "_CHECKPOINT_DIR", cp_dir)
    (repo / "tracked.py").write_text("x = 99\n")
    lock = repo / ".git" / "index.lock"
    lock.write_text("unknown owner")

    ref = git_ops._checkpoint_worktree(str(repo), "wf-lock", exit_code=-9)

    assert ref is None
    with pytest.raises(RuntimeError, match="refusing destructive cleanup"):
        git_ops._checkpoint_worktree_or_raise(str(repo), "wf-lock", exit_code=-9)
    assert lock.read_text() == "unknown owner"
    assert not _checkpoint_entries(cp_dir, "wf-lock")


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
    assert len(_checkpoint_entries(cp_dir, "ribosome-gc-1")) == 1


def test_gc_preserves_dirty_worktree_when_checkpoint_fails(tmp_path, monkeypatch):
    repo = _make_repo(tmp_path)
    wt = repo / ".worktrees" / "ribosome-gc-failure"
    _git(repo, "worktree", "add", "-q", str(wt), "-b", "ribosome-gc-failure")
    dirty = wt / "valuable.py"
    dirty.write_text("preserve = True\n")
    old = time.time() - 3 * 3600
    os.utime(wt, (old, old))
    monkeypatch.setattr(git_ops, "_checkpoint_worktree", lambda *args, **kwargs: None)

    git_ops._gc_worktrees(str(repo))

    assert wt.is_dir()
    assert dirty.read_text() == "preserve = True\n"


def test_translocase_checkpoints_before_cleanup_on_abnormal_exit():
    """Source-pattern gate: the rc != 0 cleanup path must checkpoint first."""
    import inspect

    from mtor.worker import translocase

    src = inspect.getsource(translocase)
    cleanup_idx = src.index("_cleanup_worktree(str(worktree_path))")
    checkpoint_idx = src.index("_checkpoint_worktree_or_raise(")
    assert checkpoint_idx < cleanup_idx
