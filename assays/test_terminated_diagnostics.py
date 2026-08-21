"""Assays for TERMINATED-state observability.

When a workflow is TERMINATED (worker self-killed, OOM, signal, etc.) the
ribosome typically leaves no `wf_result`, so the existing failure_reason path
returns "No diagnostic information available". These tests assert the new
`_terminated_diagnostics()` helper surfaces the ribosome stderr tail and a
detected kill-reason marker from the log file on disk.
"""

from __future__ import annotations

import io
import json
import shlex
import sys
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch


from mtor import cli


def _terminated_client():
    """Mock Temporal client whose handle describes a TERMINATED workflow."""
    client = MagicMock()
    handle = MagicMock()
    desc = MagicMock()
    desc.status = MagicMock()
    desc.status.name = "TERMINATED"
    desc.start_time = MagicMock()
    desc.start_time.isoformat.return_value = "2026-05-27T00:00:00+00:00"
    desc.close_time = MagicMock()
    desc.close_time.isoformat.return_value = "2026-05-27T00:28:00+00:00"
    handle.describe = AsyncMock(return_value=desc)
    # TERMINATED never reaches handle.result(); guard against accidental use.
    handle.result = AsyncMock(side_effect=AssertionError("result() must not be called"))
    client.get_workflow_handle = MagicMock(return_value=handle)
    return client


def _run_status(workflow_id: str, *, short: bool = False) -> str:
    captured = io.StringIO()
    old_stdout = sys.stdout
    try:
        sys.stdout = captured
        try:
            cli.status(workflow_id, short=short)
        except SystemExit:
            pass
    finally:
        sys.stdout = old_stdout
    return captured.getvalue()


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
        "recorded_pids": [123],
        "matched_pids": [123, 124],
        "selected_pids": [123, 124],
        "selected_pgids": [123],
        "terminated_pgids": [123],
        "killed_pgids": [],
        "remaining_pids": [],
        "workflow_id_alive": False,
        "ok": True,
        "incomplete": False,
    }
    completed = SimpleNamespace(returncode=0, stdout=json.dumps(payload), stderr="")
    workflow_id = "workflow abc"
    with patch("mtor.cli.subprocess.run", return_value=completed) as run:
        out = cli._reap_worker_processes(workflow_id)

    assert out == {
        "attempted": True,
        "ok": True,
        "verified": True,
        "incomplete": False,
        "workflow_id_alive": False,
        "recorded_pids": [123],
        "matched_pids": [123, 124],
        "selected_pids": [123, 124],
        "selected_pgids": [123],
        "terminated_pgids": [123],
        "killed_pgids": [],
        "remaining_pids": [],
    }
    cmd = run.call_args.args[0]
    assert cmd[:2] == ["ssh", cli.WORKER_HOST]
    assert len(cmd) == 3
    remote_cmd = cmd[2]
    assert "python3 -c" in remote_cmd
    assert shlex.quote(workflow_id) in remote_cmd
    assert "pkill" not in " ".join(cmd)


def test_reap_worker_processes_failure_returns_error():
    completed = SimpleNamespace(returncode=255, stdout="", stderr="connection refused")
    with patch("mtor.cli.subprocess.run", return_value=completed):
        out = cli._reap_worker_processes("workflow-abc")

    assert out["attempted"] is True
    assert out["ok"] is False
    assert out["verified"] is False
    assert out["matched_pids"] == []
    assert "connection refused" in out["error"]


def test_reap_worker_processes_reports_ghosts_as_not_ok():
    """Survivors in the rescan mean the ghost is still alive — never report ok."""
    payload = {
        "recorded_pids": [123],
        "matched_pids": [123, 124],
        "selected_pids": [123, 124],
        "selected_pgids": [123],
        "terminated_pgids": [123],
        "killed_pgids": [123],
        "remaining_pids": [124],
        "workflow_id_alive": True,
        "ok": False,
        "incomplete": True,
    }
    completed = SimpleNamespace(returncode=0, stdout=json.dumps(payload), stderr="")
    with patch("mtor.cli.subprocess.run", return_value=completed):
        out = cli._reap_worker_processes("workflow-abc")

    assert out["ok"] is False
    assert out["verified"] is True
    assert out["incomplete"] is True
    assert out["workflow_id_alive"] is True
    assert out["remaining_pids"] == [124]


