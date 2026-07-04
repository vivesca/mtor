"""Tests for _graceful_kill_group -- the CancelledError-path group reaper.

 translate()'s `except asyncio.CancelledError:` branch must reap the whole
 process group, not just the wrapper pid, so detached `timeout`/`opencode`
 grandchild processes die immediately on activity cancel instead of ghosting
 until systemd's cgroup sweep.

Run: cd ~/code/mtor && uv run pytest assays/test_graceful_kill_group.py -v
"""

from __future__ import annotations

import asyncio
import signal
import subprocess
import sys
from pathlib import Path
import contextlib
from unittest.mock import AsyncMock, MagicMock, patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from mtor.worker.translocase import _graceful_kill_group


def _run(coro):
    return asyncio.run(coro)


def _proc(pid=4242, wait=None, returncode=None):
    proc = MagicMock()
    proc.pid = pid
    proc.returncode = returncode
    proc.terminate = MagicMock()
    proc.kill = MagicMock()
    proc.wait = (
        AsyncMock(return_value=0) if wait is None else AsyncMock(side_effect=wait)
    )
    return proc


def test_graceful_kill_group_sends_sigterm_to_group():
    """SIGTERM goes to the group via os.killpg; no escalation when wait resolves."""
    proc = _proc()

    with (
        patch("mtor.worker.translocase.os.killpg") as killpg,
        patch("mtor.worker.translocase._kill_process_group") as kill_group,
    ):
        _run(_graceful_kill_group(proc, timeout=1.0))

    killpg.assert_called_once_with(proc.pid, signal.SIGTERM)
    proc.terminate.assert_not_called()
    kill_group.assert_not_called()


def test_graceful_kill_group_escalates_on_timeout():
    """When proc.wait() times out, _kill_process_group is invoked."""
    proc = _proc(wait=asyncio.TimeoutError())

    with (
        patch("mtor.worker.translocase.os.killpg"),
        patch("mtor.worker.translocase._kill_process_group") as kill_group,
    ):
        _run(_graceful_kill_group(proc, timeout=0.01))

    kill_group.assert_called_once_with(proc)


def test_cancelled_error_path_uses_group_kill():
    """translate()'s CancelledError branch must call _graceful_kill_group, not _graceful_kill."""
    from mtor.worker import translocase

    graceful_kill = AsyncMock()
    group_kill = AsyncMock()

    mock_proc = AsyncMock()
    mock_proc.pid = 4242
    mock_proc.returncode = None
    mock_proc.stdout = AsyncMock()
    mock_proc.stderr = AsyncMock()
    mock_proc.terminate = MagicMock()
    mock_proc.kill = MagicMock()
    mock_proc.wait = AsyncMock(return_value=0)

    hang_event = asyncio.Event()

    async def hang_tee(*args, **kwargs):
        await hang_event.wait()

    async def hang_heartbeat(*args, **kwargs):
        await hang_event.wait()

    def mock_run(cmd, **kwargs):
        if cmd[0] == "pgrep":
            return subprocess.CompletedProcess(
                cmd, returncode=0, stdout="0\n", stderr=""
            )
        if cmd[0] == "bash" and "-n" in cmd:
            return subprocess.CompletedProcess(cmd, returncode=0, stdout="", stderr="")
        if cmd[0] == "git":
            return subprocess.CompletedProcess(
                cmd, returncode=0, stdout="abc\n", stderr=""
            )
        return subprocess.CompletedProcess(cmd, returncode=0, stdout="", stderr="")

    mock_info = MagicMock()
    mock_info.workflow_id = "test-cancel-001"

    async def scenario():
        task = asyncio.create_task(
            translocase.translate(
                task="[t-deadbeef] Cancel-path test",
                provider="zhipu",
                mode="build",
                repo="/tmp/nonexistent-repo",
            )
        )
        # Yield so translate() reaches the gather(stdout_task, stderr_task) await.
        await asyncio.sleep(0.2)
        task.cancel()
        hang_event.set()  # release the tee/heartbeat mocks so cleanup proceeds
        try:
            await task
        except (asyncio.CancelledError, Exception):
            pass

    patches = [
        patch.object(translocase, "_graceful_kill", graceful_kill),
        patch.object(translocase, "_graceful_kill_group", group_kill),
        patch.object(translocase, "_tee_stream", hang_tee),
        patch.object(translocase, "_heartbeat_stall_check", hang_heartbeat),
        patch.object(
            translocase.asyncio, "create_subprocess_exec", return_value=mock_proc
        ),
        patch.object(translocase._subprocess, "run", side_effect=mock_run),
        patch.object(translocase, "load_health", return_value={}),
        patch.object(translocase, "select_provider", return_value="zhipu"),
        patch.object(translocase, "save_health"),
        patch.object(translocase, "update_health"),
        patch.object(translocase, "parse_rate_limit_window", return_value=None),
        patch.object(translocase.activity, "info", return_value=mock_info),
        patch.object(translocase.activity, "heartbeat"),
        patch.object(translocase.activity, "is_cancelled", return_value=False),
        patch.object(translocase, "create_task_trace", return_value=None),
        patch.object(translocase, "finalize_trace"),
        patch.object(translocase, "_create_worktree", return_value="/tmp/worktree"),
        patch.object(translocase, "_detect_prior_commits", return_value=[]),
        patch.object(translocase, "_git_pull_ff_only"),
        patch.object(
            translocase,
            "_git_snapshot",
            return_value={
                "stat": "",
                "numstat": "",
                "commits": [],
                "commit_count": 0,
                "patch": "",
            },
        ),
        patch.object(translocase, "_main_checkout_state", return_value=None),
        patch.object(translocase, "_active_count", {}),
    ]
    with contextlib.ExitStack() as stack:
        for p in patches:
            stack.enter_context(p)
        _run(scenario())

    group_kill.assert_awaited()
    graceful_kill.assert_not_awaited()
