"""Assays for `mtor status --short` — terse one-line status output.

Replaces inline `mtor status <id> | python3 -c '...'` parse-and-print pattern.
"""
from __future__ import annotations

import json
import re
import sys
from io import StringIO
from types import SimpleNamespace
from unittest.mock import MagicMock, patch

import pytest


def _make_status_obj(name: str):
    return SimpleNamespace(name=name)


def _make_describe(status: str):
    return SimpleNamespace(
        status=_make_status_obj(status),
        start_time=SimpleNamespace(isoformat=lambda: "2026-05-06T10:00:00"),
        close_time=SimpleNamespace(isoformat=lambda: "2026-05-06T10:01:00"),
    )


def _capture(callable_, *args, **kwargs):
    buf = StringIO()
    exit_code = 0
    with patch.object(sys, "stdout", buf):
        try:
            callable_(*args, **kwargs)
        except SystemExit as exc:
            exit_code = exc.code if isinstance(exc.code, int) else 1
    return buf.getvalue(), exit_code


def _make_client(status_name: str, wf_result_dict: dict | None = None):
    handle = MagicMock()
    async def describe():
        return _make_describe(status_name)
    async def result():
        return wf_result_dict or {}
    handle.describe = describe
    handle.result = result
    client = MagicMock()
    client.get_workflow_handle.return_value = handle
    return client


def test_short_emits_one_line_completed():
    from mtor import cli
    client = _make_client("COMPLETED", {"results": [{"success": True, "exit_code": 0, "review": {"verdict": "approved"}}]})
    with patch.object(cli, "_get_client", return_value=(client, None)):
        out, _ = _capture(cli.status, "wf-x", short=True)
    lines = [l for l in out.split("\n") if l.strip()]
    assert len(lines) == 1


def test_short_format_pipe_separated():
    from mtor import cli
    client = _make_client("COMPLETED", {"results": [{"success": True, "exit_code": 0, "review": {"verdict": "approved"}}]})
    with patch.object(cli, "_get_client", return_value=(client, None)):
        out, _ = _capture(cli.status, "wf-x", short=True)
    line = out.strip().split("\n")[0]
    assert re.match(r"^[A-Z_]+ \| .+ \| .+ \| .*$", line), f"unexpected format: {line}"


def test_short_uses_em_dash_for_missing_verdict():
    from mtor import cli
    client = _make_client("RUNNING")
    with patch.object(cli, "_get_client", return_value=(client, None)):
        out, _ = _capture(cli.status, "wf-x", short=True)
    line = out.strip().split("\n")[0]
    assert "—" in line  # missing fields render as em-dash


def test_short_truncates_long_failure_reason():
    from mtor import cli
    long_reason = "X" * 200
    # COMPLETED + non-approved verdict → status path builds failure_reason via _build_failure_reason
    client = _make_client("COMPLETED", {"results": [{"success": False, "exit_code": 1, "review": {"verdict": "rejected"}}]})
    with patch.object(cli, "_get_client", return_value=(client, None)):
        with patch.object(cli, "_build_failure_reason", return_value=long_reason):
            out, _ = _capture(cli.status, "wf-x", short=True)
    line = out.strip().split("\n")[0]
    assert "..." in line, f"expected truncation marker in: {line}"
    reason_field = line.split(" | ")[-1]
    assert len(reason_field) <= 80


def test_default_envelope_unchanged_when_short_absent():
    from mtor import cli
    client = _make_client("COMPLETED", {"results": [{"success": True, "exit_code": 0, "review": {"verdict": "approved"}}]})
    with patch.object(cli, "_get_client", return_value=(client, None)):
        out, _ = _capture(cli.status, "wf-x")  # short defaults to False
    parsed = json.loads(out)
    assert parsed.get("ok") is True
    assert parsed.get("result", {}).get("workflow_id") == "wf-x"
    assert parsed.get("result", {}).get("status") == "COMPLETED"


def test_short_does_not_apply_to_error_path():
    """When workflow not found, error envelope is JSON regardless of --short flag."""
    from mtor import cli
    handle = MagicMock()
    async def describe():
        raise Exception("workflow_not_found: wf-x")
    handle.describe = describe
    client = MagicMock()
    client.get_workflow_handle.return_value = handle
    with patch.object(cli, "_get_client", return_value=(client, None)):
        out, exit_code = _capture(cli.status, "wf-x", short=True)
    assert exit_code == 4
    parsed = json.loads(out)
    assert parsed.get("ok") is False
    assert parsed.get("error", {}).get("code") == "WORKFLOW_NOT_FOUND"
