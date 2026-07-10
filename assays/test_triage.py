"""Unit tests for mtor/triage.py — review/archive lifecycle for workflows."""

from __future__ import annotations

import json
import multiprocessing
from datetime import UTC, datetime, timedelta
from pathlib import Path
from unittest.mock import patch

import pytest

from mtor.triage import (
    _locked_mutate,
    archive_ids,
    get_triage_sets,
    get_verdict_overrides,
    load_triage,
    normalize_archived,
    override_verdict,
    parse_duration,
    review_ids,
    save_triage,
)


def _patch_triage_path(tmp_path: Path):
    """Return a patcher that redirects TRIAGE_PATH to tmp_path."""
    return patch("mtor.triage.TRIAGE_PATH", tmp_path / "triage.json")


def test_review_ids_adds_and_skips_archived(tmp_path: Path) -> None:
    """review_ids adds IDs to reviewed, is idempotent, and skips already-archived."""
    with _patch_triage_path(tmp_path):
        # Pre-seed an archived ID
        save_triage(
            {"reviewed": [], "archived": ["wf-archived"], "verdict_overrides": {}}
        )

        result = review_ids(["wf-1", "wf-2", "wf-archived", "wf-1"])
        assert "wf-1" in result["reviewed"]
        assert "wf-2" in result["reviewed"]
        assert "wf-archived" not in result["reviewed"]
        assert result["count"] == 2  # idempotent wf-1 counted once


def test_archive_ids_moves_from_reviewed(tmp_path: Path) -> None:
    """archive_ids moves IDs to archived and removes them from reviewed."""
    with _patch_triage_path(tmp_path):
        save_triage(
            {"reviewed": ["wf-1", "wf-2"], "archived": [], "verdict_overrides": {}}
        )

        result = archive_ids(["wf-1"], reason="reviewed diff")
        assert "wf-1" in result["archived"]
        assert result["count"] == 1

        reviewed, archived = get_triage_sets()
        assert "wf-1" not in reviewed
        assert "wf-2" in reviewed
        assert "wf-1" in archived


def test_archive_ids_stores_reason_records(tmp_path: Path) -> None:
    """archive_ids stores audit metadata for newly archived workflows."""
    with _patch_triage_path(tmp_path):
        result = archive_ids(["wf-1"], reason="reviewed diff")

        assert result["archived"] == ["wf-1"]
        data = load_triage()
        assert data["archived"] == [
            {
                "workflow_id": "wf-1",
                "reason": "reviewed diff",
                "archived_at": data["archived"][0]["archived_at"],
            }
        ]
        assert data["archived"][0]["archived_at"] is not None


def test_load_triage_normalizes_legacy_archived_strings(tmp_path: Path) -> None:
    """Legacy archived string IDs remain readable as archived records."""
    with _patch_triage_path(tmp_path):
        (tmp_path / "triage.json").write_text(
            '{"reviewed": [], "archived": ["wf-old"], "verdict_overrides": {}, "updated": null}\n'
        )

        data = load_triage()
        assert data["archived"] == [
            {"workflow_id": "wf-old", "reason": "legacy", "archived_at": None}
        ]
        _, archived = get_triage_sets()
        assert archived == {"wf-old"}


def test_override_verdict_roundtrip(tmp_path: Path) -> None:
    """override_verdict stores overrides and get_verdict_overrides retrieves them."""
    with _patch_triage_path(tmp_path):
        r = override_verdict(["wf-a", "wf-b"], "approved")
        assert r["overridden"] == 2
        assert r["verdict"] == "approved"

        overrides = get_verdict_overrides()
        assert overrides["wf-a"] == "approved"
        assert overrides["wf-b"] == "approved"


def test_archive_ids_reports_only_newly_archived_ids(tmp_path: Path) -> None:
    """archive_ids returns only IDs archived by this call, not the full historical set."""
    with _patch_triage_path(tmp_path):
        # Pre-seed two archived IDs
        save_triage(
            {
                "reviewed": [],
                "archived": ["wf-old-1", "wf-old-2"],
                "verdict_overrides": {},
            }
        )

        # Archive a brand-new ID
        result = archive_ids(["wf-new"], reason="cleanup")
        assert result["archived"] == ["wf-new"]
        assert result["count"] == 1
        assert result["archived_total"] == 3  # 2 pre-existing + 1 new
        assert len(result["archived_records"]) == 1
        assert result["archived_records"][0]["workflow_id"] == "wf-new"
        assert result["archived_records"][0]["reason"] == "cleanup"

        # Verify the pre-existing records are persisted but NOT in archived list
        data = load_triage()
        persisted_ids = [r["workflow_id"] for r in data["archived"]]
        assert "wf-old-1" in persisted_ids
        assert "wf-old-2" in persisted_ids
        assert "wf-new" in persisted_ids

        # Idempotent re-archive returns empty
        result2 = archive_ids(["wf-new"], reason="duplicate")
        assert result2["archived"] == []
        assert result2["count"] == 0
        assert result2["archived_total"] == 3


def test_parse_duration_valid_and_invalid() -> None:
    """parse_duration handles h/d/m units and rejects bad input."""
    assert parse_duration("7d") == timedelta(days=7)
    assert parse_duration("24h") == timedelta(hours=24)
    assert parse_duration("30m") == timedelta(minutes=30)

    with pytest.raises(ValueError, match="Invalid duration"):
        parse_duration("bad")

    with pytest.raises(ValueError, match="Invalid duration"):
        parse_duration("5w")


