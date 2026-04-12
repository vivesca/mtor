"""Unit tests for mtor/dedup.py — dispatch deduplication logic."""

from __future__ import annotations

import time
from pathlib import Path
from unittest.mock import patch

from mtor.dedup import check_and_record, compute_identity


def test_same_prompt_within_window_blocked(tmp_path: Path) -> None:
    """Same prompt dispatched twice within the window is blocked."""
    state_path = tmp_path / "dedup.json"
    prompt = "fix the bug"

    result1 = check_and_record(prompt, state_path=state_path, window=300)
    assert result1 is None  # first call allowed

    result2 = check_and_record(prompt, state_path=state_path, window=300)
    assert result2 is not None  # second call blocked
    assert result2 == compute_identity(prompt)


def test_same_prompt_after_window_allowed(tmp_path: Path) -> None:
    """Same prompt dispatched after the window expires is allowed."""
    state_path = tmp_path / "dedup.json"
    prompt = "fix the bug"
    window = 300

    # Record once so state file exists
    check_and_record(prompt, state_path=state_path, window=window)

    # Advance time beyond the window
    with patch("mtor.dedup.time.time", return_value=time.time() + window + 1):
        result = check_and_record(prompt, state_path=state_path, window=window)

    assert result is None  # allowed again


def test_different_prompt_allowed(tmp_path: Path) -> None:
    """A different prompt is allowed even within the same window."""
    state_path = tmp_path / "dedup.json"
    window = 300

    check_and_record("fix the bug", state_path=state_path, window=window)
    result = check_and_record("add a feature", state_path=state_path, window=window)

    assert result is None


def test_different_spec_path_allowed(tmp_path: Path) -> None:
    """Same prompt but different spec_path is allowed within the window."""
    state_path = tmp_path / "dedup.json"
    window = 300
    prompt = "fix the bug"

    check_and_record(prompt, spec_path=Path("spec_a.md"), state_path=state_path, window=window)
    result = check_and_record(prompt, spec_path=Path("spec_b.md"), state_path=state_path, window=window)

    assert result is None
