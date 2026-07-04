"""Plan command — spec scanning, DAG resolution, and DAG display."""

from __future__ import annotations

import re
from collections import Counter
from pathlib import Path
from typing import Any

CANONICAL_STATUSES = {"ready", "dispatched", "done", "stale", "failed", "superseded"}
DONE_EVIDENCE_FIELDS = (
    "completed_at",
    "completed_commit",
    "completed_note",
    "completed_by",
    "audit_status",
)


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


def _as_list(value: Any) -> list[Any]:
    """Normalize a frontmatter value to a list.

    The regex-based YAML parser returns a bare string when a list field is
    written inline (e.g. ``scope: mtor`` instead of ``scope:\\n  - mtor``).
    Without this wrapper, downstream ``', '.join(value)`` would iterate over
    individual characters, turning ``"mtor"`` into ``"m, t, o, r"``.
    """
    if isinstance(value, list):
        return value
    if value is None or value == "":
        return []
    return [value]


def parse_spec(path: Path) -> dict[str, Any]:
    """Read a .md spec file and extract YAML frontmatter fields.

    Returns dict with:
      name       — filename without .md extension
      status     — from frontmatter (default: "ready")
      priority   — from frontmatter (default: "medium")
      repo       — from frontmatter (default: "~")
      workflow_id — from frontmatter (default: "")
      verdict    — from frontmatter (default: "")
      completed_at — from frontmatter (default: "")
      completed_commit — from frontmatter (default: "")
      completed_note — from frontmatter (default: "")
      completed_by — from frontmatter (default: "")
      audit_status — from frontmatter (default: "")
      audit_reason — from frontmatter (default: "")
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

    # Normalize list fields: frontmatter parser may return a bare string
    # (e.g. ``scope: mtor``) which must be wrapped so downstream consumers
    # never accidentally iterate over individual characters.
    scope = _as_list(fm.get("scope", []))
    exclude = _as_list(fm.get("exclude", []))
    depends_on = _as_list(fm.get("depends_on", []))

    # Extract tests dict
    tests_raw = fm.get("tests", {})
    if isinstance(tests_raw, dict):
        tests = {
            "run": tests_raw.get("run", ""),
            "functions": _as_list(tests_raw.get("functions", [])),
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
        "workflow_id": fm.get("workflow_id", ""),
        "verdict": fm.get("verdict", ""),
        "completed_at": fm.get("completed_at", ""),
        "completed_commit": fm.get("completed_commit", ""),
        "completed_note": fm.get("completed_note", ""),
        "completed_by": fm.get("completed_by", ""),
        "audit_status": fm.get("audit_status", ""),
        "audit_reason": fm.get("audit_reason", ""),
        "depends_on": depends_on,
        "scope": scope,
        "exclude": exclude,
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
# Spec audit
# ---------------------------------------------------------------------------


def _has_done_evidence(spec: dict[str, Any]) -> bool:
    return any(str(spec.get(field, "")).strip() for field in DONE_EVIDENCE_FIELDS)


def _issue(
    spec: dict[str, Any],
    *,
    code: str,
    severity: str,
    message: str,
) -> dict[str, str]:
    return {
        "code": code,
        "severity": severity,
        "name": str(spec.get("name", "")),
        "status": str(spec.get("status", "")),
        "path": str(spec.get("path", "")),
        "message": message,
    }


def audit_specs(specs: list[dict[str, Any]]) -> dict[str, Any]:
    """Audit plan specs for lifecycle drift and weak completion provenance."""
    issues: list[dict[str, str]] = []
    status_counts = Counter(str(spec.get("status", "ready")) for spec in specs)

    for spec in specs:
        status = str(spec.get("status", "ready"))

        if status not in CANONICAL_STATUSES:
            allowed = ", ".join(sorted(CANONICAL_STATUSES))
            issues.append(
                _issue(
                    spec,
                    code="invalid_status",
                    severity="error",
                    message=f"status must be one of: {allowed}",
                )
            )

        if status == "done" and not _has_done_evidence(spec):
            fields = ", ".join(DONE_EVIDENCE_FIELDS)
            issues.append(
                _issue(
                    spec,
                    code="done_without_evidence",
                    severity="error",
                    message=f"done specs need completion evidence ({fields})",
                )
            )

        if status in {"stale", "failed", "superseded"} and not str(
            spec.get("audit_reason", "")
        ).strip():
            issues.append(
                _issue(
                    spec,
                    code=f"{status}_without_reason",
                    severity="warning",
                    message=f"{status} specs should record audit_reason",
                )
            )

        if status == "ready" and not spec.get("tests"):
            issues.append(
                _issue(
                    spec,
                    code="ready_without_tests",
                    severity="warning",
                    message="ready specs should include executable tests",
                )
            )

    issue_counts = Counter(issue["code"] for issue in issues)
    severity_counts = Counter(issue["severity"] for issue in issues)
    return {
        "ok": not any(issue["severity"] == "error" for issue in issues),
        "counts": {
            "specs": len(specs),
            "statuses": dict(sorted(status_counts.items())),
            "issues": dict(sorted(issue_counts.items())),
            "severities": dict(sorted(severity_counts.items())),
        },
        "issues": sorted(issues, key=lambda issue: (issue["severity"], issue["code"], issue["name"])),
    }


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
      - If status is "ready": dispatchable = True only when ALL depends_on have status "done"
      - Else: dispatchable = False

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
        if status == "ready":
            deps = spec.get("depends_on", [])
            dispatchable = all(dep in done_names for dep in deps)
        else:
            dispatchable = False
        resolved.append({**spec, "dispatchable": dispatchable})
    return resolved


# ---------------------------------------------------------------------------
# Topological sort
# ---------------------------------------------------------------------------


def topological_sort(specs: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Sort specs in topological order so dependencies precede dependents.

    Uses Kahn's algorithm.  Dependencies on names *not* in *specs* are
    treated as already satisfied (zero in-degree).  Specs at the same
    dependency level are sorted alphabetically by name for determinism.
    """
    name_to_spec = {s["name"]: s for s in specs}
    spec_names = set(name_to_spec)

    in_degree: dict[str, int] = {n: 0 for n in spec_names}
    dependents: dict[str, list[str]] = {n: [] for n in spec_names}

    for spec in specs:
        for dep in spec.get("depends_on", []):
            if dep in spec_names:
                in_degree[spec["name"]] += 1
                dependents[dep].append(spec["name"])

    queue = sorted(n for n in spec_names if in_degree[n] == 0)
    result: list[dict[str, Any]] = []

    while queue:
        name = queue.pop(0)
        result.append(name_to_spec[name])
        for dep_name in sorted(dependents[name]):
            in_degree[dep_name] -= 1
            if in_degree[dep_name] == 0:
                queue.append(dep_name)
                queue.sort()

    return result


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
        elif status in ("superseded", "stale"):
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


