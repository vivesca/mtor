"""Core dispatch logic — send a task prompt to Temporal."""

from __future__ import annotations

import contextlib
import hashlib
import re
import shlex
import subprocess
import sys
import time
from pathlib import Path

from porin import action as _action

from mtor import (
    EXPECTED_GERMLINE_REMOTE,
    EXPECTED_WORKER_BRANCH,
    TASK_QUEUE,
    TEMPORAL_HOST,
    VERSION,
    WORKER_GERMLINE_DIR,
    WORKER_HOST,
    WORKFLOW_TYPE,
)
from mtor.client import _get_client
from mtor.dedup import (
    DEFAULT_STATE_PATH,
    DEFAULT_WINDOW_S,
    _load_state,
    _prune,
    compute_identity,
)
from mtor.envelope import _err, _ok


# ---------------------------------------------------------------------------
# Task risk classification
# ---------------------------------------------------------------------------

RISK_PATTERNS: dict[str, list[str]] = {
    "high": [
        "delete",
        "remove",
        "drop",
        "config",
        "infra",
        "deploy",
        "migrate",
        "rename",
    ],
    "low": ["test", "doc", "readme", "comment", "add test", "write test", "new file"],
}
# Default: "medium"


def classify_risk(prompt: str) -> str:
    """Classify a task prompt by risk level for merge gating."""
    lower = prompt.lower()
    for level, patterns in RISK_PATTERNS.items():
        if any(p in lower for p in patterns):
            return level
    return "medium"


# ---------------------------------------------------------------------------
# Task-type routing
# ---------------------------------------------------------------------------

ROUTE_PATTERNS: dict[str, list[str]] = {
    "explore": [
        "how does",
        "find ",
        "search ",
        "what is",
        "explain",
        "where is",
        "list all",
        "show me",
    ],
    "bugfix": ["fix ", "bug", "broken", "error ", "failing", "crash", "regression"],
    "test": ["write test", "add test", "test for", "coverage"],
    "research": [
        "research ",
        "compare ",
        "evaluate ",
        "what is the latest",
        "how do others",
        "pricing",
        "benchmark",
    ],
    "receptor": ["receptor", "skill", "membrane/receptors"],
}

ROUTE_TO_PROVIDER: dict[str, str] = {
    "explore": "zhipu",
    "bugfix": "zhipu",
    "build": "zhipu",
    "test": "zhipu",
    "research": "zhipu",
    "receptor": "zhipu",
}

RETIRED_PROVIDERS: dict[str, str] = {
    "infini": "Infini CodingPlan subscription is inactive",
    "volcano": "Volcano Engine CodingPlan subscription is inactive",
}


def _resolve_default_provider(spec_mode: str) -> str:
    """Return the default provider for a spec mode."""
    return ROUTE_TO_PROVIDER.get(spec_mode, "zhipu")


def _receptor_suffix() -> str:
    return (
        "\n\nThis is a RECEPTOR task for Vivesca skill files. "
        "Only edit files allowed by the spec scope under membrane/receptors/. "
        "Preserve frontmatter, triggers, and existing local style. "
        "Do not edit genome.md, epigenome/marks/, or unrelated skills. "
        "Run the verification command from the spec before committing."
    )


# ---------------------------------------------------------------------------
# Workflow ID generation
# ---------------------------------------------------------------------------

PROVIDER_TO_MODEL: dict[str, str] = {
    "zhipu": "glm52",
    "infini": "mm27",
    "volcano": "doubao",
    "gemini": "gem31",
    "codex": "gpt54",
    "opencode": "glm52o",
    "goose": "glm51g",
    "droid": "glm5",
}


def _known_providers() -> set[str]:
    """All provider names mtor recognizes, including retired ones.

    Retired providers stay "known" so an explicit retired provider keeps
    returning PROVIDER_RETIRED rather than the harsher PROVIDER_UNKNOWN.
    """
    return (
        set(PROVIDER_TO_MODEL)
        | set(ROUTE_TO_PROVIDER.values())
        | set(RETIRED_PROVIDERS)
    )


def _provider_validation_error(provider: str | None) -> str | None:
    """Map an explicit provider to a blocked-reason tag, or None if acceptable.

    - retired provider -> "provider_retired"
    - unknown explicit provider -> "provider_unknown"
    - default resolution (provider is None) or a known provider -> None
    """
    if not provider:
        return None
    if provider in RETIRED_PROVIDERS:
        return "provider_retired"
    if provider not in _known_providers():
        return "provider_unknown"
    return None


_SLUG_WORD_RE = re.compile(r"[^a-z0-9]+")


def _slugify(text: str) -> str:
    """Lowercase, drop apostrophes, replace non-alphanumeric runs with single hyphen."""
    return _SLUG_WORD_RE.sub("-", text.lower().replace("'", "")).strip("-")


