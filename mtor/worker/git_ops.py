"""Git/worktree helpers for the mtor translocase worker."""

from __future__ import annotations

import contextlib
import fcntl as _fcntl
import json as _json
import os
import re as _re
import subprocess as _subprocess
import sys
import time as _time
from pathlib import Path

# Serialize merges so concurrent ribosomes queue instead of racing.
_MERGE_LOCK_PATH = Path.home() / "germline" / ".worktrees" / ".merge.lock"
_AUTO_COMMIT_DENYLIST = (
    "loci/ribosome-dossiers/",
    "loci/ribosome-heartbeats/",
    "loci/ribosome-outputs/",
    "loci/ribosome-runs.jsonl",
)
_CHECKPOINT_DIR = Path.home() / ".local" / "share" / "vivesca" / "ribosome-checkpoints"

_GIT_ENV_ALLOWLIST = {
    "HOME",
    "PATH",
    "SHELL",
    "TERM",
    "USER",
    "LOGNAME",
    "SSH_AUTH_SOCK",
    "XDG_CONFIG_HOME",
    "XDG_CACHE_HOME",
    "XDG_DATA_HOME",
    "XDG_RUNTIME_DIR",
}


def _minimal_git_env() -> dict[str, str]:
    """Return a minimal environment for git auth via normal credential helpers."""
    return {
        key: value
        for key, value in os.environ.items()
        if key in _GIT_ENV_ALLOWLIST and value
    }


def _run_worker_command(command, *args, **kwargs):
    """Run subprocesses, filtering inherited runtime secrets for git commands."""
    if (
        isinstance(command, (list, tuple))
        and command
        and command[0] == "git"
        and "env" not in kwargs
    ):
        kwargs["env"] = _minimal_git_env()
    return _subprocess.run(command, *args, **kwargs)


def _status_paths_for_auto_commit(status_output: str) -> list[str]:
    """Return explicit, non-runtime paths from git porcelain output."""
    paths: list[str] = []
    for line in status_output.splitlines():
        if len(line) < 4:
            continue
        raw_path = line[3:]
        candidates = raw_path.split(" -> ") if " -> " in raw_path else [raw_path]
        for candidate in candidates:
            normalized = candidate.strip()
            if not normalized or any(
                normalized == denied.rstrip("/") or normalized.startswith(denied)
                for denied in _AUTO_COMMIT_DENYLIST
            ):
                continue
            paths.append(normalized)
    return paths


def _cleanup_worktree(work_dir: str) -> None:
    """Best-effort cleanup for failed ribosome worktree runs."""
    root = Path(work_dir)
    if not work_dir or not root.exists():
        return

    git_dir = root / ".git"
    with contextlib.suppress(OSError):
        (git_dir / "index.lock").unlink(missing_ok=True)

    for state_name in ("rebase-merge", "rebase-apply", "MERGE_HEAD"):
        state_path = git_dir / state_name
        if not state_path.exists():
            continue
        command = (
            ["git", "rebase", "--abort"]
            if "rebase" in state_name
            else ["git", "merge", "--abort"]
        )
        with contextlib.suppress(Exception):
            _run_worker_command(command, capture_output=True, cwd=work_dir, timeout=10)
        break

    for command in (["git", "checkout", "--", "."], ["git", "clean", "-fd"]):
        with contextlib.suppress(Exception):
            _run_worker_command(command, capture_output=True, cwd=work_dir, timeout=10)


