"""Unit tests for mtor/dedup.py — dispatch deduplication logic."""

from __future__ import annotations

import time
from pathlib import Path
from unittest.mock import patch

from mtor.dedup import check_and_record, check_duplicate, compute_identity, record_dispatch


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


def test_check_duplicate_does_not_write_state(tmp_path: Path) -> None:
    """check_duplicate returns None for a fresh prompt without creating the state file."""
    state_path = tmp_path / "dedup.json"

    result = check_duplicate("fix the bug", state_path=state_path, window=300)

    assert result is None
    assert not state_path.exists()


def test_check_duplicate_detects_existing(tmp_path: Path) -> None:
    """check_duplicate returns key when a matching entry was previously recorded."""
    state_path = tmp_path / "dedup.json"
    prompt = "fix the bug"

    record_dispatch(prompt, state_path=state_path)
    result = check_duplicate(prompt, state_path=state_path, window=300)

    assert result is not None
    assert result == compute_identity(prompt)


def test_record_dispatch_writes_state(tmp_path: Path) -> None:
    """record_dispatch creates the state file with the identity timestamp."""
    state_path = tmp_path / "dedup.json"
    prompt = "fix the bug"

    record_dispatch(prompt, state_path=state_path)

    assert state_path.exists()
    import json
    state = json.loads(state_path.read_text())
    key = compute_identity(prompt)
    assert key in state


def test_check_and_record_still_backward_compat(tmp_path: Path) -> None:
    """check_and_record behaves the same: blocks duplicates, allows fresh prompts."""
    state_path = tmp_path / "dedup.json"
    prompt = "fix the bug"

    result1 = check_and_record(prompt, state_path=state_path, window=300)
    assert result1 is None

    result2 = check_and_record(prompt, state_path=state_path, window=300)
    assert result2 is not None
