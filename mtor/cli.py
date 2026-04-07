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
import time
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
from mtor.dedup import check_and_record as _check_dedup
from mtor.dispatch import _dispatch_prompt
from mtor.doctor import doctor as _doctor
from mtor.envelope import _err, _extract_first_result, _ok
from mtor.plan import CycleDetected, display_dag, resolve_dag, scan_specs, topological_sort
from mtor.scan import _run_checks
from mtor.triage import TRIAGE_PATH, archive_ids, load_triage, parse_duration, review_ids
from mtor.tree import tree
from mtor.spec import scaffold_spec, update_spec_status
from mtor.infra import check_health as _check_health, clean as _clean, deploy as _deploy
from mtor.watch import (
    freeze as _create_freeze,
    is_frozen as _is_frozen,
    is_paused as _is_paused,
    pause as _create_pause,
    resume as _remove_pause,
    run_watch,
    thaw as _remove_freeze,
)


# ---------------------------------------------------------------------------
# Wait/poll helpers for scout/research --wait
# ---------------------------------------------------------------------------


def _fetch_log_text(workflow_id: str, client=None) -> str:
    """Fetch workflow log text via SSH. Returns empty string on failure."""
    log_path = ""
    if client:
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

    if not log_path:
        try:
            find_result = subprocess.run(
                ["ssh", WORKER_HOST, f"ls -t {OUTPUTS_DIR}/*.txt 2>/dev/null | head -20"],
                capture_output=True,
                text=True,
                timeout=15,
            )
            if find_result.returncode == 0:
                wf_suffix = workflow_id.rsplit("-", 1)[-1] if "-" in workflow_id else workflow_id
                for line in find_result.stdout.strip().splitlines():
                    fname = line.strip().rsplit("/", 1)[-1]
                    if wf_suffix in fname:
                        log_path = line.strip()
                        break
        except (subprocess.TimeoutExpired, OSError):
            pass

    if not log_path:
        return ""

    try:
        result = subprocess.run(
            ["ssh", WORKER_HOST, f"tail -{LOG_TAIL_LINES} {log_path}"],
            capture_output=True,
            text=True,
            timeout=30,
        )
        if result.returncode == 0:
            return result.stdout
    except (subprocess.TimeoutExpired, OSError):
        pass

    return ""


def _build_failure_reason(task_result: dict) -> str:
    """Build a human-readable failure reason from task result + chaperone flags."""
    parts: list[str] = []
    err_msg = task_result.get("error") or task_result.get("stderr", "")
    if err_msg:
        parts.append(str(err_msg).splitlines()[-1])
    review = task_result.get("review", {})
    flags = review.get("flags", [])
    if flags:
        parts.append(f"flags: {', '.join(flags[:5])}")
    return "; ".join(parts) if parts else "No diagnostic information available"


def _wait_and_print_logs(workflow_id: str, *, timeout: int = 300) -> int:
    """Poll workflow until done, then print logs. Returns exit code."""
    client, err = _get_client()
    if err:
        return 1

    start_time = time.time()
    while True:
        elapsed = int(time.time() - start_time)
        if elapsed >= timeout:
            print(
                f"\n[scout] timed out after {timeout}s — workflow {workflow_id} still running",
                file=sys.stderr,
            )
            print(f"[scout] follow up manually: mtor logs {workflow_id}", file=sys.stderr)
            return 124

        try:

            async def _poll():
                handle = client.get_workflow_handle(workflow_id)
                desc = await handle.describe()
                return desc

            desc = asyncio.run(_poll())
            status_name = desc.status.name if desc.status else "UNKNOWN"
        except Exception:
            status_name = "UNKNOWN"

        if status_name != "RUNNING":
            break

        print(f"\r[scout] waiting... ({elapsed}s)", file=sys.stderr, end="", flush=True)
        time.sleep(10)

    # Workflow finished — fetch logs via SSH
    log_output = _fetch_log_text(workflow_id, client)
    if log_output:
        print(log_output)

    if status_name == "COMPLETED":
        return 0
    return 1

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
    skip_sha_check: Annotated[bool, Parameter(name=["--skip-sha-check"])] = False,
    then: Annotated[list[str] | None, Parameter(name=["--then"])] = None,
    spec: Annotated[Path | None, Parameter(name=["--spec"])] = None,
) -> None:
    """Bare invocation returns command tree; with a prompt, dispatches to Temporal.

    --then: follow-up prompts dispatched after this task completes with approved verdict.
    """
    if prompt is None:
        if sys.stdout.isatty():
            app.help_print()
        else:
            _ok("mtor", tree.to_dict(), version=VERSION)
        return
    else:
        # Freeze check — block dispatch when frozen (deptor lock)
        if _is_frozen():
            cmd = f"mtor {prompt[:60]}{'...' if len(prompt) > 60 else ''}"
            sys.exit(
                _err(
                    cmd,
                    "Dispatching is frozen. Use 'mtor thaw' to unfreeze.",
                    "FROZEN",
                    "Run: mtor thaw",
                    [_action("mtor thaw", "Unfreeze dispatching")],
                    exit_code=1,
                )
            )
        # Pause check — block dispatch when paused
        if _is_paused():
            cmd = f"mtor {prompt[:60]}{'...' if len(prompt) > 60 else ''}"
            sys.exit(
                _err(
                    cmd,
                    "Dispatching is paused. Use 'mtor resume' to resume.",
                    "PAUSED",
                    "Run: mtor resume",
                    [_action("mtor resume", "Resume dispatching")],
                    exit_code=1,
                )
            )
        # Dedup check — block identical dispatches within 5-minute window
        # Skip for empty prompts (will be caught by MISSING_PROMPT in _dispatch_prompt)
        dup_key = _check_dedup(prompt, spec_path=spec) if prompt.strip() else None
        if dup_key is not None:
            cmd = f"mtor {prompt[:60]}{'...' if len(prompt) > 60 else ''}"
            sys.exit(
                _err(
                    cmd,
                    f"Duplicate dispatch blocked (key={dup_key}). Same prompt dispatched within the last 5 minutes.",
                    "DEDUP_BLOCKED",
                    "Wait a few minutes or change the prompt/spec to dispatch again.",
                    [_action("mtor list", "View running workflows")],
                    exit_code=1,
                )
            )
        _dispatch_prompt(
            prompt,
            provider=provider,
            experiment=experiment,
            skip_sha_check=skip_sha_check,
            chain=then,
            spec_path=spec,
        )


