"""Assays for the main-checkout confinement guard.

A worktree-mode ribosome run must never mutate the repo's main checkout —
cwd is a hint, not a boundary (2026-07-04: a ghost opencode edited
/home/vivesca/germline instead of its assigned worktree). The harness
snapshots the main checkout before launch and flags mutation after.

Runs via: cd ~/code/mtor && uv run pytest assays/test_main_checkout_guard.py -q
"""

from __future__ import annotations

import subprocess
import sys
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


def test_main_checkout_state_reports_head_and_dirty_paths(tmp_path):
    """The snapshot carries the HEAD sha and every dirty path."""
    repo = _make_repo(tmp_path)
    (repo / "drifted.py").write_text("d = 1\n")
    state = git_ops._main_checkout_state(str(repo))
    assert len(state["head"]) == 40
    assert "drifted.py" in state["dirty_paths"]


def test_main_checkout_state_handles_non_repo(tmp_path):
    """A missing or non-git directory yields the empty snapshot, no raise."""
    state = git_ops._main_checkout_state(str(tmp_path / "not-a-repo"))
    assert state == {"head": "", "dirty_paths": []}


def test_translocase_flags_main_checkout_mutation():
    """Source-pattern gate: the activity snapshots the main checkout before
    launch and logs/flags mutation after the run."""
    import inspect

    from mtor.worker import translocase

    src = inspect.getsource(translocase)
    assert "_main_checkout_state" in src
    assert "main_checkout_mutated" in src
    assert src.index("main_checkout_mutated") < src.index(
        "_cleanup_worktree(str(worktree_path))"
    )
