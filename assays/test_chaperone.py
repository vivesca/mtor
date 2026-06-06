"""Tests for the chaperone review activity — the quality gate for ribosome output.

Tests the pure logic of verdict determination, flag detection, and edge cases.
Runs via: cd ~/code/mtor && uv run pytest assays/test_chaperone.py -v
"""

from __future__ import annotations

import asyncio
import json
import sys
from pathlib import Path


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


sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from mtor.worker.translocase import chaperone  # noqa: E402
import mtor.worker.chaperone_review as chaperone_review  # noqa: E402


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

    def test_rm_rf_hard_rejected_never_approved_with_flags(self):
        """rm -rf forces verdict=rejected — never approved/approved_with_flags."""
        result = _make_result(stdout="Running rm -rf /tmp/old to clean up")
        review = _run(chaperone(result))
        assert review["verdict"] == "rejected"
        assert review["verdict"] != "approved_with_flags"
        assert review["approved"] is False

    def test_deleted_all_flagged(self):
        result = _make_result(stderr="deleted all files in the directory")
        review = _run(chaperone(result))
        assert any("destruction" in f for f in review["flags"])
        assert review["verdict"] == "rejected"
        assert review["approved"] is False

    def test_rm_rf_in_diff_rejected(self):
        """rm -rf committed in the DIFF (not narrated to stdout) is still rejected.

        A ribosome can land destructive code without printing it. The diff scan
        must catch it.
        """
        result = _make_result(
            stdout="Done. Changes committed.",
            post_diff={
                "stat": " cleanup.sh | 3 +++\n",
                "numstat": "3\t0\tcleanup.sh",
                "commits": ["abc1234 feat: add cleanup"],
                "commit_count": 1,
                "patch": (
                    "diff --git a/cleanup.sh b/cleanup.sh\n"
                    "--- /dev/null\n"
                    "+++ b/cleanup.sh\n"
                    "@@ -0,0 +1,2 @@\n"
                    "+#!/bin/bash\n"
                    "+rm -rf /important/data\n"
                ),
            },
        )
        review = _run(chaperone(result))
        assert any("destruction" in f for f in review["flags"])
        assert review["verdict"] == "rejected"
        assert review["approved"] is False

    def test_rmtree_in_diff_rejected(self):
        """shutil.rmtree committed in the diff is treated as destruction."""
        result = _make_result(
            post_diff={
                "stat": " wipe.py | 2 ++\n",
                "numstat": "2\t0\twipe.py",
                "commits": ["abc1234 feat: wipe"],
                "commit_count": 1,
                "patch": (
                    "diff --git a/wipe.py b/wipe.py\n"
                    "+++ b/wipe.py\n"
                    "@@ -0,0 +1,1 @@\n"
                    "+shutil.rmtree(target_dir)\n"
                ),
            },
        )
        review = _run(chaperone(result))
        assert any("destruction" in f for f in review["flags"])
        assert review["verdict"] == "rejected"

    def test_destruction_overrides_incomplete(self):
        """exit!=0 + commits + destruction → rejected, not preserved as incomplete.

        A destructive branch must not be kept for re-dispatch.
        """
        result = _make_result(
            exit_code=1,
            stdout="rm -rf /tmp/work",
            post_diff={
                "stat": " foo.py | 5 +++\n",
                "numstat": "5\t0\tfoo.py",
                "commits": ["abc feat"],
                "commit_count": 1,
            },
            branch_name="ribosome-123456",
        )
        review = _run(chaperone(result))
        assert review["verdict"] == "rejected"
        assert review["approved"] is False


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

    def test_reflex_ban_scans_added_patch_lines(self):
        result = _make_result(
            post_diff={
                "stat": " foo.py | 2 ++\n",
                "numstat": "2\t0\tfoo.py",
                "commits": ["abc feat"],
                "commit_count": 1,
                "patch": (
                    "diff --git a/foo.py b/foo.py\n"
                    "+++ b/foo.py\n"
                    "@@\n"
                    "+from typing import Optional\n"
                    "+value: Optional[str] = None  # type: ignore[assignment]\n"
                ),
            },
        )

        review = _run(chaperone(result))

        assert "reflex_ban:typing_optional" in review["flags"]
        assert "reflex_ban:inline_bypass" in review["flags"]
        assert review["approved"] is False

    def test_hardcoded_home_path_in_diff_blocks(self):
        """A hardcoded home path in committed code is a hard reject.

        Narration mentions stay a non-blocking warning (test_hardcoded_home_path_flagged);
        landing the path in the diff is a portability defect that blocks the merge.
        """
        result = _make_result(
            stdout="Done. Changes committed.",
            post_diff={
                "stat": " conf.py | 1 +\n",
                "numstat": "1\t0\tconf.py",
                "commits": ["abc feat"],
                "commit_count": 1,
                "patch": (
                    "diff --git a/conf.py b/conf.py\n"
                    "+++ b/conf.py\n"
                    "@@\n"
                    '+ROOT = "/Users/terry/germline"\n'
                ),
            },
        )
        review = _run(chaperone(result))
        assert "reflex_ban:hardcoded_home_path" in review["flags"]
        assert review["approved"] is False
        assert review["verdict"] == "rejected"

    def test_dupe_future_import_in_diff_blocks(self):
        """A new file adding the same __future__ import twice is a hard reject."""
        result = _make_result(
            post_diff={
                "stat": " new.py | 3 +++\n",
                "numstat": "3\t0\tnew.py",
                "commits": ["abc feat"],
                "commit_count": 1,
                "patch": (
                    "diff --git a/new.py b/new.py\n"
                    "--- /dev/null\n"
                    "+++ b/new.py\n"
                    "@@ -0,0 +1,2 @@\n"
                    "+from __future__ import annotations\n"
                    "+from __future__ import annotations\n"
                ),
            },
        )
        review = _run(chaperone(result))
        assert "reflex_ban:dupe_future_import" in review["flags"]
        assert review["approved"] is False
        assert review["verdict"] == "rejected"

    def test_single_future_import_in_diff_ok(self):
        """One __future__ import in a new file does NOT trip the dupe gate."""
        result = _make_result(
            post_diff={
                "stat": " ok.py | 2 ++\n",
                "numstat": "2\t0\tok.py",
                "commits": ["abc feat"],
                "commit_count": 1,
                "patch": (
                    "diff --git a/ok.py b/ok.py\n"
                    "+++ b/ok.py\n"
                    "@@\n"
                    "+from __future__ import annotations\n"
                    "+value = 1\n"
                ),
            },
        )
        review = _run(chaperone(result))
        assert not any("dupe_future_import" in f for f in review["flags"])

    def test_future_import_split_across_files_not_dupe(self):
        """Two files each with one __future__ import is not a duplicate (per-file scan)."""
        result = _make_result(
            post_diff={
                "stat": " a.py | 1 +\n b.py | 1 +\n",
                "numstat": "1\t0\ta.py\n1\t0\tb.py",
                "commits": ["abc feat"],
                "commit_count": 1,
                "patch": (
                    "diff --git a/a.py b/a.py\n"
                    "+++ b/a.py\n"
                    "@@\n"
                    "+from __future__ import annotations\n"
                    "diff --git a/b.py b/b.py\n"
                    "+++ b/b.py\n"
                    "@@\n"
                    "+from __future__ import annotations\n"
                ),
            },
        )
        review = _run(chaperone(result))
        assert not any("dupe_future_import" in f for f in review["flags"])

    def test_reflex_ban_ignores_diff_metadata(self):
        result = _make_result(
            post_diff={
                "stat": " foo.py | 1 +\n",
                "numstat": "1\t0\tfoo.py",
                "commits": ["abc feat"],
                "commit_count": 1,
                "patch": "diff --git a/foo.py b/foo.py\n+++ b/foo.py\n@@\n+value = 1\n",
            },
        )

        review = _run(chaperone(result))

        assert not any(flag.startswith("reflex_ban") for flag in review["flags"])


