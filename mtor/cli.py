"""mtor CLI — cyclopts app definition and command handlers.

Every response is a JSON envelope:
  ok:true   -> {"ok": true, "command": "...", "result": {...}, "next_actions": [...]}
  ok:false  -> {"ok": false, "command": "...", "error": {"message": "...", "code": "..."},
                "fix": "...", "next_actions": [...]}

Exit codes:
  0 - ok
  1 - error (generic, non-temporal)
  2 - usage error (missing required args)
  3 - temporal unreachable
  4 - workflow not found
"""

from __future__ import annotations

import asyncio
import contextlib
import subprocess
import sys
from datetime import UTC
from pathlib import Path
from typing import Annotated, Any, Literal

from cyclopts import App, Parameter
from porin import action as _action

from mtor import (
    DEPLOY_REMOTE,
    LOG_TAIL_LINES,
    OUTPUTS_DIR,
    REPO_DIR,
    TEMPORAL_HOST,
    VERSION,
    WORKER_HOST,
)
from mtor.client import _get_client
from mtor.dispatch import _dispatch_prompt
from mtor.doctor import doctor as _doctor
from mtor.envelope import _err, _extract_first_result, _ok
from mtor.scan import _run_checks
from mtor.tree import tree

# ---------------------------------------------------------------------------
# Cyclopts CLI
# ---------------------------------------------------------------------------

app = App(help_flags=[], version_flags=[])


@app.default
def default_handler(
    prompt: str | None = None,
    *,
    provider: Annotated[str, Parameter(name=["-p", "--provider"])] = "zhipu",
    experiment: Annotated[bool, Parameter(name=["-x", "--experiment"])] = False,
) -> None:
    """Bare invocation returns command tree; with a prompt, dispatches to Temporal."""
    if prompt is None:
        if sys.stdout.isatty():
            app.help_print()
        else:
            _ok("mtor", tree.to_dict(), version=VERSION)
        return
    else:
        _dispatch_prompt(prompt, provider=provider, experiment=experiment)


@app.command(name="list")
def list_cmd(
    *,
    status: Literal["RUNNING", "COMPLETED", "FAILED", "CANCELED", "TERMINATED"] | None = None,
    count: int = 50,
    since: Annotated[int | None, Parameter(name=["-s", "--since"])] = None,
) -> None:
    """List recent workflows. --since N shows last N hours only."""
    cmd = "mtor list" + (f" --status {status}" if status else "") + f" --count {count}"

    client, err = _get_client()
    if err:
        sys.exit(
            _err(
                cmd,
                f"Cannot connect to Temporal at {TEMPORAL_HOST}: {err}",
                "TEMPORAL_UNREACHABLE",
                f"Start Temporal worker: ssh {WORKER_HOST} 'sudo systemctl start temporal-worker'",
                [_action("mtor doctor", "Run health check to diagnose connectivity")],
                exit_code=3,
            )
        )

    try:
        # Build Temporal visibility query
        query_parts = []
        if status:
            status_map = {
                "RUNNING": "Running",
                "COMPLETED": "Completed",
                "FAILED": "Failed",
                "CANCELED": "Canceled",
                "TERMINATED": "Terminated",
            }
            query_parts.append(f"ExecutionStatus = '{status_map.get(status, status)}'")
        if since:
            from datetime import UTC, datetime, timedelta

            cutoff = datetime.now(UTC) - timedelta(hours=since)
            query_parts.append(f"StartTime > '{cutoff.strftime('%Y-%m-%dT%H:%M:%SZ')}'")
        query_filter = " AND ".join(query_parts) if query_parts else ""

        async def _list():
            results = []
            async for execution in client.list_workflows(
                query=query_filter if query_filter else None
            ):
                results.append(execution)
                if len(results) >= count:
                    break
            return results

        executions = asyncio.run(_list())
        workflows = []
        next_actions = []
        for ex in executions:
            wf_id = ex.id
            status_val = ex.status.name if ex.status else "UNKNOWN"
            start_time = ex.start_time.isoformat() if ex.start_time else None
            close_time = ex.close_time.isoformat() if ex.close_time else None
            verdict = "\u2014"
            with contextlib.suppress(Exception):
                sa = getattr(ex, "search_attributes", None)
                if sa:
                    for key, val in sa.items():
                        if "verdict" in str(key).lower() and val:
                            verdict = str(val[0])
            workflows.append(
                {
                    "workflow_id": wf_id,
                    "status": status_val,
                    "verdict": verdict,
                    "start_time": start_time,
                    "close_time": close_time,
                }
            )
            next_actions.append(_action(f"mtor status {wf_id}", f"Get full status for {wf_id}"))

        _ok(cmd, {"workflows": workflows, "count": len(workflows)}, next_actions, version=VERSION)
    except Exception as exc:
        sys.exit(
            _err(
                cmd,
                str(exc),
                "LIST_ERROR",
                "Check Temporal server health with: mtor doctor",
                [_action("mtor doctor", "Run health check")],
            )
        )


