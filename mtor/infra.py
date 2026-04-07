"""Infrastructure operations — health checks, deploy sync, cleanup."""

from __future__ import annotations

import subprocess
import time
from dataclasses import dataclass, field
from pathlib import Path

from mtor import DEPLOY_REMOTE, OUTPUTS_DIR, REPO_DIR, VERSION, WORKER_HOST


@dataclass
class HealthReport:
    """Result of an infrastructure health check."""

    ok: bool
    checks: list[dict[str, object]] = field(default_factory=list)

    def to_dict(self) -> dict:
        return {"ok": self.ok, "checks": self.checks}


def check_health(
    *,
    worker_host: str | None = None,
    repo_dir: str | None = None,
) -> HealthReport:
    """Run infrastructure health checks and return a report.

    Checks: Temporal connectivity, worker SSH reachability, disk space on worker.
    """
    host = worker_host or WORKER_HOST
    repo = repo_dir or REPO_DIR
    checks: list[dict[str, object]] = []
    all_ok = True

    # Check 1: Worker SSH reachability
    ssh_ok = False
    ssh_detail = f"Skipped (host={host} is localhost)"
    if host != "localhost":
        try:
            result = subprocess.run(
                ["ssh", host, "echo ok"],
                capture_output=True,
                text=True,
                timeout=10,
            )
            ssh_ok = result.returncode == 0
            ssh_detail = f"SSH to {host} succeeded" if ssh_ok else f"SSH failed: {result.stderr.strip()[:100]}"
        except (subprocess.TimeoutExpired, OSError) as exc:
            ssh_detail = f"SSH to {host} failed: {exc}"
    if not ssh_ok and host != "localhost":
        all_ok = False
    checks.append({"name": "worker_ssh", "ok": ssh_ok, "detail": ssh_detail})

    # Check 2: Repo directory exists
    repo_path = Path(repo)
    repo_ok = repo_path.exists()
    repo_detail = f"Repo at {repo} exists" if repo_ok else f"Repo not found: {repo}"
    if not repo_ok:
        all_ok = False
    checks.append({"name": "repo_dir", "ok": repo_ok, "detail": repo_detail})

    # Check 3: Git working tree clean
    git_clean = False
    git_detail = "Skipped (repo missing)"
    if repo_ok:
        try:
            result = subprocess.run(
                ["git", "status", "--porcelain"],
                capture_output=True,
                text=True,
                timeout=10,
                cwd=repo,
            )
            if result.returncode == 0:
                git_clean = result.stdout.strip() == ""
                git_detail = "Working tree clean" if git_clean else f"Uncommitted changes: {result.stdout.strip()[:80]}"
            else:
                git_detail = f"git status failed: {result.stderr.strip()[:80]}"
        except (subprocess.TimeoutExpired, OSError) as exc:
            git_detail = f"git status error: {exc}"
    checks.append({"name": "git_clean", "ok": git_clean, "detail": git_detail})

    # Check 4: Disk space on worker (SSH)
    disk_ok = False
    disk_detail = f"Skipped (host={host} is localhost)"
    if host != "localhost" and ssh_ok:
        try:
            result = subprocess.run(
                ["ssh", host, "df -h . | tail -1 | awk '{print $5}'"],
                capture_output=True,
                text=True,
                timeout=10,
            )
            if result.returncode == 0:
                pct_str = result.stdout.strip().replace("%", "")
                try:
                    pct = int(pct_str)
                    disk_ok = pct < 90
                    disk_detail = f"Worker disk usage: {pct}%"
                except ValueError:
                    disk_detail = f"Could not parse disk usage: {result.stdout.strip()}"
            else:
                disk_detail = f"df command failed: {result.stderr.strip()[:80]}"
        except (subprocess.TimeoutExpired, OSError) as exc:
            disk_detail = f"Disk check error: {exc}"
    elif host == "localhost":
        disk_ok = True
        disk_detail = "Skipped (localhost)"
    checks.append({"name": "worker_disk", "ok": disk_ok, "detail": disk_detail})

    if not disk_ok:
        all_ok = False

    return HealthReport(ok=all_ok, checks=checks)


