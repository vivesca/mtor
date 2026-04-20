"""Tests for mtor ragulator command — polling sync_from_ganglion.

Runs via: cd ~/code/mtor && uv run pytest assays/test_watch.py -v
"""

from __future__ import annotations

import subprocess
from pathlib import Path

import sys

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))


def _init_repo_pair(tmp: Path) -> tuple[Path, Path]:
    """Create soma + ganglion repo pair for testing.

    Returns (soma_repo, ganglion_repo).
    """
    ganglion = tmp / "ganglion"
    ganglion.mkdir()
    subprocess.run(["git", "init", "-b", "main"], cwd=ganglion, capture_output=True, check=True)
    subprocess.run(["git", "config", "user.email", "test@test"], cwd=ganglion, capture_output=True)
    subprocess.run(["git", "config", "user.name", "test"], cwd=ganglion, capture_output=True)
    (ganglion / "README.md").write_text("init")
    subprocess.run(["git", "add", "."], cwd=ganglion, capture_output=True)
    subprocess.run(["git", "commit", "-m", "init"], cwd=ganglion, capture_output=True)

    soma = tmp / "soma"
    subprocess.run(["git", "clone", str(ganglion), str(soma)], capture_output=True, check=True)
    subprocess.run(["git", "remote", "rename", "origin", "ganglion"], cwd=soma, capture_output=True, check=True)

    return soma, ganglion


class TestWatchStats:
    """WatchStats dataclass tracks cumulative sync statistics."""

    def test_empty_stats(self):
        from mtor.watch import WatchStats

        stats = WatchStats()
        assert stats.cycles == 0
        assert stats.total_fetched == 0
        assert stats.total_merged == 0
        assert stats.total_errors == 0

    def test_record_cycle(self):
        from mtor.watch import WatchCycle, WatchStats

        stats = WatchStats()
        cycle = WatchCycle(cycle=1, fetched=3, merged=True, error=None, elapsed_s=0.5)
        stats.record(cycle)
        assert stats.cycles == 1
        assert stats.total_fetched == 3
        assert stats.total_merged == 1
        assert stats.total_errors == 0

    def test_record_error_cycle(self):
        from mtor.watch import WatchCycle, WatchStats

        stats = WatchStats()
        cycle = WatchCycle(cycle=1, fetched=0, merged=False, error="diverged", elapsed_s=0.2)
        stats.record(cycle)
        assert stats.cycles == 1
        assert stats.total_merged == 0
        assert stats.total_errors == 1

    def test_to_dict(self):
        from mtor.watch import WatchCycle, WatchStats

        stats = WatchStats()
        stats.record(WatchCycle(cycle=1, fetched=2, merged=True, error=None, elapsed_s=0.3))
        stats.record(WatchCycle(cycle=2, fetched=0, merged=True, error=None, elapsed_s=0.1))
        d = stats.to_dict()
        assert d["cycles"] == 2
        assert d["total_fetched"] == 2
        assert d["total_merged"] == 2
        assert d["total_errors"] == 0


class TestRunWatchOnce:
    """run_watch with once=True runs exactly one cycle."""

    def test_once_no_changes(self, tmp_path):
        from mtor.watch import run_watch

        soma, _ganglion = _init_repo_pair(tmp_path)
        stats = run_watch(str(soma), once=True)
        assert stats.cycles == 1
        assert stats.total_fetched == 0
        assert stats.total_merged == 1  # no-op merge counts as success

    def test_once_with_new_commits(self, tmp_path):
        from mtor.watch import run_watch

        soma, ganglion = _init_repo_pair(tmp_path)
        (ganglion / "new.py").write_text("x = 1")
        subprocess.run(["git", "add", "."], cwd=ganglion, capture_output=True)
        subprocess.run(["git", "commit", "-m", "new file"], cwd=ganglion, capture_output=True)

        stats = run_watch(str(soma), once=True)
        assert stats.cycles == 1
        assert stats.total_fetched == 1
        assert stats.total_merged == 1
        assert (soma / "new.py").exists()

    def test_once_with_diverged(self, tmp_path):
        from mtor.watch import run_watch

        soma, ganglion = _init_repo_pair(tmp_path)
        (ganglion / "g.py").write_text("g = 1")
        subprocess.run(["git", "add", "."], cwd=ganglion, capture_output=True)
        subprocess.run(["git", "commit", "-m", "ganglion change"], cwd=ganglion, capture_output=True)

        subprocess.run(["git", "config", "user.email", "test@test"], cwd=soma, capture_output=True)
        subprocess.run(["git", "config", "user.name", "test"], cwd=soma, capture_output=True)
        (soma / "s.py").write_text("s = 1")
        subprocess.run(["git", "add", "."], cwd=soma, capture_output=True)
        subprocess.run(["git", "commit", "-m", "soma change"], cwd=soma, capture_output=True)

        stats = run_watch(str(soma), once=True)
        assert stats.cycles == 1
        assert stats.total_errors == 1

    def test_once_no_ganglion_remote(self, tmp_path):
        from mtor.watch import run_watch

        repo = tmp_path / "bare"
        repo.mkdir()
        subprocess.run(["git", "init", "-b", "main"], cwd=repo, capture_output=True, check=True)
        subprocess.run(["git", "config", "user.email", "test@test"], cwd=repo, capture_output=True)
        subprocess.run(["git", "config", "user.name", "test"], cwd=repo, capture_output=True)
        (repo / "README.md").write_text("init")
        subprocess.run(["git", "add", "."], cwd=repo, capture_output=True, check=True)
        subprocess.run(["git", "commit", "-m", "init"], cwd=repo, capture_output=True, check=True)

        stats = run_watch(str(repo), once=True)
        assert stats.cycles == 1
        assert stats.total_fetched == 0


class TestRunWatchMaxCycles:
    """run_watch with max_cycles stops after N cycles."""

    def test_max_cycles_one(self, tmp_path):
        from mtor.watch import run_watch

        soma, _ganglion = _init_repo_pair(tmp_path)
        cycles_seen = []

        def on_cycle(c):
            cycles_seen.append(c.cycle)

        stats = run_watch(str(soma), interval=0, max_cycles=1, on_cycle=on_cycle)
        assert stats.cycles == 1
        assert cycles_seen == [1]

    def test_max_cycles_three(self, tmp_path):
        from mtor.watch import run_watch

        soma, _ganglion = _init_repo_pair(tmp_path)
        stats = run_watch(str(soma), interval=0, max_cycles=3)
        assert stats.cycles == 3


class TestRunWatchCallback:
    """on_cycle callback receives WatchCycle for each iteration."""

    def test_callback_called(self, tmp_path):
        from mtor.watch import run_watch

        soma, _ganglion = _init_repo_pair(tmp_path)
        callbacks = []

        def on_cycle(cycle):
            callbacks.append(cycle)

        stats = run_watch(str(soma), interval=0, max_cycles=2, on_cycle=on_cycle)
        assert len(callbacks) == 2
        assert callbacks[0].cycle == 1
        assert callbacks[1].cycle == 2


class TestWatchCycleDataclass:
    """WatchCycle stores per-cycle results."""

    def test_fields(self):
        from mtor.watch import WatchCycle

        c = WatchCycle(cycle=5, fetched=10, merged=True, error=None, elapsed_s=1.234)
        assert c.cycle == 5
        assert c.fetched == 10
        assert c.merged is True
        assert c.error is None
        assert c.elapsed_s == 1.234
