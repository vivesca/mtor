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
import json
import os
import shlex
import subprocess
import sys
import time
from pathlib import Path
from typing import Annotated, Any, Literal

from cyclopts import App, Parameter
from porin import action as _action

from mtor import (
    DEPLOY_REMOTE,
    LOG_TAIL_LINES,
    OUTPUTS_DIR,
    REPO_DIR,
    TASK_QUEUE,
    TEMPORAL_HOST,
    VERSION,
    WORKER_HOST,
    WORKER_LOG_DIR,
)
from mtor.client import _get_client, workflow_execution_state
from mtor.dedup import check_duplicate as _check_dedup
from mtor.dedup import record_dispatch as _record_dispatch
from mtor.dispatch import _dispatch_prompt
from mtor.doctor import doctor as _doctor
from mtor.envelope import _err, _extract_first_result, _ok
from mtor.harness import PROVIDER_HARNESS_MAP
from mtor.rptor import (
    CycleDetected,
    audit_specs,
    display_dag,
    resolve_dag,
    scan_specs,
    topological_sort,
)
from mtor.scan import _run_checks
from mtor.triage import (
    archive_ids,
    archived_ids,
    get_verdict_overrides,
    load_triage,
    override_verdict,
    parse_duration,
    review_ids,
)
from mtor.tree import tree
from mtor.spec import DEFAULT_SPEC_DIR, scaffold_spec, update_spec_status, validate_spec
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


def _check_dedup_only(*args, **kwargs):
    return _check_dedup(*args, **kwargs)


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
                desc = await handle.describe()
                if getattr(getattr(desc, "status", None), "name", None) == "RUNNING":
                    return ""
                wf_result = await handle.result()
                if isinstance(wf_result, dict):
                    task_result = _extract_first_result(wf_result)
                    if task_result:
                        return task_result.get("review", {}).get(
                            "output_path", ""
                        ) or task_result.get("output_path", "")
                return ""

            log_path = asyncio.run(_get_output_path())
        except Exception:
            pass

    if not log_path:
        try:
            find_result = subprocess.run(
                [
                    "ssh",
                    WORKER_HOST,
                    (
                        f"find {OUTPUTS_DIR} {WORKER_LOG_DIR} -maxdepth 1 "
                        f"\\( -name '*.txt' -o -name '{workflow_id}.log' -o "
                        f"-name '{workflow_id}.jsonl' \\) -type f "
                        f"-printf '%T@ %p\\n' 2>/dev/null | sort -rn | head -40 | cut -d' ' -f2-"
                    ),
                ],
                capture_output=True,
                text=True,
                timeout=15,
            )
            if find_result.returncode == 0:
                wf_suffix = workflow_id.rsplit("-", 1)[-1] if "-" in workflow_id else workflow_id
                for line in find_result.stdout.strip().splitlines():
                    candidate = line.strip()
                    fname = candidate.rsplit("/", 1)[-1]
                    if workflow_id in fname or wf_suffix in fname:
                        log_path = candidate
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


def _terminated_diagnostics(workflow_id: str) -> dict[str, Any]:
    """Stderr tail + detected kill reason for a TERMINATED wf (local log, no SSH)."""
    outputs = Path(OUTPUTS_DIR)
    log_path = ""
    if outputs.is_dir():
        suffix = workflow_id.rsplit("-", 1)[-1] if "-" in workflow_id else workflow_id
        matches = sorted(
            (p for p in outputs.glob("*.txt") if workflow_id in p.name or suffix in p.name),
            key=lambda p: p.stat().st_mtime,
            reverse=True,
        )
        if matches:
            log_path = str(matches[0])
    if not log_path:
        return {"kill_reason": None, "stderr_tail": "", "log_path": ""}
    try:
        content = Path(log_path).read_text(encoding="utf-8", errors="replace")
    except OSError:
        return {"kill_reason": None, "stderr_tail": "", "log_path": ""}
    low = content.lower()
    kill_reason = next((m for m in ("wall-limit", "oom", "out of memory", "sigkill", "killed") if m in low), None)
    tail = "\n".join(content.splitlines()[-LOG_TAIL_LINES:])
    return {"kill_reason": kill_reason, "stderr_tail": tail, "log_path": log_path}


_APPROVED_VERDICTS = {"accepted", "approved", "approved_with_flags", "false_positive", "early_exit_clean"}


def _operator_state(status_val: str, result_payload: dict[str, Any]) -> str:
    """Return the operator-facing outcome, not just the process lifecycle state."""
    verdict = result_payload.get("verdict")
    success = result_payload.get("success")

    if status_val == "RUNNING":
        return "running"
    if status_val == "COMPLETED":
        if verdict in _APPROVED_VERDICTS:
            return "approved"
        if verdict == "rejected":
            return "failed_review"
        if verdict == "incomplete":
            return "incomplete"
        if success is False:
            return "failed_process"
        return "completed_unreviewed"
    if status_val == "FAILED":
        return "failed_workflow"
    if status_val == "TERMINATED":
        return "terminated"
    if status_val == "CANCELED":
        return "canceled"
    return status_val.lower()


def _status_next_actions(workflow_id: str, operator_state: str) -> list[dict[str, str]]:
    """Choose status actions that make sense for the current operator state."""
    actions = [_action(f"mtor logs {workflow_id}", "Fetch last 30 lines of output")]
    if operator_state == "running":
        actions.append(_action(f"mtor cancel {workflow_id}", "Cancel this workflow"))
    elif operator_state in {"approved", "completed_unreviewed"}:
        actions.extend([
            _action(f"mtor review {workflow_id}", "Mark this workflow as reviewed"),
            _action(f"mtor archive {workflow_id}", "Archive after review"),
        ])
    elif operator_state in {"failed_review", "incomplete", "failed_process"}:
        actions.extend([
            _action(f"mtor verdict {workflow_id} --set false_positive", "Override if review was wrong"),
            _action(f"mtor review {workflow_id}", "Mark this workflow as reviewed"),
            _action(f"mtor archive {workflow_id}", "Archive after triage"),
        ])
    else:
        actions.append(_action(f"mtor archive {workflow_id}", "Archive after triage"))
    return actions


def _search_attributes_dict(execution: Any) -> dict[str, Any]:
    """Return a JSON-friendly mapping of Temporal search attributes."""
    attrs = getattr(execution, "search_attributes", None)
    if not attrs:
        return {}
    try:
        items = attrs.items()
    except AttributeError:
        return {}

    result: dict[str, Any] = {}
    for key, value in items:
        name = str(key)
        if "." in name:
            name = name.rsplit(".", 1)[-1]
        if isinstance(value, (list, tuple)):
            result[name] = list(value)
        else:
            result[name] = value
    return result


def _failure_text(value: Any) -> str:
    if value is None:
        return ""
    for attr in ("message", "cause", "application_failure_info"):
        candidate = getattr(value, attr, None)
        if isinstance(candidate, str) and candidate:
            return candidate
    return str(value)


def _pending_activity_details(desc: Any) -> list[dict[str, Any]]:
    """Extract compact pending activity details from Temporal describe output."""
    details: list[dict[str, Any]] = []
    for activity in getattr(desc, "pending_activities", None) or []:
        heartbeat_time = getattr(activity, "last_heartbeat_time", None)
        activity_type = getattr(activity, "activity_type", None)
        if not isinstance(activity_type, str):
            activity_type = getattr(activity_type, "name", None) or str(activity_type or "")
        last_failure = _failure_text(getattr(activity, "last_failure", None))
        details.append({
            "activity_id": getattr(activity, "activity_id", "") or "",
            "activity_type": activity_type,
            "state": str(getattr(activity, "state", "") or ""),
            "attempt": getattr(activity, "attempt", None),
            "last_heartbeat_time": heartbeat_time.isoformat() if heartbeat_time else None,
            "last_failure": last_failure[:500] if last_failure else "",
        })
    return details


def _trace_diagnosis(payload: dict[str, Any]) -> str:
    operator_state = payload.get("operator_state")
    if operator_state == "running":
        execution_state = payload.get("execution_state", {})
        pending_activities = payload.get("pending_activities") or []
        no_activity_evidence = not pending_activities and (
            payload.get("active_logs") or execution_state.get("execution_state") == "queued"
        )
        if no_activity_evidence:
            return "workflow is stale: running in Temporal with no activity currently executing"
        if execution_state.get("heartbeat_stale"):
            return "running workflow has a stale activity heartbeat"
        if execution_state.get("execution_state") == "queued":
            return "workflow is running in Temporal but no fresh activity heartbeat is visible"
        return "workflow is currently executing"
    if operator_state == "approved":
        return "workflow completed and review approved the artifact"
    if operator_state == "failed_review":
        return "workflow process completed but chaperone review rejected the artifact"
    if operator_state == "incomplete":
        return "workflow produced partial work but did not complete cleanly"
    if operator_state in {"failed_process", "failed_workflow"}:
        return "workflow failed during execution"
    if operator_state in {"terminated", "canceled"}:
        kill_reason = payload.get("kill_reason")
        if kill_reason:
            return f"workflow was {operator_state} ({kill_reason})"
        return f"workflow was {operator_state}"
    return "workflow state requires review"


def _trace_next_action(workflow_id: str, operator_state: str) -> dict[str, str]:
    if operator_state == "running":
        return _action("mtor logs --active", "Inspect active worker logs")
    if operator_state == "approved":
        return _action(f"mtor review {workflow_id}", "Mark approved workflow as reviewed")
    if operator_state in {"failed_review", "incomplete", "failed_process"}:
        return _action(f"mtor logs {workflow_id}", "Inspect output before retry or override")
    if operator_state in {"terminated", "canceled", "failed_workflow"}:
        return _action(f"mtor logs {workflow_id}", "Inspect any preserved output")
    return _action(f"mtor status {workflow_id}", "Re-check workflow status")


