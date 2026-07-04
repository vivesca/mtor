"""Regression tests: scout/research run reports must persist on every return path."""

from __future__ import annotations

from pathlib import Path

from mtor.worker import translocase
from mtor.worker.translocase import _persist_output_file
from mtor.worker.workflow import _summarize_workflow_result


def test_persist_output_file_writes_report(tmp_path: Path, monkeypatch) -> None:
    out_dir = tmp_path / "outputs"
    monkeypatch.setattr(translocase, "OUTPUT_DIR", out_dir)

    path = _persist_output_file("some task", "zhipu", 0, "REPORT BODY", "")

    assert path
    assert Path(path).exists()
    assert "REPORT BODY" in Path(path).read_text()


def test_persist_output_file_appends_extra(tmp_path: Path, monkeypatch) -> None:
    out_dir = tmp_path / "outputs"
    monkeypatch.setattr(translocase, "OUTPUT_DIR", out_dir)

    path = _persist_output_file("some task", "zhipu", 0, "REPORT BODY", "", extra="EXTRA SECTION")

    text = Path(path).read_text()
    assert "EXTRA SECTION" in text
    assert text.endswith("EXTRA SECTION")


def test_workflow_summary_includes_output_path() -> None:
    summary = _summarize_workflow_result(
        {
            "task": "t",
            "provider": "zhipu",
            "success": True,
            "exit_code": 0,
            "output_path": "/x/y.txt",
            "review": {},
        }
    )

    assert summary["output_path"] == "/x/y.txt"
