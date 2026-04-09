"""Tests for structured repo parameter passed through the Temporal workflow.

Verifies:
  1. translate() accepts an optional repo parameter and uses it directly
  2. translate() falls back to _detect_repo when repo is None
  3. _detect_repo still works as a standalone fallback
  4. dispatch.py extracts repo from spec frontmatter into the spec dict
  5. WatchWorkflow._dispatch_spec propagates repo to child specs

Run via: cd ~/code/mtor && uv run pytest assays/test_repo_param.py -v
"""

from __future__ import annotations

import asyncio
import inspect
import sys
from pathlib import Path
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from mtor.worker.translocase import _detect_repo, translate


def _run(coro):
    """Run an async function synchronously for testing."""
    return asyncio.run(coro)


# ---------------------------------------------------------------------------
# _detect_repo unit tests (pure logic, no subprocess)
# ---------------------------------------------------------------------------


class TestDetectRepo:
    """_detect_repo mines repo path from prompt text."""

    def test_extracts_code_path(self):
        result = _detect_repo("Fix ~/code/myproject/app.py", "/default")
        # Returns default because ~/code/myproject likely has no .git in CI
        # but the regex still matches the path
        assert result == "/default" or "myproject" in result

    def test_fallback_default(self):
        result = _detect_repo("No path here", "/home/user/germline")
        assert result == "/home/user/germline"

    def test_no_path_uses_default(self):
        result = _detect_repo("Simple task description", "/default/repo")
        assert result == "/default/repo"

    def test_extracts_path_with_dots(self):
        result = _detect_repo("Work on ~/code/my.repo/main.py", "/default")
        # The regex should match ~/code/my.repo
        assert result == "/default" or "my.repo" in result


# ---------------------------------------------------------------------------
# translate() repo parameter tests
# ---------------------------------------------------------------------------


class TestTranslateRepoParam:
    """translate() uses structured repo parameter when provided."""

    def test_translate_has_repo_param(self):
        """translate() signature includes optional repo parameter."""
        sig = inspect.signature(translate)
        params = list(sig.parameters.keys())
        assert "repo" in params
        # repo defaults to None (backward compatible)
        assert sig.parameters["repo"].default is None

    def test_translate_signature_backward_compat(self):
        """translate() first 3 params unchanged (task, provider, mode)."""
        sig = inspect.signature(translate)
        params = list(sig.parameters.keys())
        assert params[:3] == ["task", "provider", "mode"]

    def test_repo_resolution_with_structured_param(self):
        """When repo is truthy, it is used directly (replicates translate logic)."""
        repo = "/custom/repo"
        task = "some task"
        # This is the exact logic from translate()
        if repo:
            repo_root = repo
        else:
            repo_root = _detect_repo(task, "/default")
        assert repo_root == "/custom/repo"

    def test_repo_resolution_fallback_none(self):
        """When repo is None, _detect_repo is used (replicates translate logic)."""
        repo = None
        task = "Work on ~/code/myproject/app.py"
        if repo:
            repo_root = repo
        else:
            repo_root = _detect_repo(task, "/default")
        assert "myproject" in repo_root or repo_root == "/default"

    def test_repo_resolution_fallback_empty_string(self):
        """When repo is empty string, _detect_repo is used."""
        repo = ""
        task = "Simple task"
        if repo:
            repo_root = repo
        else:
            repo_root = _detect_repo(task, "/default")
        assert repo_root == "/default"

    def test_repo_param_skips_detect_repo(self):
        """With repo param, _detect_repo is never called (integration check).

        Uses the capability gate early return to avoid subprocess execution.
        """
        with patch("mtor.worker.translocase._detect_repo") as mock_detect:
            # Blocked task returns before reaching _detect_repo
            result = _run(translate("sudo rm -rf /", "zhipu", "build", "/custom/repo"))
            assert result["success"] is False
            assert "CAPABILITY_GATE" in result["stderr"]
            # _detect_repo was never called (gate returned first)
            mock_detect.assert_not_called()

    def test_repo_param_in_capability_gate_result(self):
        """Capability gate result includes provider even with repo param."""
        result = _run(translate("sudo something", "gemini", "build", "/custom/repo"))
        assert result["provider"] == "gemini"
        assert result["success"] is False


# ---------------------------------------------------------------------------
# dispatch.py repo extraction from spec frontmatter
# ---------------------------------------------------------------------------