def _cached_log_path(workflow_id: str) -> str:
    """Return the newest local cached log path for a workflow, if present."""
    cache_dir = Path.home() / ".cache" / "mtor" / "logs"
    if not cache_dir.exists():
        return ""

    wf_suffix = workflow_id.rsplit("-", 1)[-1] if "-" in workflow_id else workflow_id
    candidates: list[Path] = []
    for pattern in (
        f"{workflow_id}.jsonl",
        f"{workflow_id}.log",
        f"{workflow_id}.txt",
        f"*{workflow_id}*",
        f"*{wf_suffix}*",
    ):
        candidates.extend(cache_dir.glob(pattern))

    files = [path for path in candidates if path.is_file()]
    if not files:
        return ""

    return str(max(files, key=lambda path: path.stat().st_mtime))


def _cached_lifecycle_jsonl_path(workflow_id: str) -> str:
    """Return the cached lifecycle JSONL path for a workflow, preferring <id>.jsonl.

    Derives a .jsonl sibling from a cached .log/.txt path when the direct
    <workflow_id>.jsonl file is absent.  Returns empty string when no JSONL
    events file exists locally.
    """
    cache_dir = Path.home() / ".cache" / "mtor" / "logs"
    if not cache_dir.exists():
        return ""

    direct = cache_dir / f"{workflow_id}.jsonl"
    if direct.is_file():
        return str(direct)

    cached = _cached_log_path(workflow_id)
    if not cached:
        return ""

    jsonl_sibling = Path(cached).with_suffix(".jsonl")
    if jsonl_sibling.is_file():
        return str(jsonl_sibling)

    return ""


_SENSITIVE_FIELDS = frozenset({"task", "prompt", "stdout", "stderr", "output", "tail", "diff"})
_LIFECYCLE_MAX_EVENTS = 8


def _read_lifecycle_events(log_path: str) -> list[dict[str, Any]]:
    """Read up to 8 recent lifecycle events from a cached JSONL, dropping sensitive fields."""
    path = Path(log_path)
    if not path.exists() or not path.is_file():
        return []
    try:
        lines = path.read_text().splitlines()
    except OSError:
        return []
    events: list[dict[str, Any]] = []
    for line in lines:
        try:
            event = json.loads(line)
        except (json.JSONDecodeError, ValueError):
            continue
        if not isinstance(event, dict):
            continue
        filtered = {k: v for k, v in event.items() if k not in _SENSITIVE_FIELDS}
        events.append(filtered)
    return events[-_LIFECYCLE_MAX_EVENTS:]


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


def _parse_semver(version: str) -> tuple[int, int, int]:
    import re as _re

    match = _re.fullmatch(r"(\d+)\.(\d+)\.(\d+)", version)
    if not match:
        raise ValueError(f"Invalid semantic version: {version}")
    return int(match.group(1)), int(match.group(2)), int(match.group(3))


def _bump_semver(version: str, bump: Literal["patch", "minor", "major"]) -> str:
    major, minor, patch_v = _parse_semver(version)
    if bump == "major":
        return f"{major + 1}.0.0"
    if bump == "minor":
        return f"{major}.{minor + 1}.0"
    return f"{major}.{minor}.{patch_v + 1}"


def _read_release_version(pyproject_path: Path, init_path: Path) -> str:
    import re as _re

    pyproject_text = pyproject_path.read_text(encoding="utf-8")
    pyproject_match = _re.search(r'^version = "(\d+\.\d+\.\d+)"$', pyproject_text, flags=_re.MULTILINE)
    if pyproject_match:
        return pyproject_match.group(1)

    init_text = init_path.read_text(encoding="utf-8")
    init_match = _re.search(r'^VERSION = "(\d+\.\d+\.\d+)"$', init_text, flags=_re.MULTILINE)
    if init_match:
        return init_match.group(1)
    raise ValueError("Cannot parse version from pyproject.toml or mtor/__init__.py")


def _write_release_version(pyproject_path: Path, init_path: Path, new_version: str) -> None:
    import re as _re

    pyproject_text = pyproject_path.read_text(encoding="utf-8")
    if _re.search(r'^version = "(\d+\.\d+\.\d+)"$', pyproject_text, flags=_re.MULTILINE):
        pyproject_text = _re.sub(
            r'^version = "\d+\.\d+\.\d+"$',
            f'version = "{new_version}"',
            pyproject_text,
            count=1,
            flags=_re.MULTILINE,
        )
    elif 'dynamic = [ "version" ]' in pyproject_text:
        pyproject_text = pyproject_text.replace(
            'dynamic = [ "version" ]',
            f'version = "{new_version}"',
            1,
        )
    else:
        raise ValueError("Cannot update version in pyproject.toml")
    pyproject_path.write_text(pyproject_text, encoding="utf-8")

    init_text = init_path.read_text(encoding="utf-8")
    if not _re.search(r'^VERSION = "(\d+\.\d+\.\d+)"$', init_text, flags=_re.MULTILINE):
        raise ValueError("Cannot update VERSION in mtor/__init__.py")
    init_text = _re.sub(
        r'^VERSION = "\d+\.\d+\.\d+"$',
        f'VERSION = "{new_version}"',
        init_text,
        count=1,
        flags=_re.MULTILINE,
    )
    init_path.write_text(init_text, encoding="utf-8")

# ---------------------------------------------------------------------------
# Cyclopts CLI
# ---------------------------------------------------------------------------

app = App(help_flags=["--help", "-h"], version_flags=[])

spec_app = App(name="spec", help_flags=[], version_flags=[])
app.command(spec_app)


@app.default
def default_handler(
    prompt: str | None = None,
    *,
    provider: Annotated[str, Parameter(name=["-p", "--provider"])] = "zhipu",
    experiment: Annotated[bool, Parameter(name=["-x", "--experiment"])] = False,
    skip_sha_check: Annotated[bool, Parameter(name=["--skip-sha-check"])] = False,
    then: Annotated[list[str] | None, Parameter(name=["--then"])] = None,
    spec: Annotated[Path | None, Parameter(name=["--spec"])] = None,
    harness: Annotated[str, Parameter(name=["--harness"])] = "",
    explain: Annotated[bool, Parameter(name=["--explain"])] = False,
) -> None:
    """Bare invocation returns command tree; with a prompt, dispatches to Temporal.

    --then: follow-up prompts dispatched after this task completes with approved verdict.
    """
    # Resolve prompt from --spec file. Strip YAML frontmatter at ingest so
    # downstream harnesses never see `---` as a leading token — claude CLI
    # interprets `---` as a flag and exits with "Unknown flag: ---", which
    # the ribosome bash retry loop then misclassifies as empty-output =
    # rate-limit and retries thousands of times. Incident 2026-04-11: a
    # single recombinase dispatch produced 59k retries and a 208 MB log
    # before the worker was restarted. The same regex lives at
    # dispatch.py:201 but only fires when the positional prompt arg is
    # itself a file path, which is not the case on the `--spec` code path.
    if spec is not None:
        import re as _re

        _frontmatter_errors = validate_spec(spec)
        if _frontmatter_errors:
            cmd = f"mtor --spec {spec}"
            msg = "Spec validation failed:\n" + "\n".join(f"  - {e}" for e in _frontmatter_errors)
            sys.exit(
                _err(cmd, msg, "SPEC_INVALID", "Fix the spec and retry.", [], exit_code=1)
            )

        spec_contents = spec.read_text(encoding="utf-8").strip()
        spec_contents = _re.sub(
            r"\A---\n.*?\n---\n*", "", spec_contents, count=1, flags=_re.DOTALL
        ).strip()
        if prompt is None:
            prompt = spec_contents
        else:
            prompt = spec_contents + "\n" + prompt

    if prompt is None:
        if sys.stdout.isatty():
            app.help_print()
        else:
            _ok("mtor", tree.to_dict(), version=VERSION)
        return

    subcommand_names = frozenset({
        "riboseq", "list", "status", "logs", "cancel", "terminate",
        "schema", "scout", "research", "scan", "doctor", "tsc", "rptor",
        "approve", "deny", "reactivate", "rapa", "derapa", "deptor",
        "dedeptor", "auto", "verdict", "review", "archive", "autophagy",
        "init", "ragulator", "rictor",
    })
    stripped_prompt = prompt.strip()
    if len(stripped_prompt) < 10 or stripped_prompt.lower() in subcommand_names:
        cmd = f"mtor {prompt[:60]}{'...' if len(prompt) > 60 else ''}"
        sys.exit(
            _err(
                cmd,
                f"Prompt too short or matches a subcommand name ({len(stripped_prompt)} chars, minimum 10). "
                "Provide a meaningful task description.",
                "PROMPT_TOO_SHORT",
                "Provide a prompt >=10 characters that describes the task. "
                "Use 'mtor --help' to see available commands.",
                [_action("mtor --help", "Show available commands")],
                exit_code=2,
            )
        )
    else:
        if harness and harness not in PROVIDER_HARNESS_MAP:
            cmd = f"mtor {prompt[:60]}{'...' if len(prompt) > 60 else ''}"
            allowed = ", ".join(sorted(PROVIDER_HARNESS_MAP))
            sys.exit(
                _err(
                    cmd,
                    f"Unknown harness '{harness}'",
                    "UNKNOWN_HARNESS",
                    f"Use one of: {allowed}",
                    [],
                    exit_code=2,
                )
            )

        if explain:
            from mtor.dispatch import _dispatch_explanation

            plan = _dispatch_explanation(
                prompt,
                provider=provider,
                experiment=experiment,
                skip_sha_check=skip_sha_check,
                chain=then,
                spec_path=spec,
                harness=harness,
                paused=_is_paused(),
                frozen=_is_frozen(),
            )
            _ok(
                f"mtor {prompt[:60]}{'...' if len(prompt) > 60 else ''} --explain",
                plan,
                plan["next_actions"],
                version=VERSION,
            )
            return

        # Freeze check — block dispatch when frozen (deptor lock)
        if _is_frozen():
            cmd = f"mtor {prompt[:60]}{'...' if len(prompt) > 60 else ''}"
            sys.exit(
                _err(
                    cmd,
                    "Dispatching is frozen. Use 'mtor dedeptor' to unfreeze.",
                    "FROZEN",
                    "Run: mtor dedeptor",
                    [_action("mtor dedeptor", "Unfreeze dispatching")],
                    exit_code=1,
                )
            )
        # Pause check — block dispatch when paused
        if _is_paused():
            cmd = f"mtor {prompt[:60]}{'...' if len(prompt) > 60 else ''}"
            sys.exit(
                _err(
                    cmd,
                    "Dispatching is paused. Use 'mtor derapa' to resume.",
                    "PAUSED",
                    "Run: mtor derapa",
                    [_action("mtor derapa", "Resume dispatching")],
                    exit_code=1,
                )
            )
        # Spec dispatch-readiness gate — tests field required, no bypass
        _repo = Path(".")
        if spec is not None:
            from mtor.dispatch import validate_spec as _validate_spec
            from mtor.rptor import parse_spec as _parse_spec
            _spec_data = _parse_spec(spec)
            _repo = Path(_spec_data.get("repo", ".")).expanduser()
            _spec_errors = _validate_spec(spec, _repo)
            if _spec_errors:
                cmd = f"mtor --spec {spec}"
                msg = "Spec validation failed:\n" + "\n".join(f"  - {e}" for e in _spec_errors)
                sys.exit(
                    _err(cmd, msg, "SPEC_INVALID", "Fix the spec and retry.", [], exit_code=1)
                )

        # Dedup check — block identical dispatches within 5-minute window.
        # Check-only: do NOT record yet.  Recording happens only after
        # _dispatch_prompt successfully creates a workflow.
        dup_key = _check_dedup_only(prompt, spec_path=spec) if prompt.strip() else None
        if dup_key is not None:
            cmd = f"mtor {prompt[:60]}{'...' if len(prompt) > 60 else ''}"
            sys.exit(
                _err(
                    cmd,
                    f"Duplicate dispatch blocked (key={dup_key}). Same prompt dispatched within the last 5 minutes.",
                    "DEDUP_BLOCKED",
                    "Wait a few minutes or change the prompt/spec to dispatch again.",
                    [_action("mtor riboseq", "View running workflows")],
                    exit_code=1,
                )
            )

        # Resolve spec repo for SHA gate (uses spec frontmatter repo: path).
        _spec_repo: str | None = None
        if spec is not None:
            _spec_repo = str(_repo)

        _dispatch_prompt(
            prompt,
            provider=provider,
            experiment=experiment,
            skip_sha_check=skip_sha_check,
            chain=then,
            spec_path=spec,
            harness=harness,
            repo=_spec_repo,
        )

        # Record dedup only after dispatch succeeds — failed preflight must not
        # poison the window.
        if prompt.strip():
            _record_dispatch(prompt, spec_path=spec)


