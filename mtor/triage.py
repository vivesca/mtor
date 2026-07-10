"""Review/archive lifecycle for completed workflows.

Three-state lifecycle: completed -> reviewed -> archived.
Storage: ~/.config/mtor/triage.json
"""

from __future__ import annotations

import contextlib
import fcntl
import json
import os
import re
import tempfile
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any, Callable, TypeVar

TRIAGE_PATH = Path.home() / ".config" / "mtor" / "triage.json"

T = TypeVar("T")


def _lock_path() -> Path:
    """Path to the interprocess lock file guarding triage read-modify-write."""
    return TRIAGE_PATH.parent / (TRIAGE_PATH.name + ".lock")


@contextlib.contextmanager
def _triage_lock():
    """Exclusive interprocess lock around the triage load-mutate-save sequence.

    Uses ``fcntl.flock`` on a sibling lock file — the same convention as
    ``mtor/worker/git_ops.py``. Works on Linux and macOS. Blocks until the
    lock is acquired; released on close. Without this, concurrent
    ``mtor review``/``archive``/``verdict`` invocations each load the same
    stale state and the last writer clobbers the earlier batches.
    """
    TRIAGE_PATH.parent.mkdir(parents=True, exist_ok=True)
    lock = _lock_path()
    lock_fh = open(lock, "a+")
    try:
        fcntl.flock(lock_fh.fileno(), fcntl.LOCK_EX)
        try:
            yield
        finally:
            fcntl.flock(lock_fh.fileno(), fcntl.LOCK_UN)
    finally:
        lock_fh.close()


def _replace_file(src: str, dst: str) -> None:
    """Atomic replace of ``dst`` with ``src`` (POSIX, same filesystem)."""
    os.replace(src, dst)


def _atomic_write_text(path: Path, text: str) -> None:
    """Write ``text`` to ``path`` via a temp file + atomic replace.

    A crash before the replace leaves the prior file intact (the temp file is
    cleaned up on failure), so the destination can never be observed as a
    truncated/partial JSON document.
    """
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp_name = tempfile.mkstemp(
        dir=path.parent, prefix=path.name + ".", suffix=".tmp"
    )
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as tmp_fh:
            tmp_fh.write(text)
            tmp_fh.flush()
            os.fsync(tmp_fh.fileno())
        _replace_file(tmp_name, str(path))
    except BaseException:
        with contextlib.suppress(OSError):
            os.unlink(tmp_name)
        raise


def _locked_mutate(mutate: Callable[[dict[str, Any]], T]) -> T:
    """Load triage, run ``mutate`` under an exclusive lock, persist, return result.

    The whole load-mutate-save sequence is serialized so concurrent writers
    cannot clobber each other. ``mutate`` receives the freshly-loaded data
    dict (re-read under the lock, never a caller-cached snapshot), mutates it
    in place, and returns the caller's result.
    """
    with _triage_lock():
        data = load_triage()
        result = mutate(data)
        save_triage(data)
        return result


def _default_data() -> dict[str, Any]:
    return {"reviewed": [], "archived": [], "verdict_overrides": {}, "updated": None}


def _normalize_archived_entry(entry: Any) -> dict[str, str | None] | None:
    """Return the canonical archived record shape, accepting legacy string IDs."""
    if isinstance(entry, str):
        return {"workflow_id": entry, "reason": "legacy", "archived_at": None}
    if isinstance(entry, dict):
        workflow_id = entry.get("workflow_id")
        if isinstance(workflow_id, str) and workflow_id:
            reason = entry.get("reason")
            archived_at = entry.get("archived_at")
            return {
                "workflow_id": workflow_id,
                "reason": reason if isinstance(reason, str) and reason else "legacy",
                "archived_at": archived_at if isinstance(archived_at, str) else None,
            }
    return None


