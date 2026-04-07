"""Auto-sync ganglion → soma: fetch and fast-forward merge."""

from __future__ import annotations

import subprocess


def _has_ganglion_remote(repo_path: str) -> bool:
    """Return True if *repo_path* has a git remote named ``ganglion``."""
    result = subprocess.run(
        ["git", "remote"],
        cwd=repo_path,
        capture_output=True,
        text=True,
    )
    if result.returncode != 0:
        return False
    return "ganglion" in result.stdout.splitlines()


def sync_from_ganglion(repo_path: str) -> dict:
    """Fetch new commits from the ganglion remote and fast-forward merge.

    Returns a dict with keys:
        fetched  – number of new objects fetched (0 if nothing new)
        merged   – True if merge succeeded (or was a no-op)
        error    – None on success, descriptive string on failure
    """
    if not _has_ganglion_remote(repo_path):
        return {"fetched": 0, "merged": False, "error": None}

    # --- fetch ---
    fetch = subprocess.run(
        ["git", "fetch", "ganglion"],
        cwd=repo_path,
        capture_output=True,
        text=True,
    )
    if fetch.returncode != 0:
        return {"fetched": 0, "merged": False, "error": fetch.stderr.strip()}

    # Count new commits by comparing HEAD with the fetched branch.
    # We need to resolve the current branch name first.
    branch_result = subprocess.run(
        ["git", "rev-parse", "--abbrev-ref", "HEAD"],
        cwd=repo_path,
        capture_output=True,
        text=True,
    )
    if branch_result.returncode != 0:
        return {"fetched": 0, "merged": False, "error": branch_result.stderr.strip()}

    branch = branch_result.stdout.strip()
    remote_branch = f"ganglion/{branch}"

    # Check if the remote tracking branch exists
    rev_result = subprocess.run(
        ["git", "rev-parse", "--verify", remote_branch],
        cwd=repo_path,
        capture_output=True,
        text=True,
    )
    if rev_result.returncode != 0:
        return {"fetched": 0, "merged": False, "error": f"no remote branch {remote_branch}"}

    # Count commits that would be pulled
    log_result = subprocess.run(
        ["git", "log", "--oneline", f"HEAD..{remote_branch}"],
        cwd=repo_path,
        capture_output=True,
        text=True,
    )
    new_commits = len([l for l in log_result.stdout.strip().splitlines() if l]) if log_result.stdout.strip() else 0

    if new_commits == 0:
        return {"fetched": 0, "merged": True, "error": None}

    # --- try fast-forward merge ---
    merge = subprocess.run(
        ["git", "merge", "--ff-only", remote_branch],
        cwd=repo_path,
        capture_output=True,
        text=True,
    )
    if merge.returncode != 0:
        return {
            "fetched": new_commits,
            "merged": False,
            "error": "diverged histories; cannot fast-forward",
        }

    return {"fetched": new_commits, "merged": True, "error": None}
