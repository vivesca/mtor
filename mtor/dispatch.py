"""Core dispatch logic — send a task prompt to Temporal."""

from __future__ import annotations

import hashlib
import re
import sys
from pathlib import Path

from porin import action as _action

from mtor import TASK_QUEUE, TEMPORAL_HOST, VERSION, WORKER_HOST, WORKFLOW_TYPE
from mtor.client import _get_client
from mtor.envelope import _err, _ok


def decompose_spec(prompt: str) -> list[str] | None:
    """Split a multi-task spec into individual task prompts.

    Returns None if the prompt is a single task (no ## Task sections).
    """
    task_pattern = re.compile(r"^## Task \d+", re.MULTILINE)
    matches = list(task_pattern.finditer(prompt))

    if len(matches) < 2:
        return None  # Single task or no task sections

    preamble_end = matches[0].start()
    preamble = prompt[:preamble_end].strip()

    tasks = []
    for idx, match in enumerate(matches):
        start = match.start()
        end = matches[idx + 1].start() if idx + 1 < len(matches) else len(prompt)
        task_text = prompt[start:end].strip()
        # Prepend preamble to each task
        if preamble:
            tasks.append(f"{preamble}\n\n{task_text}")
        else:
            tasks.append(task_text)

    return tasks


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
    "build": "zhipu",
    "test": "zhipu",
    "research": "zhipu",
}


def detect_task_type(prompt: str) -> str:
    """Classify a prompt into a task type for routing."""
    lower = prompt.lower()
    for task_type, patterns in ROUTE_PATTERNS.items():
        if any(p in lower for p in patterns):
            return task_type
    return "build"


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
    """Build a deterministic workflow ID: {harness}-{model}-{slug}-{hash}.

    - model: short name mapped from *provider*
    - slug: first 3 words of *prompt*, slugified
    - hash: 8-char hex from sha256 of *prompt*
    - total length capped at 80 characters (slug truncated if needed)
    """
    model = PROVIDER_TO_MODEL.get(provider, provider)
    prompt_hash = hashlib.sha256(prompt.encode()).hexdigest()[:8]

    words = prompt.split()
    slug = _slugify(" ".join(words[:3]))

    # Assemble and enforce 80-char limit
    wid = f"{harness}-{model}-{slug}-{prompt_hash}"
    if len(wid) > 80:
        # Truncate slug to fit: harness-model--hash + safety margin
        overhead = len(harness) + 1 + len(model) + 1 + 1 + len(prompt_hash)
        max_slug = 80 - overhead
        slug = slug[: max(0, max_slug)].rstrip("-")
        wid = f"{harness}-{model}-{slug}-{prompt_hash}"

    return wid


def _dispatch_prompt(
    prompt: str, *, provider: str | None = None, experiment: bool = False, mode: str | None = None
) -> None:
    """Core dispatch logic."""
    # If prompt is a file path, read it as the spec
    prompt_path = Path(prompt).expanduser()
    if prompt_path.is_file():
        prompt = prompt_path.read_text(encoding="utf-8").strip()
        # Strip YAML frontmatter (--- ... ---) — confuses GLM into treating spec as document
        prompt = re.sub(r"\A---\n.*?\n---\n*", "", prompt, count=1, flags=re.DOTALL).strip()

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

    # Build tasks require a CC-written test file reference in the prompt.
    # Tests are judgment (CC writes), implementation is execution (ribosome).
    if spec_mode == "build" and not re.search(r"test_\w+\.py|assays/", prompt):
        sys.exit(
            _err(
                cmd,
                "Build tasks require a test file. CC writes tests first, ribosome makes them pass.",
                "NO_TEST_FILE",
                'Write tests first: assays/test_<feature>.py, then mtor "Make ~/path/assays/test_feature.py pass."',
                [_action("mtor", "Show command tree")],
                exit_code=2,
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
        workflow_id = _make_workflow_id(full_prompt, provider or "zhipu")
        spec = {
            "task": full_prompt,
            "provider": provider,
            "mode": spec_mode,
            "risk": classify_risk(full_prompt),
        }
        if spec_mode == "experiment":
            spec["experiment"] = True

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
        result_envelope: dict = {
            "workflow_id": started_id,
            "status": "RUNNING",
            "prompt_preview": prompt[:100],
            "risk": classify_risk(full_prompt),
        }
        if spec_mode == "experiment":
            result_envelope["experiment"] = True
        if spec_mode == "scout":
            result_envelope["scout"] = True

        next_actions = [
            _action(f"mtor status {started_id}", "Poll workflow status"),
            _action(f"mtor logs {started_id}", "Fetch output when complete"),
            _action(f"mtor cancel {started_id}", "Cancel if needed"),
        ]
        if spec_mode == "experiment":
            next_actions.append(
                _action(
                    f"mtor status {started_id}", "Experiment mode — will NOT auto-merge to main"
                )
            )
        if spec_mode == "scout":
            next_actions.append(
                _action(f"mtor logs {started_id}", "Scout mode — read-only analysis, no merge")
            )

        _ok(
            cmd,
            result_envelope,
            next_actions,
            version=VERSION,
        )
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
