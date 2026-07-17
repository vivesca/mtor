"""Infrastructure operations — health checks, deploy sync, cleanup."""

from __future__ import annotations

import os
import socket
import subprocess
import shlex
import sys
import time
from dataclasses import dataclass, field
from pathlib import Path

from mtor import OUTPUTS_DIR, WORKER_HOST, WORKER_LOG_DIR
from mtor.backend import require_temporal_backend


def _default_mtor_code_dir() -> str:
    """Prefer the checkout repo over the installed wheel location."""
    checkout = Path.home() / "code" / "mtor"
    if (checkout / ".git").exists():
        return str(checkout)
    return str(Path(__file__).resolve().parents[1])


MTOR_CODE_DIR = _default_mtor_code_dir()
REMOTE_MTOR_CODE_DIR = str(Path(WORKER_LOG_DIR).parent)


def _is_local_host(host: str) -> bool:
    """Return True when *host* names the current machine."""
    local_names = {
        "localhost",
        "127.0.0.1",
        "::1",
        socket.gethostname(),
        socket.getfqdn(),
    }
    return host in local_names


def _host_command(host: str, command: str) -> list[str]:
    """Build a local or SSH command for *host*."""
    if _is_local_host(host):
        return ["bash", "-lc", command]
    return ["ssh", host, command]


def _parse_systemctl_show(output: str) -> dict[str, str]:
    """Parse ``systemctl show`` key=value output into a dict."""
    values: dict[str, str] = {}
    for line in output.splitlines():
        if "=" not in line:
            continue
        key, value = line.split("=", 1)
        values[key] = value
    return values


def probe_worker_admission(worker_host: str | None = None) -> dict[str, object]:
    """Return whether the authoritative worker service can accept new work.

    Temporal remains reachable while its worker is stopping, so server
    connectivity is not an admission signal. Fail closed unless systemd
    reports the user-scoped unit as active/running with a positive MainPID.
    """
    host = worker_host or WORKER_HOST
    command = (
        "systemctl --user show mtor-worker.service "
        "--property=ActiveState,SubState,MainPID --no-pager"
    )
    try:
        result = subprocess.run(
            _host_command(host, command),
            capture_output=True,
            text=True,
            timeout=10,
        )
    except (subprocess.TimeoutExpired, OSError) as exc:
        return {
            "ok": False,
            "state": "unknown",
            "active_state": "unknown",
            "sub_state": "unknown",
            "main_pid": 0,
            "detail": f"Worker service state unavailable: {exc}",
        }

    if result.returncode != 0:
        error = (result.stderr or result.stdout or "systemctl show failed").strip()
        return {
            "ok": False,
            "state": "unknown",
            "active_state": "unknown",
            "sub_state": "unknown",
            "main_pid": 0,
            "detail": f"Worker service state unavailable: {error[:200]}",
        }

    unit = _parse_systemctl_show(result.stdout)
    active_state = unit.get("ActiveState", "unknown")
    sub_state = unit.get("SubState", "unknown")
    try:
        main_pid = int(unit.get("MainPID", "0"))
    except ValueError:
        main_pid = 0

    ok = active_state == "active" and sub_state == "running" and main_pid > 0
    if ok:
        state = "active"
    elif active_state in {"deactivating", "inactive", "failed"}:
        state = active_state
    else:
        state = "unknown"

    return {
        "ok": ok,
        "state": state,
        "active_state": active_state,
        "sub_state": sub_state,
        "main_pid": main_pid,
        "detail": (
            f"mtor-worker.service is active/running with MainPID={main_pid}"
            if ok
            else (
                "mtor-worker.service is not accepting work "
                f"(ActiveState={active_state}, SubState={sub_state}, "
                f"MainPID={main_pid})"
            )
        ),
    }


def _split_marked_sections(output: str) -> dict[str, str]:
    """Split marker-delimited command output into named sections."""
    sections: dict[str, list[str]] = {}
    current: str | None = None
    for line in output.splitlines():
        if line.startswith("__") and line.endswith("__"):
            current = line.strip("_").lower()
            sections[current] = []
            continue
        if current is not None:
            sections[current].append(line)
    return {key: "\n".join(lines) for key, lines in sections.items()}


