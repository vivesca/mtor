"""Git/worktree helpers for the mtor translocase worker."""

from __future__ import annotations

import contextlib
import fcntl as _fcntl
import os
import re as _re
import subprocess as _subprocess
import sys
import time as _time
from pathlib import Path
from subprocess import run as _run_branch_command

# Serialize merges so concurrent ribosomes queue instead of racing.
_MERGE_LOCK_PATH = Path.home() / "germline" / ".worktrees" / ".merge.lock"
_AUTO_COMMIT_DENYLIST = (
    "loci/ribosome-dossiers/",
    "loci/ribosome-heartbeats/",
    "loci/ribosome-outputs/",
    "loci/ribosome-runs.jsonl",
)


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
        command = ["git", "rebase", "--abort"] if "rebase" in state_name else ["git", "merge", "--abort"]
        with contextlib.suppress(Exception):
            _subprocess.run(command, capture_output=True, cwd=work_dir, timeout=10)
        break

    for command in (["git", "checkout", "--", "."], ["git", "clean", "-fd"]):
        with contextlib.suppress(Exception):
            _subprocess.run(command, capture_output=True, cwd=work_dir, timeout=10)


def _auto_commit(repo_dir: str, workflow_id: str | None = None) -> bool:
    """Stage and commit pending changes in *repo_dir*.

    Returns True if a commit was created, False if the working tree was clean
    or the staged diff was empty (e.g. only whitespace changes).

    Skips pre-commit hooks (--no-verify) because the ganglion worktree
    environment may lack ruff/other tools in PATH. The chaperone review
    gate catches quality issues after the fact.
    """
    try:
        branch = _run_branch_command(
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
        print("[auto-commit] WARNING refusing to commit on main/master", file=sys.stderr)
        return False

    run = _subprocess.run
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
        upstream = _subprocess.run(
            ["git", "rev-parse", "--abbrev-ref", "--symbolic-full-name", "@{u}"],
            capture_output=True,
            text=True,
            timeout=5,
            cwd=repo_root,
        )
        if upstream.returncode != 0:
            fetch = _subprocess.run(
                ["git", "fetch", "origin", "main"],
                capture_output=True,
                text=True,
                timeout=15,
                cwd=repo_root,
            )
            if fetch.returncode != 0:
                print(f"WARNING: git fetch origin main failed: {fetch.stderr.strip()}", file=sys.stderr)
            return
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
    """Push worktree branch to origin for CC review. No auto-merge to main.

    Previously merged to main directly. Now pushes the branch so CC can
    review and merge via securin. Worktree always removed after push.
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

        # Push branch to origin for CC review — no auto-merge
        push = _subprocess.run(
            ["git", "push", "origin", branch_name],
            capture_output=True,
            text=True,
            timeout=30,
            cwd=repo_root,
        )
        if push.returncode == 0:
            print(f"[merge] pushed branch {branch_name} to origin for review", file=sys.stderr)
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



def _create_pr_impl(repo_root: str, branch_name: str, title: str | None = None, body: str | None = None) -> dict:
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
    log_result = _subprocess.run(
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
    push_result = _subprocess.run(
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
        "gh", "pr", "create",
        "--head", branch_name,
        "--base", "main",
        "--title", pr_title,
        "--body", pr_body,
    ]
    pr_result = _subprocess.run(
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
