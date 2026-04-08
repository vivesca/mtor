"""Tests for mtor.plan — spec parsing, DAG resolution, and plan CLI commands."""

from __future__ import annotations

import io
import json
import sys
import tempfile
from contextlib import contextmanager
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from mtor.cli import app
from mtor.rptor import (
    CycleDetected,
    display_dag,
    parse_spec,
    resolve_dag,
    scan_specs,
)
from mtor.dispatch import _inject_spec_constraints


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


@contextmanager
def _tmp_spec(content: str, name: str = "test-spec"):
    """Write content to a temp .md file, yield Path, clean up."""
    with tempfile.TemporaryDirectory() as td:
        path = Path(td) / f"{name}.md"
        path.write_text(content, encoding="utf-8")
        yield path


def invoke(args: list[str] | None = None) -> tuple[int, dict]:
    """Invoke CLI and return (exit_code, parsed_json)."""
    captured = io.StringIO()
    old_stdout = sys.stdout
    exit_code = 0
    try:
        sys.stdout = captured
        app(args or [])
    except SystemExit as exc:
        exit_code = exc.code if isinstance(exc.code, int) else 1
    finally:
        sys.stdout = old_stdout

    output = captured.getvalue()
    try:
        data = json.loads(output)
    except json.JSONDecodeError:
        raise AssertionError(
            f"Output is not valid JSON. Exit={exit_code}\nOutput: {output!r}"
        )
    return exit_code, data


# ---------------------------------------------------------------------------
# Frontmatter parsing
# ---------------------------------------------------------------------------


def test_parse_spec_frontmatter(tmp_path):
    """Full frontmatter with all fields parses correctly."""
    spec_file = tmp_path / "mtor-archive-command.md"
    spec_file.write_text(
        "---\n"
        "status: ready\n"
        "priority: high\n"
        "repo: ~/code/mtor\n"
        "depends_on:\n"
        "  - other-spec\n"
        "  - another-spec\n"
        "scope:\n"
        "  - mtor/plan.py\n"
        "  - mtor/cli.py\n"
        "exclude:\n"
        "  - genome.md\n"
        "  - uv.lock\n"
        "tests:\n"
        "  run: cd ~/code/mtor && python -m pytest assays/test_plan.py -v\n"
        "  functions:\n"
        "    - parse_spec\n"
        "    - scan_specs\n"
        "---\n"
        "## Problem\n\n"
        "Something.\n",
        encoding="utf-8",
    )

    result = parse_spec(spec_file)

    assert result["name"] == "mtor-archive-command"
    assert result["status"] == "ready"
    assert result["priority"] == "high"
    assert result["repo"] == "~/code/mtor"
    assert result["depends_on"] == ["other-spec", "another-spec"]
    assert result["scope"] == ["mtor/plan.py", "mtor/cli.py"]
    assert result["exclude"] == ["genome.md", "uv.lock"]
    assert result["tests"]["run"] == "cd ~/code/mtor && python -m pytest assays/test_plan.py -v"
    assert result["tests"]["functions"] == ["parse_spec", "scan_specs"]
    assert result["path"] == str(spec_file.resolve())
    assert "## Problem" in result["body"]


def test_parse_spec_minimal(tmp_path):
    """Minimal frontmatter (just status) gets sensible defaults."""
    spec_file = tmp_path / "minimal.md"
    spec_file.write_text("---\nstatus: ready\n---\nBody text.\n", encoding="utf-8")

    result = parse_spec(spec_file)

    assert result["name"] == "minimal"
    assert result["status"] == "ready"
    assert result["priority"] == "medium"  # default
    assert result["repo"] == "~"  # default
    assert result["depends_on"] == []
    assert result["scope"] == []
    assert result["exclude"] == []
    assert result["tests"] == {}
    assert result["body"] == "Body text."


def test_parse_spec_missing_file():
    """parse_spec raises FileNotFoundError for missing files."""
    with pytest.raises(FileNotFoundError):
        parse_spec(Path("/nonexistent/test-spec.md"))


