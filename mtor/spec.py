"""Spec file frontmatter updater — auto-update plan status on dispatch."""

from __future__ import annotations

import re
import sys
from datetime import UTC, datetime
from pathlib import Path
from typing import Any


CANONICAL_FIELDS = {
    "status",
    "scope",
    "repo",
    "priority",
    "depends_on",
    "dispatched_at",
    "workflow_id",
    "completed_at",
    "verdict",
    "tests",
    "exclude",
}

VALID_STATUSES = {"ready", "dispatched", "done", "blocked", "abandoned"}
REQUIRED_FIELDS = {"status", "scope"}
DEFAULT_SPEC_DIR = Path("~/epigenome/chromatin/loci/plans/")


def _frontmatter_keys(text: str) -> set[str]:
    fm_match = re.match(r"^---\n(.*?)\n---\n?", text, re.DOTALL)
    if not fm_match:
        return set()

    keys: set[str] = set()
    for raw_line in fm_match.group(1).splitlines():
        if raw_line[:1].isspace():
            continue
        match = re.match(r"^(\w+):", raw_line.strip())
        if match:
            keys.add(match.group(1))
    return keys


def _normalize_list(value: Any) -> list[str]:
    if isinstance(value, list):
        return [str(item) for item in value if item != "[]"]
    if value is None or value == "" or value == "[]":
        return []
    return [str(value)]


def _validate_one_spec(spec: dict[str, Any], spec_names: set[str]) -> list[str]:
    path = Path(spec["path"])
    text = path.read_text(encoding="utf-8")
    keys = _frontmatter_keys(text)
    errors: list[str] = []

    if not keys:
        return [f"{path}: missing YAML frontmatter"]

    for key in sorted(keys - CANONICAL_FIELDS):
        errors.append(f"{path}: unknown frontmatter field '{key}'")

    for field in sorted(REQUIRED_FIELDS - keys):
        errors.append(f"{path}: missing required frontmatter field '{field}'")

    if "status" in keys:
        status = str(spec.get("status", ""))
        if status not in VALID_STATUSES:
            allowed = ", ".join(sorted(VALID_STATUSES))
            errors.append(f"{path}: invalid status '{status}' (expected one of: {allowed})")

    for dep in _normalize_list(spec.get("depends_on", [])):
        if dep not in spec_names:
            errors.append(f"{path}: depends_on target '{dep}' does not exist")

    return errors


def validate_spec(path: Path | None = None) -> list[str]:
    """Validate one spec file or every spec in the default plan directory."""
    from mtor.rptor import CycleDetected, parse_spec, resolve_dag, scan_specs

    errors: list[str] = []
    target = path.expanduser() if path is not None else None
    directory = target.parent if target is not None else DEFAULT_SPEC_DIR.expanduser()

    if target is not None and not target.is_file():
        return [f"{target}: spec file not found"]
    if target is None and not directory.is_dir():
        return [f"{directory}: spec directory not found"]

    specs = scan_specs(directory)
    if target is not None:
        resolved_target = target.resolve()
        if not any(Path(spec["path"]).resolve() == resolved_target for spec in specs):
            specs.append(parse_spec(target))

    spec_names = {spec["name"] for spec in specs}
    selected = [
        spec
        for spec in specs
        if target is None or Path(spec["path"]).resolve() == target.resolve()
    ]

    for spec in selected:
        errors.extend(_validate_one_spec(spec, spec_names))

    try:
        resolve_dag(specs)
    except CycleDetected as exc:
        selected_names = {Path(spec["path"]).stem for spec in selected}
        if target is None or selected_names.intersection(exc.cycle):
            errors.append(f"{directory}: {exc}")

    return errors


