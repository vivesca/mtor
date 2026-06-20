"""Health check logic — Temporal reachability, worker liveness, provider info."""

from __future__ import annotations

import json
import os
import shlex
import socket
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

HEARTBEAT_DIR = "~/germline/loci/ribosome-heartbeats"
HEARTBEAT_STALE_THRESHOLD = 120

_CODING_PLAN_CONFIG_PATH = os.path.expanduser(
    "~/germline/loci/ribosome-config.lock.json"
)
_OPENCODE_CONFIG_PATH = os.path.expanduser("~/.config/opencode/opencode.json")
_CODING_PLAN_EXPECTED_URL = "https://open.bigmodel.cn/api/anthropic"
_OPENCODE_EXPECTED_PROVIDER = "zhipuai-coding-plan"
_OPENCODE_EXPECTED_API = "https://open.bigmodel.cn/api/coding/paas/v4"
_OPENCODE_EXPECTED_MODEL = "zhipuai-coding-plan/glm-5.2"
_OPENCODE_EXPECTED_SMALL_MODEL = "zhipuai-coding-plan/glm-4.5-air"

PROVIDER_MODELS = {
    "zhipu": "glm-5.2",
}


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
    classification: str = "unknown"  # ok|auth|billing|quota|connection|unknown


def _classify_response_error(http_status: int | None, body: str) -> str:
    """Classify a provider HTTP error into actionable categories.

    First-match-wins ordering: status code beats body pattern, billing beats auth
    when both match (404 with billing message → billing).

    Args:
        http_status: HTTP status code, or None for connection-level failures.
        body: Response body or error message.

    Returns:
        One of: "auth", "billing", "quota", "connection", "unknown".
    """
    body_lc = (body or "").lower()
    # Chinese billing patterns from zhipu / volcano
    if any(token in body for token in ("套餐", "已到期", "请续费", "已用完")):
        return "billing"
    # English billing patterns
    if any(
        token in body_lc
        for token in (
            "subscription expired",
            "plan expired",
            "renew your plan",
            "billing required",
            "credit expired",
            "payment required",
        )
    ):
        return "billing"
    # Auth patterns (key-related, not billing)
    if any(
        token in body_lc
        for token in (
            "invalid api key",
            "invalid key",
            "unauthorized",
            "api key invalid",
            "authentication parameter not received",
            "key is invalid",
        )
    ):
        return "auth"
    # Status-code dispatch
    if http_status == 401:
        return "auth"
    if http_status == 402:
        return "billing"
    if http_status == 429:
        return "quota"
    if http_status == 403:
        return "auth"  # often credential-scope rejection
    if http_status is None:
        return "connection"
    return "unknown"


def _probe_provider(provider: str) -> ProbeResult:
    """Send a real HTTP request to the provider's Anthropic Messages API.

    Args:
        provider: One of the active coding providers.

    Returns:
        ProbeResult with ok, latency, and detail.
    """
    endpoints = {
        "zhipu": "https://open.bigmodel.cn/api/anthropic/v1/messages",
    }
    models = {
        "zhipu": "glm-5.2",
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
            classification="auth",
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
            classification="ok",
        )
    except urllib.error.HTTPError as exc:
        body_text = ""
        try:
            body_bytes = exc.read()
            body_text = body_bytes.decode("utf-8", errors="replace")[:500]
        except Exception:
            body_text = ""
        classification = _classify_response_error(exc.code, body_text)
        # Surface body snippet in detail when it's the diagnostic signal
        body_hint = ""
        if body_text and classification in ("billing", "auth", "quota"):
            stripped = body_text.strip().replace("\n", " ")[:120]
            body_hint = f" — {stripped}"
        return ProbeResult(
            provider=provider,
            ok=False,
            latency_ms=None,
            detail=f"HTTP {exc.code}: {exc.reason}{body_hint}",
            classification=classification,
        )
    except urllib.error.URLError as exc:
        return ProbeResult(
            provider=provider,
            ok=False,
            latency_ms=None,
            detail=f"Network error: {exc.reason}",
            classification="connection",
        )
    except TimeoutError:
        return ProbeResult(
            provider=provider,
            ok=False,
            latency_ms=None,
            detail="Timeout (15s)",
            classification="connection",
        )
    except Exception as exc:
        return ProbeResult(
            provider=provider,
            ok=False,
            latency_ms=None,
            detail=str(exc),
            classification="unknown",
        )


