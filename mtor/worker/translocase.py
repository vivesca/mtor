#!/usr/bin/env python3
"""Translocase -- Temporal worker (eEF2) for the polysome translation system.

Polls the 'translation-queue' task queue and executes ribosome commands as activities.

Usage:
    python translocase.py
    python translocase.py --help
"""

import asyncio
import contextlib
import json
import os
import random as _random
import re as _re
import signal as _signal
import subprocess as _subprocess
import sys
import time as _time
from datetime import timedelta
from pathlib import Path

from temporalio import activity
from temporalio.client import Client
from temporalio.exceptions import ApplicationError
from temporalio.worker import Worker

from mtor.worker.provider import (
    EXIT_RATE_LIMITED,
    PROVIDER_LIMITS,
    PROVIDER_PRIORITY,
    _active_count,
    load_health,
    parse_rate_limit_window,
    save_health,
    select_provider,
    update_health,
)
from mtor.worker.stall_trace import (
    create_task_trace,
    finalize_trace,
    record_stall_event,
)
from mtor.worker.chaperone_review import chaperone
from mtor.worker.git_ops import (
    _auto_commit,
    _checkpoint_worktree_or_raise,
    _cleanup_worktree,
    _create_pr_impl,
    _create_worktree,
    _detect_prior_commits,
    _detect_repo,
    _format_landing_banner,
    _gc_worktrees,
    _git_pull_ff_only,
    _git_snapshot,
    _main_checkout_state,
    _remove_worktree_if_inactive,
    _reap_landed_branches,
    _worktree_process_pids,
)

TASK_QUEUE = "translation-queue"
RIBOSOME_SCRIPT = Path.home() / "germline" / "effectors" / "ribosome"
OUTPUT_DIR = Path.home() / "germline" / "loci" / "ribosome-outputs"
LOG_DIR = Path.home() / "code" / "mtor" / "logs"


def _mode_allows_auto_commit(mode: str) -> bool:
    """Return whether a translation mode may commit generated code."""
    return mode not in {"scout", "research"}


def _persist_output_file(
    task: str,
    provider: str,
    rc: int,
    stdout: str,
    stderr: str,
    diff_stat: str = "",
    extra: str = "",
) -> str:
    """Write the run report to OUTPUT_DIR; return its path ('' on failure).

    For read-only modes the stdout report is the entire deliverable, so
    this must be callable from every return path of translate, including
    timeout and cancellation.
    """
    task_id_match = _re.search(r"\[t-([0-9a-fA-F]+)\]", task)
    tid_str = task_id_match.group(1) if task_id_match else _time.strftime("%H%M%S")
    try:
        OUTPUT_DIR.mkdir(parents=True, exist_ok=True)
        out_file = OUTPUT_DIR / f"{_time.strftime('%Y%m%d')}-{tid_str}.txt"
        out_text = (
            f"Task: {task}\nProvider: {provider}\nExit: {rc}\n\n"
            f"--- stdout ---\n{stdout}\n\n--- stderr ---\n{stderr}\n\n"
            f"--- diff ---\n{diff_stat}\n"
        )
        if extra:
            out_text += extra
        out_file.write_text(out_text)
        return str(out_file)
    except OSError:
        return ""


_MAX_BRANCH_LEN = 80
_BRANCH_PREFIX = "ribosome-"


def _derive_branch_name(workflow_id: str, tid_str: str) -> str:
    """Derive a unique branch name from workflow identity or fallback sources.

    When *workflow_id* is present, the branch is derived from its distinctive
    suffix so two workflows created in the same second never collide.  Falls
    back to *tid_str* (task ID from the prompt) and finally to a wall-clock
    timestamp.
    """
    if workflow_id:
        raw = f"{_BRANCH_PREFIX}{workflow_id}"
        if len(raw) > _MAX_BRANCH_LEN:
            keep = _MAX_BRANCH_LEN - len(_BRANCH_PREFIX)
            raw = f"{_BRANCH_PREFIX}{workflow_id[-keep:]}"
        return raw
    if tid_str:
        return f"{_BRANCH_PREFIX}{tid_str}"
    return f"{_BRANCH_PREFIX}{_time.strftime('%H%M%S')}"


# Rate-limit detection: patterns that signal 429/quota errors in provider output
_RATE_LIMIT_PATTERNS = _re.compile(
    r"429\b"
    r"|rate.?\s*limit"
    r"|rate.?\s*limited"
    r"|quota.?\s*(?:exceeded|exhausted|reached)"
    r"|resource.?\s*(?:exhausted|depleted)"
    r"|too.?\s*many.?\s*requests"
    r"|api.?\s*(?:rate|throttl)"
    r"|request.?\s*was.?\s*throttled"
    r"|requests.?\s*per.?\s*(?:minute|second)",
    _re.IGNORECASE,
)

# Auto-throttle configuration
_THROTTLE_BASE_SECONDS = 30.0
_THROTTLE_MAX_SECONDS = 300.0  # 5 minutes max wait
_THROTTLE_JITTER_FRACTION = 0.1  # ±10% jitter


def _detect_rate_limit_error(text: str) -> tuple[bool, float | None]:
    """Scan text for 429/quota error signals from provider APIs.

    Returns ``(is_rate_limited, suggested_wait_seconds)``.
    *suggested_wait_seconds* is ``None`` when no explicit wait time is found.
    """
    if not _RATE_LIMIT_PATTERNS.search(text):
        return (False, None)
    return (True, _extract_wait_seconds(text))


def _extract_wait_seconds(text: str) -> float | None:
    """Extract retry-after duration in seconds from error text.

    Handles patterns like ``Retry-After: 30``, ``retry after 60 seconds``,
    ``retry in 2 minutes``, ``cooldown: 1h``.
    """
    for pattern in (
        r"retry.?\s*after[:\s]+(\d+(?:\.\d+)?)\s*(s|sec|seconds?|m|min|minutes?|h|hours?)?",
        r"retry.?\s*in[:\s]+(\d+(?:\.\d+)?)\s*(s|sec|seconds?|m|min|minutes?|h|hours?)?",
        r"cooldown[:\s]+(\d+(?:\.\d+)?)\s*(s|sec|seconds?|m|min|minutes?|h|hours?)?",
        r"wait[:\s]+(\d+(?:\.\d+)?)\s*(s|sec|seconds?|m|min|minutes?|h|hours?)?",
    ):
        m = _re.search(pattern, text, _re.IGNORECASE)
        if m:
            value = float(m.group(1))
            unit = (m.group(2) or "s").lower()
            if unit.startswith("h"):
                return value * 3600
            if unit.startswith("m"):
                return value * 60
            return value
    return None


def _throttle_wait(attempt: int, suggested_seconds: float | None = None) -> float:
    """Calculate auto-throttle wait with exponential backoff + jitter.

    Uses *suggested_seconds* (e.g. from ``Retry-After`` header) when
    available, capped at ``_THROTTLE_MAX_SECONDS``.  Falls back to
    exponential backoff starting from ``_THROTTLE_BASE_SECONDS``.
    """
    if suggested_seconds is not None and suggested_seconds > 0:
        wait = min(suggested_seconds, _THROTTLE_MAX_SECONDS)
    else:
        wait = min(_THROTTLE_BASE_SECONDS * (2**attempt), _THROTTLE_MAX_SECONDS)

    jitter = wait * _THROTTLE_JITTER_FRACTION * (_random.random() * 2 - 1)
    return max(1.0, wait + jitter)


def _is_coaching_bloat_error(rc: int, stderr: str) -> bool:
    """Return True when ribosome failed before launch due to coaching size."""
    return (
        rc == 1 and "coaching file" in stderr.lower() and "limit 10kb" in stderr.lower()
    )


def _select_attempt_provider(
    health: dict,
    requested_provider: str,
    attempted: set[str],
) -> str:
    """Choose the provider for the next subprocess attempt."""
    if requested_provider and not attempted:
        return select_provider(health, requested_provider)
    if not attempted:
        return select_provider(health, override=None)

    available = [p for p in PROVIDER_PRIORITY if p not in attempted]
    if available:
        tmp_health = {p: health.get(p, {"state": "closed"}) for p in PROVIDER_PRIORITY}
        for p in attempted:
            tmp_health[p] = {"state": "open", "cooldown_until": _time.time() + 3600}
        return select_provider(tmp_health, override=None)
    return select_provider(health, override=None)


def _write_attempt_summary(
    *,
    workflow_id: str,
    provider: str,
    exit_code: int,
    duration_seconds: float,
    stdout_bytes: int,
    stderr_bytes: int,
    work_dir: str | Path,
) -> None:
    """Append one machine-readable summary row for a ribosome attempt."""
    diff_stat = {"added": 0, "removed": 0}
    try:
        numstat_r = _subprocess.run(
            ["git", "diff", "--numstat", "main..HEAD"],
            capture_output=True,
            text=True,
            timeout=10,
            cwd=work_dir,
        )
        if numstat_r.returncode == 0:
            for line in numstat_r.stdout.strip().splitlines():
                parts = line.split("\t")
                if len(parts) >= 2:
                    with contextlib.suppress(ValueError):
                        diff_stat["added"] += int(parts[0]) if parts[0] != "-" else 0
                        diff_stat["removed"] += int(parts[1]) if parts[1] != "-" else 0
    except Exception:
        pass

    summary = {
        "workflow_id": workflow_id,
        "provider": provider,
        "exit_code": exit_code,
        "duration_seconds": round(duration_seconds, 2),
        "diff_stat": diff_stat,
        "stdout_bytes": stdout_bytes,
        "stderr_bytes": stderr_bytes,
        "timestamp": _time.strftime("%Y-%m-%dT%H:%M:%S"),
    }
    LOG_DIR.mkdir(parents=True, exist_ok=True)
    with open(LOG_DIR / f"{workflow_id}.jsonl", "a") as f:
        f.write(json.dumps(summary) + "\n")