@app.command(name=["riboseq", "list"])
def list_cmd(
    *,
    status: Literal["RUNNING", "COMPLETED", "FAILED", "CANCELED", "TERMINATED"] | None = None,
    count: int = 50,
    since: Annotated[int | None, Parameter(name=["-s", "--since"])] = None,
    pending: Annotated[bool, Parameter(name=["--pending"])] = False,
    all_: Annotated[bool, Parameter(name=["--all"])] = False,
    provider_filter: Annotated[str | None, Parameter(name=["--provider"])] = None,
    verdict_filter: Annotated[str | None, Parameter(name=["--verdict"])] = None,
    mode_filter: Annotated[str | None, Parameter(name=["--mode"])] = None,
    archived: Annotated[bool, Parameter(name=["--archived"])] = False,
) -> None:
    """List recent workflows. --since N shows last N hours only. --archived prints archived IDs from triage.json."""
    cmd = "mtor riboseq" + (f" --status {status}" if status else "") + f" --count {count}"

    if archived:
        triage = load_triage()
        for workflow_id in sorted(archived_ids(triage)):
            print(workflow_id)
        return

    client, err = _get_client()
    if err:
        sys.exit(
            _err(
                cmd,
                f"Cannot connect to Temporal at {TEMPORAL_HOST}: {err}",
                "TEMPORAL_UNREACHABLE",
                f"Start Temporal worker: ssh {WORKER_HOST} 'sudo systemctl start temporal-worker'",
                [_action("mtor tsc", "Run health check to diagnose connectivity")],
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

        # Temporal Search Attribute filters
        if provider_filter:
            query_parts.append(f"mtor_provider = '{provider_filter}'")
        if verdict_filter:
            query_parts.append(f"mtor_verdict = '{verdict_filter}'")
        if mode_filter:
            query_parts.append(f"mtor_mode = '{mode_filter}'")

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

        async def _execution_states():
            states = {}
            for execution in executions:
                status_name = execution.status.name if execution.status else "UNKNOWN"
                if status_name != "RUNNING":
                    continue
                with contextlib.suppress(Exception):
                    states[execution.id] = await workflow_execution_state(client, execution.id)
            return states

        execution_states = asyncio.run(_execution_states()) if executions else {}

        reconciliation: dict[str, Any] | None = None
        if executions:
            with contextlib.suppress(Exception):
                from mtor.reconcile import reconcile_all

                reconciliation = reconcile_all(
                    DEFAULT_SPEC_DIR.expanduser(),
                    client=client,
                    workflow_descriptions={ex.id: ex for ex in executions},
                )

        # Load triage state
        triage = load_triage()
        reviewed_set = set(triage.get("reviewed", []))
        archived_set = archived_ids(triage)
        verdict_overrides = triage.get("verdict_overrides", {})

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

            # Apply local verdict override (false-positive corrections)
            if wf_id in verdict_overrides:
                sa_verdict = verdict_overrides[wf_id]

            # Filters are now handled by Temporal query, but local overrides might change verdict
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

            workflow_result = {
                "workflow_id": wf_id,
                "status": status_val,
                "verdict": sa_verdict,
                "provider": sa_provider,
                "start_time": start_time,
                "close_time": close_time,
            }
            if wf_id in execution_states:
                workflow_result.update(execution_states[wf_id])
            workflows.append(workflow_result)
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
        if reconciliation and reconciliation.get("fixed"):
            result["reconciled"] = reconciliation["fixed"]
        _ok(cmd, result, next_actions, version=VERSION)
    except Exception as exc:
        sys.exit(
            _err(
                cmd,
                str(exc),
                "LIST_ERROR",
                "Check Temporal server health with: mtor tsc",
                [_action("mtor tsc", "Run health check")],
            )
        )


@app.command
def status(workflow_id: str, short: bool = False) -> None:
    """Query status of a single workflow.

    Args:
        workflow_id: Workflow identifier.
        short: Emit one-line `STATUS | success | verdict | failure_reason` instead of JSON envelope.
    """
    cmd = f"mtor status {workflow_id}"

    client, err = _get_client()
    if err:
        sys.exit(
            _err(
                cmd,
                f"Cannot connect to Temporal at {TEMPORAL_HOST}: {err}",
                "TEMPORAL_UNREACHABLE",
                f"Start Temporal worker: ssh {WORKER_HOST} 'sudo systemctl start temporal-worker'",
                [_action("mtor tsc", "Run health check to diagnose connectivity")],
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
                review = task_result.get("review", {})
                result_payload["success"] = task_result.get("success")
                result_payload["exit_code"] = task_result.get("exit_code")
                result_payload["provider"] = task_result.get("provider")
                result_payload["task_preview"] = task_result.get("task", "")[:120]
                result_payload["output_path"] = review.get("output_path", "") or task_result.get("output_path", "")
                if not result_payload["output_path"]:
                    cached_log_path = _cached_log_path(workflow_id)
                    if cached_log_path:
                        result_payload["cached_log_path"] = cached_log_path
                result_payload["merged"] = task_result.get("merged")
                result_payload["verdict"] = review.get("verdict")
                completion_evidence = review.get("completion_evidence")
                if completion_evidence:
                    result_payload["completion_evidence"] = completion_evidence
                if review.get("dossier_path"):
                    result_payload["dossier_path"] = review.get("dossier_path")
                if review.get("completion_dossier"):
                    result_payload["completion_dossier"] = review.get("completion_dossier")

        # Apply local verdict override (false-positive corrections)
        vo = get_verdict_overrides()
        if workflow_id in vo:
            result_payload["verdict"] = vo[workflow_id]
        result_payload["operator_state"] = _operator_state(status_val, result_payload)

        # Add failure_reason for non-approved terminal states
        if status_val in ("FAILED", "CANCELED", "TERMINATED") or (
            status_val == "COMPLETED"
            and result_payload.get("verdict") is not None
            and result_payload.get("verdict") not in _APPROVED_VERDICTS
        ):
            failure_reason = "No diagnostic information available"
            if wf_result and isinstance(wf_result, dict):
                task_result = _extract_first_result(wf_result)
                if task_result:
                    failure_reason = _build_failure_reason(task_result)
            result_payload["failure_reason"] = failure_reason

        if short:
            status_field = result_payload.get("status", "?")
            success_field = result_payload.get("success", "—")
            if success_field is None:
                success_field = "—"
            verdict_field = result_payload.get("verdict", "—")
            if verdict_field is None:
                verdict_field = "—"
            failure_field = result_payload.get("failure_reason", "—")
            if isinstance(failure_field, str) and len(failure_field) > 80:
                failure_field = failure_field[:77] + "..."
            print(f"{status_field} | {success_field} | {verdict_field} | {failure_field}")
            return

        _ok(
            cmd,
            result_payload,
            _status_next_actions(workflow_id, result_payload["operator_state"]),
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
                    "Verify the workflow ID with: mtor riboseq",
                    [_action("mtor riboseq", "List all recent workflows")],
                    exit_code=4,
                )
            )
        sys.exit(
            _err(
                cmd,
                exc_str,
                "STATUS_ERROR",
                "Check Temporal server health with: mtor tsc",
                [_action("mtor tsc", "Run health check")],
            )
        )


@app.command
def trace(workflow_id: str) -> None:
    """Trace a workflow across Temporal status, review evidence, logs, and next action."""
    cmd = f"mtor trace {workflow_id}"

    client, err = _get_client()
    if err:
        sys.exit(
            _err(
                cmd,
                f"Cannot connect to Temporal at {TEMPORAL_HOST}: {err}",
                "TEMPORAL_UNREACHABLE",
                f"Start Temporal worker: ssh {WORKER_HOST} 'sudo systemctl start temporal-worker'",
                [_action("mtor tsc", "Run health check to diagnose connectivity")],
                exit_code=3,
            )
        )

    try:
        async def _trace():
            handle = client.get_workflow_handle(workflow_id)
            desc = await handle.describe()
            wf_result = None
            result_error = ""
            status_name = desc.status.name if desc.status else "UNKNOWN"
            if status_name != "RUNNING":
                try:
                    wf_result = await handle.result()
                except Exception as exc:
                    result_error = str(exc)[:1000]
            execution_state = {}
            if status_name == "RUNNING":
                with contextlib.suppress(Exception):
                    execution_state = await workflow_execution_state(client, workflow_id)
            return desc, wf_result, result_error, execution_state

        desc, wf_result, result_error, execution_state = asyncio.run(_trace())
        status_val = desc.status.name if desc.status else "UNKNOWN"
        task_result = _extract_first_result(wf_result) if isinstance(wf_result, dict) else None
        review = task_result.get("review", {}) if task_result else {}

        output_path = ""
        cached_log_path = _cached_log_path(workflow_id)
        if task_result:
            output_path = review.get("output_path", "") or task_result.get("output_path", "")

        lifecycle_jsonl_path = _cached_lifecycle_jsonl_path(workflow_id)
        lifecycle_events: list[dict[str, Any]] = []
        if lifecycle_jsonl_path:
            lifecycle_events = _read_lifecycle_events(lifecycle_jsonl_path)

        result_payload: dict[str, Any] = {
            "workflow_id": workflow_id,
            "status": status_val,
            "start_time": desc.start_time.isoformat() if desc.start_time else None,
            "close_time": desc.close_time.isoformat() if desc.close_time else None,
            "search_attributes": _search_attributes_dict(desc),
            "pending_activities": _pending_activity_details(desc),
            "execution_state": execution_state,
            "output_path": output_path,
            "cached_log_path": cached_log_path,
        }

        if lifecycle_jsonl_path and lifecycle_events:
            result_payload["lifecycle_log_path"] = lifecycle_jsonl_path
            result_payload["lifecycle_events"] = lifecycle_events

        if result_error:
            result_payload["temporal_result_error"] = result_error

        if task_result:
            result_payload["result_summary"] = {
                "success": task_result.get("success"),
                "exit_code": task_result.get("exit_code"),
                "provider": task_result.get("provider"),
                "mode": task_result.get("mode"),
                "merged": task_result.get("merged"),
                "branch_name": task_result.get("branch_name", ""),
                "pr_url": task_result.get("pr_url", ""),
                "pr_number": task_result.get("pr_number", 0),
                "task_preview": task_result.get("task", "")[:120],
            }
            result_payload["review"] = {
                "verdict": review.get("verdict"),
                "approved": review.get("approved"),
                "flags": review.get("flags", []),
                "satisfaction": review.get("satisfaction"),
                "completion_evidence": review.get("completion_evidence"),
                "completion_dossier": review.get("completion_dossier"),
                "dossier_path": review.get("dossier_path", ""),
            }
            if review.get("verdict") not in _APPROVED_VERDICTS:
                result_payload["failure_reason"] = _build_failure_reason(task_result)

        if status_val == "RUNNING":
            active_logs = _active_log_entries(workflow_id)
            if active_logs:
                result_payload["active_logs"] = active_logs

        # Reuse status verdict overrides so trace matches the visible operator surface.
        vo = get_verdict_overrides()
        visible_status = {
            "success": task_result.get("success") if task_result else None,
            "verdict": review.get("verdict") if review else None,
        }
        if workflow_id in vo:
            visible_status["verdict"] = vo[workflow_id]
            result_payload["verdict_override"] = vo[workflow_id]
        operator_state = _operator_state(status_val, visible_status)
        result_payload["operator_state"] = operator_state
        result_payload["diagnosis"] = _trace_diagnosis(result_payload)
        primary_action = _trace_next_action(workflow_id, operator_state)
        result_payload["next_action"] = primary_action

        next_actions = [primary_action, _action(f"mtor status {workflow_id}", "Return lightweight status")]
        if primary_action["command"] != f"mtor logs {workflow_id}":
            next_actions.append(_action(f"mtor logs {workflow_id}", "Fetch preserved output"))

        _ok(cmd, result_payload, next_actions, version=VERSION)
    except Exception as exc:
        exc_str = str(exc)
        if "not found" in exc_str.lower() or "workflow_not_found" in exc_str.lower():
            sys.exit(
                _err(
                    cmd,
                    f"Workflow {workflow_id} not found",
                    "WORKFLOW_NOT_FOUND",
                    "Verify the workflow ID with: mtor riboseq",
                    [_action("mtor riboseq", "List recent workflows")],
                    exit_code=4,
                )
            )
        sys.exit(
            _err(
                cmd,
                exc_str,
                "TRACE_ERROR",
                "Check Temporal server health with: mtor tsc",
                [_action("mtor tsc", "Run health check")],
            )
        )


@app.command
def wait(
    workflow_id: str,
    timeout: int = 1800,
    interval: int = 5,
) -> None:
    """Block until the workflow leaves RUNNING. Returns final state as porin envelope.

    Args:
        workflow_id: Workflow to wait for.
        timeout: Max seconds before giving up. Default 1800 (30 min) — matches Temporal start_to_close.
        interval: Poll interval seconds. Bounded [2, 60].
    """
    cmd = f"mtor wait {workflow_id}"

    if interval < 2 or interval > 60:
        sys.exit(_err(cmd, f"interval must be 2-60s, got {interval}", "INVALID_INTERVAL", "Try --interval 5", []))

    client, err = _get_client()
    if err:
        sys.exit(
            _err(
                cmd,
                f"Cannot connect to Temporal at {TEMPORAL_HOST}: {err}",
                "TEMPORAL_UNREACHABLE",
                f"Start Temporal worker: ssh {WORKER_HOST} 'sudo systemctl start temporal-worker'",
                [_action("mtor tsc", "Run health check to diagnose connectivity")],
                exit_code=3,
            )
        )

    running_states = {"RUNNING", "CONTINUED_AS_NEW"}
    polls = 0
    start_wall = time.time()

    try:
        async def _wait_loop():
            nonlocal polls
            handle = client.get_workflow_handle(workflow_id)
            while True:
                desc = await handle.describe()
                polls += 1
                status_val = desc.status.name if desc.status else "UNKNOWN"
                if status_val not in running_states:
                    wf_result_local = None
                    if status_val == "COMPLETED":
                        try:
                            wf_result_local = await handle.result()
                        except Exception:
                            wf_result_local = None
                    return desc, status_val, False, wf_result_local
                if time.time() - start_wall >= timeout:
                    return desc, status_val, True, None
                await asyncio.sleep(interval)

        desc, final_status, timed_out, wf_result = asyncio.run(_wait_loop())
        waited = round(time.time() - start_wall, 1)

        result_payload: dict[str, Any] = {
            "workflow_id": workflow_id,
            "status": final_status,
            "start_time": desc.start_time.isoformat() if desc.start_time else None,
            "close_time": desc.close_time.isoformat() if desc.close_time else None,
            "waited_seconds": waited,
            "polls": polls,
            "timed_out": timed_out,
        }
        if wf_result and isinstance(wf_result, dict):
            task_result = _extract_first_result(wf_result)
            if task_result:
                result_payload["success"] = task_result.get("success")
                result_payload["exit_code"] = task_result.get("exit_code")
                result_payload["provider"] = task_result.get("provider")
                result_payload["verdict"] = task_result.get("review", {}).get("verdict")

        vo = get_verdict_overrides()
        if workflow_id in vo:
            result_payload["verdict"] = vo[workflow_id]

        if timed_out:
            sys.exit(_err(
                cmd,
                f"Wait exceeded {timeout}s — workflow still {final_status}",
                "WAIT_TIMEOUT",
                f"Workflow may still be running. Check with: mtor status {workflow_id}",
                [_action(f"mtor status {workflow_id}", "Re-check status"),
                 _action(f"mtor logs {workflow_id}", "Inspect output")],
                exit_code=5,
            ))

        _ok(
            cmd,
            result_payload,
            [_action(f"mtor logs {workflow_id}", "Fetch output")],
            version=VERSION,
        )
    except Exception as exc:
        exc_str = str(exc)
        if "not found" in exc_str.lower():
            sys.exit(_err(
                cmd,
                f"Workflow {workflow_id} not found",
                "WORKFLOW_NOT_FOUND",
                "Verify ID with: mtor riboseq",
                [_action("mtor riboseq", "List recent workflows")],
                exit_code=4,
            ))
        sys.exit(_err(cmd, exc_str, "WAIT_ERROR", "Check Temporal health: mtor tsc",
                      [_action("mtor tsc", "Run health check")]))


def _active_logs() -> None:
    """SSH to ganglion, find log files modified in last 5 minutes, show filename + last 3 lines."""
    cmd = "mtor logs --active"
    entries = _active_log_entries(strict=True)
    _ok(cmd, {"active_logs": entries, "count": len(entries)}, version=VERSION)


def _active_log_entries(
    workflow_id: str | None = None,
    *,
    strict: bool = False,
) -> list[dict[str, Any]]:
    """Return active worker log entries, optionally filtered to a workflow ID."""
    find_cmd = r"find ~/code/mtor/logs -name '*.log' -mmin -5 -printf '%T@ %p\n' | sort -rn"
    cmd = "mtor logs --active"
    try:
        result = subprocess.run(
            ["ssh", WORKER_HOST, find_cmd],
            capture_output=True,
            text=True,
            timeout=15,
        )
        if result.returncode != 0:
            if strict:
                sys.exit(
                    _err(cmd, result.stderr.strip() or "SSH find failed", "SSH_ERROR", f"Verify worker host: ssh {WORKER_HOST}", [])
                )
            return []
    except subprocess.TimeoutExpired:
        if strict:
            sys.exit(_err(cmd, f"SSH to {WORKER_HOST} timed out", "SSH_TIMEOUT", f"Check connectivity: ping {WORKER_HOST}", []))
        return []
    except FileNotFoundError:
        if strict:
            sys.exit(_err(cmd, "ssh binary not found", "SSH_NOT_FOUND", "Install openssh-client", []))
        return []

    entries: list[dict[str, Any]] = []
    for line in result.stdout.strip().splitlines():
        parts = line.strip().split(" ", 1)
        if len(parts) != 2:
            continue
        log_path = parts[1]
        fname = log_path.rsplit("/", 1)[-1] if "/" in log_path else log_path
        if workflow_id and workflow_id not in fname:
            continue
        try:
            tail = subprocess.run(
                ["ssh", WORKER_HOST, f"tail -3 {log_path}"],
                capture_output=True,
                text=True,
                timeout=10,
            )
            last_lines = tail.stdout.strip().splitlines() if tail.returncode == 0 else []
        except (subprocess.TimeoutExpired, OSError):
            last_lines = []
        entries.append({"filename": fname, "path": log_path, "last_lines": last_lines})

    return entries


@app.command
def logs(
    workflow_id: Annotated[str | None, Parameter(name=["workflow_id"])] = None,
    *,
    active: Annotated[bool, Parameter(name=["--active"])] = False,
    lines: Annotated[int, Parameter(name=["--lines", "-n"])] = LOG_TAIL_LINES,
) -> None:
    """Fetch workflow output from worker host. --active shows currently-writing logs."""
    if active:
        _active_logs()
        return

    if lines < 1 or lines > 1000:
        sys.exit(_err("mtor logs", f"lines must be 1-1000, got {lines}", "INVALID_LINES", "Try --lines 30", []))

    if workflow_id is None:
        sys.exit(
            _err(
                "mtor logs",
                "Missing workflow_id (or use --active)",
                "MISSING_ARGS",
                "Provide a workflow ID or use --active to show currently-writing logs",
                exit_code=2,
            )
        )

    cmd = f"mtor logs {workflow_id}"

    # Step 1: Query Temporal for the workflow result to get output_path
    log_path = ""
    client, client_err = _get_client()
    if client and not client_err:
        try:

            async def _get_output_path():
                handle = client.get_workflow_handle(workflow_id)
                desc = await handle.describe()
                if getattr(getattr(desc, "status", None), "name", None) == "RUNNING":
                    return ""
                wf_result = await handle.result()
                if isinstance(wf_result, dict):
                    task_result = _extract_first_result(wf_result)
                    if task_result:
                        return task_result.get("review", {}).get(
                            "output_path", ""
                        ) or task_result.get("output_path", "")
                return ""

            log_path = asyncio.run(_get_output_path())
        except Exception:
            pass

    # Step 2: If no output_path from result, fall back to local glob then SSH ls.
    # Rejected workflows often have no review.output_path but do have
    # ~/code/mtor/logs/<workflow_id>.log from the worker tee.
    if not log_path:
        wf_suffix = workflow_id.rsplit("-", 1)[-1] if "-" in workflow_id else workflow_id
        # Try local directory first
        for base_dir, patterns in (
            (Path(OUTPUTS_DIR), ("*.txt",)),
            (Path(WORKER_LOG_DIR), (f"{workflow_id}.log", f"{workflow_id}.jsonl")),
        ):
            if log_path or not base_dir.exists():
                continue
            candidates = []
            for pattern in patterns:
                candidates.extend(base_dir.glob(pattern))
            for candidate in sorted(candidates, key=lambda p: p.stat().st_mtime, reverse=True)[:40]:
                if workflow_id in candidate.name or wf_suffix in candidate.name:
                    log_path = str(candidate)
                    break
        # Fall back to SSH
        if not log_path:
            try:
                find_result = subprocess.run(
                    [
                        "ssh",
                        WORKER_HOST,
                        (
                            f"find {OUTPUTS_DIR} {WORKER_LOG_DIR} -maxdepth 1 "
                            f"\\( -name '*.txt' -o -name '{workflow_id}.log' -o "
                            f"-name '{workflow_id}.jsonl' \\) -type f "
                            f"-printf '%T@ %p\\n' 2>/dev/null | sort -rn | head -40 | cut -d' ' -f2-"
                        ),
                    ],
                    capture_output=True,
                    text=True,
                    timeout=15,
                )
                if find_result.returncode == 0:
                    for line in find_result.stdout.strip().splitlines():
                        candidate = line.strip()
                        fname = candidate.rsplit("/", 1)[-1]
                        if workflow_id in fname or wf_suffix in fname:
                            log_path = candidate
                            break
            except (subprocess.TimeoutExpired, OSError):
                pass

    if not log_path:
        sys.exit(
            _err(
                cmd,
                f"No log file found for workflow {workflow_id}",
                "LOG_NOT_FOUND",
                f"Trace workflow diagnostics with: mtor trace {workflow_id}",
                [_action(f"mtor trace {workflow_id}", "Inspect Temporal and review evidence")],
                exit_code=4,
            )
        )

    # Try local file first (avoids SSH round-trip); fetch + cache if missing.
    # Remote worker paths such as /home/vivesca/... are not valid local cache
    # roots unless this CLI is itself running as that worker home.
    local_path = Path(log_path)
    worker_path_is_local = (
        local_path.is_absolute()
        and str(local_path).startswith("/home/vivesca/")
        and Path.home() == Path("/home/vivesca")
    )
    if (
        local_path.is_absolute()
        and str(local_path).startswith("/home/vivesca/")
        and not worker_path_is_local
    ):
        local_path = Path.home() / ".cache" / "mtor" / "logs" / Path(log_path).name
    elif not local_path.is_absolute():
        local_path = Path.home() / log_path.lstrip("~/")
    if not local_path.exists():
        # Fetch single file from worker host into local mirror
        local_path = Path.home() / ".cache" / "mtor" / "logs" / Path(log_path).name
        local_path.parent.mkdir(parents=True, exist_ok=True)
        with contextlib.suppress(Exception):
            subprocess.run(
                ["scp", f"{WORKER_HOST}:{log_path}", str(local_path)],
                capture_output=True,
                timeout=15,
            )
    if local_path.exists():
        log_lines = local_path.read_text().splitlines()[-lines:]
        _ok(
            cmd,
            {
                "lines": log_lines,
                "log_path": str(local_path),
                "truncated": len(log_lines) == lines,
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
            ["ssh", WORKER_HOST, f"tail -{lines} {log_path}"],
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
                        f"Trace workflow diagnostics with: mtor trace {workflow_id}",
                        [_action(f"mtor trace {workflow_id}", "Inspect Temporal and review evidence")],
                        exit_code=4,
                    )
                )
            sys.exit(
                _err(
                    cmd,
                    f"SSH command failed: {stderr_msg}",
                    "SSH_ERROR",
                    f"Verify worker host is reachable: ping {WORKER_HOST}",
                    [_action("mtor tsc", "Run health check")],
                )
            )

        log_lines = result.stdout.splitlines()
        _ok(
            cmd,
            {
                "lines": log_lines,
                "log_path": log_path,
                "truncated": len(log_lines) == lines,
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
                [_action("mtor tsc", "Run health check")],
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


@app.command(name="batch-cancel")
def batch_cancel(
    *,
    query: str,
    reason: str = "batch cleanup via mtor",
) -> None:
    """Cancel all workflows matching a Temporal visibility query.

    Example: mtor batch-cancel --query "ExecutionStatus = 'Running' AND StartTime < '2026-04-06'"
    """
    client, err = _get_client()
    if err:
        sys.exit(
            _err(
                "mtor batch-cancel",
                f"Cannot connect: {err}",
                "TEMPORAL_UNREACHABLE",
                "mtor doctor",
                exit_code=3,
            )
        )

    async def _do_batch():
        count = 0
        cancelled = 0
        async for wf in client.list_workflows(query=query):
            count += 1
            with contextlib.suppress(Exception):
                handle = client.get_workflow_handle(wf.id)
                await handle.cancel()
                cancelled += 1
        return count, cancelled

    total, done = asyncio.run(_do_batch())
    _ok(
        "mtor batch-cancel",
        {"query": query, "matched": total, "cancelled": done, "reason": reason},
        version=VERSION,
    )


def _reap_worker_processes(workflow_id: str) -> dict[str, Any]:
    """Terminate worker-side ribosome/Claude processes for one workflow ID."""
    empty = {
        "terminated_pids": [],
        "killed_pids": [],
        "remaining_pids": [],
    }
    if not workflow_id.strip():
        return {"attempted": False, "ok": False, **empty, "error": "workflow_id is empty"}

    script = r"""
import json
import os
import signal
import subprocess
import sys
import time

workflow_id = sys.argv[1]
own_pids = {os.getpid(), os.getppid()}
proc = subprocess.run(["ps", "-eo", "pid,args"], capture_output=True, text=True, check=False)
pids = []
for line in proc.stdout.splitlines()[1:]:
    parts = line.split(None, 1)
    if len(parts) < 2:
        continue
    pid_s, args = parts
    try:
        pid = int(pid_s)
    except ValueError:
        continue
    if pid in own_pids or workflow_id not in args:
        continue
    low = args.lower()
    if "effectors/ribosome" not in low and "claude" not in low:
        continue
    pids.append(pid)

terminated = []
killed = []
for pid in pids:
    try:
        os.kill(pid, signal.SIGTERM)
        terminated.append(pid)
    except (ProcessLookupError, PermissionError, OSError):
        pass

time.sleep(0.3)
for pid in terminated:
    try:
        os.kill(pid, 0)
    except (ProcessLookupError, PermissionError, OSError):
        continue
    try:
        os.kill(pid, signal.SIGKILL)
        killed.append(pid)
    except (ProcessLookupError, PermissionError, OSError):
        pass

time.sleep(0.1)
remaining = []
for pid in terminated:
    try:
        os.kill(pid, 0)
    except (ProcessLookupError, PermissionError, OSError):
        continue
    remaining.append(pid)

print(json.dumps({
    "terminated_pids": terminated,
    "killed_pids": killed,
    "remaining_pids": remaining,
}))
"""
    try:
        remote_cmd = f"python3 -c {shlex.quote(script)} {shlex.quote(workflow_id)}"
        result = subprocess.run(
            ["ssh", WORKER_HOST, remote_cmd],
            capture_output=True,
            text=True,
            timeout=15,
        )
        if result.returncode == 0 and result.stdout.strip():
            parsed = json.loads(result.stdout.strip())
            remaining = parsed.get("remaining_pids", [])
            return {
                "attempted": True,
                "ok": len(remaining) == 0,
                "terminated_pids": parsed.get("terminated_pids", []),
                "killed_pids": parsed.get("killed_pids", []),
                "remaining_pids": remaining,
            }
        return {
            "attempted": True,
            "ok": False,
            **empty,
            "error": result.stderr.strip() or f"SSH exit {result.returncode}",
        }
    except subprocess.TimeoutExpired:
        return {"attempted": True, "ok": False, **empty, "error": "SSH timed out"}
    except Exception as exc:
        return {"attempted": True, "ok": False, **empty, "error": str(exc)}

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
                [_action("mtor tsc", "Run health check to diagnose connectivity")],
                exit_code=3,
            )
        )

    try:

        async def _do_terminate():
            handle = client.get_workflow_handle(workflow_id)
            await handle.terminate(reason="Terminated via mtor CLI")

        asyncio.run(_do_terminate())
        process_cleanup = _reap_worker_processes(workflow_id)
        _ok(
            cmd,
            {"workflow_id": workflow_id, "terminated": True, "process_cleanup": process_cleanup},
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
                    "Verify the workflow ID with: mtor riboseq",
                    [_action("mtor riboseq", "List all recent workflows")],
                    exit_code=4,
                )
            )
        # Already terminated/cancelled — idempotent success
        if any(
            phrase in exc_str.lower()
            for phrase in ["already", "terminated", "cancelled", "canceled", "completed"]
        ):
            process_cleanup = _reap_worker_processes(workflow_id)
            _ok(
                cmd,
                {
                    "workflow_id": workflow_id,
                    "terminated": True,
                    "note": "Workflow was already in terminal state",
                    "process_cleanup": process_cleanup,
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
                "Check Temporal server health with: mtor tsc",
                [_action("mtor tsc", "Run health check")],
            )
        )


@app.command(name="doctor", show=False)
@app.command(name="tsc")
def tsc(
    *,
    reconcile: Annotated[bool, Parameter(name=["--reconcile"])] = False,
) -> None:
    """Health check: Temporal reachability, worker liveness, provider info."""
    _doctor(reconcile=reconcile)


@app.command
def history(
    *,
    count: int = 20,
) -> None:
    """Show recent ribosome run history from JSONL log."""
    import json as _json

    original_repo_dir = Path(os.environ.get("HOME", str(Path.home()))) / "germline"
    if "MTOR_REPO_DIR" in os.environ:
        repo_dir = Path(os.environ["MTOR_REPO_DIR"])
    elif Path(REPO_DIR) == original_repo_dir:
        repo_dir = Path.home() / "germline"
    else:
        repo_dir = Path(REPO_DIR)
    log_path = repo_dir / "loci" / "ribosome-runs.jsonl"
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
def audit(
    *,
    runs: Annotated[Path, Parameter(name=["--runs"])] = Path("~/germline/loci/ribosome-runs.jsonl"),
    reviews: Annotated[Path, Parameter(name=["--reviews"])] = Path("~/germline/loci/ribosome-reviews.jsonl"),
    logs_dir: Annotated[Path, Parameter(name=["--logs-dir"])] = Path("~/code/mtor/logs"),
    limit: Annotated[int, Parameter(name=["--limit"])] = 10,
) -> None:
    """Summarize ribosome run/review ledgers without external services."""
    from mtor.audit import summarize_audit

    result = summarize_audit(
        runs.expanduser(),
        reviews.expanduser(),
        logs_dir.expanduser(),
        limit=limit,
    )
    _ok("mtor audit", result, version=VERSION)


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
def receptor(
    prompt: str | None = None,
    *,
    spec: Annotated[Path | None, Parameter(name=["--spec"])] = None,
    provider: Annotated[str, Parameter(name=["-p", "--provider"])] = "zhipu",
    skip_sha_check: Annotated[bool, Parameter(name=["--skip-sha-check"])] = False,
    harness: Annotated[str, Parameter(name=["--harness"])] = "",
    explain: Annotated[bool, Parameter(name=["--explain"])] = False,
) -> None:
    """Dispatch protected receptor/skill work through mTOR's stricter route."""
    cmd = "mtor receptor"
    if spec is None:
        sys.exit(
            _err(
                cmd,
                "Receptor dispatch requires --spec",
                "SPEC_REQUIRED",
                "Create a spec with scope under membrane/receptors/ and a tests: run command.",
                [_action("mtor spec new <name>", "Scaffold a spec")],
                exit_code=2,
            )
        )

    import re as _re

    frontmatter_errors = validate_spec(spec)
    if frontmatter_errors:
        msg = "Spec validation failed:\n" + "\n".join(f"  - {e}" for e in frontmatter_errors)
        sys.exit(_err(cmd, msg, "SPEC_INVALID", "Fix the spec and retry.", [], exit_code=1))

    from mtor.dispatch import validate_receptor_spec
    from mtor.dispatch import validate_spec as validate_dispatch_spec
    from mtor.rptor import parse_spec

    spec_data = parse_spec(spec)
    repo = Path(spec_data.get("repo", ".")).expanduser()
    dispatch_errors = validate_dispatch_spec(spec, repo)
    receptor_errors = validate_receptor_spec(spec)
    if dispatch_errors or receptor_errors:
        errors = dispatch_errors + receptor_errors
        msg = "Receptor spec validation failed:\n" + "\n".join(f"  - {e}" for e in errors)
        sys.exit(
            _err(
                cmd,
                msg,
                "RECEPTOR_SPEC_INVALID",
                "Use scope under membrane/receptors/ and include a populated tests: field.",
                [],
                exit_code=1,
            )
        )

    spec_contents = spec.read_text(encoding="utf-8").strip()
    spec_contents = _re.sub(
        r"\A---\n.*?\n---\n*", "", spec_contents, count=1, flags=_re.DOTALL
    ).strip()
    dispatch_prompt = spec_contents if prompt is None else spec_contents + "\n" + prompt

    if harness and harness not in PROVIDER_HARNESS_MAP:
        allowed = ", ".join(sorted(PROVIDER_HARNESS_MAP))
        sys.exit(
            _err(
                cmd,
                f"Unknown harness '{harness}'",
                "UNKNOWN_HARNESS",
                f"Use one of: {allowed}",
                [],
                exit_code=2,
            )
        )

    if explain:
        from mtor.dispatch import _dispatch_explanation

        plan = _dispatch_explanation(
            dispatch_prompt,
            provider=provider,
            mode="receptor",
            skip_sha_check=skip_sha_check,
            spec_path=spec,
            harness=harness,
            paused=_is_paused(),
            frozen=_is_frozen(),
        )
        _ok(cmd, plan, plan["next_actions"], version=VERSION)
        return

    if _is_frozen():
        sys.exit(
            _err(
                cmd,
                "Dispatching is frozen. Use 'mtor dedeptor' to unfreeze.",
                "FROZEN",
                "Run: mtor dedeptor",
                [_action("mtor dedeptor", "Unfreeze dispatching")],
                exit_code=1,
            )
        )
    if _is_paused():
        sys.exit(
            _err(
                cmd,
                "Dispatching is paused. Use 'mtor derapa' to resume.",
                "PAUSED",
                "Run: mtor derapa",
                [_action("mtor derapa", "Resume dispatching")],
                exit_code=1,
            )
        )

    _dispatch_prompt(
        dispatch_prompt,
        provider=provider,
        mode="receptor",
        skip_sha_check=skip_sha_check,
        spec_path=spec,
        harness=harness,
    )


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


@app.command(name="reactivate")
def reactivate(workflow_id: str) -> None:
    """Send reactivation signal to a dormant workflow."""
    cmd = f"mtor reactivate {workflow_id}"

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
                    "Verify the workflow ID with: mtor riboseq",
                    [_action("mtor riboseq", "List all recent workflows")],
                    exit_code=4,
                )
            )
        sys.exit(
            _err(
                cmd,
                exc_str,
                "REACTIVATE_ERROR",
                "Check Temporal server health with: mtor tsc",
                [_action("mtor tsc", "Run health check")],
            )
        )


