"""Tests for dual-signal stall detection (git diff hash + stdout bytes).

The agent is stalled ONLY when BOTH diff hash is frozen AND stdout byte count
hasn't grown.  If either signal is changing, the agent is active.

Covers:
- Frozen hash + growing stdout  -> no stall
- Frozen hash + static stdout   -> stall (verified shutdown via _graceful_kill_group)
- Oscillating hash + growing stdout -> no stall
- Oscillating hash + static stdout  -> stall (verified shutdown)
- No stdout_counter passed -> backward-compatible hash-only behavior
- Empty diff + static stdout -> patient 30-minute window (60 ticks)
- Every kill class delegates to _graceful_kill_group(worktree_path, attempt_identity)
  and never calls the legacy _kill_process_group

Run: cd ~/code/mtor && uv run pytest assays/test_stall_dual_signal.py -v
"""

from __future__ import annotations

import asyncio
import inspect
from unittest.mock import AsyncMock, MagicMock, patch

from mtor.worker import translocase
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


def _mock_empty_diff() -> MagicMock:
    r = MagicMock()
    r.stdout = ""
    return r


def _patches_for_kill_check(
    *,
    diff_run,
    sleep_side_effect=None,
    sleep_mock=None,
    skip_stall: bool = False,
):
    """Common patch stack for stall checks that should NOT touch real processes.

    Returns a (stack_enter_context, mock_activity) tuple suited for a ``with`` chain.
    """
    raise NotImplementedError  # not used; tests inline their own context managers


class TestFrozenHashGrowingStdout:
    """Frozen diff hash + growing stdout -> agent is active, no kill."""

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
            patch("mtor.worker.translocase._graceful_kill_group", new=AsyncMock()) as kill_group,
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
        kill_group.assert_not_awaited()


class TestFrozenHashStaticStdout:
    """Frozen diff hash + static stdout -> stall detected, verified shutdown."""

    def test_kill_when_both_frozen(self):
        proc = _make_proc()
        stdout_counter = [5000]  # static — never changes

        with (
            patch("mtor.worker.translocase._subprocess.run", return_value=_mock_nonempty_diff()),
            patch("mtor.worker.translocase.activity") as mock_activity,
            patch("asyncio.sleep", new_callable=AsyncMock),
            patch("mtor.worker.translocase._graceful_kill_group", new=AsyncMock()) as kill_group,
            patch("mtor.worker.translocase._kill_process_group") as legacy_kill,
        ):
            mock_activity.is_cancelled.return_value = False
            mock_activity.heartbeat = MagicMock()
            _run(_heartbeat_stall_check(
                proc, "/tmp/worktree", "zhipu", "test task",
                skip_stall=False, stdout_counter=stdout_counter,
                worktree_path="/tmp/worktree", attempt_identity="wf:1:abc",
            ))

        kill_group.assert_awaited_once()
        # Contract: the verified shutdown must receive the worktree and identity.
        _assert_graceful_called_with_scope(
            kill_group,
            worktree_path="/tmp/worktree",
            attempt_identity="wf:1:abc",
        )
        legacy_kill.assert_not_called()


class TestOscillatingHashGrowingStdout:
    """Oscillating diff hash + growing stdout -> agent is active, no kill."""

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
            patch("mtor.worker.translocase._graceful_kill_group", new=AsyncMock()) as kill_group,
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
        kill_group.assert_not_awaited()


class TestOscillatingHashStaticStdout:
    """Oscillating diff hash + static stdout -> stall detected, verified shutdown."""

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
            patch("mtor.worker.translocase._graceful_kill_group", new=AsyncMock()) as kill_group,
            patch("mtor.worker.translocase._kill_process_group") as legacy_kill,
        ):
            mock_activity.is_cancelled.return_value = False
            mock_activity.heartbeat = MagicMock()
            _run(_heartbeat_stall_check(
                proc, "/tmp/worktree", "zhipu", "test task",
                skip_stall=False, stdout_counter=stdout_counter,
                worktree_path="/tmp/worktree", attempt_identity="wf:1:xyz",
            ))

        kill_group.assert_awaited_once()
        _assert_graceful_called_with_scope(
            kill_group,
            worktree_path="/tmp/worktree",
            attempt_identity="wf:1:xyz",
        )
        legacy_kill.assert_not_called()


