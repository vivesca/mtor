"""Health check logic — Temporal reachability, worker liveness, provider info."""

from __future__ import annotations

import json
import os
import subprocess
import sys
import time
import urllib.error
import urllib.request
from dataclasses import dataclass

from porin import action as _action

from mtor import COACHING_PATH, TASK_QUEUE, TEMPORAL_HOST, VERSION, WORKER_HOST
from mtor.client import _get_client
from mtor.envelope import _ok


# ---------------------------------------------------------------------------
# Provider API probe
# ---------------------------------------------------------------------------

_API_KEY_ENVVARS = {
    "zhipu": "ZHIPU_API_KEY",
    "volcano": "VOLCANO_API_KEY",
    "infini": "INFINI_API_KEY",
}


@dataclass
class ProbeResult:
    provider: str
    ok: bool
    latency_ms: float | None
    detail: str


def _probe_provider(provider: str) -> ProbeResult:
    """Send a real HTTP request to the provider's Anthropic Messages API.

    Args:
        provider: One of "zhipu", "volcano", "infini".

    Returns:
        ProbeResult with ok, latency, and detail.
    """
    endpoints = {
        "zhipu": "https://open.bigmodel.cn/api/anthropic/v1/messages",
        "volcano": "https://ark.cn-beijing.volces.com/api/coding/v1/messages",
        "infini": "https://cloud.infini-ai.com/maas/coding/v1/messages",
    }
    models = {
        "zhipu": "glm-5.1",
        "volcano": "doubao-seed-2-0-code",
        "infini": "minimax-m2.7",
    }

    endpoint = endpoints[provider]
    model = models[provider]
    key_envvar = _API_KEY_ENVVARS[provider]
    api_key = os.environ.get(key_envvar)

    if not api_key:
        return ProbeResult(
            provider=provider,
            ok=False,
            latency_ms=None,
            detail=f"{key_envvar} not set",
        )

    payload = {
        "model": model,
        "max_tokens": 5,
        "messages": [{"role": "user", "content": "hi"}],
    }
    body = json.dumps(payload).encode("utf-8")

    try:
        start = time.perf_counter()
        req = urllib.request.Request(
            endpoint,
            data=body,
            headers={
                "Content-Type": "application/json",
                "x-api-key": api_key,
                "anthropic-version": "2023-06-01",
            },
            method="POST",
        )
        with urllib.request.urlopen(req, timeout=15) as resp:
            _ = resp.read()
        latency_ms = (time.perf_counter() - start) * 1000
        return ProbeResult(
            provider=provider,
            ok=True,
            latency_ms=round(latency_ms, 1),
            detail=f"OK ({latency_ms:.0f}ms)",
        )
    except urllib.error.HTTPError as exc:
        return ProbeResult(
            provider=provider,
            ok=False,
            latency_ms=None,
            detail=f"HTTP {exc.code}: {exc.reason}",
        )
    except urllib.error.URLError as exc:
        return ProbeResult(
            provider=provider,
            ok=False,
            latency_ms=None,
            detail=f"Network error: {exc.reason}",
        )
    except TimeoutError:
        return ProbeResult(
            provider=provider,
            ok=False,
            latency_ms=None,
            detail="Timeout (15s)",
        )
    except Exception as exc:
        return ProbeResult(
            provider=provider,
            ok=False,
            latency_ms=None,
            detail=str(exc),
        )


# ---------------------------------------------------------------------------
# Human-readable health display
# ---------------------------------------------------------------------------

_OK_MARK = "✔"
_FAIL_MARK = "✘"
_WARN_MARK = "⚠"


def format_health_display(checks: list[dict], provider_states: dict | None = None) -> str:
    """Render health checks as a human-readable table.

    Args:
        checks: List of check dicts with keys name, ok, detail.
        provider_states: Optional per-provider circuit-breaker state dict.

    Returns:
        Multi-line string suitable for terminal output.
    """
    lines: list[str] = []
    lines.append("")
    lines.append("mtor doctor — health report")
    lines.append("─" * 40)

    for check in checks:
        mark = _OK_MARK if check.get("ok") else _FAIL_MARK
        name = check.get("name", "unknown")
        detail = check.get("detail", "")
        lines.append(f"  {mark} {name}: {detail}")

    # Provider detail table
    if provider_states:
        lines.append("")
        lines.append("provider circuit-breaker states:")
        lines.append(f"  {'provider':<12} {'state':<12} {'failures':<10} {'cooldown'}")
        lines.append(f"  {'─' * 12} {'─' * 12} {'─' * 10} {'─' * 20}")
        now = time.time()
        for prov, info in provider_states.items():
            state = info.get("state", "closed")
            failures = info.get("consecutive_failures", 0)
            cooldown_epoch = info.get("cooldown_until")
            if cooldown_epoch and state == "open":
                remaining = max(0.0, cooldown_epoch - now)
                if remaining < 60:
                    cooldown_str = f"{remaining:.0f}s remaining"
                elif remaining < 3600:
                    cooldown_str = f"{remaining / 60:.1f}m remaining"
                else:
                    cooldown_str = f"{remaining / 3600:.1f}h remaining"
            else:
                cooldown_str = "—"
            mark = _OK_MARK if state == "closed" else (_WARN_MARK if state == "half_open" else _FAIL_MARK)
            lines.append(f"  {mark} {prov:<10} {state:<12} {failures:<10} {cooldown_str}")

    lines.append("─" * 40)
    all_ok = all(c.get("ok", False) for c in checks)
    status_word = "ALL CHECKS PASSED" if all_ok else "SOME CHECKS FAILED"
    lines.append(f"  {status_word}")
    lines.append("")
    return "\n".join(lines)