def _check_coding_plan_lane(config_path: str | None = None) -> dict:
    """Verify ribosome's zhipu provider uses the coding-plan lane."""
    path = config_path or _CODING_PLAN_CONFIG_PATH
    expected = _CODING_PLAN_EXPECTED_URL

    try:
        with open(path) as f:
            cfg = json.load(f)
    except FileNotFoundError:
        return {
            "name": "coding_plan_lane",
            "ok": False,
            "detail": f"Config file not found: {path}",
            "base_url": None,
            "budget_lane": None,
        }

    zhipu = cfg.get("providers", {}).get("zhipu", {})
    base_url = zhipu.get("base_url", "")
    key_env = zhipu.get("key_env", "ZHIPU_API_KEY")
    models = zhipu.get("models", {})

    if "api.z.ai" in base_url:
        return {
            "name": "coding_plan_lane",
            "ok": False,
            "detail": f"REJECTED: api.z.ai mirror detected: {base_url}",
            "base_url": base_url,
            "budget_lane": "unknown",
            "key_env": key_env,
            "models": models,
        }

    if base_url != expected:
        return {
            "name": "coding_plan_lane",
            "ok": False,
            "detail": f"Unexpected base_url: {base_url} (expected {expected})",
            "base_url": base_url,
            "budget_lane": "unknown",
            "key_env": key_env,
            "models": models,
        }

    return {
        "name": "coding_plan_lane",
        "ok": True,
        "detail": f"zhipu-coding-plan via {base_url} (models: {json.dumps(models)})",
        "base_url": base_url,
        "budget_lane": "zhipu-coding-plan",
        "key_env": key_env,
        "models": models,
    }


def _secret_placeholder_ok(value: object) -> bool:
    """Return True when a config value delegates secret loading to the environment."""
    return isinstance(value, str) and value.startswith("{env:") and value.endswith("}")


def _authorization_placeholder_ok(value: object) -> bool:
    """Return True when an Authorization header uses an environment placeholder."""
    return (
        isinstance(value, str)
        and value.startswith("Bearer {env:")
        and value.endswith("}")
    )


def _check_opencode_config_payload(config: dict, *, source: str = "local") -> dict:
    """Validate an OpenCode config payload against the BigModel coding-plan lane."""
    if not isinstance(config, dict):
        return {
            "name": f"opencode_config_{source}",
            "ok": False,
            "detail": f"{source}: config root is {type(config).__name__}, expected object",
        }
    provider = config.get("provider", {})
    provider_keys = set(provider)
    zhipu_provider = provider.get(_OPENCODE_EXPECTED_PROVIDER, {})
    options = zhipu_provider.get("options", {})
    permission = config.get("permission", {})
    mcp = config.get("mcp", {})

    failures: list[str] = []
    if provider_keys != {_OPENCODE_EXPECTED_PROVIDER}:
        failures.append(f"provider keys={sorted(provider_keys)}")
    if options.get("baseURL") != _OPENCODE_EXPECTED_API:
        failures.append(f"baseURL={options.get('baseURL')!r}")
    if not _secret_placeholder_ok(options.get("apiKey")):
        failures.append("apiKey is not an env placeholder")
    if config.get("model") != _OPENCODE_EXPECTED_MODEL:
        failures.append(f"model={config.get('model')!r}")
    if config.get("small_model") != _OPENCODE_EXPECTED_SMALL_MODEL:
        failures.append(f"small_model={config.get('small_model')!r}")
    if (
        permission.get("*") != "allow"
        or permission.get("external_directory", {}).get("*") != "allow"
    ):
        failures.append("permissions do not allow external_directory")

    for name, entry in mcp.items():
        auth_header = entry.get("headers", {}).get("Authorization")
        if auth_header is not None and not _authorization_placeholder_ok(auth_header):
            failures.append(f"mcp {name} Authorization is not an env placeholder")

    return {
        "name": f"opencode_config_{source}",
        "ok": not failures,
        "detail": (
            f"{source}: {_OPENCODE_EXPECTED_MODEL} via {_OPENCODE_EXPECTED_API}"
            if not failures
            else f"{source}: " + "; ".join(failures)
        ),
        "provider": _OPENCODE_EXPECTED_PROVIDER,
        "model": config.get("model"),
        "small_model": config.get("small_model"),
        "base_url": options.get("baseURL"),
    }


def _check_opencode_config_file(
    config_path: str | None = None, *, source: str = "local"
) -> dict:
    """Validate a local OpenCode config file."""
    path = config_path or _OPENCODE_CONFIG_PATH
    try:
        with open(path) as f:
            config = json.load(f)
    except FileNotFoundError:
        return {
            "name": f"opencode_config_{source}",
            "ok": False,
            "detail": f"Config file not found: {path}",
        }
    except json.JSONDecodeError as exc:
        return {
            "name": f"opencode_config_{source}",
            "ok": False,
            "detail": f"Config is not valid JSON: {exc}",
        }
    return _check_opencode_config_payload(config, source=source)


