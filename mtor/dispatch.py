"""Core dispatch logic — send a task prompt to Temporal."""

from __future__ import annotations

import hashlib
import re
import subprocess
import sys
import time
from pathlib import Path

from porin import action as _action

from mtor import TASK_QUEUE, TEMPORAL_HOST, VERSION, WORKER_HOST, WORKFLOW_TYPE
from mtor.client import _get_client
from mtor.envelope import _err, _ok


# ---------------------------------------------------------------------------
# Task risk classification
# ---------------------------------------------------------------------------

RISK_PATTERNS: dict[str, list[str]] = {
    "high": ["delete", "remove", "drop", "config", "infra", "deploy", "migrate", "rename"],
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
}

ROUTE_TO_PROVIDER: dict[str, str] = {
    "explore": "droid",
    "bugfix": "goose",
    "build": "volcano",
    "test": "zhipu",
    "research": "zhipu",
}


def _resolve_default_provider(spec_mode: str) -> str:
    """Return the default provider for a spec mode."""
    return ROUTE_TO_PROVIDER.get(spec_mode, "zhipu")


# ---------------------------------------------------------------------------
# Workflow ID generation
# ---------------------------------------------------------------------------

PROVIDER_TO_MODEL: dict[str, str] = {
    "zhipu": "glm51",
    "infini": "mm27",
    "volcano": "doubao",
    "gemini": "gem31",
    "codex": "gpt54",
    "goose": "glm51g",
    "droid": "glm51d",
}

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
        overhead = len(harness) + 1 + len(model) + 1 + 1 + len(prompt_hash) + 1 + len(ts)
        max_slug = 80 - overhead
        slug = slug[: max(0, max_slug)].rstrip("-")
        wid = f"{harness}-{model}-{slug}-{prompt_hash}-{ts}"

    return wid


def _check_worker_sha(*, skip: bool = False) -> bool:
    """Compare local HEAD with worker HEAD. Returns True if in sync.

    Raises RuntimeError on failures.  If out of sync and skip=False,
    auto-deploys (push + merge + restart) before returning True.
    """
    if skip:
        return True

    local = subprocess.run(
        ["git", "rev-parse", "HEAD"],
        capture_output=True, text=True, timeout=5,
    )
    if local.returncode != 0:
        raise RuntimeError(f"local git HEAD lookup failed: {local.stderr.strip()}")

    remote = subprocess.run(
        ["ssh", WORKER_HOST, "cd ~/germline && git rev-parse HEAD"],
        capture_output=True, text=True, timeout=10,
    )
    if remote.returncode != 0:
        raise RuntimeError(f"worker git HEAD lookup failed: {remote.stderr.strip()}")

    if local.stdout.strip() == remote.stdout.strip():
        return True

    # Auto-deploy: push + merge + restart.
    # Use -C to pin git context to ~/germline regardless of caller cwd —
    # otherwise dispatching from a non-`main` repo (e.g. quorate on master)
    # fails with bogus "src refspec main does not match any".
    germline_dir = str(Path.home() / "germline")
    push = subprocess.run(
        ["git", "-C", germline_dir, "push",
         WORKER_HOST + ":~/germline", "main:deploy-sync", "--force"],
        capture_output=True, text=True, timeout=120,
    )
    if push.returncode != 0:
        raise RuntimeError(f"push failed: {push.stderr.strip()}")

    subprocess.run(
        ["ssh", WORKER_HOST,
         "cd ~/germline && git merge deploy-sync --no-edit"],
        capture_output=True, text=True, timeout=30,
    )

    restart = subprocess.run(
        ["ssh", WORKER_HOST, "sudo systemctl restart temporal-worker"],
        capture_output=True, text=True, timeout=15,
    )
    if restart.returncode != 0:
        raise RuntimeError(f"restart failed: {restart.stderr.strip()}")

    time.sleep(3)
    return True


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
    harness: str = "ribosome",
) -> str | None:
    """Core dispatch logic. Returns workflow_id when wait=True, else prints JSON."""
    # If prompt is a file path, read it as the spec
    prompt_path = Path(prompt).expanduser()
    if prompt_path.is_file():
        prompt = prompt_path.read_text(encoding="utf-8").strip()
        # Strip YAML frontmatter (--- ... ---) — confuses GLM into treating spec as document
        prompt = re.sub(r"\A---\n.*?\n---\n*", "", prompt, count=1, flags=re.DOTALL).strip()

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

    # SHA gate — auto-deploy if worker is out of sync
    # Scout/research are read-only — worker code version doesn't matter
    if spec_mode not in ("scout", "research"):
        _check_worker_sha(skip=skip_sha_check)

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
    else:
        full_prompt = prompt

    client, err = _get_client()
    if err:
        sys.exit(
            _err(
                cmd,
                f"Cannot connect to Temporal at {TEMPORAL_HOST}: {err}",
                "TEMPORAL_UNREACHABLE",
                f"Start Temporal worker: ssh {WORKER_HOST} 'sudo systemctl start temporal-worker'",
                [_action("mtor doctor", "Run health check to diagnose connectivity")],
                exit_code=3,
            )
        )

    try:
        import asyncio

        # Deterministic ID — Temporal rejects if already running (dedup)
        resolved_provider = provider or _resolve_default_provider(spec_mode)
        workflow_id = _make_workflow_id(full_prompt, resolved_provider, harness=harness)
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
                # Normalize repo to an absolute path.  Bare names like
                # "terryli-hm" or "germline" must be resolved — expanduser()
                # alone only handles `~` prefixes, leaving bare names as
                # relative paths that crash translocase with FileNotFoundError
                # when used as subprocess cwd.
                resolved = Path(repo).expanduser()
                if not resolved.is_absolute():
                    # Try ~/code/<name> first (most repos), then ~/<name>
                    for candidate in [Path.home() / "code" / repo, Path.home() / repo]:
                        if candidate.is_dir():
                            resolved = candidate
                            break
                    else:
                        # Last resort: resolve relative to cwd
                        resolved = Path(repo).resolve()
                spec["repo"] = str(resolved)

        async def _start():
            from temporalio.common import WorkflowIDConflictPolicy, WorkflowIDReusePolicy
            handle = await client.start_workflow(
                WORKFLOW_TYPE,
                args=[[spec]],
                id=workflow_id,
                task_queue=TASK_QUEUE,
                id_reuse_policy=WorkflowIDReusePolicy.ALLOW_DUPLICATE_FAILED_ONLY,
                id_conflict_policy=WorkflowIDConflictPolicy.USE_EXISTING,
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
                f"mtor status {started_id}", "Experiment mode — will NOT auto-merge to main"
            )
        if spec_mode == "scout":
            next_actions[1] = _action(f"mtor logs {started_id}", "Scout mode — read-only analysis, no merge")

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

    # Repo context (only when non-default). Expand `~` defensively — the
    # spec-ingest path above already expands, but prompts can reach this
    # function via other callers.
    repo = spec.get("repo", "~")
    if repo != "~":
        repo = str(Path(repo).expanduser())
        parts.append(f"Working directory: {repo}")

    # Test run command and function list
    tests = spec.get("tests", {})
    if tests.get("run"):
        parts.append(f"Run: {tests['run']}")
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
        errors.append("Spec 'tests' field is not populated — provide 'run' command or 'functions' list")
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
