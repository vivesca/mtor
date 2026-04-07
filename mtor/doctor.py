"""Health check logic — Temporal reachability, worker liveness, provider info."""

from __future__ import annotations

import json
import subprocess
import sys

from porin import action as _action

from mtor import COACHING_PATH, TASK_QUEUE, TEMPORAL_HOST, VERSION, WORKER_HOST
from mtor.client import _get_client
from mtor.envelope import _ok


def doctor() -> None:
    """Health check: Temporal reachability, worker liveness, provider info."""
    cmd = "mtor doctor"
    checks = []
    all_ok = True

    # Check 1: Temporal server reachable
    client, err = _get_client()
    temporal_ok = err is None
    if not temporal_ok:
        all_ok = False
    checks.append(
        {
            "name": "temporal_reachable",
            "ok": temporal_ok,
            "detail": f"Connected to {TEMPORAL_HOST}" if temporal_ok else f"Cannot connect: {err}",
        }
    )

    # Check 2: Worker alive (query for recent RUNNING workflows as a proxy)
    worker_ok = False
    worker_detail = "Skipped (Temporal unreachable)"
    if temporal_ok and client is not None:
        try:
            import asyncio

            async def _probe():
                count = 0
                async for _ in client.list_workflows():
                    count += 1
                    if count >= 1:
                        break
                return count

            asyncio.run(_probe())
            worker_ok = True
            worker_detail = "Worker service responsive (list_workflows succeeded)"
        except Exception as probe_exc:
            worker_detail = f"Worker probe failed: {probe_exc}"
            all_ok = False
    else:
        all_ok = False

    checks.append(
        {
            "name": "worker_alive",
            "ok": worker_ok,
            "detail": worker_detail,
        }
    )

    # Check 3: Coaching file present (optional — skip if not configured)
    if COACHING_PATH is not None:
        coaching_ok = COACHING_PATH.exists()
        checks.append(
            {
                "name": "coaching_file",
                "ok": coaching_ok,
                "detail": str(COACHING_PATH) if coaching_ok else f"Missing: {COACHING_PATH}",
            }
        )
    else:
        coaching_ok = True
        checks.append(
            {
                "name": "coaching_file",
                "ok": True,
                "detail": "Not configured (MTOR_COACHING_PATH unset)",
            }
        )

    # Check 4: Provider readiness on ganglion (where ribosome executes)
    try:
        provider_result = subprocess.run(
            [
                "ssh",
                WORKER_HOST,
                "set -a; source ~/.temporal-worker.env 2>/dev/null; set +a;"
                " ribosome-tools status --compact --json",
            ],
            capture_output=True,
            text=True,
            timeout=30,
        )
        providers = json.loads(provider_result.stdout) if provider_result.returncode == 0 else []
        healthy = [p for p in providers if p.get("health") in ("OK", "HEALTHY")]
        checks.append(
            {
                "name": "providers",
                "ok": len(healthy) > 0,
                "detail": f"{len(healthy)}/{len(providers)} providers available ({WORKER_HOST})",
                "providers": providers,
            }
        )
        if not healthy:
            all_ok = False
    except (subprocess.TimeoutExpired, OSError) as exc:
        all_ok = False
        checks.append(
            {
                "name": "providers",
                "ok": False,
                "detail": f"{WORKER_HOST} unreachable: {exc}",
            }
        )
    except Exception:
        checks.append(
            {
                "name": "providers",
                "ok": False,
                "detail": f"ribosome status not available on {WORKER_HOST}",
            }
        )

    result = {
        "temporal_reachable": temporal_ok,
        "temporal_host": TEMPORAL_HOST,
        "worker_alive": worker_ok,
        "task_queue": TASK_QUEUE,
        "checks": checks,
    }

    if all_ok:
        _ok(cmd, result, [], version=VERSION)
    else:
        payload = {
            "ok": False,
            "command": cmd,
            "error": {
                "message": "One or more health checks failed",
                "code": "HEALTH_CHECK_FAILED",
            },
            "fix": f"Start Temporal worker: ssh {WORKER_HOST} 'sudo systemctl start temporal-worker'",
            "result": result,
            "next_actions": [
                _action(
                    f"ssh {WORKER_HOST} 'sudo systemctl status temporal-worker'",
                    "Check worker service status",
                ),
                _action(
                    f"ssh {WORKER_HOST} 'sudo systemctl start temporal-worker'", "Start the worker"
                ),
            ],
        }
        sys.stdout.write(json.dumps(payload) + "\n")
        sys.stdout.flush()
        sys.exit(3)