@app.command(name="list")
def list_cmd(
    *,
    status: Literal["RUNNING", "COMPLETED", "FAILED", "CANCELED", "TERMINATED"] | None = None,
    count: int = 50,
    since: Annotated[int | None, Parameter(name=["-s", "--since"])] = None,
    pending: Annotated[bool, Parameter(name=["--pending"])] = False,
    all_: Annotated[bool, Parameter(name=["--all"])] = False,
    provider_filter: Annotated[str | None, Parameter(name=["--provider"])] = None,
    verdict_filter: Annotated[str | None, Parameter(name=["--verdict"])] = None,
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

        # Load triage state
        triage = load_triage()
        reviewed_set = set(triage.get("reviewed", []))
        archived_set = set(triage.get("archived", []))

        workflows = []
        next_actions = []
        archived_hidden = 0
        reviewed_count = 0
        pending_count = 0

        for ex in executions:
            wf_id = ex.id
            status_val = ex.status.name if ex.status else "UNKNOWN"
            start_time = ex.start_time.isoformat() if ex.start_time else None
            close_time = ex.close_time.isoformat() if ex.close_time else None
            sa_verdict = "\u2014"
            sa_provider = ""
            with contextlib.suppress(Exception):
                sa = getattr(ex, "search_attributes", None)
                if sa:
                    for key, val in sa.items():
                        if "verdict" in str(key).lower() and val:
                            sa_verdict = str(val[0])
                        if "provider" in str(key).lower() and val:
                            sa_provider = str(val[0])

            # Filter by --provider / --verdict search attributes
            if provider_filter and sa_provider != provider_filter:
                continue
            if verdict_filter and sa_verdict != verdict_filter:
                continue

            is_reviewed = wf_id in reviewed_set
            is_archived = wf_id in archived_set

            if is_reviewed:
                sa_verdict = f"[R] {sa_verdict}"
                reviewed_count += 1

            # --pending: only unreviewed completed workflows
            if pending:
                if is_reviewed or is_archived:
                    continue
                if status_val != "COMPLETED":
                    continue
                pending_count += 1
            elif not all_:
                # Default: hide archived
                if is_archived:
                    archived_hidden += 1
                    continue

            workflows.append(
                {
                    "workflow_id": wf_id,
                    "status": status_val,
                    "verdict": sa_verdict,
                    "provider": sa_provider,
                    "start_time": start_time,
                    "close_time": close_time,
                }
            )
            next_actions.append(_action(f"mtor status {wf_id}", f"Get full status for {wf_id}"))

        # Count pending (unreviewed completed) for envelope
        if not pending:
            for ex in executions:
                wf_id = ex.id
                status_val = ex.status.name if ex.status else "UNKNOWN"
                if (status_val == "COMPLETED"
                        and wf_id not in reviewed_set
                        and wf_id not in archived_set):
                    pending_count += 1

        result: dict[str, Any] = {
            "workflows": workflows,
            "count": len(workflows),
            "archived_hidden": archived_hidden,
            "reviewed_count": reviewed_count,
            "pending_count": pending_count,
        }
        _ok(cmd, result, next_actions, version=VERSION)
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

        # Add failure_reason for non-approved terminal states
        if status_val in ("FAILED", "CANCELED", "TERMINATED") or (
            status_val == "COMPLETED" and result_payload.get("verdict") not in ("approved", "approved_with_flags", None)
        ):
            failure_reason = "No diagnostic information available"
            if wf_result and isinstance(wf_result, dict):
                task_result = _extract_first_result(wf_result)
                if task_result:
                    failure_reason = _build_failure_reason(task_result)
            result_payload["failure_reason"] = failure_reason

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

    # Step 2: If no output_path from result, fall back to local glob then SSH ls
    if not log_path:
        wf_suffix = workflow_id.rsplit("-", 1)[-1] if "-" in workflow_id else workflow_id
        # Try local directory first
        outputs_path = Path(OUTPUTS_DIR)
        if outputs_path.exists():
            for txt_file in sorted(outputs_path.glob("*.txt"), key=lambda p: p.stat().st_mtime, reverse=True)[:20]:
                if wf_suffix in txt_file.name:
                    log_path = str(txt_file)
                    break
        # Fall back to SSH
        if not log_path:
            try:
                find_result = subprocess.run(
                    ["ssh", WORKER_HOST, f"ls -t {OUTPUTS_DIR}/*.txt 2>/dev/null | head -20"],
                    capture_output=True,
                    text=True,
                    timeout=15,
                )
                if find_result.returncode == 0:
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

    # Try local file first (avoids SSH round-trip); fetch + cache if missing
    local_path = Path(log_path)
    if not local_path.is_absolute():
        local_path = Path.home() / log_path.lstrip("~/")
    if not local_path.exists():
        # Fetch single file from worker host into local mirror
        local_path.parent.mkdir(parents=True, exist_ok=True)
        with contextlib.suppress(Exception):
            subprocess.run(
                ["scp", f"{WORKER_HOST}:{log_path}", str(local_path)],
                capture_output=True,
                timeout=15,
            )
    if local_path.exists():
        lines = local_path.read_text().splitlines()[-LOG_TAIL_LINES:]
        _ok(
            cmd,
            {
                "lines": lines,
                "log_path": str(local_path),
                "truncated": len(lines) == LOG_TAIL_LINES,
            },
            [
                _action(f"mtor status {workflow_id}", "Check workflow status"),
                _action(f"mtor cancel {workflow_id}", "Cancel if still running"),
            ],
            version=VERSION,
        )
        return

    # Fall back to SSH
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
def terminate(workflow_id: str) -> None:
    """Immediately terminate a running workflow."""
    _terminate_workflow(workflow_id, "mtor terminate")


@app.command
def cancel(workflow_id: str) -> None:
    """Cancel a running workflow. Delegates to terminate for immediate stop."""
    _terminate_workflow(workflow_id, "mtor cancel")


def _terminate_workflow(workflow_id: str, cmd: str) -> None:
    """Shared terminate logic for both cancel and terminate commands."""
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

        async def _do_terminate():
            handle = client.get_workflow_handle(workflow_id)
            await handle.terminate(reason="Terminated via mtor CLI")

        asyncio.run(_do_terminate())
        _ok(
            cmd,
            {"workflow_id": workflow_id, "terminated": True},
            [
                _action(f"mtor status {workflow_id}", "Verify termination status"),
            ],
            version=VERSION,
        )
    except Exception as exc:
        exc_str = str(exc)
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
        # Already terminated/cancelled — idempotent success
        if any(
            phrase in exc_str.lower()
            for phrase in ["already", "terminated", "cancelled", "canceled", "completed"]
        ):
            _ok(
                cmd,
                {
                    "workflow_id": workflow_id,
                    "terminated": True,
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
                "TERMINATE_ERROR",
                "Check Temporal server health with: mtor doctor",
                [_action("mtor doctor", "Run health check")],
            )
        )


@app.command
def doctor() -> None:
    """Health check: Temporal reachability, worker liveness, provider info."""
    _doctor()


@app.command
def probe() -> None:
    """Show provider circuit-breaker state and recommended selection."""
    from mtor.worker.provider import (
        PROVIDER_PRIORITY,
        load_health,
        select_provider,
    )

    cmd = "mtor probe"
    health = load_health()
    recommended = select_provider(health)

    providers = []
    for name in PROVIDER_PRIORITY:
        entry = health.get(name, {})
        providers.append({
            "name": name,
            "state": entry.get("state", "closed"),
            "cooldown_until": entry.get("cooldown_until"),
            "consecutive_failures": entry.get("consecutive_failures", 0),
        })

    _ok(
        cmd,
        {
            "providers": providers,
            "recommended": recommended,
        },
        [_action("mtor doctor", "Full health check")],
        version=VERSION,
    )


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
    skip_sha_check: Annotated[bool, Parameter(name=["--skip-sha-check"])] = False,
    wait: Annotated[bool, Parameter(negative="--no-wait")] = True,
    timeout: Annotated[int, Parameter(name=["--timeout"])] = 300,
) -> None:
    """Dispatch a read-only analysis task. Returns findings, not code."""
    workflow_id = _dispatch_prompt(
        prompt, provider=provider, mode="scout",
        skip_sha_check=skip_sha_check, wait=wait, timeout=timeout,
    )
    if wait and workflow_id:
        sys.exit(_wait_and_print_logs(workflow_id, timeout=timeout))