def update_spec_status(
    spec_path: Path,
    status: str,
    workflow_id: str | None = None,
    verdict: str | None = None,
) -> None:
    """Parse YAML frontmatter via regex, update status and metadata fields.

    - Split on ``---`` delimiters (first two occurrences)
    - Replace ``status:`` line value
    - Append or replace ``workflow_id``, ``dispatched_at``, ``completed_at``, ``verdict``
    - Expand ``~`` via ``Path.expanduser()``
    - If file missing or no frontmatter: warn to stderr, don't raise
    - Preserve markdown body below frontmatter unchanged
    """
    resolved = spec_path.expanduser()

    if not resolved.is_file():
        print(
            f"[spec] file not found: {resolved} — skipping frontmatter update",
            file=sys.stderr,
        )
        return

    text = resolved.read_text(encoding="utf-8")

    # Match opening --- on its own line, then content, then closing --- on its own line
    fm_match = re.match(r"^---\n(.*?)\n---\n?(.*)", text, re.DOTALL)
    if not fm_match:
        print(
            f"[spec] no YAML frontmatter found in {resolved} — skipping update",
            file=sys.stderr,
        )
        return

    frontmatter = fm_match.group(1)
    body = fm_match.group(2)

    # Update status
    frontmatter = re.sub(
        r"^status:\s*.*$",
        f"status: {status}",
        frontmatter,
        flags=re.MULTILINE,
    )

    # Helper: replace or append a field
    def _set_field(fm: str, field: str, value: str) -> str:
        pattern = rf"^{re.escape(field)}:\s*.*$"
        new_line = f"{field}: {value}"
        if re.search(pattern, fm, re.MULTILINE):
            return re.sub(pattern, new_line, fm, flags=re.MULTILINE)
        return fm + "\n" + new_line

    # Set dispatched_at
    if status == "dispatched":
        now_iso = datetime.now(UTC).isoformat()
        frontmatter = _set_field(frontmatter, "dispatched_at", now_iso)

    # Set completed_at
    if status in ("completed", "done", "approved", "rejected"):
        now_iso = datetime.now(UTC).isoformat()
        frontmatter = _set_field(frontmatter, "completed_at", now_iso)

    # Set workflow_id
    if workflow_id is not None:
        frontmatter = _set_field(frontmatter, "workflow_id", workflow_id)

    # Set verdict
    if verdict is not None:
        frontmatter = _set_field(frontmatter, "verdict", verdict)

    # Reassemble: preserve body exactly (including leading newline if present)
    new_text = f"---\n{frontmatter}\n---\n{body}"
    resolved.write_text(new_text, encoding="utf-8")


# ---------------------------------------------------------------------------
# Scaffold
# ---------------------------------------------------------------------------


def scaffold_spec(
    name: str,
    path: Path,
    repo: str = "~",
    scope: list[str] | None = None,
    exclude: list[str] | None = None,
    template: str = "legacy",
) -> Path:
    """Scaffold a new spec file with YAML frontmatter.

    - Build frontmatter as plain string (no yaml library)
    - Fields: status, repo, scope (YAML list), exclude (YAML list)
    - genome.md and uv.lock are always in exclude (merged with user values)
    - Title is kebab-case → Title Case (mtor-archive-command → Mtor Archive Command)
    - Template body with spec sections
    - Raises FileExistsError if path already exists
    - Returns absolute path
    """
    if path.exists():
        raise FileExistsError(f"Spec file already exists: {path}")

    resolved = path.resolve()

    # Title: kebab-case → Title Case
    title = " ".join(word.capitalize() for word in name.split("-"))

    if template == "plan":
        scope_value = scope[0] if scope else "mtor"
        body_title = name
        text = f"""---
status: done
scope: {scope_value}
repo: {repo}
priority: medium
depends_on: []
---

# {body_title}

## Problem

## Fix

## Test

## Files to edit
"""
        resolved.parent.mkdir(parents=True, exist_ok=True)
        resolved.write_text(text, encoding="utf-8")
        return resolved

    # Build frontmatter as plain string
    lines = [
        "---",
        f"title: {title}",
        "status: ready",
        f"repo: {repo}",
    ]
    if scope:
        lines.append("scope:")
        for item in scope:
            lines.append(f"  - {item}")

    # Always include exclude: label + defaults (merge user values)
    default_excludes = ["genome.md", "uv.lock"]
    user_excludes = list(exclude) if exclude else []
    all_excludes = user_excludes + [d for d in default_excludes if d not in user_excludes]
    lines.append("exclude:")
    for item in all_excludes:
        lines.append(f"  - {item}")

    lines.append("---")

    # Template body with HTML comment placeholders
    body = """\
## Problem

<!-- What specific problem does this solve? Who is affected? -->

## Implementation

<!-- How should this be built? What files change, what is the algorithm? -->

## Tests

<!-- What test cases prove this works? What edge cases must be covered? -->

## Non-goals

<!-- What is explicitly out of scope? What will NOT be addressed here? -->
"""

    resolved.parent.mkdir(parents=True, exist_ok=True)
    resolved.write_text("\n".join(lines) + "\n" + body, encoding="utf-8")
    return resolved