def _parse_worker_roots(output: str) -> tuple[bool, str]:
    """Validate one non-orphaned ``op run ... python3 -m mtor.worker`` root."""
    roots: list[tuple[int, int, str]] = []
    for line in output.splitlines():
        parts = line.strip().split(None, 2)
        if len(parts) < 3:
            continue
        try:
            pid = int(parts[0])
            ppid = int(parts[1])
        except ValueError:
            continue
        args = parts[2]
        if "op run" in args and "python3 -m mtor.worker" in args:
            roots.append((pid, ppid, args))

    orphaned = [pid for pid, ppid, _args in roots if ppid == 1]
    if len(roots) == 1 and not orphaned:
        pid, ppid, _args = roots[0]
        return True, f"one mtor.worker root pid={pid} ppid={ppid}"
    if orphaned:
        return (
            False,
            f"orphaned mtor.worker root(s) under PID 1: {orphaned}; roots={len(roots)}",
        )
    return False, f"expected one mtor.worker root, found {len(roots)}"


def _parse_orphan_cleanup_output(output: str) -> dict[str, object]:
    """Parse key=value output from the orphan worker cleanup command."""
    values: dict[str, str] = {}
    for line in output.splitlines():
        if "=" not in line:
            continue
        key, value = line.split("=", 1)
        values[key] = value

    def as_int(key: str) -> int:
        try:
            return int(values.get(key, "0"))
        except ValueError:
            return 0

    pids = [pid for pid in values.get("pids", "").split(",") if pid]
    return {
        "found": as_int("found"),
        "terminated": as_int("terminated"),
        "remaining": as_int("remaining"),
        "pids": pids,
    }


def _cleanup_orphaned_worker_roots(host: str) -> dict[str, object]:
    """Terminate orphaned ``op run ... python3 -m mtor.worker`` roots on *host*."""
    cleanup_script = r"""
set -u
list_orphans() {
  ps -eo pid=,ppid=,args= | awk '$2 == 1 && index($0, "op run") && index($0, "python3 -m mtor.worker") {print $1}'
}

pids="$(list_orphans || true)"
if [ -z "$pids" ]; then
  printf 'found=0\nterminated=0\nremaining=0\npids=\n'
  exit 0
fi

found="$(printf '%s\n' "$pids" | sed '/^$/d' | wc -l | tr -d ' ')"
pid_csv="$(printf '%s\n' "$pids" | paste -sd, -)"
kill -TERM $pids 2>/dev/null || true
sleep 2

remaining_pids="$(list_orphans || true)"
if [ -n "$remaining_pids" ]; then
  kill -KILL $remaining_pids 2>/dev/null || true
  sleep 1
fi

final_pids="$(list_orphans || true)"
remaining=0
if [ -n "$final_pids" ]; then
  remaining="$(printf '%s\n' "$final_pids" | sed '/^$/d' | wc -l | tr -d ' ')"
fi
terminated=$((found - remaining))
printf 'found=%s\nterminated=%s\nremaining=%s\npids=%s\n' "$found" "$terminated" "$remaining" "$pid_csv"
"""
    result = subprocess.run(
        ["ssh", host, "bash", "-lc", cleanup_script],
        capture_output=True,
        text=True,
        timeout=10,
    )
    parsed = _parse_orphan_cleanup_output(result.stdout)
    parsed["ok"] = result.returncode == 0 and parsed["remaining"] == 0
    if result.returncode != 0:
        parsed["error"] = result.stderr.strip()[:200]
    return parsed


