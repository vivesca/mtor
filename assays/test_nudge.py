"""Tests for mtor nudge — send reactivation signal to dormant workflows.

Runs via: cd ~/code/mtor && uv run pytest assays/test_nudge.py -x -v
"""

from __future__ import annotations

import io
import json
import sys
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from mtor.cli import app


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def invoke(args: list[str] | None = None) -> tuple[int, dict]:
    """Invoke CLI and return (exit_code, parsed_json)."""
    captured = io.StringIO()
    old_stdout = sys.stdout
    exit_code = 0
    try:
        sys.stdout = captured
        app(args or [])
    except SystemExit as exc:
        exit_code = exc.code if isinstance(exc.code, int) else 1
    finally:
        sys.stdout = old_stdout

    output = captured.getvalue()
    try:
        data = json.loads(output)
    except json.JSONDecodeError as exc:
        raise AssertionError(
            f"Output is not valid JSON. Exit={exit_code}\nOutput: {output!r}\nException: {exc}"
        ) from exc
    return exit_code, data


def make_mock_client():
    """Build a minimal async mock Temporal client."""
    client = MagicMock()
    handle = MagicMock()
    handle.signal = AsyncMock()
    client.get_workflow_handle = MagicMock(return_value=handle)
    return client, handle


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------


def test_test_nudge_sends_signal():
    """nudge <workflow_id> sends 'nudge' signal and returns ok envelope."""
    mock_client, mock_handle = make_mock_client()

    with patch("mtor.cli._get_client", return_value=(mock_client, None)):
        exit_code, data = invoke(["nudge", "ribosome-test1234"])

    assert exit_code == 0, f"Expected exit 0, got {exit_code}: {data}"
    assert data["ok"] is True
    assert data["result"]["workflow_id"] == "ribosome-test1234"
    assert data["result"]["nudged"] is True

    # Verify signal was sent with correct name
    mock_handle.signal.assert_awaited_once_with("nudge")


def test_test_nudge_nonexistent_workflow():
    """nudge <workflow_id> returns error when workflow not found."""
    mock_client, mock_handle = make_mock_client()
    mock_handle.signal = AsyncMock(side_effect=Exception("workflow not found: nope"))

    with patch("mtor.cli._get_client", return_value=(mock_client, None)):
        exit_code, data = invoke(["nudge", "nonexistent-wf-9999"])

    assert exit_code == 4, f"Expected exit 4 (WORKFLOW_NOT_FOUND), got {exit_code}: {data}"
    assert data["ok"] is False
    assert data["error"]["code"] == "WORKFLOW_NOT_FOUND"
