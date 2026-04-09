#!/usr/bin/env python3
"""Translocase -- Temporal worker (eEF2) for the polysome translation system.

Polls the 'translation-queue' task queue and executes ribosome commands as activities.

Usage:
    python translocase.py
    python translocase.py --help
"""

import asyncio
import contextlib
import fcntl as _fcntl
import json
import os
import re as _re
import subprocess as _subprocess
import sys
import time as _time
from datetime import timedelta
from pathlib import Path

from temporalio import activity
from temporalio.client import Client
from temporalio.worker import Worker

from mtor.worker.provider import (
    EXIT_RATE_LIMITED,
    PROVIDER_PRIORITY,
    load_health,
    parse_rate_limit_window,
    save_health,
    select_provider,
    update_health,
)
from mtor.worker.stall_trace import create_task_trace, finalize_trace

TASK_QUEUE = "translation-queue"
RIBOSOME_SCRIPT = Path.home() / "germline" / "effectors" / "ribosome"
REVIEW_LOG = Path.home() / "germline" / "loci" / "ribosome-reviews.jsonl"
OUTPUT_DIR = Path.home() / "germline" / "loci" / "ribosome-outputs"
LOG_DIR = Path.home() / "code" / "mtor" / "logs"

PROVIDER_LIMITS = {
    "zhipu": 2,
    "infini": 2,
    "volcano": 2,
    "gemini": 2,
    "codex": 2,
}

# Serialize merges so concurrent ribosomes queue instead of racing
_MERGE_LOCK_PATH = Path.home() / "germline" / ".worktrees" / ".merge.lock"

# Accept branch version on conflict -- lockfiles get regenerated
_LOCKFILE_NAMES = {"uv.lock", "package-lock.json", "yarn.lock", "pnpm-lock.yaml", "Cargo.lock"}

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


def _detect_repo(task: str, default: str) -> str:
    """Detect target repo from task prompt, falling back to default."""
    match = _re.search(r"~/code/[\w.-]+", task)
    if not match:
        return default
    candidate = Path(match.group()).expanduser()
    for d in [candidate] + list(candidate.parents):
        if (d / ".git").is_dir():
            print(f"[translocase] detected target repo: {d}", file=sys.stderr)
            return str(d)
    return default


def _git_snapshot(cwd: str | None = None, *, base_sha: str | None = None) -> dict:
    """Capture git diff stat + numstat + commit list + full patch for review.

    When ``base_sha`` is provided and ``main..HEAD`` yields nothing (worktree
    creation failed, ribosome committed directly on main), falls back to
    ``{base_sha}..HEAD`` so the actual work is still captured.
    """
    work_dir = cwd or str(Path.home() / "germline")
    empty_result = {"stat": "", "numstat": "", "commits": [], "commit_count": 0, "patch": ""}
    try:
        diff_range = "main..HEAD"
        fallback = False

        stat = _subprocess.run(
            ["git", "diff", "--stat", diff_range],
            capture_output=True, text=True, timeout=10, cwd=work_dir,
        )
        commits_r = _subprocess.run(
            ["git", "log", "--oneline", diff_range],
            capture_output=True, text=True, timeout=10, cwd=work_dir,
        )
        commit_lines = [ln.strip() for ln in commits_r.stdout.strip().splitlines() if ln.strip()]

        # Fallback: main..HEAD is empty but base_sha was recorded before execution
        if not commit_lines and not stat.stdout.strip() and base_sha:
            fb_range = f"{base_sha}..HEAD"
            fb_stat = _subprocess.run(
                ["git", "diff", "--stat", fb_range],
                capture_output=True, text=True, timeout=10, cwd=work_dir,
            )
            fb_commits = _subprocess.run(
                ["git", "log", "--oneline", fb_range],
                capture_output=True, text=True, timeout=10, cwd=work_dir,
            )
            fb_lines = [ln.strip() for ln in fb_commits.stdout.strip().splitlines() if ln.strip()]
            if fb_lines or fb_stat.stdout.strip():
                diff_range = fb_range
                stat = fb_stat
                commits_r = fb_commits
                commit_lines = fb_lines
                fallback = True

        numstat = _subprocess.run(
            ["git", "diff", "--numstat", diff_range],
            capture_output=True, text=True, timeout=10, cwd=work_dir,
        )
        patch_r = _subprocess.run(
            ["git", "diff", diff_range],
            capture_output=True, text=True, timeout=10, cwd=work_dir,
        )
        result = {
            "stat": stat.stdout[:2000],
            "numstat": numstat.stdout[:2000],
            "commits": commit_lines,
            "commit_count": len(commit_lines),
            "patch": patch_r.stdout[:5000],
        }
        if fallback:
            result["fallback"] = True
        return result
    except Exception as exc:
        print(f"WARNING: _git_snapshot failed in {work_dir}: {exc}", file=sys.stderr)
        return empty_result


