"""Tests for mtor.plan — spec parsing, DAG resolution, and plan CLI commands."""

from __future__ import annotations

import io
import json
import sys
import tempfile
from contextlib import contextmanager
from pathlib import Path

import pytest

from mtor.cli import app
from mtor.rptor import (
    CycleDetected,
    audit_specs,
    display_dag,
    parse_spec,
    resolve_dag,
    scan_specs,
)
from mtor.dispatch import _inject_spec_constraints
from mtor.spec import validate_spec


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
        "workflow_id: wf-123\n"
        "verdict: accepted\n"
        "depends_on:\n"
        "  - other-spec\n"
        "  - another-spec\n"
        "scope:\n"
        "  - mtor/rptor.py\n"
        "  - mtor/cli.py\n"
        "exclude:\n"
        "  - genome.md\n"
        "  - uv.lock\n"
        "tests:\n"
        "  run: cd ~/code/mtor && python -m pytest assays/test_rptor.py -v\n"
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
    assert result["workflow_id"] == "wf-123"
    assert result["verdict"] == "accepted"
    assert result["depends_on"] == ["other-spec", "another-spec"]
    assert result["scope"] == ["mtor/rptor.py", "mtor/cli.py"]
    assert result["exclude"] == ["genome.md", "uv.lock"]
    assert result["tests"]["run"] == "cd ~/code/mtor && python -m pytest assays/test_rptor.py -v"
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


def test_parse_spec_completion_metadata(tmp_path):
    """Completion and audit metadata are available to quality checks."""
    spec_file = tmp_path / "completed.md"
    spec_file.write_text(
        "---\n"
        "status: done\n"
        "completed_at: 2026-05-15T00:00:00+00:00\n"
        "completed_commit: abc1234\n"
        "completed_note: Verified with focused tests.\n"
        "completed_by: codex\n"
        "audit_status: audited_present\n"
        "audit_reason: implementation present\n"
        "---\n"
        "Body text.\n",
        encoding="utf-8",
    )

    result = parse_spec(spec_file)

    assert result["completed_at"] == "2026-05-15T00:00:00+00:00"
    assert result["completed_commit"] == "abc1234"
    assert result["completed_note"] == "Verified with focused tests."
    assert result["completed_by"] == "codex"
    assert result["audit_status"] == "audited_present"
    assert result["audit_reason"] == "implementation present"


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


@pytest.mark.parametrize("status", ["draft", "failed", "open", "queued", "shipped"])
def test_resolve_dag_only_ready_status_dispatchable(tmp_path, status):
    """Specs with non-ready workflow states are not dispatchable."""
    spec_file = tmp_path / "spec-a.md"
    spec_file.write_text(f"---\nstatus: {status}\n---\n", encoding="utf-8")

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
# Spec audit
# ---------------------------------------------------------------------------


def test_audit_specs_flags_invalid_status(tmp_path):
    """Audit reports status vocabulary drift."""
    (tmp_path / "invalid.md").write_text("---\nstatus: open\n---\n", encoding="utf-8")

    audit = audit_specs(scan_specs(tmp_path))

    assert audit["ok"] is False
    assert audit["counts"]["issues"]["invalid_status"] == 1
    assert audit["issues"][0]["name"] == "invalid"


def test_audit_specs_flags_done_without_evidence(tmp_path):
    """Done specs need provenance, not just a status flip."""
    (tmp_path / "weak.md").write_text("---\nstatus: done\n---\n", encoding="utf-8")

    audit = audit_specs(scan_specs(tmp_path))

    assert audit["ok"] is False
    assert audit["counts"]["issues"]["done_without_evidence"] == 1


def test_audit_specs_accepts_done_with_completion_evidence(tmp_path):
    """A done spec with completion metadata passes the hard audit gates."""
    (tmp_path / "done.md").write_text(
        "---\nstatus: done\ncompleted_at: 2026-05-15T00:00:00+00:00\n---\n",
        encoding="utf-8",
    )

    audit = audit_specs(scan_specs(tmp_path))

    assert audit["ok"] is True
    assert audit["counts"]["issues"] == {}


def test_validate_spec_allows_archival_metadata(tmp_path):
    """Historical plan metadata is not a hard validation failure."""
    spec_file = tmp_path / "archive.md"
    spec_file.write_text(
        "---\nstatus: stale\naudited_at: 2026-05-15\nauthor: terry\n---\n",
        encoding="utf-8",
    )

    assert validate_spec(spec_file) == []


