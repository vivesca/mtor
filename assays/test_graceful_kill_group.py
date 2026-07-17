"""Tests for _graceful_kill_group -- the CancelledError-path group reaper.

 translate()'s `except asyncio.CancelledError:` branch must reap the whole
 process group, not just the wrapper pid, so detached `timeout`/`opencode`
 grandchild processes die immediately on activity cancel instead of ghosting
 until systemd's cgroup sweep.

Run: cd ~/code/mtor && uv run pytest assays/test_graceful_kill_group.py -v
"""

from __future__ import annotations

import asyncio
import contextlib
import inspect
import os
import signal
import subprocess
import sys
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from mtor.worker.translocase import _graceful_kill_group, _kill_process_group


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


def test_graceful_kill_group_signals_only_verified_processes():
    """Graceful stopping uses captured PID identities, not a bare PGID."""
    proc = _proc()
    reap = AsyncMock(return_value=True)
    wait_group = AsyncMock(return_value=True)

    with (
        patch("mtor.worker.translocase._process_identity", return_value="leader"),
        patch(
            "mtor.worker.translocase._descendant_process_identities", return_value={}
        ),
        patch("mtor.worker.translocase._signal_verified_processes") as signal_owned,
        patch("mtor.worker.translocase._reap_detached_descendants", reap),
        patch("mtor.worker.translocase._wait_for_process_group_exit", wait_group),
        patch("mtor.worker.translocase.os.killpg") as killpg,
    ):
        _run(_graceful_kill_group(proc, timeout=1.0))

    signal_owned.assert_called_once_with({proc.pid: "leader"}, signal.SIGTERM)
    reap.assert_awaited_once()
    killpg.assert_not_called()


def test_graceful_kill_group_reaps_verified_processes_after_timeout():
    """A wait timeout is followed by the identity-verified reaper."""
    proc = _proc(wait=asyncio.TimeoutError())
    reap = AsyncMock(return_value=True)

    with (
        patch("mtor.worker.translocase._process_identity", return_value="leader"),
        patch(
            "mtor.worker.translocase._descendant_process_identities", return_value={}
        ),
        patch("mtor.worker.translocase._signal_verified_processes"),
        patch("mtor.worker.translocase._reap_detached_descendants", reap),
        patch(
            "mtor.worker.translocase._wait_for_process_group_exit",
            new=AsyncMock(return_value=True),
        ),
    ):
        _run(_graceful_kill_group(proc, timeout=0.01))

    reap.assert_awaited_once()


def test_exited_leader_never_signals_historical_process_group():
    """A recycled PGID cannot receive a post-exit SIGKILL."""
    proc = _proc(returncode=0)

    with (
        patch("mtor.worker.translocase._process_identity", return_value=None),
        patch(
            "mtor.worker.translocase._descendant_process_identities", return_value={}
        ),
        patch(
            "mtor.worker.translocase._reap_detached_descendants",
            new=AsyncMock(return_value=True),
        ),
        patch(
            "mtor.worker.translocase._wait_for_process_group_exit",
            new=AsyncMock(return_value=True),
        ),
        patch("mtor.worker.translocase.os.killpg") as killpg,
        patch("mtor.worker.translocase._kill_process_group") as kill_group,
    ):
        _run(_graceful_kill_group(proc))

    killpg.assert_not_called()
    kill_group.assert_not_called()


def test_legacy_stall_kill_is_noop_after_leader_exit():
    """A late heartbeat cannot SIGKILL a recycled process group."""
    proc = _proc(returncode=0)

    with patch("mtor.worker.translocase._signal_group") as signal_group:
        _kill_process_group(proc)

    signal_group.assert_not_called()
    proc.kill.assert_not_called()


def test_translate_uses_verified_tree_reaper_for_completion_and_timeout():
    from mtor.worker import translocase

    source = inspect.getsource(translocase.translate)
    assert "await _graceful_kill(proc)" not in source
    assert source.count("await _graceful_kill_group(") == 2


