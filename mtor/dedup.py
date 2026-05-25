"""Dispatch deduplication — block identical prompts within a time window.

Prevents the same prompt (with optional spec_path identity) from being
dispatched more than once within a configurable window (default 300s).
State is persisted to a JSON file so dedup works across invocations.
"""

from __future__ import annotations

import hashlib
import json
import time
from pathlib import Path

DEFAULT_WINDOW_S = 300
DEFAULT_STATE_PATH = Path.home() / ".local" / "share" / "mtor" / "dedup-state.json"


def compute_identity(prompt: str, spec_path: Path | None = None) -> str:
    """Compute a dedup identity key from prompt text and optional spec_path."""
    parts = [prompt]
    if spec_path is not None:
        parts.append(str(spec_path))
    return hashlib.sha256("|".join(parts).encode()).hexdigest()[:16]


def _load_state(state_path: Path) -> dict[str, float]:
    """Load dedup state from a JSON file."""
    if not state_path.exists():
        return {}
    try:
        return json.loads(state_path.read_text())
    except (json.JSONDecodeError, OSError):
        return {}


def _save_state(state_path: Path, state: dict[str, float]) -> None:
    """Persist dedup state to a JSON file."""
    state_path.parent.mkdir(parents=True, exist_ok=True)
    state_path.write_text(json.dumps(state))


def _prune(state: dict[str, float], now: float, window: int) -> dict[str, float]:
    """Remove entries older than *window* seconds."""
    cutoff = now - window
    return {k: v for k, v in state.items() if v > cutoff}


def check_duplicate(
    prompt: str,
    spec_path: Path | None = None,
    window: int = DEFAULT_WINDOW_S,
    state_path: Path | None = None,
) -> str | None:
    """Check if *prompt* was recently dispatched **without** recording.

    Returns the identity key when the dispatch should be blocked (duplicate
    within the window).  Returns ``None`` when the dispatch is allowed.
    Does **not** write any state.
    """
    path = state_path or DEFAULT_STATE_PATH
    key = compute_identity(prompt, spec_path)
    now = time.time()

    state = _prune(_load_state(path), now, window)

    if key in state and (now - state[key]) < window:
        return key  # blocked — duplicate

    return None  # allowed


def record_dispatch(
    prompt: str,
    spec_path: Path | None = None,
    state_path: Path | None = None,
) -> None:
    """Record that a dispatch has been initiated (write-only).

    Call this only after workflow creation succeeds so that failed preflight
    attempts do not poison the dedup window.
    """
    path = state_path or DEFAULT_STATE_PATH
    key = compute_identity(prompt, spec_path)
    now = time.time()

    state = _prune(_load_state(path), now, DEFAULT_WINDOW_S)
    state[key] = now
    _save_state(path, state)


def check_and_record(
    prompt: str,
    spec_path: Path | None = None,
    window: int = DEFAULT_WINDOW_S,
    state_path: Path | None = None,
) -> str | None:
    """Check if *prompt* was recently dispatched; record if not a duplicate.

    Returns ``None`` when the dispatch is allowed (and records the timestamp).
    Returns the identity key when the dispatch should be blocked (duplicate
    within the window).

    Backward-compatible wrapper around :func:`check_duplicate` +
    :func:`record_dispatch`.
    """
    blocked = check_duplicate(prompt, spec_path=spec_path, window=window, state_path=state_path)
    if blocked is not None:
        return blocked
    record_dispatch(prompt, spec_path=spec_path, state_path=state_path)
    return None
