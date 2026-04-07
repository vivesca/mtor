"""Tests for mtor pause/resume — file-based dispatch blocker.

Runs via: cd ~/code/mtor && uv run pytest assays/test_pause.py -x -v
"""

from __future__ import annotations

import io
import json
import sys
from contextlib import ExitStack
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, patch

import mtor
from mtor.cli import app


# ---------------------------------------------------------------------------
# Helpers (mirrored from test_mtor.py)
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


# ---------------------------------------------------------------------------
# Pause / Resume tests
# ---------------------------------------------------------------------------


def test_test_pause_blocks_dispatch(tmp_path, monkeypatch):
    """When paused, dispatch returns PAUSED error."""
    monkeypatch.setattr(mtor, "REPO_DIR", str(tmp_path))

    # Create pause file
    from mtor.watch import pause
    pause(str(tmp_path))

    mock_client, _ = make_mock_client()
    with _patch_client(mock_client):
        exit_code, data = invoke(["Make assays/test_foo.py pass"])

    assert exit_code == 1
    assert data["ok"] is False
    assert data["error"]["code"] == "PAUSED"
    assert "resume" in data["fix"].lower()

    # Cleanup
    from mtor.watch import resume as _resume
    _resume(str(tmp_path))


def test_test_resume_allows_dispatch(tmp_path, monkeypatch):
    """After resume, dispatch succeeds normally."""
    monkeypatch.setattr(mtor, "REPO_DIR", str(tmp_path))

    # Pause then resume
    from mtor.watch import pause, resume
    pause(str(tmp_path))
    resume(str(tmp_path))

    mock_client, _ = make_mock_client()
    with _patch_client(mock_client):
        # Use scout mode which doesn't require --spec
        exit_code, data = invoke(["scout", "--no-wait", "Find patterns in code"])

    assert exit_code == 0
    assert data["ok"] is True
    assert "workflow_id" in data["result"]


# ---------------------------------------------------------------------------
# Pause/resume unit tests
# ---------------------------------------------------------------------------


class TestPauseResume:
    """Unit tests for pause file mechanism."""

    def test_is_paused_false_by_default(self, tmp_path):
        from mtor.watch import is_paused
        assert is_paused(str(tmp_path)) is False

    def test_pause_creates_file(self, tmp_path):
        from mtor.watch import is_paused, pause
        path = pause(str(tmp_path))
        assert path.exists()
        assert is_paused(str(tmp_path)) is True

    def test_resume_removes_file(self, tmp_path):
        from mtor.watch import is_paused, pause, resume
        pause(str(tmp_path))
        assert is_paused(str(tmp_path)) is True
        was_paused = resume(str(tmp_path))
        assert was_paused is True
        assert is_paused(str(tmp_path)) is False

    def test_resume_idempotent_when_not_paused(self, tmp_path):
        from mtor.watch import resume
        was_paused = resume(str(tmp_path))
        assert was_paused is False

    def test_pause_file_contains_timestamp(self, tmp_path):
        from mtor.watch import pause
        path = pause(str(tmp_path))
        content = path.read_text()
        assert "paused_at:" in content


class TestPauseCLI:
    """CLI commands for pause and resume."""

    def test_pause_cmd_returns_paused(self, tmp_path, monkeypatch):
        monkeypatch.setattr(mtor, "REPO_DIR", str(tmp_path))
        exit_code, data = invoke(["pause"])
        assert exit_code == 0
        assert data["ok"] is True
        assert data["result"]["status"] == "paused"

    def test_pause_cmd_idempotent(self, tmp_path, monkeypatch):
        monkeypatch.setattr(mtor, "REPO_DIR", str(tmp_path))
        invoke(["pause"])
        exit_code, data = invoke(["pause"])
        assert exit_code == 0
        assert data["result"]["status"] == "already_paused"

    def test_resume_cmd_returns_resumed(self, tmp_path, monkeypatch):
        monkeypatch.setattr(mtor, "REPO_DIR", str(tmp_path))
        invoke(["pause"])
        exit_code, data = invoke(["resume"])
        assert exit_code == 0
        assert data["ok"] is True
        assert data["result"]["status"] == "resumed"

    def test_resume_cmd_idempotent(self, tmp_path, monkeypatch):
        monkeypatch.setattr(mtor, "REPO_DIR", str(tmp_path))
        exit_code, data = invoke(["resume"])
        assert exit_code == 0
        assert data["result"]["status"] == "already_running"


class TestWatchPaused:
    """Watch skips sync cycles when paused."""

    def test_watch_skips_cycle_when_paused(self, tmp_path):
        from mtor.watch import pause, run_watch

        # Initialize a git repo so sync doesn't crash
        import subprocess
        subprocess.run(["git", "init", "-b", "main"], cwd=tmp_path, capture_output=True, check=True)
        subprocess.run(["git", "config", "user.email", "t@t"], cwd=tmp_path, capture_output=True)
        subprocess.run(["git", "config", "user.name", "t"], cwd=tmp_path, capture_output=True)
        (tmp_path / "README.md").write_text("init")
        subprocess.run(["git", "add", "."], cwd=tmp_path, capture_output=True)
        subprocess.run(["git", "commit", "-m", "init"], cwd=tmp_path, capture_output=True)

        pause(str(tmp_path))
        cycles = []
        stats = run_watch(
            str(tmp_path),
            once=True,
            on_cycle=lambda c: cycles.append(c),
        )
        assert stats.cycles == 1
        assert stats.total_errors == 1
        assert len(cycles) == 1
        assert cycles[0].error == "paused"

    def test_watch_resumes_after_unpause(self, tmp_path):
        from mtor.watch import pause, resume, run_watch
        import subprocess

        subprocess.run(["git", "init", "-b", "main"], cwd=tmp_path, capture_output=True, check=True)
        subprocess.run(["git", "config", "user.email", "t@t"], cwd=tmp_path, capture_output=True)
        subprocess.run(["git", "config", "user.name", "t"], cwd=tmp_path, capture_output=True)
        (tmp_path / "README.md").write_text("init")
        subprocess.run(["git", "add", "."], cwd=tmp_path, capture_output=True)
        subprocess.run(["git", "commit", "-m", "init"], cwd=tmp_path, capture_output=True)

        pause(str(tmp_path))
        resume(str(tmp_path))
        stats = run_watch(str(tmp_path), once=True)
        assert stats.cycles == 1
        assert stats.total_errors == 0
