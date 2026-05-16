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
from mtor.worker.stall_trace import create_task_trace, finalize_trace
from mtor.worker.chaperone_review import chaperone
from mtor.worker.git_ops import (
    _auto_commit,
    _cleanup_worktree,
    _create_pr_impl,
    _create_worktree,
    _detect_prior_commits,
    _detect_repo,
    _gc_worktrees,
    _git_pull_ff_only,
    _git_push,
    _git_snapshot,
    _merge_branch,
)

TASK_QUEUE = "translation-queue"
RIBOSOME_SCRIPT = Path.home() / "germline" / "effectors" / "ribosome"
OUTPUT_DIR = Path.home() / "germline" / "loci" / "ribosome-outputs"
LOG_DIR = Path.home() / "code" / "mtor" / "logs"


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
        wait = min(_THROTTLE_BASE_SECONDS * (2 ** attempt), _THROTTLE_MAX_SECONDS)

    jitter = wait * _THROTTLE_JITTER_FRACTION * (_random.random() * 2 - 1)
    return max(1.0, wait + jitter)


def _is_coaching_bloat_error(rc: int, stderr: str) -> bool:
    """Return True when ribosome failed before launch due to coaching size."""
    return rc == 1 and "coaching file" in stderr.lower() and "limit 10kb" in stderr.lower()


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
        return
    try:
        os.killpg(proc.pid, _signal.SIGKILL)
    except (ProcessLookupError, PermissionError):
        with contextlib.suppress(ProcessLookupError):
            proc.kill()


async def _graceful_kill(
    proc: asyncio.subprocess.Process,
    timeout: float = 5.0,
) -> None:
    """Send SIGTERM then escalate to SIGKILL if the process doesn't exit."""
    if proc.returncode is not None:
        return
    proc.terminate()
    try:
        await asyncio.wait_for(proc.wait(), timeout=timeout)
    except asyncio.TimeoutError:
        proc.kill()
        with contextlib.suppress(Exception):
            await asyncio.wait_for(proc.wait(), timeout=2.0)







_HEARTBEAT_INTERVAL = 30.0



_ACTIVITY_TIMEOUT = timedelta(hours=2)  # generous circuit breaker; stall detection fires first

# Capability gate: keywords indicating out-of-scope or dangerous operations.
# Case-insensitive substring match against the task prompt.
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




def _extract_test_paths(task: str) -> list[str]:
    """Extract test file paths from task YAML frontmatter ``tests:`` field."""
    fm = _re.search(r'^---\s*\n(.*?)\n---', task, _re.DOTALL)
    if not fm:
        return []
    in_tests = False
    paths: list[str] = []
    for line in fm.group(1).splitlines():
        s = line.strip()
        if s.startswith("tests:"):
            in_tests = True
            rest = s[len("tests:"):].strip()
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


