"""Scan — organism gap detection.

Runs deterministic checks on the codebase:
  1. grep for TODO/FIXME in effectors (hygiene)
  2. find effectors without assays/ directories (coverage)
  3. check for stale marks older than 30 days in ~/epigenome/marks/ (maintenance)
  4. detect divergent forks — an in-tree package (germline/packages|effectors)
     that also lives standalone in ~/code, i.e. an incomplete abscission
     (the donor copy was never removed after the component graduated). (maintenance)
"""

from __future__ import annotations

import os
import re
import time
import tomllib
from pathlib import Path
from typing import Any

REPO_DIR = os.environ.get("MTOR_REPO_DIR", str(Path.home() / "germline"))
EPIGENOME_DIR = os.environ.get("MTOR_EPIGENOME_DIR", str(Path.home() / "epigenome"))
CODE_DIR = os.environ.get("MTOR_CODE_DIR", str(Path.home() / "code"))

VALID_CATEGORIES: tuple[str, ...] = ("hygiene", "coverage", "maintenance")

_STALE_DAYS = 30
_TODO_PATTERN = re.compile(r"\b(TODO|FIXME)\b")
# Parents under germline that hold in-tree Python packages, checked for
# standalone twins in ~/code (the graduated-component location).
_IN_TREE_PARENTS: tuple[str, ...] = ("packages", "effectors")


def _run_checks(
    effectors_dir: Path | None = None,
    marks_dir: Path | None = None,
    repo_dir: Path | None = None,
    code_dir: Path | None = None,
) -> list[dict[str, Any]]:
    """Run all scan checks and return findings.

    Parameters
    ----------
    effectors_dir : Path
        Directory containing effector subdirectories. Defaults to
        ``$REPO_DIR/effectors``.
    marks_dir : Path
        Directory containing mark files. Defaults to ``~/epigenome/marks``.
    repo_dir : Path
        Germline root (holds ``packages/`` and ``effectors/``). Defaults to
        ``$REPO_DIR``. Used by the divergent-fork check.
    code_dir : Path
        Standalone-component root. Defaults to ``$CODE_DIR`` (``~/code``).
        Used by the divergent-fork check.

    Returns
    -------
    list[dict]
        Each dict has keys: description, category, priority, target.
    """
    if effectors_dir is None:
        effectors_dir = Path(REPO_DIR) / "effectors"
    if marks_dir is None:
        marks_dir = Path(EPIGENOME_DIR) / "marks"
    if repo_dir is None:
        repo_dir = Path(REPO_DIR)
    if code_dir is None:
        code_dir = Path(CODE_DIR)

    findings: list[dict[str, Any]] = []
    findings.extend(_check_todo_fixme(effectors_dir))
    findings.extend(_check_missing_assays(effectors_dir))
    findings.extend(_check_stale_marks(marks_dir))
    findings.extend(_check_divergent_forks(repo_dir, code_dir))
    return findings


def _check_todo_fixme(effectors_dir: Path) -> list[dict[str, Any]]:
    """Find TODO/FIXME comments in effector source files."""
    findings: list[dict[str, Any]] = []
    if not effectors_dir.is_dir():
        return findings

    for child in sorted(effectors_dir.iterdir()):
        if not child.is_dir():
            continue
        if child.name.startswith(".") or child.name == "__pycache__":
            continue
        # Skip .venv and other non-effector directories
        if child.name == ".venv" or child.name.startswith("."):
            continue
        for py_file in child.rglob("*.py"):
            # Skip .venv directories within effectors
            if ".venv" in py_file.parts:
                continue
            try:
                text = py_file.read_text(errors="ignore")
            except OSError:
                continue
            for line_no, line in enumerate(text.splitlines(), 1):
                if _TODO_PATTERN.search(line):
                    findings.append({
                        "description": f"Found {line.strip()} at {py_file.name}:{line_no}",
                        "category": "hygiene",
                        "priority": "low",
                        "target": str(py_file.relative_to(effectors_dir)),
                    })
    return findings


def _check_missing_assays(effectors_dir: Path) -> list[dict[str, Any]]:
    """Find effectors that lack an assays/ directory."""
    findings: list[dict[str, Any]] = []
    if not effectors_dir.is_dir():
        return findings

    for child in sorted(effectors_dir.iterdir()):
        if not child.is_dir():
            continue
        if child.name.startswith(".") or child.name == "__pycache__":
            continue
        if child.name == ".venv":
            continue
        assays_dir = child / "assays"
        if not assays_dir.is_dir():
            findings.append({
                "description": f"Effector '{child.name}' has no assays/ directory",
                "category": "coverage",
                "priority": "medium",
                "target": child.name,
            })
    return findings