# ---------------------------------------------------------------------------
# Concurrency + atomicity regressions for the triage ledger.
#
# Background: on 2026-07-04, parallel `mtor archive` invocations each loaded
# the same stale triage.json, mutated in memory, and wrote the whole file
# back. The last writer won and earlier batches were lost from the ledger.
# These tests guard the locked load-mutate-save and the atomic save path.
# ---------------------------------------------------------------------------


def _child_archive(tmp_path_str: str, workflow_id: str, reason: str) -> None:
    """Child-process target: redirect TRIAGE_PATH into tmp and archive one ID.

    Runs in a forked child so it inherits nothing about the parent's mock
    state except the explicit path assignment. Using a real process exercises
    the real ``fcntl.flock`` interprocess lock.
    """
    import mtor.triage as triage

    triage.TRIAGE_PATH = Path(tmp_path_str) / "triage.json"
    triage.archive_ids([workflow_id], reason=reason)


def test_concurrent_archive_all_records_survive(tmp_path: Path) -> None:
    """Parallel archive calls starting from the same file must all persist.

    Regression for the lost-update bug: without an interprocess lock around
    load-mutate-save, concurrent writers that read the same initial state
    clobber each other and only the last batch survives in the ledger.
    """
    with _patch_triage_path(tmp_path):
        save_triage({"reviewed": [], "archived": [], "verdict_overrides": {}})
        ids = [f"wf-{i}" for i in range(6)]

        ctx = multiprocessing.get_context("fork")
        procs = [
            ctx.Process(
                target=_child_archive,
                args=(str(tmp_path), wid, f"batch-{wid}"),
                name=f"archive-{wid}",
            )
            for wid in ids
        ]
        try:
            for proc in procs:
                proc.start()
            for proc in procs:
                proc.join(timeout=30)
                if proc.is_alive():
                    proc.terminate()
                    proc.join(timeout=5)
                assert proc.exitcode == 0, f"{proc.name} exited {proc.exitcode}"
        finally:
            for proc in procs:
                if proc.is_alive():
                    proc.kill()

        data = load_triage()
        surviving = {rec["workflow_id"]: rec for rec in data["archived"]}
        assert set(surviving) == set(ids), (
            f"expected all {len(ids)} archived IDs to survive, got {sorted(surviving)}"
        )
        for wid in ids:
            assert surviving[wid]["reason"] == f"batch-{wid}"
            assert surviving[wid]["archived_at"] is not None


def test_locked_mutate_rereads_fresh_data_each_call(tmp_path: Path) -> None:
    """Two stale-read-style updates both persist because the helper re-reads under the lock.

    Each caller conceptually holds the same stale snapshot, but ``_locked_mutate``
    re-reads from disk inside the lock rather than trusting a caller-cached copy.
    The second call must therefore observe the first call's write.
    """
    with _patch_triage_path(tmp_path):
        save_triage({"reviewed": [], "archived": [], "verdict_overrides": {}})
        stale_snapshot = load_triage()  # both callers conceptually start here
        observed: list[list[str]] = []

        def archive_a(data: dict) -> dict:
            observed.append([r["workflow_id"] for r in data["archived"]])
            data["archived"] = normalize_archived(data["archived"]) + [
                {
                    "workflow_id": "wf-a",
                    "reason": "a",
                    "archived_at": datetime.now(UTC).isoformat(),
                }
            ]
            return {"count": 1}

        def archive_b(data: dict) -> dict:
            observed.append([r["workflow_id"] for r in data["archived"]])
            data["archived"] = normalize_archived(data["archived"]) + [
                {
                    "workflow_id": "wf-b",
                    "reason": "b",
                    "archived_at": datetime.now(UTC).isoformat(),
                }
            ]
            return {"count": 1}

        _locked_mutate(archive_a)
        # Caller B still holds the stale snapshot, but the helper re-reads from
        # disk under the lock, so it must observe wf-a rather than the stale
        # empty set.
        _locked_mutate(archive_b)

        assert observed == [[], ["wf-a"]], (
            f"second call did not re-read fresh data under lock: {observed}"
        )
        # The caller's stale snapshot is untouched (helper never mutates it).
        assert stale_snapshot["archived"] == []

        data = load_triage()
        assert {r["workflow_id"] for r in data["archived"]} == {"wf-a", "wf-b"}


def test_save_triage_atomic_replace_leaves_prior_file_intact(tmp_path: Path) -> None:
    """A crash during save cannot leave triage.json as a truncated/partial file.

    The writer must use a temp file + atomic replace: if the replace step
    fails, the destination still holds the previous valid content and no
    leftover temp fragment remains.
    """
    with _patch_triage_path(tmp_path):
        path = tmp_path / "triage.json"
        save_triage({"reviewed": ["wf-1"], "archived": [], "verdict_overrides": {}})
        prior = path.read_text()
        assert json.loads(prior)["reviewed"] == ["wf-1"]

        # Simulate a crash right before the atomic replace step: the temp file
        # has been written and fsynced, but replace never lands.
        with patch("mtor.triage._replace_file", side_effect=OSError("simulated crash")):
            with pytest.raises(OSError, match="simulated crash"):
                save_triage(
                    {"reviewed": ["wf-2"], "archived": [], "verdict_overrides": {}}
                )

        # Destination must still hold the prior, valid, untruncated content.
        assert path.read_text() == prior
        assert json.loads(path.read_text())["reviewed"] == ["wf-1"]

        # No leftover temp fragments in the directory (only the real file and
        # possibly its sibling lock file should remain).
        leftovers = [
            p.name
            for p in path.parent.iterdir()
            if p.name not in {path.name, path.name + ".lock"}
        ]
        assert leftovers == [], f"leftover temp files: {leftovers}"