def _make_workflow_id(prompt: str, provider: str, harness: str = "ribosome") -> str:
    """Build a semi-deterministic workflow ID: {harness}-{model}-{slug}-{hash}-{ts}.

    - model: short name mapped from *provider*
    - slug: first 3 words of *prompt*, slugified
    - hash: 8-char hex from sha256 of *prompt*
    - ts: hex unix epoch seconds (allows re-dispatch after archiving)
    - total length capped at 80 characters (slug truncated if needed)
    """
    model = PROVIDER_TO_MODEL.get(provider, provider)
    prompt_hash = hashlib.sha256(prompt.encode()).hexdigest()[:8]
    ts = format(int(time.time()), "x")

    words = prompt.split()
    slug = _slugify(" ".join(words[:3]))

    # Assemble and enforce 80-char limit
    wid = f"{harness}-{model}-{slug}-{prompt_hash}-{ts}"
    if len(wid) > 80:
        # Truncate slug to fit: harness-model--hash-ts + safety margin
        overhead = (
            len(harness) + 1 + len(model) + 1 + 1 + len(prompt_hash) + 1 + len(ts)
        )
        max_slug = 80 - overhead
        slug = slug[: max(0, max_slug)].rstrip("-")
        wid = f"{harness}-{model}-{slug}-{prompt_hash}-{ts}"

    return wid


_CHECKOUT_OK: dict = {
    "ok": True,
    "branch": "",
    "origin": "",
    "dirty": False,
    "status": "",
    "detail": "",
}

_TARGET_REPO_OK: dict = {
    "ok": True,
    "skipped": False,
    "local_sha": "",
    "worker_sha": "",
    "origin_sha": "",
    "branch": "",
    "dirty": False,
    "detail": "",
}


def _worker_checkout_state() -> dict:
    """Inspect worker ~/germline checkout hygiene via a single SSH call.

    Returns a dict with keys: ok, branch, origin, dirty, status, detail.
    """
    germline_dir = shlex.quote(WORKER_GERMLINE_DIR)
    remote_cmd = (
        f"cd {germline_dir} && "
        "printf 'BRANCH:%s\\n' \"$(git rev-parse --abbrev-ref HEAD)\" && "
        "printf 'ORIGIN:%s\\n' \"$(git remote get-url origin)\" && "
        "printf '%s\\n' 'MTOR_STATUS_START' && "
        "git status --porcelain=v1 -uall"
    )
    result = subprocess.run(
        ["ssh", WORKER_HOST, remote_cmd],
        capture_output=True,
        text=True,
        timeout=10,
    )
    if result.returncode != 0:
        return {
            "ok": False,
            "branch": "",
            "origin": "",
            "dirty": False,
            "status": "",
            "detail": f"worker checkout unhealthy: SSH check failed: {result.stderr.strip()}",
        }

    branch = ""
    origin = ""
    status_lines: list[str] = []
    in_status = False
    for line in result.stdout.split("\n"):
        if line.startswith("BRANCH:"):
            branch = line[7:]
        elif line.startswith("ORIGIN:"):
            origin = line[7:]
        elif line == "MTOR_STATUS_START":
            in_status = True
        elif in_status and line:
            status_lines.append(line)

    dirty = bool(status_lines)
    status = "\n".join(status_lines)

    errors: list[str] = []
    if branch != EXPECTED_WORKER_BRANCH:
        errors.append(f"branch is {branch!r}, expected {EXPECTED_WORKER_BRANCH!r}")
    if origin != EXPECTED_GERMLINE_REMOTE:
        errors.append(f"origin is {origin!r}, expected {EXPECTED_GERMLINE_REMOTE!r}")
    if dirty:
        errors.append(f"dirty/untracked files: {status}")

    ok = not errors
    detail = "worker checkout unhealthy: " + "; ".join(errors) if errors else ""

    return {
        "ok": ok,
        "branch": branch,
        "origin": origin,
        "dirty": dirty,
        "status": status,
        "detail": detail,
    }


def _check_worker_checkout() -> None:
    """Raise RuntimeError if worker ~/germline checkout is unhealthy."""
    state = _worker_checkout_state()
    if not state["ok"]:
        raise RuntimeError(state["detail"])


