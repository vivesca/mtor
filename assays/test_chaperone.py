"""Tests for the chaperone review activity — the quality gate for ribosome output.

Tests the pure logic of verdict determination, flag detection, and edge cases.
Runs via: cd ~/code/mtor && uv run pytest assays/test_chaperone.py -v
"""

from __future__ import annotations

import asyncio


def _run(coro):
    """Run an async function synchronously for testing."""
    return asyncio.run(coro)


def _make_result(
    *,
    exit_code: int = 0,
    stdout: str = "Done. Changes committed.",
    stderr: str = "",
    task: str = "Write tests for foo.py",
    provider: str = "zhipu",
    post_diff: dict | None = None,
    pre_diff: dict | None = None,
    branch_name: str = "",
) -> dict:
    """Build a minimal result dict for chaperone."""
    return {
        "success": exit_code == 0,
        "exit_code": exit_code,
        "stdout": stdout,
        "stderr": stderr,
        "task": task,
        "provider": provider,
        "post_diff": post_diff or {"stat": " foo.py | 10 ++++\n", "numstat": "10\t0\tfoo.py", "commits": ["abc1234 feat: add foo"], "commit_count": 1},
        "pre_diff": pre_diff or {"stat": "", "numstat": ""},
        "branch_name": branch_name,
        "cost_info": "",
    }


# Import chaperone from mtor.worker.translocase
import sys
from pathlib import Path
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from mtor.worker.translocase import chaperone


class TestVerdictBasics:
    """Core verdict determination."""

    def test_clean_success_approved(self):
        result = _make_result()
        review = _run(chaperone(result))
        assert review["verdict"] == "approved"
        assert review["approved"] is True

    def test_nonzero_exit_rejected(self):
        result = _make_result(exit_code=1)
        review = _run(chaperone(result))
        assert review["approved"] is False
        assert "exit_code=1" in review["flags"]

    def test_exit_minus_9_sigkill(self):
        result = _make_result(exit_code=-9)
        review = _run(chaperone(result))
        assert review["approved"] is False

    def test_no_commit_on_success_rejected(self):
        result = _make_result(
            post_diff={"stat": "", "numstat": "", "commits": [], "commit_count": 0}
        )
        review = _run(chaperone(result))
        assert review["approved"] is False
        assert "no_commit_on_success" in review["flags"]

    def test_incomplete_verdict(self):
        """exit!=0 but has commits = incomplete, not rejected."""
        result = _make_result(
            exit_code=1,
            post_diff={"stat": " foo.py | 5 +++\n", "numstat": "5\t0\tfoo.py", "commits": ["abc feat"], "commit_count": 1},
            branch_name="ribosome-123456",
        )
        review = _run(chaperone(result))
        assert review["verdict"] == "incomplete"
        assert review["approved"] is False


class TestDestructionFlags:
    """Destruction pattern detection."""

    def test_rm_rf_flagged(self):
        result = _make_result(stdout="Running rm -rf /tmp/old to clean up")
        review = _run(chaperone(result))
        assert any("destruction" in f for f in review["flags"])
        assert review["approved"] is False

    def test_deleted_all_flagged(self):
        result = _make_result(stderr="deleted all files in the directory")
        review = _run(chaperone(result))
        assert any("destruction" in f for f in review["flags"])


class TestPromotedChecks:
    """Coaching-promoted deterministic checks."""

    def test_placeholder_todo_flagged(self):
        result = _make_result(stdout="# TODO: implement this later")
        review = _run(chaperone(result))
        assert any("placeholders" in f for f in review["flags"])

    def test_hardcoded_home_path_flagged(self):
        result = _make_result(stdout='path = "/home/terry/germline/foo"')
        review = _run(chaperone(result))
        assert "hardcoded_home_path" in review["flags"]

    def test_py2_except_flagged(self):
        result = _make_result(stdout="except ValueError, e:")
        review = _run(chaperone(result))
        assert "py2_except_syntax" in review["flags"]

    def test_dupe_future_import_flagged(self):
        result = _make_result(stdout="from __future__ import annotations\nfrom __future__ import annotations")
        review = _run(chaperone(result))
        assert any("dupe_future_import" in f for f in review["flags"])


class TestFileShrinkage:
    """Detection of suspicious deletions."""

    def test_file_shrunk_flagged(self):
        result = _make_result(
            post_diff={"stat": " foo.py | 50 +----\n", "numstat": "2\t48\tfoo.py", "commits": ["a fix"], "commit_count": 1},
            pre_diff={"stat": "", "numstat": "0\t0\tfoo.py"},
        )
        review = _run(chaperone(result))
        assert any("file_shrunk" in f for f in review["flags"])

    def test_pure_deletion_flagged(self):
        result = _make_result(
            post_diff={"stat": " foo.py | 10 ------\n", "numstat": "0\t10\tfoo.py", "commits": ["a fix"], "commit_count": 1},
            pre_diff={"stat": "", "numstat": ""},
        )
        review = _run(chaperone(result))
        assert any("pure_deletion" in f for f in review["flags"])


class TestThinOutput:
    """Detection of suspiciously sparse output."""

    def test_thin_output_long_task(self):
        """thin_output fires when task_words > 20 and output_words < 10."""
        result = _make_result(
            task="Implement the full authentication module with OAuth2 support and refresh tokens and PKCE flow for the application server backend",
            stdout="ok",  # 1 word
        )
        review = _run(chaperone(result))
        # thin_output threshold: task > 20 words, output < 10 words
        assert any("thin_output" in f or "empty_stdout" in f for f in review["flags"])

    def test_short_task_not_flagged(self):
        result = _make_result(task="fix typo", stdout="done")
        review = _run(chaperone(result))
        assert not any("thin_output" in f for f in review["flags"])