def test_validate_spec_ignores_archival_missing_dependencies(tmp_path):
    """Stale historical specs may reference removed or renamed ancestors."""
    spec_file = tmp_path / "archive.md"
    spec_file.write_text(
        "---\nstatus: stale\ndepends_on:\n  - removed-spec\n---\n",
        encoding="utf-8",
    )

    assert validate_spec(spec_file) == []


def test_validate_spec_checks_active_missing_dependencies(tmp_path):
    """Ready specs still need resolvable dependencies."""
    spec_file = tmp_path / "active.md"
    spec_file.write_text(
        "---\nstatus: ready\ndepends_on:\n  - missing-spec\n---\n",
        encoding="utf-8",
    )

    errors = validate_spec(spec_file)

    assert errors == [f"{spec_file}: depends_on target 'missing-spec' does not exist"]


# ---------------------------------------------------------------------------
# CLI: rptor command
# ---------------------------------------------------------------------------


def test_rptor_shows_dag(tmp_path):
    """mtor rptor returns DAG with specs and counts."""
    (tmp_path / "alpha.md").write_text("---\nstatus: done\n---\n", encoding="utf-8")
    (tmp_path / "beta.md").write_text("---\nstatus: ready\n---\n", encoding="utf-8")

    exit_code, data = invoke(["rptor", "--dir", str(tmp_path)])

    assert exit_code == 0
    assert data["ok"] is True
    assert data["result"]["counts"]["done"] == 1
    assert data["result"]["counts"]["ready"] == 1
    assert data["result"]["directory"] == str(tmp_path)


def test_rptor_pending_only_shows_ready(tmp_path):
    """mtor rptor --pending shows only dispatchable specs."""
    (tmp_path / "alpha.md").write_text("---\nstatus: done\n---\n", encoding="utf-8")
    (tmp_path / "beta.md").write_text(
        "---\nstatus: ready\ndepends_on:\n  - alpha\n---\n",
        encoding="utf-8",
    )

    exit_code, data = invoke(["rptor", "--pending", "--dir", str(tmp_path)])

    assert exit_code == 0
    assert "ready" in data["result"]["specs"]
    assert data["result"]["counts"]["ready"] == 1


def test_rptor_pending_excludes_non_ready_statuses(tmp_path):
    """mtor rptor --pending excludes draft/failed/open specs."""
    (tmp_path / "ready.md").write_text("---\nstatus: ready\n---\n", encoding="utf-8")
    (tmp_path / "draft.md").write_text("---\nstatus: draft\n---\n", encoding="utf-8")
    (tmp_path / "failed.md").write_text("---\nstatus: failed\n---\n", encoding="utf-8")
    (tmp_path / "open.md").write_text("---\nstatus: open\n---\n", encoding="utf-8")

    exit_code, data = invoke(["rptor", "--pending", "--dir", str(tmp_path)])

    assert exit_code == 0
    assert data["result"]["counts"]["ready"] == 1
    assert [spec["name"] for spec in data["result"]["specs"]["ready"]] == ["ready"]


def test_rptor_empty_directory(tmp_path):
    """mtor rptor on empty directory returns empty lists with zero counts."""
    exit_code, data = invoke(["rptor", "--dir", str(tmp_path)])

    assert exit_code == 0
    assert data["result"]["specs"] == []
    assert data["result"]["counts"]["ready"] == 0


def test_rptor_audit_command_reports_issues(tmp_path):
    """mtor rptor --audit returns lifecycle quality findings."""
    (tmp_path / "weak.md").write_text("---\nstatus: done\n---\n", encoding="utf-8")
    (tmp_path / "invalid.md").write_text("---\nstatus: open\n---\n", encoding="utf-8")

    exit_code, data = invoke(["rptor", "--audit", "--dir", str(tmp_path)])

    assert exit_code == 0
    assert data["ok"] is True
    assert data["result"]["ok"] is False
    assert data["result"]["counts"]["issues"]["done_without_evidence"] == 1
    assert data["result"]["counts"]["issues"]["invalid_status"] == 1
    assert data["result"]["directory"] == str(tmp_path)


def test_rptor_audit_strict_exits_for_errors(tmp_path):
    """mtor rptor --audit --strict can be used as a hard gate."""
    (tmp_path / "weak.md").write_text("---\nstatus: done\n---\n", encoding="utf-8")

    exit_code, data = invoke(["rptor", "--audit", "--strict", "--dir", str(tmp_path)])

    assert exit_code == 1
    assert data["ok"] is False
    assert data["error"]["code"] == "SPEC_AUDIT_FAILED"


