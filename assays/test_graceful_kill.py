"""Tests for _graceful_kill in mtor.worker.translocase.

1. proc exits after terminate within timeout -- no kill needed.
2. proc ignores terminate -- kill called after timeout.
3. proc already exited -- function returns immediately.

Run: cd ~/code/mtor && uv run pytest assays/test_graceful_kill.py -v
"""

from __future__ import annotations

import asyncio
from unittest.mock import AsyncMock, MagicMock

from mtor.worker.translocase import _graceful_kill


def _run(coro):
    return asyncio.run(coro)


def test_terminate_exits_within_timeout():
    """Process exits after terminate within the timeout -- no kill needed."""
    proc = MagicMock(spec=asyncio.subprocess.Process)
    proc.returncode = None
    proc.wait = AsyncMock(return_value=0)
    proc.terminate = MagicMock()
    proc.kill = MagicMock()

    _run(_graceful_kill(proc, timeout=5.0))

    proc.terminate.assert_called_once()
    proc.wait.assert_awaited()
    proc.kill.assert_not_called()


def test_terminate_ignored_kill_called():
    """Process ignores terminate -- kill is called after timeout."""
    proc = MagicMock(spec=asyncio.subprocess.Process)
    proc.returncode = None

    # First wait() (inside wait_for) times out, second wait() succeeds.
    proc.wait = AsyncMock(side_effect=[asyncio.TimeoutError(), 0])
    proc.terminate = MagicMock()
    proc.kill = MagicMock()

    _run(_graceful_kill(proc, timeout=0.1))

    proc.terminate.assert_called_once()
    proc.kill.assert_called_once()
    assert proc.wait.await_count == 2


def test_process_already_exited():
    """Process has already exited -- function returns immediately."""
    proc = MagicMock(spec=asyncio.subprocess.Process)
    proc.returncode = 1  # already dead
    proc.wait = AsyncMock()
    proc.terminate = MagicMock()
    proc.kill = MagicMock()

    _run(_graceful_kill(proc, timeout=5.0))

    proc.terminate.assert_not_called()
    proc.wait.assert_not_awaited()
    proc.kill.assert_not_called()
