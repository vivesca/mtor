"""Spec file frontmatter updater — auto-update plan status on dispatch."""

from __future__ import annotations

import re
import sys
from datetime import UTC, datetime
from pathlib import Path


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
) -> Path:
    """Scaffold a new spec file with YAML frontmatter.

    - Build frontmatter as plain string (no yaml library)
    - Fields: status, repo, scope (YAML list), exclude (YAML list)
    - genome.md and uv.lock are always in exclude (merged with user values)
    - Title is kebab-case → Title Case (mtor-archive-command → Mtor Archive Command)
    - Template body with ## Problem, ## Implementation, ## Tests, ## Non-goals
    - Raises FileExistsError if path already exists
    - Returns absolute path
    """
    if path.exists():
        raise FileExistsError(f"Spec file already exists: {path}")

    resolved = path.resolve()

    # Title: kebab-case → Title Case
    title = " ".join(word.capitalize() for word in name.split("-"))

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
