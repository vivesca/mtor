"""Watch ganglion remote for new commits and auto-sync.

Runs sync_from_ganglion() on a polling loop with configurable interval.
Reports each sync cycle and cumulative statistics.
"""

from __future__ import annotations

import signal
import time
from collections import deque
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path
from typing import Callable

from mtor.sync import sync_from_ganglion


# ---------------------------------------------------------------------------
# Pause / resume mechanism
# ---------------------------------------------------------------------------


def pause_file_path(repo_path: str | None = None) -> Path:
    """Return the path to the pause marker file."""
    from mtor import REPO_DIR

    return Path(repo_path or REPO_DIR) / ".mtor-pause"


def is_paused(repo_path: str | None = None) -> bool:
    """Check if dispatching is paused."""
    return pause_file_path(repo_path).exists()


def pause(repo_path: str | None = None) -> Path:
    """Create pause marker file. Returns path to the pause file."""
    path = pause_file_path(repo_path)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(f"paused_at: {datetime.now(UTC).isoformat()}\n")
    return path


def resume(repo_path: str | None = None) -> bool:
    """Remove pause marker file. Returns True if was paused, False if not."""
    path = pause_file_path(repo_path)
    if path.exists():
        path.unlink()
        return True
    return False


# ---------------------------------------------------------------------------
# Freeze / thaw mechanism (deptor)
# ---------------------------------------------------------------------------


def freeze_file_path(repo_path: str | None = None) -> Path:
    """Return the path to the freeze marker file."""
    from mtor import REPO_DIR

    return Path(repo_path or REPO_DIR) / ".mtor-freeze"


def is_frozen(repo_path: str | None = None) -> bool:
    """Check if dispatching is frozen (deptor lock)."""
    return freeze_file_path(repo_path).exists()


def freeze(repo_path: str | None = None) -> Path:
    """Create freeze marker file. Returns path to the freeze file."""
    path = freeze_file_path(repo_path)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(f"frozen_at: {datetime.now(UTC).isoformat()}\n")
    return path


def thaw(repo_path: str | None = None) -> bool:
    """Remove freeze marker file. Returns True if was frozen, False if not."""
    path = freeze_file_path(repo_path)
    if path.exists():
        path.unlink()
        return True
    return False


@dataclass
class WatchCycle:
    """Result of a single watch-sync cycle."""

    cycle: int
    fetched: int
    merged: bool
    error: str | None
    elapsed_s: float


@dataclass
class WatchStats:
    """Cumulative statistics across all watch cycles."""

    cycles: int = 0
    total_fetched: int = 0
    total_merged: int = 0
    total_errors: int = 0
    cycles_run: list[WatchCycle] = field(default_factory=list)

    def record(self, cycle: WatchCycle) -> None:
        self.cycles += 1
        self.total_fetched += cycle.fetched
        if cycle.merged:
            self.total_merged += 1
        if cycle.error:
            self.total_errors += 1
        self.cycles_run.append(cycle)

    def to_dict(self) -> dict:
        return {
            "cycles": self.cycles,
            "total_fetched": self.total_fetched,
            "total_merged": self.total_merged,
            "total_errors": self.total_errors,
        }


@dataclass
class RejectionTracker:
    """Tracks dispatch rejection rate over a sliding window for negative feedback.

    When the rejection rate exceeds *threshold*, the caller should throttle
    dispatch.  Outcomes older than *window_size* are automatically evicted
    so the rate reflects only recent history.
    """

    window_size: int = 20
    threshold: float = 0.5
    _outcomes: deque = field(default_factory=deque, init=False, repr=False)

    def record(self, rejected: bool) -> None:
        """Record a single dispatch outcome (rejected=True or accepted=False)."""
        self._outcomes.append(rejected)
        while len(self._outcomes) > self.window_size:
            self._outcomes.popleft()

    def rejection_rate(self) -> float:
        """Return the fraction of rejected outcomes in the current window."""
        if not self._outcomes:
            return 0.0
        return sum(self._outcomes) / len(self._outcomes)

    def should_throttle(self) -> bool:
        """Return True when the rejection rate meets or exceeds the threshold."""
        return self.rejection_rate() >= self.threshold


