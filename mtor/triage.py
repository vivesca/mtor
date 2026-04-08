"""Review/archive lifecycle for completed workflows.

Three-state lifecycle: completed -> reviewed -> archived.
Storage: ~/.config/mtor/triage.json
"""

from __future__ import annotations

import json
import re
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

TRIAGE_PATH = Path.home() / ".config" / "mtor" / "triage.json"


def _default_data() -> dict[str, Any]:
    return {"reviewed": [], "archived": [], "verdict_overrides": {}, "updated": None}


def load_triage() -> dict[str, Any]:
    """Load triage data from disk. Returns defaults if file missing."""
    if TRIAGE_PATH.exists():
        try:
            data = json.loads(TRIAGE_PATH.read_text())
            # Ensure keys exist
            for key in ("reviewed", "archived"):
                if key not in data:
                    data[key] = []
            if "verdict_overrides" not in data:
                data["verdict_overrides"] = {}
            if "updated" not in data:
                data["updated"] = None
            return data
        except (json.JSONDecodeError, OSError):
            pass
    return _default_data()


def save_triage(data: dict[str, Any]) -> None:
    """Save triage data to disk. Creates parent dirs on first write."""
    TRIAGE_PATH.parent.mkdir(parents=True, exist_ok=True)
    data["updated"] = datetime.now(UTC).isoformat()
    TRIAGE_PATH.write_text(json.dumps(data, indent=2) + "\n")


def review_ids(ids: list[str]) -> dict[str, Any]:
    """Add IDs to reviewed set. Idempotent. Returns envelope result dict."""
    data = load_triage()
    reviewed = set(data["reviewed"])
    archived = set(data["archived"])
    for wid in ids:
        if wid not in archived:
            reviewed.add(wid)
    data["reviewed"] = sorted(reviewed)
    save_triage(data)
    return {"reviewed": data["reviewed"], "count": len(data["reviewed"])}


def archive_ids(ids: list[str]) -> dict[str, Any]:
    """Move IDs to archived set. Removes from reviewed. Returns envelope result dict."""
    data = load_triage()
    archived = set(data["archived"])
    incoming = set(ids)
    archived |= incoming
    # Remove newly archived from reviewed
    data["reviewed"] = sorted(set(data["reviewed"]) - incoming)
    data["archived"] = sorted(archived)
    save_triage(data)
    return {"archived": data["archived"], "count": len(data["archived"])}


def parse_duration(duration_str: str) -> timedelta:
    """Parse duration string like '7d', '24h', '30m' into timedelta."""
    match = re.match(r"^(\d+)(h|d|m)$", duration_str)
    if not match:
        raise ValueError(
            f"Invalid duration format: {duration_str!r}. Use e.g. '7d', '24h', '30m'."
        )
    amount = int(match.group(1))
    unit = match.group(2)
    if unit == "h":
        return timedelta(hours=amount)
    elif unit == "d":
        return timedelta(days=amount)
    elif unit == "m":
        return timedelta(minutes=amount)
    raise ValueError(f"Unknown unit: {unit}")


def override_verdict(ids: list[str], verdict: str) -> dict[str, Any]:
    """Set verdict override for workflow IDs. Stored locally, overlays Temporal SA."""
    data = load_triage()
    overrides = data.get("verdict_overrides", {})
    for wid in ids:
        overrides[wid] = verdict
    data["verdict_overrides"] = overrides
    save_triage(data)
    return {"overridden": len(ids), "verdict": verdict}


def get_verdict_overrides() -> dict[str, str]:
    """Return {workflow_id: verdict} overrides."""
    data = load_triage()
    return data.get("verdict_overrides", {})


def get_triage_sets() -> tuple[set[str], set[str]]:
    """Return (reviewed_ids, archived_ids) sets."""
    data = load_triage()
    return set(data["reviewed"]), set(data["archived"])