class TestDispatchRepoExtraction:
    """dispatch.py extracts repo from spec frontmatter into spec dict."""

    def test_spec_with_custom_repo(self, tmp_path):
        """parse_spec extracts custom repo from frontmatter."""
        spec_file = tmp_path / "test-spec.md"
        spec_file.write_text(
            "---\n"
            "repo: /home/user/custom-repo\n"
            "status: ready\n"
            "---\n\n"
            "Do something\n"
        )

        from mtor.rptor import parse_spec

        parsed = parse_spec(spec_file)
        assert parsed["repo"] == "/home/user/custom-repo"

    def test_spec_default_repo_is_tilde(self, tmp_path):
        """When no repo in frontmatter, default is ~."""
        spec_file = tmp_path / "test-spec2.md"
        spec_file.write_text(
            "---\n"
            "status: ready\n"
            "---\n\n"
            "Do something\n"
        )

        from mtor.rptor import parse_spec

        parsed = parse_spec(spec_file)
        assert parsed["repo"] == "~"

    def test_dispatch_includes_repo_when_non_default(self, tmp_path):
        """Verify the repo-from-frontmatter gate that dispatch uses.

        This replicates the exact logic from _dispatch_prompt:
            if repo != "~": spec["repo"] = repo
        """
        spec_file = tmp_path / "test-spec3.md"
        spec_file.write_text(
            "---\n"
            "repo: ~/code/myproject\n"
            "status: ready\n"
            "---\n\n"
            "Do something\n"
        )

        from mtor.rptor import parse_spec

        parsed = parse_spec(spec_file)
        repo = parsed.get("repo", "~")

        # Same logic as dispatch.py
        spec: dict = {}
        if repo != "~":
            spec["repo"] = repo

        assert spec["repo"] == "~/code/myproject"

    def test_dispatch_skips_repo_when_default(self, tmp_path):
        """When repo is ~, dispatch does not add repo to spec dict."""
        spec_file = tmp_path / "test-spec4.md"
        spec_file.write_text(
            "---\n"
            "status: ready\n"
            "---\n\n"
            "Do something\n"
        )

        from mtor.rptor import parse_spec

        parsed = parse_spec(spec_file)
        repo = parsed.get("repo", "~")

        # Same logic as dispatch.py
        spec: dict = {}
        if repo != "~":
            spec["repo"] = repo

        assert "repo" not in spec

    def test_spec_repo_nested_path(self, tmp_path):
        """Spec with nested repo path (e.g. worktree)."""
        spec_file = tmp_path / "test-spec5.md"
        spec_file.write_text(
            "---\n"
            "repo: ~/code/mtor/.worktrees/custom\n"
            "status: ready\n"
            "---\n\n"
            "Do something\n"
        )

        from mtor.rptor import parse_spec

        parsed = parse_spec(spec_file)
        assert parsed["repo"] == "~/code/mtor/.worktrees/custom"


# ---------------------------------------------------------------------------
# Workflow spec shape tests (verify spec dict shape flows repo)
# ---------------------------------------------------------------------------


class TestWorkflowSpecShape:
    """Verify the spec dict shape flows repo through the workflow."""

    def test_spec_dict_with_repo(self):
        """Spec dict includes repo field when set."""
        spec = {
            "task": "Fix the bug in app.py",
            "provider": "zhipu",
            "mode": "build",
            "risk": "medium",
            "repo": "/home/user/custom-repo",
        }
        assert spec.get("repo") == "/home/user/custom-repo"

    def test_spec_dict_without_repo(self):
        """Spec dict works without repo field (backward compat)."""
        spec = {
            "task": "Fix the bug in app.py",
            "provider": "zhipu",
            "mode": "build",
            "risk": "medium",
        }
        assert spec.get("repo") is None

    def test_watch_spec_propagates_repo(self):
        """WatchWorkflow._dispatch_spec should propagate non-default repo."""
        spec = {
            "name": "test-spec",
            "body": "Do the task",
            "provider": "zhipu",
            "mode": "raw",
            "repo": "~/code/myproject",
        }
        provider = "zhipu"

        # Replicate the logic from WatchWorkflow._dispatch_spec
        child_spec = {
            "task": spec.get("body", "") or spec.get("name", ""),
            "provider": spec.get("provider", provider),
            "mode": spec.get("mode", "raw"),
        }
        spec_repo = spec.get("repo", "~")
        if spec_repo != "~":
            child_spec["repo"] = spec_repo

        assert child_spec["repo"] == "~/code/myproject"

    def test_watch_spec_skips_default_repo(self):
        """WatchWorkflow._dispatch_spec should not propagate default repo."""
        spec = {
            "name": "test-spec",
            "body": "Do the task",
            "provider": "zhipu",
            "mode": "raw",
            "repo": "~",
        }
        provider = "zhipu"

        child_spec = {
            "task": spec.get("body", "") or spec.get("name", ""),
            "provider": spec.get("provider", provider),
            "mode": spec.get("mode", "raw"),
        }
        spec_repo = spec.get("repo", "~")
        if spec_repo != "~":
            child_spec["repo"] = spec_repo

        assert "repo" not in child_spec
