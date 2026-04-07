"""Tests for mtor scan command — organism gap detection.

The scan command runs deterministic checks on the codebase and returns
findings as a list of dicts in a porin envelope.
"""

from __future__ import annotations

import io
import json
import os
import sys

from mtor.cli import app

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


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
    except json.JSONDecodeError as exc:
        raise AssertionError(
            f"Output is not valid JSON. Exit={exit_code}\nOutput: {output!r}\nException: {exc}"
        ) from exc
    return exit_code, data


# ---------------------------------------------------------------------------
# VALID_CATEGORIES tests
# ---------------------------------------------------------------------------


class TestValidCategories:
    def test_categories_exist(self):
        from mtor.scan import VALID_CATEGORIES

        assert isinstance(VALID_CATEGORIES, tuple)

    def test_categories_contain_expected(self):
        from mtor.scan import VALID_CATEGORIES

        for cat in ("hygiene", "coverage", "maintenance"):
            assert cat in VALID_CATEGORIES, f"Missing category: {cat}"


# ---------------------------------------------------------------------------
# _run_checks tests
# ---------------------------------------------------------------------------


class TestRunChecks:
    def test_returns_list_of_dicts(self, tmp_path):
        from mtor.scan import _run_checks

        results = _run_checks(effectors_dir=tmp_path, marks_dir=tmp_path)
        assert isinstance(results, list)
        for r in results:
            assert isinstance(r, dict)

    def test_each_finding_has_required_fields(self, tmp_path):
        from mtor.scan import _run_checks

        results = _run_checks(effectors_dir=tmp_path, marks_dir=tmp_path)
        required = {"description", "category", "priority", "target"}
        for r in results:
            assert required.issubset(r.keys()), f"Missing keys: {required - r.keys()}"

    def test_categories_are_valid(self, tmp_path):
        from mtor.scan import VALID_CATEGORIES, _run_checks

        results = _run_checks(effectors_dir=tmp_path, marks_dir=tmp_path)
        for r in results:
            assert r["category"] in VALID_CATEGORIES, f"Invalid category: {r['category']}"

    def test_priorities_are_valid(self, tmp_path):
        from mtor.scan import _run_checks

        results = _run_checks(effectors_dir=tmp_path, marks_dir=tmp_path)
        valid_priorities = {"low", "medium", "high"}
        for r in results:
            assert r["priority"] in valid_priorities, f"Invalid priority: {r['priority']}"

    def test_finds_todo_fixme(self, tmp_path):
        from mtor.scan import _run_checks

        # Create a fake effector with TODO
        eff = tmp_path / "myeffector"
        eff.mkdir()
        (eff / "main.py").write_text("# TODO: fix this later\nprint('hello')\n")

        results = _run_checks(effectors_dir=tmp_path, marks_dir=tmp_path)
        todo_findings = [r for r in results if r["category"] == "hygiene"]
        assert len(todo_findings) > 0
        assert any(
            "TODO" in r["description"] or "FIXME" in r["description"] for r in todo_findings
        )

    def test_finds_effectors_without_assays(self, tmp_path):
        from mtor.scan import _run_checks

        # Create a fake effector WITHOUT assays dir
        eff = tmp_path / "bare-effector"
        eff.mkdir()
        (eff / "run.py").write_text("print('hello')")

        results = _run_checks(effectors_dir=tmp_path, marks_dir=tmp_path)
        coverage_findings = [r for r in results if r["category"] == "coverage"]
        assert len(coverage_findings) > 0
        assert any("bare-effector" in r["target"] for r in coverage_findings)

    def test_effector_with_assays_not_flagged(self, tmp_path):
        from mtor.scan import _run_checks

        # Create a fake effector WITH assays dir
        eff = tmp_path / "tested-effector"
        eff.mkdir()
        assays = eff / "assays"
        assays.mkdir()
        (assays / "test_main.py").write_text("def test_ok(): pass")

        results = _run_checks(effectors_dir=tmp_path, marks_dir=tmp_path)
        coverage_findings = [r for r in results if r["category"] == "coverage"]
        assert not any("tested-effector" in r["target"] for r in coverage_findings)

    def test_finds_stale_marks(self, tmp_path):
        from mtor.scan import _run_checks

        marks = tmp_path / "marks"
        marks.mkdir()
        # Create a stale mark file (>30 days old mtime)
        stale = marks / "old-mark.json"
        stale.write_text('{"status": "active"}')
        # Set mtime to 60 days ago
        import time

        old_time = time.time() - 60 * 86400
        os.utime(stale, (old_time, old_time))

        results = _run_checks(effectors_dir=tmp_path, marks_dir=marks)
        maint_findings = [r for r in results if r["category"] == "maintenance"]
        assert len(maint_findings) > 0
        assert any(
            "old-mark" in r["target"] or "stale" in r["description"].lower()
            for r in maint_findings
        )

    def test_recent_mark_not_flagged(self, tmp_path):
        from mtor.scan import _run_checks

        marks = tmp_path / "marks"
        marks.mkdir()
        fresh = marks / "fresh-mark.json"
        fresh.write_text('{"status": "active"}')

        results = _run_checks(effectors_dir=tmp_path, marks_dir=marks)
        maint_findings = [r for r in results if r["category"] == "maintenance"]
        assert not any("fresh-mark" in r["target"] for r in maint_findings)

    def test_excludes_venv_dirs(self, tmp_path):
        from mtor.scan import _run_checks

        venv = tmp_path / ".venv"
        venv.mkdir()
        (venv / "lib.py").write_text("# TODO: bad\n")

        results = _run_checks(effectors_dir=tmp_path, marks_dir=tmp_path)
        hygiene_findings = [r for r in results if r["category"] == "hygiene"]
        assert not any(".venv" in r.get("target", "") for r in hygiene_findings)


# ---------------------------------------------------------------------------
# scan CLI subcommand tests
# ---------------------------------------------------------------------------


class TestScanCLI:
    def test_scan_returns_ok_envelope(self, tmp_path, monkeypatch):
        import mtor.scan as _scan

        monkeypatch.setattr(_scan, "REPO_DIR", str(tmp_path))
        exit_code, data = invoke(["scan"])
        assert exit_code == 0
        assert data["ok"] is True

    def test_scan_has_findings_key(self, tmp_path, monkeypatch):
        import mtor.scan as _scan

        monkeypatch.setattr(_scan, "REPO_DIR", str(tmp_path))
        _, data = invoke(["scan"])
        assert "findings" in data["result"]

    def test_scan_findings_is_list(self, tmp_path, monkeypatch):
        import mtor.scan as _scan

        monkeypatch.setattr(_scan, "REPO_DIR", str(tmp_path))
        _, data = invoke(["scan"])
        assert isinstance(data["result"]["findings"], list)

    def test_scan_has_next_actions(self, tmp_path, monkeypatch):
        import mtor.scan as _scan

        monkeypatch.setattr(_scan, "REPO_DIR", str(tmp_path))
        _, data = invoke(["scan"])
        assert "next_actions" in data

    def test_scan_command_field(self, tmp_path, monkeypatch):
        import mtor.scan as _scan

        monkeypatch.setattr(_scan, "REPO_DIR", str(tmp_path))
        _, data = invoke(["scan"])
        assert data["command"] == "mtor scan"