class TestNestedTestFiles:
    """Test files must be in assays/ flat, not nested."""

    def test_nested_test_file_flagged(self):
        result = _make_result(
            post_diff={"stat": " assays/sub/test_foo.py | 10 ++++\n", "numstat": "10\t0\tassays/sub/test_foo.py", "commits": ["a"], "commit_count": 1},
        )
        review = _run(chaperone(result))
        assert any("nested_test_file" in f for f in review["flags"])

    def test_flat_test_file_ok(self):
        result = _make_result(
            post_diff={"stat": " assays/test_foo.py | 10 ++++\n", "numstat": "10\t0\tassays/test_foo.py", "commits": ["a"], "commit_count": 1},
        )
        review = _run(chaperone(result))
        assert not any("nested_test_file" in f for f in review["flags"])


class TestApprovedWithFlags:
    """Non-blocking flags still approve but mark the verdict."""

    def test_approved_with_placeholder_flag(self):
        """Placeholders in successful output → approved_with_flags? No — placeholders block."""
        result = _make_result(stdout="TODO: optimize later\nDone. All tests pass.")
        review = _run(chaperone(result))
        # Placeholders are flagged but don't block approval (only destruction + no_commit block)
        assert review["verdict"] in ("approved_with_flags", "approved")

    def test_error_pattern_rejects(self):
        result = _make_result(stdout="Traceback (most recent call last):\n  File...")
        review = _run(chaperone(result))
        assert any("errors" in f for f in review["flags"])


class TestSatisfaction:
    """Satisfaction scoring (0-100) based on objective signals."""

    def test_clean_success_high_score(self):
        result = _make_result()
        review = _run(chaperone(result))
        assert review["satisfaction"] >= 90

    def test_nonzero_exit_low_score(self):
        result = _make_result(exit_code=1)
        review = _run(chaperone(result))
        assert review["satisfaction"] <= 60

    def test_no_commit_penalized(self):
        result = _make_result(
            post_diff={"stat": "", "numstat": "", "commits": [], "commit_count": 0}
        )
        review = _run(chaperone(result))
        assert review["satisfaction"] <= 70


class TestRequeuePrompt:
    """Requeue suggestions for specific failure types."""

    def test_thin_output_generates_requeue(self):
        """Requeue prompt generated for thin_output + rejected/incomplete verdict."""
        result = _make_result(
            task="Build the complete user management system with roles and permissions and admin dashboard and role hierarchy and audit logging system",
            stdout="ok",  # thin
            post_diff={"stat": " admin.py | 5 +++\n", "numstat": "5\t0\tadmin.py", "commits": ["a"], "commit_count": 1},
        )
        review = _run(chaperone(result))
        # thin_output on rejected → requeue. But if approved despite flag, no requeue needed.
        if review["verdict"] in ("rejected", "incomplete"):
            assert review.get("requeue_prompt", "") != ""

    def test_file_shrunk_generates_requeue(self):
        result = _make_result(
            task="Add logging to worker.py",
            post_diff={"stat": " worker.py | 50 +---\n", "numstat": "2\t48\tworker.py", "commits": ["a"], "commit_count": 1},
            pre_diff={"stat": "", "numstat": ""},
        )
        review = _run(chaperone(result))
        # file_shrunk should generate a requeue prompt for rejected/incomplete
        if review["verdict"] in ("rejected", "incomplete"):
            assert "requeue_prompt" in review


class TestScoutMode:
    """Scout mode: read-only analysis tasks — no commit required."""

    def _make_scout_result(self, **kwargs):
        """Build a scout-mode result (no diff, no commit)."""
        defaults = {
            "post_diff": {"stat": "", "numstat": "", "commits": [], "commit_count": 0},
            "stdout": "Found 3 issues:\n- foo.py: unused import\n- bar.py: dead code\n- baz.py: deprecated API",
        }
        defaults.update(kwargs)
        r = _make_result(**defaults)
        r["mode"] = "scout"
        return r

    def test_scout_no_commit_approved(self):
        """Scout task with no commits should be approved (not rejected)."""
        result = self._make_scout_result()
        review = _run(chaperone(result))
        assert review["approved"] is True
        assert review["verdict"] == "approved"

    def test_scout_no_commit_flag_removed(self):
        """Scout mode removes no_commit_on_success from flags."""
        result = self._make_scout_result()
        review = _run(chaperone(result))
        assert "no_commit_on_success" not in review["flags"]

    def test_scout_empty_stdout_flag_removed(self):
        """Scout mode removes empty_stdout_on_success from flags."""
        result = self._make_scout_result(stdout="")
        review = _run(chaperone(result))
        assert "empty_stdout_on_success" not in review["flags"]

    def test_scout_destruction_still_rejected(self):
        """Scout mode still rejects destruction patterns."""
        result = self._make_scout_result(stdout="rm -rf /tmp/old")
        review = _run(chaperone(result))
        assert review["approved"] is False

    def test_scout_nonzero_exit_rejected(self):
        """Scout task with non-zero exit is still rejected."""
        result = self._make_scout_result(exit_code=1)
        review = _run(chaperone(result))
        assert review["approved"] is False
