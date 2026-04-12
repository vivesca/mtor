"""Tests for ganglion→soma auto-sync on mtor riboseq/status.

After a task completes on ganglion, soma should auto-pull new commits
when running `mtor riboseq` or `mtor status`.

Runs via: cd ~/code/mtor && uv run pytest assays/test_sync.py -v
"""

from __future__ import annotations

import subprocess
from pathlib import Path

import sys

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))


def _init_repo_pair(tmp: Path) -> tuple[Path, Path]:
    """Create two repos simulating soma↔ganglion with a remote link.

    Returns (soma_repo, ganglion_repo).
    """
    ganglion = tmp / "ganglion"
    ganglion.mkdir()
    subprocess.run(["git", "init", "-b", "main"], cwd=ganglion, capture_output=True, check=True)
    subprocess.run(["git", "config", "user.email", "test@test"], cwd=ganglion, capture_output=True)
    subprocess.run(["git", "config", "user.name", "test"], cwd=ganglion, capture_output=True)
    (ganglion / "README.md").write_text("init")
    subprocess.run(["git", "add", "."], cwd=ganglion, capture_output=True, check=True)
    subprocess.run(["git", "commit", "-m", "init"], cwd=ganglion, capture_output=True, check=True)

    soma = tmp / "soma"
    subprocess.run(["git", "clone", str(ganglion), str(soma)], capture_output=True, check=True)
    # Rename origin to ganglion to match real setup
    subprocess.run(["git", "remote", "rename", "origin", "ganglion"], cwd=soma, capture_output=True, check=True)

    return soma, ganglion


class TestSyncFromGanglion:
    """sync_from_ganglion() fetches and FF-merges."""

    def test_ff_merge_new_commits(self, tmp_path):
        """New commits on ganglion are fetched and FF-merged."""
        from mtor.sync import sync_from_ganglion

        soma, ganglion = _init_repo_pair(tmp_path)
        # Add commits on ganglion
        (ganglion / "foo.py").write_text("x = 1")
        subprocess.run(["git", "add", "."], cwd=ganglion, capture_output=True)
        subprocess.run(["git", "commit", "-m", "add foo"], cwd=ganglion, capture_output=True)
        (ganglion / "bar.py").write_text("y = 2")
        subprocess.run(["git", "add", "."], cwd=ganglion, capture_output=True)
        subprocess.run(["git", "commit", "-m", "add bar"], cwd=ganglion, capture_output=True)

        result = sync_from_ganglion(str(soma))
        assert result["fetched"] == 2
        assert result["merged"] is True
        assert result["error"] is None
        # Verify files actually landed
        assert (soma / "foo.py").exists()
        assert (soma / "bar.py").exists()

    def test_no_new_commits(self, tmp_path):
        """No new commits — noop."""
        from mtor.sync import sync_from_ganglion

        soma, _ganglion = _init_repo_pair(tmp_path)
        result = sync_from_ganglion(str(soma))
        assert result["fetched"] == 0
        assert result["merged"] is True  # or True — nothing to merge is success
        assert result["error"] is None

    def test_diverged_no_force(self, tmp_path):
        """Diverged histories — report but don't force merge."""
        from mtor.sync import sync_from_ganglion

        soma, ganglion = _init_repo_pair(tmp_path)
        # Diverge: different commits on each side
        (ganglion / "ganglion_file.py").write_text("g = 1")
        subprocess.run(["git", "add", "."], cwd=ganglion, capture_output=True)
        subprocess.run(["git", "commit", "-m", "ganglion change"], cwd=ganglion, capture_output=True)

        (soma / "soma_file.py").write_text("s = 1")
        subprocess.run(["git", "add", "."], cwd=soma, capture_output=True)
        subprocess.run(["git", "config", "user.email", "test@test"], cwd=soma, capture_output=True)
        subprocess.run(["git", "config", "user.name", "test"], cwd=soma, capture_output=True)
        subprocess.run(["git", "commit", "-m", "soma change"], cwd=soma, capture_output=True)

        result = sync_from_ganglion(str(soma))
        assert result["fetched"] >= 1
        assert result["merged"] is False
        assert "diverged" in (result["error"] or "").lower()
        # Verify soma_file still exists (no data loss)
        assert (soma / "soma_file.py").exists()

    def test_no_ganglion_remote_graceful(self, tmp_path):
        """Repo without ganglion remote — skip gracefully."""
        from mtor.sync import sync_from_ganglion

        repo = tmp_path / "nope"
        repo.mkdir()
        subprocess.run(["git", "init", "-b", "main"], cwd=repo, capture_output=True, check=True)
        subprocess.run(["git", "config", "user.email", "test@test"], cwd=repo, capture_output=True)
        subprocess.run(["git", "config", "user.name", "test"], cwd=repo, capture_output=True)
        (repo / "README.md").write_text("init")
        subprocess.run(["git", "add", "."], cwd=repo, capture_output=True, check=True)
        subprocess.run(["git", "commit", "-m", "init"], cwd=repo, capture_output=True, check=True)

        result = sync_from_ganglion(str(repo))
        assert result["fetched"] == 0
        assert result["error"] is None or "no remote" in result["error"].lower()


class TestHasGanglionRemote:
    """_has_ganglion_remote() detection."""

    def test_with_remote(self, tmp_path):
        from mtor.sync import _has_ganglion_remote

        soma, _ganglion = _init_repo_pair(tmp_path)
        assert _has_ganglion_remote(str(soma)) is True

    def test_without_remote(self, tmp_path):
        from mtor.sync import _has_ganglion_remote

        repo = tmp_path / "bare"
        repo.mkdir()
        subprocess.run(["git", "init", "-b", "main"], cwd=repo, capture_output=True, check=True)
        assert _has_ganglion_remote(str(repo)) is False
