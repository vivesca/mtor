"""Regression tests for per-attempt ribosome JSONL summaries."""

from __future__ import annotations

import json
from types import SimpleNamespace

from mtor.worker import translocase


def test_write_attempt_summary_appends_one_line(tmp_path, monkeypatch):
    monkeypatch.setattr(translocase, "LOG_DIR", tmp_path)

    def fake_run(cmd, **kwargs):
        assert cmd == ["git", "diff", "--numstat", "main..HEAD"]
        assert kwargs["cwd"] == tmp_path / "worktree"
        return SimpleNamespace(returncode=0, stdout="3\t1\tmtor/foo.py\n-\t-\tasset.png\n")

    monkeypatch.setattr(translocase._subprocess, "run", fake_run)

    translocase._write_attempt_summary(
        workflow_id="ribosome-test",
        provider="zhipu",
        exit_code=0,
        duration_seconds=12.345,
        stdout_bytes=42,
        stderr_bytes=7,
        work_dir=tmp_path / "worktree",
    )

    lines = (tmp_path / "ribosome-test.jsonl").read_text().splitlines()
    assert len(lines) == 1
    row = json.loads(lines[0])
    assert row["workflow_id"] == "ribosome-test"
    assert row["provider"] == "zhipu"
    assert row["exit_code"] == 0
    assert row["duration_seconds"] == 12.35
    assert row["diff_stat"] == {"added": 3, "removed": 1}
    assert row["stdout_bytes"] == 42
    assert row["stderr_bytes"] == 7


def test_write_attempt_summary_survives_diff_probe_failure(tmp_path, monkeypatch):
    monkeypatch.setattr(translocase, "LOG_DIR", tmp_path)

    def failing_run(cmd, **kwargs):
        raise OSError("git unavailable")

    monkeypatch.setattr(translocase._subprocess, "run", failing_run)

    translocase._write_attempt_summary(
        workflow_id="ribosome-failed-probe",
        provider="codex",
        exit_code=1,
        duration_seconds=1.0,
        stdout_bytes=0,
        stderr_bytes=12,
        work_dir=tmp_path / "worktree",
    )

    row = json.loads((tmp_path / "ribosome-failed-probe.jsonl").read_text())
    assert row["diff_stat"] == {"added": 0, "removed": 0}

