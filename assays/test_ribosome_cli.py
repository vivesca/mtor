"""Tests for ribosome execution engine — prompt building, coaching, harness dispatch.

Runs via: cd ~/code/mtor && uv run pytest assays/test_ribosome_cli.py -v
"""

from __future__ import annotations

from unittest.mock import MagicMock, patch

import pytest


class TestExecute:
    """Ribosome execute() orchestration."""

    def test_injects_coaching_into_prompt(self, tmp_path):
        """Coaching content is prepended to the prompt."""
        from mtor.ribosome import execute

        coaching = tmp_path / "coaching.md"
        coaching.write_text("## Rules\n- Be careful\n")

        with patch("mtor.ribosome.run_harness") as mock_harness:
            mock_harness.return_value = MagicMock(
                stdout="done", stderr="", exit_code=0, duration_s=5.0
            )
            execute(
                "implement foo",
                provider="zhipu",
                work_dir=str(tmp_path),
                coaching_path=coaching,
            )
            called_prompt = mock_harness.call_args[0][1]  # second positional arg
            assert "<coaching-notes>" in called_prompt
            assert "## Rules" in called_prompt
            assert "implement foo" in called_prompt

    def test_explore_mode_wraps_prompt(self, tmp_path):
        """Explore mode wraps the user query in explore system prompt."""
        from mtor.ribosome import execute

        with patch("mtor.ribosome.run_harness") as mock_harness:
            mock_harness.return_value = MagicMock(
                stdout="findings", stderr="", exit_code=0, duration_s=3.0
            )
            execute(
                "what patterns exist in mtor/",
                provider="zhipu",
                work_dir=str(tmp_path),
                mode="explore",
            )
            called_prompt = mock_harness.call_args[0][1]
            assert "explore" in called_prompt.lower()
            assert "what patterns exist" in called_prompt

    def test_fallback_on_failure(self, tmp_path):
        """First provider fails, falls back to next in priority."""
        from mtor.ribosome import execute

        call_count = 0

        def mock_run(harness, prompt, **kwargs):
            nonlocal call_count
            call_count += 1
            if call_count == 1:
                return MagicMock(stdout="", stderr="rate limited", exit_code=1, duration_s=1.0)
            return MagicMock(stdout="done", stderr="", exit_code=0, duration_s=5.0)

        with patch("mtor.ribosome.run_harness", side_effect=mock_run):
            result = execute(
                "implement foo",
                provider="zhipu",
                work_dir=str(tmp_path),
                fallback=True,
            )
            assert result.exit_code == 0
            assert call_count >= 2

    def test_no_fallback_returns_failure(self, tmp_path):
        """With fallback=False, first failure is final."""
        from mtor.ribosome import execute

        with patch("mtor.ribosome.run_harness") as mock_harness:
            mock_harness.return_value = MagicMock(
                stdout="", stderr="error", exit_code=1, duration_s=1.0
            )
            result = execute(
                "implement foo",
                provider="zhipu",
                work_dir=str(tmp_path),
                fallback=False,
            )
            assert result.exit_code == 1
