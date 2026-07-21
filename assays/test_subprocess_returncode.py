"""Regression coverage for subprocess completion accounting."""

from mtor.worker.translocase import _resolved_returncode


def test_unreaped_subprocess_is_not_success():
    assert _resolved_returncode(None) == -1


def test_numeric_subprocess_returncode_is_preserved():
    assert _resolved_returncode(0) == 0
    assert _resolved_returncode(42) == 42