def _check_worker_sha(*, skip: bool = False, repo: str | None = None) -> bool:
    """Compare local HEAD with worker HEAD. Returns True if in sync.

    Raises RuntimeError on failures.  If out of sync and skip=False,
    auto-deploys (push + merge + restart) before returning True.

    When *repo* is provided, the local SHA lookup uses ``git -C <repo>``
    instead of the caller's cwd.  This matters for ``--spec`` dispatch where
    the spec frontmatter declares the target repo.
    """
    if skip:
        return True

    local_cmd = ["git"]
    if repo:
        local_cmd += ["-C", str(Path(repo).expanduser())]
    local_cmd += ["rev-parse", "HEAD"]

    local = subprocess.run(
        local_cmd,
        capture_output=True,
        text=True,
        timeout=5,
    )
    if local.returncode != 0:
        raise RuntimeError(f"local git HEAD lookup failed: {local.stderr.strip()}")

    remote = subprocess.run(
        ["ssh", WORKER_HOST, "cd ~/germline && git rev-parse HEAD"],
        capture_output=True,
        text=True,
        timeout=10,
    )
    if remote.returncode != 0:
        raise RuntimeError(f"worker git HEAD lookup failed: {remote.stderr.strip()}")

    _check_worker_checkout()

    if local.stdout.strip() == remote.stdout.strip():
        return True

    # Auto-deploy: push + fast-forward + restart.
    # Use -C to pin git context to ~/germline regardless of caller cwd —
    # otherwise dispatching from a non-`main` repo (e.g. quorate on master)
    # fails with bogus "src refspec main does not match any".
    germline_dir = str(Path.home() / "germline")
    push = subprocess.run(
        [
            "git",
            "-C",
            germline_dir,
            "push",
            "origin",
            "HEAD:main",
        ],
        capture_output=True,
        text=True,
        timeout=120,
    )
    if push.returncode != 0:
        raise RuntimeError(f"push failed: {push.stderr.strip()}")

    # Fast-forward the worker checkout, then verify the worker HEAD actually
    # advanced to the pushed SHA. GitHub push-propagation lag can leave the
    # worker's `git fetch origin main` seeing the OLD origin/main, so
    # `git merge --ff-only origin/main` is a no-op that still exits 0 —
    # trusting that exit restarts the worker on stale code and reports "in
    # sync". Retry the fetch/merge a few times with a short backoff to absorb
    # the lag; fail closed (before restart) if the worker never reaches the
    # pushed SHA. Mirrors infra.deploy() step 2.
    pushed_sha = local.stdout.strip()
    merge_attempts = 3
    merge_backoff = 2.0
    merge_ok = False
    worker_sha = ""
    for attempt in range(1, merge_attempts + 1):
        merge = subprocess.run(
            [
                "ssh",
                WORKER_HOST,
                (
                    "cd ~/germline && "
                    "git fetch origin main && "
                    "git merge --ff-only origin/main"
                ),
            ],
            capture_output=True,
            text=True,
            timeout=30,
        )
        if merge.returncode != 0:
            raise RuntimeError(f"merge failed: {merge.stderr.strip()}")
        worker_head = subprocess.run(
            ["ssh", WORKER_HOST, "cd ~/germline && git rev-parse HEAD"],
            capture_output=True,
            text=True,
            timeout=10,
        )
        worker_sha = worker_head.stdout.strip()
        if worker_head.returncode == 0 and worker_sha == pushed_sha:
            merge_ok = True
            break
        if attempt < merge_attempts:
            time.sleep(merge_backoff)

    if not merge_ok:
        raise RuntimeError(
            "worker HEAD did not advance to pushed SHA after "
            f"{merge_attempts} attempts: local {pushed_sha[:8] or '<none>'} "
            f"worker {worker_sha[:8] or '<none>'}"
        )

    restart = subprocess.run(
        ["ssh", WORKER_HOST, "systemctl --user restart mtor-worker"],
        capture_output=True,
        text=True,
        timeout=15,
    )
    if restart.returncode != 0:
        raise RuntimeError(f"restart failed: {restart.stderr.strip()}")

    time.sleep(3)
    return True


def _worker_sha_plan(*, skip: bool = False, repo: str | None = None) -> dict:
    """Return worker SHA state without deploying or restarting anything."""
    if skip:
        return {
            "skipped": True,
            "in_sync": True,
            "auto_deploy_would_occur": False,
            "local_sha": "",
            "worker_sha": "",
            "error": "",
            "worker_checkout": {**_CHECKOUT_OK},
        }

    state: dict = {
        "skipped": False,
        "in_sync": False,
        "auto_deploy_would_occur": False,
        "local_sha": "",
        "worker_sha": "",
        "error": "",
        "worker_checkout": {**_CHECKOUT_OK},
    }
    try:
        local_cmd = ["git"]
        if repo:
            local_cmd += ["-C", str(Path(repo).expanduser())]
        local_cmd += ["rev-parse", "HEAD"]
        local = subprocess.run(
            local_cmd,
            capture_output=True,
            text=True,
            timeout=5,
        )
        if local.returncode != 0:
            state["error"] = f"local git HEAD lookup failed: {local.stderr.strip()}"
            return state
        remote = subprocess.run(
            ["ssh", WORKER_HOST, "cd ~/germline && git rev-parse HEAD"],
            capture_output=True,
            text=True,
            timeout=10,
        )
        if remote.returncode != 0:
            state["error"] = f"worker git HEAD lookup failed: {remote.stderr.strip()}"
            return state
        state["local_sha"] = local.stdout.strip()
        state["worker_sha"] = remote.stdout.strip()
        state["in_sync"] = state["local_sha"] == state["worker_sha"]
        state["auto_deploy_would_occur"] = not state["in_sync"]

        checkout = _worker_checkout_state()
        state["worker_checkout"] = checkout
        if not checkout["ok"]:
            state["error"] = checkout["detail"]
    except (OSError, subprocess.TimeoutExpired) as exc:
        state["error"] = str(exc)
    return state