def test_reap_script_kills_process_groups_from_recorded_pids():
    """Remote script inspects pid/pgid/args, kills selected groups, and does
    not use provider-name hints or pkill -f."""
    assert "subprocess_started" in cli._REAP_SCRIPT
    assert "killpg" in cli._REAP_SCRIPT
    assert "pgid" in cli._REAP_SCRIPT
    assert "NAME_HINTS" not in cli._REAP_SCRIPT
    assert "pkill" not in cli._REAP_SCRIPT
    assert "claude" not in cli._REAP_SCRIPT.lower()


def test_reap_script_runs_and_reports_empty_for_unknown_workflow():
    """The embedded script is valid python and reports empty results for a
    workflow with no recorded pids and no matching processes."""
    import subprocess as _sp

    result = _sp.run(
        [sys.executable, "-c", cli._REAP_SCRIPT, "no-such-workflow-1f0e2d3c"],
        capture_output=True,
        text=True,
        timeout=30,
    )
    assert result.returncode == 0, result.stderr
    parsed = json.loads(result.stdout.strip())
    assert parsed["matched_pids"] == []
    assert parsed["selected_pids"] == []
    assert parsed["selected_pgids"] == []
    assert parsed["remaining_pids"] == []
    assert parsed["workflow_id_alive"] is False
    assert parsed["ok"] is True


JULY4_WF = "ribosome-glm52-problem-mtor-08dc82a3-6a48a7dc"
OTHER_WF = "ribosome-other-workflow-aaaaaaaa-bbbbbbbb"


def _july4_process_table(workflow_id=JULY4_WF, other_id=OTHER_WF):
    """Process table shaped like the 2026-07-04 cancel false-green."""
    return [
        (
            1001,
            100,
            f"bash /home/vivesca/germline/effectors/ribosome --provider zhipu {workflow_id}",
        ),
        (
            1002,
            100,
            f"/bin/bash /home/vivesca/germline/effectors/ribosome {workflow_id}",
        ),
        (
            2001,
            200,
            "timeout 7200 opencode run "
            f"--dir /home/vivesca/code/mtor/.worktrees/{workflow_id}",
        ),
        (
            2002,
            200,
            "opencode run "
            f"--dir /home/vivesca/code/mtor/.worktrees/{workflow_id}",
        ),
        (
            3001,
            300,
            "timeout 7200 opencode run "
            f"--dir /home/vivesca/code/mtor/.worktrees/{other_id}",
        ),
        (
            3002,
            300,
            f"opencode run --dir /home/vivesca/code/mtor/.worktrees/{other_id}",
        ),
        (9000, 900, f"sshd: python3 -c reap-script {workflow_id}"),
        (9001, 900, f"python3 -c reap-script {workflow_id}"),
    ]


def _assert_no_claude(rows):
    for _pid, _pgid, args in rows:
        assert "claude" not in args.lower(), args


def _fake_killpg(live_pgids=None):
    calls = []
    live = set(live_pgids or ())

    def killpg(pgid, sig):
        calls.append((pgid, sig))
        if sig == 0 and pgid not in live:
            raise ProcessLookupError

    killpg.calls = calls
    return killpg


def test_reap_selects_nested_opencode_groups_without_claude():
    """2026-07-04 shape: wrapper group + nested timeout/opencode group, no claude."""
    rows = _july4_process_table()
    _assert_no_claude(rows)
    killpg = _fake_killpg()
    result = cli._run_process_cleanup(
        JULY4_WF,
        scan=lambda: rows,
        own_pids={9000, 9001},
        killpg=killpg,
        sleeper=lambda _seconds: None,
    )
    assert set(result["selected_pgids"]) == {100, 200}
    assert set(result["selected_pids"]) == {1001, 1002, 2001, 2002}
    assert 9000 not in result["selected_pids"]
    assert 9001 not in result["selected_pids"]
    targeted = {pgid for pgid, _sig in killpg.calls}
    assert targeted == {100, 200}