def _git_pull_ff_only(repo_root: str) -> None:
    """Pull latest so CC-written test files are available before ribosome runs."""
    try:
        result = _subprocess.run(
            ["git", "pull", "--ff-only"],
            capture_output=True,
            text=True,
            timeout=15,
            cwd=repo_root,
        )
        if result.returncode != 0:
            print(f"WARNING: git pull --ff-only failed: {result.stderr.strip()}", file=sys.stderr)
    except _subprocess.TimeoutExpired:
        print("WARNING: git pull --ff-only timed out", file=sys.stderr)
    except Exception as exc:
        print(f"WARNING: git pull --ff-only error: {exc}", file=sys.stderr)


def _git_push(repo_root: str) -> None:
    """Push ribosome commits so soma can pull without manual intervention."""
    try:
        result = _subprocess.run(
            ["git", "push"],
            capture_output=True,
            text=True,
            timeout=30,
            cwd=repo_root,
        )
        if result.returncode != 0:
            print(f"WARNING: git push failed: {result.stderr.strip()}", file=sys.stderr)
    except _subprocess.TimeoutExpired:
        print("WARNING: git push timed out", file=sys.stderr)
    except Exception as exc:
        print(f"WARNING: git push error: {exc}", file=sys.stderr)


def _create_worktree(repo_root: str, branch_name: str, retries: int = 3) -> str:
    """Create a git worktree for isolated ribosome execution. Returns worktree path.

    Retries with exponential backoff to handle git index.lock contention
    from concurrent ribosome dispatches on the same repo.
    """
    worktree_base = os.path.join(repo_root, ".worktrees")
    os.makedirs(worktree_base, exist_ok=True)
    worktree_path = os.path.join(worktree_base, branch_name)

    if os.path.exists(worktree_path):
        _subprocess.run(
            ["git", "worktree", "remove", "--force", worktree_path],
            capture_output=True,
            timeout=10,
            cwd=repo_root,
        )

    # Delete stale branch if it exists from a prior failed attempt
    _subprocess.run(
        ["git", "branch", "-D", branch_name],
        capture_output=True,
        timeout=5,
        cwd=repo_root,
    )

    last_err = ""
    for attempt in range(retries):
        result = _subprocess.run(
            ["git", "worktree", "add", "-b", branch_name, worktree_path, "HEAD"],
            capture_output=True,
            text=True,
            timeout=15,
            cwd=repo_root,
        )
        if result.returncode == 0:
            return worktree_path
        last_err = result.stderr.strip()
        if attempt < retries - 1:
            delay = (attempt + 1) * 2  # 2s, 4s
            print(
                f"worktree add attempt {attempt + 1} failed ({last_err}), "
                f"retrying in {delay}s",
                file=sys.stderr,
            )
            _time.sleep(delay)
    raise RuntimeError(f"worktree add failed after {retries} attempts: {last_err}")


