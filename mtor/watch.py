"""Watch ganglion remote for new commits and auto-sync.

Runs sync_from_ganglion() on a polling loop with configurable interval.
Reports each sync cycle and cumulative statistics.
"""

from __future__ import annotations

import signal
import time
from dataclasses import dataclass, field
from typing import Callable

from mtor.sync import sync_from_ganglion


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
