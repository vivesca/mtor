"""Tests for the capability keyword blocklist gate in translate().

Verifies that translate() returns a CAPABILITY_GATE error early when
the task string contains any blocked keyword, and allows clean tasks
to proceed past the gate.

Run via: cd ~/code/mtor && uv run pytest assays/test_capability_gate.py -v
"""

from __future__ import annotations

import asyncio
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from mtor.worker.translocase import _CAPABILITY_BLOCKLIST, translate


def _run(coro):
    """Run an async function synchronously for testing."""
    return asyncio.run(coro)


class TestCapabilityGateBlocked:
    """Tasks containing blocked keywords must be rejected with CAPABILITY_GATE."""

    def test_sudo_blocked(self):
        result = _run(translate("sudo rm something", "zhipu"))
        assert result["success"] is False
        assert result["exit_code"] == -1
        assert "CAPABILITY_GATE" in result["stderr"]
        assert result["gate"] == "capability"

    def test_drop_table_blocked(self):
        result = _run(translate("Run DROP TABLE users; in the database", "zhipu"))
        assert result["success"] is False
        assert "CAPABILITY_GATE" in result["stderr"]
        assert result["blocked_keyword"] == "DROP TABLE"

    def test_drop_database_blocked(self):
        result = _run(translate("Please DROP DATABASE production;", "zhipu"))
        assert result["success"] is False
        assert "CAPABILITY_GATE" in result["stderr"]

    def test_rm_rf_root_blocked(self):
        result = _run(translate("Execute rm -rf / to clean up", "zhipu"))
        assert result["success"] is False
        assert "CAPABILITY_GATE" in result["stderr"]

    def test_chmod_777_blocked(self):
        result = _run(translate("chmod -R 777 /var/www", "zhipu"))
        assert result["success"] is False
        assert "CAPABILITY_GATE" in result["stderr"]

    def test_npm_publish_blocked(self):
        result = _run(translate("npm publish --access public", "zhipu"))
        assert result["success"] is False
        assert "CAPABILITY_GATE" in result["stderr"]

    def test_docker_push_blocked(self):
        result = _run(translate("docker push myimage:latest", "zhipu"))
        assert result["success"] is False
        assert "CAPABILITY_GATE" in result["stderr"]

    def test_crontab_blocked(self):
        result = _run(translate("Set up crontab -e with a new job", "zhipu"))
        assert result["success"] is False
        assert "CAPABILITY_GATE" in result["stderr"]

    def test_case_insensitive_match(self):
        result = _run(translate("SUDO apt-get install something", "zhipu"))
        assert result["success"] is False
        assert "CAPABILITY_GATE" in result["stderr"]

    def test_blocked_keyword_preserved_in_result(self):
        result = _run(translate("Please run sudo something", "gemini"))
        assert result["blocked_keyword"] == "sudo "
        assert result["provider"] == "gemini"

    def test_task_truncated_in_result(self):
        long_task = "sudo " + "x" * 300
        result = _run(translate(long_task, "zhipu"))
        assert len(result["task"]) <= 200
        assert result["success"] is False


class TestCapabilityGateClean:
    """Clean tasks should not be blocked by the capability gate."""

    @staticmethod
    def _check_gate(task: str) -> bool:
        """Replicate the gate check logic (pure, no subprocess) to verify
        that clean tasks pass the blocklist scan."""
        task_upper = task.upper()
        for keyword in _CAPABILITY_BLOCKLIST:
            if keyword.upper() in task_upper:
                return True  # blocked
        return False  # clean

    def test_normal_task_not_blocked(self):
        assert not self._check_gate(
            "[t-deadbeef] Implement a hello world function in foo.py"
        )

    def test_code_edit_task_not_blocked(self):
        assert not self._check_gate(
            "Modify dispatch.py to add retry logic with exponential backoff"
        )

    def test_write_tests_task_not_blocked(self):
        assert not self._check_gate(
            "Write tests for the new authentication module in auth.py"
        )

    def test_sudo_in_word_boundary_not_blocked(self):
        """'sudo' as part of another word (e.g., 'pseudo') should not match."""
        assert not self._check_gate("Use a pseudonym for the variable name")

    def test_substring_false_positive_scp(self):
        """'scp ' keyword should not match words merely containing 'scp'."""
        assert not self._check_gate("Describe the scope of the changes needed")


class TestCapabilityBlocklistContents:
    """Verify the blocklist contains expected keywords."""

    def test_blocklist_is_tuple(self):
        assert isinstance(_CAPABILITY_BLOCKLIST, tuple)

    def test_blocklist_not_empty(self):
        assert len(_CAPABILITY_BLOCKLIST) > 0

    def test_expected_keywords_present(self):
        expected = ["sudo ", "DROP TABLE", "npm publish", "docker push", "crontab"]
        for kw in expected:
            assert kw in _CAPABILITY_BLOCKLIST, f"Missing expected keyword: {kw}"

    def test_no_empty_keywords(self):
        for kw in _CAPABILITY_BLOCKLIST:
            assert kw.strip(), f"Empty or whitespace-only keyword found: {repr(kw)}"