def test_rptor_circular_dependency_reported(tmp_path):
    """mtor rptor exits with error when circular dependency is found."""
    (tmp_path / "x.md").write_text("---\nstatus: ready\ndepends_on:\n  - y\n---\n", encoding="utf-8")
    (tmp_path / "y.md").write_text("---\nstatus: ready\ndepends_on:\n  - x\n---\n", encoding="utf-8")

    exit_code, data = invoke(["rptor", "--dir", str(tmp_path)])

    assert exit_code == 1
    assert data["ok"] is False
    assert data["error"]["code"] == "CIRCULAR_DEPENDENCY"
    assert "Circular dependency" in data["error"]["message"]


def test_plan_hidden_alias_still_runs(tmp_path):
    """mtor plan remains a transition alias for mtor rptor."""
    (tmp_path / "alpha.md").write_text("---\nstatus: ready\n---\n", encoding="utf-8")

    exit_code, data = invoke(["plan", "--dir", str(tmp_path)])

    assert exit_code == 0
    assert data["command"] == "mtor rptor"
    assert data["result"]["counts"]["ready"] == 1


# ---------------------------------------------------------------------------
# CLI: rptor done command
# ---------------------------------------------------------------------------


def test_rptor_done_updates_status(tmp_path):
    """mtor rptor done <name> updates spec status to done."""
    spec_file = tmp_path / "my-spec.md"
    spec_file.write_text(
        "---\nstatus: ready\nrepo: ~\n---\n## Problem\n\nTest.\n",
        encoding="utf-8",
    )

    exit_code, data = invoke(["rptor_done", "my-spec", "--dir", str(tmp_path)])

    assert exit_code == 0
    assert data["ok"] is True
    assert data["result"]["name"] == "my-spec"
    assert data["result"]["status"] == "done"

    # Verify the file was actually updated
    text = spec_file.read_text(encoding="utf-8")
    assert "status: done" in text
    assert "completed_at:" in text


def test_rptor_done_missing_spec(tmp_path):
    """mtor rptor done for missing spec returns SPEC_NOT_FOUND error."""
    exit_code, data = invoke(["rptor_done", "nonexistent", "--dir", str(tmp_path)])

    assert exit_code == 1
    assert data["ok"] is False
    assert data["error"]["code"] == "SPEC_NOT_FOUND"


def test_plan_done_hidden_alias_still_runs(tmp_path):
    """mtor plan_done remains a transition alias for mtor rptor_done."""
    spec_file = tmp_path / "my-spec.md"
    spec_file.write_text("---\nstatus: ready\nrepo: ~\n---\n", encoding="utf-8")

    exit_code, data = invoke(["plan_done", "my-spec", "--dir", str(tmp_path)])

    assert exit_code == 0
    assert data["command"] == "mtor rptor done my-spec"
    assert "status: done" in spec_file.read_text(encoding="utf-8")


# ---------------------------------------------------------------------------
# Dispatch: scope/test injection
# ---------------------------------------------------------------------------


def test_scope_injected_into_prompt(tmp_path):
    """_inject_spec_constraints appends scope CONSTRAINT to prompt."""
    spec_file = tmp_path / "scope-spec.md"
    spec_file.write_text(
        "---\nstatus: ready\nscope:\n  - mtor/rptor.py\n  - mtor/cli.py\nexclude:\n  - genome.md\n---\n",
        encoding="utf-8",
    )

    result = _inject_spec_constraints(
        "Write the plan module.",
        spec_path=spec_file,
        prompt_for_cmd="Write the plan module.",
    )

    assert "CONSTRAINT: Only modify mtor/rptor.py, mtor/cli.py." in result
    assert "Do NOT modify: genome.md." in result


def test_tests_injected_into_prompt(tmp_path):
    """_inject_spec_constraints appends test run + functions to prompt."""
    spec_file = tmp_path / "test-spec.md"
    spec_file.write_text(
        "---\nstatus: ready\ntests:\n  run: cd ~/code/mtor && python -m pytest assays/test_rptor.py -v\n  functions:\n    - parse_spec\n    - scan_specs\n---\n",
        encoding="utf-8",
    )

    result = _inject_spec_constraints(
        "Build the feature.",
        spec_path=spec_file,
        prompt_for_cmd="Build the feature.",
    )

    assert "Run: cd ~/code/mtor && python -m pytest assays/test_rptor.py -v" in result
    assert "Verify test functions:" in result
    assert "test_parse_spec" in result
    assert "test_scan_specs" in result