def _worker_addressable_repo_path(repo: str | None) -> str:
    """Return a worker-usable absolute path, or empty string when not addressable."""
    if not repo:
        return ""

    raw = str(repo).strip()
    if raw in (".", "~"):
        return ""

    if raw == "~/germline" or raw.startswith("~/germline/"):
        return "/home/vivesca" + raw[1:]
    if raw == "~/code" or raw.startswith("~/code/"):
        return "/home/vivesca" + raw[1:]
    if raw == "/home/vivesca/germline" or raw.startswith("/home/vivesca/germline/"):
        return raw
    if raw == "/home/vivesca/code" or raw.startswith("/home/vivesca/code/"):
        return raw

    users_code = re.match(r"^/Users/[^/]+/code(/.*)?$", raw)
    if users_code:
        return "/home/vivesca/code" + (users_code.group(1) or "")

    users_germline = re.match(r"^/Users/[^/]+/germline(/.*)?$", raw)
    if users_germline:
        return "/home/vivesca/germline" + (users_germline.group(1) or "")

    return ""


def _target_repo_allowed_noise(status_line: str) -> bool:
    """Allow untracked target-repo worktree directories as benign status noise."""
    if not status_line.startswith("?? "):
        return False
    path = status_line[3:].strip()
    return path == ".worktrees/" or path.startswith(".worktrees/")


def _worker_target_repo_state(repo: str | None, *, skip: bool = False) -> dict:
    """Inspect local/worker target repo state without mutating either checkout."""
    state = {**_TARGET_REPO_OK}
    if skip:
        state["skipped"] = True
        state["detail"] = "skipped by --skip-sha-check"
        return state

    worker_repo = _worker_addressable_repo_path(repo)
    if not worker_repo:
        state["skipped"] = True
        state["detail"] = "no worker-addressable target repo"
        return state

    try:
        local = subprocess.run(
            ["git", "-C", str(Path(str(repo)).expanduser()), "rev-parse", "HEAD"],
            capture_output=True,
            text=True,
            timeout=5,
        )
        if local.returncode != 0:
            state["ok"] = False
            state["detail"] = f"local target HEAD lookup failed: {local.stderr.strip()}"
            return state

        remote_cmd = (
            f"cd {shlex.quote(worker_repo)} && "
            "printf 'BRANCH:%s\\n' \"$(git rev-parse --abbrev-ref HEAD)\" && "
            "printf 'HEAD:%s\\n' \"$(git rev-parse HEAD)\" && "
            "printf 'ORIGIN_MAIN:%s\\n' "
            "\"$(git ls-remote origin refs/heads/main | awk '{print $1}')\" && "
            "printf '%s\\n' 'MTOR_STATUS_START' && "
            "git status --porcelain=v1 -uall"
        )
        remote = subprocess.run(
            ["ssh", WORKER_HOST, remote_cmd],
            capture_output=True,
            text=True,
            timeout=15,
        )
        if remote.returncode != 0:
            state["ok"] = False
            state["local_sha"] = local.stdout.strip()
            state["detail"] = (
                f"worker target repo check failed: {remote.stderr.strip()}"
            )
            return state

        status_lines: list[str] = []
        in_status = False
        for line in remote.stdout.splitlines():
            if line.startswith("BRANCH:"):
                state["branch"] = line[7:]
            elif line.startswith("HEAD:"):
                state["worker_sha"] = line[5:]
            elif line.startswith("ORIGIN_MAIN:"):
                state["origin_sha"] = line[12:]
            elif line == "MTOR_STATUS_START":
                in_status = True
            elif in_status and line:
                status_lines.append(line)

        state["local_sha"] = local.stdout.strip()
        dirty_lines = [
            line for line in status_lines if not _target_repo_allowed_noise(line)
        ]
        state["dirty"] = bool(dirty_lines)

        errors: list[str] = []
        if state["local_sha"] != state["worker_sha"]:
            errors.append("worker target HEAD differs from local target HEAD")
        if state["origin_sha"] and state["origin_sha"] != state["worker_sha"]:
            errors.append("worker target HEAD differs from origin/main")
        if not state["origin_sha"]:
            errors.append("worker target origin/main lookup returned no SHA")
        if state["dirty"]:
            errors.append(
                "worker target repo dirty/untracked files: " + "\n".join(dirty_lines)
            )

        state["ok"] = not errors
        state["detail"] = "; ".join(errors)
    except (OSError, subprocess.TimeoutExpired) as exc:
        state["ok"] = False
        state["detail"] = str(exc)

    return state


def _dedup_plan(prompt: str, spec_path: Path | None = None) -> dict:
    """Return dedup identity and block status without recording a dispatch."""
    key = compute_identity(prompt, spec_path)
    now = time.time()
    state = _prune(_load_state(DEFAULT_STATE_PATH), now, DEFAULT_WINDOW_S)
    last_seen = state.get(key)
    blocked = last_seen is not None and (now - last_seen) < DEFAULT_WINDOW_S
    return {
        "key": key,
        "blocked": blocked,
        "window_seconds": DEFAULT_WINDOW_S,
        "seconds_since_last": round(now - last_seen, 3)
        if last_seen is not None
        else None,
    }


def _search_attr_preview(
    *,
    provider: str,
    mode: str,
    risk: str,
    spec_path: Path | None,
) -> dict[str, str]:
    attrs = {
        "mtor_provider": provider,
        "mtor_mode": mode,
        "mtor_risk": risk,
    }
    if spec_path:
        attrs["mtor_spec"] = str(spec_path)
    return attrs