@app.command
def research(
    prompt: str,
    *,
    provider: Annotated[str | None, Parameter(name=["-p", "--provider"])] = None,
    skip_sha_check: Annotated[bool, Parameter(name=["--skip-sha-check"])] = False,
    wait: Annotated[bool, Parameter(negative="--no-wait")] = True,
    timeout: Annotated[int, Parameter(name=["--timeout"])] = 600,
) -> None:
    """Dispatch an external research task. Searches web, synthesizes findings."""
    workflow_id = _dispatch_prompt(
        prompt, provider=provider, mode="research",
        skip_sha_check=skip_sha_check, wait=wait, timeout=timeout,
    )
    if wait and workflow_id:
        sys.exit(_wait_and_print_logs(workflow_id, timeout=timeout))


@app.command
def auto(
    *,
    provider: Annotated[str, Parameter(name=["-p", "--provider"])] = "zhipu",
    skip_sha_check: Annotated[bool, Parameter(name=["--skip-sha-check"])] = False,
) -> None:
    """Self-improvement: scan mtor codebase for issues, dispatch a fix task."""
    findings = _run_checks()
    if not findings:
        _ok("mtor auto", {"action": "none", "reason": "No issues found"}, version=VERSION)
        return

    # Build a prompt from the top findings
    finding_lines = "\n".join(f"- {f['file']}:{f.get('line','')} {f['issue']}" for f in findings[:5])
    auto_prompt = (
        f"Fix the following issues in ~/code/mtor:\n{finding_lines}\n\n"
        "Make assays/test_auto_fixes.py pass if you add new tests."
    )
    _dispatch_prompt(
        auto_prompt,
        provider=provider,
        mode="build",
        skip_sha_check=skip_sha_check,
    )


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
def nudge(workflow_id: str) -> None:
    """Send reactivation signal to a dormant workflow."""
    cmd = f"mtor nudge {workflow_id}"

    client, err = _get_client()
    if err:
        sys.exit(
            _err(
                cmd,
                f"Cannot connect to Temporal at {TEMPORAL_HOST}: {err}",
                "TEMPORAL_UNREACHABLE",
                "Check Temporal connectivity",
                exit_code=3,
            )
        )

    try:

        async def _signal():
            handle = client.get_workflow_handle(workflow_id)
            await handle.signal("nudge")

        asyncio.run(_signal())
        _ok(
            cmd,
            {"workflow_id": workflow_id, "nudged": True},
            [_action(f"mtor status {workflow_id}", "Check workflow status")],
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
                "NUDGE_ERROR",
                "Check Temporal server health with: mtor doctor",
                [_action("mtor doctor", "Run health check")],
            )
        )