def test_spec_injection_repo_context(tmp_path):
    """_inject_spec_constraints adds worktree-safe repo context when non-default."""
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

    assert "Working directory:" not in result
    assert "Canonical repository: /home/vivesca/code/mtor" in result
    assert "isolated git worktree" in result
    assert "do not cd to /home/vivesca/code/mtor" in result


def test_spec_injection_preserves_home_relative_worker_repo(tmp_path):
    """Home-relative spec repos stay portable in worker prompts."""
    spec_file = tmp_path / "repo-spec.md"
    spec_file.write_text(
        "---\nstatus: ready\nrepo: ~/code/mtor\n---\n",
        encoding="utf-8",
    )

    result = _inject_spec_constraints(
        "Implement feature.",
        spec_path=spec_file,
        prompt_for_cmd="Implement feature.",
    )

    assert "Working directory:" not in result
    assert "Canonical repository: ~/code/mtor" in result
    assert "isolated git worktree" in result
    assert str(Path.home() / "code/mtor") not in result


def test_spec_injection_no_spec_path_unchanged():
    """_inject_spec_constraints returns base prompt unchanged when spec_path is None."""
    result = _inject_spec_constraints(
        "Original prompt.",
        spec_path=None,
        prompt_for_cmd="Original prompt.",
    )
    assert result == "Original prompt."


def test_spec_injection_strips_cd_tilde_prefix(tmp_path):
    """cd <repo> && prefix is stripped when repo matches tests.run."""
    spec_file = tmp_path / "cd-spec.md"
    spec_file.write_text(
        "---\nstatus: ready\nrepo: ~/code/mtor\n"
        "tests:\n  run: cd ~/code/mtor && uv run pytest assays/test_rptor.py -q\n---\n",
        encoding="utf-8",
    )

    result = _inject_spec_constraints(
        "Build the feature.",
        spec_path=spec_file,
        prompt_for_cmd="Build the feature.",
    )

    assert "Run: uv run pytest assays/test_rptor.py -q" in result
    assert "cd ~/code/mtor" not in result


def test_spec_injection_strips_cd_absolute_prefix(tmp_path):
    """cd /home/vivesca/... && prefix is stripped for tilde-form repo."""
    spec_file = tmp_path / "cd-abs-spec.md"
    spec_file.write_text(
        "---\nstatus: ready\nrepo: ~/code/mtor\n"
        "tests:\n  run: cd /home/vivesca/code/mtor && uv run pytest assays/test_rptor.py -q\n---\n",
        encoding="utf-8",
    )

    result = _inject_spec_constraints(
        "Build the feature.",
        spec_path=spec_file,
        prompt_for_cmd="Build the feature.",
    )

    assert "Run: uv run pytest assays/test_rptor.py -q" in result
    assert "cd /home/vivesca" not in result


def test_spec_injection_strips_cd_semicolon_prefix(tmp_path):
    """cd <repo>; prefix is also stripped."""
    spec_file = tmp_path / "cd-semi-spec.md"
    spec_file.write_text(
        "---\nstatus: ready\nrepo: ~/code/mtor\n"
        "tests:\n  run: cd ~/code/mtor; uv run pytest assays/test_rptor.py -q\n---\n",
        encoding="utf-8",
    )

    result = _inject_spec_constraints(
        "Build the feature.",
        spec_path=spec_file,
        prompt_for_cmd="Build the feature.",
    )

    assert "Run: uv run pytest assays/test_rptor.py -q" in result
    assert "cd ~/code/mtor" not in result


def test_spec_injection_strips_cd_quoted_prefix(tmp_path):
    """cd with single/double-quoted repo path is stripped."""
    spec_file = tmp_path / "cd-quoted-spec.md"
    spec_file.write_text(
        "---\nstatus: ready\nrepo: ~/code/mtor\n"
        "tests:\n  run: cd '~/code/mtor' && uv run pytest assays/test_rptor.py -q\n---\n",
        encoding="utf-8",
    )

    result = _inject_spec_constraints(
        "Build the feature.",
        spec_path=spec_file,
        prompt_for_cmd="Build the feature.",
    )

    assert "Run: uv run pytest assays/test_rptor.py -q" in result
    assert "Run: cd " not in result