@app.command
def release(
    *,
    minor: Annotated[bool, Parameter(name=["--minor"])] = False,
    major: Annotated[bool, Parameter(name=["--major"])] = False,
) -> None:
    """Bump version, tag, and publish a release to PyPI."""
    cmd = "mtor release"
    if minor:
        cmd += " --minor"
    if major:
        cmd += " --major"

    if minor and major:
        sys.exit(
            _err(
                cmd,
                "Choose at most one version bump flag.",
                "INVALID_BUMP",
                "Use the default patch bump, or pass exactly one of --minor / --major.",
            )
        )

    bump: Literal["patch", "minor", "major"] = "patch"
    if major:
        bump = "major"
    elif minor:
        bump = "minor"

    repo_root = Path.cwd()
    pyproject_path = repo_root / "pyproject.toml"
    init_path = repo_root / "mtor" / "__init__.py"

    if not pyproject_path.exists() or not init_path.exists():
        sys.exit(
            _err(
                cmd,
                "Release must run from the mtor repository root.",
                "REPO_ROOT_REQUIRED",
                "cd ~/code/mtor and retry.",
            )
        )

    status_result = subprocess.run(
        ["git", "status", "--porcelain"],
        cwd=repo_root,
        capture_output=True,
        text=True,
        check=True,
    )
    if status_result.stdout.strip():
        sys.exit(
            _err(
                cmd,
                "Refusing to release from a dirty working tree.",
                "DIRTY_REPO",
                "Commit or stash local changes, then retry.",
            )
        )

    try:
        current_version = _read_release_version(pyproject_path, init_path)
        new_version = _bump_semver(current_version, bump)
        _write_release_version(pyproject_path, init_path, new_version)
    except ValueError as exc:
        sys.exit(_err(cmd, str(exc), "VERSION_PARSE_ERROR", "Check release version metadata."))

    tag_name = f"v{new_version}"
    try:
        subprocess.run(
            ["git", "add", "pyproject.toml", "mtor/__init__.py"],
            cwd=repo_root,
            check=True,
        )
        subprocess.run(
            ["git", "commit", "-m", f"chore: release {tag_name}"],
            cwd=repo_root,
            check=True,
        )
        subprocess.run(["git", "tag", tag_name], cwd=repo_root, check=True)
        subprocess.run(["git", "push", "origin", tag_name], cwd=repo_root, check=True)
        subprocess.run(["uv", "build"], cwd=repo_root, check=True)
        subprocess.run(["uv", "publish"], cwd=repo_root, check=True)
    except subprocess.CalledProcessError as exc:
        sys.exit(
            _err(
                cmd,
                f"Release command failed: {exc}",
                "RELEASE_FAILED",
                "Check the command output, fix the failure, and retry.",
            )
        )

    _ok(
        cmd,
        {
            "version": new_version,
            "tag": tag_name,
            "published": True,
            "next_step": "ganglion: uv tool upgrade mtor",
        },
        version=new_version,
    )


