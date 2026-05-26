"""Unit tests for mtor/triage.py — review/archive lifecycle for workflows."""

from __future__ import annotations

from datetime import timedelta
from pathlib import Path
from unittest.mock import patch

import pytest

from mtor.triage import (
    archive_ids,
    get_triage_sets,
    get_verdict_overrides,
    load_triage,
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
        save_triage({"reviewed": [], "archived": ["wf-archived"], "verdict_overrides": {}})

        result = review_ids(["wf-1", "wf-2", "wf-archived", "wf-1"])
        assert "wf-1" in result["reviewed"]
        assert "wf-2" in result["reviewed"]
        assert "wf-archived" not in result["reviewed"]
        assert result["count"] == 2  # idempotent wf-1 counted once


def test_archive_ids_moves_from_reviewed(tmp_path: Path) -> None:
    """archive_ids moves IDs to archived and removes them from reviewed."""
    with _patch_triage_path(tmp_path):
        save_triage({"reviewed": ["wf-1", "wf-2"], "archived": [], "verdict_overrides": {}})

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
        save_triage({"reviewed": [], "archived": ["wf-old-1", "wf-old-2"], "verdict_overrides": {}})

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