# Lazy import to avoid circular dependency
_providers_module: object | None = None


def _get_provider_module():
    global _providers_module
    if _providers_module is None:
        try:
            import mtor.worker.provider as _m

            _providers_module = _m
        except Exception:
            _providers_module = None
    return _providers_module


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

    # Check 2: WORKER_HOST not set to localhost
    worker_host_ok = WORKER_HOST != "localhost"
    if not worker_host_ok:
        all_ok = False
    checks.append(
        {
            "name": "worker_host",
            "ok": worker_host_ok,
            "detail": (
                f"MTOR_WORKER_HOST={WORKER_HOST}"
                if worker_host_ok
                else "MTOR_WORKER_HOST not set (defaults to localhost) "
                     "— SSH operations (logs, SHA gate) will fail. "
                     "Set MTOR_WORKER_HOST to your worker hostname."
            ),
        }
    )

    # Check 3: Worker alive (query for recent RUNNING workflows as a proxy)
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

    # Check 3: Coaching file present + size cap (optional — skip if not configured)
    from mtor import COACHING_MAX_KB

    if COACHING_PATH is not None:
        coaching_ok = os.path.exists(COACHING_PATH)
        coaching_detail = COACHING_PATH if coaching_ok else f"Missing: {COACHING_PATH}"
        if coaching_ok:
            size_kb = os.path.getsize(COACHING_PATH) / 1024
            if size_kb > COACHING_MAX_KB:
                coaching_ok = False
                coaching_detail = (
                    f"OVER BUDGET: {size_kb:.1f}KB > {COACHING_MAX_KB}KB cap. "
                    f"GLM exits immediately when coaching + spec > 15KB. Trim now."
                )
            else:
                coaching_detail = f"{coaching_detail} ({size_kb:.1f}KB / {COACHING_MAX_KB}KB)"
        checks.append(
            {"name": "coaching_file", "ok": coaching_ok, "detail": coaching_detail}
        )
    else:
        checks.append(
            {
                "name": "coaching_file",
                "ok": True,
                "detail": "Not configured (MTOR_COACHING_PATH unset)",
            }
        )

    # Check 4: Provider readiness — defer to circuit_breaker (Check 6) which
    # reads the actual provider HEALTH_FILE on WORKER_HOST. The earlier
    # ribosome-tools status probe was retired 2026-05-06 — that binary never
    # existed; circuit_breaker is the canonical health signal.

    result = {
        "temporal_reachable": temporal_ok,
        "temporal_host": TEMPORAL_HOST,
        "worker_alive": worker_ok,
        "task_queue": TASK_QUEUE,
        "checks": checks,
    }

    # Check 5: Real API probe — only meaningful when soma == worker (API keys
    # live on WORKER_HOST via op run, not in soma's shell). Skip with a
    # non-failing note when remote so doctor isn't a false negative.
    if WORKER_HOST != "localhost":
        checks.append(
            {
                "name": "provider_api_probe",
                "ok": True,
                "detail": (
                    f"Skipped — WORKER_HOST={WORKER_HOST}; provider keys live"
                    f" on {WORKER_HOST} via op run, not in soma shell."
                    f" See provider_circuit_breaker for actual health."
                ),
            }
        )
    else:
        probe_providers = []
        probe_threads_results: list[ProbeResult] = []

        def _run_probe(p: str) -> None:
            probe_threads_results.append(_probe_provider(p))

        import threading

        for p in ("zhipu", "volcano", "infini"):
            t = threading.Thread(target=_run_probe, args=(p,))
            t.start()
            probe_providers.append((p, t))

        for p, t in probe_providers:
            t.join()

        provider_probe_states: dict[str, dict] = {}
        for pr in probe_threads_results:
            provider_probe_states[pr.provider] = {
                "ok": pr.ok,
                "latency_ms": pr.latency_ms,
                "detail": pr.detail,
            }
        all_probes_ok = all(pr.ok for pr in probe_threads_results)
        if not all_probes_ok:
            all_ok = False
        probe_detail = ", ".join(
            f"{pr.provider}: {pr.detail}" for pr in probe_threads_results
        )
        checks.append(
            {
                "name": "provider_api_probe",
                "ok": all_probes_ok,
                "detail": probe_detail,
                "provider_probe_states": provider_probe_states,
            }
        )

    # Check 6: Circuit-breaker health state for each provider
    pm = _get_provider_module()
    if pm is not None and WORKER_HOST != "localhost":
        try:
            health_result = subprocess.run(
                [
                    "ssh",
                    WORKER_HOST,
                    "python3 -c \""
                    "import json; "
                    "h=json.load(open('"
                    + str(pm.HEALTH_FILE)
                    + "')); "
                    "print(json.dumps(h))\"",
                ],
                capture_output=True,
                text=True,
                timeout=15,
            )
            if health_result.returncode == 0 and health_result.stdout.strip():
                circuit_health = json.loads(health_result.stdout)
                provider_states = {}
                for prov in pm.PROVIDER_PRIORITY:
                    entry = circuit_health.get(prov, {})
                    state = entry.get("state", "closed")
                    cooldown = entry.get("cooldown_until")
                    failures = entry.get("consecutive_failures", 0)
                    provider_states[prov] = {
                        "state": state,
                        "cooldown_until": cooldown,
                        "consecutive_failures": failures,
                    }
                detail_parts = []
                for p, d in provider_states.items():
                    part = f"{p}={d['state']}"
                    if d["state"] == "open" and d["cooldown_until"]:
                        part += f" (cooldown={d['cooldown_until']})"
                    detail_parts.append(part)
                checks.append(
                    {
                        "name": "provider_circuit_breaker",
                        "ok": True,
                        "detail": ", ".join(detail_parts),
                        "provider_states": provider_states,
                    }
                )
                result["provider_circuit_breaker"] = provider_states
            else:
                checks.append(
                    {
                        "name": "provider_circuit_breaker",
                        "ok": True,
                        "detail": "No health records yet (all providers closed)",
                    }
                )
        except (subprocess.TimeoutExpired, OSError):
            checks.append(
                {
                    "name": "provider_circuit_breaker",
                    "ok": True,
                    "detail": "Health file not accessible via SSH",
                }
            )
        except Exception:
            checks.append(
                {
                    "name": "provider_circuit_breaker",
                    "ok": True,
                    "detail": "Could not read provider health state",
                }
            )
    elif pm is not None and WORKER_HOST == "localhost":
        # Local mode: read health file directly
        try:
            health = pm.load_health()
            provider_states = {}
            for prov in pm.PROVIDER_PRIORITY:
                entry = health.get(prov, {})
                state = entry.get("state", "closed")
                cooldown = entry.get("cooldown_until")
                failures = entry.get("consecutive_failures", 0)
                provider_states[prov] = {
                    "state": state,
                    "cooldown_until": cooldown,
                    "consecutive_failures": failures,
                }
            detail_parts = []
            for p, d in provider_states.items():
                part = f"{p}={d['state']}"
                if d["state"] == "open" and d["cooldown_until"]:
                    part += f" (cooldown={d['cooldown_until']})"
                detail_parts.append(part)
            checks.append(
                {
                    "name": "provider_circuit_breaker",
                    "ok": True,
                    "detail": ", ".join(detail_parts),
                    "provider_states": provider_states,
                }
            )
            result["provider_circuit_breaker"] = provider_states
        except Exception:
            checks.append(
                {
                    "name": "provider_circuit_breaker",
                    "ok": True,
                    "detail": "Could not read local provider health state",
                }
            )

    # Check 7: GitHub CLI auth on ganglion
    if WORKER_HOST == "localhost":
        checks.append(
            {
                "name": "ganglion_gh_auth",
                "ok": False,
                "detail": "Skipped — WORKER_HOST is localhost (set MTOR_WORKER_HOST first)",
            }
        )
    else:
        try:
            gh_result = subprocess.run(
                ["ssh", WORKER_HOST, "gh auth status"],
                capture_output=True,
                text=True,
                timeout=15,
            )
            if gh_result.returncode == 0:
                checks.append(
                    {
                        "name": "ganglion_gh_auth",
                        "ok": True,
                        "detail": f"GitHub CLI authenticated on {WORKER_HOST}",
                    }
                )
            else:
                all_ok = False
                snippet = (gh_result.stderr or gh_result.stdout or "").strip().split("\n")[0]
                checks.append(
                    {
                        "name": "ganglion_gh_auth",
                        "ok": False,
                        "detail": f"Not authenticated on {WORKER_HOST}: {snippet}",
                    }
                )
        except subprocess.TimeoutExpired:
            all_ok = False
            checks.append(
                {
                    "name": "ganglion_gh_auth",
                    "ok": False,
                    "detail": f"SSH to {WORKER_HOST} timed out",
                }
            )
        except OSError as exc:
            all_ok = False
            checks.append(
                {
                    "name": "ganglion_gh_auth",
                    "ok": False,
                    "detail": f"SSH to {WORKER_HOST} failed: {exc}",
                }
            )

    # Emit human-readable health report to stderr so JSON on stdout stays clean
    display = format_health_display(checks, result.get("provider_circuit_breaker"))
    sys.stderr.write(display)
    sys.stderr.flush()

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