@app.command(show=False)
def publish(
    *,
    bump: Annotated[Literal["patch", "minor", "major"], Parameter(name=["-b", "--bump"])] = "patch",
) -> None:
    """Compatibility alias for the release workflow."""
    release(minor=bump == "minor", major=bump == "major")


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


@app.command(name="polysome", show=False)
def polysome() -> None:
    """Alias for stats command (polysome is multiple ribosomes = multiple stats)."""
    return stats()

@app.command
def stats() -> None:
    """Show dispatch statistics: today's verdicts, running count, weekly totals."""
    from datetime import datetime, timedelta, timezone

    client, err = _get_client()
    if err:
        sys.exit(_err("mtor stats", f"Cannot connect: {err}", "TEMPORAL_UNREACHABLE", "mtor doctor", exit_code=3))

    today = datetime.now(timezone.utc).strftime("%Y-%m-%dT00:00:00Z")
    week_ago = (datetime.now(timezone.utc) - timedelta(days=7)).strftime("%Y-%m-%dT%H:%M:%SZ")

    async def _count(query: str) -> int:
        result = await client.count_workflows(query=query)
        return int(getattr(result, "count", result))

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

    cp_dir = Path.home() / ".local" / "share" / "vivesca" / "ribosome-checkpoints"
    files: list[tuple[str, str]] = []  # (filename, file_content)

    if cp_dir.is_dir():
        for f in sorted(cp_dir.glob("*.json"), reverse=True):
            files.append((f.name, f.read_text()))
    else:
        # Fallback: read from worker via ssh
        remote_base = "$HOME/.local/share/vivesca/ribosome-checkpoints"
        ls_result = subprocess.run(
            ["ssh", WORKER_HOST, f"ls -1 {remote_base}/*.json 2>/dev/null"],
            capture_output=True, text=True, timeout=15,
        )
        if ls_result.returncode == 0 and ls_result.stdout.strip():
            for remote_path in ls_result.stdout.strip().splitlines():
                fname = remote_path.rsplit("/", 1)[-1]
                cat_result = subprocess.run(
                    ["ssh", WORKER_HOST, f"cat {remote_path}"],
                    capture_output=True, text=True, timeout=15,
                )
                if cat_result.returncode == 0:
                    files.append((fname, cat_result.stdout))

    cps: list[dict] = []
    malformed_count = 0
    malformed_files: list[str] = []
    for fname, content in files:
        try:
            cps.append(_json.loads(content))
        except (json.JSONDecodeError, ValueError):
            malformed_count += 1
            malformed_files.append(fname)

    result: dict[str, Any] = {
        "checkpoints": cps,
        "count": len(cps),
    }
    if malformed_count:
        result["malformed_count"] = malformed_count
        result["malformed_files"] = malformed_files

    _ok("mtor checkpoints", result, version=VERSION)


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
                    "mtor tsc",
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
            [_action("mtor riboseq", "View updated list")],
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
        [_action("mtor riboseq", "View updated list")],
        version=VERSION,
    )


