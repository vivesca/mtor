"""Hermetic git-repo tests for _reap_landed_branches in mtor.worker.git_ops.

Builds REAL git repositories under pytest's tmp_path with subprocess.run —
no mocks, no network, no ssh, no patching. The root conftest already puts
the repo root on sys.path (it imports from `mtor`), so the function under
test imports cleanly with no path shims.
"""

from __future__ import annotations

import subprocess
from pathlib import Path

from mtor.worker.git_ops import _reap_landed_branches


def _git(cwd, *args):
    """Run a git command in *cwd*, returning the completed process."""
    return subprocess.run(
        ["git", *args],
        cwd=cwd,
        capture_output=True,
        text=True,
        check=True,
    )


def _make_repo(tmp_path, default="main"):
    """Create a fresh repo with one commit on *default*; return its path."""
    repo = tmp_path / "repo"
    repo.mkdir()
    _git(str(repo), "init", "-b", default)
    _git(str(repo), "config", "user.email", "ribosome@test.local")
    _git(str(repo), "config", "user.name", "ribosome-test")
    seed = repo / "README.md"
    seed.write_text("seed\n")
    _git(str(repo), "add", "README.md")
    _git(str(repo), "commit", "-m", "initial")
    return str(repo)


def _add_merged_branch(repo, name):
    """Create *name* at the default tip — already an ancestor of default."""
    _git(repo, "branch", name)


def _add_unmerged_branch(repo, name):
    """Create *name* with one extra commit, then return to the default branch."""
    default = _git(repo, "rev-parse", "--abbrev-ref", "HEAD").stdout.strip()
    _git(repo, "checkout", "-b", name)
    extra = Path(repo) / f"{name}.txt"
    extra.write_text(f"work on {name}\n")
    _git(repo, "add", extra.name)
    _git(repo, "commit", "-m", f"work on {name}")
    _git(repo, "checkout", default)


def _branches(repo):
    """Return the set of local branch names in *repo*."""
    out = _git(repo, "for-each-ref", "--format=%(refname:short)", "refs/heads").stdout
    return {line.strip() for line in out.splitlines() if line.strip()}


def test_reap_deletes_merged_ribosome_branch_and_ignores_other_branches(tmp_path):
    repo = _make_repo(tmp_path)
    _add_merged_branch(repo, "ribosome-done-1")
    _add_merged_branch(repo, "feature-keep")

    summary = _reap_landed_branches(str(repo), min_age_seconds=0)

    branches = _branches(repo)
    assert "ribosome-done-1" not in branches
    assert "feature-keep" in branches
    assert "main" in branches
    assert summary["reaped"] == ["ribosome-done-1"]
    assert summary["errors"] == []


def test_reap_keeps_unmerged_ribosome_branch(tmp_path):
    repo = _make_repo(tmp_path)
    _add_unmerged_branch(repo, "ribosome-wip-1")

    summary = _reap_landed_branches(str(repo), min_age_seconds=0)

    assert "ribosome-wip-1" in _branches(repo)
    assert summary["reaped"] == []
    assert summary["errors"] == []


def test_reap_skips_active_branches(tmp_path):
    repo = _make_repo(tmp_path)
    _add_merged_branch(repo, "ribosome-active-1")

    summary = _reap_landed_branches(
        str(repo),
        active_branches=frozenset({"ribosome-active-1"}),
        min_age_seconds=0,
    )

    assert "ribosome-active-1" in _branches(repo)
    assert summary["reaped"] == []
    assert "ribosome-active-1" in summary["skipped_active"]
    assert summary["errors"] == []


def test_reap_skips_young_branches(tmp_path):
    repo = _make_repo(tmp_path)
    _add_merged_branch(repo, "ribosome-young-1")

    summary = _reap_landed_branches(str(repo), min_age_seconds=3600)

    assert "ribosome-young-1" in _branches(repo)
    assert summary["reaped"] == []
    assert summary["errors"] == []


def test_reap_skips_branch_with_live_worktree(tmp_path):
    repo = _make_repo(tmp_path)
    _add_merged_branch(repo, "ribosome-busy-1")
    Path(repo, ".worktrees", "ribosome-busy-1").mkdir(parents=True)

    summary = _reap_landed_branches(str(repo), min_age_seconds=0)

    assert "ribosome-busy-1" in _branches(repo)
    assert summary["reaped"] == []
    assert summary["errors"] == []


def test_reap_gates_remote_deletion_on_merged_remote_sha(tmp_path):
    repo = _make_repo(tmp_path)
    bare = tmp_path / "bare.git"
    bare.mkdir()
    _git(str(bare), "init", "--bare")
    _git(repo, "remote", "add", "origin", str(bare))
    _git(repo, "push", "origin", "main")

    # (a) landed: merged at default tip, pushed.
    _add_merged_branch(repo, "ribosome-landed-1")
    _git(repo, "push", "origin", "ribosome-landed-1")

    # (b) drift: one extra commit, pushed, then local tip reset to default.
    _add_unmerged_branch(repo, "ribosome-drift-1")
    _git(repo, "push", "origin", "ribosome-drift-1")
    _git(repo, "branch", "-f", "ribosome-drift-1", "main")

    summary = _reap_landed_branches(str(repo), min_age_seconds=0)

    branches = _branches(repo)
    assert "ribosome-landed-1" not in branches
    assert "ribosome-drift-1" not in branches
    assert set(summary["reaped"]) == {"ribosome-landed-1", "ribosome-drift-1"}

    ls_landed = _git(repo, "ls-remote", "--heads", "origin", "ribosome-landed-1")
    assert ls_landed.stdout.strip() == ""
    assert "ribosome-landed-1" in summary["remote_deleted"]

    ls_drift = _git(repo, "ls-remote", "--heads", "origin", "ribosome-drift-1")
    assert ls_drift.stdout.strip() != ""
    assert "ribosome-drift-1" not in summary["remote_deleted"]

    assert summary["errors"] == []


def test_reap_resolves_master_default_branch(tmp_path):
    repo = _make_repo(tmp_path, default="master")
    _add_merged_branch(repo, "ribosome-old-1")

    summary = _reap_landed_branches(str(repo), min_age_seconds=0)

    assert "ribosome-old-1" not in _branches(repo)
    assert "ribosome-old-1" in summary["reaped"]
    assert summary["errors"] == []
