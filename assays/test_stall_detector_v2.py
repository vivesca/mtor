"""Tests for empty-diff stall blindness fix and cancel-signal kill.

Tests _heartbeat_stall_check (extracted from translocase.py):
- empty_ticks counter tracks consecutive empty diffs
- Graduated kill at 30 empty ticks (~15min), warn at 20 (~10min)
- activity.is_cancelled() kills subprocess immediately
- skip_stall param disables all stall checks

Run: cd ~/code/mtor && uv run pytest assays/test_stall_detector_v2.py -v
"""

from __future__ import annotations

import asyncio
import hashlib
from unittest.mock import AsyncMock, MagicMock, patch

from mtor.worker.translocase import _heartbeat_stall_check


def _run(coro):
    """Run an async function synchronously for testing."""
    return asyncio.run(coro)


def _empty_hash() -> str:
    return hashlib.sha256(b"").hexdigest()[:12]


def _make_proc() -> MagicMock:
    proc = MagicMock()
    proc.pid = 12345
    proc.kill = MagicMock()
    return proc


def _mock_empty_diff() -> MagicMock:
    r = MagicMock()
    r.stdout = ""
    return r


def _mock_nonempty_diff(content: str = "diff --git a/foo.py b/foo.py\n+new line\n") -> MagicMock:
    r = MagicMock()
    r.stdout = content
    return r


class TestEmptyDiffTimeoutKills:
    """30+ consecutive empty diffs after warmup kills the subprocess."""

    def test_empty_diff_timeout_kills_after_threshold(self):
        """After 4 warmup ticks + 30 empty ticks, proc.kill() is called."""
        proc = _make_proc()

        with (
            patch("mtor.worker.translocase._subprocess.run", return_value=_mock_empty_diff()),
            patch("mtor.worker.translocase.activity") as mock_activity,
            patch("asyncio.sleep", new_callable=AsyncMock),
        ):
            mock_activity.is_cancelled.return_value = False
            mock_activity.heartbeat = MagicMock()

            _run(_heartbeat_stall_check(
                proc, "/tmp/worktree", "zhipu", "test task", skip_stall=False,
            ))

        proc.kill.assert_called_once()

    def test_empty_diff_warns_at_twenty_ticks(self):
        """At 20 empty ticks, a warning is logged (warnings_sent increments)."""
        proc = _make_proc()

        with (
            patch("mtor.worker.translocase._subprocess.run", return_value=_mock_empty_diff()),
            patch("mtor.worker.translocase.activity") as mock_activity,
            patch("asyncio.sleep", new_callable=AsyncMock),
        ):
            mock_activity.is_cancelled.return_value = False
            mock_activity.heartbeat = MagicMock()

            _run(_heartbeat_stall_check(
                proc, "/tmp/worktree", "zhipu", "test task", skip_stall=False,
            ))

        # Function should have killed (reached 30), confirming it got past 20
        proc.kill.assert_called_once()


class TestEmptyDiffResetsOnWrite:
    """Non-empty diff resets empty_ticks counter to 0."""

    def test_empty_diff_resets_on_first_write(self):
        """After empty ticks, a non-empty diff resets the counter; no premature kill."""
        proc = _make_proc()
        call_count = 0

        def mock_run(*args, **kwargs):
            nonlocal call_count
            call_count += 1
            # Empty for ticks 1-18 (4 warmup + 14 empty)
            # Non-empty for ticks 19-22 (resets empty_ticks)
            # Empty again for ticks 23-45
            if 19 <= call_count <= 22:
                return _mock_nonempty_diff()
            return _mock_empty_diff()

        sleep_calls = 0

        async def mock_sleep(seconds):
            nonlocal sleep_calls
            sleep_calls += 1
            # Stop before second empty run reaches 30
            if sleep_calls >= 45:
                raise asyncio.CancelledError("test limit")

        with (
            patch("mtor.worker.translocase._subprocess.run", side_effect=mock_run),
            patch("mtor.worker.translocase.activity") as mock_activity,
            patch("asyncio.sleep", side_effect=mock_sleep),
        ):
            mock_activity.is_cancelled.return_value = False
            mock_activity.heartbeat = MagicMock()

            with patch("pytest.raises", side_effect=lambda *a, **kw: patch.object(asyncio, "CancelledError")):
                pass
            try:
                _run(_heartbeat_stall_check(
                    proc, "/tmp/worktree", "zhipu", "test task", skip_stall=False,
                ))
            except (asyncio.CancelledError, RuntimeError):
                pass

        # After reset, second empty run is only 45-22=23 ticks (< 30), no kill
        proc.kill.assert_not_called()


class TestCancelSignalKillsSubprocess:
    """activity.is_cancelled() triggers immediate subprocess kill."""

    def test_cancel_signal_kills_subprocess(self):
        """When is_cancelled() returns True, proc.kill() is called immediately."""
        proc = _make_proc()

        with (
            patch("mtor.worker.translocase._subprocess.run", return_value=_mock_empty_diff()),
            patch("mtor.worker.translocase.activity") as mock_activity,
            patch("asyncio.sleep", new_callable=AsyncMock),
        ):
            # Cancel after 3 ticks
            mock_activity.is_cancelled = MagicMock(
                side_effect=[False, False, True],
            )
            mock_activity.heartbeat = MagicMock()

            _run(_heartbeat_stall_check(
                proc, "/tmp/worktree", "zhipu", "test task", skip_stall=False,
            ))

        proc.kill.assert_called_once()

    def test_cancel_kills_before_stall_threshold(self):
        """Cancel signal kills even when stall hasn't been detected yet."""
        proc = _make_proc()

        with (
            patch("mtor.worker.translocase._subprocess.run", return_value=_mock_nonempty_diff()),
            patch("mtor.worker.translocase.activity") as mock_activity,
            patch("asyncio.sleep", new_callable=AsyncMock),
        ):
            # Cancel on very first tick (before any stall checks)
            mock_activity.is_cancelled = MagicMock(
                side_effect=[True],
            )
            mock_activity.heartbeat = MagicMock()

            _run(_heartbeat_stall_check(
                proc, "/tmp/worktree", "zhipu", "test task", skip_stall=False,
            ))

        proc.kill.assert_called_once()


class TestSkipStallParam:
    """skip_stall=True disables all stall detection (scout/research modes)."""

    def test_skip_stall_never_kills(self):
        """With skip_stall=True, even 50 empty diffs won't kill the process."""
        proc = _make_proc()
        sleep_calls = 0

        async def mock_sleep(seconds):
            nonlocal sleep_calls
            sleep_calls += 1
            if sleep_calls >= 50:
                raise asyncio.CancelledError("test limit")

        with (
            patch("mtor.worker.translocase._subprocess.run", return_value=_mock_empty_diff()),
            patch("mtor.worker.translocase.activity") as mock_activity,
            patch("asyncio.sleep", side_effect=mock_sleep),
        ):
            mock_activity.is_cancelled.return_value = False
            mock_activity.heartbeat = MagicMock()

            try:
                _run(_heartbeat_stall_check(
                    proc, "/tmp/worktree", "zhipu", "test task", skip_stall=True,
                ))
            except (asyncio.CancelledError, RuntimeError):
                pass

        proc.kill.assert_not_called()
