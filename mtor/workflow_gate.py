"""Workflow trigger assessment and ledger management.

Classifies task prompts as workflow-worthy (multi-worker, high-cost) or
single-lane (routine), and writes resumable ledger files for tracking.
"""

from __future__ import annotations

import re
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

# Trigger patterns that escalate to workflow route
_TRIGGER_PATTERNS: list[tuple[str, re.Pattern[str]]] = [
    ("bug_hunt", re.compile(r"(repo-wide|codebase-wide|bug hunt|regression sweep|cross-service regression)", re.IGNORECASE)),
    ("migration", re.compile(r"(migration|migrate|modernization|framework swap|api deprecation|language port)", re.IGNORECASE)),
    ("security_audit", re.compile(r"(security audit|hardening|auth check|input validation|unsafe pattern)", re.IGNORECASE)),
    ("adversarial_review", re.compile(r"(adversarial review|stress-test|refute|break the result|checked twice)", re.IGNORECASE)),
    ("high_cost_decision", re.compile(r"(high-cost|critical work|before merge|before publish|board|production)", re.IGNORECASE)),
]

# Required controls when route is 'workflow'
REQUIRED_CONTROLS: list[str] = [
    "resumable_ledger",
    "independent_verification",
    "explicit_cost_visibility",
    "human_or_admin_enablement",
]

DEFAULT_LEDGER_DIR = Path.home() / ".local" / "share" / "mtor" / "workflows"


def assess_workflow_trigger(prompt: str) -> dict[str, Any]:
    """Assess whether a task prompt warrants a workflow or single-lane route.

    Returns a dict with:
      - prompt: the original prompt
      - route: "workflow" or "single"
      - triggers: list of matched trigger names (empty for single)
      - required_controls: list of control names (empty for single)
    """
    triggers = [name for name, pattern in _TRIGGER_PATTERNS if pattern.search(prompt)]

    if triggers:
        return {
            "prompt": prompt,
            "route": "workflow",
            "triggers": triggers,
            "required_controls": list(REQUIRED_CONTROLS),
        }

    return {
        "prompt": prompt,
        "route": "single",
        "triggers": [],
        "required_controls": [],
    }


def _ledger_filename(prompt: str) -> str:
    """Generate a deterministic, readable ledger filename from the prompt."""
    timestamp = datetime.now(UTC).strftime("%Y%m%d-%H%M%S")
    slug = re.sub(r"[^a-z0-9]+", "-", prompt.lower().strip())[:40].strip("-")
    return f"{timestamp}-{slug}.md"


def _build_ledger_content(
    prompt: str,
    assessment: dict[str, Any],
) -> str:
    """Build the Markdown content for a workflow ledger."""
    now = datetime.now(UTC).isoformat()
    triggers = assessment.get("triggers", [])
    controls = assessment.get("required_controls", [])

    lines = [
        "# Workflow Ledger",
        "",
        f"- **created**: {now}",
        "- **status**: open",
        f"- **route**: {assessment['route']}",
        "",
        "## Task",
        "",
        prompt,
        "",
        "## Trigger Assessment",
        "",
        f"- **route**: {assessment['route']}",
        f"- **triggers**: {', '.join(triggers) if triggers else 'none'}",
    ]

    if controls:
        lines.append("- **required_controls**:")
        for ctrl in controls:
            lines.append(f"  - {ctrl}")

    lines.extend([
        "",
        "## Subtasks",
        "",
        "<!-- Add subtasks here -->",
        "",
        "## Evidence",
        "",
        "<!-- Link evidence, logs, outputs -->",
        "",
        "## Verification",
        "",
        "<!-- How was correctness verified? -->",
        "",
        "## Decisions",
        "",
        "<!-- Key decisions made during execution -->",
        "",
        "## Open Loops",
        "",
        "<!-- Unresolved items -->",
    ])

    return "\n".join(lines) + "\n"


def write_workflow_ledger(
    prompt: str,
    ledger_dir: Path,
    *,
    dry_run: bool = False,
) -> dict[str, Any]:
    """Write a workflow ledger file to disk.

    Args:
        prompt: The task prompt.
        ledger_dir: Directory to write the ledger into.
        dry_run: If True, report the path without writing.

    Returns:
        Dict with path, route, triggers, and controls.

    Raises:
        FileExistsError: If the ledger file already exists.
    """
    assessment = assess_workflow_trigger(prompt)
    ledger_dir = Path(ledger_dir)
    filename = _ledger_filename(prompt)
    ledger_path = ledger_dir / filename

    if not dry_run and ledger_path.exists():
        raise FileExistsError(f"Ledger already exists: {ledger_path}")

    content = _build_ledger_content(prompt, assessment)

    result = {
        "path": str(ledger_path),
        "route": assessment["route"],
        "triggers": assessment["triggers"],
        "required_controls": assessment["required_controls"],
        "dry_run": dry_run,
    }

    if dry_run:
        result["message"] = f"Would write ledger to {ledger_path}"
        return result

    ledger_dir.mkdir(parents=True, exist_ok=True)
    ledger_path.write_text(content, encoding="utf-8")

    return result