def _check_worker_opencode_config() -> dict:
    """Validate OpenCode config on the worker host without exposing secrets."""
    if WORKER_HOST == "localhost":
        return _check_opencode_config_file(source="worker")
    try:
        result = subprocess.run(
            ["ssh", WORKER_HOST, "cat ~/.config/opencode/opencode.json"],
            capture_output=True,
            text=True,
            timeout=15,
        )
    except (subprocess.TimeoutExpired, OSError) as exc:
        return {
            "name": "opencode_config_worker",
            "ok": False,
            "detail": f"Cannot read worker OpenCode config: {exc}",
        }
    if result.returncode != 0:
        return {
            "name": "opencode_config_worker",
            "ok": False,
            "detail": f"Cannot read worker OpenCode config: {result.stderr.strip()[:200]}",
        }
    try:
        config = json.loads(result.stdout)
    except json.JSONDecodeError as exc:
        return {
            "name": "opencode_config_worker",
            "ok": False,
            "detail": f"Worker config is not valid JSON: {exc}",
        }
    return _check_opencode_config_payload(config, source="worker")


def _check_worker_opencode_runtime() -> dict:
    """Run a minimal worker-host OpenCode probe through the worker bootstrap path."""
    probe_prompt = "Reply with exactly coding-plan-ok and do not edit files."
    quoted_prompt = shlex.quote(probe_prompt)
    command = (
        'tmp=$(mktemp -d); cd "$tmp"; git init -q; '
        "git config user.email canary@example.invalid; "
        "git config user.name Canary; "
        'source "$HOME/.env.bootstrap"; '
        'timeout 120 op run --env-file "$HOME/germline/loci/env.op" -- '
        "opencode run --model zhipuai-coding-plan/glm-5.2 --format json "
        '--dangerously-skip-permissions --dir "$PWD" '
        f"{quoted_prompt}"
    )
    run_command = (
        ["bash", "-lc", command]
        if WORKER_HOST == "localhost"
        else ["ssh", WORKER_HOST, shlex.join(["bash", "-lc", command])]
    )
    try:
        result = subprocess.run(
            run_command,
            capture_output=True,
            text=True,
            timeout=150,
        )
    except (subprocess.TimeoutExpired, OSError) as exc:
        return {
            "name": "opencode_runtime_probe",
            "ok": False,
            "detail": f"Probe failed before completion: {exc}",
        }

    output = result.stdout
    ok = result.returncode == 0 and "coding-plan-ok" in output
    detail = (
        "zhipuai-coding-plan/glm-5.2 returned coding-plan-ok"
        if ok
        else (
            f"exit={result.returncode}; stdout={len(result.stdout)} bytes; "
            f"stderr={len(result.stderr)} bytes"
        )
    )
    return {
        "name": "opencode_runtime_probe",
        "ok": ok,
        "detail": detail,
    }


# ---------------------------------------------------------------------------
# Human-readable health display
# ---------------------------------------------------------------------------

_OK_MARK = "✔"
_FAIL_MARK = "✘"
_WARN_MARK = "⚠"


def _format_duration(seconds: float) -> str:
    if seconds < 60:
        return f"{seconds:.0f}s"
    if seconds < 3600:
        return f"{seconds / 60:.1f}m"
    return f"{seconds / 3600:.1f}h"


def _provider_health_explanation(
    provider: str, info: dict, now: float | None = None
) -> str:
    """Return a human-readable circuit-breaker explanation for one provider."""
    now = time.time() if now is None else now
    state = info.get("state", "closed")
    active = info.get("active_count", 0)
    limit = info.get("limit", 2)
    failures = info.get("consecutive_failures", 0)
    cooldown_until = info.get("cooldown_until")

    if state == "closed":
        return f"healthy ({active} active, limit {limit})"
    if state == "half_open":
        return f"probing recovery ({active} active, limit {limit})"
    if state == "open":
        if cooldown_until:
            remaining = max(0.0, cooldown_until - now)
            failure_note = f", {failures} failures" if failures else ""
            return f"cooldown (reopens in {_format_duration(remaining)}{failure_note})"
        return f"cooldown ({failures} failures)" if failures else "cooldown"
    return f"{state} ({provider})"


