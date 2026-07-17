from __future__ import annotations

from pathlib import Path

from mtor.dispatch import spec_gate_path_warnings, validate_spec


def _write_spec(tmp_path: Path, *, body: str) -> Path:
    spec = tmp_path / "spec.md"
    spec.write_text(
        "---\n"
        "status: ready\n"
        f"repo: {tmp_path}\n"
        "scope:\n"
        "  - mtor\n"
        "tests:\n"
        "  run: uv run pytest -k x\n"
        "---\n"
        f"{body}\n",
        encoding="utf-8",
    )
    return spec


def test_spec_gate_ignores_negated_out_of_scope_path(tmp_path):
    decoy = "assays/legacy_thing.py"
    spec_file = _write_spec(
        tmp_path,
        body=f"Do not modify {decoy} under any circumstances.",
    )

    warnings = spec_gate_path_warnings(spec_file)

    assert warnings == []


def test_spec_gate_warns_on_positive_out_of_scope_mutation(tmp_path):
    decoy = "assays/legacy_thing.py"
    spec_file = _write_spec(
        tmp_path,
        body=f"Modify {decoy} to cover the regression.",
    )

    warnings = spec_gate_path_warnings(spec_file)

    assert len(warnings) == 1
    assert decoy in warnings[0]
    assert "outside scope" in warnings[0]


def test_spec_gate_silent_when_paths_in_scope(tmp_path):
    spec_file = _write_spec(
        tmp_path,
        body="Modify mtor/dispatch.py to add the new helper.",
    )

    warnings = spec_gate_path_warnings(spec_file)

    assert warnings == []


def test_spec_gate_ignores_test_run_path(tmp_path, capsys):
    decoy = "assays/test_legacy_thing.py"
    spec_file = tmp_path / "spec.md"
    spec_file.write_text(
        "---\n"
        "status: ready\n"
        f"repo: {tmp_path}\n"
        "scope:\n"
        "  - mtor\n"
        "tests:\n"
        f"  run: uv run pytest {decoy}\n"
        "---\n"
        "Modify mtor/dispatch.py to add the new helper.\n",
        encoding="utf-8",
    )

    errors = validate_spec(spec_file, tmp_path)
    captured = capsys.readouterr()

    assert errors == [f"Test file not found: {decoy}"]
    assert "WARNING:" not in captured.err
