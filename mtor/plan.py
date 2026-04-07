"""Plan command — spec scanning, DAG resolution, and DAG display."""

from __future__ import annotations

import re
from pathlib import Path
from typing import Any


# ---------------------------------------------------------------------------
# Frontmatter parsing (no yaml library — regex only)
# ---------------------------------------------------------------------------


def _parse_frontmatter(text: str) -> dict[str, Any]:
    """Parse YAML-like frontmatter with regex.

    State machine:
      - Tracks current indentation level (from raw line, not stripped)
      - Tracks active_list_key for accumulating list items
      - Handles: key: value | bare key | list items | nested dict children
    """
    result: dict[str, Any] = {}
    fm_match = re.match(r"^---\n(.*?)\n---\n?(.*)", text, re.DOTALL)
    if not fm_match:
        return result

    frontmatter = fm_match.group(1)
    lines = frontmatter.splitlines()

    i = 0
    active_list_key: str | None = None
    current_indent = 0

    while i < len(lines):
        raw_line = lines[i]
        stripped = raw_line.strip()

        # Empty line
        if not stripped:
            i += 1
            continue

        # Calculate raw indent (before stripping)
        raw_indent = len(raw_line) - len(raw_line.lstrip())

        # Key: value or bare key (not starting with dash)
        kv_match = re.match(r"^(\w+):\s*(.*)$", stripped)
        if kv_match:
            key = kv_match.group(1)
            val = kv_match.group(2).strip()

            # List items are always siblings of their parent, not children
            # If next line is a list item, collect items under this key
            if i + 1 < len(lines):
                next_line = lines[i + 1]
                next_stripped = next_line.strip()
                next_indent = len(next_line) - len(next_line.lstrip())

                # Next line is a list item (starts with dash, same or deeper indent)
                if next_stripped.startswith("-") and next_indent > raw_indent:
                    result[key] = ""
                    active_list_key = key
                    current_indent = raw_indent
                    i += 1
                    continue

                # Next line is a child key-value (indented, has colon)
                if re.match(r"^\w+:", next_stripped) and next_indent > raw_indent:
                    nested: dict[str, Any] = {}
                    result[key] = nested
                    i += 1
                    while i < len(lines):
                        child_raw = lines[i]
                        child_stripped = child_raw.strip()
                        child_indent = len(child_raw) - len(child_raw.lstrip())
                        if child_indent <= raw_indent:
                            break
                        child_match = re.match(r"^(\w+):\s*(.*)$", child_stripped)
                        if child_match:
                            c_key = child_match.group(1)
                            c_val = child_match.group(2).strip()
                            if c_val:
                                nested[c_key] = c_val
                            else:
                                # Check for list items
                                if i + 1 < len(lines):
                                    nxt = lines[i + 1]
                                    nxt_stripped = nxt.strip()
                                    nxt_indent = len(nxt) - len(nxt.lstrip())
                                    if nxt_stripped.startswith("-") and nxt_indent > child_indent:
                                        lst: list[str] = []
                                        nested[c_key] = lst
                                        i += 1
                                        while i < len(lines):
                                            item_raw = lines[i].strip()
                                            item_indent = len(lines[i]) - len(lines[i].lstrip())
                                            if item_indent <= child_indent:
                                                break
                                            if item_raw.startswith("- "):
                                                lst.append(item_raw[2:].strip())
                                            elif item_raw.startswith("-"):
                                                lst.append(item_raw[1:].strip())
                                            else:
                                                break
                                            i += 1
                                        continue
                                nested[c_key] = ""
                        i += 1
                    continue

            # Simple key: value (no next line, or next is not a child)
            result[key] = val
            active_list_key = None
            i += 1
            continue

        # List item (starts with dash)
        if stripped.startswith("- ") or stripped.startswith("-"):
            if stripped.startswith("- "):
                item_val = stripped[2:].strip()
            else:
                item_val = stripped[1:].strip()

            if active_list_key:
                existing = result.get(active_list_key)
                if isinstance(existing, list):
                    existing.append(item_val)
                else:
                    result[active_list_key] = [item_val]
            i += 1
            continue

        # Any other line (e.g. closing ---)
        i += 1

    return result


def parse_spec(path: Path) -> dict[str, Any]:
    """Read a .md spec file and extract YAML frontmatter fields.

    Returns dict with:
      name       — filename without .md extension
      status     — from frontmatter (default: "ready")
      priority   — from frontmatter (default: "medium")
      repo       — from frontmatter (default: "~")
      depends_on — list of spec names this one depends on (default: [])
      scope      — list of file/dir paths (default: [])
      exclude    — list of excluded paths (default: [])
      tests      — dict with "run" (str) and "functions" (list), empty if absent
      path       — absolute Path to the spec file
      body       — markdown body text (everything after second ---)

    Raises FileNotFoundError if path does not exist.
    """
    resolved = path.expanduser()
    if not resolved.is_file():
        raise FileNotFoundError(f"Spec file not found: {resolved}")

    text = resolved.read_text(encoding="utf-8")
    fm = _parse_frontmatter(text)

    name = resolved.stem  # filename without extension

    # Extract tests dict
    tests_raw = fm.get("tests", {})
    if isinstance(tests_raw, dict):
        tests = {
            "run": tests_raw.get("run", ""),
            "functions": tests_raw.get("functions", []),
        }
        # Only include functions if non-empty
        if not tests["functions"]:
            tests = {"run": tests_raw.get("run", "")}
            if not tests["run"]:
                tests = {}
    else:
        tests = {}

    # Extract body
    body_match = re.search(r"^---\n.*?\n---\n(.*)", text, re.DOTALL)
    body = body_match.group(1).strip() if body_match else ""

    return {
        "name": name,
        "status": fm.get("status", "ready"),
        "priority": fm.get("priority", "medium"),
        "repo": fm.get("repo", "~"),
        "depends_on": fm.get("depends_on", []),
        "scope": fm.get("scope", []),
        "exclude": fm.get("exclude", []),
        "tests": tests,
        "path": str(resolved),
        "body": body,
    }