# ---------------------------------------------------------------------------
# Circadian dispatch curve — time-based dispatch modulation
# ---------------------------------------------------------------------------

# Rate schedule (UTC hours):
#   22–05  Overnight peak  →  1.0
#   06–07  Morning taper   →  linear 1.0 → 0.2
#   08–17  Daytime low     →  0.2
#   18–21  Evening ramp    →  linear 0.2 → 1.0

_TAPER_START = 6     # hour when taper begins
_TAPER_END = 8       # hour when daytime floor is reached
_RAMP_START = 18     # hour when evening ramp begins
_RAMP_END = 22       # hour when overnight peak resumes

_DAYTIME_RATE = 0.2
_PEAK_RATE = 1.0


# ---------------------------------------------------------------------------
# AMPK sensing — ganglion load monitoring
# ---------------------------------------------------------------------------


@dataclass
class GanglionLoad:
    """Load metrics from the ganglion (remote worker).

    AMPK metaphor: senses energy deficit on the ganglion so the dispatcher
    (mTOR) can throttle when resources are scarce.
    """

    running_tasks: int
    load_avg: float
    load_level: str  # "low", "medium", "high"


# Thresholds for load-level classification.
_HIGH_TASKS = 5
_HIGH_LOAD = 4.0
_MEDIUM_TASKS = 3
_MEDIUM_LOAD = 2.0


def run_watch(
    repo_path: str,
    *,
    interval: int = 60,
    max_cycles: int | None = None,
    once: bool = False,
    on_cycle: Callable[[WatchCycle], None] | None = None,
) -> WatchStats:
    """Run sync_from_ganglion in a loop until stopped.

    Args:
        repo_path: Path to the git repository to sync.
        interval: Seconds between sync attempts.
        max_cycles: Stop after N cycles (None = unlimited).
        once: Run exactly one cycle and return.
        on_cycle: Optional callback after each cycle (e.g. for logging).

    Returns:
        WatchStats with cumulative sync statistics.
    """
    stats = WatchStats()
    stopped = False

    def _handle_signal(signum: int, frame: object) -> None:
        nonlocal stopped
        stopped = True

    original_sigint = signal.getsignal(signal.SIGINT)
    signal.signal(signal.SIGINT, _handle_signal)

    try:
        while not stopped:
            cycle_num = stats.cycles + 1

            # Skip sync cycle when paused or frozen
            if is_frozen(repo_path):
                cycle = WatchCycle(
                    cycle=cycle_num,
                    fetched=0,
                    merged=False,
                    error="frozen",
                    elapsed_s=0.0,
                )
                stats.record(cycle)

                if on_cycle is not None:
                    on_cycle(cycle)

                if once:
                    break

                if max_cycles is not None and stats.cycles >= max_cycles:
                    break

                try:
                    time.sleep(interval)
                except KeyboardInterrupt:
                    stopped = True
                continue

            if is_paused(repo_path):
                cycle = WatchCycle(
                    cycle=cycle_num,
                    fetched=0,
                    merged=False,
                    error="paused",
                    elapsed_s=0.0,
                )
                stats.record(cycle)

                if on_cycle is not None:
                    on_cycle(cycle)

                if once:
                    break

                if max_cycles is not None and stats.cycles >= max_cycles:
                    break

                try:
                    time.sleep(interval)
                except KeyboardInterrupt:
                    stopped = True
                continue

            t0 = time.monotonic()
            result = sync_from_ganglion(repo_path)
            elapsed = time.monotonic() - t0

            cycle = WatchCycle(
                cycle=cycle_num,
                fetched=result["fetched"],
                merged=result["merged"],
                error=result.get("error"),
                elapsed_s=round(elapsed, 3),
            )
            stats.record(cycle)

            if on_cycle is not None:
                on_cycle(cycle)

            if once:
                break

            if max_cycles is not None and stats.cycles >= max_cycles:
                break

            # Sleep, but wake early on SIGINT
            try:
                time.sleep(interval)
            except KeyboardInterrupt:
                stopped = True
    finally:
        signal.signal(signal.SIGINT, original_sigint)

    return stats