async def _heartbeat_stall_check(
    proc, work_dir: str, provider: str, task: str, *,
    skip_stall: bool = False, stdout_counter: list[int] | None = None,
    workflow_id: str = "",
) -> None:
    """Dual-signal stall detection: git diff hash + stdout byte growth.

    Every 30s, hash the git diff in the worktree AND read the cumulative
    stdout byte count.  The agent is considered stalled **only** when BOTH
    the diff hash is static AND stdout hasn't grown.  If either signal is
    changing the agent is still active.

    Graduated response: first stall detection logs a warning; second kills.
    Empty-diff blindness: if diff stays empty for 20+ ticks (~10min), warn;
    at 30+ ticks (~15min), kill.
    """
    import hashlib

    stall_frozen_threshold = 20  # consecutive identical hashes (~5 min) — complex tasks need thinking time
    stall_oscillation_threshold = 12  # alternating between 2 hashes
    recent_hashes: list[str] = []
    recent_stdout_bytes: list[int] = []
    warnings_sent = 0
    empty_ticks = 0
    empty_diff_hash = hashlib.sha256(b"").hexdigest()[:12]

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
            _kill_process_group(proc)
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

        recent_hashes.append(diff_hash)
        recent_stdout_bytes.append(current_stdout_bytes)
        max_stall_window = max(stall_frozen_threshold, stall_oscillation_threshold) + 1
        if len(recent_hashes) > max_stall_window:
            recent_hashes.pop(0)
            recent_stdout_bytes.pop(0)

        with contextlib.suppress(Exception):
            activity.heartbeat(
                f"{provider}:{task[:60]} tick:{tick} diff:{diff_hash} out:{current_stdout_bytes}"
            )

        # Skip stall checks for scout/research modes
        if skip_stall:
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
            if empty_ticks >= 30:
                if workflow_id:
                    with contextlib.suppress(Exception):
                        _log_event(workflow_id, "stall_detected", tick=tick, reason="empty_diff_timeout")
                print(
                    f"[stall-detect] empty diff + stagnant stdout timeout at tick {tick} "
                    f"({empty_ticks} empty ticks, ~{empty_ticks * 30 // 60}min), "
                    f"killing process (pid={proc.pid})",
                    file=sys.stderr,
                )
                _kill_process_group(proc)
                return
            if empty_ticks >= 20:
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
                    _log_event(workflow_id, "stall_detected", tick=tick, reason=f"{stall_type}_diff")
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
                _kill_process_group(proc)
                return