def _log_event(workflow_id: str, event_type: str, **fields) -> None:
    """Append one lifecycle event to the workflow's JSONL log."""
    LOG_DIR.mkdir(parents=True, exist_ok=True)
    entry = {
        "type": event_type,
        "workflow_id": workflow_id,
        "timestamp": _time.strftime("%Y-%m-%dT%H:%M:%S"),
        **fields,
    }
    log_file = LOG_DIR / f"{workflow_id}.jsonl"
    with open(log_file, "a") as f:
        f.write(json.dumps(entry) + "\n")


def _kill_process_group(proc: asyncio.subprocess.Process) -> None:
    """Kill the entire process group (ribosome + claude + children).

    Requires start_new_session=True on the subprocess.
    Falls back to proc.kill() if pgid lookup fails.
    """
    returncode = getattr(proc, "returncode", None)
    if isinstance(returncode, int):
        # The leader's numeric PGID can be recycled immediately after exit.
        # The verified completion reaper owns any surviving descendants.
        return
    if not _signal_group(proc.pid, _signal.SIGKILL):
        with contextlib.suppress(ProcessLookupError):
            proc.kill()


def _signal_group(pid: int, sig: int) -> bool:
    """Signal the process group led by *pid*; True if the signal was sent."""
    try:
        os.killpg(pid, sig)
        return True
    except (ProcessLookupError, PermissionError, OSError):
        return False


def _process_group_has_live_members(pgid: int) -> bool:
    """Return whether *pgid* still contains a non-zombie process."""
    try:
        result = _subprocess.run(
            ["ps", "-eo", "pgid=,stat="],
            capture_output=True,
            text=True,
            timeout=2,
        )
    except Exception:
        result = None
    if result is not None and result.returncode == 0:
        for line in result.stdout.splitlines():
            parts = line.split(None, 1)
            if len(parts) != 2:
                continue
            try:
                member_pgid = int(parts[0])
            except ValueError:
                continue
            if member_pgid == pgid and not parts[1].startswith("Z"):
                return True
        return False
    try:
        os.killpg(pgid, 0)
        return True
    except ProcessLookupError:
        return False
    except (PermissionError, OSError):
        return True


def _process_identity(pid: int) -> str | None:
    """Return a PID-reuse-safe start token for a live, non-zombie process."""
    proc_stat = Path(f"/proc/{pid}/stat")
    if proc_stat.exists():
        try:
            raw = proc_stat.read_text()
            fields = raw[raw.rfind(")") + 2 :].split()
            if fields[0].startswith("Z"):
                return None
            return f"linux:{fields[19]}"
        except (OSError, IndexError):
            return None
    try:
        result = _subprocess.run(
            ["ps", "-o", "lstart=", "-o", "stat=", "-p", str(pid)],
            capture_output=True,
            text=True,
            timeout=2,
        )
    except Exception:
        return None
    line = result.stdout.strip()
    if result.returncode != 0 or not line:
        return None
    tokens = line.split()
    status = tokens[-1]
    if status.startswith("Z"):
        return None
    return f"ps:{' '.join(tokens[:-1])}"


def _descendant_process_identities(root_pid: int) -> dict[int, str]:
    """Snapshot all current descendants of *root_pid* with start identities."""
    try:
        result = _subprocess.run(
            ["ps", "-eo", "pid=,ppid="],
            capture_output=True,
            text=True,
            timeout=2,
        )
    except Exception:
        return {}
    children: dict[int, list[int]] = {}
    for line in result.stdout.splitlines():
        parts = line.split()
        if len(parts) != 2:
            continue
        try:
            pid, ppid = int(parts[0]), int(parts[1])
        except ValueError:
            continue
        children.setdefault(ppid, []).append(pid)
    descendants: set[int] = set()
    pending = list(children.get(root_pid, []))
    while pending:
        pid = pending.pop()
        if pid in descendants:
            continue
        descendants.add(pid)
        pending.extend(children.get(pid, []))
    return {
        pid: identity
        for pid in descendants
        if (identity := _process_identity(pid)) is not None
    }


def _worktree_process_identities(worktree_path: str | None) -> dict[int, str]:
    """Find live processes whose cwd or open files are inside a worktree."""
    if not worktree_path:
        return {}
    pids = _worktree_process_pids(worktree_path)
    pids.discard(os.getpid())
    return {
        pid: identity
        for pid in pids
        if (identity := _process_identity(pid)) is not None
    }


def _attempt_process_identities(
    attempt_identity: str | None, proc_root: Path = Path("/proc")
) -> dict[int, str]:
    """Find Linux descendants by their per-activity inherited identity."""
    if not attempt_identity or not proc_root.is_dir():
        return {}
    marker = f"RIBOSOME_ATTEMPT_ID={attempt_identity}".encode()
    pids: set[int] = set()
    for pid_dir in proc_root.iterdir():
        if not pid_dir.name.isdigit():
            continue
        try:
            environment = (pid_dir / "environ").read_bytes().split(b"\0")
        except OSError:
            continue
        if marker in environment:
            pids.add(int(pid_dir.name))
    pids.discard(os.getpid())
    return {
        pid: identity
        for pid in pids
        if (identity := _process_identity(pid)) is not None
    }


async def _reap_detached_descendants(
    tracked: dict[int, str],
    worktree_path: str | None,
    attempt_identity: str | None,
    timeout: float = 2.0,
) -> bool:
    """Repeatedly kill descendants that escaped the original process group."""
    deadline = asyncio.get_running_loop().time() + timeout
    while True:
        worktree_processes = await asyncio.to_thread(
            _worktree_process_identities, worktree_path
        )
        attempt_processes = await asyncio.to_thread(
            _attempt_process_identities, attempt_identity
        )
        owned_candidates = {**tracked, **attempt_processes}
        live_owned = {
            pid: identity
            for pid, identity in owned_candidates.items()
            if await asyncio.to_thread(_process_identity, pid) == identity
        }
        live_worktree = {
            pid: identity
            for pid, identity in worktree_processes.items()
            if await asyncio.to_thread(_process_identity, pid) == identity
        }
        unowned_worktree = set(live_worktree) - set(live_owned)
        if not live_owned:
            # Worktree use is evidence of a conflict, not authority to kill an
            # editor, shell or indexer that does not carry this task's lineage
            # or identity. Stop our own tree first, then block cleanup while
            # leaving every unowned process untouched.
            return not unowned_worktree
        for pid in live_owned:
            with contextlib.suppress(ProcessLookupError, PermissionError):
                os.kill(pid, _signal.SIGKILL)
        if asyncio.get_running_loop().time() >= deadline:
            final_live = await asyncio.to_thread(
                lambda: {
                    pid
                    for pid, identity in live_owned.items()
                    if _process_identity(pid) == identity
                }
            )
            return not final_live and not unowned_worktree
        await asyncio.sleep(0.05)


async def _wait_for_process_group_exit(pgid: int, timeout: float = 2.0) -> bool:
    """Wait until a process group has no member capable of further mutation."""
    deadline = asyncio.get_running_loop().time() + timeout
    while await asyncio.to_thread(_process_group_has_live_members, pgid):
        if asyncio.get_running_loop().time() >= deadline:
            return False
        await asyncio.sleep(0.05)
    return True


def _signal_verified_processes(processes: dict[int, str], sig: int) -> set[int]:
    """Signal only PIDs whose start identity still matches the captured owner."""
    signalled: set[int] = set()
    for pid, identity in processes.items():
        if _process_identity(pid) != identity:
            continue
        try:
            os.kill(pid, sig)
        except (ProcessLookupError, PermissionError, OSError):
            continue
        signalled.add(pid)
    return signalled


def _reap_orphaned_worktree_processes(repo_root: str) -> dict:
    """Kill opencode/timeout process groups whose cwd is a deleted worktree.

    _graceful_kill only sweeps the process group on the timeout and
    cancellation branches of translate() — a task that exits normally never
    signals its group, so an opencode grandchild left in the wrapper's
    original pgid (start_new_session=True makes proc.pid the pgid) survives
    after _cleanup_worktree deletes the worktree dir. Its /proc/<pid>/cwd
    then resolves as "<path> (deleted)", which this sweep detects.
    """
    result: dict = {"scanned": 0, "killed_pgids": [], "errors": []}
    worktrees_root = os.path.join(repo_root, ".worktrees")
    try:
        ps = _subprocess.run(
            ["ps", "-eo", "pid,pgid,comm"],
            capture_output=True,
            text=True,
            timeout=10,
        )
    except Exception as exc:
        result["errors"].append(str(exc))
        return result
    seen_pgids: set = set()
    for line in ps.stdout.splitlines()[1:]:
        parts = line.split(None, 2)
        if len(parts) < 3:
            continue
        try:
            pid, pgid = int(parts[0]), int(parts[1])
        except ValueError:
            continue
        comm = parts[2]
        if "opencode" not in comm and "timeout" not in comm:
            continue
        result["scanned"] += 1
        try:
            cwd_link = os.readlink(f"/proc/{pid}/cwd")
        except OSError:
            continue
        if worktrees_root not in cwd_link:
            continue
        if "(deleted)" not in cwd_link and os.path.isdir(cwd_link):
            continue
        if pgid in seen_pgids:
            continue
        seen_pgids.add(pgid)
        if _signal_group(pgid, _signal.SIGKILL):
            result["killed_pgids"].append(pgid)
        else:
            result["errors"].append(f"killpg failed for pgid={pgid}")
    return result


async def _graceful_kill(
    proc: asyncio.subprocess.Process,
    timeout: float = 5.0,
) -> None:
    """Send SIGTERM then escalate to SIGKILL if the process doesn't exit.

    Signals the whole process group — the subprocess is spawned with
    start_new_session=True, so proc.pid is the pgid. Signalling only the
    wrapper leaves its `timeout ... opencode run` children alive as ghosts
    that keep editing the checkout after cancel (2026-07-04 incident).
    Falls back to single-process terminate/kill when killpg fails.
    """
    if proc.returncode is not None:
        return
    if not _signal_group(proc.pid, _signal.SIGTERM):
        with contextlib.suppress(ProcessLookupError):
            proc.terminate()
    try:
        await asyncio.wait_for(proc.wait(), timeout=timeout)
    except asyncio.TimeoutError:
        if not _signal_group(proc.pid, _signal.SIGKILL):
            with contextlib.suppress(ProcessLookupError):
                proc.kill()
        with contextlib.suppress(Exception):
            await asyncio.wait_for(proc.wait(), timeout=2.0)
    # The wrapper can exit on SIGTERM while detached children survive in the
    # group — sweep the group once more after the leader is gone.
    _signal_group(proc.pid, _signal.SIGKILL)


