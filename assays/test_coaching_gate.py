"""Tests for ribosome coaching-size gates."""

from __future__ import annotations

import os
import subprocess
from pathlib import Path

import pytest

from mtor.worker.translocase import _is_coaching_bloat_error

integration = pytest.mark.skipif(
    not os.environ.get("MTOR_INTEGRATION"),
    reason="integration: reads ~/germline/effectors/ribosome (not in this repo). Set MTOR_INTEGRATION=1 to run.",
)

RIBOSOME_SCRIPT = Path.home() / "germline" / "effectors" / "ribosome"


@integration
def test_ribosome_script_has_clear_coaching_budget_gate():
    """The shell effector fails before dispatching when coaching is too large."""
    source = RIBOSOME_SCRIPT.read_text()

    assert "ERROR: coaching file is ${size_kb}KB (limit 10KB)." in source
    assert "before dispatching" in source
    assert "WARNING: coaching file is ${size_kb}KB - approaching 10KB limit" in source
    assert "bytes > 10 * 1024" in source
    assert "bytes > 8 * 1024" in source


@integration
def test_ribosome_script_syntax_stays_valid():
    """The live effector remains parseable after the guard is inserted."""
    result = subprocess.run(
        ["bash", "-n", str(RIBOSOME_SCRIPT)],
        capture_output=True,
        text=True,
        timeout=5,
    )

    assert result.returncode == 0, result.stderr


def test_translocase_classifies_coaching_bloat_as_non_retryable():
    """Translocase recognizes the effector's coaching gate as a terminal error."""
    stderr = (
        "ERROR: coaching file is 10.2KB (limit 10KB). "
        "Trim ~/epigenome/marks/feedback_ribosome_coaching.md before dispatching."
    )

    assert _is_coaching_bloat_error(1, stderr) is True
    assert _is_coaching_bloat_error(42, stderr) is False
    assert _is_coaching_bloat_error(1, "rate limit exceeded") is False