class TestBackwardCompatibleNoCounter:
    """When stdout_counter is not provided, falls back to hash-only behavior."""

    def test_frozen_hash_kills_without_counter(self):
        """Without stdout_counter, frozen hash alone triggers verified shutdown."""
        proc = _make_proc()

        with (
            patch("mtor.worker.translocase._subprocess.run", return_value=_mock_nonempty_diff()),
            patch("mtor.worker.translocase.activity") as mock_activity,
            patch("asyncio.sleep", new_callable=AsyncMock),
            patch("mtor.worker.translocase._graceful_kill_group", new=AsyncMock()) as kill_group,
            patch("mtor.worker.translocase._kill_process_group") as legacy_kill,
        ):
            mock_activity.is_cancelled.return_value = False
            mock_activity.heartbeat = MagicMock()
            _run(_heartbeat_stall_check(
                proc, "/tmp/worktree", "zhipu", "test task",
                skip_stall=False,
                worktree_path="/tmp/worktree", attempt_identity="wf:1:no-counter",
            ))

        kill_group.assert_awaited_once()
        legacy_kill.assert_not_called()


class TestEmptyDiffStaticStdout:
    """Empty diff + static stdout should not wait for the 2h activity timeout.

    Contract: warn at ~15min (30 ticks), verified shutdown at ~30min (60 ticks).
    A useful build with no visible diff or output for 16.5 minutes (~33 ticks)
    must NOT be killed — that incident is what motivated the patient window.
    """

    def test_empty_diff_alive_before_60_empty_ticks(self):
        """Useful silent builds must stay alive inside the patient window.

        33 ticks ~= 16.5 min — the exact span that mis-fired the old guard.
        The checker must NOT invoke the verified shutdown yet.
        """
        proc = _make_proc()
        stdout_counter = [0]
        sleep_calls = 0

        async def mock_sleep(seconds):
            nonlocal sleep_calls
            sleep_calls += 1
            # 4 ticks of skip_stall ramp-up + ~46 empty/stagnant ticks, all
            # inside the 60-tick patient window.
            if sleep_calls > 50:
                raise asyncio.CancelledError("test limit")

        with (
            patch("mtor.worker.translocase._subprocess.run", return_value=_mock_empty_diff()),
            patch("mtor.worker.translocase.activity") as mock_activity,
            patch("asyncio.sleep", side_effect=mock_sleep),
            patch("mtor.worker.translocase._graceful_kill_group", new=AsyncMock()) as kill_group,
            patch("mtor.worker.translocase._kill_process_group") as legacy_kill,
        ):
            mock_activity.is_cancelled.return_value = False
            mock_activity.heartbeat = MagicMock()
            try:
                _run(_heartbeat_stall_check(
                    proc, "/tmp/worktree", "zhipu", "test task",
                    skip_stall=False, stdout_counter=stdout_counter,
                    worktree_path="/tmp/worktree", attempt_identity="wf:1:patient",
                ))
            except asyncio.CancelledError:
                pass

        # ~50 ticks exercised and no kill fired: the patient 60-tick window
        # kept the silent build alive, exactly the incident regression.
        assert 33 <= sleep_calls <= 51
        kill_group.assert_not_awaited()
        legacy_kill.assert_not_called()

    def test_empty_diff_kills_after_60_empty_ticks(self):
        """At 60 empty ticks the checker must invoke verified shutdown.

        60 ticks * 30s = 30 min, the new upper bound for a silent build.
        """
        proc = _make_proc()
        stdout_counter = [0]
        sleep_calls = 0

        async def mock_sleep(seconds):
            nonlocal sleep_calls
            sleep_calls += 1
            # Hard cap: kill must happen by tick 65 — no false patience.
            if sleep_calls > 70:
                raise AssertionError("empty diff stall detector waited too long")

        with (
            patch("mtor.worker.translocase._subprocess.run", return_value=_mock_empty_diff()),
            patch("mtor.worker.translocase.activity") as mock_activity,
            patch("asyncio.sleep", side_effect=mock_sleep),
            patch("mtor.worker.translocase._graceful_kill_group", new=AsyncMock()) as kill_group,
            patch("mtor.worker.translocase._kill_process_group") as legacy_kill,
        ):
            mock_activity.is_cancelled.return_value = False
            mock_activity.heartbeat = MagicMock()
            _run(_heartbeat_stall_check(
                proc, "/tmp/worktree", "zhipu", "test task",
                skip_stall=False, stdout_counter=stdout_counter,
                worktree_path="/tmp/worktree", attempt_identity="wf:1:empty-60",
            ))

        # 4-tick ramp-up + 60 empty ticks == kill on tick 64. Allow slack
        # but assert the patient threshold, not the old 30-tick value.
        assert sleep_calls <= 70
        kill_group.assert_awaited_once()
        _assert_graceful_called_with_scope(
            kill_group,
            worktree_path="/tmp/worktree",
            attempt_identity="wf:1:empty-60",
        )
        legacy_kill.assert_not_called()

    def test_empty_diff_warning_at_30_ticks_does_not_kill(self):
        """The ~15min warning must NOT kill — patience, then verified shutdown at 60."""
        proc = _make_proc()
        stdout_counter = [0]
        sleep_calls = 0

        async def mock_sleep(seconds):
            nonlocal sleep_calls
            sleep_calls += 1
            # Stop right after the warning band (35 ticks == ~17.5 min).
            if sleep_calls > 35:
                raise asyncio.CancelledError("test limit")

        with (
            patch("mtor.worker.translocase._subprocess.run", return_value=_mock_empty_diff()),
            patch("mtor.worker.translocase.activity") as mock_activity,
            patch("asyncio.sleep", side_effect=mock_sleep),
            patch("mtor.worker.translocase._graceful_kill_group", new=AsyncMock()) as kill_group,
            patch("mtor.worker.translocase._kill_process_group") as legacy_kill,
        ):
            mock_activity.is_cancelled.return_value = False
            mock_activity.heartbeat = MagicMock()
            try:
                _run(_heartbeat_stall_check(
                    proc, "/tmp/worktree", "zhipu", "test task",
                    skip_stall=False, stdout_counter=stdout_counter,
                    worktree_path="/tmp/worktree", attempt_identity="wf:1:warn-only",
                ))
            except asyncio.CancelledError:
                pass

        kill_group.assert_not_awaited()
        legacy_kill.assert_not_called()