async def _graceful_kill_group(
    proc: asyncio.subprocess.Process,
    timeout: float = 5.0,
    worktree_path: str | None = None,
    attempt_identity: str | None = None,
) -> None:
    """Stop the owned task tree without signalling a recycled historical PGID."""
    tracked_descendants = await asyncio.to_thread(
        _descendant_process_identities, proc.pid
    )
    leader_identity = await asyncio.to_thread(_process_identity, proc.pid)
    if leader_identity is not None:
        tracked_descendants[proc.pid] = leader_identity
    if proc.returncode is None:
        await asyncio.to_thread(
            _signal_verified_processes,
            tracked_descendants,
            _signal.SIGTERM,
        )
        with contextlib.suppress(asyncio.TimeoutError):
            await asyncio.wait_for(proc.wait(), timeout=timeout)

    # Once the leader exits, its numeric PGID can be recycled. Reap only the
    # identity-snapshotted descendants and processes carrying this attempt's
    # inherited marker; never send SIGKILL to the bare historical PGID.
    if not await _reap_detached_descendants(
        tracked_descendants, worktree_path, attempt_identity
    ):
        raise RuntimeError(f"detached descendants of {proc.pid} survived SIGKILL")
    if proc.returncode is None:
        with contextlib.suppress(Exception):
            await asyncio.wait_for(proc.wait(), timeout=2.0)
    if not await _wait_for_process_group_exit(proc.pid):
        raise RuntimeError(f"process group {proc.pid} survived verified reaping")


async def _checkpoint_shutdown_worktree(
    worktree_path: str | None,
    workflow_id: str,
    *,
    task: str,
    provider: str,
    exit_code: int,
) -> str | None:
    """Persist dirty work after any cancelled activity attempt."""
    if not worktree_path:
        return None
    return await asyncio.to_thread(
        _checkpoint_worktree_or_raise,
        str(worktree_path),
        workflow_id,
        task=task,
        provider=provider,
        exit_code=exit_code,
    )


def _checkpoint_uncommitted_worktree(
    worktree_path: str | None,
    workflow_id: str,
    *,
    auto_committed: bool,
    task: str,
    provider: str,
    exit_code: int,
) -> str | None:
    """Preserve dirty work whenever auto-commit did not create a commit."""
    if not worktree_path or auto_committed:
        return None
    return _checkpoint_worktree_or_raise(
        str(worktree_path),
        workflow_id,
        task=task,
        provider=provider,
        exit_code=exit_code,
    )


async def _prepare_cancelled_retry(
    proc: asyncio.subprocess.Process,
    worktree_path: str | None,
    workflow_id: str,
    *,
    task: str,
    provider: str,
    attempt_identity: str | None = None,
) -> bool:
    """Reap and checkpoint a cancelled attempt; report whether shutdown retries."""
    await _graceful_kill_group(
        proc,
        worktree_path=worktree_path,
        attempt_identity=attempt_identity,
    )
    shutdown_requested = _worker_shutdown_requested()
    await _checkpoint_shutdown_worktree(
        worktree_path,
        workflow_id,
        task=task,
        provider=provider,
        exit_code=proc.returncode if proc.returncode is not None else -15,
    )
    return shutdown_requested


def _worker_shutdown_requested() -> bool:
    """True when the current activity was cancelled by worker graceful shutdown."""
    try:
        return activity.is_worker_shutdown()
    except Exception:
        return False


_HEARTBEAT_INTERVAL = 30.0


_ACTIVITY_TIMEOUT = timedelta(
    hours=2
)  # generous circuit breaker; stall detection fires first

# Capability gate: keywords indicating out-of-scope or dangerous operations.
_CAPABILITY_BLOCKLIST: tuple[str, ...] = (
    "sudo ",
    "DROP TABLE",
    "DROP DATABASE",
    "rm -rf /",
    "chmod -R 777",
    "mkfs.",
    "dd if=",
    "format disk",
    "crontab",
    "npm publish",
    "twine upload",
    "docker push",
    "scp ",
    "rsync",
    "> /dev/sd",
    "delete all data",
    "wipe disk",
)


def _strip_markdown_code(text: str) -> str:
    """Remove Markdown code spans/blocks before capability keyword scanning."""
    text = _re.sub(r"```.*?```", "", text, flags=_re.DOTALL)
    return _re.sub(r"`[^`]*`", "", text)


def _blocked_capability_keyword(task: str) -> str:
    """Return the blocked keyword in task prose, ignoring literal code snippets."""
    task_upper = _strip_markdown_code(task).upper()
    for keyword in _CAPABILITY_BLOCKLIST:
        if keyword.upper() in task_upper:
            return keyword
    return ""


def _extract_test_paths(task: str) -> list[str]:
    """Extract test file paths from task YAML frontmatter ``tests:`` field."""
    fm = _re.search(r"^---\s*\n(.*?)\n---", task, _re.DOTALL)
    if not fm:
        return []
    in_tests = False
    paths: list[str] = []
    for line in fm.group(1).splitlines():
        s = line.strip()
        if s.startswith("tests:"):
            in_tests = True
            rest = s[len("tests:") :].strip()
            if rest:
                raw = rest.strip("[]")
                tokens = raw.split(",") if "," in raw else raw.split()
                for tok in tokens:
                    tok = tok.strip().strip("'\"")
                    if tok:
                        paths.append(tok)
            continue
        if in_tests:
            if s.startswith("- "):
                tok = s[2:].strip().strip("'\"")
                if tok:
                    paths.append(tok)
            elif not s or not line[0:1].isspace():
                in_tests = False
    return paths


async def _tee_stream(
    stream: asyncio.StreamReader | None,
    log_fh,
    label: str,
    counter: list[int] | None = None,
) -> bytes:
    """Read from async stream, tee chunks to *log_fh*, track byte count in *counter*."""
    if stream is None:
        return b""
    buf = bytearray()
    while True:
        chunk = await stream.read(8192)
        if not chunk:
            break
        buf.extend(chunk)
        if counter is not None:
            counter[0] += len(chunk)
        if log_fh is not None:
            with contextlib.suppress(OSError):
                log_fh.write(f"[{label}] ".encode() + chunk)
                log_fh.flush()
    return bytes(buf)


async def _heartbeat_pulse(
    label: str,
    *,
    interval_s: float = 15.0,
    beat=None,
) -> None:
    """Independent heartbeat pulse — unstarveable by other translocase work.

    Wave-1 2026-07-11 post-mortem: ``activity.heartbeat()`` was only sent at the
    tail of each 30s stall-check tick, after an awaited git-diff computation on
    the shared event loop / thread pool. Under event-loop starvation that
    delivery slipped past the 3m heartbeat timeout and Temporal SIGKILLed a
    healthy worker. This pulse runs as its own task with nothing in its loop
    body besides ``beat()`` and ``sleep``, so its cadence is independent of
    stall-check work. The default ``beat`` is a closure over
    ``activity.heartbeat`` resolved at call time (so tests inject a fake);
    every exception from ``beat`` is swallowed so a transient RPC failure can
    never end the pulse. Loops forever until the task is cancelled.
    """
    if beat is None:

        def beat():
            activity.heartbeat(f"{label} pulse")

    while True:
        with contextlib.suppress(Exception):
            beat()
        await asyncio.sleep(interval_s)