@app.command
def publish(
    *,
    bump: Annotated[Literal["patch", "minor", "major"], Parameter(name=["-b", "--bump"])] = "patch",
) -> None:
    """Bump version, build, publish to PyPI, upgrade soma + ganglion."""
    import re as _re

    init_path = Path(__file__).parent / "__init__.py"
    init_text = init_path.read_text()
    match = _re.search(r'VERSION = "(\d+)\.(\d+)\.(\d+)"', init_text)
    if not match:
        sys.exit(_err("mtor publish", "Cannot parse VERSION", "VERSION_PARSE_ERROR", "Check mtor/__init__.py"))

    major, minor, patch_v = int(match.group(1)), int(match.group(2)), int(match.group(3))
    if bump == "major":
        major += 1; minor = 0; patch_v = 0
    elif bump == "minor":
        minor += 1; patch_v = 0
    else:
        patch_v += 1
    new_version = f"{major}.{minor}.{patch_v}"

    # Step 1: bump version
    init_path.write_text(init_text.replace(match.group(0), f'VERSION = "{new_version}"'))
    print(f"[publish] bumped to {new_version}", file=sys.stderr)

    # Step 2: commit + push
    subprocess.run(["git", "add", "-A"], cwd=str(init_path.parent.parent), check=True)
    subprocess.run(
        ["git", "commit", "-m", f"chore: bump to v{new_version}"],
        cwd=str(init_path.parent.parent), check=True,
    )
    subprocess.run(["git", "push"], cwd=str(init_path.parent.parent), check=True)
    print("[publish] committed + pushed", file=sys.stderr)

    # Step 3: build + publish
    subprocess.run(["uv", "build"], cwd=str(init_path.parent.parent), check=True)

    token_result = subprocess.run(
        ["op", "item", "get", "pypi-token", "--vault", "Agents", "--fields", "credential", "--reveal"],
        capture_output=True, text=True, timeout=10,
    )
    if token_result.returncode != 0:
        sys.exit(_err("mtor publish", "Cannot get PyPI token from 1Password", "PYPI_TOKEN_ERROR", "op signin"))

    subprocess.run(
        ["uv", "publish", "--token", token_result.stdout.strip()],
        cwd=str(init_path.parent.parent), check=True,
    )
    print(f"[publish] published {new_version} to PyPI", file=sys.stderr)

    # Step 4: upgrade both machines
    subprocess.run(["uv", "tool", "install", "mtor", "--upgrade"], check=True)
    print("[publish] upgraded soma", file=sys.stderr)

    subprocess.run(
        ["ssh", WORKER_HOST,
         "export PATH=$HOME/.local/bin:$HOME/.cargo/bin:$PATH && uv tool install mtor --upgrade"],
        timeout=30,
    )
    print("[publish] upgraded ganglion", file=sys.stderr)

    _ok("mtor publish", {"version": new_version, "published": True}, version=new_version)


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


