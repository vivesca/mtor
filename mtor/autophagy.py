"""Autophagy — salvage ganglion commits and verify with tests.

Provides ``salvage()``, which fetches new commits from the ganglion remote,
cherry-picks those not already present in HEAD, and runs the test suite
to verify nothing is broken.
"""

from __future__ import annotations

import subprocess
from dataclasses import dataclass, field


@dataclass
class SalvageResult:
    """Outcome of a salvage operation."""

    fetched: int = 0
    cherry_picked: list[str] = field(default_factory=list)
    skipped: list[str] = field(default_factory=list)
    tests_passed: bool = False
    test_output: str = ""
    error: str | None = None


def _has_ganglion_remote(repo_path: str) -> bool:
    """Return True if the repo has a ``ganglion`` remote."""
    result = subprocess.run(
        ["git", "remote"],
        cwd=repo_path,
        capture_output=True,
        text=True,
    )
    if result.returncode != 0:
        return False
    return "ganglion" in result.stdout.splitlines()


def _current_branch(repo_path: str) -> str | None:
    """Return the current branch name, or None on failure."""
    result = subprocess.run(
        ["git", "rev-parse", "--abbrev-ref", "HEAD"],
        cwd=repo_path,
        capture_output=True,
        text=True,
    )
    if result.returncode != 0:
        return None
    return result.stdout.strip()


def _is_commit_merged(repo_path: str, commit_sha: str) -> bool:
    """Return True if *commit_sha* is already reachable from HEAD."""
    result = subprocess.run(
        ["git", "merge-base", "--is-ancestor", commit_sha, "HEAD"],
        cwd=repo_path,
        capture_output=True,
        text=True,
    )
    return result.returncode == 0


def _run_tests(repo_path: str) -> tuple[bool, str]:
    """Run ``uv run pytest`` and return (passed, output)."""
    result = subprocess.run(
        ["uv", "run", "pytest", "--tb=short", "-q"],
        cwd=repo_path,
        capture_output=True,
        text=True,
        timeout=300,
    )
    output = result.stdout + result.stderr
    return result.returncode == 0, output


def salvage(repo_path: str, *, run_tests: bool = True) -> SalvageResult:
    """Salvage new commits from ganglion, cherry-pick, and optionally run tests.

    1. Fetch from ganglion remote.
    2. List commits on ganglion/<branch> that are not in HEAD.
    3. Skip commits already reachable (merged via another path).
    4. Cherry-pick the remaining new commits.
    5. Run the test suite if *run_tests* is True.

    Returns a ``SalvageResult`` with details.
    """
    result = SalvageResult()

    if not _has_ganglion_remote(repo_path):
        result.error = "no ganglion remote configured"
        return result

    # Fetch from ganglion
    fetch = subprocess.run(
        ["git", "fetch", "ganglion"],
        cwd=repo_path,
        capture_output=True,
        text=True,
    )
    if fetch.returncode != 0:
        result.error = f"fetch failed: {fetch.stderr.strip()}"
        return result

    branch = _current_branch(repo_path)
    if branch is None:
        result.error = "cannot determine current branch"
        return result

    remote_branch = f"ganglion/{branch}"

    # Verify remote tracking branch exists
    rev = subprocess.run(
        ["git", "rev-parse", "--verify", remote_branch],
        cwd=repo_path,
        capture_output=True,
        text=True,
    )
    if rev.returncode != 0:
        result.error = f"no remote branch {remote_branch}"
        return result

    # List new commits (ganglion has but HEAD doesn't)
    log = subprocess.run(
        ["git", "log", "--format=%H", f"HEAD..{remote_branch}"],
        cwd=repo_path,
        capture_output=True,
        text=True,
    )
    raw_shas = [line for line in log.stdout.strip().splitlines() if line]
    result.fetched = len(raw_shas)

    if not raw_shas:
        result.tests_passed = True
        return result

    # Separate: already-merged vs. truly new
    to_pick: list[str] = []
    for sha in raw_shas:
        if _is_commit_merged(repo_path, sha):
            result.skipped.append(sha)
        else:
            to_pick.append(sha)

    # Cherry-pick new commits (oldest first — git log gives newest-first)
    for sha in reversed(to_pick):
        cp = subprocess.run(
            ["git", "cherry-pick", "--no-commit", sha],
            cwd=repo_path,
            capture_output=True,
            text=True,
        )
        if cp.returncode != 0:
            # Abort cherry-pick on failure, roll back
            subprocess.run(
                ["git", "cherry-pick", "--abort"],
                cwd=repo_path,
                capture_output=True,
                text=True,
            )
            result.error = f"cherry-pick failed for {sha[:8]}: {cp.stderr.strip()}"
            return result
        result.cherry_picked.append(sha)

    # Run tests
    if run_tests and result.cherry_picked:
        passed, output = _run_tests(repo_path)
        result.tests_passed = passed
        result.test_output = output
        if not passed:
            # Roll back cherry-picks if tests fail
            subprocess.run(
                ["git", "reset", "--hard", "HEAD"],
                cwd=repo_path,
                capture_output=True,
                text=True,
            )
            result.error = "tests failed after cherry-pick; changes rolled back"
            return result
    elif not result.cherry_picked:
        result.tests_passed = True

    return result
