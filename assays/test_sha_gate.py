"""SHA gate tests — auto-deploy on drift between local and worker HEAD.

Tests the _check_worker_sha() function and --skip-sha-check CLI flag
integration across default, scout, and research subcommands.
"""

from __future__ import annotations

import io
import json
import sys
from contextlib import ExitStack
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from mtor.cli import app


# ---------------------------------------------------------------------------
# Helpers (mirrors test_mtor.py patterns)
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
    client.start_workflow = AsyncMock(return_value=handle)

    async def _fake_list(query=None):
        yield MagicMock()

    client.list_workflows = _fake_list

    wf_handle = MagicMock()
    desc = MagicMock()
    desc.status = MagicMock()
    desc.status.name = "COMPLETED"
    wf_handle.describe = AsyncMock(return_value=desc)
    wf_handle.cancel = AsyncMock(return_value=None)
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
    """Context manager: patch _get_client in all modules."""
    stack = ExitStack()
    for target in _CLIENT_PATCH_TARGETS:
        stack.enter_context(patch(target, return_value=(mock_client, None)))
    return stack


# ---------------------------------------------------------------------------
# Unit tests for _check_worker_sha
# ---------------------------------------------------------------------------


class TestCheckWorkerSha:
    """Unit tests for _check_worker_sha in dispatch.py."""

    def test_match_returns_true_no_deploy(self):
        """When local and remote SHA match, returns True without deploying."""
        from mtor.dispatch import _check_worker_sha

        with patch("mtor.dispatch.subprocess") as mock_sp:
            mock_sp.run.side_effect = [
                MagicMock(returncode=0, stdout="abc123def456\n"),
                MagicMock(returncode=0, stdout="abc123def456\n"),
            ]
            result = _check_worker_sha()
        assert result is True
        assert mock_sp.run.call_count == 2  # local + remote only

    def test_mismatch_auto_deploys_and_returns_true(self):
        """SHA mismatch triggers push + merge + restart, then returns True."""
        from mtor.dispatch import _check_worker_sha

        with patch("mtor.dispatch.subprocess") as mock_sp, patch(
            "mtor.dispatch.time"
        ) as mock_time:
            mock_sp.run.side_effect = [
                MagicMock(returncode=0, stdout="aaa111\n"),  # local SHA
                MagicMock(returncode=0, stdout="bbb222\n"),  # remote SHA (diff)
                MagicMock(returncode=0, stdout=""),  # push
                MagicMock(returncode=0, stdout=""),  # merge
                MagicMock(returncode=0, stdout=""),  # restart
            ]
            result = _check_worker_sha()
        assert result is True
        assert mock_sp.run.call_count == 5
        mock_time.sleep.assert_called_once_with(3)

    def test_skip_returns_true_immediately(self):
        """skip=True returns True without any subprocess calls."""
        from mtor.dispatch import _check_worker_sha

        with patch("mtor.dispatch.subprocess") as mock_sp:
            result = _check_worker_sha(skip=True)
        assert result is True
        mock_sp.run.assert_not_called()

    def test_local_git_failure_raises(self):
        """If local git rev-parse fails, raises RuntimeError."""
        from mtor.dispatch import _check_worker_sha

        with patch("mtor.dispatch.subprocess") as mock_sp:
            mock_sp.run.return_value = MagicMock(
                returncode=1, stderr="fatal: not a git repo"
            )
            with pytest.raises(RuntimeError, match="local git HEAD"):
                _check_worker_sha()

    def test_remote_git_failure_raises(self):
        """If SSH to worker fails, raises RuntimeError."""
        from mtor.dispatch import _check_worker_sha

        with patch("mtor.dispatch.subprocess") as mock_sp:
            mock_sp.run.side_effect = [
                MagicMock(returncode=0, stdout="abc123\n"),
                MagicMock(returncode=1, stderr="Connection refused"),
            ]
            with pytest.raises(RuntimeError, match="worker git HEAD"):
                _check_worker_sha()

    def test_push_failure_raises(self):
        """If git push fails during auto-deploy, raises RuntimeError."""
        from mtor.dispatch import _check_worker_sha

        with patch("mtor.dispatch.subprocess") as mock_sp, patch(
            "mtor.dispatch.time"
        ):
            mock_sp.run.side_effect = [
                MagicMock(returncode=0, stdout="aaa\n"),
                MagicMock(returncode=0, stdout="bbb\n"),
                MagicMock(returncode=1, stderr="remote rejected"),
            ]
            with pytest.raises(RuntimeError, match="push failed"):
                _check_worker_sha()

    def test_restart_failure_raises(self):
        """If worker restart fails during auto-deploy, raises RuntimeError."""
        from mtor.dispatch import _check_worker_sha

        with patch("mtor.dispatch.subprocess") as mock_sp, patch(
            "mtor.dispatch.time"
        ):
            mock_sp.run.side_effect = [
                MagicMock(returncode=0, stdout="aaa\n"),
                MagicMock(returncode=0, stdout="bbb\n"),
                MagicMock(returncode=0, stdout=""),  # push ok
                MagicMock(returncode=0, stdout=""),  # merge ok
                MagicMock(returncode=1, stderr="systemctl failed"),  # restart fail
            ]
            with pytest.raises(RuntimeError, match="restart failed"):
                _check_worker_sha()

    def test_deploy_pushes_main_to_deploy_sync(self):
        """Auto-deploy pushes main:deploy-sync to DEPLOY_REMOTE."""
        from mtor.dispatch import _check_worker_sha

        with patch("mtor.dispatch.subprocess") as mock_sp, patch(
            "mtor.dispatch.time"
        ):
            mock_sp.run.side_effect = [
                MagicMock(returncode=0, stdout="aaa\n"),
                MagicMock(returncode=0, stdout="bbb\n"),
                MagicMock(returncode=0, stdout=""),
                MagicMock(returncode=0, stdout=""),
                MagicMock(returncode=0, stdout=""),
            ]
            _check_worker_sha()

        # Third call should be the push
        push_call = mock_sp.run.call_args_list[2]
        push_args = push_call[0][0]
        assert "push" in push_args
        assert any("deploy-sync" in arg for arg in push_args)