@app.command
def verdict(
    workflow_id: str | None = None,
    *,
    new_verdict: Annotated[str, Parameter(name=["--set"])] = "false_positive",
    all_rejected: Annotated[bool, Parameter(name=["--all-rejected"])] = False,
) -> None:
    """Override verdict for completed workflows. Stored locally in triage.json."""
    cmd = "mtor verdict"

    if all_rejected:
        # Bulk: find all rejected workflows and override
        client, err = _get_client()
        if err:
            sys.exit(_err(cmd, f"Cannot connect: {err}", "TEMPORAL_UNREACHABLE", "mtor tsc", exit_code=3))

        async def _list_rejected():
            results = []
            async for execution in client.list_workflows(query=None):
                sa_verdict = "—"
                with contextlib.suppress(Exception):
                    sa = getattr(execution, "search_attributes", None)
                    if sa:
                        for key, val in sa.items():
                            if "verdict" in str(key).lower() and val:
                                sa_verdict = str(val[0])
                if sa_verdict == "rejected":
                    results.append(execution.id)
            return results

        rejected_ids = asyncio.run(_list_rejected())
        # Exclude already-overridden
        existing = get_verdict_overrides()
        to_override = [wid for wid in rejected_ids if wid not in existing]
        if not to_override:
            _ok(cmd + " --all-rejected", {"overridden": 0, "message": "No rejected workflows to override"}, [], version=VERSION)
            return
        result = override_verdict(to_override, new_verdict)
        _ok(
            cmd + " --all-rejected",
            result,
            [_action("mtor riboseq", "View updated verdicts")],
            version=VERSION,
        )
        return

    if workflow_id is None:
        sys.exit(_err(cmd, "Missing workflow_id or --all-rejected", "MISSING_ARGS", "Provide a workflow ID or use --all-rejected"))

    result = override_verdict([workflow_id], new_verdict)
    _ok(
        f"mtor verdict {workflow_id}",
        result,
        [_action("mtor riboseq", "View updated list")],
        version=VERSION,
    )