@app.command
def status(workflow_id: str) -> None:
    """Query status of a single workflow."""
    cmd = f"mtor status {workflow_id}"

    client, err = _get_client()
    if err:
        sys.exit(
            _err(
                cmd,
                f"Cannot connect to Temporal at {TEMPORAL_HOST}: {err}",
                "TEMPORAL_UNREACHABLE",
                f"Start Temporal worker: ssh {WORKER_HOST} 'sudo systemctl start temporal-worker'",
                [_action("mtor doctor", "Run health check to diagnose connectivity")],
                exit_code=3,
            )
        )

    try:

        async def _status():
            handle = client.get_workflow_handle(workflow_id)
            desc = await handle.describe()
            wf_result = None
            if desc.status and desc.status.name == "COMPLETED":
                with contextlib.suppress(Exception):
                    wf_result = await handle.result()
            return desc, wf_result

        desc, wf_result = asyncio.run(_status())
        status_val = desc.status.name if desc.status else "UNKNOWN"
        start_time = desc.start_time.isoformat() if desc.start_time else None
        close_time = desc.close_time.isoformat() if desc.close_time else None

        result_payload: dict[str, Any] = {
            "workflow_id": workflow_id,
            "status": status_val,
            "start_time": start_time,
            "close_time": close_time,
        }
        if wf_result and isinstance(wf_result, dict):
            task_result = _extract_first_result(wf_result)
            if task_result:
                result_payload["success"] = task_result.get("success")
                result_payload["exit_code"] = task_result.get("exit_code")
                result_payload["provider"] = task_result.get("provider")
                result_payload["task_preview"] = task_result.get("task", "")[:120]
                result_payload["output_path"] = task_result.get("review", {}).get(
                    "output_path", ""
                )
                result_payload["merged"] = task_result.get("merged")
                result_payload["verdict"] = task_result.get("review", {}).get("verdict")
                satisfaction = task_result.get("review", {}).get("satisfaction")
                if satisfaction is not None:
                    result_payload["satisfaction"] = satisfaction

        _ok(
            cmd,
            result_payload,
            [
                _action(f"mtor logs {workflow_id}", "Fetch last 30 lines of output"),
                _action(f"mtor cancel {workflow_id}", "Cancel this workflow"),
            ],
            version=VERSION,
        )
    except Exception as exc:
        exc_str = str(exc)
        if "not found" in exc_str.lower() or "workflow_not_found" in exc_str.lower():
            sys.exit(
                _err(
                    cmd,
                    f"Workflow {workflow_id} not found",
                    "WORKFLOW_NOT_FOUND",
                    "Verify the workflow ID with: mtor list",
                    [_action("mtor list", "List all recent workflows")],
                    exit_code=4,
                )
            )
        sys.exit(
            _err(
                cmd,
                exc_str,
                "STATUS_ERROR",
                "Check Temporal server health with: mtor doctor",
                [_action("mtor doctor", "Run health check")],
            )
        )