def format_health_display(
    checks: list[dict], provider_states: dict | None = None
) -> str:
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
        now = time.time()
        for prov, info in provider_states.items():
            state = info.get("state", "closed")
            mark = (
                _OK_MARK
                if state == "closed"
                else (_WARN_MARK if state == "half_open" else _FAIL_MARK)
            )
            lines.append(
                f"  {mark} {prov:<10} {_provider_health_explanation(prov, info, now)}"
            )

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


def reconcile_running_workflows(client) -> list[dict]:
    """Classify RUNNING workflows by heartbeat freshness.

    Args:
        client: Connected Temporal client.

    Returns:
        List of dicts with workflow_id, classification, last_heartbeat_age_s,
        and suggested next_action.
    """
    import asyncio

    async def _list_running():
        results = []
        async for wf in client.list_workflows(query="ExecutionStatus = 'Running'"):
            results.append(wf)
        return results

    running = asyncio.run(_list_running())
    now = time.time()
    classifications = []

    for wf in running:
        wf_id = wf.id
        heartbeat_path = f"{HEARTBEAT_DIR}/{wf_id}"
        try:
            stat_result = subprocess.run(
                [
                    "ssh",
                    WORKER_HOST,
                    f"stat -c %Y {heartbeat_path} 2>/dev/null || echo MISSING",
                ],
                capture_output=True,
                text=True,
                timeout=15,
            )
            output = stat_result.stdout.strip()
        except (subprocess.TimeoutExpired, OSError):
            output = "MISSING"

        if output == "MISSING" or not output:
            classification = "abandoned"
            age_s = None
        else:
            try:
                mtime = float(output)
                age_s = now - mtime
                if age_s <= HEARTBEAT_STALE_THRESHOLD:
                    classification = "alive"
                else:
                    classification = "stale"
            except ValueError:
                classification = "abandoned"
                age_s = None

        entry: dict[str, object] = {
            "workflow_id": wf_id,
            "classification": classification,
        }
        if age_s is not None:
            entry["last_heartbeat_age_s"] = round(age_s, 1)
        if classification in ("stale", "abandoned"):
            entry["next_action"] = f"mtor cancel {wf_id}"
        classifications.append(entry)

    return classifications