# ---------------------------------------------------------------------------
# Directory scanning
# ---------------------------------------------------------------------------


def test_scan_specs_directory(tmp_path):
    """scan_specs finds and parses all .md files in a directory."""
    (tmp_path / "spec-a.md").write_text("---\nstatus: done\n---\nA.\n", encoding="utf-8")
    (tmp_path / "spec-b.md").write_text("---\nstatus: ready\n---\nB.\n", encoding="utf-8")
    (tmp_path / "readme.txt").write_text("not a spec\n", encoding="utf-8")

    specs = scan_specs(tmp_path)

    assert len(specs) == 2
    names = {s["name"] for s in specs}
    assert names == {"spec-a", "spec-b"}


def test_scan_specs_empty_directory(tmp_path):
    """scan_specs returns empty list when no .md files exist."""
    specs = scan_specs(tmp_path)
    assert specs == []


def test_scan_specs_nonexistent_directory():
    """scan_specs returns empty list for non-existent directory."""
    specs = scan_specs(Path("/nonexistent/directory"))
    assert specs == []


# ---------------------------------------------------------------------------
# DAG resolution
# ---------------------------------------------------------------------------


def test_dependency_resolution_dispatchable(tmp_path):
    """Spec with all deps done is marked dispatchable."""
    done_a = tmp_path / "spec-a.md"
    done_a.write_text("---\nstatus: done\n---\n", encoding="utf-8")
    done_b = tmp_path / "spec-b.md"
    done_b.write_text("---\nstatus: done\n---\n", encoding="utf-8")
    ready = tmp_path / "spec-c.md"
    ready.write_text(
        "---\nstatus: ready\ndepends_on:\n  - spec-a\n  - spec-b\n---\n",
        encoding="utf-8",
    )

    specs = scan_specs(tmp_path)
    resolved = resolve_dag(specs)

    spec_c = next(s for s in resolved if s["name"] == "spec-c")
    assert spec_c["dispatchable"] is True


def test_dependency_resolution_blocked(tmp_path):
    """Spec with pending deps is blocked (not dispatchable)."""
    ready = tmp_path / "spec-a.md"
    ready.write_text("---\nstatus: ready\n---\n", encoding="utf-8")
    blocked = tmp_path / "spec-b.md"
    blocked.write_text(
        "---\nstatus: ready\ndepends_on:\n  - spec-a\n---\n",
        encoding="utf-8",
    )

    specs = scan_specs(tmp_path)
    resolved = resolve_dag(specs)

    spec_b = next(s for s in resolved if s["name"] == "spec-b")
    assert spec_b["dispatchable"] is False


def test_circular_dependency_detected(tmp_path):
    """CycleDetected is raised when specs have circular dependencies."""
    (tmp_path / "a.md").write_text(
        "---\nstatus: ready\ndepends_on:\n  - b\n---\n",
        encoding="utf-8",
    )
    (tmp_path / "b.md").write_text(
        "---\nstatus: ready\ndepends_on:\n  - a\n---\n",
        encoding="utf-8",
    )

    specs = scan_specs(tmp_path)
    with pytest.raises(CycleDetected) as exc_info:
        resolve_dag(specs)

    assert "a" in exc_info.value.cycle
    assert "b" in exc_info.value.cycle


def test_resolve_dag_already_done_not_dispatchable(tmp_path):
    """Spec with status=done is never dispatchable even if deps are met."""
    done = tmp_path / "spec-a.md"
    done.write_text("---\nstatus: done\n---\n", encoding="utf-8")
    done2 = tmp_path / "spec-b.md"
    done2.write_text("---\nstatus: done\ndepends_on:\n  - spec-a\n---\n", encoding="utf-8")

    specs = scan_specs(tmp_path)
    resolved = resolve_dag(specs)

    spec_b = next(s for s in resolved if s["name"] == "spec-b")
    assert spec_b["dispatchable"] is False