@app.command
def archive(
    workflow_id: Annotated[str | None, Parameter(name=["workflow_id"])] = None,
    *,
    before: Annotated[str | None, Parameter(name=["--before"])] = None,
    all_reviewed: Annotated[bool, Parameter(name=["--all-reviewed"])] = False,
    reason: Annotated[str | None, Parameter(name=["--reason"])] = None,
    force: Annotated[bool, Parameter(name=["--force"])] = False,
) -> None:
    """Archive task(s) — hide from default list."""
    if not force and not reason:
        sys.exit(
            _err(
                "mtor archive",
                "Review the diff first, then archive with --reason 'your assessment'",
                "ARCHIVE_REQUIRES_REASON",
                "Review the diff first, then archive with --reason 'your assessment'",
                [_action("mtor logs <workflow_id>", "Review workflow output")],
                exit_code=2,
            )
        )

    archive_reason = reason if reason else "force"

    if all_reviewed:
        triage = load_triage()
        ids_to_archive = list(triage.get("reviewed", []))
        result = archive_ids(ids_to_archive, reason=archive_reason)
        _ok(
            "mtor archive --all-reviewed",
            result,
            [_action("mtor riboseq", "View updated list")],
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
                    "mtor tsc",
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

        result = archive_ids(ids_to_archive, reason=archive_reason)
        _ok(
            f"mtor archive --before {before}",
            result,
            [_action("mtor riboseq", "View updated list")],
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

    result = archive_ids([workflow_id], reason=archive_reason)
    _ok(
        f"mtor archive {workflow_id}",
        result,
        [_action("mtor riboseq", "View updated list")],
        version=VERSION,
    )


@spec_app.command(name="new")
def spec_new(
    name: str,
    *,
    dir: Annotated[Path, Parameter(name=["--dir"])] = DEFAULT_SPEC_DIR,
) -> None:
    """Create a plan spec with the canonical frontmatter template."""
    import os
    import shlex

    out_path = (dir.expanduser() / name).with_suffix(".md")
    try:
        created_path = scaffold_spec(
            name=name,
            path=out_path,
            repo="~/code/mtor",
            scope=["mtor"],
            template="plan",
        )
    except FileExistsError as exc:
        sys.exit(
            _err(
                f"mtor spec new {name}",
                str(exc),
                "SPEC_EXISTS",
                f"Remove or rename {out_path} before scaffolding",
                [_action(f"ls -la {out_path}", "Inspect existing file")],
                exit_code=1,
            )
        )

    print(created_path)
    editor = os.environ.get("EDITOR")
    if editor:
        subprocess.run([*shlex.split(editor), str(created_path)], check=False)


@spec_app.command(name="validate")
def spec_validate(path: Path | None = None) -> None:
    """Validate one spec or every spec in the plan directory."""
    errors = validate_spec(path)
    for error in errors:
        print(error)
    if errors:
        sys.exit(1)


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


@app.command(name="plan", show=False)
@app.command(name="rptor")
def rptor(
    *,
    dir: Annotated[Path, Parameter(name=["--dir"])] = Path("~/epigenome/chromatin/loci/plans/"),
    pending: Annotated[bool, Parameter(name=["--pending"])] = False,
    audit: Annotated[bool, Parameter(name=["--audit"])] = False,
    strict: Annotated[bool, Parameter(name=["--strict"])] = False,
) -> None:
    """Display spec DAG — status, dependencies, and dispatchability."""
    cmd = "mtor rptor"
    directory = dir.expanduser()

    specs = scan_specs(directory)

    if audit:
        result = {**audit_specs(specs), "directory": str(directory)}
        if strict and not result["ok"]:
            sys.exit(
                _err(
                    cmd,
                    "Spec audit failed",
                    "SPEC_AUDIT_FAILED",
                    "Fix invalid statuses or add completion evidence, then retry.",
                    [_action(f"mtor rptor --audit --dir {directory}", "Inspect audit issues")],
                    exit_code=1,
                )
            )
        _ok(cmd, result, version=VERSION)
        return

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
                [_action("mtor rptor", "Re-run after fixing the cycle")],
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


@app.command(name="plan_done", show=False)
@app.command(name="rptor_done")
def rptor_done(
    name: str,
    *,
    dir: Annotated[Path, Parameter(name=["--dir"])] = Path("~/epigenome/chromatin/loci/plans/"),
) -> None:
    """Mark a spec as done."""
    cmd = f"mtor rptor done {name}"
    directory = dir.expanduser()

    spec_file = directory / f"{name}.md"
    if not spec_file.is_file():
        sys.exit(
            _err(
                cmd,
                f"Spec not found: {name}",
                "SPEC_NOT_FOUND",
                f"List specs: mtor rptor --dir {directory}",
                exit_code=1,
            )
        )

    update_spec_status(spec_file, "done")

    _ok(cmd, {"name": name, "status": "done"}, version=VERSION)


@app.command(name="watch", show=False)
@app.command(name="ragulator")
def ragulator(
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
    cmd = f"mtor ragulator {action}"

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
        _ok(cmd, stats.to_dict(), [_action("mtor riboseq", "Check synced workflows")], version=VERSION)
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
                [_action("mtor tsc", "Run health check")],
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
            _action(f"mtor ragulator query -w {started_id}", "Query watch status"),
            _action(f"mtor ragulator stop -w {started_id}", "Stop watch workflow"),
        ],
        version=VERSION,
    )


def _stop_watch_workflow(cmd: str, workflow_id: str | None) -> None:
    """Stop a running WatchWorkflow via signal or termination."""
    if not workflow_id:
        # Try to find running watch workflows
        client, err = _get_client()
        if err:
            sys.exit(_err(cmd, f"Cannot connect: {err}", "TEMPORAL_UNREACHABLE", "mtor tsc", exit_code=3))

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
                            await handle.terminate(reason="Stopped via mtor ragulator stop")
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
        sys.exit(_err(cmd, f"Cannot connect: {err}", "TEMPORAL_UNREACHABLE", "mtor tsc", exit_code=3))

    async def _stop():
        handle = client.get_workflow_handle(workflow_id)
        try:
            await handle.signal("stop")
            return "signaled"
        except Exception:
            await handle.terminate(reason="Stopped via mtor ragulator stop")
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
        sys.exit(_err(cmd, f"Cannot connect: {err}", "TEMPORAL_UNREACHABLE", "mtor tsc", exit_code=3))

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


@app.command(name="rapa")
def rapa() -> None:
    """Pause dispatching — blocks new tasks and watch sync cycles (rapamycin)."""
    cmd = "mtor rapa"
    if _is_paused():
        _ok(cmd, {"status": "already_paused"}, version=VERSION)
        return
    path = _create_pause()
    _ok(
        cmd,
        {"status": "paused", "pause_file": str(path)},
        [_action("mtor derapa", "Resume dispatching")],
        version=VERSION,
    )


@app.command(name="derapa")
def derapa() -> None:
    """Resume dispatching — removes pause marker."""
    cmd = "mtor derapa"
    if not _is_paused():
        _ok(cmd, {"status": "already_running"}, version=VERSION)
        return
    was_paused = _remove_pause()
    _ok(
        cmd,
        {"status": "resumed", "was_paused": was_paused},
        [_action("mtor rapa", "Pause again if needed")],
        version=VERSION,
    )


@app.command(name="deptor")
def deptor() -> None:
    """Freeze all activity — blocks dispatch and watch sync (deptor lock)."""
    cmd = "mtor deptor"
    if _is_frozen():
        _ok(cmd, {"status": "already_frozen"}, version=VERSION)
        return
    path = _create_freeze()
    _ok(
        cmd,
        {"status": "frozen", "freeze_file": str(path)},
        [_action("mtor dedeptor", "Unfreeze dispatching")],
        version=VERSION,
    )


@app.command(name="dedeptor")
def dedeptor() -> None:
    """Unfreeze — resumes all dispatch and sync activity."""
    cmd = "mtor dedeptor"
    if not _is_frozen():
        _ok(cmd, {"status": "not_frozen"}, version=VERSION)
        return
    was_frozen = _remove_freeze()
    _ok(
        cmd,
        {"status": "thawed", "was_frozen": was_frozen},
        [_action("mtor deptor", "Freeze again if needed")],
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
        next_actions.append(_action("mtor riboseq", "Check synced workflows"))
    if result.error:
        next_actions.append(_action("mtor tsc", "Diagnose connectivity"))

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


_NON_DISPATCHABLE_AUDIT_STATUSES = frozenset({
    "already_satisfied",
    "not_outstanding_ready",
    "status_normalized_done",
    "audited_present",
})


def _select_dispatch_candidates(
    resolved: list[dict[str, Any]],
    repo: Path,
) -> tuple[list[dict[str, Any]], list[dict[str, str]]]:
    """Filter resolved specs into dispatch candidates and skipped entries.

    Skipped reasons:
      status:<status>  — spec is not ready or has unmet dependencies
      audit:<value>    — audit_status indicates spec is not outstanding
      invalid:<msg>    — dispatch validation failed (e.g. missing tests)

    Returns (candidates, skipped) where each skipped item has
    {name, path, reason}.
    """
    from mtor.dispatch import validate_spec as _validate_spec

    skipped: list[dict[str, str]] = []
    candidates: list[dict[str, Any]] = []

    for spec in resolved:
        if not spec.get("dispatchable"):
            status = spec.get("status", "ready")
            reason = "blocked" if status == "ready" else f"status:{status}"
            skipped.append({
                "name": spec["name"],
                "path": spec["path"],
                "reason": reason,
            })
            continue

        audit_status = spec.get("audit_status", "")
        if audit_status in _NON_DISPATCHABLE_AUDIT_STATUSES:
            skipped.append({
                "name": spec["name"],
                "path": spec["path"],
                "reason": f"audit:{audit_status}",
            })
            continue

        spec_path = Path(spec["path"])
        spec_errors = _validate_spec(spec_path, repo)
        if spec_errors:
            skipped.append({
                "name": spec["name"],
                "path": spec["path"],
                "reason": "invalid:" + "; ".join(spec_errors),
            })
            continue

        candidates.append(spec)

    return candidates, skipped


@app.command(name="dispatch-all")
def dispatch_all(
    *,
    dir: Annotated[Path, Parameter(name=["--dir"])] = Path("~/epigenome/chromatin/loci/plans/"),
    provider: Annotated[str, Parameter(name=["-p", "--provider"])] = "zhipu",
    dry_run: Annotated[bool, Parameter(name=["--dry-run"])] = False,
    limit: Annotated[int, Parameter(name=["--limit"])] = 0,
) -> None:
    """Dispatch all ready (dispatchable) specs from a plan directory."""
    import io as _io

    from mtor.dispatch import _inject_spec_constraints

    cmd = "mtor dispatch-all"
    directory = dir.expanduser()

    specs = scan_specs(directory)

    if not specs:
        _ok(cmd, {"dispatched": [], "count": 0, "skipped": [], "directory": str(directory)}, version=VERSION)
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
                [_action("mtor rptor", "View the DAG")],
                exit_code=1,
            )
        )

    _repo = Path.home() / "code" / "mtor"
    candidates, skipped = _select_dispatch_candidates(resolved, _repo)
    candidates = topological_sort(candidates)

    if limit > 0:
        candidates = candidates[:limit]

    if not candidates:
        _ok(
            cmd,
            {"dispatched": [], "count": 0, "skipped": skipped, "message": "No dispatchable specs", "directory": str(directory)},
            version=VERSION,
        )
        return

    dispatched: list[dict[str, Any]] = []
    errors: list[dict[str, str]] = []

    for spec in candidates:
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
        "skipped": skipped,
        "directory": str(directory),
    }
    if dry_run:
        result["dry_run"] = True

    _ok(cmd, result, version=VERSION)


