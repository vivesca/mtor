"""Assays for TERMINATED-state observability.

When a workflow is TERMINATED (worker self-killed, OOM, signal, etc.) the
ribosome typically leaves no `wf_result`, so the existing failure_reason path
returns "No diagnostic information available". These tests assert the new
`_terminated_diagnostics()` helper surfaces the ribosome stderr tail and a
detected kill-reason marker from the log file on disk.
"""
from __future__ import annotations

import json
from pathlib import Path
from types import SimpleNamespace
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


def test_reap_worker_processes_success_uses_targeted_ssh():
    payload = {
        "terminated_pids": [123],
        "killed_pids": [],
        "remaining_pids": [],
    }
    completed = SimpleNamespace(returncode=0, stdout=json.dumps(payload), stderr="")
    with patch("mtor.cli.subprocess.run", return_value=completed) as run:
        out = cli._reap_worker_processes("workflow-abc")

    assert out == {
        "attempted": True,
        "ok": True,
        "terminated_pids": [123],
        "killed_pids": [],
        "remaining_pids": [],
    }
    cmd = run.call_args.args[0]
    assert cmd[:4] == ["ssh", cli.WORKER_HOST, "python3", "-c"]
    assert cmd[-1] == "workflow-abc"
    assert "pkill" not in " ".join(cmd)


def test_reap_worker_processes_failure_returns_error():
    completed = SimpleNamespace(returncode=255, stdout="", stderr="connection refused")
    with patch("mtor.cli.subprocess.run", return_value=completed):
        out = cli._reap_worker_processes("workflow-abc")

    assert out["attempted"] is True
    assert out["ok"] is False
    assert out["terminated_pids"] == []
    assert "connection refused" in out["error"]


def test_running_trace_with_no_pending_activity_is_stale():
    payload = {
        "operator_state": "running",
        "pending_activities": [],
        "execution_state": {"execution_state": "queued"},
        "active_logs": [{"filename": "20260602-workflow-abc.txt"}],
    }

    out = cli._trace_diagnosis(payload)

    assert "stale" in out.lower()
    assert "no activity" in out.lower()