def _auto_commit(repo_dir: str, workflow_id: str | None = None) -> bool:
    """Stage and commit pending changes in *repo_dir*.

    Returns True if a commit was created, False if the working tree was clean
    or the staged diff was empty (e.g. only whitespace changes).

    Skips pre-commit hooks (--no-verify) because the ganglion worktree
    environment may lack ruff/other tools in PATH. The chaperone review
    gate catches quality issues after the fact.
    """
    try:
        branch = _run_worker_command(
            ["git", "rev-parse", "--abbrev-ref", "HEAD"],
            cwd=repo_dir,
            capture_output=True,
            text=True,
            timeout=10,
        )
        branch_name = branch.stdout.strip()
    except (OSError, _subprocess.SubprocessError):
        branch_name = ""
    if branch_name in {"main", "master"}:
        print(
            "[auto-commit] WARNING refusing to commit on main/master", file=sys.stderr
        )
        return False

    run = _run_worker_command
    wf_label = workflow_id or "unknown"

    try:
        # 1. Check for dirty working tree
        status = run(
            ["git", "status", "--porcelain"],
            cwd=repo_dir,
            capture_output=True,
            text=True,
            timeout=10,
        )
        if not status.stdout.strip():
            return False

        # 2. Stage only explicit non-runtime paths. Never use git add -A here:
        # scout/read-only runs can dirty ribosome bookkeeping in the main repo.
        paths = _status_paths_for_auto_commit(status.stdout)
        if not paths:
            return False
        run(["git", "add", "--", *paths], cwd=repo_dir, check=True, timeout=10)

        # 3. Check if staged diff is empty (no substantive changes)
        diff = run(
            ["git", "diff", "--cached", "--quiet"],
            cwd=repo_dir,
            timeout=10,
        )
        if diff.returncode == 0:
            return False

        # 4. Commit — skip hooks (worktree may lack lint tools)
        run(
            ["git", "commit", "--no-verify", "-m", f"ribosome: {wf_label}"],
            cwd=repo_dir,
            check=True,
            timeout=30,
        )
        print(f"[auto-commit] committed dirty work for {wf_label}", file=sys.stderr)
        return True
    except Exception as exc:
        print(f"[auto-commit] failed for {wf_label}: {exc}", file=sys.stderr)
        return False