@app.command
def logs(workflow_id: str) -> None:
    """Fetch last 30 lines of workflow output from worker host."""
    cmd = f"mtor logs {workflow_id}"

    # Step 1: Query Temporal for the workflow result to get output_path
    log_path = ""
    client, client_err = _get_client()
    if client and not client_err:
        try:

            async def _get_output_path():
                handle = client.get_workflow_handle(workflow_id)
                wf_result = await handle.result()
                if isinstance(wf_result, dict):
                    task_result = _extract_first_result(wf_result)
                    if task_result:
                        return task_result.get("review", {}).get("output_path", "")
                return ""

            log_path = asyncio.run(_get_output_path())
        except Exception:
            pass

    # Step 2: If no output_path from result, fall back to ls + grep
    if not log_path:
        try:
            find_result = subprocess.run(
                ["ssh", WORKER_HOST, f"ls -t {OUTPUTS_DIR}/*.txt 2>/dev/null | head -20"],
                capture_output=True,
                text=True,
                timeout=15,
            )
            if find_result.returncode == 0:
                # Extract the hex suffix from workflow_id (e.g. ribosome-zhipu-af3c43d1 -> af3c43d1)
                wf_suffix = workflow_id.rsplit("-", 1)[-1] if "-" in workflow_id else workflow_id
                for line in find_result.stdout.strip().splitlines():
                    fname = line.strip().rsplit("/", 1)[-1]
                    if wf_suffix in fname:
                        log_path = line.strip()
                        break
        except (subprocess.TimeoutExpired, OSError):
            pass

    if not log_path:
        sys.exit(
            _err(
                cmd,
                f"No log file found for workflow {workflow_id}",
                "LOG_NOT_FOUND",
                f"Verify the workflow ID with: mtor status {workflow_id}",
                [_action(f"mtor status {workflow_id}", "Check if workflow exists")],
                exit_code=4,
            )
        )

    try:
        result = subprocess.run(
            ["ssh", WORKER_HOST, f"tail -{LOG_TAIL_LINES} {log_path}"],
            capture_output=True,
            text=True,
            timeout=30,
        )
        if result.returncode != 0:
            stderr_msg = result.stderr.strip()
            if "no such file" in stderr_msg.lower() or "not found" in stderr_msg.lower():
                sys.exit(
                    _err(
                        cmd,
                        f"Log file not found on worker host: {log_path}",
                        "LOG_NOT_FOUND",
                        f"Verify the workflow ID with: mtor status {workflow_id}",
                        [_action(f"mtor status {workflow_id}", "Check if workflow exists")],
                        exit_code=4,
                    )
                )
            sys.exit(
                _err(
                    cmd,
                    f"SSH command failed: {stderr_msg}",
                    "SSH_ERROR",
                    f"Verify worker host is reachable: ping {WORKER_HOST}",
                    [_action("mtor doctor", "Run health check")],
                )
            )

        lines = result.stdout.splitlines()
        _ok(
            cmd,
            {
                "lines": lines,
                "log_path": log_path,
                "truncated": len(lines) == LOG_TAIL_LINES,
            },
            [
                _action(f"mtor status {workflow_id}", "Check workflow status"),
                _action(f"mtor cancel {workflow_id}", "Cancel if still running"),
            ],
            version=VERSION,
        )
    except subprocess.TimeoutExpired:
        sys.exit(
            _err(
                cmd,
                f"SSH to {WORKER_HOST} timed out after 30s",
                "SSH_TIMEOUT",
                f"Check connectivity: ping {WORKER_HOST}",
                [_action("mtor doctor", "Run health check")],
            )
        )
    except FileNotFoundError:
        sys.exit(
            _err(
                cmd,
                "ssh binary not found",
                "SSH_NOT_FOUND",
                "Install openssh-client",
                [],
            )
        )


