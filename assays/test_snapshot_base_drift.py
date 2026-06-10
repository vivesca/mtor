"""Tests for _git_snapshot under parallel-commit base drift.

Incident 2026-06-10 (ribosome-glm51-fix-cytokinesis-verify-...): a parallel
session landed a commit on main *after* a ribosome worker branched from its
recorded base_sha. `git diff main..HEAD` then became contaminated — files the
parallel commit ADDED to main showed up as DELETIONS in the worker diff,
raising false `pure_deletion` / `file_shrunk` blocking flags and a false
`rejected`, even though `git show --stat <worker-sha>` proved the worker commit
touched only its intended files.

The snapshot must diff `base_sha..worker_HEAD` (the recorded branch point), not
the worker branch against an advanced main. The dossier already records the
correct base_sha, so the data to fix it is present.

See: ~/epigenome/marks/finding_chaperone_false_positive_parallel_commit_base_drift.md

Runs via: cd ~/code/mtor && uv run pytest assays/test_snapshot_base_drift.py -v
"""

from __future__ import annotations

import asyncio
import subprocess
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from mtor.worker.translocase import _git_snapshot, chaperone


def _git(repo: Path, *args: str) -> subprocess.CompletedProcess:
    return subprocess.run(
        ["git", *args], cwd=repo, capture_output=True, text=True, check=True
    )


def _init_repo(tmp: Path) -> Path:
    repo = tmp / "repo"
    repo.mkdir()
    _git(repo, "init", "-b", "main")
    _git(repo, "config", "user.email", "test@test")
    _git(repo, "config", "user.name", "test")
    (repo / "README.md").write_text("init\n")
    _git(repo, "add", "README.md")
    _git(repo, "commit", "-m", "init")
    return repo


def _head(repo: Path) -> str:
    return _git(repo, "rev-parse", "HEAD").stdout.strip()


def _make_drift(tmp: Path) -> tuple[Path, str, str]:
    """Reproduce the incident topology.

    Returns (repo, base_sha, worker_sha). The worker touches only
    worker_feature.py. A parallel commit then ADDS effectors/titer (656 lines)
    and assays/test_titer.py (430 lines) to main, advancing it past base_sha.
    """
    repo = _init_repo(tmp)
    base_sha = _head(repo)

    # Worker branches from base_sha and makes its intended, additive change.
    _git(repo, "checkout", "-b", "ribosome-worker")
    (repo / "worker_feature.py").write_text("def verify_summary():\n    return True\n")
    _git(repo, "add", "worker_feature.py")
    _git(repo, "commit", "-m", "feat: add verify summary")
    worker_sha = _head(repo)

    # Parallel session lands a large additive commit on main AFTER the branch.
    _git(repo, "checkout", "main")
    (repo / "effectors").mkdir()
    (repo / "effectors" / "titer").write_text(
        "\n".join(f"line {i}" for i in range(656)) + "\n"
    )
    (repo / "assays").mkdir()
    (repo / "assays" / "test_titer.py").write_text(
        "\n".join(f"# test line {i}" for i in range(430)) + "\n"
    )
    _git(repo, "add", "effectors/titer", "assays/test_titer.py")
    _git(repo, "commit", "-m", "feat: add titer (parallel)")

    # The review inspects the worker branch.
    _git(repo, "checkout", "ribosome-worker")
    return repo, base_sha, worker_sha


class TestSnapshotParallelCommitBaseDrift:
    """_git_snapshot must diff base_sha..HEAD, not the advanced main..HEAD."""

    def test_snapshot_diffs_against_base_sha_not_advanced_main(self, tmp_path):
        """Parallel-added files must not appear in the worker's diff scope."""
        repo, base_sha, _worker_sha = _make_drift(tmp_path)
        snap = _git_snapshot(str(repo), base_sha=base_sha)

        # The worker's true scope is exactly worker_feature.py.
        assert "worker_feature.py" in snap["stat"]
        assert snap["commit_count"] == 1

        # Files a parallel commit ADDED to main must NOT show up as deletions.
        assert "titer" not in snap["numstat"], snap["numstat"]
        assert "titer" not in snap["stat"], snap["stat"]

    def test_chaperone_no_false_pure_deletion_on_base_drift(self, tmp_path):
        """Chaperone must not raise pure_deletion/file_shrunk on base drift."""
        repo, base_sha, _worker_sha = _make_drift(tmp_path)
        snap = _git_snapshot(str(repo), base_sha=base_sha)

        result = {
            "success": True,
            "exit_code": 0,
            "stdout": "Done. Added verify_summary. All tests pass.",
            "stderr": "",
            "task": "Add verify summary to ~/code/mtor",
            "provider": "zhipu",
            "post_diff": snap,
            "pre_diff": {"stat": "", "numstat": ""},
            "base_sha": base_sha,
            "branch_name": "ribosome-worker",
            "cost_info": "",
            "mode": "build",
        }
        review = asyncio.run(chaperone(result))

        assert not any(
            f.startswith("pure_deletion") for f in review["flags"]
        ), review["flags"]
        assert not any(
            f.startswith("file_shrunk") for f in review["flags"]
        ), review["flags"]
        assert review["approved"] is True, review["flags"]