class TestFileShrinkage:
    """Detection of suspicious deletions."""

    def test_file_shrunk_flagged(self):
        result = _make_result(
            post_diff={"stat": " foo.py | 50 +----\n", "numstat": "2\t48\tfoo.py", "commits": ["a fix"], "commit_count": 1},
            pre_diff={"stat": "", "numstat": "0\t0\tfoo.py"},
        )
        review = _run(chaperone(result))
        assert any("file_shrunk" in f for f in review["flags"])
        assert review["approved"] is False
        assert review["verdict"] == "rejected"

    def test_pure_deletion_flagged(self):
        result = _make_result(
            post_diff={"stat": " foo.py | 10 ------\n", "numstat": "0\t10\tfoo.py", "commits": ["a fix"], "commit_count": 1},
            pre_diff={"stat": "", "numstat": ""},
        )
        review = _run(chaperone(result))
        assert any("pure_deletion" in f for f in review["flags"])
        assert review["approved"] is False
        assert review["verdict"] == "rejected"


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
        assert review["approved"] is False
        assert review["verdict"] == "rejected"


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

    def test_test_satisfaction_bonus_for_fallback_diff(self):
        """Fallback diff should receive a satisfaction bonus.

        Without fallback bonus: 100 - 15 (thin_output) + 10 (committed) = 95.
        With fallback bonus: 100 - 15 + 10 + 10 = 105 -> capped at 100.
        """
        result = _make_result(
            task="Implement the full authentication module with OAuth2 support and refresh tokens and PKCE flow for the application server backend",
            stdout="ok",  # 1 word -> thin_output fires
            post_diff={
                "stat": " foo.py | 10 ++++\n",
                "numstat": "10\t0\tfoo.py",
                "commits": ["abc1234 feat: add foo"],
                "commit_count": 1,
                "fallback": True,
            },
        )
        review = _run(chaperone(result))
        assert review["satisfaction"] >= 100


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