@app.command
def cancel(workflow_id: str) -> None:
    """Cancel a running workflow. Idempotent."""
    cmd = f"mtor cancel {workflow_id}"

    client, err = _get_client()
    if err:
        sys.exit(
            _err(
                cmd,
                f"Cannot connect to Temporal at {TEMPORAL_HOST}: {err}",
                "TEMPORAL_UNREACHABLE",
                f"Start Temporal worker: ssh {WORKER_HOST} 'sudo systemctl start temporal-worker'",
                [_action("mtor doctor", "Run health check to diagnose connectivity")],
                exit_code=3,
            )
        )

    try:

        async def _cancel():
            handle = client.get_workflow_handle(workflow_id)
            await handle.cancel()

        asyncio.run(_cancel())
        _ok(
            cmd,
            {"workflow_id": workflow_id, "cancelled": True},
            [
                _action(f"mtor status {workflow_id}", "Verify cancellation status"),
            ],
            version=VERSION,
        )
    except Exception as exc:
        exc_str = str(exc)
        # Idempotent: already cancelled/completed = ok
        if any(phrase in exc_str.lower() for phrase in ["not found", "workflow_not_found"]):
            sys.exit(
                _err(
                    cmd,
                    f"Workflow {workflow_id} not found",
                    "WORKFLOW_NOT_FOUND",
                    "Verify the workflow ID with: mtor list",
                    [_action("mtor list", "List all recent workflows")],
                    exit_code=4,
                )
            )
        # Already terminated/cancelled — treat as idempotent success
        if any(
            phrase in exc_str.lower()
            for phrase in ["already", "terminated", "cancelled", "canceled", "completed"]
        ):
            _ok(
                cmd,
                {
                    "workflow_id": workflow_id,
                    "cancelled": True,
                    "note": "Workflow was already in terminal state",
                },
                [
                    _action(f"mtor status {workflow_id}", "Verify final status"),
                ],
                version=VERSION,
            )
            return
        sys.exit(
            _err(
                cmd,
                exc_str,
                "CANCEL_ERROR",
                "Check Temporal server health with: mtor doctor",
                [_action("mtor doctor", "Run health check")],
            )
        )


@app.command
def doctor() -> None:
    """Health check: Temporal reachability, worker liveness, provider info."""
    _doctor()


@app.command
def history(
    *,
    count: int = 20,
) -> None:
    """Show recent ribosome run history from JSONL log."""
    import json as _json

    log_path = Path(REPO_DIR) / "loci" / "ribosome-runs.jsonl"
    if not log_path.exists():
        _ok("mtor history", {"runs": [], "count": 0}, version=VERSION)
        return
    lines = log_path.read_text().strip().splitlines()
    runs = []
    for line in reversed(lines[-count:]):
        with contextlib.suppress(Exception):
            runs.append(_json.loads(line))
    _ok("mtor history", {"runs": runs, "count": len(runs)}, version=VERSION)


@app.command
def scan() -> None:
    """Run deterministic checks: TODO/FIXME, missing tests, stale marks."""
    findings = _run_checks()
    next_actions = [
        _action("mtor scan", "Re-run scan after fixes"),
    ]
    _ok("mtor scan", {"findings": findings, "count": len(findings)}, next_actions, version=VERSION)


@app.command
def scout(
    prompt: str,
    *,
    provider: Annotated[str | None, Parameter(name=["-p", "--provider"])] = None,
) -> None:
    """Dispatch a read-only analysis task. Returns findings, not code."""
    _dispatch_prompt(prompt, provider=provider, mode="scout")


@app.command
def research(
    prompt: str,
    *,
    provider: Annotated[str | None, Parameter(name=["-p", "--provider"])] = None,
) -> None:
    """Dispatch an external research task. Searches web, synthesizes findings."""
    _dispatch_prompt(prompt, provider=provider, mode="research")


@app.command
def schema() -> None:
    """Emit full JSON schema of all commands."""
    _ok("mtor schema", tree.to_schema(), version=VERSION)


@app.command
def approve(workflow_id: str) -> None:
    """Approve a deferred (SRP-paused) ribosome task."""
    client, err = _get_client()
    if err:
        sys.exit(
            _err(
                "mtor approve",
                f"Cannot connect to Temporal at {TEMPORAL_HOST}: {err}",
                "TEMPORAL_UNREACHABLE",
                "Check Temporal connectivity",
                exit_code=3,
            )
        )

    async def _signal():
        handle = client.get_workflow_handle(workflow_id)
        await handle.signal("approve_task", workflow_id)

    asyncio.run(_signal())
    _ok("mtor approve", {"workflow_id": workflow_id, "decision": "approved"}, version=VERSION)


@app.command
def deny(workflow_id: str) -> None:
    """Deny a deferred (SRP-paused) ribosome task."""
    client, err = _get_client()
    if err:
        sys.exit(
            _err(
                "mtor deny",
                f"Cannot connect to Temporal at {TEMPORAL_HOST}: {err}",
                "TEMPORAL_UNREACHABLE",
                "Check Temporal connectivity",
                exit_code=3,
            )
        )

    async def _signal():
        handle = client.get_workflow_handle(workflow_id)
        await handle.signal("reject_task", workflow_id)

    asyncio.run(_signal())
    _ok("mtor deny", {"workflow_id": workflow_id, "decision": "denied"}, version=VERSION)