def doctor(*, reconcile: bool = False, probe_opencode: bool = False) -> None:
    """Health check: Temporal reachability, worker liveness, provider info."""
    cmd = "mtor doctor"
    checks = []
    all_ok = True
    probe_opencode = probe_opencode or os.environ.get("MTOR_PROBE_OPENCODE") == "1"

    # Check 1: Temporal server reachable
    client, err = _get_client()
    temporal_ok = err is None
    if not temporal_ok:
        all_ok = False
    checks.append(
        {
            "name": "temporal_reachable",
            "ok": temporal_ok,
            "detail": f"Connected to {TEMPORAL_HOST}"
            if temporal_ok
            else f"Cannot connect: {err}",
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

    # Check 3b: authoritative rictor topology. Temporal visibility can be
    # reachable while stale or duplicate worker services are still present.
    try:
        from mtor.infra import check_health as _rictor_check_health

        rictor_report = _rictor_check_health()
        rictor_detail = "; ".join(
            f"{c.get('name')}={c.get('ok')}" for c in rictor_report.checks
        )
        checks.append(
            {"name": "rictor_topology", "ok": rictor_report.ok, "detail": rictor_detail}
        )
        if not rictor_report.ok:
            all_ok = False
        result_rictor_checks = rictor_report.checks
    except Exception as exc:
        all_ok = False
        checks.append(
            {"name": "rictor_topology", "ok": False, "detail": str(exc)[:200]}
        )
        result_rictor_checks = []

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
                coaching_detail = (
                    f"{coaching_detail} ({size_kb:.1f}KB / {COACHING_MAX_KB}KB)"
                )
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
        "rictor_checks": result_rictor_checks,
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

        for p in ("zhipu",):
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
                "classification": pr.classification,
            }
        all_probes_ok = all(pr.ok for pr in probe_threads_results)
        if not all_probes_ok:
            all_ok = False
        probe_detail = ", ".join(
            f"{pr.provider}: [{pr.classification}] {pr.detail}"
            for pr in probe_threads_results
        )
        checks.append(
            {
                "name": "provider_api_probe",
                "ok": all_probes_ok,
                "detail": probe_detail,
                "provider_probe_states": provider_probe_states,
            }
        )

    # Check 5b: Coding-plan lane validation
    lane_check = _check_coding_plan_lane()
    if not lane_check["ok"]:
        all_ok = False
    checks.append(lane_check)

    # Check 5c: OpenCode config agreement on local and worker hosts.
    local_opencode_check = _check_opencode_config_file(source="local")
    if not local_opencode_check["ok"]:
        all_ok = False
    checks.append(local_opencode_check)

    worker_opencode_check = _check_worker_opencode_config()
    if not worker_opencode_check["ok"]:
        all_ok = False
    checks.append(worker_opencode_check)

    if probe_opencode:
        runtime_probe = _check_worker_opencode_runtime()
        if not runtime_probe["ok"]:
            all_ok = False
        checks.append(runtime_probe)
    else:
        checks.append(
            {
                "name": "opencode_runtime_probe",
                "ok": True,
                "detail": "Skipped; run mtor doctor --probe-opencode for a live BigModel coding-plan call",
            }
        )

    # Check 6: Circuit-breaker health state for each provider
    pm = _get_provider_module()
    default_provider_states = {}
    if pm is not None:
        route_parts = [
            f"{provider}({PROVIDER_MODELS.get(provider, provider)}, limit={pm.PROVIDER_LIMITS.get(provider, 2)})"
            for provider in pm.PROVIDER_PRIORITY
        ]
        default_provider_states = {
            provider: {
                "state": "closed",
                "cooldown_until": None,
                "consecutive_failures": 0,
                "active_count": pm._active_count.get(provider, 0),
                "limit": pm.PROVIDER_LIMITS.get(provider, 2),
            }
            for provider in pm.PROVIDER_PRIORITY
        }
        checks.append(
            {
                "name": "provider_routing",
                "ok": True,
                "detail": "priority: " + " > ".join(route_parts),
            }
        )
        result["provider_routing"] = {
            "priority": list(pm.PROVIDER_PRIORITY),
            "models": {
                provider: PROVIDER_MODELS.get(provider, provider)
                for provider in pm.PROVIDER_PRIORITY
            },
            "limits": {
                provider: pm.PROVIDER_LIMITS.get(provider, 2)
                for provider in pm.PROVIDER_PRIORITY
            },
        }

    if pm is not None and WORKER_HOST != "localhost":
        try:
            health_result = subprocess.run(
                [
                    "ssh",
                    WORKER_HOST,
                    'python3 -c "'
                    "import json; "
                    "h=json.load(open('" + str(pm.HEALTH_FILE) + "')); "
                    'print(json.dumps(h))"',
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
                        "active_count": pm._active_count.get(prov, 0),
                        "limit": pm.PROVIDER_LIMITS.get(prov, 2),
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
                result["provider_circuit_breaker"] = default_provider_states
        except (subprocess.TimeoutExpired, OSError):
            checks.append(
                {
                    "name": "provider_circuit_breaker",
                    "ok": True,
                    "detail": "Health file not accessible via SSH",
                }
            )
            result["provider_circuit_breaker"] = default_provider_states
        except Exception:
            checks.append(
                {
                    "name": "provider_circuit_breaker",
                    "ok": True,
                    "detail": "Could not read provider health state",
                }
            )
            result["provider_circuit_breaker"] = default_provider_states
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
                    "active_count": pm._active_count.get(prov, 0),
                    "limit": pm.PROVIDER_LIMITS.get(prov, 2),
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
            local_names = {socket.gethostname(), socket.getfqdn(), "localhost"}
            command = (
                ["gh", "auth", "status"]
                if WORKER_HOST in local_names
                else ["ssh", WORKER_HOST, "gh auth status"]
            )
            gh_result = subprocess.run(
                command,
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
                snippet = (
                    (gh_result.stderr or gh_result.stdout or "").strip().split("\n")[0]
                )
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

    # Reconciliation: classify RUNNING workflows by heartbeat freshness
    if reconcile and temporal_ok and client is not None:
        try:
            classifications = reconcile_running_workflows(client)
            result["reconciliation"] = {
                "workflows": classifications,
                "count": len(classifications),
            }
        except Exception as reconcile_exc:
            result["reconciliation"] = {"error": str(reconcile_exc)}

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
            "fix": f"Start mtor worker: ssh {WORKER_HOST} 'systemctl --user start mtor-worker'",
            "result": result,
            "next_actions": [
                _action(
                    f"ssh {WORKER_HOST} 'systemctl --user status mtor-worker'",
                    "Check worker service status",
                ),
                _action(
                    f"ssh {WORKER_HOST} 'systemctl --user start mtor-worker'",
                    "Start the worker",
                ),
            ],
        }
        sys.stdout.write(json.dumps(payload) + "\n")
        sys.stdout.flush()
        sys.exit(3)