def _merge_worktree(repo_root: str, branch_name: str, worktree_path: str) -> bool:
    """Merge worktree branch into main under exclusive file lock.

    FF when possible, 3-way merge otherwise. Lockfile conflicts auto-resolved
    (accept branch version). Code conflicts abort cleanly, leaving the branch.
    Worktree always removed; branch deleted only on success.
    """
    _MERGE_LOCK_PATH.parent.mkdir(parents=True, exist_ok=True)
    lock_fd = open(_MERGE_LOCK_PATH, "w")
    delete_branch = False
    try:
        _fcntl.flock(lock_fd, _fcntl.LOCK_EX)

        check = _subprocess.run(
            ["git", "log", "--oneline", f"main..{branch_name}"],
            capture_output=True,
            text=True,
            timeout=10,
            cwd=repo_root,
        )
        if not check.stdout.strip():
            delete_branch = True
            return True

        # Try FF first (zero overhead when no contention)
        merge = _subprocess.run(
            ["git", "merge", "--ff-only", branch_name],
            capture_output=True,
            text=True,
            timeout=15,
            cwd=repo_root,
        )
        if merge.returncode == 0:
            delete_branch = True
            return True

        # FF failed -- real 3-way merge
        merge = _subprocess.run(
            ["git", "merge", "--no-ff", "--no-edit", branch_name],
            capture_output=True,
            text=True,
            timeout=30,
            cwd=repo_root,
        )
        if merge.returncode == 0:
            delete_branch = True
            return True

        # Categorise conflicts
        conflicted = _subprocess.run(
            ["git", "diff", "--name-only", "--diff-filter=U"],
            capture_output=True,
            text=True,
            timeout=10,
            cwd=repo_root,
        )
        conflicted_files = [f.strip() for f in conflicted.stdout.splitlines() if f.strip()]
        lockfiles = [f for f in conflicted_files if Path(f).name in _LOCKFILE_NAMES]
        code_files = [f for f in conflicted_files if Path(f).name not in _LOCKFILE_NAMES]

        for lockfile in lockfiles:
            _subprocess.run(
                ["git", "checkout", "--theirs", lockfile],
                capture_output=True,
                timeout=10,
                cwd=repo_root,
            )
            _subprocess.run(
                ["git", "add", lockfile],
                capture_output=True,
                timeout=10,
                cwd=repo_root,
            )

        if not code_files:
            commit = _subprocess.run(
                ["git", "commit", "--no-edit"],
                capture_output=True,
                text=True,
                timeout=15,
                cwd=repo_root,
            )
            if commit.returncode == 0:
                delete_branch = True
                return True
            _subprocess.run(
                ["git", "merge", "--abort"], capture_output=True, timeout=10, cwd=repo_root
            )
            print(
                f"ERROR: merge commit failed for {branch_name}: {commit.stderr.strip()}",
                file=sys.stderr,
            )
            return False

        # Code conflicts -- abort, leave branch for inspection
        _subprocess.run(
            ["git", "merge", "--abort"], capture_output=True, timeout=10, cwd=repo_root
        )
        conflict_list = ", ".join(code_files[:5])
        print(f"CONFLICT: {branch_name} has code conflicts: {conflict_list}", file=sys.stderr)
        return False

    except Exception as exc:
        print(f"ERROR: merge error for {branch_name}: {exc}", file=sys.stderr)
        with contextlib.suppress(Exception):
            _subprocess.run(
                ["git", "merge", "--abort"], capture_output=True, timeout=10, cwd=repo_root
            )
        return False
    finally:
        _fcntl.flock(lock_fd, _fcntl.LOCK_UN)
        lock_fd.close()
        with contextlib.suppress(Exception):
            _subprocess.run(
                ["git", "worktree", "remove", "--force", worktree_path],
                capture_output=True,
                timeout=10,
                cwd=repo_root,
            )
        if delete_branch:
            with contextlib.suppress(Exception):
                _subprocess.run(
                    ["git", "branch", "-D", branch_name],
                    capture_output=True,
                    timeout=10,
                    cwd=repo_root,
                )


