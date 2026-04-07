"""Tests for _git_snapshot base_sha fallback — catches commits on main when no worktree.

When worktree creation fails and ribosome commits directly to main,
`git diff main..HEAD` is always empty. The base_sha fallback re-runs
the diff against the pre-recorded HEAD SHA to detect the actual work.

Runs via: cd ~/code/mtor && uv run pytest assays/test_snapshot_fallback.py -v
"""

from __future__ import annotations

import subprocess
from pathlib import Path

import sys

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from mtor.worker.translocase import _git_snapshot


def _init_repo(tmp: Path) -> Path:
    """Create a git repo with an initial commit on main."""
    repo = tmp / "repo"
    repo.mkdir()
    subprocess.run(["git", "init", "-b", "main"], cwd=repo, capture_output=True, check=True)
    subprocess.run(["git", "config", "user.email", "test@test"], cwd=repo, capture_output=True)
    subprocess.run(["git", "config", "user.name", "test"], cwd=repo, capture_output=True)
    (repo / "README.md").write_text("init")
    subprocess.run(["git", "add", "."], cwd=repo, capture_output=True, check=True)
    subprocess.run(["git", "commit", "-m", "init"], cwd=repo, capture_output=True, check=True)
    return repo


def _get_head_sha(repo: Path) -> str:
    result = subprocess.run(
        ["git", "rev-parse", "HEAD"], capture_output=True, text=True, cwd=repo
    )
    return result.stdout.strip()


class TestSnapshotBaseShaFallback:
    """_git_snapshot should use base_sha when main..HEAD is empty."""

    def test_no_base_sha_empty_on_main(self, tmp_path):
        """Without base_sha, commits on main produce empty snapshot."""
        repo = _init_repo(tmp_path)
        # Add a commit directly on main
        (repo / "foo.py").write_text("print('hello')")
        subprocess.run(["git", "add", "."], cwd=repo, capture_output=True)
        subprocess.run(["git", "commit", "-m", "add foo"], cwd=repo, capture_output=True)
        # main..HEAD is empty because HEAD IS main
        snap = _git_snapshot(str(repo))
        assert snap["commit_count"] == 0
        assert snap["stat"].strip() == ""

    def test_base_sha_sees_commits_on_main(self, tmp_path):
        """With base_sha, commits on main are detected via {base_sha}..HEAD diff."""
        repo = _init_repo(tmp_path)
        pre_sha = _get_head_sha(repo)
        # Add commits directly on main (simulating worktree fallback)
        (repo / "foo.py").write_text("print('hello')")
        subprocess.run(["git", "add", "."], cwd=repo, capture_output=True)
        subprocess.run(["git", "commit", "-m", "add foo"], cwd=repo, capture_output=True)
        (repo / "bar.py").write_text("x = 1")
        subprocess.run(["git", "add", "."], cwd=repo, capture_output=True)
        subprocess.run(["git", "commit", "-m", "add bar"], cwd=repo, capture_output=True)
        # With base_sha, snapshot should see the 2 new commits
        snap = _git_snapshot(str(repo), base_sha=pre_sha)
        assert snap["commit_count"] == 2
        assert "foo.py" in snap["stat"]
        assert "bar.py" in snap["stat"]
        assert snap.get("fallback") is True

    def test_base_sha_not_needed_on_branch(self, tmp_path):
        """On a worktree branch, main..HEAD works — base_sha not needed."""
        repo = _init_repo(tmp_path)
        pre_sha = _get_head_sha(repo)
        subprocess.run(["git", "checkout", "-b", "feature"], cwd=repo, capture_output=True)
        (repo / "baz.py").write_text("z = 1")
        subprocess.run(["git", "add", "."], cwd=repo, capture_output=True)
        subprocess.run(["git", "commit", "-m", "add baz"], cwd=repo, capture_output=True)
        # main..HEAD shows the branch commit — no fallback needed
        snap = _git_snapshot(str(repo), base_sha=pre_sha)
        assert snap["commit_count"] == 1
        assert "baz.py" in snap["stat"]
        # Should NOT use fallback since main..HEAD already works
        assert snap.get("fallback") is not True

    def test_base_sha_none_ignored(self, tmp_path):
        """base_sha=None behaves like old API (no fallback)."""
        repo = _init_repo(tmp_path)
        (repo / "foo.py").write_text("print('hello')")
        subprocess.run(["git", "add", "."], cwd=repo, capture_output=True)
        subprocess.run(["git", "commit", "-m", "add foo"], cwd=repo, capture_output=True)
        snap = _git_snapshot(str(repo), base_sha=None)
        assert snap["commit_count"] == 0  # Still empty — no fallback
        assert snap.get("fallback") is not True

    def test_invalid_base_sha_graceful(self, tmp_path):
        """Invalid base_sha doesn't crash — falls back to empty."""
        repo = _init_repo(tmp_path)
        snap = _git_snapshot(str(repo), base_sha="0000000000000000000000000000000000000000")
        # Should not raise, just return empty-ish result
        assert isinstance(snap, dict)
        assert "stat" in snap


class TestChaperoneWorktreeFallbackContext:
    """Chaperone should not false-reject when worktree failed but commits exist on main."""

    def test_no_false_reject_with_base_sha_commits(self):
        """When post_diff was captured via base_sha fallback, no_commit_on_success should not fire."""
        import asyncio
        from mtor.worker.translocase import chaperone

        result = {
            "success": True,
            "exit_code": 0,
            "stdout": "Done. Created foo.py and bar.py. All tests pass.",
            "stderr": "",
            "task": "Implement feature X in ~/code/mtor",
            "provider": "zhipu",
            "post_diff": {
                "stat": " foo.py | 10 ++++\n bar.py | 5 +++\n",
                "numstat": "10\t0\tfoo.py\n5\t0\tbar.py",
                "commits": ["abc1234 feat: add foo", "def5678 feat: add bar"],
                "commit_count": 2,
                "fallback": True,
            },
            "pre_diff": {"stat": "", "numstat": ""},
            "branch_name": "",
            "cost_info": "",
            "mode": "build",
        }
        review = asyncio.run(chaperone(result))
        assert review["approved"] is True
        assert "no_commit_on_success" not in review["flags"]