def _dispatch_explanation(
    prompt: str,
    *,
    provider: str | None = None,
    experiment: bool = False,
    mode: str | None = None,
    skip_sha_check: bool = False,
    chain: list[str] | None = None,
    spec_path: Path | None = None,
    harness: str = "",
    paused: bool = False,
    frozen: bool = False,
) -> dict:
    """Build a read-only dispatch plan explanation."""
    if spec_path is not None:
        prompt = _inject_spec_constraints(
            prompt,
            spec_path=spec_path,
            prompt_for_cmd=prompt[:60],
        )

    if mode:
        spec_mode = mode
    elif experiment:
        spec_mode = "experiment"
    else:
        spec_mode = "build"

    if spec_mode == "scout":
        full_prompt = prompt + (
            "\n\nThis is a READ-ONLY analysis task. Do NOT modify any files. "
            "Report your findings as structured output. Format: list each finding with: "
            "file path, issue, recommendation."
        )
    elif spec_mode == "research":
        full_prompt = prompt + (
            "\n\nThis is a RESEARCH task. Search external sources (web, docs, papers) "
            "to answer the question. Use rheotaxis, curl, or any available search tools. "
            "Do NOT modify any files in the repository. "
            "Format findings as:\n"
            "## Key Findings\n- finding 1 (source: URL)\n- finding 2 (source: URL)\n"
            "## Synthesis\nOne paragraph summary.\n"
            "## Recommendations\n- actionable item 1\n- actionable item 2"
        )
    elif spec_mode == "receptor":
        full_prompt = prompt + _receptor_suffix()
    else:
        full_prompt = prompt

    resolved_provider = provider or _resolve_default_provider(spec_mode)
    risk = classify_risk(full_prompt)
    workflow_id = _make_workflow_id(
        full_prompt, resolved_provider, harness=harness or "ribosome"
    )
    prompt_hash = hashlib.sha256(full_prompt.encode()).hexdigest()[:16]

    spec_data = {}
    validation_errors: list[str] = []
    if spec_path is not None:
        from mtor.rptor import parse_spec

        spec_data = parse_spec(spec_path)
        repo = Path(spec_data.get("repo", ".")).expanduser()
        validation_errors = validate_spec(spec_path, repo)

    dedup = _dedup_plan(prompt, spec_path=spec_path) if prompt.strip() else {}
    spec_repo = spec_data.get("repo") if spec_data else None
    worker_sha = (
        {
            "skipped": True,
            "in_sync": True,
            "auto_deploy_would_occur": False,
            "local_sha": "",
            "worker_sha": "",
            "error": "",
            "worker_checkout": {**_CHECKOUT_OK},
        }
        if spec_mode in ("scout", "research")
        else _worker_sha_plan(skip=skip_sha_check, repo=spec_repo)
    )
    target_repo = (
        {
            **_TARGET_REPO_OK,
            "skipped": True,
            "detail": f"skipped for {spec_mode} mode",
        }
        if spec_mode in ("scout", "research")
        else _worker_target_repo_state(spec_repo, skip=skip_sha_check)
    )
    blocked_reasons = []
    if frozen:
        blocked_reasons.append("frozen")
    if paused:
        blocked_reasons.append("paused")
    if validation_errors:
        blocked_reasons.append("spec_invalid")
    if dedup.get("blocked"):
        blocked_reasons.append("dedup_blocked")
    provider_issue = _provider_validation_error(provider)
    if provider_issue:
        blocked_reasons.append(provider_issue)
    if worker_sha.get("error"):
        blocked_reasons.append("worker_sha_unknown")
    if not target_repo.get("ok"):
        blocked_reasons.append("target_repo_preflight_failed")

    search_attrs = _search_attr_preview(
        provider=resolved_provider,
        mode=spec_mode,
        risk=risk,
        spec_path=spec_path,
    )

    return {
        "would_dispatch": not blocked_reasons,
        "blocked_reasons": blocked_reasons,
        "prompt_preview": full_prompt[:200],
        "prompt_hash": prompt_hash,
        "spec": {
            "path": str(spec_path) if spec_path else "",
            "repo": spec_data.get("repo", ""),
            "scope": spec_data.get("scope", []),
            "exclude": spec_data.get("exclude", []),
            "tests": spec_data.get("tests", {}),
        },
        "validation": {
            "ok": not validation_errors,
            "errors": validation_errors,
        },
        "dedup": dedup,
        "pause": {"paused": paused},
        "freeze": {"frozen": frozen},
        "worker_sha": worker_sha,
        "target_repo": target_repo,
        "risk": risk,
        "provider": {
            "selected": resolved_provider,
            "why": "explicit provider" if provider else f"default for {spec_mode}",
            "retired_reason": RETIRED_PROVIDERS.get(provider or "", ""),
        },
        "workflow_id": workflow_id,
        "search_attributes": search_attrs,
        "planned_spec_frontmatter_mutation": (
            {"status": "dispatched", "workflow_id": workflow_id} if spec_path else {}
        ),
        "chain": chain or [],
        "next_actions": [
            _action(
                f"mtor status {workflow_id}", "Poll workflow status after real dispatch"
            ),
            _action(f"mtor --spec {spec_path}", "Dispatch this spec")
            if spec_path
            else _action("mtor <prompt>", "Dispatch this prompt"),
        ],
    }