async def _heartbeat_stall_check(
    proc,
    work_dir: str,
    provider: str,
    task: str,
    *,
    skip_stall: bool = False,
    stdout_counter: list[int] | None = None,
    stderr_counter: list[int] | None = None,
    workflow_id: str = "",
    trace=None,
    worktree_path: str | None = None,
    attempt_identity: str | None = None,
) -> None:
    """Dual-signal stall detection: git diff hash + stdout byte growth.

    Every 30s, hash the git diff in the worktree AND read the cumulative
    stdout byte count.  The agent is considered stalled **only** when BOTH
    the diff hash is static AND stdout hasn't grown.  If either signal is
    changing the agent is still active.

    Graduated response: first stall detection logs a warning; second kills.
    Empty-diff patience: if the diff stays empty for 30+ ticks (~15min),
    warn; at 60+ ticks (~30min) kill. The patient window tolerates silent
    build harnesses (e.g. OpenCode) that take >15min to expose a diff.

    Every termination path delegates to ``_graceful_kill_group`` so the
    owned descendant tree is snapshotted, reaped by identity, and verified
    gone. A bare historical-PGID signal can never kill an unrelated process
    that recycled the leader's numeric pgid.
    """
    import hashlib

    stall_frozen_threshold = (
        20  # consecutive identical hashes (~5 min) — complex tasks need thinking time
    )
    stall_oscillation_threshold = 12  # alternating between 2 hashes
    recent_hashes: list[str] = []
    recent_stdout_bytes: list[int] = []
    warnings_sent = 0
    empty_ticks = 0
    empty_diff_hash = hashlib.sha256(b"").hexdigest()[:12]
    no_output_ticks = 0
    last_output_total = -1

    tick = 0
    while True:
        await asyncio.sleep(_HEARTBEAT_INTERVAL)
        tick += 1

        # Check if activity was cancelled — kill subprocess immediately
        if activity.is_cancelled():
            print(
                f"[stall-detect] activity cancelled at tick {tick}, "
                f"killing process (pid={proc.pid})",
                file=sys.stderr,
            )
            await _graceful_kill_group(
                proc,
                worktree_path=worktree_path,
                attempt_identity=attempt_identity,
            )
            return

        # Compute diff content hash
        diff_hash = "unknown"
        try:
            diff_result = await asyncio.to_thread(
                lambda: _subprocess.run(
                    ["git", "diff", "main..HEAD"],
                    capture_output=True,
                    text=True,
                    timeout=10,
                    cwd=work_dir,
                )
            )
            diff_hash = hashlib.sha256(diff_result.stdout.encode()).hexdigest()[:12]
        except Exception:
            pass

        # Read cumulative stdout byte count (monotonically non-decreasing)
        current_stdout_bytes = stdout_counter[0] if stdout_counter else -1
        current_stderr_bytes = stderr_counter[0] if stderr_counter else -1

        recent_hashes.append(diff_hash)
        recent_stdout_bytes.append(current_stdout_bytes)
        max_stall_window = max(stall_frozen_threshold, stall_oscillation_threshold) + 1
        if len(recent_hashes) > max_stall_window:
            recent_hashes.pop(0)
            recent_stdout_bytes.pop(0)

        # Heartbeat delivery moved to the dedicated _heartbeat_pulse task so
        # RPC cadence cannot be starved by the git-diff computation above.

        # Scout/research modes are read-only, so use output growth as liveness.
        if skip_stall:
            has_signal = stdout_counter is not None or stderr_counter is not None
            if not has_signal:
                continue
            if tick < 4:
                continue
            output_total = max(current_stdout_bytes, 0) + max(current_stderr_bytes, 0)
            if last_output_total >= 0 and output_total > last_output_total:
                no_output_ticks = 0
            else:
                no_output_ticks += 1
            last_output_total = output_total
            scout_no_output_warn = 20
            scout_no_output_kill = 30
            if no_output_ticks >= scout_no_output_kill:
                if workflow_id:
                    with contextlib.suppress(Exception):
                        _log_event(
                            workflow_id,
                            "stall_detected",
                            tick=tick,
                            reason="no_output_timeout",
                        )
                        record_stall_event(
                            workflow_id,
                            "no_output_timeout",
                            "kill",
                            {
                                "tick": tick,
                                "no_output_ticks": no_output_ticks,
                                "provider": provider,
                            },
                            trace=trace,
                        )
                print(
                    f"[stall-detect] scout/research no-output timeout at tick {tick} "
                    f"({no_output_ticks} no-output ticks, ~{no_output_ticks * 30 // 60}min), "
                    f"killing process (pid={proc.pid})",
                    file=sys.stderr,
                )
                await _graceful_kill_group(
                    proc,
                    worktree_path=worktree_path,
                    attempt_identity=attempt_identity,
                )
                return
            if no_output_ticks >= scout_no_output_warn:
                print(
                    f"[stall-detect] scout/research no-output warning at tick {tick} "
                    f"({no_output_ticks} no-output ticks, ~{no_output_ticks * 30 // 60}min)",
                    file=sys.stderr,
                )
            continue

        # Skip stall checks for first 2 minutes (4 ticks) — let agent ramp up
        if tick < 4:
            continue

        # Track empty diff ticks — but only when stdout is also stagnant.
        # If stdout is growing, the agent is still active (reading/thinking).
        if diff_hash == empty_diff_hash:
            stdout_growing = (
                len(recent_stdout_bytes) >= 2
                and recent_stdout_bytes[-1] > recent_stdout_bytes[-2]
            )
            if stdout_growing:
                empty_ticks = 0  # stdout is active, agent is not stalled
                continue
            empty_ticks += 1
            if empty_ticks >= 60:
                if workflow_id:
                    with contextlib.suppress(Exception):
                        _log_event(
                            workflow_id,
                            "stall_detected",
                            tick=tick,
                            reason="empty_diff_timeout",
                        )
                        record_stall_event(
                            workflow_id,
                            "empty_diff_timeout",
                            "kill",
                            {
                                "tick": tick,
                                "empty_ticks": empty_ticks,
                                "provider": provider,
                            },
                            trace=trace,
                        )
                print(
                    f"[stall-detect] empty diff + stagnant stdout timeout at tick {tick} "
                    f"({empty_ticks} empty ticks, ~{empty_ticks * 30 // 60}min), "
                    f"killing process (pid={proc.pid})",
                    file=sys.stderr,
                )
                await _graceful_kill_group(
                    proc,
                    worktree_path=worktree_path,
                    attempt_identity=attempt_identity,
                )
                return
            if empty_ticks >= 30:
                print(
                    f"[stall-detect] empty diff + stagnant stdout warning at tick {tick} "
                    f"({empty_ticks} empty ticks, ~{empty_ticks * 30 // 60}min)",
                    file=sys.stderr,
                )
                warnings_sent += 1
            continue

        # Non-empty diff — reset empty counter
        empty_ticks = 0

        # Check whether stdout has grown over the frozen window
        stdout_grew = (
            len(recent_stdout_bytes) >= stall_frozen_threshold
            and recent_stdout_bytes[-1] > recent_stdout_bytes[-stall_frozen_threshold]
        )

        # Detect frozen: last N diff hashes identical AND stdout static
        diff_frozen = (
            len(recent_hashes) >= stall_frozen_threshold
            and len(set(recent_hashes[-stall_frozen_threshold:])) == 1
        )
        is_frozen = diff_frozen and not stdout_grew

        # Detect oscillation: alternating between exactly 2 hashes AND stdout static
        is_oscillating = False
        if len(recent_hashes) >= stall_oscillation_threshold:
            tail = recent_hashes[-stall_oscillation_threshold:]
            unique = set(tail)
            if len(unique) == 2:
                is_alternating = all(
                    tail[idx] != tail[idx + 1] for idx in range(len(tail) - 1)
                )
                if is_alternating:
                    stdout_tail = recent_stdout_bytes[-stall_oscillation_threshold:]
                    is_oscillating = stdout_tail[-1] == stdout_tail[0]

        if is_frozen or is_oscillating:
            stall_type = "frozen" if is_frozen else "oscillating"
            if workflow_id:
                with contextlib.suppress(Exception):
                    _log_event(
                        workflow_id,
                        "stall_detected",
                        tick=tick,
                        reason=f"{stall_type}_diff",
                    )
                    record_stall_event(
                        workflow_id,
                        f"{stall_type}_diff",
                        "kill" if warnings_sent >= 2 else "warn",
                        {
                            "tick": tick,
                            "warnings_sent": warnings_sent + 1,
                            "provider": provider,
                        },
                        trace=trace,
                    )
            warnings_sent += 1
            print(
                f"[stall-detect] {stall_type} at tick {tick} "
                f"(warnings={warnings_sent}, hashes={recent_hashes[-4:]}, "
                f"stdout={current_stdout_bytes})",
                file=sys.stderr,
            )
            if warnings_sent >= 3:
                print(
                    f"[stall-detect] killing stalled process (pid={proc.pid})",
                    file=sys.stderr,
                )
                await _graceful_kill_group(
                    proc,
                    worktree_path=worktree_path,
                    attempt_identity=attempt_identity,
                )
                return


def _derive_output_tid(task_id_match, workflow_id: str) -> str:
    """Derive the id used in the ribosome-outputs filename.

    Priority: explicit `[t-XXXX]` task marker (stable across retries, used
    for the cache-hit check) > workflow_id (unique per Temporal workflow,
    so concurrent same-second completions never collide) > wall-clock+pid
    as a last resort when neither is available.
    """
    if task_id_match:
        return task_id_match.group(1)
    if workflow_id:
        return _re.sub(r"[^0-9a-zA-Z_-]", "_", workflow_id)
    return f"{_time.strftime('%H%M%S')}-{os.getpid()}"


# ---------------------------------------------------------------------------
# Claude Code stream-json normalization
# ---------------------------------------------------------------------------
# The Claude harness emits a multi-megabyte newline-delimited JSON event
# stream (``--output-format stream-json --verbose``). The ``translate()``
# activity ships only ``stdout[:1000]`` to chaperone as the review payload
# -- which on a real Claude session is one minified
# ``type:"system", subtype:"init"`` record, hiding every signal chaperone
# needs (final report, pytest invocations, pass/fail tails). The helpers
# below project the full session into a bounded review payload so a
# successful Claude run no longer trips ``thin_output``.

# Bounds — sized so the normalized payload stays inside Temporal's review
# envelope (the prior payload was 1000 chars). Each field has its own cap
# so a single huge report or a single huge result tail cannot crowd out
# the rest of the evidence.
_CLAUDE_RESULT_BUDGET = 1200  # final ``result`` text
_CLAUDE_VERIFY_CMD_BUDGET = 200  # per extracted verifier command
_CLAUDE_VERIFY_RESULT_BUDGET = 400  # per extracted verifier tail
_CLAUDE_VERIFY_MAX_ITEMS = 6  # cap on extracted verify blocks
_CLAUDE_TOTAL_BUDGET = 3500  # entire normalized payload

# Test and quality commands whose Bash invocations are safe to surface in
# the review payload. ``arbitrary`` shell (pushes, rm, sudo, scp, ...) is
# never copied; only these commands and bounded result tails reach
# chaperone. ``go test`` is matched without a trailing word boundary so
# ``go test ./...`` survives the regex (the boundary would reject the
# leading dot in the path).
_CLAUDE_VERIFY_COMMAND_RE = _re.compile(
    r"\b(?:"
    r"uv\s+run\s+pytest|pytest|"
    r"uv\s+run\s+ruff|ruff|"
    r"uv\s+run\s+mypy|mypy|"
    r"npm\s+test|npm\s+run\s+test|pnpm\s+test|yarn\s+test|bun\s+test|"
    r"cargo\s+test|"
    r"go\s+test\b"
    r")",
    _re.IGNORECASE,
)

# Even when a command matches the test/quality recognizer, a destructive
# preamble or chained operator means the line is not actually a verifier
# run (e.g. ``rm -rf / && pytest``). Reject those so destructive shell
# never reaches the review payload regardless of how it is wrapped.
_CLAUDE_DESTRUCTIVE_RE = _re.compile(
    r"\brm\s+-[a-zA-Z]*r[a-zA-Z]*f\b"
    r"|\brm\s+-[a-zA-Z]*f[a-zA-Z]*r\b"
    r"|\bshutil\.rmtree\b"
    r"|\bgit\s+push\s+--force\b"
    r"|\bmkfs\b"
    r"|\bdd\s+if="
    r"|\bsudo\b"
    r"|\bDROP\s+TABLE\b"
    r"|\bDROP\s+DATABASE\b",
    _re.IGNORECASE,
)