def _detect_prior_commits(
    repo_root: str, time_window_minutes: int = 40, author: str = "ribosome"
) -> list[str]:
    """Find recent commits from a prior killed attempt so retries can resume."""
    try:
        result = _subprocess.run(
            [
                "git",
                "log",
                "--oneline",
                f"--since={time_window_minutes} minutes ago",
                f"--author={author}",
            ],
            capture_output=True,
            text=True,
            timeout=10,
            cwd=repo_root,
        )
        return [line.strip() for line in result.stdout.strip().splitlines() if line.strip()]
    except Exception:
        return []


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
            proc.kill()
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
        if len(recent_hashes) > stall_oscillation_threshold + 1:
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
            if empty_ticks >= 60:
                print(
                    f"[stall-detect] empty diff + stagnant stdout timeout at tick {tick} "
                    f"({empty_ticks} empty ticks, ~{empty_ticks * 30 // 60}min), "
                    f"killing process (pid={proc.pid})",
                    file=sys.stderr,
                )
                proc.kill()
                return
            if empty_ticks >= 40:
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
                proc.kill()
                return


@activity.defn
async def translate(task: str, provider: str, mode: str = "build", repo: str | None = None) -> dict:
    """Execute a single ribosome task as a subprocess."""
    # Capability gate: reject tasks containing blocked keywords
    task_upper = task.upper()
    for keyword in _CAPABILITY_BLOCKLIST:
        if keyword.upper() in task_upper:
            return {
                "success": False,
                "exit_code": -1,
                "provider": provider,
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
    workflow_id = activity.info().workflow_id
    _trace = create_task_trace(task, provider, workflow_id)

    # Use structured repo parameter when provided; fall back to prompt mining
    if repo:
        repo_root = repo
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
            print(
                f"WARNING: worktree creation failed ({exc}), falling back to repo root",
                file=sys.stderr,
            )
            work_dir = repo_root
            worktree_path = None

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
        # Skip providers we've already tried; select next available
        available = [p for p in PROVIDER_PRIORITY if p not in _attempted]
        if available:
            # Build a temporary health view that pretends unchecked providers are closed
            tmp_health = {p: health.get(p, {"state": "closed"}) for p in available}
            resolved_provider = select_provider(tmp_health, override=None)

        _attempted.add(resolved_provider)
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

        start_time = _time.time()
        _run_start = _time.monotonic()
        proc = await asyncio.create_subprocess_exec(
            *cmd,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
            cwd=work_dir,
            env={**os.environ, "RIBOSOME_PROVIDER": resolved_provider, "HOME": str(Path.home())},
        )

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
                    proc.kill()
            except TimeoutError:
                proc.kill()
                with contextlib.suppress(Exception):
                    await asyncio.wait_for(
                        asyncio.gather(stdout_task, stderr_task, return_exceptions=True),
                        timeout=5,
                    )
                _r = {
                    "success": False,
                    "exit_code": -1,
                    "provider": provider,
                    "task": task[:200],
                    "stdout": "",
                    "stderr": "timeout after 30m",
                }
                finalize_trace(_trace, _r)
                return _r
            except asyncio.CancelledError:
                # Temporal cancelled the activity (stall-detect kill or workflow cancel).
                # Capture whatever output we can before re-raising.
                proc.kill()
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
                    "provider": provider,
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

        rc = proc.returncode or 0
        stdout = stdout_bytes.decode(errors="replace")
        stderr = stderr_bytes.decode(errors="replace")

        # Write per-attempt JSON summary to logs/<workflow_id>.jsonl
        try:
            _duration = _time.monotonic() - _run_start
            _ds_r = _subprocess.run(
                ["git", "diff", "--shortstat", "main..HEAD"],
                capture_output=True, text=True, timeout=10, cwd=work_dir,
            )
            _ds_text = _ds_r.stdout.strip() if _ds_r.returncode == 0 else ""
            _added, _removed = 0, 0
            for _part in _ds_text.split(","):
                _part = _part.strip()
                _m = _re.search(r"(\d+) insertion", _part)
                if _m:
                    _added = int(_m.group(1))
                _m = _re.search(r"(\d+) deletion", _part)
                if _m:
                    _removed = int(_m.group(1))
            _wf_id = activity.info().workflow_id
            LOG_DIR.mkdir(parents=True, exist_ok=True)
            with open(LOG_DIR / f"{_wf_id}.jsonl", "a") as _f:
                _f.write(json.dumps({
                    "workflow_id": _wf_id,
                    "provider": resolved_provider,
                    "exit_code": rc,
                    "duration_seconds": round(_duration, 2),
                    "diff_stat": {"added": _added, "removed": _removed},
                    "stdout_bytes": len(stdout_bytes),
                    "stderr_bytes": len(stderr_bytes),
                    "timestamp": _time.strftime("%Y-%m-%dT%H:%M:%S"),
                }) + "\n")
        except Exception:
            pass

        # Update provider health state and persist
        window_hours = parse_rate_limit_window(stderr)
        update_health(resolved_provider, rc, health, window_hours)
        save_health(health)

        # Write per-attempt JSON summary
        try:
            diff_stat = {"added": 0, "removed": 0}
            numstat_r = _subprocess.run(
                ["git", "diff", "--numstat", "main..HEAD"],
                capture_output=True, text=True, timeout=10, cwd=work_dir,
            )
            for _line in numstat_r.stdout.strip().splitlines():
                _parts = _line.split("\t")
                if len(_parts) >= 2:
                    try:
                        diff_stat["added"] += int(_parts[0]) if _parts[0] != "-" else 0
                        diff_stat["removed"] += int(_parts[1]) if _parts[1] != "-" else 0
                    except ValueError:
                        pass

            summary = {
                "workflow_id": workflow_id,
                "provider": resolved_provider,
                "exit_code": rc,
                "duration_seconds": round(_time.time() - start_time, 2),
                "diff_stat": diff_stat,
                "stdout_bytes": len(stdout_bytes),
                "stderr_bytes": len(stderr_bytes),
                "timestamp": _time.strftime("%Y-%m-%dT%H:%M:%S"),
            }
            LOG_DIR.mkdir(parents=True, exist_ok=True)
            log_file = LOG_DIR / f"{workflow_id}.jsonl"
            with open(log_file, "a") as f:
                f.write(json.dumps(summary) + "\n")
        except Exception as exc:
            print(f"WARNING: failed to write attempt summary: {exc}", file=sys.stderr)

        # Retry on rate-limit exit: circuit-trip this provider, select next, re-run
        if rc == EXIT_RATE_LIMITED:
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
                    "task": task[:200],
                    "stdout": stdout[:1000],
                    "stderr": f"All providers rate-limited: {sorted(_attempted)}",
                }
                finalize_trace(_trace, _r)
                return _r
            continue

    # SRP defer detection: supervised mode returns JSON with stop_reason
    if is_supervised and rc == 0:
        with contextlib.suppress(Exception):
            import json as _json

            output_json = _json.loads(stdout)
            if output_json.get("stop_reason") == "tool_deferred":
                _r = {
                    "success": False,
                    "exit_code": 0,
                    "provider": provider,
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
        "provider": provider,
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


_DESTRUCTION_PATTERNS = _re.compile(
    r"rm -rf|rmdir|replaced entire|overwrote|deleted all|"
    r"file is now empty|wrote 0 bytes|No such file",
    _re.IGNORECASE,
)

_ERROR_PATTERNS = _re.compile(
    r"SyntaxError|ImportError|ModuleNotFoundError|PermissionError|"
    r"Traceback \(most recent|panic:|fatal:",
    _re.IGNORECASE,
)


def _merge_branch(repo_root: str, branch_name: str) -> bool:
    """Merge a branch into main under exclusive lock. FF then 3-way.

    Extracted from _merge_worktree -- same merge logic, no worktree handling.
    Deletes branch on successful merge.
    """
    _MERGE_LOCK_PATH.parent.mkdir(parents=True, exist_ok=True)
    lock_fd = open(_MERGE_LOCK_PATH, "w")
    delete_branch = False
    try:
        _fcntl.flock(lock_fd, _fcntl.LOCK_EX)
        check = _subprocess.run(
            ["git", "log", "--oneline", f"main..{branch_name}"],
            capture_output=True,
            text=True,
            timeout=10,
            cwd=repo_root,
        )
        if not check.stdout.strip():
            delete_branch = True
            return True
        merge = _subprocess.run(
            ["git", "merge", "--ff-only", branch_name],
            capture_output=True,
            text=True,
            timeout=15,
            cwd=repo_root,
        )
        if merge.returncode == 0:
            delete_branch = True
            return True
        merge = _subprocess.run(
            ["git", "merge", "--no-ff", "--no-edit", branch_name],
            capture_output=True,
            text=True,
            timeout=30,
            cwd=repo_root,
        )
        if merge.returncode == 0:
            delete_branch = True
            return True
        # Conflicts -- abort
        _subprocess.run(
            ["git", "merge", "--abort"], capture_output=True, timeout=10, cwd=repo_root
        )
        print(f"CONFLICT: {branch_name} has conflicts, leaving branch", file=sys.stderr)
        return False
    except Exception as exc:
        print(f"ERROR: merge error for {branch_name}: {exc}", file=sys.stderr)
        with contextlib.suppress(Exception):
            _subprocess.run(
                ["git", "merge", "--abort"], capture_output=True, timeout=10, cwd=repo_root
            )
        return False
    finally:
        _fcntl.flock(lock_fd, _fcntl.LOCK_UN)
        lock_fd.close()
        if delete_branch:
            with contextlib.suppress(Exception):
                _subprocess.run(
                    ["git", "branch", "-D", branch_name],
                    capture_output=True,
                    timeout=10,
                    cwd=repo_root,
                )


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
_PLACEHOLDER_PATTERNS = _re.compile(r"\bTODO\b|\bFIXME\b|\bstub\b", _re.IGNORECASE)
_HARDCODED_HOME = _re.compile(r"/Users/terry/|/home/terry/")
_PY2_EXCEPT = _re.compile(r"^\s*except\s+\w+\s*,\s*\w+\s*:", _re.MULTILINE)
_DUPE_FUTURE = _re.compile(r"from\s+__future__\s+import\s+annotations")


@activity.defn
async def chaperone(result: dict) -> dict:
    """Review a ribosome task result for quality signals.

    Returns {"approved": bool, "flags": [...], "verdict": str}.
    """
    task = result.get("task", "")
    provider = result.get("provider", "")
    stdout = result.get("stdout", "")
    stderr = result.get("stderr", "")
    exit_code = result.get("exit_code", -1)
    combined = f"{stdout}\n{stderr}"

    flags: list[str] = []

    if exit_code != 0:
        flags.append(f"exit_code={exit_code}")

    destruction_hits = _DESTRUCTION_PATTERNS.findall(combined)
    if destruction_hits:
        flags.append(f"destruction: {', '.join(list(set(destruction_hits))[:3])}")

    error_hits = _ERROR_PATTERNS.findall(combined)
    if error_hits:
        flags.append(f"errors: {', '.join(list(set(error_hits))[:3])}")

    # Coaching-promoted checks (deterministic, formerly prose-only)
    placeholder_hits = _PLACEHOLDER_PATTERNS.findall(combined)
    if placeholder_hits:
        flags.append(f"placeholders: {', '.join(list(set(placeholder_hits))[:3])}")

    if _HARDCODED_HOME.search(combined):
        flags.append("hardcoded_home_path")

    if _PY2_EXCEPT.search(combined):
        flags.append("py2_except_syntax")

    # Check for duplicate `from __future__ import annotations` per file in output
    future_count = len(_DUPE_FUTURE.findall(combined))
    if future_count > 1:
        flags.append(f"dupe_future_import: {future_count} occurrences")

    task_words = len(task.split())
    output_words = len(stdout.split())
    if task_words > 20 and output_words < 10 and exit_code == 0:
        flags.append(f"thin_output: {output_words} words for {task_words}-word task")

    if exit_code == 0 and len(stdout.strip()) < 5:
        flags.append("empty_stdout_on_success")

    # GLM ran to completion but committed nothing -- likely no-op
    post_stat_text = (
        result.get("post_diff", {}).get("stat", "")
        if isinstance(result.get("post_diff"), dict)
        else ""
    )
    commit_count = (
        result.get("post_diff", {}).get("commit_count", 0)
        if isinstance(result.get("post_diff"), dict)
        else 0
    )
    branch_name = result.get("branch_name", "")

    # Test files must be in assays/ flat (not nested subdirectories)
    if post_stat_text:
        for line in post_stat_text.splitlines():
            fname = line.strip().split("|")[0].strip() if "|" in line else line.strip()
            if fname.startswith("assays/") and fname.count("/") > 1 and "test_" in fname:
                flags.append(f"nested_test_file: {fname}")

    if exit_code == 0 and not post_stat_text.strip() and commit_count == 0:
        flags.append("no_commit_on_success")

    # Extract ALL file paths mentioned in the task and check if they appear in the diff.
    # Catches "task mentions dispatch.py but diff only touches cli.py" mismatches.
    task_files = set(_re.findall(r"[\w./~-]+\.(?:py|sh|toml|md|yaml|yml|json)", task))
    # Filter out common false positives (URLs, example paths in code blocks)
    task_files = {f for f in task_files if not f.startswith("http") and len(f) > 4}
    if task_files and exit_code == 0 and post_stat_text:
        # Normalize: strip ~/ and leading path prefixes to match git diff short paths
        def _normalize(path: str) -> str:
            path = path.lstrip("~/")
            # Strip common prefixes that git diff won't show
            for prefix in ("germline/", "home/vivesca/germline/"):
                path = path.removeprefix(prefix)
            return path

        diff_files = set()
        for line in post_stat_text.splitlines():
            fname = line.strip().split("|")[0].strip() if "|" in line else ""
            if fname:
                diff_files.add(fname)

        for task_file in task_files:
            norm = _normalize(task_file)
            if (
                norm
                and not any(norm in df or df.endswith(norm) for df in diff_files)
                and any(
                    kw in task.lower()
                    for kw in ["modify", "edit", "change", "add to", "update", "fix", "create"]
                )
            ):
                flags.append(f"target_file_missing: {norm}")

    pre_diff = result.get("pre_diff", {})
    post_diff = result.get("post_diff", {})
    pre_numstat = pre_diff.get("numstat", "") if isinstance(pre_diff, dict) else ""
    post_numstat = post_diff.get("numstat", "") if isinstance(post_diff, dict) else ""
    post_stat = post_diff.get("stat", "") if isinstance(post_diff, dict) else str(post_diff)

    if post_numstat and post_numstat != pre_numstat:
        for line in post_numstat.splitlines():
            parts = line.split("\t")
            if len(parts) == 3:
                added, removed, fname = parts
                try:
                    a, r = int(added), int(removed)
                    if r > a * 3 and r > 10:
                        flags.append(f"file_shrunk: {fname} +{a}/-{r}")
                    if a == 0 and r > 5:
                        flags.append(f"pure_deletion: {fname} -{r}")
                except ValueError:
                    pass

    # Determine verdict: incomplete when work was done but process failed
    if exit_code != 0 and commit_count > 0:
        verdict = "incomplete"
        approved = False
    else:
        result_mode = result.get("mode", "build")
        if result_mode == "scout":
            # Scout tasks don't produce commits — different approval criteria
            approved = exit_code == 0 and not any(f.startswith("destruction") for f in flags)
            # Don't flag no_commit_on_success or empty_stdout_on_success for scout
            flags = [
                f for f in flags if f not in ("no_commit_on_success", "empty_stdout_on_success")
            ]
        else:
            approved = exit_code == 0 and not any(
                f.startswith("destruction") or f == "no_commit_on_success" for f in flags
            )
        verdict = "approved" if approved else "rejected"
        if flags and approved:
            verdict = "approved_with_flags"

    review = {
        "ts": _time.strftime("%Y-%m-%dT%H:%M:%S"),
        "task": task[:200],
        "provider": provider,
        "exit_code": exit_code,
        "flags": flags,
        "verdict": verdict,
        "stdout_len": len(stdout),
        "stderr_len": len(stderr),
        "diff": post_stat[:500] if post_stat else "",
        "cost_info": result.get("cost_info", ""),
    }
    if verdict == "incomplete" and branch_name:
        review["branch_name"] = branch_name

    try:
        REVIEW_LOG.parent.mkdir(parents=True, exist_ok=True)
        with open(REVIEW_LOG, "a") as f:
            f.write(json.dumps(review) + "\n")
    except OSError:
        pass

    # Satisfaction scoring: 0-100 based on objective signals
    score = 100

    # Major deductions
    if exit_code != 0:
        score -= 40
    if not post_stat_text.strip():
        score -= 30  # no changes
    if any(f.startswith("destruction") for f in flags):
        score -= 50

    # Moderate deductions
    if any("file_shrunk" in f for f in flags):
        score -= 20
    if any("thin_output" in f for f in flags):
        score -= 15
    if any("placeholders" in f for f in flags):
        score -= 10
    if any("errors" in f for f in flags):
        score -= 10

    # Bonuses
    if commit_count > 0 and exit_code == 0:
        score += 10  # actually committed
    if any("test" in f.lower() for f in post_stat_text.splitlines()):
        score += 5  # includes test files

    # Fallback diff bonus: main..HEAD was empty but base_sha..HEAD captured work
    if isinstance(post_diff, dict) and post_diff.get("fallback") and post_stat_text.strip():
        score += 10

    score = max(0, min(100, score))

    requeue_prompt = ""
    if verdict in ("rejected", "incomplete") and any("thin_output" in f for f in flags):
        requeue_prompt = task[:200] + " -- Be thorough. Read files before editing. Show your work."
    elif verdict in ("rejected", "incomplete") and any("file_shrunk" in f for f in flags):
        requeue_prompt = (
            task[:200]
            + " -- IMPORTANT: Read the entire file before modifying. Preserve ALL existing content."
        )

    return {
        "approved": approved,
        "flags": flags,
        "verdict": verdict,
        "satisfaction": score,
        "requeue_prompt": requeue_prompt,
    }


def _gc_worktrees(repo_root: str) -> None:
    """Remove orphaned ribosome worktrees older than 2 hours."""
    worktree_base = os.path.join(repo_root, ".worktrees")
    if not os.path.isdir(worktree_base):
        return
    for entry in os.listdir(worktree_base):
        if not entry.startswith("ribosome-"):
            continue
        wt_path = os.path.join(worktree_base, entry)
        try:
            age_seconds = _time.time() - os.path.getmtime(wt_path)
            if age_seconds < 7200:
                continue
        except OSError:
            continue
        print(f"[gc] removing orphaned worktree: {entry}", file=sys.stderr)
        with contextlib.suppress(Exception):
            _subprocess.run(
                ["git", "worktree", "remove", "--force", wt_path],
                capture_output=True,
                timeout=10,
                cwd=repo_root,
            )
        with contextlib.suppress(Exception):
            _subprocess.run(
                ["git", "branch", "-D", entry],
                capture_output=True,
                timeout=10,
                cwd=repo_root,
            )


async def main() -> None:
    if "--help" in sys.argv or "-h" in sys.argv:
        print(__doc__)
        sys.exit(0)

    # Deferred import to avoid circular dependency with workflow.py
    from mtor.worker.workflow import TranslationWorkflow, WatchWorkflow

    host = os.getenv("TEMPORAL_HOST", "ganglion:7233")
    client = await Client.connect(host)
    max_concurrent = sum(PROVIDER_LIMITS.values())

    worker = Worker(
        client=client,
        task_queue=TASK_QUEUE,
        workflows=[TranslationWorkflow, WatchWorkflow],
        activities=[translate, chaperone, merge_approved, watch_cycle],
        max_concurrent_activities=max_concurrent,
    )
    _gc_worktrees(str(Path.home() / "germline"))
    print(f"Translocase started on queue '{TASK_QUEUE}' (max_concurrent={max_concurrent})")
    await worker.run()


if __name__ == "__main__":
    asyncio.run(main())