def _normalize_spec_repo_for_worker(repo: str) -> str:
    """Keep spec repo paths valid on the worker host."""
    if repo == "~" or repo.startswith("~/"):
        return repo

    path = Path(repo)
    if path.is_absolute():
        with contextlib.suppress(ValueError):
            rel = path.relative_to(Path.home())
            return f"~/{rel.as_posix()}"
        return repo

    return repo


def _dispatch_prompt(
    prompt: str,
    *,
    provider: str | None = None,
    experiment: bool = False,
    mode: str | None = None,
    skip_sha_check: bool = False,
    chain: list[str] | None = None,
    wait: bool = False,
    timeout: int = 300,
    spec_path: Path | None = None,
    harness: str = "",
    repo: str | None = None,
) -> str | None:
    """Core dispatch logic. Returns workflow_id when wait=True, else prints JSON."""
    # If prompt is a file path, read it as the spec
    prompt_path = None
    if "\n" not in prompt and len(prompt) < 512:
        prompt_path = Path(prompt).expanduser()
    if prompt_path is not None and prompt_path.is_file():
        prompt = prompt_path.read_text(encoding="utf-8").strip()
        # Strip YAML frontmatter (--- ... ---) — confuses GLM into treating spec as document
        prompt = re.sub(
            r"\A---\n.*?\n---\n*", "", prompt, count=1, flags=re.DOTALL
        ).strip()

    # Inject scope/tests/exclude constraints from spec frontmatter
    if spec_path is not None:
        prompt = _inject_spec_constraints(
            prompt,
            spec_path=spec_path,
            prompt_for_cmd=prompt[:60],
        )

    cmd = f"mtor {prompt[:60]}{'...' if len(prompt) > 60 else ''}"

    if not prompt.strip():
        sys.exit(
            _err(
                "mtor",
                "Prompt is required",
                "MISSING_PROMPT",
                'Provide a task description: mtor "Write tests for metabolon/foo.py"',
                [_action("mtor", "Show command tree")],
                exit_code=2,
            )
        )

    # Determine spec mode: explicit mode > experiment flag > build default
    if mode:
        spec_mode = mode
    elif experiment:
        spec_mode = "experiment"
    else:
        spec_mode = "build"

    if provider in RETIRED_PROVIDERS:
        sys.exit(
            _err(
                cmd,
                f"Provider '{provider}' is retired: {RETIRED_PROVIDERS[provider]}",
                "PROVIDER_RETIRED",
                "Use zhipu for coding dispatch, or gemini/codex only as explicit escape hatches.",
                [_action("mtor tsc", "Show current provider routing")],
                exit_code=2,
            )
        )

    if provider and provider not in _known_providers():
        sys.exit(
            _err(
                cmd,
                f"Provider '{provider}' is not a known provider",
                "PROVIDER_UNKNOWN",
                "Use a known provider: " + ", ".join(sorted(_known_providers())) + ".",
                [_action("mtor tsc", "Show current provider routing")],
                exit_code=2,
            )
        )

    preflight_repo = repo
    if preflight_repo is None and spec_path is not None:
        from mtor.rptor import parse_spec

        parsed_for_preflight = parse_spec(spec_path)
        parsed_repo = parsed_for_preflight.get("repo")
        if parsed_repo is not None:
            preflight_repo = str(parsed_repo)

    # SHA gate — auto-deploy if worker is out of sync
    # Scout/research are read-only — worker code version doesn't matter
    if spec_mode not in ("scout", "research"):
        _check_worker_sha(skip=skip_sha_check, repo=preflight_repo)
        target_repo = _worker_target_repo_state(preflight_repo, skip=skip_sha_check)
        if not target_repo["ok"]:
            sys.exit(
                _err(
                    cmd,
                    f"Target repo preflight failed: {target_repo['detail']}",
                    "TARGET_REPO_PREFLIGHT_FAILED",
                    "Repair or update the worker target checkout explicitly, then retry dispatch.",
                    [
                        _action(
                            "mtor --explain <prompt>",
                            "Inspect dispatch preflight state",
                        )
                    ],
                    exit_code=1,
                )
            )

    # Mode-specific prompt suffixes
    if spec_mode == "scout":
        scout_suffix = (
            "\n\nThis is a READ-ONLY analysis task. Do NOT modify any files. "
            "Report your findings as structured output. Format: list each finding with: "
            "file path, issue, recommendation."
        )
        full_prompt = prompt + scout_suffix
    elif spec_mode == "research":
        research_suffix = (
            "\n\nThis is a RESEARCH task. Search external sources (web, docs, papers) "
            "to answer the question. Use rheotaxis, curl, or any available search tools. "
            "Do NOT modify any files in the repository. "
            "Format findings as:\n"
            "## Key Findings\n- finding 1 (source: URL)\n- finding 2 (source: URL)\n"
            "## Synthesis\nOne paragraph summary.\n"
            "## Recommendations\n- actionable item 1\n- actionable item 2"
        )
        full_prompt = prompt + research_suffix
    elif spec_mode == "receptor":
        full_prompt = prompt + _receptor_suffix()
    else:
        full_prompt = prompt

    client, err = _get_client()
    if err:
        sys.exit(
            _err(
                cmd,
                f"Cannot connect to Temporal at {TEMPORAL_HOST}: {err}",
                "TEMPORAL_UNREACHABLE",
                f"Start mtor worker: ssh {WORKER_HOST} 'systemctl --user start mtor-worker'",
                [_action("mtor doctor", "Run health check to diagnose connectivity")],
                exit_code=3,
            )
        )

    try:
        import asyncio

        # Deterministic ID — Temporal rejects if already running (dedup)
        resolved_provider = provider or _resolve_default_provider(spec_mode)
        workflow_id = _make_workflow_id(
            full_prompt,
            resolved_provider,
            harness=harness or "ribosome",
        )
        spec = {
            "task": full_prompt,
            "provider": resolved_provider,
            "mode": spec_mode,
            "risk": classify_risk(full_prompt),
            "harness": harness,
        }
        if spec_mode == "experiment":
            spec["experiment"] = True
        if chain:
            spec["chain"] = chain

        # Extract repo from spec frontmatter (structured parameter for workflow)
        if spec_path is not None:
            from mtor.rptor import parse_spec

            parsed = parse_spec(spec_path)
            repo = parsed.get("repo", "~")
            if repo != "~":
                spec["repo"] = _normalize_spec_repo_for_worker(str(repo))

        from temporalio.common import (
            SearchAttributeKey,
            SearchAttributePair,
            TypedSearchAttributes,
            WorkflowIDConflictPolicy,
            WorkflowIDReusePolicy,
        )

        search_attrs = [
            SearchAttributePair(
                SearchAttributeKey.for_keyword("mtor_provider"), resolved_provider
            ),
            SearchAttributePair(SearchAttributeKey.for_keyword("mtor_mode"), spec_mode),
            SearchAttributePair(
                SearchAttributeKey.for_keyword("mtor_risk"), classify_risk(full_prompt)
            ),
        ]
        if spec_path:
            search_attrs.append(
                SearchAttributePair(
                    SearchAttributeKey.for_keyword("mtor_spec"), str(spec_path)
                )
            )

        async def _start():
            handle = await client.start_workflow(
                WORKFLOW_TYPE,
                args=[[spec]],
                id=workflow_id,
                task_queue=TASK_QUEUE,
                id_reuse_policy=WorkflowIDReusePolicy.ALLOW_DUPLICATE_FAILED_ONLY,
                id_conflict_policy=WorkflowIDConflictPolicy.USE_EXISTING,
                search_attributes=TypedSearchAttributes(search_attrs),
            )
            return handle.id

        started_id = asyncio.run(_start())

        # Update spec file frontmatter if --spec was provided
        if spec_path is not None:
            try:
                from mtor.spec import update_spec_status

                update_spec_status(spec_path, "dispatched", workflow_id=started_id)
            except Exception as exc:
                print(f"[spec] warning: {exc}", file=sys.stderr)

        result_envelope: dict = {
            "workflow_id": started_id,
            "status": "RUNNING",
            "prompt_preview": prompt[:100],
            "risk": classify_risk(full_prompt),
        }
        result_envelope["provider"] = resolved_provider
        if spec_mode == "experiment":
            result_envelope["experiment"] = True
        if spec_mode == "scout":
            result_envelope["scout"] = True
        if chain:
            result_envelope["chain"] = chain
            result_envelope["chain_length"] = len(chain)
        if spec_path is not None:
            result_envelope["spec"] = str(spec_path)

        next_actions = [
            _action(f"mtor status {started_id}", "Poll workflow status"),
            _action(f"mtor logs {started_id}", "Fetch output when complete"),
            _action(f"mtor cancel {started_id}", "Cancel if needed"),
        ]
        if spec_mode == "experiment":
            next_actions[0] = _action(
                f"mtor status {started_id}",
                "Experiment mode — will NOT auto-merge to main",
            )
        if spec_mode == "scout":
            next_actions[1] = _action(
                f"mtor logs {started_id}", "Scout mode — read-only analysis, no merge"
            )

        if wait:
            return started_id
        _ok(
            cmd,
            result_envelope,
            next_actions,
            version=VERSION,
        )
        return started_id
    except Exception as exc:
        sys.exit(
            _err(
                cmd,
                f"Failed to start workflow: {exc}",
                "DISPATCH_ERROR",
                "Check Temporal server health: mtor doctor",
                [_action("mtor doctor", "Run health check")],
            )
        )


