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


def test_spec_gate_warns_on_out_of_scope_path(tmp_path):
    decoy = "assays/legacy_thing.py"
    spec_file = _write_spec(
        tmp_path,
        body=f"Do not modify {decoy} under any circumstances.",
    )

    warnings = spec_gate_path_warnings(spec_file)

    assert len(warnings) == 1
    assert decoy in warnings[0]
    assert "target_file_missing" in warnings[0]


def test_spec_gate_silent_when_paths_in_scope(tmp_path):
    spec_file = _write_spec(
        tmp_path,
        body="Modify mtor/dispatch.py to add the new helper.",
    )

    warnings = spec_gate_path_warnings(spec_file)

    assert warnings == []


def test_spec_gate_warning_does_not_block_validate_spec(tmp_path, capsys):
    decoy = "assays/legacy_thing.py"
    spec_file = _write_spec(
        tmp_path,
        body=f"Do not modify {decoy} under any circumstances.",
    )

    errors = validate_spec(spec_file, tmp_path)
    captured = capsys.readouterr()

    assert errors == []
    assert "WARNING:" in captured.err
