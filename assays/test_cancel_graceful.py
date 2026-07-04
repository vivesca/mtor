"""Tests for graceful subprocess termination (SIGTERM before SIGKILL).

_graceful_kill signals the whole process group (start_new_session=True makes
proc.pid the pgid) so the ribosome wrapper's children — the `timeout NNN
opencode run ...` pair — die with it instead of ghosting on after cancel.

Runs via: cd ~/code/mtor && uv run pytest assays/test_cancel_graceful.py -x
"""

from __future__ import annotations

import asyncio
import signal
import sys
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))


def _proc(pid=4242, wait=None):
    proc = AsyncMock()
    proc.returncode = None
    proc.pid = pid
    proc.terminate = MagicMock()
    proc.kill = MagicMock()
    proc.wait = (
        AsyncMock(return_value=0) if wait is None else AsyncMock(side_effect=wait)
    )
    return proc


class TestGracefulKillGroup:
    """_graceful_kill signals the process group, not just the wrapper."""

    def test_graceful_kill_signals_process_group(self):
        """SIGTERM goes to the group via killpg; terminate is fallback only."""
        from mtor.worker import translocase

        proc = _proc()
        with patch.object(translocase.os, "killpg") as killpg:
            asyncio.run(translocase._graceful_kill(proc, timeout=0.1))

        assert (proc.pid, signal.SIGTERM) in [c.args for c in killpg.call_args_list]
        proc.terminate.assert_not_called()

    def test_graceful_kill_sweeps_group_after_clean_exit(self):
        """Even when the wrapper exits on SIGTERM, the group is swept with SIGKILL."""
        from mtor.worker import translocase

        proc = _proc()
        with patch.object(translocase.os, "killpg") as killpg:
            asyncio.run(translocase._graceful_kill(proc, timeout=0.1))

        assert (proc.pid, signal.SIGKILL) in [c.args for c in killpg.call_args_list]
        proc.kill.assert_not_called()

    def test_graceful_kill_escalates_group_kill_on_timeout(self):
        """SIGKILL goes to the group when the wrapper ignores SIGTERM."""
        from mtor.worker import translocase

        proc = _proc(wait=asyncio.TimeoutError)
        with patch.object(translocase.os, "killpg") as killpg:
            asyncio.run(translocase._graceful_kill(proc, timeout=0.1))

        calls = [c.args for c in killpg.call_args_list]
        assert (proc.pid, signal.SIGTERM) in calls
        assert (proc.pid, signal.SIGKILL) in calls


class TestGracefulKillFallback:
    """When killpg is unavailable, fall back to single-process signals."""

    def test_graceful_kill_sends_terminate_first(self):
        """SIGTERM fallback is sent before SIGKILL."""
        from mtor.worker import translocase

        proc = _proc()
        with patch.object(translocase.os, "killpg", side_effect=ProcessLookupError):
            asyncio.run(translocase._graceful_kill(proc, timeout=0.1))

        proc.terminate.assert_called_once()

    def test_graceful_kill_kills_if_terminate_times_out(self):
        """SIGKILL fallback fires when the process ignores SIGTERM."""
        from mtor.worker import translocase

        proc = _proc(wait=asyncio.TimeoutError)
        with patch.object(translocase.os, "killpg", side_effect=ProcessLookupError):
            asyncio.run(translocase._graceful_kill(proc, timeout=0.1))

        proc.terminate.assert_called_once()
        proc.kill.assert_called_once()

    def test_graceful_kill_no_kill_if_terminate_succeeds(self):
        """SIGKILL fallback is NOT sent when SIGTERM works."""
        from mtor.worker import translocase

        proc = _proc()
        with patch.object(translocase.os, "killpg", side_effect=ProcessLookupError):
            asyncio.run(translocase._graceful_kill(proc, timeout=1.0))

        proc.terminate.assert_called_once()
        proc.kill.assert_not_called()
