"""Tests for scout/research no-output stall detection."""

from __future__ import annotations

import asyncio
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
    result = MagicMock()
    result.stdout = content
    return result


def _mock_empty_diff() -> MagicMock:
    result = MagicMock()
    result.stdout = ""
    return result


def test_scout_no_output_kills_after_window():
    proc = _make_proc()
    stdout_counter = [0]
    stderr_counter = [0]
    sleep_calls = 0

    async def mock_sleep(seconds):
        nonlocal sleep_calls
        sleep_calls += 1
        if sleep_calls > 35:
            raise AssertionError("scout no-output watchdog waited too long")

    with (
        patch("mtor.worker.translocase._subprocess.run", return_value=_mock_empty_diff()),
        patch("mtor.worker.translocase.activity") as mock_activity,
        patch("asyncio.sleep", side_effect=mock_sleep),
        patch(
            "mtor.worker.translocase._graceful_kill_group", new=AsyncMock()
        ) as kill_group,
        patch("mtor.worker.translocase._kill_process_group") as legacy_kill,
    ):
        mock_activity.is_cancelled.return_value = False
        mock_activity.heartbeat = MagicMock()
        _run(_heartbeat_stall_check(
            proc, "/tmp/worktree", "zhipu", "test task",
            skip_stall=True,
            stdout_counter=stdout_counter,
            stderr_counter=stderr_counter,
            worktree_path="/tmp/worktree", attempt_identity="wf:1:scout",
        ))

    kill_group.assert_awaited_once()
    legacy_kill.assert_not_called()


def test_scout_stdout_growth_not_killed():
    proc = _make_proc()
    stdout_counter = [0]
    stderr_counter = [0]
    sleep_calls = 0

    async def mock_sleep(seconds):
        nonlocal sleep_calls
        sleep_calls += 1
        stdout_counter[0] = sleep_calls * 512
        if sleep_calls > 35:
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
                proc, "/tmp/worktree", "zhipu", "test task",
                skip_stall=True,
                stdout_counter=stdout_counter,
                stderr_counter=stderr_counter,
            ))
        except (asyncio.CancelledError, RuntimeError):
            pass

    proc.kill.assert_not_called()


def test_scout_stderr_growth_not_killed():
    proc = _make_proc()
    stdout_counter = [0]
    stderr_counter = [0]
    sleep_calls = 0

    async def mock_sleep(seconds):
        nonlocal sleep_calls
        sleep_calls += 1
        stderr_counter[0] = sleep_calls * 512
        if sleep_calls > 35:
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
                proc, "/tmp/worktree", "zhipu", "test task",
                skip_stall=True,
                stdout_counter=stdout_counter,
                stderr_counter=stderr_counter,
            ))
        except (asyncio.CancelledError, RuntimeError):
            pass

    proc.kill.assert_not_called()


def test_scout_no_counters_does_not_kill():
    proc = _make_proc()
    sleep_calls = 0

    async def mock_sleep(seconds):
        nonlocal sleep_calls
        sleep_calls += 1
        if sleep_calls > 35:
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
                proc, "/tmp/worktree", "zhipu", "test task",
                skip_stall=True,
            ))
        except (asyncio.CancelledError, RuntimeError):
            pass

    proc.kill.assert_not_called()


def test_build_mode_frozen_static_still_kills():
    proc = _make_proc()
    stdout_counter = [5000]

    with (
        patch("mtor.worker.translocase._subprocess.run", return_value=_mock_nonempty_diff()),
        patch("mtor.worker.translocase.activity") as mock_activity,
        patch("asyncio.sleep", new_callable=MagicMock) as mock_sleep,
        patch(
            "mtor.worker.translocase._graceful_kill_group", new=AsyncMock()
        ) as kill_group,
        patch("mtor.worker.translocase._kill_process_group") as legacy_kill,
    ):
        async def instant_sleep(seconds):
            return None

        mock_sleep.side_effect = instant_sleep
        mock_activity.is_cancelled.return_value = False
        mock_activity.heartbeat = MagicMock()
        _run(_heartbeat_stall_check(
            proc, "/tmp/worktree", "zhipu", "test task",
            skip_stall=False,
            stdout_counter=stdout_counter,
            worktree_path="/tmp/worktree", attempt_identity="wf:1:frozen",
        ))

    kill_group.assert_awaited_once()
    legacy_kill.assert_not_called()


def test_build_mode_frozen_growing_stdout_not_killed():
    proc = _make_proc()
    stdout_counter = [0]
    sleep_calls = 0

    async def mock_sleep(seconds):
        nonlocal sleep_calls
        sleep_calls += 1
        stdout_counter[0] = sleep_calls * 512
        if sleep_calls > 35:
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
                skip_stall=False,
                stdout_counter=stdout_counter,
            ))
        except (asyncio.CancelledError, RuntimeError):
            pass

    proc.kill.assert_not_called()