def test_reap_incomplete_when_opencode_survives_terminate():
    """First terminate pass leaves a matching opencode process alive."""
    initial = _july4_process_table()
    _assert_no_claude(initial)
    leftover = [
        (
            2002,
            200,
            "opencode run "
            f"--dir /home/vivesca/code/mtor/.worktrees/{JULY4_WF}",
        )
    ]
    scans = {"n": 0}

    def scan():
        scans["n"] += 1
        if scans["n"] == 1:
            return initial
        return leftover

    result = cli._run_process_cleanup(
        JULY4_WF,
        scan=scan,
        own_pids={9000, 9001},
        killpg=_fake_killpg(live_pgids={200}),
        sleeper=lambda _seconds: None,
    )
    assert result["ok"] is False
    assert result["incomplete"] is True
    assert result["workflow_id_alive"] is True
    assert result["remaining_pids"] == [2002]


def test_reap_does_not_select_other_workflow_id():
    """A different workflow id in the same table is not selected."""
    rows = _july4_process_table()
    _assert_no_claude(rows)
    selected_pids, selected_pgids = cli._select_workflow_process_groups(
        rows, JULY4_WF, own_pids={9000, 9001}
    )
    assert 300 not in selected_pgids
    assert 3001 not in selected_pids
    assert 3002 not in selected_pids
    other_pids, other_pgids = cli._select_workflow_process_groups(
        rows, OTHER_WF, own_pids={9000, 9001}
    )
    assert other_pgids == [300]
    assert other_pids == [3001, 3002]


def test_status_terminated_surfaces_diagnostics_fallback(tmp_path: Path):
    wf_id = "ribosome-terminated-1"
    _write_log(
        tmp_path, wf_id, "ribosome: working...\n[wall-limit] aborting at 28m cap\n"
    )
    client = _terminated_client()
    with (
        patch.object(cli, "OUTPUTS_DIR", str(tmp_path)),
        patch.object(cli, "_get_client", return_value=(client, None)),
    ):
        out = _run_status(wf_id)
    data = json.loads(out)
    result = data["result"]
    assert result["operator_state"] == "terminated"
    assert result["kill_reason"] == "wall-limit"
    assert result["log_path"].endswith(".txt")
    assert "wall-limit" in result["terminated_diagnostics"]["stderr_tail"].lower()
    assert "wall-limit" in result["failure_reason"].lower()
    assert result["failure_reason"] != "No diagnostic information available"


def test_status_terminated_short_includes_kill_reason_without_tail(tmp_path: Path):
    wf_id = "ribosome-terminated-2"
    _write_log(
        tmp_path,
        wf_id,
        "ribosome: working...\n" + "noise line\n" * 50 + "[oom] killed\n",
    )
    client = _terminated_client()
    with (
        patch.object(cli, "OUTPUTS_DIR", str(tmp_path)),
        patch.object(cli, "_get_client", return_value=(client, None)),
    ):
        out = _run_status(wf_id, short=True)
    assert "oom" in out.lower()
    # One-line short output must not dump the multi-line tail.
    assert out.count("\n") <= 1
    assert "noise line" not in out


def test_status_terminated_no_log_keeps_default_reason(tmp_path: Path):
    client = _terminated_client()
    with (
        patch.object(cli, "OUTPUTS_DIR", str(tmp_path)),
        patch.object(cli, "_get_client", return_value=(client, None)),
    ):
        out = _run_status("ribosome-no-log-xyz")
    result = json.loads(out)["result"]
    assert result["failure_reason"] == "No diagnostic information available"
    assert "terminated_diagnostics" not in result
    assert "kill_reason" not in result


def test_running_trace_with_no_pending_activity_is_stale():
    payload = {
        "operator_state": "running",
        "pending_activities": [],
        "execution_state": {
            "execution_state": "executing",
            "source": "log-cache",
            "active_log_count": 1,
        },
        "active_logs": [{"filename": "20260602-workflow-abc.txt"}],
    }

    out = cli._trace_diagnosis(payload)

    assert "log-cache" in out.lower()
    assert "no pending-activity heartbeat" in out.lower()
