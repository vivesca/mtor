"""Tests for dual-signal stall detection (git diff hash + stdout bytes).

The agent is stalled ONLY when BOTH diff hash is frozen AND stdout byte count
hasn't grown.  If either signal is changing, the agent is active.

Covers:
- Frozen hash + growing stdout  → no stall
- Frozen hash + static stdout   → stall (kill)
- Oscillating hash + growing stdout → no stall
- Oscillating hash + static stdout  → stall (kill)
- No stdout_counter passed → backward-compatible hash-only behavior

Run: cd ~/code/mtor && uv run pytest assays/test_stall_dual_signal.py -v
"""

from __future__ import annotations

import asyncio
import hashlib
from unittest.mock import AsyncMock, MagicMock, patch

from mtor.worker.translocase import _heartbeat_stall_check


def _run(coro):
    return asyncio.run(coro)


def _make_proc() -> MagicMock:
    proc = MagicMock()
    proc.pid = 99999
    proc.kill = MagicMock()
    return proc


def _mock_nonempty_diff(content: str = "diff --git a/foo.py b/foo.py\n+line\n") -> MagicMock:
    r = MagicMock()
    r.stdout = content
    return r


class TestFrozenHashGrowingStdout:
    """Frozen diff hash + growing stdout → agent is active, no kill."""

    def test_no_kill_when_stdout_grows(self):
        proc = _make_proc()
        stdout_counter = [0]

        # Same diff hash every tick, stdout grows each tick
        sleep_calls = 0

        async def mock_sleep(seconds):
            nonlocal sleep_calls
            sleep_calls += 1
            # Simulate stdout growth: 1KB per tick
            stdout_counter[0] = sleep_calls * 1024
            # Stop after enough ticks that frozen-only would have killed
            if sleep_calls >= 30:
                raise asyncio.CancelledError("test limit")

        with (
            patch("mtor.worker.translocase._subprocess.run", return_value=_mock_nonempty_diff()),
            patch("mtor.worker.translocase.activity") as mock_activity,
            patch("asyncio.sleep", side_effect=mock_sleep),
        ):
            mock_activity.is_cancelled.return_value = False
            mock_activity.heartbeat = MagicMock()
            try:
                _run(_heartbeat_stall_check(
                    proc, "/tmp/worktree", "zhipu", "test task",
                    skip_stall=False, stdout_counter=stdout_counter,
                ))
            except (asyncio.CancelledError, RuntimeError):
                pass

        proc.kill.assert_not_called()


class TestFrozenHashStaticStdout:
    """Frozen diff hash + static stdout → stall detected, kill."""

    def test_kill_when_both_frozen(self):
        proc = _make_proc()
        stdout_counter = [5000]  # static — never changes

        with (
            patch("mtor.worker.translocase._subprocess.run", return_value=_mock_nonempty_diff()),
            patch("mtor.worker.translocase.activity") as mock_activity,
            patch("asyncio.sleep", new_callable=AsyncMock),
        ):
            mock_activity.is_cancelled.return_value = False
            mock_activity.heartbeat = MagicMock()
            _run(_heartbeat_stall_check(
                proc, "/tmp/worktree", "zhipu", "test task",
                skip_stall=False, stdout_counter=stdout_counter,
            ))

        proc.kill.assert_called_once()


class TestOscillatingHashGrowingStdout:
    """Oscillating diff hash + growing stdout → agent is active, no kill."""

    def test_no_kill_on_oscillation_with_growing_stdout(self):
        proc = _make_proc()
        stdout_counter = [0]
        call_count = 0
        sleep_calls = 0

        # Two different diff contents to create oscillation
        diff_a = _mock_nonempty_diff("diff --git a/a.py b/a.py\n+A\n")
        diff_b = _mock_nonempty_diff("diff --git a/b.py b/b.py\n+B\n")

        def mock_run(*args, **kwargs):
            nonlocal call_count
            call_count += 1
            return diff_a if call_count % 2 == 1 else diff_b

        async def mock_sleep(seconds):
            nonlocal sleep_calls
            sleep_calls += 1
            stdout_counter[0] = sleep_calls * 512
            if sleep_calls >= 30:
                raise asyncio.CancelledError("test limit")

        with (
            patch("mtor.worker.translocase._subprocess.run", side_effect=mock_run),
            patch("mtor.worker.translocase.activity") as mock_activity,
            patch("asyncio.sleep", side_effect=mock_sleep),
        ):
            mock_activity.is_cancelled.return_value = False
            mock_activity.heartbeat = MagicMock()
            try:
                _run(_heartbeat_stall_check(
                    proc, "/tmp/worktree", "zhipu", "test task",
                    skip_stall=False, stdout_counter=stdout_counter,
                ))
            except (asyncio.CancelledError, RuntimeError):
                pass

        proc.kill.assert_not_called()


class TestOscillatingHashStaticStdout:
    """Oscillating diff hash + static stdout → stall detected, kill."""

    def test_kill_on_oscillation_with_static_stdout(self):
        proc = _make_proc()
        stdout_counter = [5000]  # static
        call_count = 0

        diff_a = _mock_nonempty_diff("diff --git a/a.py b/a.py\n+A\n")
        diff_b = _mock_nonempty_diff("diff --git a/b.py b/b.py\n+B\n")

        def mock_run(*args, **kwargs):
            nonlocal call_count
            call_count += 1
            return diff_a if call_count % 2 == 1 else diff_b

        with (
            patch("mtor.worker.translocase._subprocess.run", side_effect=mock_run),
            patch("mtor.worker.translocase.activity") as mock_activity,
            patch("asyncio.sleep", new_callable=AsyncMock),
        ):
            mock_activity.is_cancelled.return_value = False
            mock_activity.heartbeat = MagicMock()
            _run(_heartbeat_stall_check(
                proc, "/tmp/worktree", "zhipu", "test task",
                skip_stall=False, stdout_counter=stdout_counter,
            ))

        proc.kill.assert_called_once()


class TestBackwardCompatibleNoCounter:
    """When stdout_counter is not provided, falls back to hash-only behavior."""

    def test_frozen_hash_kills_without_counter(self):
        """Without stdout_counter, frozen hash alone triggers kill."""
        proc = _make_proc()

        with (
            patch("mtor.worker.translocase._subprocess.run", return_value=_mock_nonempty_diff()),
            patch("mtor.worker.translocase.activity") as mock_activity,
            patch("asyncio.sleep", new_callable=AsyncMock),
        ):
            mock_activity.is_cancelled.return_value = False
            mock_activity.heartbeat = MagicMock()
            _run(_heartbeat_stall_check(
                proc, "/tmp/worktree", "zhipu", "test task",
                skip_stall=False,
            ))

        proc.kill.assert_called_once()
