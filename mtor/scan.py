"""Scan — organism gap detection.

Runs deterministic checks on the codebase:
  1. grep for TODO/FIXME in effectors (hygiene)
  2. find effectors without assays/ directories (coverage)
  3. check for stale marks older than 30 days in ~/epigenome/marks/ (maintenance)
"""

from __future__ import annotations

import os
import re
import time
from pathlib import Path
from typing import Any

REPO_DIR = os.environ.get("MTOR_REPO_DIR", str(Path.home() / "germline"))
EPIGENOME_DIR = os.environ.get("MTOR_EPIGENOME_DIR", str(Path.home() / "epigenome"))

VALID_CATEGORIES: tuple[str, ...] = ("hygiene", "coverage", "maintenance")

_STALE_DAYS = 30
_TODO_PATTERN = re.compile(r"\b(TODO|FIXME)\b")


def _run_checks(
    effectors_dir: Path | None = None,
    marks_dir: Path | None = None,
) -> list[dict[str, Any]]:
    """Run all scan checks and return findings.

    Parameters
    ----------
    effectors_dir : Path
        Directory containing effector subdirectories. Defaults to
        ``$REPO_DIR/effectors``.
    marks_dir : Path
        Directory containing mark files. Defaults to ``~/epigenome/marks``.

    Returns
    -------
    list[dict]
        Each dict has keys: description, category, priority, target.
    """
    if effectors_dir is None:
        effectors_dir = Path(REPO_DIR) / "effectors"
    if marks_dir is None:
        marks_dir = Path(EPIGENOME_DIR) / "marks"

    findings: list[dict[str, Any]] = []
    findings.extend(_check_todo_fixme(effectors_dir))
    findings.extend(_check_missing_assays(effectors_dir))
    findings.extend(_check_stale_marks(marks_dir))
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
    """Find mark files older than 30 days."""
    findings: list[dict[str, Any]] = []
    if not marks_dir.is_dir():
        return findings

    cutoff = time.time() - _STALE_DAYS * 86400
    for mark_file in sorted(marks_dir.iterdir()):
        if not mark_file.is_file():
            continue
        try:
            mtime = mark_file.stat().st_mtime
        except OSError:
            continue
        if mtime < cutoff:
            days_stale = int((time.time() - mtime) / 86400)
            findings.append({
                "description": f"Stale mark '{mark_file.name}' ({days_stale} days old)",
                "category": "maintenance",
                "priority": "high",
                "target": str(mark_file),
            })
    return findings
