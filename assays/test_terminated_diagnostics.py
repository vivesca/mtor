"""Assays for TERMINATED-state observability.

When a workflow is TERMINATED (worker self-killed, OOM, signal, etc.) the
ribosome typically leaves no `wf_result`, so the existing failure_reason path
returns "No diagnostic information available". These tests assert the new
`_terminated_diagnostics()` helper surfaces the ribosome stderr tail and a
detected kill-reason marker from the log file on disk.
"""
from __future__ import annotations

from pathlib import Path
from unittest.mock import patch


from mtor import cli


def _write_log(tmp_path: Path, workflow_id: str, content: str) -> Path:
    p = tmp_path / f"20260527-000000-{workflow_id}.txt"
    p.write_text(content, encoding="utf-8")
    return p


def test_terminated_with_wall_limit_marker_surfaces_kill_reason(tmp_path: Path):
    wf_id = "test-wf-wall-limit"
    log_content = (
        "ribosome: starting task\n"
        "ribosome: working...\n"
        "[wall-limit] aborting at 28m cap\n"
    )
    _write_log(tmp_path, wf_id, log_content)
    with patch.object(cli, "OUTPUTS_DIR", str(tmp_path)):
        diag = cli._terminated_diagnostics(wf_id)
    assert diag["kill_reason"] == "wall-limit", diag
    assert "wall-limit" in diag["stderr_tail"].lower()


def test_terminated_with_no_log_falls_back_gracefully(tmp_path: Path):
    with patch.object(cli, "OUTPUTS_DIR", str(tmp_path)):
        diag = cli._terminated_diagnostics("nonexistent-id-xyz")
    assert diag["kill_reason"] is None, diag
    assert diag["stderr_tail"] == ""
    assert diag["log_path"] == ""


def test_terminated_diagnosis_string_includes_kill_reason():
    payload = {"operator_state": "terminated", "kill_reason": "wall-limit"}
    out = cli._trace_diagnosis(payload)
    assert "terminated" in out.lower()
    assert "wall-limit" in out.lower()