def normalize_archived(entries: list[Any]) -> list[dict[str, str | None]]:
    """Normalize archived entries and de-duplicate by workflow ID."""
    records: dict[str, dict[str, str | None]] = {}
    for entry in entries:
        record = _normalize_archived_entry(entry)
        if record is not None and record["workflow_id"] not in records:
            records[record["workflow_id"]] = record
    return [records[workflow_id] for workflow_id in sorted(records)]


def archived_ids(data: dict[str, Any]) -> set[str]:
    """Return archived workflow IDs from raw or normalized triage data."""
    return {
        record["workflow_id"]
        for record in normalize_archived(data.get("archived", []))
        if record["workflow_id"] is not None
    }


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
            data["archived"] = normalize_archived(data["archived"])
            return data
        except (json.JSONDecodeError, OSError):
            pass
    return _default_data()


def save_triage(data: dict[str, Any]) -> None:
    """Save triage data to disk atomically. Creates parent dirs on first write.

    Writes through a temp file followed by atomic replace, so a crash mid-save
    cannot leave a partial/truncated ``triage.json``. Note: this is a full-file
    overwrite and does NOT itself take the interprocess lock — callers that
    read-modify-write must go through ``_locked_mutate`` to avoid lost updates.
    """
    data["updated"] = datetime.now(UTC).isoformat()
    _atomic_write_text(TRIAGE_PATH, json.dumps(data, indent=2) + "\n")


def review_ids(ids: list[str]) -> dict[str, Any]:
    """Add IDs to reviewed set. Idempotent. Returns envelope result dict."""

    def mutate(data: dict[str, Any]) -> dict[str, Any]:
        reviewed = set(data["reviewed"])
        archived = archived_ids(data)
        for wid in ids:
            if wid not in archived:
                reviewed.add(wid)
        data["reviewed"] = sorted(reviewed)
        return {"reviewed": data["reviewed"], "count": len(data["reviewed"])}

    return _locked_mutate(mutate)


def archive_ids(ids: list[str], *, reason: str = "legacy") -> dict[str, Any]:
    """Move IDs to archived set. Removes from reviewed. Returns envelope result dict.

    Only IDs newly archived by *this* call appear in ``archived`` and
    ``archived_records``.  The total size of the persisted archive is
    reported separately via ``archived_total`` (reflects the merged persisted
    archive after the locked write).
    """

    def mutate(data: dict[str, Any]) -> dict[str, Any]:
        records = {
            record["workflow_id"]: record
            for record in normalize_archived(data["archived"])
        }
        incoming = set(ids)
        archived_at = datetime.now(UTC).isoformat()
        newly_archived_ids: list[str] = []
        for workflow_id in sorted(incoming):
            if workflow_id not in records:
                records[workflow_id] = {
                    "workflow_id": workflow_id,
                    "reason": reason,
                    "archived_at": archived_at,
                }
                newly_archived_ids.append(workflow_id)
        # Remove newly archived from reviewed
        data["reviewed"] = sorted(set(data["reviewed"]) - incoming)
        data["archived"] = [records[workflow_id] for workflow_id in sorted(records)]
        newly_archived_records = [records[wid] for wid in newly_archived_ids]
        return {
            "archived": newly_archived_ids,
            "archived_records": newly_archived_records,
            "count": len(newly_archived_ids),
            "archived_total": len(data["archived"]),
        }

    return _locked_mutate(mutate)


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

    def mutate(data: dict[str, Any]) -> dict[str, Any]:
        overrides = data.get("verdict_overrides", {})
        for wid in ids:
            overrides[wid] = verdict
        data["verdict_overrides"] = overrides
        return {"overridden": len(ids), "verdict": verdict}

    return _locked_mutate(mutate)


def get_verdict_overrides() -> dict[str, str]:
    """Return {workflow_id: verdict} overrides."""
    data = load_triage()
    return data.get("verdict_overrides", {})


def get_triage_sets() -> tuple[set[str], set[str]]:
    """Return (reviewed_ids, archived_ids) sets."""
    data = load_triage()
    return set(data["reviewed"]), archived_ids(data)
