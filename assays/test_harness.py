"""Tests for harness execution — spawn coding agents as subprocesses.

Runs via: cd ~/code/mtor && uv run pytest assays/test_harness.py -v
"""

from __future__ import annotations

from unittest.mock import patch

import pytest


class TestRunHarness:
    """Harness subprocess execution."""

    def test_claude_builds_correct_command(self):
        """Claude harness builds correct subprocess command."""
        from mtor.harness import _build_claude_command

        cmd, env = _build_claude_command(
            prompt="implement foo",
            work_dir="/tmp/test",
            bare=False,
            supervised=False,
            max_turns=25,
        )
        assert "claude" in cmd[0] or "claude" in " ".join(cmd)
        assert "--print" in cmd
        assert "--dangerously-skip-permissions" in cmd

    def test_result_captures_output(self):
        """HarnessResult captures stdout, stderr, exit_code, duration."""
        from mtor.harness import HarnessResult

        result = HarnessResult(
            stdout="done",
            stderr="",
            exit_code=0,
            duration_s=12.5,
        )
        assert result.exit_code == 0
        assert result.duration_s == 12.5

    def test_unknown_harness_raises(self):
        """Unknown harness name raises ValueError."""
        from mtor.harness import run_harness

        with pytest.raises(ValueError, match="unknown"):
            run_harness(
                "nonexistent_harness",
                "test prompt",
                work_dir="/tmp",
            )

    def test_provider_to_harness_mapping(self):
        """Provider names map to correct harness names."""
        from mtor.harness import provider_to_harness

        assert provider_to_harness("zhipu") == "claude"
        assert provider_to_harness("infini") == "claude"
        assert provider_to_harness("volcano") == "claude"
        assert provider_to_harness("goose") == "goose"
        assert provider_to_harness("droid") == "droid"
        assert provider_to_harness("gemini") == "gemini"
        assert provider_to_harness("codex") == "codex"