@app.command
def review(
    workflow_id: str | None = None,
    *,
    all_: Annotated[bool, Parameter(name=["--all"])] = False,
) -> None:
    """Mark task(s) as reviewed — seen, verdict noted."""
    if all_:
        # review --all: mark all completed non-running tasks
        client, err = _get_client()
        if err:
            sys.exit(
                _err(
                    "mtor review --all",
                    f"Cannot connect: {err}",
                    "TEMPORAL_UNREACHABLE",
                    "mtor doctor",
                    exit_code=3,
                )
            )

        async def _list_completed():
            results = []
            async for execution in client.list_workflows(query=None):
                results.append(execution)
            return results

        executions = asyncio.run(_list_completed())
        ids_to_review = [
            ex.id for ex in executions
            if ex.status and ex.status.name not in ("RUNNING",)
        ]
        result = review_ids(ids_to_review)
        _ok(
            "mtor review --all",
            result,
            [_action("mtor list", "View updated list")],
            version=VERSION,
        )
        return

    if workflow_id is None:
        sys.exit(
            _err(
                "mtor review",
                "Missing workflow_id or --all",
                "MISSING_ARGS",
                "Provide a workflow ID or use --all",
            )
        )

    result = review_ids([workflow_id])
    _ok(
        f"mtor review {workflow_id}",
        result,
        [_action("mtor list", "View updated list")],
        version=VERSION,
    )


@app.command
def archive(
    workflow_id: Annotated[str | None, Parameter(name=["workflow_id"])] = None,
    *,
    before: Annotated[str | None, Parameter(name=["--before"])] = None,
    all_reviewed: Annotated[bool, Parameter(name=["--all-reviewed"])] = False,
) -> None:
    """Archive task(s) — hide from default list."""
    if all_reviewed:
        triage = load_triage()
        ids_to_archive = list(triage.get("reviewed", []))
        result = archive_ids(ids_to_archive)
        _ok(
            "mtor archive --all-reviewed",
            result,
            [_action("mtor list", "View updated list")],
            version=VERSION,
        )
        return

    if before:
        client, err = _get_client()
        if err:
            sys.exit(
                _err(
                    "mtor archive --before",
                    f"Cannot connect: {err}",
                    "TEMPORAL_UNREACHABLE",
                    "mtor doctor",
                    exit_code=3,
                )
            )

        delta = parse_duration(before)
        from datetime import UTC, datetime

        cutoff = datetime.now(UTC) - delta

        async def _list_all():
            results = []
            async for execution in client.list_workflows(query=None):
                results.append(execution)
            return results

        executions = asyncio.run(_list_all())
        ids_to_archive = []
        for ex in executions:
            if ex.status and ex.status.name == "COMPLETED" and ex.close_time:
                close_time = ex.close_time
                # Handle both aware and naive datetimes
                if close_time.tzinfo is None:
                    close_time = close_time.replace(tzinfo=UTC)
                if close_time < cutoff:
                    ids_to_archive.append(ex.id)

        result = archive_ids(ids_to_archive)
        _ok(
            f"mtor archive --before {before}",
            result,
            [_action("mtor list", "View updated list")],
            version=VERSION,
        )
        return

    if workflow_id is None:
        sys.exit(
            _err(
                "mtor archive",
                "Missing workflow_id or filter flag",
                "MISSING_ARGS",
                "Provide a workflow ID, --before <duration>, or --all-reviewed",
            )
        )

    result = archive_ids([workflow_id])
    _ok(
        f"mtor archive {workflow_id}",
        result,
        [_action("mtor list", "View updated list")],
        version=VERSION,
    )


@app.command
def init(
    name: str,
    *,
    repo: Annotated[str | None, Parameter(name=["--repo"])] = None,
    scope: Annotated[str | None, Parameter(name=["--scope"])] = None,
    exclude: Annotated[str | None, Parameter(name=["--exclude"])] = None,
    dir: Annotated[Path, Parameter(name=["--dir"])] = Path("."),
) -> None:
    """Scaffold a new spec file with YAML frontmatter."""
    import subprocess

    cmd = f"mtor init {name}"

    # Default repo: git rev-parse --show-toplevel, fall back to ~
    if repo is None:
        try:
            result = subprocess.run(
                ["git", "rev-parse", "--show-toplevel"],
                capture_output=True,
                text=True,
                timeout=10,
                cwd=dir.resolve() if dir.exists() else None,
            )
            repo = result.stdout.strip() if result.returncode == 0 else "~"
        except Exception:
            repo = "~"

    # Parse comma-separated strings to lists
    scope_list: list[str] | None = None
    if scope:
        scope_list = [s.strip() for s in scope.split(",") if s.strip()]

    exclude_list: list[str] | None = None
    if exclude:
        exclude_list = [e.strip() for e in exclude.split(",") if e.strip()]

    # Build output path: <dir>/<name>.md
    out_path = (dir / name).with_suffix(".md")

    try:
        created_path = scaffold_spec(
            name=name,
            path=out_path,
            repo=repo,
            scope=scope_list,
            exclude=exclude_list,
        )
        _ok(
            cmd,
            {"path": str(created_path), "name": name},
            [
                _action(
                    f"$EDITOR {created_path}",
                    "Open spec in editor",
                )
            ],
            version=VERSION,
        )
    except FileExistsError as exc:
        sys.exit(
            _err(
                cmd,
                str(exc),
                "SPEC_EXISTS",
                f"Remove or rename {out_path} before scaffolding",
                [_action(f"ls -la {out_path}", "Inspect existing file")],
                exit_code=1,
            )
        )