def scan_specs(directory: Path) -> list[dict[str, Any]]:
    """Scan directory for *.md spec files and parse each one.

    Returns list of parsed spec dicts (may be empty).
    Silently skips files that cannot be parsed.
    """
    resolved = directory.expanduser()
    if not resolved.is_dir():
        return []

    specs: list[dict[str, Any]] = []
    for md_file in sorted(resolved.glob("*.md")):
        try:
            specs.append(parse_spec(md_file))
        except Exception:
            # Skip unreadable files
            pass
    return specs


# ---------------------------------------------------------------------------
# DAG resolution
# ---------------------------------------------------------------------------


class CycleDetected(ValueError):
    """Raised when a circular dependency is detected among specs."""

    def __init__(self, cycle: list[str]) -> None:
        self.cycle = cycle
        super().__init__(f"Circular dependency: {' -> '.join(cycle)}")


def _find_cycle(specs: list[dict[str, Any]]) -> list[str] | None:
    """DFS-based cycle detection. Returns cycle list or None."""
    name_to_spec = {s["name"]: s for s in specs}
    # `visiting` tracks the current DFS path; `visited` tracks fully-explored nodes
    visiting: set[str] = set()
    visited: set[str] = set()
    path: list[str] = []

    def dfs(name: str) -> list[str] | None:
        if name in visiting:
            # Cycle: name is already on the current DFS path
            idx = path.index(name)
            return path[idx:] + [name]
        if name in visited:
            return None
        visiting.add(name)
        path.append(name)
        try:
            spec = name_to_spec.get(name)
            if spec:
                for dep in spec.get("depends_on", []):
                    result = dfs(dep)
                    if result:
                        return result
        finally:
            path.pop()
            visiting.discard(name)
            visited.add(name)
        return None

    for spec in specs:
        if spec["name"] not in visited:
            cycle = dfs(spec["name"])
            if cycle:
                return cycle
    return None


def resolve_dag(specs: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Resolve DAG dispatchability for specs.

    For each spec:
      - If status is "done": dispatchable = False (already complete)
      - If status is "dispatched": dispatchable = False (in flight)
      - If status is "superseded": dispatchable = False
      - Else: dispatchable = True only when ALL depends_on have status "done"
                AND the spec's own status is not "done"

    Raises CycleDetected if a circular dependency exists among specs.

    Returns updated specs list with 'dispatchable' key added.
    """
    cycle = _find_cycle(specs)
    if cycle:
        raise CycleDetected(cycle)

    done_names = {s["name"] for s in specs if s["status"] == "done"}

    resolved: list[dict[str, Any]] = []
    for spec in specs:
        status = spec.get("status", "ready")
        if status in ("done", "dispatched", "superseded"):
            dispatchable = False
        else:
            deps = spec.get("depends_on", [])
            dispatchable = all(dep in done_names for dep in deps)
        resolved.append({**spec, "dispatchable": dispatchable})
    return resolved


# ---------------------------------------------------------------------------
# DAG display
# ---------------------------------------------------------------------------


def display_dag(specs: list[dict[str, Any]]) -> dict[str, Any]:
    """Build porin-compatible envelope for DAG display.

    Sorts specs into status buckets:
      done       — status == "done"
      dispatched — status == "dispatched"
      ready      — dispatchable == True (and not done/dispatched)
      blocked    — dispatchable == False (and not done/dispatched/superseded)
      superseded — status == "superseded"
      other      — anything else

    Returns dict with:
      specs (per-bucket lists)
      counts (per-bucket integer)
    """
    buckets: dict[str, list[dict[str, Any]]] = {
        "done": [],
        "dispatched": [],
        "ready": [],
        "blocked": [],
        "superseded": [],
        "other": [],
    }

    for spec in specs:
        status = spec.get("status", "ready")
        dispatchable = spec.get("dispatchable", False)

        if status == "done":
            buckets["done"].append(spec)
        elif status == "dispatched":
            buckets["dispatched"].append(spec)
        elif status == "superseded":
            buckets["superseded"].append(spec)
        elif dispatchable:
            buckets["ready"].append(spec)
        else:
            buckets["blocked"].append(spec)

    counts = {k: len(v) for k, v in buckets.items()}
    return {
        "specs": buckets,
        "counts": counts,
    }
