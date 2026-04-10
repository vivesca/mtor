"""Tests for mtor.ribosome — Python ribosome effector."""

from __future__ import annotations

import sys
from pathlib import Path
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from mtor.harness import HarnessResult
from mtor.ribosome import execute


class TestExecute:
    """Tests for ribosome.execute."""

    @patch("mtor.ribosome.run_harness")
    @patch("mtor.ribosome.load_coaching")
    def test_execute_injects_coaching(self, mock_coaching, mock_run):
        """execute() prepends coaching text to the task before running."""
        mock_coaching.return_value = "COACHING: be excellent"
        mock_run.return_value = HarnessResult(
            stdout="done", stderr="", exit_code=0,
            provider="zhipu", command=["claude"],
        )

        result = execute("write tests", coaching_path="/fake/coaching.md")

        # load_coaching was called
        mock_coaching.assert_called_once()
        # run_harness was called with coaching-injected task
        call_args = mock_run.call_args
        task_arg = call_args[0][1]  # second positional arg (task)
        assert task_arg.startswith("COACHING: be excellent")
        assert "write tests" in task_arg
        assert result["success"] is True

    @patch("mtor.ribosome.run_harness")
    @patch("mtor.ribosome.load_coaching")
    def test_execute_explore_mode(self, mock_coaching, mock_run):
        """execute(mode='explore') appends read-only suffix to the task."""
        mock_coaching.return_value = None
        mock_run.return_value = HarnessResult(
            stdout="analysis done", stderr="", exit_code=0,
            provider="zhipu", command=["claude"],
        )

        result = execute("explain the codebase", mode="explore", fallback=False)

        call_args = mock_run.call_args
        task_arg = call_args[0][1]
        assert "READ-ONLY" in task_arg
        assert "Do NOT modify any files" in task_arg
        assert result["mode"] == "explore"

    @patch("mtor.ribosome.run_harness")
    @patch("mtor.ribosome.load_coaching")
    def test_execute_fallback_on_failure(self, mock_coaching, mock_run):
        """execute() falls back to next provider when primary fails."""
        mock_coaching.return_value = None

        # First call fails, second succeeds
        mock_run.side_effect = [
            HarnessResult(
                stdout="", stderr="error", exit_code=1,
                provider="zhipu", command=["claude"],
            ),
            HarnessResult(
                stdout="success", stderr="", exit_code=0,
                provider="infini", command=["claude"],
            ),
        ]

        result = execute("fix the bug", provider="zhipu", fallback=True)

        assert result["success"] is True
        assert result["provider"] == "infini"
        assert mock_run.call_count == 2