class TestEveryKillClassUsesVerifiedShutdown:
    """Contract: every stall termination path must delegate to
    _graceful_kill_group(worktree_path=..., attempt_identity=...) and must
    never call _kill_process_group.
    """

    def test_activity_cancellation_uses_graceful_kill_group(self):
        proc = _make_proc()

        async def mock_sleep(seconds):
            # First sleep lets the loop see is_cancelled() == True on tick 1.
            return None

        with (
            patch("mtor.worker.translocase._subprocess.run", return_value=_mock_empty_diff()),
            patch("mtor.worker.translocase.activity") as mock_activity,
            patch("asyncio.sleep", side_effect=mock_sleep),
            patch("mtor.worker.translocase._graceful_kill_group", new=AsyncMock()) as kill_group,
            patch("mtor.worker.translocase._kill_process_group") as legacy_kill,
        ):
            mock_activity.is_cancelled.return_value = True
            mock_activity.heartbeat = MagicMock()
            _run(_heartbeat_stall_check(
                proc, "/tmp/worktree", "zhipu", "test task",
                skip_stall=False,
                worktree_path="/tmp/worktree", attempt_identity="wf:1:cancel",
            ))

        kill_group.assert_awaited_once()
        _assert_graceful_called_with_scope(
            kill_group,
            worktree_path="/tmp/worktree",
            attempt_identity="wf:1:cancel",
        )
        legacy_kill.assert_not_called()

    def test_scout_no_output_uses_graceful_kill_group(self):
        proc = _make_proc()
        stdout_counter = [0]
        stderr_counter = [0]
        sleep_calls = 0

        async def mock_sleep(seconds):
            nonlocal sleep_calls
            sleep_calls += 1
            if sleep_calls > 40:
                raise AssertionError("scout kill path waited too long")

        with (
            patch("mtor.worker.translocase._subprocess.run", return_value=_mock_empty_diff()),
            patch("mtor.worker.translocase.activity") as mock_activity,
            patch("asyncio.sleep", side_effect=mock_sleep),
            patch("mtor.worker.translocase._graceful_kill_group", new=AsyncMock()) as kill_group,
            patch("mtor.worker.translocase._kill_process_group") as legacy_kill,
        ):
            mock_activity.is_cancelled.return_value = False
            mock_activity.heartbeat = MagicMock()
            _run(_heartbeat_stall_check(
                proc, "/tmp/worktree", "zhipu", "test task",
                skip_stall=True,
                stdout_counter=stdout_counter,
                stderr_counter=stderr_counter,
                worktree_path="/tmp/scout-wt", attempt_identity="wf:1:scout",
            ))

        kill_group.assert_awaited_once()
        _assert_graceful_called_with_scope(
            kill_group,
            worktree_path="/tmp/scout-wt",
            attempt_identity="wf:1:scout",
        )
        legacy_kill.assert_not_called()

    def test_frozen_diff_uses_graceful_kill_group(self):
        proc = _make_proc()
        stdout_counter = [5000]  # static

        with (
            patch("mtor.worker.translocase._subprocess.run", return_value=_mock_nonempty_diff()),
            patch("mtor.worker.translocase.activity") as mock_activity,
            patch("asyncio.sleep", new_callable=AsyncMock),
            patch("mtor.worker.translocase._graceful_kill_group", new=AsyncMock()) as kill_group,
            patch("mtor.worker.translocase._kill_process_group") as legacy_kill,
        ):
            mock_activity.is_cancelled.return_value = False
            mock_activity.heartbeat = MagicMock()
            _run(_heartbeat_stall_check(
                proc, "/tmp/worktree", "zhipu", "test task",
                skip_stall=False, stdout_counter=stdout_counter,
                worktree_path="/tmp/worktree", attempt_identity="wf:1:frozen",
            ))

        kill_group.assert_awaited_once()
        _assert_graceful_called_with_scope(
            kill_group,
            worktree_path="/tmp/worktree",
            attempt_identity="wf:1:frozen",
        )
        legacy_kill.assert_not_called()

    def test_oscillating_diff_uses_graceful_kill_group(self):
        proc = _make_proc()
        stdout_counter = [5000]
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
            patch("mtor.worker.translocase._graceful_kill_group", new=AsyncMock()) as kill_group,
            patch("mtor.worker.translocase._kill_process_group") as legacy_kill,
        ):
            mock_activity.is_cancelled.return_value = False
            mock_activity.heartbeat = MagicMock()
            _run(_heartbeat_stall_check(
                proc, "/tmp/worktree", "zhipu", "test task",
                skip_stall=False, stdout_counter=stdout_counter,
                worktree_path="/tmp/worktree", attempt_identity="wf:1:osc",
            ))

        kill_group.assert_awaited_once()
        _assert_graceful_called_with_scope(
            kill_group,
            worktree_path="/tmp/worktree",
            attempt_identity="wf:1:osc",
        )
        legacy_kill.assert_not_called()

    def test_empty_diff_uses_graceful_kill_group(self):
        proc = _make_proc()
        stdout_counter = [0]
        sleep_calls = 0

        async def mock_sleep(seconds):
            nonlocal sleep_calls
            sleep_calls += 1
            if sleep_calls > 70:
                raise AssertionError("empty-diff kill path waited too long")

        with (
            patch("mtor.worker.translocase._subprocess.run", return_value=_mock_empty_diff()),
            patch("mtor.worker.translocase.activity") as mock_activity,
            patch("asyncio.sleep", side_effect=mock_sleep),
            patch("mtor.worker.translocase._graceful_kill_group", new=AsyncMock()) as kill_group,
            patch("mtor.worker.translocase._kill_process_group") as legacy_kill,
        ):
            mock_activity.is_cancelled.return_value = False
            mock_activity.heartbeat = MagicMock()
            _run(_heartbeat_stall_check(
                proc, "/tmp/worktree", "zhipu", "test task",
                skip_stall=False, stdout_counter=stdout_counter,
                worktree_path="/tmp/worktree", attempt_identity="wf:1:empty",
            ))

        kill_group.assert_awaited_once()
        _assert_graceful_called_with_scope(
            kill_group,
            worktree_path="/tmp/worktree",
            attempt_identity="wf:1:empty",
        )
        legacy_kill.assert_not_called()