def _parse_claude_stream_records(stdout: str) -> list[dict]:
    """Defensively split *stdout* into parsed JSON object records.

    Each line is parsed independently; lines that are not JSON objects
    (blank, prose, fragments, scalars) are skipped without aborting the
    parse. The ribosome tee logger writes raw chunks into
    :func:`_tee_stream`'s in-memory buffer, so the helper receives bare
    NDJSON without any ``[stdout] `` prefix at runtime.
    """
    records: list[dict] = []
    for raw_line in stdout.splitlines():
        line = raw_line.strip()
        if not line or not line.startswith("{"):
            continue
        try:
            rec = json.loads(line)
        except (ValueError, TypeError):
            continue
        if isinstance(rec, dict):
            records.append(rec)
    return records


def _extract_claude_final_report(records: list[dict]) -> str | None:
    """Return the latest successful ``type=result`` ``result`` string.

    Claude emits exactly one final ``type=result`` record per session,
    but defensive code tolerates multiples. ``subtype="success"`` (or an
    unlabeled subtype treated as success) is required; error subtypes
    like ``error_max_tokens`` are ignored so a truncated run is never
    projected as a completed report. An empty/whitespace ``result``
    string is treated as missing.
    """
    candidate: str | None = None
    for rec in records:
        if not isinstance(rec, dict) or rec.get("type") != "result":
            continue
        subtype = rec.get("subtype")
        if subtype not in ("success", None):
            continue
        text = rec.get("result")
        if isinstance(text, str) and text.strip():
            candidate = text
    return candidate


def _tail_for_claude_verify(text: str, max_lines: int = 6) -> str:
    """Return the bounded tail of a verification result.

    pytest/ruff/mypy/cargo/go put their pass/fail summary at the end; the
    last few lines carry ``N passed`` or ``failed:`` without dragging
    tracebacks into the review payload.
    """
    if not text:
        return ""
    lines = text.splitlines()
    return "\n".join(lines[-max_lines:]).strip()


def _extract_claude_verification_evidence(
    records: list[dict],
) -> list[tuple[str, str]]:
    """Pair each test/quality Bash tool_use with its matching tool_result.

    Walks *records* in order, collecting ``Bash`` tool_use commands whose
    ``input.command`` matches :data:`_CLAUDE_VERIFY_COMMAND_RE` and the
    first matching ``tool_result`` for each ``tool_use_id``. Returns
    ``(command, tail)`` tuples in invocation order.

    Each command has its leading ``cd <dir> && `` preamble stripped so
    the review payload shows the actual verifier invocation, not the
    worktree path. Arbitrary and destructive Bash commands never match
    the recognizer and so are never surfaced.
    """
    pending: dict[str, str] = {}  # tool_use_id → command
    results: dict[str, str] = {}  # tool_use_id → first result content
    order: list[str] = []  # tool_use_ids in arrival order

    for rec in records:
        if not isinstance(rec, dict):
            continue
        msg = rec.get("message")
        if not isinstance(msg, dict):
            continue
        contents = msg.get("content")
        if not isinstance(contents, list):
            continue
        for item in contents:
            if not isinstance(item, dict):
                continue
            item_type = item.get("type")
            if item_type == "tool_use" and item.get("name") == "Bash":
                call_id = item.get("id")
                cmd = (item.get("input") or {}).get("command")
                if (
                    isinstance(call_id, str)
                    and isinstance(cmd, str)
                    and _CLAUDE_VERIFY_COMMAND_RE.search(cmd)
                    and not _CLAUDE_DESTRUCTIVE_RE.search(cmd)
                ):
                    pending[call_id] = cmd
                    if call_id not in order:
                        order.append(call_id)
            elif item_type == "tool_result":
                call_id = item.get("tool_use_id")
                content = item.get("content")
                if (
                    isinstance(call_id, str)
                    and call_id in pending
                    and isinstance(content, str)
                    and call_id not in results
                ):
                    results[call_id] = content

    evidence: list[tuple[str, str]] = []
    for call_id in order:
        if call_id not in results:
            continue
        cmd = pending[call_id]
        cmd_clean = _re.sub(r"^(?:cd\s+\S+\s*&&\s*)+", "", cmd).strip()
        if not cmd_clean:
            continue
        tail = _tail_for_claude_verify(results[call_id])
        evidence.append((cmd_clean, tail))
        if len(evidence) >= _CLAUDE_VERIFY_MAX_ITEMS:
            break
    return evidence


def _normalize_claude_stream_json(stdout: str) -> str:
    """Project a Claude Code stream-json session into a bounded review payload.

    Parses the stream defensively and assembles:

    1. ``[auto-verify] <cmd>`` blocks (with bounded result tails) for
       each detected test/quality Bash command (pytest, ruff, mypy,
       npm/pnpm/yarn/bun test, cargo test, go test).
    2. The final ``type=result`` record's human-readable ``result``
       string.

    Verification evidence precedes the final report so an existing
    1,000-byte review window still sees it. Each field is bounded, and
    the total payload is capped at :data:`_CLAUDE_TOTAL_BUDGET`.

    Returns ``stdout[:1000]`` (the existing review payload) unchanged
    when:

    * *stdout* is empty (returned unchanged as ``""``),
    * no recognizable stream-json record is present,
    * the stream lacks a final successful ``type=result`` record, or
    * any unexpected error occurs during parsing.

    Normalization must never turn a successful ribosome run into a
    failure: on any doubt it preserves the prior review payload.
    """
    if not stdout:
        return stdout
    try:
        records = _parse_claude_stream_records(stdout)
        if not records:
            return stdout[:1000]
        final_report = _extract_claude_final_report(records)
        if final_report is None:
            return stdout[:1000]
        evidence = _extract_claude_verification_evidence(records)
    except Exception:
        return stdout[:1000]

    sections: list[str] = []
    for cmd, tail in evidence:
        block = f"[auto-verify] {cmd[:_CLAUDE_VERIFY_CMD_BUDGET]}"
        if tail:
            block += f"\n{tail[:_CLAUDE_VERIFY_RESULT_BUDGET]}"
        sections.append(block)
    if final_report:
        sections.append(final_report[:_CLAUDE_RESULT_BUDGET])

    body = "\n\n".join(sections).strip()
    if not body:
        return stdout[:1000]
    return body[:_CLAUDE_TOTAL_BUDGET]


