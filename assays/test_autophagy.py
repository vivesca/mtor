"""Tests for mtor/autophagy.py — salvage ganglion commits."""

from __future__ import annotations

import subprocess
from unittest.mock import MagicMock, patch, call

import pytest

from mtor.autophagy import SalvageResult, salvage


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _run_mock(returncode=0, stdout="", stderr="") -> MagicMock:
    m = MagicMock(spec=subprocess.CompletedProcess)
    m.returncode = returncode
    m.stdout = stdout
    m.stderr = stderr
    return m


REPO = "/fake/repo"

COMMIT_A = "a" * 40
COMMIT_B = "b" * 40
COMMIT_C = "c" * 40


# ---------------------------------------------------------------------------
# test_salvage_extracts_ganglion_commits
# ---------------------------------------------------------------------------


class TestSalvageExtractsGanglionCommits:
    """salvage() cherry-picks new commits from ganglion and returns them."""

    @patch("mtor.autophagy.subprocess.run")
    def test_salvage_extracts_ganglion_commits(self, mock_run):
        # git remote -> has ganglion
        # git fetch ganglion -> ok
        # git rev-parse --abbrev-ref HEAD -> main
        # git rev-parse --verify ganglion/main -> ok
        # git log --format=%H HEAD..ganglion/main -> two commits
        # git merge-base --is-ancestor (x2) -> not merged
        # git cherry-pick --no-commit (x2, reversed order) -> ok
        # uv run pytest -> pass
        mock_run.side_effect = [
            _run_mock(stdout="ganglion\norigin\n"),          # git remote
            _run_mock(),                                      # git fetch ganglion
            _run_mock(stdout="main\n"),                       # git rev-parse --abbrev-ref
            _run_mock(),                                      # git rev-parse --verify
            _run_mock(stdout=f"{COMMIT_A}\n{COMMIT_B}\n"),   # git log
            _run_mock(returncode=1),                          # merge-base A (not merged)
            _run_mock(returncode=1),                          # merge-base B (not merged)
            _run_mock(),                                      # cherry-pick B (reversed: B first)
            _run_mock(),                                      # cherry-pick A
            _run_mock(returncode=0, stdout="3 passed", stderr=""),  # pytest
        ]

        result = salvage(REPO)

        assert result.fetched == 2
        assert len(result.cherry_picked) == 2
        assert result.error is None
        assert result.tests_passed is True
        # Verify cherry-pick calls (reversed: B then A)
        cherry_pick_calls = [
            c for c in mock_run.call_args_list
            if c[0][0][0:2] == ["git", "cherry-pick"]
        ]
        assert len(cherry_pick_calls) == 2
        assert cherry_pick_calls[0] == call(
            ["git", "cherry-pick", "--no-commit", COMMIT_B],
            capture_output=True, text=True, cwd=REPO,
        )
        assert cherry_pick_calls[1] == call(
            ["git", "cherry-pick", "--no-commit", COMMIT_A],
            capture_output=True, text=True, cwd=REPO,
        )


# ---------------------------------------------------------------------------
# test_salvage_skips_already_merged
# ---------------------------------------------------------------------------


class TestSalvageSkipsAlreadyMerged:
    """salvage() skips commits that are already merged into HEAD."""

    @patch("mtor.autophagy.subprocess.run")
    def test_salvage_skips_already_merged(self, mock_run):
        mock_run.side_effect = [
            _run_mock(stdout="ganglion\n"),               # git remote
            _run_mock(),                                   # git fetch ganglion
            _run_mock(stdout="main\n"),                    # branch name
            _run_mock(),                                   # rev-parse --verify
            _run_mock(stdout=f"{COMMIT_A}\n{COMMIT_B}\n{COMMIT_C}\n"),  # git log (3 commits)
            _run_mock(returncode=0),                       # A already merged
            _run_mock(returncode=1),                       # B not merged
            _run_mock(returncode=0),                       # C already merged
            _run_mock(),                                   # cherry-pick B (only non-merged)
            _run_mock(returncode=0, stdout="1 passed", stderr=""),  # pytest
        ]

        result = salvage(REPO)

        assert result.fetched == 3
        assert result.cherry_picked == [COMMIT_B]
        assert COMMIT_A in result.skipped
        assert COMMIT_C in result.skipped
        assert result.error is None
        assert result.tests_passed is True


# ---------------------------------------------------------------------------
# test_salvage_runs_tests
# ---------------------------------------------------------------------------


class TestSalvageRunsTests:
    """salvage() runs the test suite after cherry-picking."""

    @patch("mtor.autophagy.subprocess.run")
    def test_salvage_runs_tests(self, mock_run):
        mock_run.side_effect = [
            _run_mock(stdout="ganglion\n"),               # git remote
            _run_mock(),                                   # git fetch ganglion
            _run_mock(stdout="main\n"),                    # branch name
            _run_mock(),                                   # rev-parse --verify
            _run_mock(stdout=f"{COMMIT_A}\n"),             # git log (1 commit)
            _run_mock(returncode=1),                       # not merged
            _run_mock(),                                   # cherry-pick A
            _run_mock(returncode=0, stdout="5 passed", stderr="OK"),  # pytest
        ]

        result = salvage(REPO)

        # Verify pytest was called
        pytest_calls = [
            c for c in mock_run.call_args_list
            if c[0][0][0:3] == ["uv", "run", "pytest"]
        ]
        assert len(pytest_calls) == 1
        assert result.tests_passed is True
        assert "5 passed" in result.test_output

    @patch("mtor.autophagy.subprocess.run")
    def test_salvage_rolls_back_on_test_failure(self, mock_run):
        mock_run.side_effect = [
            _run_mock(stdout="ganglion\n"),               # git remote
            _run_mock(),                                   # git fetch ganglion
            _run_mock(stdout="main\n"),                    # branch name
            _run_mock(),                                   # rev-parse --verify
            _run_mock(stdout=f"{COMMIT_A}\n"),             # git log
            _run_mock(returncode=1),                       # not merged
            _run_mock(),                                   # cherry-pick A
            _run_mock(returncode=1, stdout="1 failed", stderr="ERROR"),  # pytest fails
            _run_mock(),                                   # git reset --hard (rollback)
        ]

        result = salvage(REPO)

        assert result.tests_passed is False
        assert result.error is not None
        assert "tests failed" in result.error
        # Verify rollback happened
        reset_calls = [
            c for c in mock_run.call_args_list
            if c[0][0] == ["git", "reset", "--hard", "HEAD"]
        ]
        assert len(reset_calls) == 1

    @patch("mtor.autophagy.subprocess.run")
    def test_salvage_no_new_commits(self, mock_run):
        """When ganglion has no new commits, salvage is a no-op."""
        mock_run.side_effect = [
            _run_mock(stdout="ganglion\n"),               # git remote
            _run_mock(),                                   # git fetch ganglion
            _run_mock(stdout="main\n"),                    # branch name
            _run_mock(),                                   # rev-parse --verify
            _run_mock(stdout=""),                           # git log (empty)
        ]

        result = salvage(REPO)

        assert result.fetched == 0
        assert result.cherry_picked == []
        assert result.error is None
        assert result.tests_passed is True