@activity.defn
async def translate(task: str, provider: str, mode: str = "build", repo: str | None = None, harness: str = "") -> dict:
    """Execute a single ribosome task as a subprocess."""
    workflow_id = ""
    with contextlib.suppress(RuntimeError):
        workflow_id = activity.info().workflow_id
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
        raise ApplicationError(f"Concurrency gate: {_proc_count} ribosome processes", non_retryable=False)
    # Capability gate: reject tasks containing blocked keywords
    task_upper = task.upper()
    for keyword in _CAPABILITY_BLOCKLIST:
        if keyword.upper() in task_upper:
            return {
                "success": False,
                "exit_code": -1,
                "provider": provider,
                "workflow_id": workflow_id,
                "task": task[:200],
                "stdout": "",
                "stderr": f"CAPABILITY_GATE: blocked keyword '{keyword}' detected in task",
                "gate": "capability",
                "blocked_keyword": keyword,
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

    branch_name = f"ribosome-{tid_str or _time.strftime('%H%M%S')}"
    worktree_path = None

    # Scout mode: no worktree, run in main repo (read-only)
    if mode == "scout":
        worktree_path = None
        work_dir = repo_root
    else:
        try:
            worktree_path = await asyncio.to_thread(_create_worktree, repo_root, branch_name)
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
            ["git", "rev-parse", "HEAD"], capture_output=True, text=True, timeout=5, cwd=work_dir
        )
        pre_sha = pre_sha_r.stdout.strip() if pre_sha_r.returncode == 0 else None
    except Exception:
        pre_sha = None
    pre_diff = await asyncio.to_thread(_git_snapshot, work_dir)

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
        _log_event(workflow_id, "provider_selected", provider=resolved_provider, attempt=len(_attempted))
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
            effective_task,
        ]

        _run_start = _time.monotonic()
        proc = await asyncio.create_subprocess_exec(
            *cmd,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
            cwd=work_dir,
            env={**os.environ, "RIBOSOME_PROVIDER": harness or resolved_provider, "HOME": str(Path.home())},
            start_new_session=True,  # process group kill — prevents orphan ribosome processes
        )
        _log_event(workflow_id, "subprocess_started", pid=proc.pid)

        stdout_counter: list[int] = [0]  # mutable counter shared with heartbeat

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
                    "\n" + "=" * 60 + "\n"
                    + "[" + _time.strftime("%Y-%m-%dT%H:%M:%S") + "] "
                    + "provider=" + resolved_provider + "\n"
                    + "task=" + task[:120] + "\n"
                    + "=" * 60 + "\n"
                )
                log_fh.write(_hdr.encode())
                log_fh.flush()

        stdout_task = asyncio.create_task(
            _tee_stream(proc.stdout, log_fh, "stdout", counter=stdout_counter)
        )
        stderr_task = asyncio.create_task(
            _tee_stream(proc.stderr, log_fh, "stderr")
        )

        _skip_stall = mode in ("scout", "research")
        hb_task = asyncio.create_task(
            _heartbeat_stall_check(
                proc, work_dir, provider, task,
                skip_stall=_skip_stall,
                stdout_counter=stdout_counter,
                workflow_id=workflow_id,
            )
        )
        try:
            try:
                stdout_bytes, stderr_bytes = await asyncio.wait_for(
                    asyncio.gather(stdout_task, stderr_task),
                    timeout=_ACTIVITY_TIMEOUT.total_seconds(),
                )
                try:
                    await asyncio.wait_for(proc.wait(), timeout=10)
                except TimeoutError:
                    await _graceful_kill(proc)
            except TimeoutError:
                await _graceful_kill(proc)
                with contextlib.suppress(Exception):
                    await asyncio.wait_for(
                        asyncio.gather(stdout_task, stderr_task, return_exceptions=True),
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
                }
                finalize_trace(_trace, _r)
                return _r
            except asyncio.CancelledError:
                # Temporal cancelled the activity (stall-detect kill or workflow cancel).
                # Capture whatever output we can before re-raising.
                await _graceful_kill(proc)
                try:
                    stdout_bytes, stderr_bytes = await asyncio.wait_for(
                        asyncio.gather(stdout_task, stderr_task, return_exceptions=True),
                        timeout=5,
                    )
                    if isinstance(stdout_bytes, BaseException):
                        stdout_bytes = b""
                    if isinstance(stderr_bytes, BaseException):
                        stderr_bytes = b"cancelled"
                except Exception:
                    stdout_bytes, stderr_bytes = b"", b"cancelled"
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
                    "stdout": stdout_bytes.decode(errors="replace")[:1000],
                    "stderr": f"cancelled: {stderr_bytes.decode(errors='replace')[:500]}",
                }
                finalize_trace(_trace, _r)
                return _r
        finally:
            hb_task.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await hb_task
            if log_fh:
                with contextlib.suppress(OSError):
                    log_fh.close()
            _active_count[resolved_provider] = max(0, _active_count.get(resolved_provider, 0) - 1)

        rc = proc.returncode or 0
        if rc != 0 and worktree_path:
            _cleanup_worktree(str(worktree_path))
        # Always attempt auto-commit — GLM often exits non-zero (test failure,
        # lint, timeout) after producing good code. The function checks for
        # dirty tree + non-empty diff internally, so it's safe unconditionally.
        if work_dir:
            _auto_commit(str(work_dir), wf_id)
        stdout = stdout_bytes.decode(errors="replace")
        stderr = stderr_bytes.decode(errors="replace")

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
                capture_output=True, text=True, timeout=5, cwd=work_dir,
            )
            _post_head = _head_r.stdout.strip() if _head_r.returncode == 0 else None
        except Exception:
            _post_head = None

        if _post_head and _post_head != pre_sha:
            _test_paths = _extract_test_paths(task)
            _tests_pass = True
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

            if _tests_pass:
                if worktree_path:
                    _subprocess.run(
                        ["git", "push", "origin", f"{branch_name}:{branch_name}"],
                        check=False, timeout=60, cwd=repo_root,
                    )
                    with contextlib.suppress(Exception):
                        _subprocess.run(
                            ["git", "worktree", "remove", "--force", worktree_path],
                            capture_output=True, timeout=10, cwd=repo_root,
                        )
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
                    "stdout": stdout[:1000],
                    "stderr": stderr[:500],
                    "pre_diff": pre_diff,
                    "post_diff": {"stat": "", "numstat": "", "commits": [], "commit_count": 0, "patch": ""},
                    "cost_info": "",
                    "output_path": "",
                    "branch_name": branch_name if worktree_path else "",
                    "merged": False,
                    "mode": mode,
                    "verdict": "early_exit_clean",
                    "post_head": _post_head,
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
                capture_output=True, text=True, timeout=5, cwd=work_dir,
            )
            current_sha = head_r.stdout.strip() if head_r.returncode == 0 else None
            if current_sha and current_sha != pre_sha:
                # Count actual commits between pre_sha and HEAD
                count_r = _subprocess.run(
                    ["git", "rev-list", "--count", f"{pre_sha}..HEAD"],
                    capture_output=True, text=True, timeout=5, cwd=work_dir,
                )
                real_count = int(count_r.stdout.strip()) if count_r.returncode == 0 else 1
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
        with contextlib.suppress(Exception):
            _subprocess.run(
                ["git", "worktree", "remove", "--force", worktree_path],
                capture_output=True,
                timeout=10,
                cwd=repo_root,
            )
        if is_incomplete:
            print(
                f"INCOMPLETE: branch {branch_name} preserved ({commit_count} commits)",
                file=sys.stderr,
            )

    cost_info = ""
    for line in stdout.splitlines()[-10:]:
        if any(k in line.lower() for k in ["token", "cost", "usage", "input:", "output:"]):
            cost_info += line + "\n"

    task_id_match = _re.search(r"\[t-([0-9a-fA-F]+)\]", task)
    tid_str = task_id_match.group(1) if task_id_match else _time.strftime("%H%M%S")
    out_path = ""
    try:
        OUTPUT_DIR.mkdir(parents=True, exist_ok=True)
        out_file = OUTPUT_DIR / f"{_time.strftime('%Y%m%d')}-{tid_str}.txt"
        out_text = (
            f"Task: {task}\nProvider: {provider}\nExit: {rc}\n\n"
            f"--- stdout ---\n{stdout}\n\n--- stderr ---\n{stderr}\n\n"
            f"--- diff ---\n{post_diff.get('stat', '')}\n"
        )
        if is_incomplete:
            out_text += f"\nBranch preserved for re-dispatch: {branch_name}\n"
        # Preserve full patch when rejected or incomplete so work is recoverable
        if rc != 0 or not merged:
            out_text += f"\n\n--- full patch (recoverable) ---\n{post_diff.get('patch', '')}\n"
        out_file.write_text(out_text)
        out_path = str(out_file)
    except OSError:
        pass

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
        "stdout": stdout[:1000],
        "stderr": stderr[:500],
        "pre_diff": pre_diff,
        "post_diff": post_diff,
        "cost_info": cost_info[:500],
        "output_path": out_path,
        "branch_name": branch_name if worktree_path else "",
        "merged": merged,
        "mode": mode,
    }
    finalize_trace(_trace, _r)
    return _r