# ---------------------------------------------------------------------------
# Autotriage rubric
# ---------------------------------------------------------------------------

_PRIORITY_ORDER = {"high": 0, "medium": 1, "low": 2}
_INTENT_FIT_KEYWORDS = frozenset({
    "north star", "vision", "intent", "bug", "fix", "test", "assay",
    "reliability", "autopoiesis",
})
_CLEAR_FIX_VERBS = frozenset({
    "add", "fix", "update", "replace", "remove", "write", "implement",
    "create", "validate", "refactor",
})
_VERIFIABLE_KEYWORDS = frozenset({
    "test", "pytest", "verify", "smoke", "browser", "screenshot", "log",
    "assay",
})
_BROAD_ROOTS = frozenset({
    ".", "/", "~", "/Users/terry", "/Users/terry/germline",
    "/Users/terry/code", str(Path.home()),
    str(Path.home() / "germline"),
    str(Path.home() / "code"),
})


def _assess_intent_fit(spec: dict[str, Any]) -> dict[str, Any]:
    priority = spec.get("priority", "medium")
    if priority in ("high", "medium"):
        return {"ok": True, "reason": f"priority={priority}"}
    body_lower = spec.get("body", "").lower()
    for kw in _INTENT_FIT_KEYWORDS:
        if kw in body_lower:
            return {"ok": True, "reason": f"body mentions '{kw}'"}
    return {"ok": False, "reason": "low priority without intent keywords in body"}


def _assess_inferable(spec: dict[str, Any]) -> dict[str, Any]:
    scope = spec.get("scope", [])
    if scope:
        return {"ok": True, "reason": f"scope has {len(scope)} entries"}
    body = spec.get("body", "")
    if re.search(r"(?:[\w./]+\.(?:py|js|ts|md|yaml|yml|toml))", body):
        return {"ok": True, "reason": "body contains file-like paths"}
    if re.search(r"\btest_\w+", body):
        return {"ok": True, "reason": "body mentions exact test names"}
    return {"ok": False, "reason": "empty scope and no file paths or test names in body"}


