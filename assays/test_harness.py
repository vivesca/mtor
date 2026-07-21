"""Tests for mtor.harness — provider harness runners."""

from __future__ import annotations

import sys
from pathlib import Path
from unittest.mock import MagicMock, patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from mtor.harness import (
    PROVIDER_HARNESS_MAP,
    HarnessResult,
    build_command,
    get_harness,
    run_harness,
)


class TestRunHarness:
    """Tests for harness.run_harness."""

    def test_run_harness_claude_builds_command(self):
        """Provider 'claude' builds a claude --print command."""
        cmd = build_command("claude", "write a test")
        assert cmd[0] == "claude"
        assert "--print" in cmd
        assert "write a test" in cmd

    def test_run_harness_droid_builds_exec_command(self):
        """Provider 'droid' builds a droid exec command."""
        cmd = build_command("droid", "inspect the repo")
        assert cmd == ["droid", "exec", "inspect the repo"]

    def test_run_harness_opencode_builds_glm52_command(self):
        """Provider 'opencode' builds an OpenCode command on GLM-5.2."""
        cmd = build_command("opencode", "write a test")
        assert cmd[:2] == ["opencode", "run"]
        assert "--model" in cmd
        assert "zhipuai-coding-plan/glm-5.2" in cmd
        assert "--dangerously-skip-permissions" in cmd
        assert "write a test" in cmd

    def test_run_harness_zhipu_uses_opencode_glm52(self):
        """Provider 'zhipu' should not route through the Claude harness."""
        cmd = build_command("zhipu", "write a test")
        assert cmd[:2] == ["opencode", "run"]
        assert "zhipuai-coding-plan/glm-5.2" in cmd

    def test_run_harness_pi_builds_ephemeral_glm52_command(self):
        cmd = build_command("pi", "inspect the repo")
        assert cmd[:3] == ["pi", "--provider", "zai-coding-cn"]
        assert ["--model", "glm-5.2"] == cmd[3:5]
        assert "--no-session" in cmd
        assert "--mode" in cmd
        assert "inspect the repo" in cmd

    @patch("mtor.harness.subprocess.run")
    def test_run_harness_result_captures_output(self, mock_run):
        """run_harness returns a HarnessResult with captured stdout/stderr."""
        mock_proc = MagicMock()
        mock_proc.stdout = "hello output"
        mock_proc.stderr = "some stderr"
        mock_proc.returncode = 0
        mock_run.return_value = mock_proc

        result = run_harness("claude", "test task", work_dir="/tmp")
        assert isinstance(result, HarnessResult)
        assert result.stdout == "hello output"
        assert result.stderr == "some stderr"
        assert result.exit_code == 0
        assert result.provider == "claude"
        assert result.command[0] == "claude"

    def test_run_harness_unknown_raises(self):
        """Unknown provider raises ValueError."""
        import pytest

        with pytest.raises(ValueError, match="Unknown provider"):
            run_harness("nonexistent_provider", "test task")

    def test_provider_to_harness_mapping(self):
        """Each provider in PROVIDER_HARNESS_MAP maps to a known harness."""
        expected_providers = {
            "claude",
            "zhipu",
            "gemini",
            "codex",
            "opencode",
            "pi",
            "goose",
            "droid",
        }
        assert set(PROVIDER_HARNESS_MAP.keys()) == expected_providers
        assert PROVIDER_HARNESS_MAP["zhipu"] == "opencode"
        # Every harness value should have a command builder
        for provider, harness in PROVIDER_HARNESS_MAP.items():
            assert isinstance(harness, str)
            assert len(harness) > 0
            # get_harness should not raise for known providers
            assert get_harness(provider) == harness