@app.command
def plan(
    *,
    dir: Annotated[Path, Parameter(name=["--dir"])] = Path("~/epigenome/chromatin/loci/plans/"),
    pending: Annotated[bool, Parameter(name=["--pending"])] = False,
) -> None:
    """Display spec DAG — status, dependencies, and dispatchability."""
    cmd = "mtor plan"
    directory = dir.expanduser()

    specs = scan_specs(directory)

    if not specs:
        _ok(
            cmd,
            {"specs": [], "counts": {"ready": 0}, "directory": str(directory)},
            version=VERSION,
        )
        return

    try:
        resolved = resolve_dag(specs)
    except CycleDetected as exc:
        sys.exit(
            _err(
                cmd,
                str(exc),
                "CIRCULAR_DEPENDENCY",
                "Break the cycle by removing one depends_on entry",
                [_action("mtor plan", "Re-run after fixing the cycle")],
                exit_code=1,
            )
        )

    dag = display_dag(resolved)

    if pending:
        ready_specs = dag["specs"].get("ready", [])
        result = {
            "specs": {"ready": ready_specs} if ready_specs else {},
            "counts": {"ready": len(ready_specs)},
            "directory": str(directory),
        }
    else:
        result = {
            "specs": dag["specs"],
            "counts": dag["counts"],
            "directory": str(directory),
        }

    _ok(cmd, result, version=VERSION)


@app.command(name="plan_done")
def plan_done(
    name: str,
    *,
    dir: Annotated[Path, Parameter(name=["--dir"])] = Path("~/epigenome/chromatin/loci/plans/"),
) -> None:
    """Mark a spec as done."""
    cmd = f"mtor plan done {name}"
    directory = dir.expanduser()

    spec_file = directory / f"{name}.md"
    if not spec_file.is_file():
        sys.exit(
            _err(
                cmd,
                f"Spec not found: {name}",
                "SPEC_NOT_FOUND",
                f"List specs: mtor plan --dir {directory}",
                exit_code=1,
            )
        )

    update_spec_status(spec_file, "done")

    _ok(cmd, {"name": name, "status": "done"}, version=VERSION)


@app.command
def watch(
    action: Literal["start", "query", "stop"] = "start",
    workflow_id: Annotated[str | None, Parameter(name=["--workflow-id", "-w"])] = None,
    *,
    interval: Annotated[int, Parameter(name=["-i", "--interval"])] = 60,
    once: Annotated[bool, Parameter(name=["--once"])] = False,
    max_cycles: Annotated[int | None, Parameter(name=["--max-cycles"])] = None,
    max_concurrent: Annotated[int, Parameter(name=["--max-concurrent"])] = 3,
    plan_dir: Annotated[str, Parameter(name=["--plan-dir"])] = "",
    provider: Annotated[str, Parameter(name=["-p", "--provider"])] = "zhipu",
) -> None:
    """Poll ganglion remote, auto-sync, and dispatch ready specs.

    Actions:
      start  – start WatchWorkflow on Temporal (default)
      query  – query status of a running WatchWorkflow (needs -w ID)
      stop   – stop a running WatchWorkflow (needs -w ID)
    """
    cmd = f"mtor watch {action}"

    if action == "stop":
        _stop_watch_workflow(cmd, workflow_id)
        return

    if action == "query":
        _query_watch_workflow(cmd, workflow_id)
        return

    # action == "start"
    # --once: use local run_watch (backward compat)
    if once:
        import sys as _sys

        def _on_cycle(cycle):
            if cycle.fetched > 0:
                status = "merged" if cycle.merged else f"error: {cycle.error}"
                print(f"[watch] cycle {cycle.cycle}: fetched {cycle.fetched} commits, {status}", file=_sys.stderr)
            else:
                print(f"[watch] cycle {cycle.cycle}: up to date", file=_sys.stderr)

        stats = run_watch(
            REPO_DIR,
            interval=interval,
            max_cycles=max_cycles,
            once=once,
            on_cycle=_on_cycle,
        )
        _ok(cmd, stats.to_dict(), [_action("mtor list", "Check synced workflows")], version=VERSION)
        return

    # Start Temporal-native WatchWorkflow
    client, err = _get_client()
    if err:
        sys.exit(
            _err(
                cmd,
                f"Cannot connect to Temporal at {TEMPORAL_HOST}: {err}",
                "TEMPORAL_UNREACHABLE",
                f"Start Temporal worker: ssh {WORKER_HOST} 'sudo systemctl start temporal-worker'",
                [_action("mtor doctor", "Run health check")],
                exit_code=3,
            )
        )

    from datetime import UTC, datetime

    from mtor.worker.workflow import WatchWorkflow

    wf_id = f"watch-{datetime.now(UTC).strftime('%Y%m%d-%H%M%S')}"
    params = {
        "repo_path": REPO_DIR,
        "plan_dir": plan_dir,
        "interval": interval,
        "max_concurrent": max_concurrent,
        "max_cycles": max_cycles or 100,
        "provider": provider,
    }

    async def _start():
        handle = await client.start_workflow(
            WatchWorkflow.run,
            args=[params],
            id=wf_id,
            task_queue=TASK_QUEUE,
        )
        return handle.id

    started_id = asyncio.run(_start())
    _ok(
        cmd,
        {"workflow_id": started_id, "status": "started", "params": params},
        [
            _action(f"mtor watch query -w {started_id}", "Query watch status"),
            _action(f"mtor watch stop -w {started_id}", "Stop watch workflow"),
        ],
        version=VERSION,
    )