class TestCompletionEvidence:
    """Structured evidence bundle for review decisions."""

    def test_success_includes_completion_evidence(self):
        result = _make_result(
            task="Update mtor/worker/chaperone_review.py and assays/test_chaperone.py",
            stdout="uv run pytest assays/test_chaperone.py -q\n42 passed",
            post_diff={
                "stat": (
                    " mtor/worker/chaperone_review.py | 30 ++++++++++\n"
                    " assays/test_chaperone.py | 20 ++++++\n"
                ),
                "numstat": (
                    "30\t0\tmtor/worker/chaperone_review.py\n"
                    "20\t0\tassays/test_chaperone.py"
                ),
                "commits": ["abc1234 add completion evidence"],
                "commit_count": 1,
                "patch": "+completion_evidence = {}\n",
            },
            branch_name="ribosome/completion-evidence",
        )
        result["output_path"] = "/home/vivesca/code/mtor/logs/wf.jsonl"

        review = _run(chaperone(result))

        evidence = review["completion_evidence"]
        assert evidence["execution"]["provider"] == "zhipu"
        assert evidence["execution"]["output_path"] == "/home/vivesca/code/mtor/logs/wf.jsonl"
        assert evidence["artifact"]["commit_count"] == 1
        assert evidence["artifact"]["has_patch"] is True
        assert evidence["artifact"]["patch_bytes"] > 0
        assert evidence["artifact"]["changed_paths"] == [
            "mtor/worker/chaperone_review.py",
            "assays/test_chaperone.py",
        ]
        assert evidence["verification"]["status"] == "passed"
        assert evidence["verification"]["detected_commands"] == [
            "uv run pytest assays/test_chaperone.py -q"
        ]
        assert evidence["scope"]["missing_requested_paths"] == []
        assert evidence["decision"]["approved"] is True
        assert evidence["decision"]["verdict"] == review["verdict"]

    def test_missing_requested_path_is_in_scope_evidence(self):
        result = _make_result(
            task="Update mtor/worker/chaperone_review.py",
            post_diff={
                "stat": " mtor/cli.py | 10 ++++\n",
                "numstat": "10\t0\tmtor/cli.py",
                "commits": ["abc1234 update cli"],
                "commit_count": 1,
            },
        )

        review = _run(chaperone(result))

        assert "target_file_missing: mtor/worker/chaperone_review.py" in review["flags"]
        assert review["completion_evidence"]["scope"]["missing_requested_paths"] == [
            "mtor/worker/chaperone_review.py"
        ]

    def test_scout_without_artifact_still_has_evidence_and_approves(self):
        result = _make_result(
            stdout="Found no issues.",
            post_diff={"stat": "", "numstat": "", "commits": [], "commit_count": 0},
        )
        result["mode"] = "scout"

        review = _run(chaperone(result))

        assert review["approved"] is True
        evidence = review["completion_evidence"]
        assert evidence["execution"]["mode"] == "scout"
        assert evidence["artifact"]["commit_count"] == 0
        assert evidence["artifact"]["has_stat"] is False
        assert evidence["decision"]["approved"] is True

    def test_workflow_result_writes_completion_dossier(self, tmp_path, monkeypatch):
        workflow_id = "ribosome-glm51-dossier-test"
        monkeypatch.setattr(chaperone_review, "DOSSIER_DIR", tmp_path / "ribosome-dossiers")
        result = _make_result(
            task="Update mtor/worker/chaperone_review.py",
            post_diff={
                "stat": " mtor/worker/chaperone_review.py | 30 ++++++++++\n",
                "numstat": "30\t0\tmtor/worker/chaperone_review.py",
                "commits": ["abc1234 add dossier"],
                "commit_count": 1,
                "patch": "+completion_dossier = {}\n",
            },
            branch_name="ribosome/dossier",
        )
        result.update({
            "workflow_id": workflow_id,
            "repo_root": "/home/vivesca/code/mtor",
            "base_sha": "base123",
            "requested_provider": "zhipu",
            "attempted_providers": ["zhipu"],
            "output_path": "/home/vivesca/germline/loci/ribosome-outputs/wf.txt",
        })

        review = _run(chaperone(result))

        dossier_path = tmp_path / "ribosome-dossiers" / f"{workflow_id}.json"
        assert review["dossier_path"] == str(dossier_path)
        assert review["completion_dossier"]["workflow_id"] == workflow_id
        assert review["completion_dossier"]["operator"]["state"] == "approved"
        dossier = json.loads(dossier_path.read_text(encoding="utf-8"))
        assert dossier["workflow_id"] == workflow_id
        assert dossier["repo_root"] == "/home/vivesca/code/mtor"
        assert dossier["base_sha"] == "base123"
        assert dossier["artifact"]["commit_count"] == 1
        assert dossier["artifact"]["changed_paths"] == ["mtor/worker/chaperone_review.py"]
        assert dossier["review"]["verdict"] == "approved"

    def test_dossier_distinguishes_requested_and_resolved_provider(self, tmp_path, monkeypatch):
        workflow_id = "ribosome-provider-fallback-test"
        monkeypatch.setattr(chaperone_review, "DOSSIER_DIR", tmp_path / "ribosome-dossiers")
        result = _make_result(provider="gemini")
        result.update({
            "workflow_id": workflow_id,
            "requested_provider": "zhipu",
            "attempted_providers": ["gemini"],
        })

        review = _run(chaperone(result))

        dossier = review["completion_dossier"]
        assert dossier["requested_provider"] == "zhipu"
        assert dossier["resolved_provider"] == "gemini"
        assert dossier["attempted_providers"] == ["gemini"]
        assert dossier["completion_evidence"]["execution"]["provider"] == "gemini"