# ---------------------------------------------------------------------------
# Rictor subcommand group (formerly infra)
# ---------------------------------------------------------------------------

rictor_app = App(name="rictor", help_flags=[], version_flags=[])
app.command(rictor_app)


@rictor_app.command
def check() -> None:
    """Infrastructure health check — worker SSH, repo, git, disk."""
    cmd = "mtor rictor check"
    report = _check_health()
    result = report.to_dict()
    next_actions = []
    if not report.ok:
        next_actions.append(_action("mtor tsc", "Full health check"))
        next_actions.append(_action("mtor rictor deploy", "Redeploy to fix issues"))
    _ok(cmd, result, next_actions, version=VERSION)


@rictor_app.command(name="deploy")
def rictor_deploy() -> None:
    """Sync code to worker, restart services, verify health."""
    cmd = "mtor rictor deploy"
    result = _deploy()
    payload = result.to_dict()
    next_actions = []
    if result.healthy:
        next_actions.append(_action("mtor rictor check", "Verify health after deploy"))
    else:
        next_actions.append(_action("mtor tsc", "Full health check"))
    _ok(cmd, payload, next_actions, version=VERSION)


@rictor_app.command
def clean(
    *,
    older_than_days: Annotated[int, Parameter(name=["--older-than-days"])] = 7,
) -> None:
    """Remove old output and checkpoint files."""
    cmd = "mtor rictor clean"
    result = _clean(older_than_days=older_than_days)
    _ok(cmd, result.to_dict(), version=VERSION)


@rictor_app.command(name="setup-search-attrs")
def setup_search_attrs() -> None:
    """Register custom search attributes on the Temporal server."""
    from mtor.infra import setup_search_attributes

    cmd = "mtor rictor setup-search-attrs"
    try:
        result = asyncio.run(setup_search_attributes())
        _ok(cmd, result, version=VERSION)
    except Exception as exc:
        sys.exit(_err(cmd, str(exc), "SETUP_ERROR", "Check Temporal server health"))


@app.command
def reconcile(
    *,
    dry_run: Annotated[bool, Parameter(name=["--dry-run"])] = False,
    dir: Annotated[Path, Parameter(name=["--dir"])] = Path("~/epigenome/chromatin/loci/plans/"),
) -> None:
    """Reconcile spec status with reality — fix stale frontmatter based on Temporal and git.

    Logic:
    - dispatched → ready if workflow gone or completed without commits
    - dispatched → done if workflow completed and commits exist on main
    - done → warn if listed files/functions not found in codebase
    - ready → report blocked if any dependency not done
    """
    from mtor.reconcile import reconcile_all

    cmd = "mtor reconcile"
    directory = dir.expanduser()

    if not directory.exists():
        sys.exit(
            _err(
                cmd,
                f"Spec directory not found: {directory}",
                "DIRECTORY_NOT_FOUND",
                "Check --dir path",
                exit_code=1,
            )
        )

    result = reconcile_all(directory, dry_run=dry_run)
    _ok(cmd, result, version=VERSION)
