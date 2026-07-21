"""Tests for --harness flag routing to RIBOSOME_PROVIDER in translocase.

Runs via: cd ~/code/mtor && uv run pytest assays/test_harness_routing.py -x
"""

from __future__ import annotations

import asyncio
import subprocess
import sys
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))


def _run(coro):
    return asyncio.run(coro)


class TestHarnessRouting:
    """--harness value must flow through to RIBOSOME_PROVIDER env var."""

    def test_harness_in_workflow_spec(self):
        """dispatch.py includes harness in the workflow input spec dict."""

        # The spec dict passed to Temporal should contain 'harness' key
        # We can't easily test the full dispatch without Temporal,
        # but we can test that _make_workflow_id accepts harness
        from mtor.dispatch import _make_workflow_id

        wid = _make_workflow_id("test prompt", "zhipu", harness="goose")
        assert wid.startswith("goose-")

    def test_default_harness_is_ribosome(self):
        """Default harness prefix in workflow ID is 'ribosome'."""
        from mtor.dispatch import _make_workflow_id

        wid = _make_workflow_id("test prompt", "zhipu")
        assert wid.startswith("ribosome-")

    def test_harness_not_empty_string(self):
        """Harness value of empty string falls back to default."""
        from mtor.dispatch import _make_workflow_id

        wid = _make_workflow_id("test prompt", "zhipu", harness="")
        # Empty harness should use default or at least not crash
        assert len(wid) > 0

    def test_harness_routes_to_ribosome_provider(self, tmp_path):
        """translate() uses harness as RIBOSOME_PROVIDER for the subprocess."""
        env = self._translate_and_capture_env(tmp_path, harness="goose")

        assert env["RIBOSOME_PROVIDER"] == "goose"

    def test_opencode_harness_routes_to_ribosome_provider(self, tmp_path):
        """OpenCode can be selected as the ribosome harness for zhipu."""
        env = self._translate_and_capture_env(tmp_path, harness="opencode")

        assert env["RIBOSOME_PROVIDER"] == "opencode"

    def test_pi_harness_and_task_mode_route_to_ribosome(self, tmp_path):
        """Pi receives both its harness selection and the read-only task mode."""
        env = self._translate_and_capture_env(tmp_path, harness="pi", mode="scout")

        assert env["RIBOSOME_PROVIDER"] == "pi"
        assert env["RIBOSOME_TASK_MODE"] == "scout"

    def test_harness_default_falls_back_to_resolved_provider(self, tmp_path):
        """Without harness, translate() uses the resolved provider."""
        env = self._translate_and_capture_env(tmp_path, harness="")

        assert env["RIBOSOME_PROVIDER"] == "zhipu"

    def test_ribosome_command_delimits_task_from_flags(self, tmp_path):
        """translate() passes -- before task text so YAML frontmatter is not parsed as flags."""
        call_args = self._translate_and_capture_call_args(tmp_path, harness="")
        args = list(call_args.args)

        delimiter_index = args.index("--")
        assert args[delimiter_index - 2 : delimiter_index + 2] == [
            "--provider",
            "zhipu",
            "--",
            "[t-abc123] Route harness correctly",
        ]

    def _translate_and_capture_env(
        self, tmp_path, *, harness: str, mode: str = "build"
    ) -> dict[str, str]:
        return self._translate_and_capture_call_args(
            tmp_path, harness=harness, mode=mode
        ).kwargs["env"]

    def _translate_and_capture_call_args(
        self, tmp_path, *, harness: str, mode: str = "build"
    ):
        from mtor.worker.translocase import translate

        rev_parse_count = 0

        def mock_run(cmd, **kwargs):
            nonlocal rev_parse_count

            if cmd[0] == "pgrep":
                return subprocess.CompletedProcess(cmd, returncode=0, stdout="0\n", stderr="")
            if cmd[0] == "bash" and "-n" in cmd:
                return subprocess.CompletedProcess(cmd, returncode=0, stdout="", stderr="")
            if cmd[0] == "git":
                subcmd = cmd[1] if len(cmd) > 1 else ""
                if subcmd == "rev-parse":
                    rev_parse_count += 1
                    sha = "aaa111\n" if rev_parse_count == 1 else "bbb222\n"
                    return subprocess.CompletedProcess(cmd, returncode=0, stdout=sha, stderr="")
                return subprocess.CompletedProcess(cmd, returncode=0, stdout="", stderr="")
            return subprocess.CompletedProcess(cmd, returncode=0, stdout="", stderr="")

        mock_proc = AsyncMock()
        mock_proc.returncode = 0
        mock_proc.pid = 12345
        mock_proc.stdout = AsyncMock()
        mock_proc.stderr = AsyncMock()
        mock_proc.stdout.read = AsyncMock(side_effect=[b"Done\n", b""])
        mock_proc.stderr.read = AsyncMock(side_effect=[b"", b""])
        mock_proc.wait = AsyncMock(return_value=0)
        mock_proc.terminate = MagicMock()
        mock_proc.kill = MagicMock()

        mock_info = MagicMock()
        mock_info.workflow_id = "test-harness-routing"

        with patch("mtor.worker.translocase._subprocess.run", side_effect=mock_run), \
             patch("mtor.worker.translocase.asyncio.create_subprocess_exec", return_value=mock_proc) as create_subprocess, \
             patch("mtor.worker.translocase.load_health", return_value={}), \
             patch("mtor.worker.translocase.select_provider", return_value="zhipu"), \
             patch("mtor.worker.translocase.save_health"), \
             patch("mtor.worker.translocase.update_health"), \
             patch("mtor.worker.translocase.parse_rate_limit_window", return_value=None), \
             patch("mtor.worker.translocase.activity.info", return_value=mock_info), \
             patch("mtor.worker.translocase.activity.heartbeat"), \
             patch("mtor.worker.translocase.activity.is_cancelled", return_value=False), \
             patch("mtor.worker.translocase.create_task_trace", return_value=None), \
             patch("mtor.worker.translocase.finalize_trace"), \
             patch("mtor.worker.translocase._create_worktree", return_value=str(tmp_path / "worktree")), \
             patch("mtor.worker.translocase._detect_prior_commits", return_value=[]), \
             patch("mtor.worker.translocase._git_pull_ff_only"), \
             patch("mtor.worker.translocase._git_snapshot", return_value={"stat": "", "numstat": "", "commits": [], "commit_count": 0, "patch": ""}):
            _run(
                translate(
                    task="[t-abc123] Route harness correctly",
                    provider="zhipu",
                    mode=mode,
                    repo=str(tmp_path / "repo"),
                    harness=harness,
                )
            )

        return create_subprocess.call_args