def _stop_watch_workflow(cmd: str, workflow_id: str | None) -> None:
    """Stop a running WatchWorkflow via signal or termination."""
    if not workflow_id:
        # Try to find running watch workflows
        client, err = _get_client()
        if err:
            sys.exit(_err(cmd, f"Cannot connect: {err}", "TEMPORAL_UNREACHABLE", "mtor doctor", exit_code=3))

        async def _find_and_stop():
            stopped = []
            async for ex in client.list_workflows(query="ExecutionStatus = 'Running'"):
                if "watch-" in ex.id:
                    handle = client.get_workflow_handle(ex.id)
                    try:
                        await handle.signal("stop")
                        stopped.append(ex.id)
                    except Exception:
                        with contextlib.suppress(Exception):
                            await handle.terminate(reason="Stopped via mtor watch stop")
                        stopped.append(ex.id)
            return stopped

        stopped = asyncio.run(_find_and_stop())
        if stopped:
            _ok(cmd, {"stopped": stopped, "count": len(stopped)}, version=VERSION)
        else:
            _ok(cmd, {"stopped": [], "count": 0, "message": "No running watch workflows found"}, version=VERSION)
        return

    client, err = _get_client()
    if err:
        sys.exit(_err(cmd, f"Cannot connect: {err}", "TEMPORAL_UNREACHABLE", "mtor doctor", exit_code=3))

    async def _stop():
        handle = client.get_workflow_handle(workflow_id)
        try:
            await handle.signal("stop")
            return "signaled"
        except Exception:
            await handle.terminate(reason="Stopped via mtor watch stop")
            return "terminated"

    method = asyncio.run(_stop())
    _ok(cmd, {"workflow_id": workflow_id, "status": method}, version=VERSION)


def _query_watch_workflow(cmd: str, workflow_id: str | None) -> None:
    """Query the status of a running WatchWorkflow."""
    if not workflow_id:
        sys.exit(
            _err(
                cmd,
                "Missing workflow_id",
                "MISSING_ARGS",
                "Provide -w/--workflow-id or omit query to list watch workflows",
                exit_code=2,
            )
        )

    client, err = _get_client()
    if err:
        sys.exit(_err(cmd, f"Cannot connect: {err}", "TEMPORAL_UNREACHABLE", "mtor doctor", exit_code=3))

    async def _query():
        handle = client.get_workflow_handle(workflow_id)
        desc = await handle.describe()
        result = None
        status_name = desc.status.name if desc.status else "UNKNOWN"
        if status_name == "COMPLETED":
            with contextlib.suppress(Exception):
                result = await handle.result()
        return {
            "workflow_id": workflow_id,
            "status": status_name,
            "start_time": desc.start_time.isoformat() if desc.start_time else None,
            "result": result,
        }

    result = asyncio.run(_query())
    _ok(cmd, result, version=VERSION)


@app.command
def pause() -> None:
    """Pause dispatching — blocks new tasks and watch sync cycles."""
    cmd = "mtor pause"
    if _is_paused():
        _ok(cmd, {"status": "already_paused"}, version=VERSION)
        return
    path = _create_pause()
    _ok(
        cmd,
        {"status": "paused", "pause_file": str(path)},
        [_action("mtor resume", "Resume dispatching")],
        version=VERSION,
    )


@app.command
def resume() -> None:
    """Resume dispatching — removes pause marker."""
    cmd = "mtor resume"
    if not _is_paused():
        _ok(cmd, {"status": "already_running"}, version=VERSION)
        return
    was_paused = _remove_pause()
    _ok(
        cmd,
        {"status": "resumed", "was_paused": was_paused},
        [_action("mtor pause", "Pause again if needed")],
        version=VERSION,
    )


@app.command
def freeze() -> None:
    """Freeze all activity — blocks dispatch and watch sync (deptor lock)."""
    cmd = "mtor freeze"
    if _is_frozen():
        _ok(cmd, {"status": "already_frozen"}, version=VERSION)
        return
    path = _create_freeze()
    _ok(
        cmd,
        {"status": "frozen", "freeze_file": str(path)},
        [_action("mtor thaw", "Unfreeze dispatching")],
        version=VERSION,
    )


@app.command
def thaw() -> None:
    """Thaw (unfreeze) — resumes all dispatch and sync activity."""
    cmd = "mtor thaw"
    if not _is_frozen():
        _ok(cmd, {"status": "not_frozen"}, version=VERSION)
        return
    was_frozen = _remove_freeze()
    _ok(
        cmd,
        {"status": "thawed", "was_frozen": was_frozen},
        [_action("mtor freeze", "Freeze again if needed")],
        version=VERSION,
    )