def _check_stale_marks(marks_dir: Path) -> list[dict[str, Any]]:
    """Summarize mark files older than 30 days as one maintenance batch."""
    if not marks_dir.is_dir():
        return []

    cutoff = time.time() - _STALE_DAYS * 86400
    count = 0
    oldest_days = 0
    for mark_file in sorted(marks_dir.iterdir()):
        if not mark_file.is_file():
            continue
        try:
            mtime = mark_file.stat().st_mtime
        except OSError:
            continue
        if mtime < cutoff:
            days_stale = int((time.time() - mtime) / 86400)
            count += 1
            oldest_days = max(oldest_days, days_stale)
    if not count:
        return []

    noun = "mark" if count == 1 else "marks"
    return [
        {
            "description": (
                f"{count} stale {noun} older than {_STALE_DAYS} days "
                f"(oldest {oldest_days} days); review as one maintenance batch"
            ),
            "category": "maintenance",
            "priority": "high",
            "target": str(marks_dir),
        }
    ]


def _pkg_name_version(pkg_root: Path) -> tuple[str | None, str | None]:
    """Read the declared package name and (static) version from a pyproject.toml.

    Returns ``(None, None)`` when there is no parseable ``[project].name``.
    A dynamic version (``dynamic = ["version"]``) yields ``(name, None)`` —
    the name match alone is enough to detect a twin.
    """
    pyproject = pkg_root / "pyproject.toml"
    if not pyproject.is_file():
        return None, None
    try:
        data = tomllib.loads(pyproject.read_text(errors="ignore"))
    except (OSError, tomllib.TOMLDecodeError, ValueError):
        return None, None
    project = data.get("project", {})
    name = project.get("name")
    if not isinstance(name, str):
        return None, None
    version = project.get("version")
    return name, version if isinstance(version, str) else None


def _check_divergent_forks(repo_dir: Path, code_dir: Path) -> list[dict[str, Any]]:
    """Flag in-tree packages that also exist standalone in ~/code.

    The smell is an *incomplete abscission*: a component graduated to a
    standalone repo (``~/code/<name>``) but its in-tree donor copy under
    ``germline/packages|effectors`` was never removed (abscission step 8).
    The two then drift, and the stale copy looks load-bearing to anyone
    auditing it because nobody is sure which is live.

    Detection is deliberately conservative: it only fires when the same
    *declared package name* appears both in-tree and at ``~/code/<name>``.
    That matches the organism's topology (``~/code`` holds graduated
    standalones) and avoids false-positiving on effectors installed
    editable from their own in-tree location.
    """
    findings: list[dict[str, Any]] = []
    if not code_dir.is_dir():
        return findings

    for parent_name in _IN_TREE_PARENTS:
        parent = repo_dir / parent_name
        if not parent.is_dir():
            continue
        for child in sorted(parent.iterdir()):
            if not child.is_dir() or child.name.startswith(".") or child.name == "__pycache__":
                continue
            in_name, in_ver = _pkg_name_version(child)
            if not in_name:
                continue
            standalone = code_dir / in_name
            try:
                if not standalone.is_dir() or standalone.resolve() == child.resolve():
                    continue
            except OSError:
                continue
            st_name, st_ver = _pkg_name_version(standalone)
            # Require the standalone to declare the SAME package name — a bare
            # directory-name collision is not a divergent fork.
            if st_name != in_name:
                continue

            if in_ver and st_ver and in_ver != st_ver:
                ver_note = f" at diverging versions (in-tree {in_ver} vs standalone {st_ver})"
            elif in_ver and st_ver and in_ver == st_ver:
                ver_note = f" (both {in_ver} — a vendored second copy)"
            else:
                ver_note = ""

            findings.append({
                "description": (
                    f"Possible incomplete abscission: package '{in_name}' exists both "
                    f"in-tree at {parent_name}/{child.name} and standalone at {standalone}{ver_note}. "
                    f"Re-derive which one is live (recipe, not a stale note), then remove or "
                    f"demote the donor per the abscission step-8 completion gate."
                ),
                "category": "maintenance",
                "priority": "high",
                "target": f"{parent_name}/{child.name}",
            })
    return findings
