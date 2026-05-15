"""Assays for structured mtor task lifecycle logging.

Spec: ~/epigenome/chromatin/loci/plans/spec-task-lifecycle-tracking.md
"""
from __future__ import annotations

import json

from mtor.worker import translocase


def test_log_event_creates_file(tmp_path, monkeypatch):
    """First event creates the JSONL file."""
    monkeypatch.setattr(translocase, "LOG_DIR", tmp_path)

    translocase._log_event("wf-create", "dispatch", task="hello world", mode="build")

    log_file = tmp_path / "wf-create.jsonl"
    assert log_file.exists()

    lines = log_file.read_text().strip().splitlines()
    assert len(lines) == 1
    entry = json.loads(lines[0])
    assert entry["type"] == "dispatch"
    assert entry["workflow_id"] == "wf-create"
    assert entry["task"] == "hello world"
    assert entry["mode"] == "build"


def test_log_event_appends(tmp_path, monkeypatch):
    """Multiple events append as separate lines."""
    monkeypatch.setattr(translocase, "LOG_DIR", tmp_path)

    translocase._log_event("wf-append", "dispatch", task="test", mode="build")
    translocase._log_event("wf-append", "provider_selected", provider="zhipu", attempt=1)
    translocase._log_event("wf-append", "subprocess_started", pid=99999)
    translocase._log_event("wf-append", "subprocess_exited", exit_code=0, duration_seconds=12.0)

    log_file = tmp_path / "wf-append.jsonl"
    lines = log_file.read_text().strip().splitlines()
    assert len(lines) == 4

    types = [json.loads(line)["type"] for line in lines]
    assert types == [
        "dispatch",
        "provider_selected",
        "subprocess_started",
        "subprocess_exited",
    ]


def test_log_event_has_required_fields(tmp_path, monkeypatch):
    """Every event has type, workflow_id, timestamp."""
    monkeypatch.setattr(translocase, "LOG_DIR", tmp_path)

    translocase._log_event("wf-fields", "verdict", approved=True, flags=[])

    entry = json.loads((tmp_path / "wf-fields.jsonl").read_text().strip())
    assert entry["type"] == "verdict"
    assert entry["workflow_id"] == "wf-fields"
    assert "timestamp" in entry
    assert len(entry["timestamp"]) == 19  # YYYY-MM-DDTHH:MM:SS
    assert entry["approved"] is True
    assert entry["flags"] == []


def test_full_lifecycle_sequence(tmp_path, monkeypatch):
    """Simulate a full lifecycle and verify events in the JSONL log."""
    monkeypatch.setattr(translocase, "LOG_DIR", tmp_path)

    wf_id = "wf-lifecycle-full"

    translocase._log_event(wf_id, "dispatch", task="do something", mode="build")
    translocase._log_event(wf_id, "provider_selected", provider="zhipu", attempt=1)
    translocase._log_event(wf_id, "subprocess_started", pid=12345)
    translocase._log_event(wf_id, "subprocess_exited", exit_code=0, duration_seconds=42.5)
    translocase._log_event(wf_id, "verdict", approved=True, flags=[])

    log_file = tmp_path / f"{wf_id}.jsonl"
    assert log_file.exists()

    events = [json.loads(line) for line in log_file.read_text().strip().splitlines()]
    assert len(events) == 5

    event_types = [e["type"] for e in events]
    assert event_types == [
        "dispatch",
        "provider_selected",
        "subprocess_started",
        "subprocess_exited",
        "verdict",
    ]

    for event in events:
        assert event["workflow_id"] == wf_id
        assert "timestamp" in event

    assert events[0]["task"] == "do something"
    assert events[1]["provider"] == "zhipu"
    assert events[2]["pid"] == 12345
    assert events[3]["exit_code"] == 0
    assert events[3]["duration_seconds"] == 42.5
    assert events[4]["approved"] is True