def _strip_cd_prefix(run_cmd: str, repo: str) -> str:
    """Strip leading ``cd <repo> && `` or ``cd <repo>; `` from a test run command.

    Handles ~/, /home/vivesca/, and single/double-quoted path variants.
    Returns the command unchanged when *repo* is the default ``~``.
    """
    if repo == "~":
        return run_cmd

    tilde_form = _normalize_spec_repo_for_worker(str(repo))
    abs_form = tilde_form.replace("~", "/home/vivesca", 1)

    candidates: set[str] = set()
    for r in (tilde_form, abs_form):
        candidates.add(r)
        candidates.add(f"'{r}'")
        candidates.add(f'"{r}"')

    # Derive the tilde form from the absolute form directly, in case
    # _normalize_spec_repo_for_worker couldn't (e.g. local HOME differs).
    if abs_form.startswith("/home/vivesca/"):
        direct_tilde = "~" + abs_form[len("/home/vivesca") :]
        candidates.add(direct_tilde)
        candidates.add(f"'{direct_tilde}'")
        candidates.add(f'"{direct_tilde}"')

    for r in sorted(candidates, key=len, reverse=True):
        for sep in ("&&", ";"):
            for gap in (f" {sep} ", f"{sep} "):
                prefix = f"cd {r}{gap}"
                if run_cmd.startswith(prefix):
                    return run_cmd[len(prefix) :]

    return run_cmd