def _checkpoint_worktree(
    work_dir: str,
    workflow_id: str,
    *,
    task: str = "",
    provider: str = "",
    exit_code: int | None = None,
) -> str | None:
    """Stash and record dirty work before a worktree is reset or removed.

    The ribosome wrapper's own checkpoint only runs on clean nonzero exits —
    when the wrapper dies by signal (SIGKILL, OOM, collateral pkill) this is
    the only salvage path before _cleanup_worktree or a force-remove destroys
    the tree (2026-07-04: exit_code=-9 lost three uncommitted test files).
    Never raises; returns the stash sha (or patch path) on success, None when
    there was nothing to save.
    """
    try:
        root = Path(work_dir)
        if not work_dir or not root.exists():
            return None
        # A killed git process can leave a stale index.lock that would block
        # everything below. Worktree .git is a file — resolve the real dir.
        gd = _run_worker_command(
            ["git", "rev-parse", "--git-dir"],
            cwd=work_dir,
            capture_output=True,
            text=True,
            timeout=10,
        )
        if gd.returncode == 0 and gd.stdout.strip():
            git_dir = Path(gd.stdout.strip())
            if not git_dir.is_absolute():
                git_dir = root / git_dir
            with contextlib.suppress(OSError):
                (git_dir / "index.lock").unlink(missing_ok=True)

        status = _run_worker_command(
            ["git", "status", "--porcelain", "-uall"],
            cwd=work_dir,
            capture_output=True,
            text=True,
            timeout=10,
        )
        if status.returncode != 0 or not status.stdout.strip():
            return None
        paths = _status_paths_for_auto_commit(status.stdout)
        if not paths:
            return None

        wf_label = _re.sub(r"[^A-Za-z0-9._-]", "_", workflow_id or "unknown")
        # Stage explicit paths so untracked files are captured by the stash.
        with contextlib.suppress(Exception):
            _run_worker_command(["git", "add", "--", *paths], cwd=work_dir, timeout=30)

        stash_ref = ""
        stash = _run_worker_command(
            ["git", "stash", "create", f"ribosome checkpoint {wf_label}"],
            cwd=work_dir,
            capture_output=True,
            text=True,
            timeout=30,
        )
        if stash.returncode == 0:
            stash_ref = stash.stdout.strip()
        if stash_ref:
            # Anchor the dangling stash commit in the shared object store so
            # gc cannot reap it after the worktree is removed.
            with contextlib.suppress(Exception):
                _run_worker_command(
                    [
                        "git",
                        "update-ref",
                        f"refs/ribosome-checkpoints/{wf_label}",
                        stash_ref,
                    ],
                    cwd=work_dir,
                    timeout=10,
                )

        _CHECKPOINT_DIR.mkdir(parents=True, exist_ok=True)
        # Belt and braces: a plain patch survives even without the object store.
        patch_file = ""
        patch = _run_worker_command(
            ["git", "diff", "HEAD"],
            cwd=work_dir,
            capture_output=True,
            text=True,
            timeout=30,
        )
        if patch.returncode == 0 and patch.stdout.strip():
            patch_path = _CHECKPOINT_DIR / f"{wf_label}.patch"
            patch_path.write_text(patch.stdout)
            patch_file = str(patch_path)

        if not stash_ref and not patch_file:
            return None

        stat = _run_worker_command(
            ["git", "diff", "--stat", "HEAD"],
            cwd=work_dir,
            capture_output=True,
            text=True,
            timeout=30,
        )
        stat_tail = (
            stat.stdout.strip().splitlines()[-1]
            if stat.returncode == 0 and stat.stdout.strip()
            else ""
        )
        entry = {
            "workflow_id": workflow_id or "unknown",
            "timestamp": _time.strftime("%Y-%m-%dT%H:%M:%SZ", _time.gmtime()),
            "task": task[:100],
            "provider": provider,
            "exit_code": exit_code,
            "stash_ref": stash_ref,
            "diff_stat": stat_tail,
            "patch_file": patch_file,
            "source": "translocase",
        }
        (_CHECKPOINT_DIR / f"{wf_label}.json").write_text(_json.dumps(entry) + "\n")
        print(
            f"[checkpoint] saved uncommitted work for {wf_label}: "
            f"stash={stash_ref[:12] if stash_ref else 'none'} patch={patch_file or 'none'}",
            file=sys.stderr,
        )
        return stash_ref or patch_file
    except Exception as exc:
        print(f"[checkpoint] failed for {workflow_id}: {exc}", file=sys.stderr)
        return None


def _main_checkout_state(repo_root: str) -> dict:
    """Snapshot HEAD and dirty paths of the main checkout for confinement checks."""
    state: dict = {"head": "", "dirty_paths": []}
    try:
        head = _run_worker_command(
            ["git", "rev-parse", "HEAD"],
            cwd=repo_root,
            capture_output=True,
            text=True,
            timeout=10,
        )
        if head.returncode == 0:
            state["head"] = head.stdout.strip()
        status = _run_worker_command(
            ["git", "status", "--porcelain"],
            cwd=repo_root,
            capture_output=True,
            text=True,
            timeout=10,
        )
        if status.returncode == 0:
            state["dirty_paths"] = [
                line[3:].strip() for line in status.stdout.splitlines() if len(line) > 3
            ]
    except Exception:
        pass
    return state


def _detect_repo(task: str, default: str) -> str:
    """Detect target repo from task prompt, falling back to default."""
    match = _re.search(r"~/code/[\w.-]+", task)
    if not match:
        return default
    candidate = Path(match.group()).expanduser()
    code_root = Path.home() / "code"
    for d in [candidate] + list(candidate.parents):
        if d == code_root:
            break
        if (d / ".git").is_dir():
            print(f"[translocase] detected target repo: {d}", file=sys.stderr)
            return str(d)
    return default


