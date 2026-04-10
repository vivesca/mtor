"""Tests for graceful subprocess termination (SIGTERM before SIGKILL).

Runs via: cd ~/code/mtor && uv run pytest assays/test_cancel_graceful.py -x
"""

from __future__ import annotations

import asyncio
import sys
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))


class TestGracefulKill:
    """_graceful_kill sends SIGTERM, waits, then SIGKILL if needed."""

    def test_graceful_kill_sends_terminate_first(self):
        """SIGTERM is sent before SIGKILL."""
        from mtor.worker.translocase import _graceful_kill

        proc = AsyncMock()
        proc.returncode = None
        proc.terminate = MagicMock()
        proc.kill = MagicMock()
        proc.wait = AsyncMock(return_value=0)

        asyncio.run(_graceful_kill(proc, timeout=0.1))

        proc.terminate.assert_called_once()

    def test_graceful_kill_kills_if_terminate_times_out(self):
        """SIGKILL fires when process doesn't exit after SIGTERM."""
        from mtor.worker.translocase import _graceful_kill

        proc = AsyncMock()
        proc.returncode = None
        proc.terminate = MagicMock()
        proc.kill = MagicMock()
        proc.wait = AsyncMock(side_effect=asyncio.TimeoutError)

        asyncio.run(_graceful_kill(proc, timeout=0.1))

        proc.terminate.assert_called_once()
        proc.kill.assert_called_once()

    def test_graceful_kill_no_kill_if_terminate_succeeds(self):
        """SIGKILL is NOT sent when SIGTERM works."""
        from mtor.worker.translocase import _graceful_kill

        proc = AsyncMock()
        proc.returncode = None
        proc.terminate = MagicMock()
        proc.kill = MagicMock()
        proc.wait = AsyncMock(return_value=0)

        asyncio.run(_graceful_kill(proc, timeout=1.0))

        proc.terminate.assert_called_once()
        proc.kill.assert_not_called()