def _assess_clear_fix(spec: dict[str, Any]) -> dict[str, Any]:
    body_lower = spec.get("body", "").lower()
    for verb in _CLEAR_FIX_VERBS:
        if re.search(rf"\b{verb}\b", body_lower):
            return {"ok": True, "reason": f"body contains action verb '{verb}'"}
    return {"ok": False, "reason": "no concrete action verbs in body"}


def _assess_verifiable(spec: dict[str, Any]) -> dict[str, Any]:
    tests = spec.get("tests", {})
    if isinstance(tests, dict) and tests.get("run"):
        return {"ok": True, "reason": "tests.run is present"}
    body_lower = spec.get("body", "").lower()
    for kw in _VERIFIABLE_KEYWORDS:
        if kw in body_lower:
            return {"ok": True, "reason": f"body mentions '{kw}'"}
    return {"ok": False, "reason": "no tests.run and no verification keywords in body"}


def _assess_bounded_blast_radius(spec: dict[str, Any]) -> dict[str, Any]:
    scope = spec.get("scope", [])
    if not scope:
        return {"ok": False, "reason": "scope is empty"}
    if len(scope) > 5:
        return {"ok": False, "reason": f"scope has {len(scope)} entries (max 5)"}
    for entry in scope:
        normalized = str(entry).rstrip("/")
        if normalized in _BROAD_ROOTS:
            return {"ok": False, "reason": f"broad scope: {entry}"}
    return {"ok": True, "reason": f"{len(scope)} scoped path(s)"}


_GATE_ASSESSORS = [
    ("intent_fit", _assess_intent_fit),
    ("inferable", _assess_inferable),
    ("clear_fix", _assess_clear_fix),
    ("verifiable", _assess_verifiable),
    ("bounded_blast_radius", _assess_bounded_blast_radius),
]


def _assess_candidate(spec: dict[str, Any]) -> dict[str, Any]:
    gates: dict[str, dict[str, Any]] = {}
    for gate_name, assessor in _GATE_ASSESSORS:
        gates[gate_name] = assessor(spec)
    score = sum(1 for g in gates.values() if g["ok"])

    tests = spec.get("tests", {})
    proof = (
        tests["run"]
        if isinstance(tests, dict) and tests.get("run")
        else "manual verification required"
    )

    scope = spec.get("scope", [])
    if not scope:
        blast = "unscoped"
    else:
        broad = [e for e in scope if str(e).rstrip("/") in _BROAD_ROOTS]
        if broad:
            blast = f"broad scope: {broad[0]}"
        else:
            blast = f"{len(scope)} scoped path(s)"

    return {
        "name": spec["name"],
        "path": spec["path"],
        "priority": spec.get("priority", "medium"),
        "score": score,
        "gates": gates,
        "proof_required": proof,
        "blast_radius": blast,
    }


def _defer_reason(spec: dict[str, Any]) -> str:
    status = spec.get("status", "ready")
    if status != "ready":
        return f"status={status}"
    if spec.get("depends_on"):
        deps = spec.get("depends_on", [])
        return f"waiting on depends_on: {', '.join(deps)}"
    return "not dispatchable"


def autotriage(resolved_specs: list[dict[str, Any]], directory: str) -> dict[str, Any]:
    """Rank dispatchable specs by autonomy rubric, defer the rest.

    Returns a porin-compatible dict with best_next, ready, defer, counts, directory.
    """
    dispatchable = [s for s in resolved_specs if s.get("dispatchable")]
    non_dispatchable = [s for s in resolved_specs if not s.get("dispatchable")]

    ready = [_assess_candidate(s) for s in dispatchable]
    ready.sort(
        key=lambda c: (
            _PRIORITY_ORDER.get(c["priority"], 99),
            -c["score"],
            c["name"],
        )
    )

    defer = [
        {
            "name": s["name"],
            "path": s["path"],
            "status": s.get("status", "ready"),
            "reason": _defer_reason(s),
        }
        for s in non_dispatchable
    ]

    return {
        "best_next": ready[0] if ready else None,
        "ready": ready,
        "defer": defer,
        "counts": {"ready": len(ready), "defer": len(defer)},
        "directory": directory,
    }