def _format_landing_banner(
    repo_root: str,
    work_dir: str,
    branch_name: str,
    worktree_path: str | None,
    mode: str,
    pre_sha: str | None,
) -> str:
    """Return a one-line operator banner describing where a dispatch will land.

    Pure formatting so it can be unit-tested without a live Temporal/git
    environment. Never raises on a missing field -- scout runs and missing
    base SHAs degrade to literal markers rather than blowing up dispatch.
    """
    read_only = worktree_path is None or mode == "scout"
    if read_only:
        landing = "main (read-only scout)"
    else:
        landing = branch_name or "main (read-only scout)"
    base = "unknown" if not pre_sha else pre_sha[:12]
    return (
        f"[translocase] landing target: {repo_root} @ {landing} "
        f"(base {base}) [work_dir={work_dir}]"
    )


def _main_moved_off(work_dir: str, base_sha: str) -> bool:
    """True if the local ``main`` ref has advanced off the recorded base_sha.

    When the worker branched, ``main`` pointed at ``base_sha``. If ``git
    rev-parse main`` no longer equals ``base_sha``, a parallel session landed a
    commit (or a fast-forward merge moved ``main``) after the branch point, so
    ``main..HEAD`` is no longer the worker's true diff base. Returns False when
    ``main`` can't be resolved — don't force the base range on a guess.
    """
    try:
        r = _run_worker_command(
            ["git", "rev-parse", "--verify", "--quiet", "main"],
            capture_output=True,
            text=True,
            timeout=5,
            cwd=work_dir,
        )
    except Exception:
        return False
    if r.returncode != 0:
        return False
    return r.stdout.strip() != base_sha


