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

        result = archive_ids(["wf-1"])
        assert "wf-1" in result["archived"]
        assert result["count"] == 1

        reviewed, archived = get_triage_sets()
        assert "wf-1" not in reviewed
        assert "wf-2" in reviewed
        assert "wf-1" in archived


def test_override_verdict_roundtrip(tmp_path: Path) -> None:
    """override_verdict stores overrides and get_verdict_overrides retrieves them."""
    with _patch_triage_path(tmp_path):
        r = override_verdict(["wf-a", "wf-b"], "approved")
        assert r["overridden"] == 2
        assert r["verdict"] == "approved"

        overrides = get_verdict_overrides()
        assert overrides["wf-a"] == "approved"
        assert overrides["wf-b"] == "approved"


def test_parse_duration_valid_and_invalid() -> None:
    """parse_duration handles h/d/m units and rejects bad input."""
    assert parse_duration("7d") == timedelta(days=7)
    assert parse_duration("24h") == timedelta(hours=24)
    assert parse_duration("30m") == timedelta(minutes=30)

    with pytest.raises(ValueError, match="Invalid duration"):
        parse_duration("bad")

    with pytest.raises(ValueError, match="Invalid duration"):
        parse_duration("5w")