# ---------------------------------------------------------------------------
# Integration tests for --skip-sha-check CLI flag
# ---------------------------------------------------------------------------


class TestSkipShaCheckFlag:
    """Integration tests for --skip-sha-check on dispatch subcommands."""

    def test_default_with_skip_sha_check(self):
        """Default dispatch with --skip-sha-check dispatches successfully."""
        mock_client, _ = make_mock_client()
        with _patch_client(mock_client):
            exit_code, data = invoke(
                ["--skip-sha-check", "Make assays/test_feature.py pass"]
            )
        assert exit_code == 0
        assert data["ok"] is True
        assert data["result"]["status"] == "RUNNING"

    def test_scout_with_skip_sha_check(self):
        """Scout with --skip-sha-check dispatches successfully."""
        mock_client, _ = make_mock_client()
        with _patch_client(mock_client):
            exit_code, data = invoke(
                ["scout", "--no-wait", "--skip-sha-check", "Find all issues"]
            )
        assert exit_code == 0
        assert data["ok"] is True

    def test_research_with_skip_sha_check(self):
        """Research with --skip-sha-check dispatches successfully."""
        mock_client, _ = make_mock_client()
        with _patch_client(mock_client):
            exit_code, data = invoke(
                ["research", "--no-wait", "--skip-sha-check", "Compare frameworks"]
            )
        assert exit_code == 0
        assert data["ok"] is True

    def test_dispatch_without_skip_calls_sha_gate(self):
        """Default dispatch without flag calls _check_worker_sha(skip=False)."""
        from mtor import dispatch as dispatch_mod

        mock_client, _ = make_mock_client()
        with _patch_client(mock_client), patch.object(
            dispatch_mod, "_check_worker_sha", return_value=True
        ) as mock_sha:
            exit_code, data = invoke(
                ["Make assays/test_feature.py pass"]
            )
        assert exit_code == 0
        mock_sha.assert_called_once_with(skip=False)

    def test_dispatch_with_skip_passes_skip_true(self):
        """Default dispatch with --skip-sha-check passes skip=True to gate."""
        from mtor import dispatch as dispatch_mod

        mock_client, _ = make_mock_client()
        with _patch_client(mock_client), patch.object(
            dispatch_mod, "_check_worker_sha", return_value=True
        ) as mock_sha:
            exit_code, data = invoke(
                ["--skip-sha-check", "Make assays/test_feature.py pass"]
            )
        assert exit_code == 0
        mock_sha.assert_called_once_with(skip=True)

    def test_scout_skips_sha_gate(self):
        """Scout mode skips _check_worker_sha entirely (read-only task)."""
        from mtor import dispatch as dispatch_mod

        mock_client, _ = make_mock_client()
        with _patch_client(mock_client), patch.object(
            dispatch_mod, "_check_worker_sha", return_value=True
        ) as mock_sha:
            invoke(["scout", "--no-wait", "Find all issues"])
        mock_sha.assert_not_called()

    def test_research_skips_sha_gate(self):
        """Research mode skips _check_worker_sha entirely (read-only task)."""
        from mtor import dispatch as dispatch_mod

        mock_client, _ = make_mock_client()
        with _patch_client(mock_client), patch.object(
            dispatch_mod, "_check_worker_sha", return_value=True
        ) as mock_sha:
            invoke(["research", "--no-wait", "Compare frameworks"])
        mock_sha.assert_not_called()