def test_spec_injection_strips_cd_tilde_prefix_from_abs_repo(tmp_path):
    """cd ~/code/mtor && prefix is stripped when repo is absolute /home/vivesca/... form."""
    spec_file = tmp_path / "cd-abs-repo-spec.md"
    spec_file.write_text(
        "---\nstatus: ready\nrepo: /home/vivesca/code/mtor\n"
        "tests:\n  run: cd ~/code/mtor && uv run pytest assays/test_rptor.py -q\n---\n",
        encoding="utf-8",
    )

    result = _inject_spec_constraints(
        "Build the feature.",
        spec_path=spec_file,
        prompt_for_cmd="Build the feature.",
    )

    assert "Run: uv run pytest assays/test_rptor.py -q" in result
    assert "cd ~/code/mtor" not in result


def test_spec_injection_preserves_cd_when_repo_mismatched(tmp_path):
    """cd prefix is NOT stripped when repo doesn't match tests.run path."""
    spec_file = tmp_path / "cd-mismatch-spec.md"
    spec_file.write_text(
        "---\nstatus: ready\nrepo: ~/code/other\n"
        "tests:\n  run: cd ~/code/mtor && uv run pytest assays/test_rptor.py -q\n---\n",
        encoding="utf-8",
    )

    result = _inject_spec_constraints(
        "Build the feature.",
        spec_path=spec_file,
        prompt_for_cmd="Build the feature.",
    )

    assert "Run: cd ~/code/mtor && uv run pytest assays/test_rptor.py -q" in result


# ---------------------------------------------------------------------------
# CLI: dispatch-all
# ---------------------------------------------------------------------------


def test_dispatch_all_dry_run_reports_skipped_statuses(tmp_path):
    """dispatch-all --dry-run reports skipped entries for done, stale, dispatched, and audit-not-outstanding specs."""
    (tmp_path / "done-spec.md").write_text(
        "---\nstatus: done\ncompleted_at: 2026-05-15T00:00:00+00:00\n---\nDone.\n",
        encoding="utf-8",
    )
    (tmp_path / "stale-spec.md").write_text(
        "---\nstatus: stale\naudit_reason: abandoned\n---\nStale.\n",
        encoding="utf-8",
    )
    (tmp_path / "dispatched-spec.md").write_text(
        "---\nstatus: dispatched\n---\nDispatched.\n",
        encoding="utf-8",
    )
    (tmp_path / "audit-done.md").write_text(
        "---\nstatus: ready\naudit_status: audited_present\ntests:\n  run: echo ok\n---\nAudited.\n",
        encoding="utf-8",
    )
    (tmp_path / "valid-ready.md").write_text(
        "---\nstatus: ready\ntests:\n  run: echo ok\n---\nDo something.\n",
        encoding="utf-8",
    )

    exit_code, data = invoke(["dispatch-all", "--dry-run", "--dir", str(tmp_path)])

    assert exit_code == 0
    skipped = data["result"]["skipped"]
    skipped_map = {s["name"]: s["reason"] for s in skipped}

    assert skipped_map["done-spec"] == "status:done"
    assert skipped_map["stale-spec"] == "status:stale"
    assert skipped_map["dispatched-spec"] == "status:dispatched"
    assert skipped_map["audit-done"] == "audit:audited_present"

    dispatched_names = [d["name"] for d in data["result"]["dispatched"]]
    assert "valid-ready" in dispatched_names


def test_dispatch_all_dry_run_limit_caps_candidates(tmp_path):
    """dispatch-all --dry-run --limit 1 returns exactly one would_dispatch when multiple valid candidates exist."""
    for i in range(3):
        (tmp_path / f"spec-{i}.md").write_text(
            f"---\nstatus: ready\ntests:\n  run: echo ok\n---\nTask {i}.\n",
            encoding="utf-8",
        )

    exit_code, data = invoke(["dispatch-all", "--dry-run", "--limit", "1", "--dir", str(tmp_path)])

    assert exit_code == 0
    assert len(data["result"]["dispatched"]) == 1
    assert data["result"]["dispatched"][0]["status"] == "would_dispatch"


def test_dispatch_all_skips_invalid_ready_specs(tmp_path):
    """dispatch-all skips ready specs missing tests field with an invalid: reason."""
    (tmp_path / "no-tests.md").write_text(
        "---\nstatus: ready\n---\nNo tests field.\n",
        encoding="utf-8",
    )

    exit_code, data = invoke(["dispatch-all", "--dry-run", "--dir", str(tmp_path)])

    assert exit_code == 0
    skipped = data["result"]["skipped"]
    assert len(skipped) == 1
    assert skipped[0]["name"] == "no-tests"
    assert skipped[0]["reason"].startswith("invalid:")
    assert data["result"]["dispatched"] == []