@app.command
def autophagy(
    *,
    repo: Annotated[str | None, Parameter(name=["--repo"])] = None,
    no_test: Annotated[bool, Parameter(name=["--no-test"])] = False,
) -> None:
    """Salvage new commits from ganglion, cherry-pick, and run tests."""
    from mtor.autophagy import salvage

    cmd = "mtor autophagy"
    repo_path = repo or REPO_DIR

    result = salvage(repo_path, run_tests=not no_test)

    next_actions = []
    if result.cherry_picked:
        next_actions.append(_action("mtor list", "Check synced workflows"))
    if result.error:
        next_actions.append(_action("mtor doctor", "Diagnose connectivity"))

    _ok(
        cmd,
        {
            "fetched": result.fetched,
            "cherry_picked": result.cherry_picked,
            "skipped": result.skipped,
            "tests_passed": result.tests_passed,
            "error": result.error,
        },
        next_actions,
        version=VERSION,
    )


@app.command(name="dispatch-all")
def dispatch_all(
    *,
    dir: Annotated[Path, Parameter(name=["--dir"])] = Path("~/epigenome/chromatin/loci/plans/"),
    provider: Annotated[str, Parameter(name=["-p", "--provider"])] = "zhipu",
    dry_run: Annotated[bool, Parameter(name=["--dry-run"])] = False,
) -> None:
    """Dispatch all ready (dispatchable) specs from a plan directory."""
    import io as _io

    from mtor.dispatch import _inject_spec_constraints

    cmd = "mtor dispatch-all"
    directory = dir.expanduser()

    specs = scan_specs(directory)

    if not specs:
        _ok(cmd, {"dispatched": [], "count": 0, "directory": str(directory)}, version=VERSION)
        return

    try:
        resolved = resolve_dag(specs)
    except CycleDetected as exc:
        sys.exit(
            _err(
                cmd,
                str(exc),
                "CIRCULAR_DEPENDENCY",
                "Break the cycle by removing one depends_on entry",
                [_action("mtor plan", "View the DAG")],
                exit_code=1,
            )
        )

    dispatchable = topological_sort([s for s in resolved if s.get("dispatchable")])

    if not dispatchable:
        _ok(
            cmd,
            {"dispatched": [], "count": 0, "message": "No dispatchable specs", "directory": str(directory)},
            version=VERSION,
        )
        return

    dispatched: list[dict[str, Any]] = []
    errors: list[dict[str, str]] = []

    for spec in dispatchable:
        base_prompt = spec.get("body", "") or spec.get("name", "")
        spec_path = Path(spec["path"])

        prompt = _inject_spec_constraints(
            base_prompt,
            spec_path=spec_path,
            prompt_for_cmd=base_prompt,
        )

        if dry_run:
            dispatched.append({
                "name": spec["name"],
                "status": "would_dispatch",
                "prompt_preview": prompt[:100],
            })
            continue

        # Capture stdout to prevent individual dispatch from printing
        captured = _io.StringIO()
        old_stdout = sys.stdout
        try:
            sys.stdout = captured
            workflow_id = _dispatch_prompt(
                prompt,
                provider=provider,
                spec_path=spec_path,
            )
            dispatched.append({
                "name": spec["name"],
                "workflow_id": workflow_id,
                "status": "dispatched",
            })
        except SystemExit as exc:
            code = exc.code if isinstance(exc.code, int) else 1
            output = captured.getvalue()
            error_msg = output[:200] if output else f"exit_code={code}"
            errors.append({
                "name": spec["name"],
                "error": error_msg,
            })
        finally:
            sys.stdout = old_stdout

    result: dict[str, Any] = {
        "dispatched": dispatched,
        "count": len(dispatched),
        "errors": errors,
        "directory": str(directory),
    }
    if dry_run:
        result["dry_run"] = True

    _ok(cmd, result, version=VERSION)


# ---------------------------------------------------------------------------
# Infra subcommand group
# ---------------------------------------------------------------------------

infra_app = App(name="infra", help_flags=[], version_flags=[])
app.command(infra_app)


@infra_app.command
def check() -> None:
    """Infrastructure health check — worker SSH, repo, git, disk."""
    cmd = "mtor infra check"
    report = _check_health()
    result = report.to_dict()
    next_actions = []
    if not report.ok:
        next_actions.append(_action("mtor doctor", "Full health check"))
        next_actions.append(_action("mtor infra deploy", "Redeploy to fix issues"))
    _ok(cmd, result, next_actions, version=VERSION)


@infra_app.command
def deploy() -> None:
    """Sync code to worker, restart services, verify health."""
    cmd = "mtor infra deploy"
    result = _deploy()
    payload = result.to_dict()
    next_actions = []
    if result.healthy:
        next_actions.append(_action("mtor infra check", "Verify health after deploy"))
    else:
        next_actions.append(_action("mtor doctor", "Full health check"))
    _ok(cmd, payload, next_actions, version=VERSION)


@infra_app.command
def clean(
    *,
    older_than_days: Annotated[int, Parameter(name=["--older-than-days"])] = 7,
) -> None:
    """Remove old output and checkpoint files."""
    cmd = "mtor infra clean"
    result = _clean(older_than_days=older_than_days)
    _ok(cmd, result.to_dict(), version=VERSION)
