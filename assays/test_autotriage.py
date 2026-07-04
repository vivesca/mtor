"""Tests for mtor autotriage rubric — gate assessment, ranking, and CLI."""

from __future__ import annotations

import io
import json
import sys
from pathlib import Path

from mtor.cli import app
from mtor.rptor import autotriage, resolve_dag, scan_specs


def _invoke(args: list[str]) -> tuple[int, dict]:
    captured = io.StringIO()
    old_stdout = sys.stdout
    exit_code = 0
    try:
        sys.stdout = captured
        app(args)
    except SystemExit as exc:
        exit_code = exc.code if isinstance(exc.code, int) else 1
    finally:
        sys.stdout = old_stdout
    return exit_code, json.loads(captured.getvalue())


def _write(tmp_path: Path, name: str, content: str) -> Path:
    p = tmp_path / f"{name}.md"
    p.write_text(content, encoding="utf-8")
    return p


def test_high_priority_scoped_with_tests_is_best_next(tmp_path):
    """A high-priority scoped spec with tests.run becomes best_next."""
    _write(tmp_path, "high-good", (
        "---\n"
        "status: ready\n"
        "priority: high\n"
        "scope:\n"
        "  - mtor/rptor.py\n"
        "  - assays/test_rptor.py\n"
        "tests:\n"
        "  run: pytest assays/test_rptor.py -v\n"
        "---\n"
        "## Goal\n\n"
        "Add autotriage ranking to rptor.\n"
    ))
    _write(tmp_path, "low-vague", (
        "---\n"
        "status: ready\n"
        "priority: low\n"
        "---\n"
        "Consider improvements.\n"
    ))
    _write(tmp_path, "done-spec", (
        "---\n"
        "status: done\n"
        "completed_at: 2026-01-01T00:00:00Z\n"
        "---\n"
    ))

    specs = scan_specs(tmp_path)
    resolved = resolve_dag(specs)
    result = autotriage(resolved, str(tmp_path))

    assert result["best_next"] is not None
    assert result["best_next"]["name"] == "high-good"
    assert result["best_next"]["score"] == 5
    assert result["best_next"]["gates"]["intent_fit"]["ok"] is True
    assert result["best_next"]["gates"]["bounded_blast_radius"]["ok"] is True
    assert result["best_next"]["blast_radius"] == "2 scoped path(s)"
    assert result["counts"]["ready"] == 2
    assert result["counts"]["defer"] == 1


def test_broad_unscoped_spec_scores_lower(tmp_path):
    """A broad/unscoped spec is scored lower and has bounded_blast_radius=False."""
    _write(tmp_path, "broad-spec", (
        "---\n"
        "status: ready\n"
        "priority: high\n"
        "scope:\n"
        "  - .\n"
        "---\n"
        "Fix the entire codebase.\n"
    ))
    _write(tmp_path, "no-scope", (
        "---\n"
        "status: ready\n"
        "priority: medium\n"
        "---\n"
        "Maybe refactor something.\n"
    ))

    specs = scan_specs(tmp_path)
    resolved = resolve_dag(specs)
    result = autotriage(resolved, str(tmp_path))

    broad = next(c for c in result["ready"] if c["name"] == "broad-spec")
    assert broad["gates"]["bounded_blast_radius"]["ok"] is False
    assert "broad scope" in broad["gates"]["bounded_blast_radius"]["reason"]

    no_scope = next(c for c in result["ready"] if c["name"] == "no-scope")
    assert no_scope["gates"]["bounded_blast_radius"]["ok"] is False
    assert no_scope["blast_radius"] == "unscoped"


def test_spec_waiting_on_dependency_in_defer(tmp_path):
    """A spec waiting on dependency appears in defer with dependency reason."""
    _write(tmp_path, "dep-a", (
        "---\n"
        "status: ready\n"
        "---\n"
    ))
    _write(tmp_path, "dep-b", (
        "---\n"
        "status: ready\n"
        "depends_on:\n"
        "  - dep-a\n"
        "---\n"
    ))
    _write(tmp_path, "done-base", (
        "---\n"
        "status: dispatched\n"
        "---\n"
    ))

    specs = scan_specs(tmp_path)
    resolved = resolve_dag(specs)
    result = autotriage(resolved, str(tmp_path))

    deferred_b = next(d for d in result["defer"] if d["name"] == "dep-b")
    assert "waiting on depends_on: dep-a" in deferred_b["reason"]

    dispatched = next(d for d in result["defer"] if d["name"] == "done-base")
    assert dispatched["reason"] == "status=dispatched"


def test_cli_autotriage_returns_full_structure(tmp_path):
    """mtor rptor --autotriage --dir <tmp> returns best_next, ready, defer, counts."""
    _write(tmp_path, "good-spec", (
        "---\n"
        "status: ready\n"
        "priority: high\n"
        "scope:\n"
        "  - mtor/rptor.py\n"
        "tests:\n"
        "  run: pytest assays/test_rptor.py -v\n"
        "---\n"
        "Add the autotriage feature.\n"
    ))
    _write(tmp_path, "blocked-spec", (
        "---\n"
        "status: ready\n"
        "depends_on:\n"
        "  - good-spec\n"
        "---\n"
    ))

    exit_code, data = _invoke(["rptor", "--autotriage", "--dir", str(tmp_path)])

    assert exit_code == 0
    assert data["ok"] is True
    result = data["result"]
    assert "best_next" in result
    assert "ready" in result
    assert "defer" in result
    assert "counts" in result
    assert result["directory"] == str(tmp_path)
    assert result["best_next"]["name"] == "good-spec"
    assert result["counts"]["ready"] == 1
    assert result["counts"]["defer"] == 1