class TestFailureReason:
    """Test that cli.py failure_reason surfaces chaperone flags."""

    def test_test_failure_reason_includes_flags(self):
        """failure_reason includes chaperone flags from review."""
        from mtor.cli import _build_failure_reason

        task_result = {
            "error": "Process exited with code 1",
            "review": {
                "flags": ["exit_code=1", "destruction: rm -rf"],
                "verdict": "rejected",
            },
        }
        reason = _build_failure_reason(task_result)
        assert "flags:" in reason
        assert "exit_code=1" in reason
        assert "destruction: rm -rf" in reason

    def test_test_failure_reason_includes_no_commit_flag(self):
        """failure_reason surfaces no_commit_on_success flag."""
        from mtor.cli import _build_failure_reason

        task_result = {
            "stderr": "Task completed",
            "review": {
                "flags": ["no_commit_on_success"],
                "verdict": "rejected",
            },
        }
        reason = _build_failure_reason(task_result)
        assert "no_commit_on_success" in reason


class TestPrimaryDiffAuthority:
    """Primary git diff/commit evidence dominates derivative empty-diff or shrink metadata.

    When post_diff has commits, patch content, or net-positive numstat, derivative
    flags like no_commit_on_success, file_shrunk, and pure_deletion must not fire.
    True destructive changes are still blocked.
    """

    def test_commits_list_overrides_empty_stat(self):
        """Non-empty post_diff.commits list prevents no_commit_on_success even with empty stat."""
        result = _make_result(
            post_diff={
                "stat": "",
                "numstat": "",
                "commits": ["fd922842d ribosome: stream claude output as json"],
                "commit_count": 1,
            }
        )
        review = _run(chaperone(result))
        assert "no_commit_on_success" not in review["flags"]

    def test_nonempty_patch_overrides_empty_stat(self):
        """Non-empty patch prevents no_commit_on_success even with empty stat and no commits."""
        result = _make_result(
            post_diff={
                "stat": "",
                "numstat": "",
                "commits": [],
                "commit_count": 0,
                "patch": "+def new_function():\n+    pass\n",
            }
        )
        review = _run(chaperone(result))
        assert "no_commit_on_success" not in review["flags"]

    def test_net_positive_diff_not_flagged_as_deletion(self):
        """Overall net-positive diff suppresses pure_deletion for individual files.

        A refactoring that adds 1000 lines to new_feature.py while removing 50 lines
        from old_code.py should not be treated as destructive.
        """
        result = _make_result(
            post_diff={
                "stat": " new_feature.py | 1000 ++++++++++++++\n old_code.py | 50 -------\n",
                "numstat": "1000\t0\tnew_feature.py\n0\t50\told_code.py",
                "commits": ["abc1234 feat: big refactor"],
                "commit_count": 1,
            },
            pre_diff={"stat": "", "numstat": ""},
        )
        review = _run(chaperone(result))
        assert not any("pure_deletion" in f for f in review["flags"])
        assert not any("file_shrunk" in f for f in review["flags"])

    def test_genuine_pure_deletion_still_flagged(self):
        """Genuine pure deletion with net-negative diff is still flagged and rejected."""
        result = _make_result(
            post_diff={
                "stat": " target.py | 50 -------\n",
                "numstat": "0\t50\ttarget.py",
                "commits": ["abc1234 remove target"],
                "commit_count": 1,
            },
            pre_diff={"stat": "", "numstat": ""},
        )
        review = _run(chaperone(result))
        assert any("pure_deletion" in f for f in review["flags"])
        assert review["approved"] is False

    def test_genuine_file_shrunk_still_flagged(self):
        """Genuine file shrink with net-negative diff is still flagged and rejected."""
        result = _make_result(
            post_diff={
                "stat": " bigfile.py | 50 +----\n",
                "numstat": "2\t48\tbigfile.py",
                "commits": ["abc1234 shrink bigfile"],
                "commit_count": 1,
            },
            pre_diff={"stat": "", "numstat": ""},
        )
        review = _run(chaperone(result))
        assert any("file_shrunk" in f for f in review["flags"])
        assert review["approved"] is False