@dataclass
class DeployResult:
    """Result of an infrastructure deploy."""

    steps: list[dict[str, object]] = field(default_factory=list)
    healthy: bool = False
    error: str | None = None

    def to_dict(self) -> dict:
        return {"steps": self.steps, "healthy": self.healthy, "error": self.error}


def deploy(
    *,
    worker_host: str | None = None,
    deploy_remote: str | None = None,
    repo_dir: str | None = None,
) -> DeployResult:
    """Sync code to worker host, restart Temporal worker, verify health.

    Steps:
      1. git push to deploy remote
      2. SSH merge on worker
      3. Restart temporal-worker service
      4. Verify health with check_health
    """
    host = worker_host or WORKER_HOST
    remote = deploy_remote or DEPLOY_REMOTE
    repo = repo_dir or REPO_DIR
    steps: list[dict[str, object]] = []

    # Step 1: push to deploy remote
    push = subprocess.run(
        ["git", "push", remote, "main:deploy-sync", "--force"],
        capture_output=True,
        text=True,
        timeout=30,
        cwd=repo,
    )
    if push.returncode != 0:
        return DeployResult(
            steps=steps,
            healthy=False,
            error=f"push failed: {push.stderr.strip()[:200]}",
        )
    steps.append({"step": "push", "ok": True})

    # Step 2: merge on worker
    merge = subprocess.run(
        [
            "ssh",
            host,
            f"cd {repo} && git merge deploy-sync --no-edit; git branch -d deploy-sync 2>/dev/null; true",
        ],
        capture_output=True,
        text=True,
        timeout=15,
    )
    steps.append({"step": "merge", "ok": merge.returncode == 0})

    # Step 3: restart worker
    restart = subprocess.run(
        ["ssh", host, "sudo systemctl restart temporal-worker"],
        capture_output=True,
        text=True,
        timeout=15,
    )
    steps.append({"step": "restart", "ok": restart.returncode == 0})
    if restart.returncode != 0:
        return DeployResult(
            steps=steps,
            healthy=False,
            error=f"Worker restart failed: {restart.stderr.strip()[:200]}",
        )

    # Step 4: verify health
    time.sleep(3)
    report = check_health(worker_host=host, repo_dir=repo)
    steps.append({"step": "health_check", "ok": report.ok})

    return DeployResult(
        steps=steps,
        healthy=report.ok,
        error=None if report.ok else "Health check failed after deploy",
    )


@dataclass
class CleanResult:
    """Result of an infrastructure cleanup."""

    outputs_removed: int = 0
    checkpoints_removed: int = 0
    errors: list[str] = field(default_factory=list)

    def to_dict(self) -> dict:
        return {
            "outputs_removed": self.outputs_removed,
            "checkpoints_removed": self.checkpoints_removed,
            "errors": self.errors,
        }


def clean(
    *,
    outputs_dir: str | None = None,
    older_than_days: int = 7,
) -> CleanResult:
    """Clean up old outputs and checkpoint files.

    Removes files older than ``older_than_days`` from the outputs and
    checkpoints directories.
    """
    import time

    out_dir = Path(outputs_dir or OUTPUTS_DIR)
    result = CleanResult()
    cutoff = time.time() - (older_than_days * 86400)

    # Clean outputs
    if out_dir.exists():
        for f in out_dir.glob("*.txt"):
            try:
                if f.stat().st_mtime < cutoff:
                    f.unlink()
                    result.outputs_removed += 1
            except OSError as exc:
                result.errors.append(f"{f}: {exc}")

    # Clean checkpoints
    cp_dir = out_dir / "checkpoints"
    if cp_dir.exists():
        for f in cp_dir.glob("*.json"):
            try:
                if f.stat().st_mtime < cutoff:
                    f.unlink()
                    result.checkpoints_removed += 1
            except OSError as exc:
                result.errors.append(f"{f}: {exc}")

    return result
