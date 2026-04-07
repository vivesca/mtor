"""Tests for mtor deptor freeze/thaw — deep lock blocking dispatch and sync.

Runs via: cd ~/code/mtor && uv run pytest assays/test_freeze.py -x -v
"""

from __future__ import annotations

import io
import json
import subprocess
import sys
from contextlib import ExitStack
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, patch

import mtor
from mtor.cli import app


# ---------------------------------------------------------------------------
# Helpers (mirrored from test_pause.py)
# ---------------------------------------------------------------------------


def invoke(args: list[str] | None = None) -> tuple[int, dict]:
    """Invoke CLI and return (exit_code, parsed_json)."""
    captured = io.StringIO()
    old_stdout = sys.stdout
    exit_code = 0
    try:
        sys.stdout = captured
        app(args or [])
    except SystemExit as exc:
        exit_code = exc.code if isinstance(exc.code, int) else 1
    finally:
        sys.stdout = old_stdout

    output = captured.getvalue()
    try:
        data = json.loads(output)
    except json.JSONDecodeError as exc:
        raise AssertionError(
            f"Output is not valid JSON. Exit={exit_code}\nOutput: {output!r}\nException: {exc}"
        ) from exc
    return exit_code, data


def make_mock_client():
    """Build a minimal async mock Temporal client."""
    client = MagicMock()

    handle = MagicMock()
    handle.id = "ribosome-test1234"
    start_coro = AsyncMock(return_value=handle)
    client.start_workflow = start_coro

    async def _fake_list(query=None):
        yield MagicMock()

    client.list_workflows = _fake_list

    wf_handle = MagicMock()
    desc = MagicMock()
    desc.status = MagicMock()
    desc.status.name = "COMPLETED"
    desc.start_time = MagicMock()
    desc.start_time.isoformat.return_value = "2026-04-06T00:00:00+00:00"
    desc.close_time = MagicMock()
    desc.close_time.isoformat.return_value = "2026-04-06T00:01:00+00:00"
    wf_handle.describe = AsyncMock(return_value=desc)
    wf_handle.terminate = AsyncMock(return_value=None)
    client.get_workflow_handle = MagicMock(return_value=wf_handle)

    async def _fake_count(query=None):
        return 0

    client.count_workflows = _fake_count

    return client, wf_handle


_CLIENT_PATCH_TARGETS = [
    "mtor.cli._get_client",
    "mtor.doctor._get_client",
    "mtor.dispatch._get_client",
]


def _patch_client(mock_client):
    """Context manager: patch _get_client in all modules that import it."""
    stack = ExitStack()
    for target in _CLIENT_PATCH_TARGETS:
        stack.enter_context(patch(target, return_value=(mock_client, None)))
    stack.enter_context(patch("mtor.cli._check_dedup", return_value=None))
    return stack


def _init_git_repo(path: Path) -> None:
    """Create a minimal git repo so sync doesn't crash."""
    subprocess.run(["git", "init", "-b", "main"], cwd=path, capture_output=True, check=True)
    subprocess.run(["git", "config", "user.email", "t@t"], cwd=path, capture_output=True)
    subprocess.run(["git", "config", "user.name", "t"], cwd=path, capture_output=True)
    (path / "README.md").write_text("init")
    subprocess.run(["git", "add", "."], cwd=path, capture_output=True)
    subprocess.run(["git", "commit", "-m", "init"], cwd=path, capture_output=True)


# ---------------------------------------------------------------------------
# Freeze / Thaw tests
# ---------------------------------------------------------------------------


def test_test_freeze_blocks_dispatch_and_sync(tmp_path, monkeypatch):
    """Freeze blocks both dispatch and watch sync cycles."""
    monkeypatch.setattr(mtor, "REPO_DIR", str(tmp_path))

    from mtor.watch import freeze

    freeze(str(tmp_path))

    # 1. Dispatch is blocked
    mock_client, _ = make_mock_client()
    with _patch_client(mock_client):
        exit_code, data = invoke(["Make assays/test_foo.py pass"])

    assert exit_code == 1
    assert data["ok"] is False
    assert data["error"]["code"] == "FROZEN"
    assert "thaw" in data["fix"].lower()

    # 2. Watch sync is blocked (skips cycle with "frozen" error)
    _init_git_repo(tmp_path)
    from mtor.watch import run_watch

    cycles = []
    stats = run_watch(
        str(tmp_path),
        once=True,
        on_cycle=lambda c: cycles.append(c),
    )
    assert stats.cycles == 1
    assert stats.total_errors == 1
    assert len(cycles) == 1
    assert cycles[0].error == "frozen"

    # Cleanup
    from mtor.watch import thaw
    thaw(str(tmp_path))


def test_test_thaw_resumes_all(tmp_path, monkeypatch):
    """After thaw, dispatch and watch sync resume normally."""
    monkeypatch.setattr(mtor, "REPO_DIR", str(tmp_path))

    from mtor.watch import freeze, thaw

    # Freeze then thaw
    freeze(str(tmp_path))
    thaw(str(tmp_path))

    # 1. Dispatch succeeds after thaw
    mock_client, _ = make_mock_client()
    with _patch_client(mock_client):
        exit_code, data = invoke(["scout", "--no-wait", "Find patterns in code"])

    assert exit_code == 0
    assert data["ok"] is True
    assert "workflow_id" in data["result"]

    # 2. Watch sync succeeds after thaw
    _init_git_repo(tmp_path)
    from mtor.watch import run_watch

    stats = run_watch(str(tmp_path), once=True)
    assert stats.cycles == 1
    assert stats.total_errors == 0
