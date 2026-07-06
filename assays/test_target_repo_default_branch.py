"""Tests for worker target-repo preflight resolving the default branch dynamically."""

from __future__ import annotations

import subprocess
from unittest.mock import patch

from mtor.dispatch import _worker_target_repo_state


def _completed(stdout: str = "", stderr: str = "", returncode: int = 0):
    return subprocess.CompletedProcess([], returncode, stdout=stdout, stderr=stderr)


def test_target_repo_state_resolves_worker_default_branch():
    with patch("mtor.dispatch.subprocess.run") as mock_run:
        mock_run.side_effect = [
            _completed(stdout="same-sha\n"),
            _completed(
                stdout=(
                    "BRANCH:master\n"
                    "HEAD:same-sha\n"
                    "ORIGIN_MAIN:same-sha\n"
                    "MTOR_STATUS_START\n"
                )
            ),
        ]

        state = _worker_target_repo_state("~/code/mtor")

    assert state["ok"] is True
    assert state["branch"] == "master"
    assert state["origin_sha"] == "same-sha"


def test_target_repo_state_master_default_branch_ok():
    with patch("mtor.dispatch.subprocess.run") as mock_run:
        mock_run.side_effect = [
            _completed(stdout="same-sha\n"),
            _completed(
                stdout=(
                    "BRANCH:master\n"
                    "HEAD:same-sha\n"
                    "ORIGIN_MAIN:same-sha\n"
                    "MTOR_STATUS_START\n"
                )
            ),
        ]

        _worker_target_repo_state("~/code/mtor")

    ssh_cmd = mock_run.call_args_list[1].args[0][2]
    assert "refs/heads/main" not in ssh_cmd
    assert "refs/heads/$branch" in ssh_cmd