def _inject_spec_constraints(
    prompt: str,
    *,
    spec_path: Path | None = None,
    harness: str = "ribosome",
    prompt_for_cmd: str = "",
) -> str:
    """Inject scope, tests, and repo context from a spec file into the prompt.

    Returns the base prompt unchanged when *spec_path* is None.
    """
    if spec_path is None:
        return prompt

    from mtor.rptor import parse_spec

    spec = parse_spec(spec_path)
    parts = [prompt]

    # Scope constraint
    scope = spec.get("scope", [])
    if scope:
        parts.append(f"CONSTRAINT: Only modify {', '.join(scope)}.")

    # Exclude constraint
    exclude = spec.get("exclude", [])
    if exclude:
        parts.append(f"Do NOT modify: {', '.join(exclude)}.")

    # Repo context (only when non-default). Keep home-relative paths in worker
    # form so prompts do not leak the local machine's absolute HOME.
    repo_raw = str(spec.get("repo", "~"))
    repo = repo_raw
    if repo != "~":
        if not repo.startswith("/home/vivesca/"):
            repo = _normalize_spec_repo_for_worker(repo)
        parts.append(
            f"Canonical repository: {repo}. The worker runs in an "
            f"isolated git worktree. Use the current working directory "
            f"-- do not cd to {repo}."
        )

    # Test run command and function list
    tests = spec.get("tests", {})
    if tests.get("run"):
        run_cmd = _strip_cd_prefix(tests["run"], repo_raw)
        parts.append(f"Run: {run_cmd}")
    if tests.get("functions"):
        func_names = [f"test_{f}" for f in tests["functions"]]
        parts.append(f"Verify test functions: {', '.join(func_names)}")

    return "\n".join(parts)


def validate_spec(spec_path: Path, repo: Path) -> list[str]:
    """Validate a spec file for dispatch readiness.

    Checks:
    - Status is ``ready`` (not dispatched/done/superseded)
    - ``tests`` field exists in frontmatter
    - Test file paths extracted from ``tests.run`` exist in *repo*

    Returns a list of error strings — empty means valid.
    """
    from mtor.rptor import parse_spec

    errors: list[str] = []
    spec = parse_spec(spec_path)

    # Status must be "ready"
    status = spec.get("status", "ready")
    if status != "ready":
        errors.append(f"Spec status is '{status}', expected 'ready'")

    # Tests field is required for build dispatch and must be populated
    tests = spec.get("tests", {})
    if not tests:
        errors.append("Spec is missing 'tests' field")
        return errors
    if isinstance(tests, dict) and not tests.get("run") and not tests.get("functions"):
        errors.append(
            "Spec 'tests' field is not populated — provide 'run' command or 'functions' list"
        )
        return errors

    # Verify test files referenced in tests.run exist
    run_cmd = tests.get("run", "") if isinstance(tests, dict) else ""
    if run_cmd:
        test_file_matches = re.findall(r"([\w/]*test_\w+\.py)", run_cmd)
        for tf in test_file_matches:
            test_path = repo / tf
            if not test_path.exists():
                errors.append(f"Test file not found: {tf}")

    return errors


def validate_receptor_spec(spec_path: Path) -> list[str]:
    """Validate receptor-route constraints beyond ordinary spec readiness."""
    from mtor.rptor import parse_spec

    spec = parse_spec(spec_path)
    scope = spec.get("scope", [])
    if not scope:
        return ["Receptor route requires scope under membrane/receptors/"]

    receptor_scopes = [
        str(item) for item in scope if str(item).startswith("membrane/receptors/")
    ]
    if not receptor_scopes:
        return ["Receptor route scope must include membrane/receptors/<name>/..."]

    invalid = [
        str(item)
        for item in scope
        if not str(item).startswith("membrane/receptors/") and str(item) != "typos.toml"
    ]
    if invalid:
        return [
            "Receptor route scope may only include membrane/receptors/... plus typos.toml; "
            f"invalid: {', '.join(invalid)}"
        ]

    return []
