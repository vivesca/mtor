"""Tests for the `mtor deploy` command — germline (~/germline) → worker sync.

`mtor deploy` pushes the local germline HEAD to origin/main, fast-forwards the
worker checkout, restarts the worker, then verifies health via _doctor(). It is
the sibling of dispatch._check_worker_sha(): both deploy ~/germline (REPO_DIR),
not the mtor code repo that infra.deploy() handles.

GitHub push-propagation lag can leave the worker's `git fetch origin main`
seeing the OLD origin/main, so a clean `git merge --ff-only origin/main` exit is
not proof the commit landed. These tests pin the post-merge HEAD verification:
the worker must reach the pushed SHA before the restart, else deploy fails
closed (no restart on stale code, no health check claiming success).
"""

from __future__ import annotations

from unittest.mock import MagicMock, patch

import pytest


def _restart_issued(mock_sp) -> bool:
    """True if any subprocess call restarted the worker unit."""
    return any(
        "systemctl --user restart mtor-worker" in str(c.args[0])
        for c in mock_sp.run.call_args_list
    )


class TestDeployCommand:
    """Unit tests for the inline `mtor deploy` orchestration in cli.py."""

    def test_deploy_verifies_worker_head_then_restarts(self):
        """Worker reaches the pushed SHA on the first try → restart, then doctor."""
        from mtor.cli import deploy

        with (
            patch("mtor.cli.subprocess") as mock_sp,
            patch("time.sleep") as mock_sleep,
            patch("mtor.cli._doctor") as mock_doctor,
        ):
            mock_sp.run.side_effect = [
                MagicMock(returncode=0, stdout=""),       # push
                MagicMock(returncode=0, stdout="aaa\n"),  # local rev-parse (pushed SHA)
                MagicMock(returncode=0, stdout=""),       # worker ff-merge
                MagicMock(returncode=0, stdout="aaa\n"),  # worker rev-parse (matches)
                MagicMock(returncode=0, stdout=""),       # restart
            ]
            deploy()

        assert _restart_issued(mock_sp)
        mock_doctor.assert_called_once()
        # Matched on first attempt → no backoff sleep, only the post-restart settle.
        mock_sleep.assert_called_once_with(3)

    def test_deploy_retries_then_succeeds_when_lag_resolves(self):
        """First fetch sees stale origin/main; a later attempt lands the SHA."""
        from mtor.cli import deploy

        with (
            patch("mtor.cli.subprocess") as mock_sp,
            patch("time.sleep") as mock_sleep,
            patch("mtor.cli._doctor") as mock_doctor,
        ):
            mock_sp.run.side_effect = [
                MagicMock(returncode=0, stdout=""),       # push
                MagicMock(returncode=0, stdout="aaa\n"),  # local rev-parse
                MagicMock(returncode=0, stdout=""),       # merge attempt 1
                MagicMock(returncode=0, stdout="bbb\n"),  # worker rev-parse 1 (stale)
                MagicMock(returncode=0, stdout=""),       # merge attempt 2
                MagicMock(returncode=0, stdout="aaa\n"),  # worker rev-parse 2 (now matches)
                MagicMock(returncode=0, stdout=""),       # restart
            ]
            deploy()

        assert _restart_issued(mock_sp)
        mock_doctor.assert_called_once()
        # One backoff sleep (2.0) before the successful retry, plus settle (3).
        sleep_args = [c.args for c in mock_sleep.call_args_list]
        assert (2.0,) in sleep_args
        assert (3,) in sleep_args

    def test_deploy_fails_closed_when_worker_head_stale(self):
        """ff-merge exits 0 but worker HEAD never reaches the pushed SHA →
        exits non-zero BEFORE restart, and never runs the health check."""
        from mtor.cli import deploy

        with (
            patch("mtor.cli.subprocess") as mock_sp,
            patch("time.sleep"),
            patch("mtor.cli._doctor") as mock_doctor,
        ):
            mock_sp.run.side_effect = [
                MagicMock(returncode=0, stdout=""),       # push
                MagicMock(returncode=0, stdout="aaa\n"),  # local rev-parse
                MagicMock(returncode=0, stdout=""),       # merge attempt 1 (no-op, exits 0)
                MagicMock(returncode=0, stdout="bbb\n"),  # worker HEAD still stale
                MagicMock(returncode=0, stdout=""),       # merge attempt 2
                MagicMock(returncode=0, stdout="bbb\n"),  # worker HEAD still stale
                MagicMock(returncode=0, stdout=""),       # merge attempt 3
                MagicMock(returncode=0, stdout="bbb\n"),  # worker HEAD still stale
            ]
            with pytest.raises(SystemExit) as exc:
                deploy()

        assert exc.value.code != 0
        assert not _restart_issued(mock_sp)
        mock_doctor.assert_not_called()

    def test_deploy_fails_when_worker_merge_errors(self):
        """A real (non-ff) merge failure is no longer swallowed — deploy exits
        non-zero before restart instead of marking the sync step ok."""
        from mtor.cli import deploy

        with (
            patch("mtor.cli.subprocess") as mock_sp,
            patch("time.sleep"),
            patch("mtor.cli._doctor") as mock_doctor,
        ):
            mock_sp.run.side_effect = [
                MagicMock(returncode=0, stdout=""),               # push
                MagicMock(returncode=0, stdout="aaa\n"),          # local rev-parse
                MagicMock(returncode=1, stderr="diverged"),       # merge fails
            ]
            with pytest.raises(SystemExit) as exc:
                deploy()

        assert exc.value.code != 0
        assert not _restart_issued(mock_sp)
        mock_doctor.assert_not_called()

    def test_deploy_push_failure_exits_before_merge(self):
        """A failed push exits non-zero and never touches the worker."""
        from mtor.cli import deploy

        with (
            patch("mtor.cli.subprocess") as mock_sp,
            patch("time.sleep"),
            patch("mtor.cli._doctor") as mock_doctor,
        ):
            mock_sp.run.side_effect = [
                MagicMock(returncode=1, stderr="remote rejected"),  # push fails
            ]
            with pytest.raises(SystemExit) as exc:
                deploy()

        assert exc.value.code != 0
        assert mock_sp.run.call_count == 1  # nothing after the failed push
        mock_doctor.assert_not_called()