@activity.defn
async def merge_approved(args: dict) -> dict:
    """Merge an approved branch to main + push. Called by workflow after chaperone approves."""
    repo_root = args["repo_root"]
    branch_name = args["branch_name"]
    merged = await asyncio.to_thread(_merge_branch, repo_root, branch_name)
    if merged:
        await asyncio.to_thread(_git_push, repo_root)
    return {"merged": merged, "branch_name": branch_name}




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

    # Deferred import to avoid circular dependency with workflow.py
    from mtor.worker.workflow import TranslationWorkflow, WatchWorkflow

    host = os.getenv("TEMPORAL_HOST", "ganglion:7233")
    client = await Client.connect(host)
    # Only count providers that have API keys configured
    max_concurrent = sum(
        limit for provider, limit in PROVIDER_LIMITS.items()
        if os.environ.get(f"{provider.upper()}_API_KEY")
    ) or 2  # default to 2 if no keys detected (op injects them later)

    worker = Worker(
        client=client,
        task_queue=TASK_QUEUE,
        workflows=[TranslationWorkflow, WatchWorkflow],
        activities=[translate, chaperone, merge_approved, create_pr, watch_cycle],
        max_concurrent_activities=max_concurrent,
    )
    _gc_worktrees(str(Path.home() / "germline"))
    print(f"Translocase started on queue '{TASK_QUEUE}' (max_concurrent={max_concurrent})")
    await worker.run()


if __name__ == "__main__":
    asyncio.run(main())