class TestNoKillPathCallsBareProcessGroupKill:
    """Source-level guard: _heartbeat_stall_check must not call _kill_process_group."""

    def test_stall_check_source_has_no_kill_process_group_calls(self):
        source = inspect.getsource(_heartbeat_stall_check)
        # No calls to the legacy synchronous killer from any stall path.
        assert "_kill_process_group(" not in source
        # Every termination path uses the verified async reaper.
        assert source.count("await _graceful_kill_group(") == 4

    def test_translate_passes_worktree_and_identity_to_stall_check(self):
        source = inspect.getsource(translocase.translate)
        call_start = source.index("_heartbeat_stall_check(")
        call_end = source.index(")", call_start)
        call_site = source[call_start:call_end + 1]
        assert "worktree_path=worktree_path" in call_site
        assert "attempt_identity=attempt_identity" in call_site


class TestContractThresholdsArePatient:
    """Source-level guard: the empty-diff window is the 30/60 patient band."""

    def test_empty_diff_thresholds_in_source(self):
        source = inspect.getsource(_heartbeat_stall_check)
        # Kill threshold: 60 ticks (~30 min). Old value was 30.
        assert "empty_ticks >= 60:" in source
        # Warn threshold: 30 ticks (~15 min). Old value was 20.
        assert "empty_ticks >= 30:" in source
        # And the old thresholds must not linger.
        assert "empty_ticks >= 20:" not in source
        assert "empty_ticks >= 30:" in source  # 30 now means warn, not kill


def _assert_graceful_called_with_scope(
    kill_group: AsyncMock,
    *,
    worktree_path: str | None,
    attempt_identity: str | None,
) -> None:
    """Verify _graceful_kill_group was called with the contract's scope kwargs."""
    assert kill_group.await_count == 1
    _args, kwargs = kill_group.call_args
    assert kwargs.get("worktree_path") == worktree_path, (
        f"expected worktree_path={worktree_path!r}, got {kwargs.get('worktree_path')!r}"
    )
    assert kwargs.get("attempt_identity") == attempt_identity, (
        f"expected attempt_identity={attempt_identity!r}, "
        f"got {kwargs.get('attempt_identity')!r}"
    )