@activity.defn
async def translate(
    task: str,
    provider: str,
    mode: str = "build",
    repo: str | None = None,
    harness: str = "",
) -> dict:
    """Execute a single ribosome task as a subprocess."""
    workflow_id = ""
    with contextlib.suppress(RuntimeError):
        workflow_id = activity.info().workflow_id
    attempt_identity = f"{workflow_id or 'unknown'}:{os.getpid()}:{_time.time_ns()}"
    # Match the actual ribosome effector invocation, not any process containing
    # "ribosome" in argv (e.g. rsync of ribosome-outputs/ paths). The bash effector
    # is always invoked with `--provider`, so that's the precise marker.
    _proc_count = int(
        _subprocess.run(
            ["pgrep", "-cf", "ribosome --provider"],
            capture_output=True,
            text=True,
            timeout=5,
        ).stdout.strip()
        or "0"
    )
    if _proc_count > 8:
        raise ApplicationError(
            f"Concurrency gate: {_proc_count} ribosome processes", non_retryable=False
        )
    # Capability gate: reject tasks containing blocked keywords
    blocked_keyword = _blocked_capability_keyword(task)
    if blocked_keyword:
        return {
            "success": False,
            "exit_code": -1,
            "provider": provider,
            "workflow_id": workflow_id,
            "task": task[:200],
            "stdout": "",
            "stderr": f"CAPABILITY_GATE: blocked keyword '{blocked_keyword}' detected in task",
            "gate": "capability",
            "blocked_keyword": blocked_keyword,
        }

    task_id_match = _re.search(r"\[t-([0-9a-fA-F]+)\]", task)
    tid_str = task_id_match.group(1) if task_id_match else ""
    if tid_str:
        cached = OUTPUT_DIR / f"{_time.strftime('%Y%m%d')}-{tid_str}.txt"
        if cached.exists():
            content = cached.read_text()
            if "Exit: 0" in content[:200]:
                return {
                    "success": True,
                    "exit_code": 0,
                    "provider": provider,
                    "workflow_id": workflow_id,
                    "task": task[:200],
                    "stdout": "(cached from prior attempt)",
                    "stderr": "",
                    "pre_diff": {"stat": "", "numstat": ""},
                    "post_diff": {"stat": "", "numstat": ""},
                    "cost_info": "",
                    "output_path": str(cached),
                }
            print(f"cache: stale failure for {tid_str}, re-executing")

    try:
        syntax_check = await asyncio.to_thread(
            _subprocess.run,
            ["bash", "-n", str(RIBOSOME_SCRIPT)],
            capture_output=True,
            text=True,
            timeout=5,
        )
        if syntax_check.returncode != 0:
            return {
                "exit_code": -1,
                "success": False,
                "stderr": f"ribosome script has syntax error: {syntax_check.stderr.strip()}",
            }
    except _subprocess.TimeoutExpired:
        pass

    # Create Langfuse trace for this task execution (no-op if langfuse not installed)
    _log_event(workflow_id, "dispatch", task=task[:200], mode=mode)
    _trace = create_task_trace(task, provider, workflow_id)

    # Use structured repo parameter when provided; fall back to prompt mining.
    # Expand `~` defensively — dispatch.py already expands, but specs arriving
    # via the Temporal workflow from other clients could still contain a raw
    # `~/...` path. Python's subprocess does NOT tilde-expand cwd, so an
    # unexpanded value here crashes the activity with FileNotFoundError before
    # any log is written.
    if repo:
        resolved = Path(repo).expanduser()
        # Defensive: bare names like "terryli-hm" survive expanduser unchanged.
        # Resolve to ~/code/<name> or ~/<name> before using as subprocess cwd.
        if not resolved.is_absolute():
            for candidate in [Path.home() / "code" / repo, Path.home() / repo]:
                if candidate.is_dir():
                    resolved = candidate
                    break
            else:
                resolved = Path(repo).resolve()
        repo_root = str(resolved)
    else:
        repo_root = _detect_repo(task, str(Path.home() / "germline"))

    branch_name = _derive_branch_name(workflow_id, tid_str)
    worktree_path = None

    # Scout mode: no worktree, run in main repo (read-only)
    if mode == "scout":
        worktree_path = None
        work_dir = repo_root
    else:
        try:
            worktree_path = await asyncio.to_thread(
                _create_worktree, repo_root, branch_name
            )
            work_dir = worktree_path
        except Exception as exc:
            raise RuntimeError(
                f"Worktree creation failed for {repo_root}: {exc}. "
                "Build tasks must not run on main. Fix: clear stale index.lock or worktrees."
            ) from exc

    prior_commits = await asyncio.to_thread(
        _detect_prior_commits, work_dir, time_window_minutes=40, author="ribosome"
    )
    effective_task = task
    if prior_commits:
        commit_list = "\n".join(f"  - {c}" for c in prior_commits)
        prefix = (
            "NOTE: A prior attempt on this task made the following commits "
            "before being interrupted:\n"
            f"{commit_list}\n"
            "Review these commits -- if they partially complete the task, "
            "continue from where they left off. "
            "Do NOT redo already-committed work.\n\n"
        )
        effective_task = prefix + task

    await asyncio.to_thread(_git_pull_ff_only, work_dir)
    # Record HEAD before ribosome runs — used as fallback range if main..HEAD is empty
    try:
        pre_sha_r = _subprocess.run(
            ["git", "rev-parse", "HEAD"],
            capture_output=True,
            text=True,
            timeout=5,
            cwd=work_dir,
        )
        pre_sha = pre_sha_r.stdout.strip() if pre_sha_r.returncode == 0 else None
    except Exception:
        pre_sha = None
    with contextlib.suppress(Exception):
        print(
            _format_landing_banner(
                repo_root, str(work_dir), branch_name, worktree_path, mode, pre_sha
            ),
            file=sys.stderr,
        )
    pre_diff = await asyncio.to_thread(_git_snapshot, work_dir)
    # Confinement guard: in worktree mode the main checkout must stay
    # untouched — record its state so post-run mutation is detectable
    # (2026-07-04: a ghost opencode kept editing the main checkout).
    main_state = None
    if worktree_path:
        main_state = await asyncio.to_thread(_main_checkout_state, repo_root)
    main_checkout_mutated = False

    # SRP: detect [supervised] marker in task string
    is_supervised = "[supervised]" in task
    if is_supervised:
        effective_task = effective_task.replace("[supervised]", "").strip()

    # Load provider health and resolve actual provider via circuit-breaker routing
    health = load_health()
    resolved_provider = select_provider(health, provider)

    # Retry loop: on exit 42 (rate-limited), circuit-trip and try next provider
    _attempted: set[str] = set()
    rc = None
    stdout = ""
    stderr = ""

    while True:
        resolved_provider = _select_attempt_provider(health, provider, _attempted)

        _attempted.add(resolved_provider)
        _active_count[resolved_provider] = _active_count.get(resolved_provider, 0) + 1
        _log_event(
            workflow_id,
            "provider_selected",
            provider=resolved_provider,
            attempt=len(_attempted),
        )
        print(
            f"[translocase] selected: {resolved_provider} "
            f"(health: {health.get(resolved_provider, {}).get('state', 'closed')})",
            file=sys.stderr,
        )
        cmd = [
            "bash",
            str(RIBOSOME_SCRIPT),
            *(["--supervised"] if is_supervised else []),
            "--provider",
            resolved_provider,
            "--",
            effective_task,
        ]

        _run_start = _time.monotonic()
        try:
            proc = await asyncio.create_subprocess_exec(
                *cmd,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
                cwd=work_dir,
                env={
                    **os.environ,
                    "RIBOSOME_PROVIDER": harness or resolved_provider,
                    "RIBOSOME_TASK_ID": workflow_id,
                    "RIBOSOME_ATTEMPT_ID": attempt_identity,
                    "HOME": str(Path.home()),
                },
                start_new_session=True,  # process group kill — prevents orphan ribosome processes
            )
        except BaseException:
            # This await is the only cancellation/exception point between the
            # _active_count increment above and the try/finally below that owns
            # the decrement. A spawn failure (e.g. OSError under fd/process
            # exhaustion) or an activity cancel here would otherwise leak the
            # reserved slot permanently, eventually pinning the provider over
            # its concurrency cap until the worker restarts. Release it here.
            _active_count[resolved_provider] = max(
                0, _active_count.get(resolved_provider, 0) - 1
            )
            raise
        _log_event(workflow_id, "subprocess_started", pid=proc.pid)

        stdout_counter: list[int] = [0]  # mutable counter shared with heartbeat
        stderr_counter: list[int] = [0]

        # Open workflow-scoped log file for real-time observability
        log_fh = None
        wf_id = ""
        with contextlib.suppress(Exception):
            wf_id = activity.info().workflow_id
        if wf_id:
            with contextlib.suppress(OSError):
                LOG_DIR.mkdir(parents=True, exist_ok=True)
                log_fh = open(LOG_DIR / f"{wf_id}.log", "ab")
                _hdr = (
                    "\n"
                    + "=" * 60
                    + "\n"
                    + "["
                    + _time.strftime("%Y-%m-%dT%H:%M:%S")
                    + "] "
                    + "provider="
                    + resolved_provider
                    + "\n"
                    + "task="
                    + task[:120]
                    + "\n"
                    + "=" * 60
                    + "\n"
                )
                log_fh.write(_hdr.encode())
                log_fh.flush()

        stdout_task = asyncio.create_task(
            _tee_stream(proc.stdout, log_fh, "stdout", counter=stdout_counter)
        )
        stderr_task = asyncio.create_task(
            _tee_stream(proc.stderr, log_fh, "stderr", counter=stderr_counter)
        )

        _skip_stall = mode in ("scout", "research")
        hb_task = asyncio.create_task(
            _heartbeat_stall_check(
                proc,
                work_dir,
                provider,
                task,
                skip_stall=_skip_stall,
                stdout_counter=stdout_counter,
                stderr_counter=stderr_counter,
                workflow_id=workflow_id,
                trace=_trace,
                worktree_path=worktree_path,
                attempt_identity=attempt_identity,
            )
        )
        pulse_task = asyncio.create_task(_heartbeat_pulse(f"{provider}:{task[:60]}"))
        try:
            try:
                stdout_bytes, stderr_bytes = await asyncio.wait_for(
                    asyncio.gather(stdout_task, stderr_task),
                    timeout=_ACTIVITY_TIMEOUT.total_seconds(),
                )
                try:
                    await asyncio.wait_for(proc.wait(), timeout=10)
                except TimeoutError:
                    pass
                # A wrapper can exit while background or setsid descendants
                # remain. Reap and verify the full task tree on normal and
                # timed-out completion before inspecting or removing work.
                await _graceful_kill_group(
                    proc,
                    worktree_path=worktree_path,
                    attempt_identity=attempt_identity,
                )
            except TimeoutError:
                await _graceful_kill_group(
                    proc,
                    worktree_path=worktree_path,
                    attempt_identity=attempt_identity,
                )
                await asyncio.to_thread(
                    _checkpoint_uncommitted_worktree,
                    worktree_path,
                    workflow_id or wf_id,
                    auto_committed=False,
                    task=task,
                    provider=resolved_provider,
                    exit_code=-1,
                )
                with contextlib.suppress(Exception):
                    await asyncio.wait_for(
                        asyncio.gather(
                            stdout_task, stderr_task, return_exceptions=True
                        ),
                        timeout=5,
                    )
                _r = {
                    "success": False,
                    "exit_code": -1,
                    "provider": resolved_provider,
                    "workflow_id": workflow_id,
                    "repo_root": repo_root,
                    "base_sha": pre_sha or "",
                    "requested_provider": provider,
                    "attempted_providers": sorted(_attempted),
                    "task": task[:200],
                    "stdout": "",
                    "stderr": "timeout after 30m",
                    "output_path": _persist_output_file(
                        task, resolved_provider, -1, "", "timeout after 30m"
                    ),
                }
                finalize_trace(_trace, _r)
                return _r
            except asyncio.CancelledError:
                # Temporal cancelled the activity (stall-detect kill, workflow
                # cancel, or worker graceful shutdown). Kill the subprocess
                # tree either way.
                if await _prepare_cancelled_retry(
                    proc,
                    worktree_path,
                    workflow_id or wf_id,
                    task=task,
                    provider=resolved_provider,
                    attempt_identity=attempt_identity,
                ):
                    # Worker is draining (systemd stop / rictor deploy) and the
                    # graceful window expired. Preserve dirty work before
                    # re-raising: retry setup force-removes the prior worktree,
                    # so this is the final safe checkpoint boundary.
                    # Re-raise so Temporal retries the attempt on the restarted
                    # worker instead of recording a spurious failure result.
                    raise
                try:
                    stdout_bytes, stderr_bytes = await asyncio.wait_for(
                        asyncio.gather(
                            stdout_task, stderr_task, return_exceptions=True
                        ),
                        timeout=5,
                    )
                    if isinstance(stdout_bytes, BaseException):
                        stdout_bytes = b""
                    if isinstance(stderr_bytes, BaseException):
                        stderr_bytes = b"cancelled"
                except Exception:
                    stdout_bytes, stderr_bytes = b"", b"cancelled"
                _cancelled_stdout = stdout_bytes.decode(errors="replace")
                _cancelled_stderr = (
                    f"cancelled: {stderr_bytes.decode(errors='replace')[:500]}"
                )
                _r = {
                    "success": False,
                    "exit_code": -1,
                    "provider": resolved_provider,
                    "workflow_id": workflow_id,
                    "repo_root": repo_root,
                    "base_sha": pre_sha or "",
                    "requested_provider": provider,
                    "attempted_providers": sorted(_attempted),
                    "task": task[:200],
                    "stdout": _cancelled_stdout[:1000],
                    "stderr": _cancelled_stderr,
                    "output_path": _persist_output_file(
                        task,
                        resolved_provider,
                        -1,
                        _cancelled_stdout,
                        _cancelled_stderr,
                    ),
                }
                finalize_trace(_trace, _r)
                return _r
        finally:
            hb_task.cancel()
            pulse_task.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await hb_task
            with contextlib.suppress(asyncio.CancelledError):
                await pulse_task
            if log_fh:
                with contextlib.suppress(OSError):
                    log_fh.close()
            _active_count[resolved_provider] = max(
                0, _active_count.get(resolved_provider, 0) - 1
            )
            with contextlib.suppress(Exception):
                _reap_orphaned_worktree_processes(repo_root)

        rc = proc.returncode or 0
        if main_state is not None:
            post_state = await asyncio.to_thread(_main_checkout_state, repo_root)
            head_moved = (
                bool(main_state["head"]) and post_state["head"] != main_state["head"]
            )
            new_dirt = sorted(
                set(post_state["dirty_paths"]) - set(main_state["dirty_paths"])
            )
            if head_moved or new_dirt:
                main_checkout_mutated = True
                _log_event(
                    workflow_id,
                    "main_checkout_mutated",
                    head_before=main_state["head"],
                    head_after=post_state["head"],
                    new_dirty_paths=new_dirt[:20],
                )
                print(
                    f"[guard] main checkout {repo_root} mutated during {workflow_id}: "
                    f"head_moved={head_moved} new_dirty={new_dirt[:5]}",
                    file=sys.stderr,
                )
        auto_committed = False
        # Preserve recoverable edits BEFORE failed-run cleanup resets the worktree.
        # Read-only modes (scout/research) are protected by _mode_allows_auto_commit.
        if work_dir and _mode_allows_auto_commit(mode):
            auto_committed = _auto_commit(str(work_dir), wf_id)
        # A successful subprocess can still leave dirty work when auto-commit
        # fails. Preserve it before every later force-remove, not only on a
        # nonzero process exit.
        _checkpoint_uncommitted_worktree(
            worktree_path,
            workflow_id or wf_id,
            auto_committed=auto_committed,
            task=task,
            provider=resolved_provider,
            exit_code=rc,
        )
        if rc != 0 and worktree_path and not auto_committed:
            # The checkpoint above is now durable, so failed-run cleanup may
            # reset the worktree before the retry or operator review.
            _cleanup_worktree(str(worktree_path))
        stdout = stdout_bytes.decode(errors="replace")
        stderr = stderr_bytes.decode(errors="replace")
        # Claude Code emits a stream-json session (one JSON record per line);
        # normalize it into a concise review payload (verification evidence +
        # final report) so chaperone doesn't see only the minified init
        # record that ``stdout[:1000]`` used to capture. The helper falls
        # back to ``stdout[:1000]`` on any doubt (no final result record,
        # malformed stream, parse error), so non-Claude harnesses are
        # unaffected and failures retain their original review payload.
        _review_stdout = (
            _normalize_claude_stream_json(stdout)
            if harness == "claude"
            else stdout[:1000]
        )

        if _is_coaching_bloat_error(rc, stderr):
            _r = {
                "success": False,
                "exit_code": rc,
                "provider": resolved_provider,
                "workflow_id": workflow_id,
                "repo_root": repo_root,
                "base_sha": pre_sha or "",
                "requested_provider": provider,
                "attempted_providers": sorted(_attempted),
                "task": task[:200],
                "stdout": stdout[:1000],
                "stderr": stderr[:1000],
                "error": "coaching_bloat",
                "non_retryable": True,
            }
            finalize_trace(_trace, _r)
            return _r

        # Detect rate-limit errors from stderr on non-zero, non-42 exits
        suggested_wait: float | None = None
        if rc != 0:
            _is_rl, suggested_wait = _detect_rate_limit_error(stderr)
            if _is_rl and rc != EXIT_RATE_LIMITED:
                print(
                    f"[translocase] rate-limit detected in output (exit={rc}), "
                    f"treating as rate-limited (provider={resolved_provider})",
                    file=sys.stderr,
                )
                rc = EXIT_RATE_LIMITED

        # Update provider health state and persist
        window_hours = parse_rate_limit_window(stderr)
        update_health(resolved_provider, rc, health, window_hours)
        save_health(health)

        try:
            _log_event(
                workflow_id,
                "subprocess_exited",
                provider=resolved_provider,
                exit_code=rc,
                duration_seconds=round(_time.monotonic() - _run_start, 2),
                stdout_bytes=len(stdout_bytes),
                stderr_bytes=len(stderr_bytes),
            )
        except Exception as exc:
            print(f"WARNING: failed to write lifecycle event: {exc}", file=sys.stderr)

        # Retry on rate-limit exit: circuit-trip this provider, select next, re-run
        if rc == EXIT_RATE_LIMITED:
            # Auto-throttle: exponential backoff + jitter before retry
            _wait_secs = _throttle_wait(len(_attempted) - 1, suggested_wait)
            print(
                f"[translocase] auto-throttling {_wait_secs:.1f}s before retry "
                f"(provider={resolved_provider}, attempt={len(_attempted)})",
                file=sys.stderr,
            )
            await asyncio.sleep(_wait_secs)

            print(
                f"[translocase] rate-limited provider {resolved_provider}, "
                f"retrying with fallback (attempted={sorted(_attempted)})",
                file=sys.stderr,
            )
            if _attempted.issuperset(PROVIDER_PRIORITY):
                # All providers exhausted
                _r = {
                    "success": False,
                    "exit_code": rc,
                    "provider": resolved_provider,
                    "workflow_id": workflow_id,
                    "repo_root": repo_root,
                    "base_sha": pre_sha or "",
                    "requested_provider": provider,
                    "attempted_providers": sorted(_attempted),
                    "task": task[:200],
                    "stdout": stdout[:1000],
                    "stderr": f"All providers rate-limited: {sorted(_attempted)}",
                }
                finalize_trace(_trace, _r)
                return _r
            continue

        # All other exit codes (success, non-retryable failure) exit the retry
        # loop immediately. Prior to 2026-04-11 there was no break here — a
        # fast-failing subprocess (e.g. `Unknown flag: ---` from claude CLI
        # parsing YAML frontmatter) would loop at full CPU speed writing
        # header blocks to logs/<workflow_id>.log until an external timeout
        # killed it. One incident produced 59,356 retries and a 208 MB log
        # in 10 minutes from a single bad spec.
        break

    # SRP defer detection: supervised mode returns JSON with stop_reason
    if is_supervised and rc == 0:
        with contextlib.suppress(Exception):
            import json as _json

            output_json = _json.loads(stdout)
            if output_json.get("stop_reason") == "tool_deferred":
                _r = {
                    "success": False,
                    "exit_code": 0,
                    "provider": resolved_provider,
                    "workflow_id": workflow_id,
                    "repo_root": repo_root,
                    "base_sha": pre_sha or "",
                    "requested_provider": provider,
                    "attempted_providers": sorted(_attempted),
                    "task": task[:200],
                    "stdout": stdout[:1000],
                    "stderr": stderr[:500],
                    "deferred": True,
                    "session_id": output_json.get("session_id", ""),
                    "deferred_tool": output_json.get("deferred_tool_use", {}).get(
                        "name", "unknown"
                    ),
                    "pre_diff": pre_diff,
                    "post_diff": {"stat": "", "numstat": ""},
                    "output_path": "",
                    "branch_name": branch_name if worktree_path else "",
                    "merged": False,
                }
                finalize_trace(_trace, _r)
                return _r

    # Early exit gate: if work is committed and tests pass, skip verdict review.
    # Prevents zombie pattern where translocase loops on circuit-broken providers
    # after the ribosome has already completed and committed its work.
    if rc == 0 and pre_sha and work_dir:
        try:
            _head_r = _subprocess.run(
                ["git", "rev-parse", "HEAD"],
                capture_output=True,
                text=True,
                timeout=5,
                cwd=work_dir,
            )
            _post_head = _head_r.stdout.strip() if _head_r.returncode == 0 else None
        except Exception:
            _post_head = None

        if _post_head and _post_head != pre_sha:
            _test_paths = _extract_test_paths(task)
            _tests_pass = True
            _test_r = None
            if _test_paths:
                _test_r = await asyncio.to_thread(
                    _subprocess.run,
                    ["uv", "run", "pytest", "-x", *_test_paths],
                    cwd=work_dir,
                    capture_output=True,
                    text=True,
                    timeout=300,
                )
                _tests_pass = _test_r.returncode == 0

            # Preserve the auto-verifier command + a trimmed pytest tail so the
            # chaperone's stdout/stderr-based detection (_detected_test_commands,
            # _TEST_PASSED) can mark verification.status = "passed" instead of
            # "unknown". Only built when tests actually ran — the no-tests early
            # exit keeps its original (verifier-free) stdout unchanged.
            _verifier_cmd = ""
            _verifier_tail = ""
            if _test_paths and _test_r is not None:
                _verifier_cmd = "uv run pytest -x " + " ".join(_test_paths)
                _vout = _test_r.stdout or ""
                _verr = _test_r.stderr or ""
                _verifier_tail = (
                    f"{_vout}\n{_verr}".strip()[-1200:]
                    if _verr.strip()
                    else _vout.strip()[-1200:]
                )

            if _tests_pass:
                # Capture diff evidence BEFORE removing the worktree so
                # chaperone receives real commit_count instead of rejecting
                # with no_commit_on_success.
                _post_diff = await asyncio.to_thread(
                    _git_snapshot, work_dir, base_sha=pre_sha
                )
                _ee_commit_count = _post_diff.get("commit_count", 0)
                if _ee_commit_count == 0 and _post_head != pre_sha:
                    try:
                        _cnt_r = _subprocess.run(
                            ["git", "rev-list", "--count", f"{pre_sha}..HEAD"],
                            capture_output=True,
                            text=True,
                            timeout=5,
                            cwd=work_dir,
                        )
                        if _cnt_r.returncode == 0:
                            _real = int(_cnt_r.stdout.strip())
                            if _real > 0:
                                _post_diff["commit_count"] = _real
                                _post_diff["head_moved_fallback"] = True
                                _ee_commit_count = _real
                    except Exception:
                        pass

                if worktree_path:
                    _subprocess.run(
                        ["git", "push", "origin", f"{branch_name}:{branch_name}"],
                        check=False,
                        timeout=60,
                        cwd=repo_root,
                    )
                    try:
                        await asyncio.to_thread(
                            _remove_worktree_if_inactive,
                            repo_root,
                            worktree_path,
                        )
                    except RuntimeError as exc:
                        print(f"[cleanup] preserving worktree: {exc}", file=sys.stderr)
                _ee_stdout = _review_stdout
                if _verifier_cmd:
                    _ee_stdout = f"{_ee_stdout}\n\n[auto-verify] {_verifier_cmd}\n{_verifier_tail}"
                _r = {
                    "success": True,
                    "exit_code": 0,
                    "provider": resolved_provider,
                    "workflow_id": workflow_id,
                    "repo_root": repo_root,
                    "base_sha": pre_sha or "",
                    "requested_provider": provider,
                    "attempted_providers": sorted(_attempted),
                    "task": task[:200],
                    "stdout": _ee_stdout,
                    "stderr": stderr[:500],
                    "pre_diff": pre_diff,
                    "post_diff": _post_diff,
                    "cost_info": "",
                    "output_path": "",
                    "branch_name": branch_name if worktree_path else "",
                    "merged": False,
                    "mode": mode,
                    "verdict": "early_exit_clean",
                    "post_head": _post_head,
                    "main_checkout_mutated": main_checkout_mutated,
                }
                if _verifier_cmd:
                    _r["verification"] = {
                        "command": _verifier_cmd,
                        "returncode": _test_r.returncode,
                        "status": "passed",
                        "output_tail": _verifier_tail[-500:],
                    }
                finalize_trace(_trace, _r)
                return _r

    post_diff = await asyncio.to_thread(_git_snapshot, work_dir, base_sha=pre_sha)
    commit_count = post_diff.get("commit_count", 0)

    # Robust fallback: if diff-based detection found 0 commits but HEAD actually moved
    # from pre_sha, the ribosome DID commit (likely on main, where main..HEAD is empty
    # and _git_snapshot's except swallowed git lock contention errors).
    if commit_count == 0 and pre_sha:
        try:
            head_r = _subprocess.run(
                ["git", "rev-parse", "HEAD"],
                capture_output=True,
                text=True,
                timeout=5,
                cwd=work_dir,
            )
            current_sha = head_r.stdout.strip() if head_r.returncode == 0 else None
            if current_sha and current_sha != pre_sha:
                # Count actual commits between pre_sha and HEAD
                count_r = _subprocess.run(
                    ["git", "rev-list", "--count", f"{pre_sha}..HEAD"],
                    capture_output=True,
                    text=True,
                    timeout=5,
                    cwd=work_dir,
                )
                real_count = (
                    int(count_r.stdout.strip()) if count_r.returncode == 0 else 1
                )
                post_diff["commit_count"] = real_count
                post_diff["head_moved_fallback"] = True
                commit_count = real_count
                print(
                    f"HEAD moved ({pre_sha[:8]}→{current_sha[:8]}, {real_count} commits) "
                    f"but _git_snapshot missed them",
                    file=sys.stderr,
                )
        except Exception as exc:
            print(f"WARNING: HEAD comparison failed: {exc}", file=sys.stderr)

    # Incomplete: non-zero exit but commits exist — preserve branch for re-dispatch
    is_incomplete = rc != 0 and commit_count > 0
    # Merge deferred to workflow after chaperone review approves.
    # Just clean up the worktree; keep the branch for review-gated merge.
    merged = False
    if worktree_path:
        try:
            await asyncio.to_thread(
                _remove_worktree_if_inactive,
                repo_root,
                worktree_path,
            )
        except RuntimeError as exc:
            print(f"[cleanup] preserving worktree: {exc}", file=sys.stderr)
        if is_incomplete:
            print(
                f"INCOMPLETE: branch {branch_name} preserved ({commit_count} commits)",
                file=sys.stderr,
            )

    cost_info = ""
    for line in stdout.splitlines()[-10:]:
        if any(
            k in line.lower() for k in ["token", "cost", "usage", "input:", "output:"]
        ):
            cost_info += line + "\n"

    extra = ""
    if is_incomplete:
        extra += f"\nBranch preserved for re-dispatch: {branch_name}\n"
    if rc != 0 or not merged:
        extra += f"\n\n--- full patch (recoverable) ---\n{post_diff.get('patch', '')}\n"
    out_path = _persist_output_file(
        task, provider, rc, stdout, stderr, post_diff.get("stat", ""), extra
    )

    _r = {
        "success": rc == 0,
        "exit_code": rc,
        "provider": resolved_provider,
        "workflow_id": workflow_id,
        "repo_root": repo_root,
        "base_sha": pre_sha or "",
        "requested_provider": provider,
        "attempted_providers": sorted(_attempted),
        "task": task[:200],
        "stdout": _review_stdout,
        "stderr": stderr[:500],
        "pre_diff": pre_diff,
        "post_diff": post_diff,
        "cost_info": cost_info[:500],
        "output_path": out_path,
        "branch_name": branch_name if worktree_path else "",
        "merged": merged,
        "mode": mode,
        "main_checkout_mutated": main_checkout_mutated,
    }
    finalize_trace(_trace, _r)
    return _r