def _git_snapshot(cwd: str | None = None, *, base_sha: str | None = None) -> dict:
    """Capture git diff stat + numstat + commit list + full patch for review.

    The diff base is ``main..HEAD`` in the normal case. But ``main`` is a shared
    worktree ref: if a parallel session lands a commit on ``main`` after the
    worker branched, ``main..HEAD`` becomes contaminated — files the parallel
    commit ADDED to main read as DELETIONS in the worker diff, raising false
    ``pure_deletion`` / ``file_shrunk`` flags downstream. So when ``base_sha`` is
    recorded and ``main`` has advanced off it, diff ``{base_sha}..HEAD`` instead
    — the worker's true scope. This also subsumes the worktree-fallback case
    where the ribosome committed directly on an advanced main and ``main..HEAD``
    is empty.

    See ``finding_chaperone_false_positive_parallel_commit_base_drift``.
    """
    work_dir = cwd or str(Path.home() / "germline")
    empty_result = {
        "stat": "",
        "numstat": "",
        "commits": [],
        "commit_count": 0,
        "patch": "",
    }
    try:
        diff_range = "main..HEAD"
        fallback = False

        # Base-drift guard: main has moved past the recorded branch point, so
        # diff against base_sha directly rather than the contaminated main..HEAD.
        if base_sha and _main_moved_off(work_dir, base_sha):
            diff_range = f"{base_sha}..HEAD"
            fallback = True

        stat = _run_worker_command(
            ["git", "diff", "--stat", diff_range],
            capture_output=True,
            text=True,
            timeout=10,
            cwd=work_dir,
        )
        commits_r = _run_worker_command(
            ["git", "log", "--oneline", diff_range],
            capture_output=True,
            text=True,
            timeout=10,
            cwd=work_dir,
        )
        commit_lines = [
            ln.strip() for ln in commits_r.stdout.strip().splitlines() if ln.strip()
        ]

        # Fallback: main..HEAD is empty but base_sha was recorded before execution
        # (main never diverged — e.g. main ref missing — but HEAD still has work).
        if not commit_lines and not stat.stdout.strip() and base_sha and not fallback:
            fb_range = f"{base_sha}..HEAD"
            fb_stat = _run_worker_command(
                ["git", "diff", "--stat", fb_range],
                capture_output=True,
                text=True,
                timeout=10,
                cwd=work_dir,
            )
            fb_commits = _run_worker_command(
                ["git", "log", "--oneline", fb_range],
                capture_output=True,
                text=True,
                timeout=10,
                cwd=work_dir,
            )
            fb_lines = [
                ln.strip()
                for ln in fb_commits.stdout.strip().splitlines()
                if ln.strip()
            ]
            if fb_lines or fb_stat.stdout.strip():
                diff_range = fb_range
                stat = fb_stat
                commits_r = fb_commits
                commit_lines = fb_lines
                fallback = True

        numstat = _run_worker_command(
            ["git", "diff", "--numstat", diff_range],
            capture_output=True,
            text=True,
            timeout=10,
            cwd=work_dir,
        )
        patch_r = _run_worker_command(
            ["git", "diff", diff_range],
            capture_output=True,
            text=True,
            timeout=10,
            cwd=work_dir,
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
        upstream = _run_worker_command(
            ["git", "rev-parse", "--abbrev-ref", "--symbolic-full-name", "@{u}"],
            capture_output=True,
            text=True,
            timeout=5,
            cwd=repo_root,
        )
        if upstream.returncode != 0:
            fetch = _run_worker_command(
                ["git", "fetch", "origin", "main"],
                capture_output=True,
                text=True,
                timeout=15,
                cwd=repo_root,
            )
            if fetch.returncode != 0:
                print(
                    f"WARNING: git fetch origin main failed: {fetch.stderr.strip()}",
                    file=sys.stderr,
                )
            return
        result = _run_worker_command(
            ["git", "pull", "--ff-only"],
            capture_output=True,
            text=True,
            timeout=15,
            cwd=repo_root,
        )
        if result.returncode != 0:
            print(
                f"WARNING: git pull --ff-only failed: {result.stderr.strip()}",
                file=sys.stderr,
            )
    except _subprocess.TimeoutExpired:
        print("WARNING: git pull --ff-only timed out", file=sys.stderr)
    except Exception as exc:
        print(f"WARNING: git pull --ff-only error: {exc}", file=sys.stderr)


def _git_push(repo_root: str) -> None:
    """Push ribosome commits so soma can pull without manual intervention."""
    try:
        result = _run_worker_command(
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
        _run_worker_command(
            ["git", "worktree", "remove", "--force", worktree_path],
            capture_output=True,
            timeout=10,
            cwd=repo_root,
        )

    # Delete stale branch if it exists from a prior failed attempt
    _run_worker_command(
        ["git", "branch", "-D", branch_name],
        capture_output=True,
        timeout=5,
        cwd=repo_root,
    )

    last_err = ""
    for attempt in range(retries):
        result = _run_worker_command(
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
    """Push worktree branch to origin for CC review. No auto-merge to main.

    Previously merged to main directly. Now pushes the branch so CC can
    review and merge via securin. Worktree always removed after push.
    """
    _MERGE_LOCK_PATH.parent.mkdir(parents=True, exist_ok=True)
    lock_fd = open(_MERGE_LOCK_PATH, "w")
    delete_branch = False
    try:
        _fcntl.flock(lock_fd, _fcntl.LOCK_EX)

        check = _run_worker_command(
            ["git", "log", "--oneline", f"main..{branch_name}"],
            capture_output=True,
            text=True,
            timeout=10,
            cwd=repo_root,
        )
        if not check.stdout.strip():
            delete_branch = True
            return True

        # Push branch to origin for CC review — no auto-merge
        push = _run_worker_command(
            ["git", "push", "origin", branch_name],
            capture_output=True,
            text=True,
            timeout=30,
            cwd=repo_root,
        )
        if push.returncode == 0:
            print(
                f"[merge] pushed branch {branch_name} to origin for review",
                file=sys.stderr,
            )
            # Don't delete branch — CC will merge and clean up
            return True

        print(
            f"[merge] push failed for {branch_name}: {push.stderr.strip()[:200]}",
            file=sys.stderr,
        )
        return False

    except Exception as exc:
        print(f"ERROR: push error for {branch_name}: {exc}", file=sys.stderr)
        return False
    finally:
        _fcntl.flock(lock_fd, _fcntl.LOCK_UN)
        lock_fd.close()
        with contextlib.suppress(Exception):
            _run_worker_command(
                ["git", "worktree", "remove", "--force", worktree_path],
                capture_output=True,
                timeout=10,
                cwd=repo_root,
            )
        if delete_branch:
            with contextlib.suppress(Exception):
                _run_worker_command(
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
        result = _run_worker_command(
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
        return [
            line.strip() for line in result.stdout.strip().splitlines() if line.strip()
        ]
    except Exception:
        return []


def _create_pr_impl(
    repo_root: str, branch_name: str, title: str | None = None, body: str | None = None
) -> dict:
    """Push branch to remote and create a GitHub PR.

    Returns dict with:
        created     – True if PR was created
        pr_url      – URL of the created PR (empty on failure)
        pr_number   – integer PR number (0 on failure)
        branch_name – the branch name
        error       – error message on failure (empty on success)
        skipped     – True if branch has no new commits (no PR needed)
    """
    pr_title = title or branch_name
    pr_body = body or f"Automated PR from ribosome branch `{branch_name}`."

    # Check if branch has commits ahead of main
    log_result = _run_worker_command(
        ["git", "log", "--oneline", f"main..{branch_name}"],
        capture_output=True,
        text=True,
        timeout=10,
        cwd=repo_root,
    )
    if log_result.returncode == 0 and not log_result.stdout.strip():
        return {
            "created": False,
            "pr_url": "",
            "pr_number": 0,
            "branch_name": branch_name,
            "error": "No commits on branch ahead of main",
            "skipped": True,
        }

    # Push branch to remote
    push_result = _run_worker_command(
        ["git", "push", "origin", branch_name],
        capture_output=True,
        text=True,
        timeout=30,
        cwd=repo_root,
    )
    if push_result.returncode != 0:
        return {
            "created": False,
            "pr_url": "",
            "pr_number": 0,
            "branch_name": branch_name,
            "error": f"push failed: {push_result.stderr.strip()[:200]}",
        }

    # Create PR via gh CLI
    pr_cmd = [
        "gh",
        "pr",
        "create",
        "--head",
        branch_name,
        "--base",
        "main",
        "--title",
        pr_title,
        "--body",
        pr_body,
    ]
    pr_result = _run_worker_command(
        pr_cmd,
        capture_output=True,
        text=True,
        timeout=30,
        cwd=repo_root,
    )
    if pr_result.returncode != 0:
        return {
            "created": False,
            "pr_url": "",
            "pr_number": 0,
            "branch_name": branch_name,
            "error": f"gh pr create failed: {pr_result.stderr.strip()[:200]}",
        }

    pr_url = pr_result.stdout.strip()
    # Extract PR number from URL (e.g. https://github.com/org/repo/pull/42)
    pr_number = 0
    pr_number_match = _re.search(r"/pull/(\d+)", pr_url)
    if pr_number_match:
        pr_number = int(pr_number_match.group(1))

    return {
        "created": True,
        "pr_url": pr_url,
        "pr_number": pr_number,
        "branch_name": branch_name,
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
        # An orphaned worktree can hold the only copy of work whose harness
        # died by signal — salvage before the force-remove destroys it.
        _checkpoint_worktree(wt_path, entry, task="gc: orphaned worktree")
        with contextlib.suppress(Exception):
            _run_worker_command(
                ["git", "worktree", "remove", "--force", wt_path],
                capture_output=True,
                timeout=10,
                cwd=repo_root,
            )
        with contextlib.suppress(Exception):
            _run_worker_command(
                ["git", "branch", "-D", entry],
                capture_output=True,
                timeout=10,
                cwd=repo_root,
            )