def test_resolve_dag_dispatched_not_dispatchable(tmp_path):
    """Spec with status=dispatched is never dispatchable."""
    disp = tmp_path / "spec-a.md"
    disp.write_text("---\nstatus: dispatched\ndepends_on:\n  - other\n---\n", encoding="utf-8")

    specs = scan_specs(tmp_path)
    resolved = resolve_dag(specs)

    spec_a = next(s for s in resolved if s["name"] == "spec-a")
    assert spec_a["dispatchable"] is False


# ---------------------------------------------------------------------------
# DAG display
# ---------------------------------------------------------------------------


def test_display_dag_buckets(tmp_path):
    """display_dag groups specs into correct status buckets."""
    (tmp_path / "done-a.md").write_text("---\nstatus: done\n---\n", encoding="utf-8")
    (tmp_path / "done-b.md").write_text("---\nstatus: done\n---\n", encoding="utf-8")
    (tmp_path / "ready-a.md").write_text("---\nstatus: ready\n---\n", encoding="utf-8")
    (tmp_path / "blocked-a.md").write_text(
        "---\nstatus: ready\ndepends_on:\n  - ready-a\n---\n",
        encoding="utf-8",
    )
    (tmp_path / "disp-a.md").write_text("---\nstatus: dispatched\n---\n", encoding="utf-8")
    (tmp_path / "super-a.md").write_text("---\nstatus: superseded\n---\n", encoding="utf-8")

    specs = scan_specs(tmp_path)
    resolved = resolve_dag(specs)
    dag = display_dag(resolved)

    assert dag["counts"]["done"] == 2
    assert dag["counts"]["dispatched"] == 1
    assert dag["counts"]["ready"] == 1
    assert dag["counts"]["blocked"] == 1
    assert dag["counts"]["superseded"] == 1
    assert dag["counts"]["other"] == 0


# ---------------------------------------------------------------------------
# CLI: plan command
# ---------------------------------------------------------------------------


def test_plan_shows_dag(tmp_path):
    """mtor plan returns DAG with specs and counts."""
    (tmp_path / "alpha.md").write_text("---\nstatus: done\n---\n", encoding="utf-8")
    (tmp_path / "beta.md").write_text("---\nstatus: ready\n---\n", encoding="utf-8")

    exit_code, data = invoke(["plan", "--dir", str(tmp_path)])

    assert exit_code == 0
    assert data["ok"] is True
    assert data["result"]["counts"]["done"] == 1
    assert data["result"]["counts"]["ready"] == 1
    assert data["result"]["directory"] == str(tmp_path)


def test_plan_pending_only_shows_ready(tmp_path):
    """mtor plan --pending shows only dispatchable specs."""
    (tmp_path / "alpha.md").write_text("---\nstatus: done\n---\n", encoding="utf-8")
    (tmp_path / "beta.md").write_text(
        "---\nstatus: ready\ndepends_on:\n  - alpha\n---\n",
        encoding="utf-8",
    )

    exit_code, data = invoke(["plan", "--pending", "--dir", str(tmp_path)])

    assert exit_code == 0
    assert "ready" in data["result"]["specs"]
    assert data["result"]["counts"]["ready"] == 1


def test_plan_empty_directory(tmp_path):
    """mtor plan on empty directory returns empty lists with zero counts."""
    exit_code, data = invoke(["plan", "--dir", str(tmp_path)])

    assert exit_code == 0
    assert data["result"]["specs"] == []
    assert data["result"]["counts"]["ready"] == 0


def test_plan_circular_dependency_reported(tmp_path):
    """mtor plan exits with error when circular dependency is found."""
    (tmp_path / "x.md").write_text("---\nstatus: ready\ndepends_on:\n  - y\n---\n", encoding="utf-8")
    (tmp_path / "y.md").write_text("---\nstatus: ready\ndepends_on:\n  - x\n---\n", encoding="utf-8")

    exit_code, data = invoke(["plan", "--dir", str(tmp_path)])

    assert exit_code == 1
    assert data["ok"] is False
    assert data["error"]["code"] == "CIRCULAR_DEPENDENCY"
    assert "Circular dependency" in data["error"]["message"]


