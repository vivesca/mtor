"""Tests for zombie-after-success early exit gate.

When the ribosome subprocess commits work and tests pass, translocase should
return success immediately without entering verdict review loops that cause
the zombie pattern (60+ minutes looping on circuit-broken providers).

Runs via: cd ~/code/mtor && uv run pytest assays/test_zombie_early_exit.py -x
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


class TestExtractTestPaths:
    """_extract_test_paths parses test file paths from YAML frontmatter."""

    def test_extracts_list_from_frontmatter(self):
        from mtor.worker.translocase import _extract_test_paths

        task = "---\ntests:\n  - assays/test_foo.py\n  - assays/test_bar.py\n---\nDo the thing"
        result = _extract_test_paths(task)
        assert result == ["assays/test_foo.py", "assays/test_bar.py"]

    def test_extracts_inline_string(self):
        from mtor.worker.translocase import _extract_test_paths

        task = "---\ntests: assays/test_foo.py assays/test_bar.py\n---\nDo the thing"
        result = _extract_test_paths(task)
        assert result == ["assays/test_foo.py", "assays/test_bar.py"]

    def test_returns_empty_when_no_frontmatter(self):
        from mtor.worker.translocase import _extract_test_paths

        result = _extract_test_paths("Just a regular task without frontmatter")
        assert result == []

    def test_returns_empty_when_no_tests_key(self):
        from mtor.worker.translocase import _extract_test_paths

        task = "---\nname: my-task\n---\nDo the thing"
        result = _extract_test_paths(task)
        assert result == []


class TestEarlyExitCleanCommit:
    """When ribosome commits + tests pass, translocase returns early_exit_clean."""

    def test_translocase_early_exits_on_clean_commit(self, tmp_path):
        """When ribosome commits + tests pass, translocase returns success without verdict loop."""
        from mtor.worker.translocase import translate

        rev_parse_count = 0

        def mock_run(cmd, **kwargs):
            nonlocal rev_parse_count

            if cmd[0] == "pgrep":
                return subprocess.CompletedProcess(cmd, returncode=0, stdout="0\n", stderr="")

            if cmd[0] == "bash" and "-n" in cmd:
                return subprocess.CompletedProcess(cmd, returncode=0, stdout="", stderr="")

            if cmd[0] == "uv" and "pytest" in cmd:
                return subprocess.CompletedProcess(cmd, returncode=0, stdout="1 passed\n", stderr="")

            if cmd[0] == "git":
                subcmd = cmd[1] if len(cmd) > 1 else ""
                if subcmd == "rev-parse":
                    rev_parse_count += 1
                    if rev_parse_count == 1:
                        return subprocess.CompletedProcess(cmd, returncode=0, stdout="aaa111\n", stderr="")
                    return subprocess.CompletedProcess(cmd, returncode=0, stdout="bbb222\n", stderr="")
                if subcmd == "status":
                    return subprocess.CompletedProcess(cmd, returncode=0, stdout="", stderr="")
                if subcmd == "diff":
                    return subprocess.CompletedProcess(cmd, returncode=0, stdout="", stderr="")
                if subcmd == "log":
                    return subprocess.CompletedProcess(cmd, returncode=0, stdout="", stderr="")
                if subcmd == "push":
                    return subprocess.CompletedProcess(cmd, returncode=0, stdout="", stderr="")
                if subcmd == "worktree":
                    return subprocess.CompletedProcess(cmd, returncode=0, stdout="", stderr="")
                if subcmd == "add":
                    return subprocess.CompletedProcess(cmd, returncode=0, stdout="", stderr="")
                if subcmd == "branch":
                    return subprocess.CompletedProcess(cmd, returncode=0, stdout="", stderr="")
                if subcmd == "commit":
                    return subprocess.CompletedProcess(cmd, returncode=0, stdout="", stderr="")
                if subcmd == "rev-list":
                    return subprocess.CompletedProcess(cmd, returncode=0, stdout="1\n", stderr="")
                if subcmd == "pull":
                    return subprocess.CompletedProcess(cmd, returncode=0, stdout="Already up to date.\n", stderr="")

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

        task = "---\ntests:\n  - assays/test_zombie_early_exit.py\n---\n[t-abc123] Fix the zombie bug"

        mock_info = MagicMock()
        mock_info.workflow_id = "test-wf-001"

        with patch("mtor.worker.translocase._subprocess.run", side_effect=mock_run), \
             patch("mtor.worker.translocase.asyncio.create_subprocess_exec", return_value=mock_proc), \
             patch("mtor.worker.translocase.load_health", return_value={}), \
             patch("mtor.worker.translocase.select_provider", return_value="zhipu"), \
             patch("mtor.worker.translocase.save_health"), \
             patch("mtor.worker.translocase.update_health"), \
             patch("mtor.worker.translocase.parse_rate_limit_window", return_value=None), \
             patch("mtor.worker.translocase.activity.info", return_value=mock_info), \
             patch("mtor.worker.translocase.activity.heartbeat"), \
             patch("mtor.worker.translocase.activity.is_cancelled", return_value=False), \
             patch("mtor.worker.translocase.create_task_trace", return_value=None), \
             patch("mtor.worker.translocase.finalize_trace") as mock_finalize, \
             patch("mtor.worker.translocase._create_worktree", return_value=str(tmp_path / "worktree")), \
             patch("mtor.worker.translocase._detect_prior_commits", return_value=[]), \
             patch("mtor.worker.translocase._git_pull_ff_only"), \
             patch("mtor.worker.translocase._git_snapshot", return_value={"stat": "", "numstat": "", "commits": [], "commit_count": 0, "patch": ""}):

            result = _run(translate(
                task=task,
                provider="zhipu",
                mode="build",
                repo=str(tmp_path / "repo"),
            ))

        assert result["success"] is True
        assert result.get("verdict") == "early_exit_clean"
        assert result["post_head"] == "bbb222"
        # Verify finalize_trace was called (trace cleanup)
        mock_finalize.assert_called_once()

    def test_translocase_falls_back_to_verdict_when_tests_fail(self, tmp_path):
        """When commit lands but tests fail, fall through to verdict review."""
        from mtor.worker.translocase import translate

        rev_parse_count = 0

        def mock_run(cmd, **kwargs):
            nonlocal rev_parse_count

            if cmd[0] == "pgrep":
                return subprocess.CompletedProcess(cmd, returncode=0, stdout="0\n", stderr="")
            if cmd[0] == "bash" and "-n" in cmd:
                return subprocess.CompletedProcess(cmd, returncode=0, stdout="", stderr="")
            if cmd[0] == "uv" and "pytest" in cmd:
                return subprocess.CompletedProcess(cmd, returncode=1, stdout="1 failed\n", stderr="AssertionError")
            if cmd[0] == "git":
                subcmd = cmd[1] if len(cmd) > 1 else ""
                if subcmd == "rev-parse":
                    rev_parse_count += 1
                    if rev_parse_count == 1:
                        return subprocess.CompletedProcess(cmd, returncode=0, stdout="aaa111\n", stderr="")
                    return subprocess.CompletedProcess(cmd, returncode=0, stdout="bbb222\n", stderr="")
                if subcmd == "status":
                    return subprocess.CompletedProcess(cmd, returncode=0, stdout="", stderr="")
                if subcmd == "diff":
                    return subprocess.CompletedProcess(cmd, returncode=0, stdout="", stderr="")
                if subcmd == "log":
                    return subprocess.CompletedProcess(cmd, returncode=0, stdout="", stderr="")
                if subcmd == "push":
                    return subprocess.CompletedProcess(cmd, returncode=0, stdout="", stderr="")
                if subcmd == "worktree":
                    return subprocess.CompletedProcess(cmd, returncode=0, stdout="", stderr="")
                if subcmd == "add":
                    return subprocess.CompletedProcess(cmd, returncode=0, stdout="", stderr="")
                if subcmd == "branch":
                    return subprocess.CompletedProcess(cmd, returncode=0, stdout="", stderr="")
                if subcmd == "commit":
                    return subprocess.CompletedProcess(cmd, returncode=0, stdout="", stderr="")
                if subcmd == "rev-list":
                    return subprocess.CompletedProcess(cmd, returncode=0, stdout="1\n", stderr="")
                if subcmd == "pull":
                    return subprocess.CompletedProcess(cmd, returncode=0, stdout="Already up to date.\n", stderr="")
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

        task = "---\ntests:\n  - assays/test_zombie_early_exit.py\n---\n[t-def456] Fix the zombie bug"

        mock_info = MagicMock()
        mock_info.workflow_id = "test-wf-002"

        with patch("mtor.worker.translocase._subprocess.run", side_effect=mock_run), \
             patch("mtor.worker.translocase.asyncio.create_subprocess_exec", return_value=mock_proc), \
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

            result = _run(translate(
                task=task,
                provider="zhipu",
                mode="build",
                repo=str(tmp_path / "repo"),
            ))

        # Should NOT early exit — falls through to normal verdict review
        assert result.get("verdict") != "early_exit_clean"
        assert result["success"] is True  # rc was 0


class TestEarlyExitNoTestsField:
    """When no tests field in spec, early exit still works (commit-only gate)."""

    def test_early_exit_without_tests_field(self, tmp_path):
        """When commit exists but no tests in frontmatter, skip pytest and early exit."""
        from mtor.worker.translocase import translate

        rev_parse_count = 0
        pytest_called = False

        def mock_run(cmd, **kwargs):
            nonlocal rev_parse_count, pytest_called

            if cmd[0] == "pgrep":
                return subprocess.CompletedProcess(cmd, returncode=0, stdout="0\n", stderr="")
            if cmd[0] == "bash" and "-n" in cmd:
                return subprocess.CompletedProcess(cmd, returncode=0, stdout="", stderr="")
            if cmd[0] == "uv" and "pytest" in cmd:
                pytest_called = True
                return subprocess.CompletedProcess(cmd, returncode=0, stdout="", stderr="")
            if cmd[0] == "git":
                subcmd = cmd[1] if len(cmd) > 1 else ""
                if subcmd == "rev-parse":
                    rev_parse_count += 1
                    if rev_parse_count == 1:
                        return subprocess.CompletedProcess(cmd, returncode=0, stdout="aaa111\n", stderr="")
                    return subprocess.CompletedProcess(cmd, returncode=0, stdout="bbb222\n", stderr="")
                if subcmd == "status":
                    return subprocess.CompletedProcess(cmd, returncode=0, stdout="", stderr="")
                if subcmd == "diff":
                    return subprocess.CompletedProcess(cmd, returncode=0, stdout="", stderr="")
                if subcmd == "log":
                    return subprocess.CompletedProcess(cmd, returncode=0, stdout="", stderr="")
                if subcmd == "push":
                    return subprocess.CompletedProcess(cmd, returncode=0, stdout="", stderr="")
                if subcmd == "worktree":
                    return subprocess.CompletedProcess(cmd, returncode=0, stdout="", stderr="")
                if subcmd == "add":
                    return subprocess.CompletedProcess(cmd, returncode=0, stdout="", stderr="")
                if subcmd == "branch":
                    return subprocess.CompletedProcess(cmd, returncode=0, stdout="", stderr="")
                if subcmd == "commit":
                    return subprocess.CompletedProcess(cmd, returncode=0, stdout="", stderr="")
                if subcmd == "rev-list":
                    return subprocess.CompletedProcess(cmd, returncode=0, stdout="1\n", stderr="")
                if subcmd == "pull":
                    return subprocess.CompletedProcess(cmd, returncode=0, stdout="Already up to date.\n", stderr="")
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

        # No frontmatter at all — no tests: field
        task = "[t-no007] Fix the zombie bug without test spec"

        mock_info = MagicMock()
        mock_info.workflow_id = "test-wf-003"

        with patch("mtor.worker.translocase._subprocess.run", side_effect=mock_run), \
             patch("mtor.worker.translocase.asyncio.create_subprocess_exec", return_value=mock_proc), \
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

            result = _run(translate(
                task=task,
                provider="zhipu",
                mode="build",
                repo=str(tmp_path / "repo"),
            ))

        assert result["success"] is True
        assert result.get("verdict") == "early_exit_clean"
        assert not pytest_called  # pytest was NOT called (no test paths found)