@activity.defn
async def create_pr(args: dict) -> dict:
    """Push branch and create a GitHub PR. Called by workflow when pr mode is active.

    Args:
        repo_root: path to git repo
        branch_name: branch to create PR from
        title: PR title (default: branch name)
        body: PR body text

    Returns:
        {"created": bool, "pr_url": str, "pr_number": int, "branch_name": str}
    """
    repo_root = args["repo_root"]
    branch_name = args["branch_name"]
    title = args.get("title")
    body = args.get("body")
    return await asyncio.to_thread(_create_pr_impl, repo_root, branch_name, title, body)


@activity.defn
async def watch_cycle(repo_path: str, plan_dir: str) -> dict:
    """Single watch cycle: sync from ganglion, scan for ready specs.

    Returns dict with:
        synced      – True if sync succeeded
        fetched     – number of new commits fetched
        ready_specs – list of spec dicts ready for dispatch (topologically sorted)
    """
    from mtor.sync import sync_from_ganglion

    sync_result = await asyncio.to_thread(sync_from_ganglion, repo_path)

    ready_specs: list[dict] = []
    if plan_dir:
        try:
            from mtor.rptor import resolve_dag, scan_specs, topological_sort

            specs = scan_specs(Path(plan_dir))
            if specs:
                resolved = resolve_dag(specs)
                dispatchable = [s for s in resolved if s.get("dispatchable")]
                ready_specs = topological_sort(dispatchable)
        except Exception:
            pass

    return {
        "synced": sync_result.get("merged", False),
        "fetched": sync_result.get("fetched", 0),
        "ready_specs": ready_specs,
    }