def _retire_legacy_temporal_worker(host: str) -> dict[str, object]:
    """Stop and disable the obsolete system-level temporal-worker unit."""
    retire_script = r"""
set -u
sudo systemctl disable --now temporal-worker.service >/dev/null 2>&1 || true
active="$(systemctl is-active temporal-worker.service 2>/dev/null || true)"
enabled="$(systemctl is-enabled temporal-worker.service 2>/dev/null || true)"
printf 'active=%s\nenabled=%s\n' "$active" "$enabled"
case "$active" in
  inactive|failed|unknown) active_ok=1 ;;
  *) active_ok=0 ;;
esac
case "$enabled" in
  disabled|disabled-runtime|not-found|masked) enabled_ok=1 ;;
  *) enabled_ok=0 ;;
esac
[ "$active_ok" = 1 ] && [ "$enabled_ok" = 1 ]
"""
    result = subprocess.run(
        ["ssh", host, "bash", "-lc", retire_script],
        capture_output=True,
        text=True,
        timeout=15,
    )
    values: dict[str, str] = {}
    for line in result.stdout.splitlines():
        if "=" not in line:
            continue
        key, value = line.split("=", 1)
        values[key] = value
    payload: dict[str, object] = {
        "ok": result.returncode == 0,
        "active": values.get("active", "unknown"),
        "enabled": values.get("enabled", "unknown"),
    }
    if result.returncode != 0:
        payload["error"] = result.stderr.strip()[:200]
    return payload


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
    remote_repo_dir: str | None = None,
    expected_sha: str | None = None,
) -> HealthReport:
    """Run infrastructure health checks and return a report.

    Checks: Temporal connectivity, worker SSH reachability, disk space on worker.
    """
    host = worker_host or WORKER_HOST
    repo = repo_dir or MTOR_CODE_DIR
    remote_repo = remote_repo_dir or REMOTE_MTOR_CODE_DIR
    checks: list[dict[str, object]] = []
    all_ok = True

    # Check 1: Worker SSH reachability
    host_is_local = _is_local_host(host)
    ssh_ok = host_is_local
    ssh_detail = f"Skipped (host={host} is local)"
    if not host_is_local:
        try:
            result = subprocess.run(
                ["ssh", host, "echo ok"],
                capture_output=True,
                text=True,
                timeout=10,
            )
            ssh_ok = result.returncode == 0
            ssh_detail = (
                f"SSH to {host} succeeded"
                if ssh_ok
                else f"SSH failed: {result.stderr.strip()[:100]}"
            )
        except (subprocess.TimeoutExpired, OSError) as exc:
            ssh_detail = f"SSH to {host} failed: {exc}"
    if not ssh_ok and not host_is_local:
        all_ok = False
    checks.append({"name": "worker_ssh", "ok": ssh_ok, "detail": ssh_detail})

    # Check 2: Worker repo directory exists
    checked_repo = repo if host_is_local else remote_repo
    if not ssh_ok and not host_is_local:
        repo_ok = False
        repo_detail = f"Skipped (host={host} unreachable)"
    elif host_is_local:
        repo_ok = Path(checked_repo).exists()
        repo_detail = (
            f"Repo at {checked_repo} exists"
            if repo_ok
            else f"Repo not found: {checked_repo}"
        )
    else:
        result = subprocess.run(
            _host_command(host, f"test -d {shlex.quote(checked_repo)}/.git"),
            capture_output=True,
            text=True,
            timeout=10,
        )
        repo_ok = result.returncode == 0
        repo_detail = (
            f"Repo at {checked_repo} exists on {host}"
            if repo_ok
            else f"Repo not found on {host}: {checked_repo}"
        )
    if not repo_ok:
        all_ok = False
    checks.append({"name": "repo_dir", "ok": repo_ok, "detail": repo_detail})

    # Check 3: Worker checkout contains the immutable deployed SHA when one is
    # supplied. Standalone checks retain exact local-vs-worker drift detection.
    head_ok = False
    head_detail = "Skipped (repo missing)"
    if repo_ok:
        try:
            if expected_sha is not None:
                if host_is_local:
                    worker_head = subprocess.run(
                        ["git", "rev-parse", "HEAD"],
                        capture_output=True,
                        text=True,
                        timeout=10,
                        cwd=repo,
                    )
                    contains = subprocess.run(
                        [
                            "git",
                            "merge-base",
                            "--is-ancestor",
                            expected_sha,
                            "HEAD",
                        ],
                        capture_output=True,
                        text=True,
                        timeout=10,
                        cwd=repo,
                    )
                else:
                    worker_head = subprocess.run(
                        _host_command(
                            host,
                            f"cd {shlex.quote(remote_repo)} && git rev-parse HEAD",
                        ),
                        capture_output=True,
                        text=True,
                        timeout=10,
                    )
                    contains = subprocess.run(
                        _host_command(
                            host,
                            f"cd {shlex.quote(remote_repo)} && "
                            "git merge-base --is-ancestor "
                            f"{shlex.quote(expected_sha)} HEAD",
                        ),
                        capture_output=True,
                        text=True,
                        timeout=10,
                    )
                worker_sha = worker_head.stdout.strip()
                if worker_head.returncode != 0:
                    head_detail = (
                        "worker git rev-parse failed: "
                        f"{worker_head.stderr.strip()[:80]}"
                    )
                else:
                    head_ok = contains.returncode == 0
                    head_detail = (
                        "worker HEAD contains deployed SHA: "
                        f"deployed {expected_sha[:8]} worker {worker_sha[:8]}"
                        if head_ok
                        else "worker HEAD does not contain deployed SHA: "
                        f"deployed {expected_sha[:8]} worker {worker_sha[:8]}"
                    )
            else:
                local_head = subprocess.run(
                    ["git", "rev-parse", "HEAD"],
                    capture_output=True,
                    text=True,
                    timeout=10,
                    cwd=repo,
                )
                if local_head.returncode != 0:
                    head_detail = (
                        f"local git rev-parse failed: {local_head.stderr.strip()[:80]}"
                    )
                elif host_is_local:
                    short = local_head.stdout.strip()[:8]
                    head_ok = True
                    head_detail = f"worker HEAD matches local HEAD: {short}"
                else:
                    remote_head = subprocess.run(
                        _host_command(
                            host,
                            f"cd {shlex.quote(remote_repo)} && git rev-parse HEAD",
                        ),
                        capture_output=True,
                        text=True,
                        timeout=10,
                    )
                    if remote_head.returncode != 0:
                        head_detail = (
                            "worker git rev-parse failed: "
                            f"{remote_head.stderr.strip()[:80]}"
                        )
                    else:
                        local_sha = local_head.stdout.strip()
                        remote_sha = remote_head.stdout.strip()
                        head_ok = local_sha == remote_sha
                        head_detail = (
                            f"worker HEAD matches local HEAD: {local_sha[:8]}"
                            if head_ok
                            else "worker HEAD differs: "
                            f"local {local_sha[:8]} worker {remote_sha[:8]}"
                        )
        except (subprocess.TimeoutExpired, OSError) as exc:
            head_detail = f"git HEAD check failed: {exc}"
    checks.append({"name": "worker_repo_head", "ok": head_ok, "detail": head_detail})
    if not head_ok:
        all_ok = False

    # Check 4: Git working tree clean
    git_clean = False
    git_detail = "Skipped (repo missing)"
    if repo_ok:
        try:
            if host_is_local:
                result = subprocess.run(
                    ["git", "status", "--porcelain", "--untracked-files=no"],
                    capture_output=True,
                    text=True,
                    timeout=10,
                    cwd=checked_repo,
                )
            else:
                result = subprocess.run(
                    _host_command(
                        host,
                        f"cd {shlex.quote(checked_repo)} && "
                        "git status --porcelain --untracked-files=no",
                    ),
                    capture_output=True,
                    text=True,
                    timeout=10,
                )
            if result.returncode == 0:
                git_clean = result.stdout.strip() == ""
                git_detail = (
                    "Working tree clean"
                    if git_clean
                    else f"Uncommitted changes: {result.stdout.strip()[:80]}"
                )
            else:
                git_detail = f"git status failed: {result.stderr.strip()[:80]}"
        except (subprocess.TimeoutExpired, OSError) as exc:
            git_detail = f"git status error: {exc}"
    checks.append({"name": "git_clean", "ok": git_clean, "detail": git_detail})
    if not git_clean:
        all_ok = False

    # Check 5: Disk space on worker (SSH)
    disk_ok = False
    disk_detail = f"Skipped (host={host} is local)"
    if ssh_ok:
        try:
            result = subprocess.run(
                _host_command(host, "df -h . | tail -1 | awk '{print $5}'"),
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
    checks.append({"name": "worker_disk", "ok": disk_ok, "detail": disk_detail})

    if not disk_ok:
        all_ok = False

    # Check 6: exactly one authoritative worker service/process tree.
    service_ok = True
    service_detail = f"Skipped (host={host} unreachable)"
    process_ok = True
    process_detail = f"Skipped (host={host} unreachable)"
    if ssh_ok:
        try:
            result = subprocess.run(
                _host_command(
                    host,
                    "printf '__MTOR_WORKER__\\n'; "
                    "systemctl --user show mtor-worker.service "
                    "--property=ActiveState,SubState,MainPID --no-pager 2>/dev/null || true; "
                    "printf '__TEMPORAL_WORKER_USER__\\n'; "
                    "systemctl --user show temporal-worker.service "
                    "--property=ActiveState,SubState,MainPID --no-pager 2>/dev/null || true; "
                    "printf '__TEMPORAL_WORKER_SYSTEM__\\n'; "
                    "systemctl show temporal-worker.service "
                    "--property=ActiveState,SubState,MainPID --no-pager 2>/dev/null || true; "
                    "printf '__MTOR_WORKER_SYSTEM__\\n'; "
                    "systemctl show mtor-worker.service "
                    "--property=ActiveState,SubState,MainPID --no-pager 2>/dev/null || true",
                ),
                capture_output=True,
                text=True,
                timeout=10,
            )
            sections = _split_marked_sections(result.stdout)
            mtor_unit = _parse_systemctl_show(sections.get("mtor_worker", ""))
            temporal_user_unit = _parse_systemctl_show(
                sections.get("temporal_worker_user", "")
            )
            temporal_system_unit = _parse_systemctl_show(
                sections.get("temporal_worker_system", "")
            )
            mtor_system_unit = _parse_systemctl_show(
                sections.get("mtor_worker_system", "")
            )
            mtor_active = (
                mtor_unit.get("ActiveState") == "active"
                and mtor_unit.get("SubState") == "running"
            )
            temporal_user_active = temporal_user_unit.get("ActiveState") == "active"
            temporal_system_active = temporal_system_unit.get("ActiveState") == "active"
            mtor_system_active = mtor_system_unit.get("ActiveState") == "active"
            service_ok = (
                result.returncode == 0
                and mtor_active
                and not temporal_user_active
                and not temporal_system_active
                and not mtor_system_active
            )
            service_detail = (
                "mtor-worker.service active/running (user scope only); temporal-worker.service inactive in user/system scope"
                if service_ok
                else (
                    "Expected mtor-worker.service active/running (user scope only) and "
                    "legacy temporal-worker.service inactive in user/system scope "
                    f"(mtor={mtor_unit or 'absent'}, "
                    f"temporal_user={temporal_user_unit or 'absent'}, "
                    f"temporal_system={temporal_system_unit or 'absent'}, "
                    f"mtor_system={mtor_system_unit or 'absent'})"
                )
            )
        except (subprocess.TimeoutExpired, OSError) as exc:
            service_ok = False
            service_detail = f"Worker service check failed: {exc}"

        try:
            result = subprocess.run(
                _host_command(
                    host,
                    "ps -eo pid=,ppid=,args=",
                ),
                capture_output=True,
                text=True,
                timeout=10,
            )
            process_ok, process_detail = _parse_worker_roots(result.stdout)
            process_ok = result.returncode == 0 and process_ok
        except (subprocess.TimeoutExpired, OSError) as exc:
            process_ok = False
            process_detail = f"Worker process check failed: {exc}"

    checks.append(
        {"name": "worker_service_singleton", "ok": service_ok, "detail": service_detail}
    )
    checks.append(
        {"name": "worker_process_singleton", "ok": process_ok, "detail": process_detail}
    )
    if not service_ok or not process_ok:
        all_ok = False

    return HealthReport(ok=all_ok, checks=checks)


def _count_active_ribosomes(host: str) -> int | None:
    """Count in-flight ribosome subprocesses on the worker host.

    Returns None when the probe fails (SSH error, unparsable output);
    callers treat None as unknown and do not block on it.
    """
    # Bracketed pattern so the probe's own remote command line never matches.
    try:
        result = subprocess.run(
            _host_command(host, "pgrep -fc 'effectors/[r]ibosome'"),
            capture_output=True,
            text=True,
            timeout=10,
        )
    except (subprocess.TimeoutExpired, OSError):
        return None
    if result.returncode == 1:
        return 0  # pgrep: no processes matched
    if result.returncode != 0:
        return None
    try:
        return int(result.stdout.strip())
    except ValueError:
        return None


def _wait_for_ribosome_idle(
    host: str,
    timeout_seconds: int,
    poll_seconds: float = 10.0,
) -> dict[str, object]:
    """Wait until no ribosome subprocesses are running on *host*.

    Never blocks the deploy indefinitely: on timeout, or when the probe
    fails, it reports and lets the worker's graceful shutdown handle
    whatever is still in flight.
    """
    waited = 0.0
    active = _count_active_ribosomes(host)
    while active and waited < timeout_seconds:
        time.sleep(poll_seconds)
        waited += poll_seconds
        active = _count_active_ribosomes(host)
    return {
        "ok": True,
        "idle": active == 0,
        "active": active if active is not None else "unknown",
        "waited_seconds": int(waited),
    }


@dataclass
class DeployResult:
    """Result of an infrastructure deploy."""

    steps: list[dict[str, object]] = field(default_factory=list)
    healthy: bool = False
    error: str | None = None

    def to_dict(self) -> dict:
        return {"steps": self.steps, "healthy": self.healthy, "error": self.error}


def _resolve_deploy_sha(repo: str) -> str:
    """Resolve one immutable commit for every stage of a deployment."""
    result = subprocess.run(
        ["git", "rev-parse", "--verify", "HEAD^{commit}"],
        capture_output=True,
        text=True,
        timeout=10,
        cwd=repo,
    )
    sha = result.stdout.strip()
    if result.returncode != 0 or not sha:
        detail = result.stderr.strip()[:200] or "git returned an empty commit"
        raise RuntimeError(f"local git HEAD lookup failed: {detail}")
    return sha


def restart_worker(host: str | None = None) -> None:
    """Restart mtor-worker on *host*, tolerating the SIGTERM drain window.

    `systemctl --user restart mtor-worker` can block for up to ~10 minutes when
    a ribosome task is in flight, because the worker now drains on SIGTERM
    (graceful_shutdown_timeout 540s + systemd stop timeout 630s). The previous
    call sites used timeout=15, which raised subprocess.TimeoutExpired mid-drain
    and crashed the caller even though the restart itself completed.
    """
    require_temporal_backend()
    host = host or WORKER_HOST

    # Busy pre-check: a positive count explains why the restart will block.
    try:
        probe = subprocess.run(
            ["ssh", host, "pgrep -cf 'ribosome --provider'"],
            capture_output=True,
            text=True,
            timeout=10,
        )
        count = 0
        if probe.returncode == 0:
            count = int(probe.stdout.strip() or "0")
    except (subprocess.TimeoutExpired, OSError, ValueError):
        count = 0
    if count > 0:
        print(
            f"[restart] {count} ribosome task(s) in flight; "
            "worker drains up to 540s before restarting",
            file=sys.stderr,
        )

    # 700s covers the 630s systemd stop timeout plus margin.
    try:
        result = subprocess.run(
            ["ssh", host, "systemctl --user restart mtor-worker"],
            capture_output=True,
            text=True,
            timeout=700,
        )
    except subprocess.TimeoutExpired as exc:
        raise RuntimeError(
            "worker restart timed out after 700s (drain window exceeded); "
            "check: systemctl --user status mtor-worker"
        ) from exc
    if result.returncode != 0:
        raise RuntimeError(f"worker restart failed: {result.stderr.strip()[:200]}")


def deploy(
    *,
    worker_host: str | None = None,
    deploy_remote: str | None = None,
    repo_dir: str | None = None,
    remote_repo_dir: str | None = None,
) -> DeployResult:
    """Sync code to worker host, restart mtor worker, verify health.

    Steps:
      1. Resolve and push one immutable commit to origin/main
      2. Fast-forward the worker and prove it contains that commit
      3. Reconcile the worker's frozen environment
      4. Restart mtor-worker.service
      5. Verify health against the same immutable commit
    """
    require_temporal_backend()
    host = worker_host or WORKER_HOST
    repo = repo_dir or MTOR_CODE_DIR
    remote_repo = remote_repo_dir or REMOTE_MTOR_CODE_DIR
    steps: list[dict[str, object]] = []

    # Step 1: capture one immutable commit before any network operation. Local
    # HEAD may advance independently while a slow worker restart is draining.
    try:
        deployed_sha = _resolve_deploy_sha(repo)
    except RuntimeError as exc:
        steps.append({"step": "resolve_head", "ok": False})
        return DeployResult(steps=steps, healthy=False, error=str(exc))
    steps.append({"step": "resolve_head", "ok": True, "sha": deployed_sha})

    # Step 2: publish that literal commit, not the moving HEAD symbolic ref.
    push = subprocess.run(
        ["git", "push", "origin", f"{deployed_sha}:refs/heads/main"],
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
    steps.append({"step": "push", "ok": True, "sha": deployed_sha})

    # Step 3: fast-forward the worker, then prove its checkout contains the
    # published commit. Retry successful no-op merges to absorb fetch lag.
    merge_attempts = 3
    verified = False
    verify = None
    for attempt in range(1, merge_attempts + 1):
        merge = subprocess.run(
            _host_command(
                host,
                f"set -e; cd {shlex.quote(remote_repo)}; "
                "git fetch origin main; "
                "git merge --ff-only origin/main",
            ),
            capture_output=True,
            text=True,
            timeout=30,
        )
        if merge.returncode != 0:
            steps.append({"step": "merge", "ok": False})
            return DeployResult(
                steps=steps,
                healthy=False,
                error=f"merge failed: {merge.stderr.strip()[:200]}",
            )
        verify = subprocess.run(
            _host_command(
                host,
                f"cd {shlex.quote(remote_repo)} && "
                "git merge-base --is-ancestor "
                f"{shlex.quote(deployed_sha)} HEAD",
            ),
            capture_output=True,
            text=True,
            timeout=10,
        )
        if verify.returncode == 0:
            verified = True
            break
        if attempt < merge_attempts:
            time.sleep(2)
    steps.append({"step": "merge", "ok": True, "attempts": attempt})
    steps.append(
        {
            "step": "verify_worker_head",
            "ok": verified,
            "sha": deployed_sha,
            "attempts": attempt,
        }
    )
    if not verified:
        detail = verify.stderr.strip()[:200] if verify is not None else ""
        return DeployResult(
            steps=steps,
            healthy=False,
            error=(
                "worker HEAD does not contain deployed SHA "
                f"{deployed_sha[:8]} after {merge_attempts} attempts"
                + (f": {detail}" if detail else "")
            ),
        )

    # Step 4: source merges do not refresh generated console entry points.
    # Reconcile from the reviewed lock before touching the running service.
    sync = subprocess.run(
        _host_command(
            host,
            f"set -e; cd {shlex.quote(remote_repo)}; uv sync --frozen",
        ),
        capture_output=True,
        text=True,
        timeout=300,
    )
    steps.append({"step": "sync_environment", "ok": sync.returncode == 0})
    if sync.returncode != 0:
        return DeployResult(
            steps=steps,
            healthy=False,
            error=f"environment sync failed: {sync.stderr.strip()[:200]}",
        )

    # Step 5: retire the obsolete system-level worker before restarting the authoritative unit.
    retire_legacy = _retire_legacy_temporal_worker(host)
    steps.append({"step": "retire_legacy_temporal_worker", **retire_legacy})
    if not retire_legacy["ok"]:
        return DeployResult(
            steps=steps,
            healthy=False,
            error="Legacy temporal-worker retirement failed",
        )

    # Step 6: wait for in-flight ribosome activities to finish so the
    # restart consumes no activity attempts. Bounded — the worker's
    # graceful shutdown covers anything still running at timeout.
    drain_timeout = int(os.getenv("MTOR_DEPLOY_DRAIN_SECONDS", "600"))
    drain = _wait_for_ribosome_idle(host, timeout_seconds=drain_timeout)
    steps.append({"step": "drain", **drain})

    # Step 7: restart worker. systemctl restart blocks through the unit's
    # stop (up to TimeoutStopSec while the worker drains), so the timeout
    # must exceed the drain window.
    try:
        restart_worker(host)
    except RuntimeError as exc:
        steps.append({"step": "restart", "ok": False})
        return DeployResult(
            steps=steps,
            healthy=False,
            error=str(exc),
        )
    steps.append({"step": "restart", "ok": True})

    # Step 8: let restart settle, then remove late orphan roots twice before health.
    time.sleep(3)
    cleanup = _cleanup_orphaned_worker_roots(host)
    steps.append({"step": "orphan_cleanup", "attempt": 1, **cleanup})
    time.sleep(3)
    cleanup_repeat = _cleanup_orphaned_worker_roots(host)
    steps.append({"step": "orphan_cleanup", "attempt": 2, **cleanup_repeat})

    # Step 9: verify health against the same SHA captured before publication.
    report = check_health(
        worker_host=host,
        repo_dir=repo,
        remote_repo_dir=remote_repo,
        expected_sha=deployed_sha,
    )
    steps.append({"step": "health_check", "ok": report.ok, "sha": deployed_sha})
    cleanup_ok = bool(cleanup["ok"]) and bool(cleanup_repeat["ok"])
    healthy = cleanup_ok and report.ok

    return DeployResult(
        steps=steps,
        healthy=healthy,
        error=(
            None
            if healthy
            else (
                "Health check failed after deploy"
                if report.ok is False
                else "Orphan cleanup failed after deploy"
            )
        ),
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


async def setup_search_attributes() -> dict[str, object]:
    """Register custom search attributes on the Temporal server."""
    require_temporal_backend()

    from temporalio.api.enums.v1 import IndexedValueType
    from temporalio.api.operatorservice.v1 import AddSearchAttributesRequest
    from temporalio.client import Client

    from mtor import TEMPORAL_HOST

    client = await Client.connect(TEMPORAL_HOST)

    # Attributes to register
    attrs = {
        "mtor_provider": IndexedValueType.INDEXED_VALUE_TYPE_KEYWORD,
        "mtor_verdict": IndexedValueType.INDEXED_VALUE_TYPE_KEYWORD,
        "mtor_mode": IndexedValueType.INDEXED_VALUE_TYPE_KEYWORD,
        "mtor_spec": IndexedValueType.INDEXED_VALUE_TYPE_KEYWORD,
        "mtor_risk": IndexedValueType.INDEXED_VALUE_TYPE_KEYWORD,
    }

    try:
        await client.operator_service.add_search_attributes(
            AddSearchAttributesRequest(
                namespace="default",
                search_attributes=attrs,
            )
        )
        return {"status": "success", "attributes": list(attrs.keys())}
    except Exception as exc:
        if "already exists" in str(exc).lower():
            return {"status": "already_registered", "attributes": list(attrs.keys())}
        raise