# ---------------------------------------------------------------------------
# CLI: plan done command
# ---------------------------------------------------------------------------


def test_plan_done_updates_status(tmp_path):
    """mtor plan done <name> updates spec status to done."""
    spec_file = tmp_path / "my-spec.md"
    spec_file.write_text(
        "---\nstatus: ready\nrepo: ~\n---\n## Problem\n\nTest.\n",
        encoding="utf-8",
    )

    exit_code, data = invoke(["plan_done", "my-spec", "--dir", str(tmp_path)])

    assert exit_code == 0
    assert data["ok"] is True
    assert data["result"]["name"] == "my-spec"
    assert data["result"]["status"] == "done"

    # Verify the file was actually updated
    text = spec_file.read_text(encoding="utf-8")
    assert "status: done" in text


def test_plan_done_missing_spec(tmp_path):
    """mtor plan done for missing spec returns SPEC_NOT_FOUND error."""
    exit_code, data = invoke(["plan_done", "nonexistent", "--dir", str(tmp_path)])

    assert exit_code == 1
    assert data["ok"] is False
    assert data["error"]["code"] == "SPEC_NOT_FOUND"


# ---------------------------------------------------------------------------
# Dispatch: scope/test injection
# ---------------------------------------------------------------------------


def test_scope_injected_into_prompt(tmp_path):
    """_inject_spec_constraints appends scope CONSTRAINT to prompt."""
    spec_file = tmp_path / "scope-spec.md"
    spec_file.write_text(
        "---\nstatus: ready\nscope:\n  - mtor/plan.py\n  - mtor/cli.py\nexclude:\n  - genome.md\n---\n",
        encoding="utf-8",
    )

    result = _inject_spec_constraints(
        "Write the plan module.",
        spec_path=spec_file,
        prompt_for_cmd="Write the plan module.",
    )

    assert "CONSTRAINT: Only modify mtor/plan.py, mtor/cli.py." in result
    assert "Do NOT modify: genome.md." in result


def test_tests_injected_into_prompt(tmp_path):
    """_inject_spec_constraints appends test run + functions to prompt."""
    spec_file = tmp_path / "test-spec.md"
    spec_file.write_text(
        "---\nstatus: ready\ntests:\n  run: cd ~/code/mtor && python -m pytest assays/test_plan.py -v\n  functions:\n    - parse_spec\n    - scan_specs\n---\n",
        encoding="utf-8",
    )

    result = _inject_spec_constraints(
        "Build the feature.",
        spec_path=spec_file,
        prompt_for_cmd="Build the feature.",
    )

    assert "Run: cd ~/code/mtor && python -m pytest assays/test_plan.py -v" in result
    assert "Verify test functions:" in result
    assert "test_parse_spec" in result
    assert "test_scan_specs" in result


def test_spec_injection_repo_context(tmp_path):
    """_inject_spec_constraints adds repo context when non-default."""
    spec_file = tmp_path / "repo-spec.md"
    spec_file.write_text(
        "---\nstatus: ready\nrepo: /home/vivesca/code/mtor\n---\n",
        encoding="utf-8",
    )

    result = _inject_spec_constraints(
        "Implement feature.",
        spec_path=spec_file,
        prompt_for_cmd="Implement feature.",
    )

    assert "Working directory: /home/vivesca/code/mtor" in result


def test_spec_injection_no_spec_path_unchanged():
    """_inject_spec_constraints returns base prompt unchanged when spec_path is None."""
    result = _inject_spec_constraints(
        "Original prompt.",
        spec_path=None,
        prompt_for_cmd="Original prompt.",
    )
    assert result == "Original prompt."