# Coaching-promoted checks: patterns that were prose coaching notes,
# now enforced as deterministic gate checks. Coaching entries should
# decay toward zero — each one either gets promoted here or retired.


async def main() -> None:
    if "--help" in sys.argv or "-h" in sys.argv:
        print(__doc__)
        sys.exit(0)

    # This worker is Temporal-specific until a real alternative runtime owns
    # recovery and draining. Refuse unsupported configuration before client
    # connection, worktree recovery or activity registration.
    from mtor.backend import require_temporal_backend

    require_temporal_backend()

    # Deferred import to avoid circular dependency with workflow.py
    from mtor.worker.workflow import TranslationWorkflow, WatchWorkflow

    host = os.getenv("TEMPORAL_HOST", "ganglion:7233")
    client = await Client.connect(host)
    # Only count providers that have API keys configured
    max_concurrent = (
        sum(
            limit
            for provider, limit in PROVIDER_LIMITS.items()
            if os.environ.get(f"{provider.upper()}_API_KEY")
        )
        or 2
    )  # default to 2 if no keys detected (op injects them later)

    # Drain window for systemd stop: must stay under TimeoutStopSec (630s)
    # in ~/.config/systemd/user/mtor-worker.service on the worker host.
    drain_seconds = int(os.getenv("MTOR_GRACEFUL_SHUTDOWN_SECONDS", "540"))

    worker = Worker(
        client=client,
        task_queue=TASK_QUEUE,
        workflows=[TranslationWorkflow, WatchWorkflow],
        activities=[translate, chaperone, create_pr, watch_cycle],
        max_concurrent_activities=max_concurrent,
        graceful_shutdown_timeout=timedelta(seconds=drain_seconds),
    )
    _gc_worktrees(str(Path.home() / "germline"))
    for _reap_repo in (Path.home() / "germline", Path.home() / "code" / "mtor"):
        with contextlib.suppress(Exception):
            _reap_orphaned_worktree_processes(str(_reap_repo))
        with contextlib.suppress(Exception):
            _reap_landed_branches(str(_reap_repo))

    # systemd stop (rictor deploy restart) sends SIGTERM. Without a handler
    # the process dies mid-activity and in-flight ribosome subprocesses are
    # killed with it, consuming the activity attempt. Drain instead: stop
    # polling, give running activities the graceful window, then exit 0.
    stop_requested = asyncio.Event()
    loop = asyncio.get_running_loop()
    for sig in (_signal.SIGTERM, _signal.SIGINT):
        with contextlib.suppress(NotImplementedError):
            loop.add_signal_handler(sig, stop_requested.set)

    print(
        f"Translocase started on queue '{TASK_QUEUE}' "
        f"(max_concurrent={max_concurrent}, drain={drain_seconds}s)"
    )
    run_task = asyncio.create_task(worker.run())
    stop_task = asyncio.create_task(stop_requested.wait())
    try:
        await asyncio.wait({run_task, stop_task}, return_when=asyncio.FIRST_COMPLETED)
        if not run_task.done():
            print("Shutdown signal received -- draining in-flight activities")
            await worker.shutdown()
        await run_task
    finally:
        stop_task.cancel()
        with contextlib.suppress(asyncio.CancelledError):
            await stop_task


def run() -> None:
    """Synchronously adapt the async worker for console-script entry points."""
    asyncio.run(main())


if __name__ == "__main__":
    run()