def test_graceful_kill_group_reaps_child_when_leader_exits_promptly(tmp_path):
    """A captured SIGTERM-ignoring child cannot mutate after its leader exits."""
    ready = tmp_path / "child.pid"
    mutations = tmp_path / "mutations.log"
    child_script = (
        "import os, signal, time\n"
        f"ready = {str(ready)!r}\n"
        f"mutations = {str(mutations)!r}\n"
        "signal.signal(signal.SIGTERM, signal.SIG_IGN)\n"
        "with open(ready, 'w') as handle:\n"
        "    handle.write(str(os.getpid()))\n"
        "    handle.flush()\n"
        "    os.fsync(handle.fileno())\n"
        "with open(mutations, 'a', buffering=1) as handle:\n"
        "    os.chdir('/')\n"
        "    while True:\n"
        "        handle.write('tick\\n')\n"
        "        time.sleep(0.01)\n"
    )
    parent_script = (
        "import subprocess, sys, time\n"
        "subprocess.Popen(\n"
        f"    [sys.executable, '-c', {child_script!r}],\n"
        "    stdout=subprocess.DEVNULL,\n"
        "    stderr=subprocess.DEVNULL,\n"
        ")\n"
        "while True:\n"
        "    time.sleep(1)\n"
    )

    async def scenario() -> None:
        proc = await asyncio.create_subprocess_exec(
            sys.executable,
            "-c",
            parent_script,
            stdout=asyncio.subprocess.DEVNULL,
            stderr=asyncio.subprocess.DEVNULL,
            start_new_session=True,
        )
        child_pid = 0
        try:
            deadline = asyncio.get_running_loop().time() + 5
            while not ready.exists() and asyncio.get_running_loop().time() < deadline:
                await asyncio.sleep(0.02)
            assert ready.exists(), "grandchild did not start"
            child_pid = int(ready.read_text())

            while (
                not mutations.exists() or mutations.stat().st_size == 0
            ) and asyncio.get_running_loop().time() < deadline:
                await asyncio.sleep(0.02)
            assert mutations.exists() and mutations.stat().st_size > 0

            # The reaper snapshots the live tree before the wrapper exits on
            # SIGTERM, then kills the identity-verified ignoring child.
            await _graceful_kill_group(proc, timeout=0.5)
            assert proc.returncode is not None
            size_after_stop = mutations.stat().st_size
            await asyncio.sleep(0.2)

            assert mutations.stat().st_size == size_after_stop
        finally:
            with contextlib.suppress(ProcessLookupError):
                os.killpg(proc.pid, signal.SIGKILL)
            if child_pid:
                with contextlib.suppress(ProcessLookupError):
                    os.kill(child_pid, signal.SIGKILL)
            if proc.returncode is None:
                with contextlib.suppress(Exception):
                    await asyncio.wait_for(proc.wait(), timeout=2)

    _run(scenario())


def test_graceful_kill_group_reaps_setsid_child(tmp_path):
    """A child in a new session cannot escape worktree-scoped reaping."""
    ready = tmp_path / "setsid-child.pid"
    mutations = tmp_path / "setsid-mutations.log"
    child_script = (
        "import os, signal, time\n"
        f"ready = {str(ready)!r}\n"
        f"mutations = {str(mutations)!r}\n"
        "os.setsid()\n"
        "signal.signal(signal.SIGTERM, signal.SIG_IGN)\n"
        "with open(ready, 'w') as handle:\n"
        "    handle.write(str(os.getpid()))\n"
        "    handle.flush()\n"
        "    os.fsync(handle.fileno())\n"
        "with open(mutations, 'a', buffering=1) as handle:\n"
        "    os.chdir('/')\n"
        "    while True:\n"
        "        handle.write('tick\\n')\n"
        "        time.sleep(0.01)\n"
    )
    parent_script = (
        "import subprocess, sys, time\n"
        "subprocess.Popen(\n"
        f"    [sys.executable, '-c', {child_script!r}],\n"
        "    stdout=subprocess.DEVNULL,\n"
        "    stderr=subprocess.DEVNULL,\n"
        ")\n"
        "while True:\n"
        "    time.sleep(1)\n"
    )

    async def scenario() -> None:
        proc = await asyncio.create_subprocess_exec(
            sys.executable,
            "-c",
            parent_script,
            cwd=str(tmp_path),
            stdout=asyncio.subprocess.DEVNULL,
            stderr=asyncio.subprocess.DEVNULL,
            start_new_session=True,
        )
        child_pid = 0
        try:
            deadline = asyncio.get_running_loop().time() + 5
            while not ready.exists() and asyncio.get_running_loop().time() < deadline:
                await asyncio.sleep(0.02)
            assert ready.exists(), "setsid grandchild did not start"
            child_pid = int(ready.read_text())
            while (
                not mutations.exists() or mutations.stat().st_size == 0
            ) and asyncio.get_running_loop().time() < deadline:
                await asyncio.sleep(0.02)
            assert mutations.exists() and mutations.stat().st_size > 0
            assert os.getpgid(child_pid) == child_pid
            assert os.getpgid(child_pid) != proc.pid

            # The daemon has left the process group and changed cwd while
            # retaining an open worktree file; its captured lineage still
            # grants task-scoped authority to reap it.

            await _graceful_kill_group(
                proc,
                timeout=0.5,
                worktree_path=str(tmp_path),
            )
            size_after_stop = mutations.stat().st_size
            await asyncio.sleep(0.2)

            assert mutations.stat().st_size == size_after_stop
        finally:
            with contextlib.suppress(ProcessLookupError):
                os.killpg(proc.pid, signal.SIGKILL)
            if child_pid:
                with contextlib.suppress(ProcessLookupError):
                    os.kill(child_pid, signal.SIGKILL)
            if proc.returncode is None:
                with contextlib.suppress(Exception):
                    await asyncio.wait_for(proc.wait(), timeout=2)

    _run(scenario())


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