@app.command
def deploy() -> None:
    """Sync code to worker host, restart Temporal worker, verify health."""
    import time

    steps = []

    # Step 1: sync to worker — push to temp branch, then ff-merge on worker
    print("[deploy] syncing to worker...", file=sys.stderr)
    push = subprocess.run(
        ["git", "push", DEPLOY_REMOTE, "main:deploy-sync", "--force"],
        capture_output=True,
        text=True,
        timeout=30,
        cwd=REPO_DIR,
    )
    if push.returncode != 0:
        sys.exit(
            _err(
                "mtor deploy",
                f"push failed: {push.stderr.strip()[:200]}",
                "PUSH_FAILED",
                "Check ganglion connectivity: ssh ganglion hostname",
                exit_code=1,
            )
        )
    subprocess.run(
        [
            "ssh",
            WORKER_HOST,
            f"cd {REPO_DIR} && git merge deploy-sync --no-edit; git branch -d deploy-sync 2>/dev/null; true",
        ],
        capture_output=True,
        text=True,
        timeout=15,
    )
    steps.append({"step": "sync", "ok": True})

    # Step 2: restart worker
    print("[deploy] restarting temporal-worker...", file=sys.stderr)
    restart = subprocess.run(
        ["ssh", WORKER_HOST, "sudo systemctl restart temporal-worker"],
        capture_output=True,
        text=True,
        timeout=15,
    )
    steps.append({"step": "restart worker", "ok": restart.returncode == 0})
    if restart.returncode != 0:
        sys.exit(
            _err(
                "mtor deploy",
                f"Worker restart failed: {restart.stderr.strip()[:200]}",
                "RESTART_FAILED",
                f"SSH to {WORKER_HOST} and check: sudo systemctl status temporal-worker",
                exit_code=1,
            )
        )

    # Step 3: wait + verify
    time.sleep(3)
    print("[deploy] verifying health...", file=sys.stderr)
    _doctor()
    steps.append({"step": "health check", "ok": True})

    _ok("mtor deploy", {"steps": steps, "healthy": True}, version=VERSION)


@app.command
def stats() -> None:
    """Show dispatch statistics: today's verdicts, running count, weekly totals."""
    from datetime import datetime, timedelta

    client, err = _get_client()
    if err:
        sys.exit(
            _err(
                "mtor stats",
                f"Cannot connect: {err}",
                "TEMPORAL_UNREACHABLE",
                "mtor doctor",
                exit_code=3,
            )
        )

    today = datetime.now(UTC).strftime("%Y-%m-%dT00:00:00Z")
    week_ago = (datetime.now(UTC) - timedelta(days=7)).strftime("%Y-%m-%dT%H:%M:%SZ")

    async def _count(query: str) -> int:
        return await client.count_workflows(query=query)

    counts: dict[str, int] = {}
    queries = {
        "running": "ExecutionStatus = 'Running'",
        "today_total": f"StartTime > '{today}'",
        "today_completed": f"StartTime > '{today}' AND ExecutionStatus = 'Completed'",
        "week_total": f"StartTime > '{week_ago}'",
        "week_completed": f"StartTime > '{week_ago}' AND ExecutionStatus = 'Completed'",
    }

    for name, query in queries.items():
        try:
            counts[name] = asyncio.run(_count(query))
        except Exception:
            counts[name] = -1

    _ok("mtor stats", {"counts": counts}, version=VERSION)


@app.command
def checkpoints() -> None:
    """List saved checkpoints from failed ribosome runs."""
    import json as _json

    cp_dir = Path(OUTPUTS_DIR) / "checkpoints"
    if not cp_dir.exists():
        _ok("mtor checkpoints", {"checkpoints": [], "count": 0}, version=VERSION)
        return
    cps = []
    for f in sorted(cp_dir.glob("*.json"), reverse=True):
        with contextlib.suppress(Exception):
            cps.append(_json.loads(f.read_text()))
    _ok("mtor checkpoints", {"checkpoints": cps, "count": len(cps)}, version=VERSION)
