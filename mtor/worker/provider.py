"""Provider routing with circuit breaker.

Tracks provider health states (closed/open/half_open) and routes requests
to the first available provider in priority order, automatically recovering
from rate-limit trips via a cooldown window.
"""

from __future__ import annotations

import json
import re
import time
from collections import deque
from pathlib import Path
from typing import Any

# Exit code emitted by ribosome when rate-limited by the provider API.
EXIT_RATE_LIMITED = 42

# Priority order for provider selection (highest first).
PROVIDER_PRIORITY = ["zhipu", "infini", "volcano", "gemini"]

# Per-provider concurrency limits (max simultaneous tasks).
PROVIDER_LIMITS: dict[str, int] = {
    "zhipu": 3,    # Max tier
    "infini": 1,   # Lite tier — 1 concurrent
    "volcano": 1,  # Lite tier — 1 concurrent
    "gemini": 2,
    "codex": 2,
}

# Active task count per provider — incremented in translate(), decremented on completion.
_active_count: dict[str, int] = {p: 0 for p in PROVIDER_PRIORITY}

# Health-dict key used to persist the round-robin index across calls.
RR_KEY = "_rr_index"

# Persisted health state file.
HEALTH_FILE = Path("~/.config/mtor/provider_health.json").expanduser()


def load_health() -> dict[str, Any]:
    """Read HEALTH_FILE and return the provider health dict.

    Returns an empty dict if the file does not exist or cannot be parsed.
    """
    if not HEALTH_FILE.exists():
        return {}
    try:
        return json.loads(HEALTH_FILE.read_text())
    except (OSError, ValueError):
        return {}


def save_health(health: dict[str, Any]) -> None:
    """Write health state to HEALTH_FILE, creating parent directories as needed."""
    HEALTH_FILE.parent.mkdir(parents=True, exist_ok=True)
    HEALTH_FILE.write_text(json.dumps(health, indent=2))


def _is_available(provider: str, health: dict[str, Any]) -> bool:
    """Check if a provider is available (closed, half_open, or open with expired cooldown)."""
    state = health.get(provider, {}).get("state", "closed")
    if state in ("closed", "half_open"):
        return True
    if state == "open":
        cooldown_until = health.get(provider, {}).get("cooldown_until")
        if cooldown_until is not None and time.time() >= cooldown_until:
            return True
    return False


def _earliest_cooldown_provider(health: dict[str, Any]) -> str:
    """Return the provider with the earliest cooldown_until among open providers.

    Falls back to the first provider in PROVIDER_PRIORITY when no open
    providers have a cooldown_until timestamp.
    """
    earliest: tuple[float, str] | None = None
    for prov in PROVIDER_PRIORITY:
        entry = health.get(prov, {})
        if entry.get("state") == "open":
            cooldown_until = entry.get("cooldown_until")
            if cooldown_until is not None:
                if earliest is None or cooldown_until < earliest[0]:
                    earliest = (cooldown_until, prov)
    if earliest is not None:
        return earliest[1]
    return PROVIDER_PRIORITY[0]


def select_provider(health: dict[str, Any], override: str | None = None) -> str:
    """Select a provider using concurrency-aware round-robin.

    1. If *override* is given, return it directly (bypasses routing).
    2. Collect healthy providers (closed, half_open, open with expired cooldown).
    3. Among healthy, filter to those under their concurrency limit.
    4. If available, pick the least-loaded (lowest active count),
       breaking ties with the round-robin index stored in *health*.
    5. All at limit: fall back to least-loaded healthy provider.
    6. All unhealthy: return the one with earliest cooldown.
    """
    if override:
        return override

    # Filter to healthy providers
    healthy = [p for p in PROVIDER_PRIORITY if _is_available(p, health)]

    # Filter to providers under their concurrency limit
    available = [p for p in healthy if _active_count.get(p, 0) < PROVIDER_LIMITS.get(p, 2)]

    if available:
        # Pick least-loaded; break ties with round-robin index
        min_count = min(_active_count.get(p, 0) for p in available)
        least_loaded = [p for p in available if _active_count.get(p, 0) == min_count]
        idx = health.get(RR_KEY, 0) % len(least_loaded)
        chosen = least_loaded[idx]
        health[RR_KEY] = health.get(RR_KEY, 0) + 1
        return chosen

    # All at limit — fall back to least-loaded healthy
    if healthy:
        return min(healthy, key=lambda p: _active_count.get(p, 0))

    # All unhealthy — earliest cooldown
    return _earliest_cooldown_provider(health)


def update_health(
    provider: str,
    exit_code: int,
    health: dict[str, Any],
    window_hours: float = 1.0,
) -> None:
    """Update health state for *provider* based on *exit_code*.

    - exit 42 (rate limited): trip circuit to "open", set cooldown_until.
    - exit  0 (success)     : close circuit, reset consecutive_failures.
    - exit  1 (code bug)    : no state change (not the provider's fault).
    """
    if provider not in health:
        health[provider] = {
            "state": "closed",
            "cooldown_until": None,
            "consecutive_failures": 0,
        }

    entry = health[provider]

    if exit_code == EXIT_RATE_LIMITED:
        entry["state"] = "open"
        entry["cooldown_until"] = time.time() + window_hours * 3600
        entry["consecutive_failures"] = entry.get("consecutive_failures", 0) + 1
    elif exit_code == 0:
        entry["state"] = "closed"
        entry["cooldown_until"] = None
        entry["consecutive_failures"] = 0
    # exit 1 -> no state change


def parse_rate_limit_window(stderr: str) -> float:
    """Extract cooldown window in hours from stderr.

    Looks for the pattern ``window=Nh`` (e.g. ``window=2h``) and returns
    the value as a float.  Returns the default of ``1.0`` hours when no
    match is found.
    """
    m = re.search(r"window=(\d+)h", stderr)
    if m:
        return float(m.group(1))
    return 1.0


# ---------------------------------------------------------------------------
# AMPK sensing — dispatch gate based on ganglion load
# ---------------------------------------------------------------------------

# Default thresholds for dispatch blocking.
DEFAULT_MAX_TASKS = 5
DEFAULT_MAX_LOAD_AVG = 4.0


class ProviderFeedbackTracker:
    """Sliding-window tracker for provider dispatch rejection rate."""

    def __init__(
        self, window_size: int = 10, rejection_threshold: float = 0.5
    ) -> None:
        self._window_size = window_size
        self._threshold = rejection_threshold
        self._outcomes: deque[bool] = deque()

    def record(self, rejected: bool) -> None:
        """Record a single dispatch outcome."""
        self._outcomes.append(rejected)
        while len(self._outcomes) > self._window_size:
            self._outcomes.popleft()

    def should_throttle(self) -> bool:
        """Return True when rejection ratio meets or exceeds threshold."""
        if not self._outcomes:
            return False
        return sum(self._outcomes) / len(self._outcomes) >= self._threshold


def dispatch_blocked(running_tasks: int, load_avg: float) -> bool:
    """Return True when ganglion load exceeds safe dispatch thresholds."""
    return running_tasks >= DEFAULT_MAX_TASKS or load_avg >= DEFAULT_MAX_LOAD_AVG


def feedback_dispatch_blocked(
    running_tasks: int,
    load_avg: float,
    feedback: ProviderFeedbackTracker | None = None,
) -> bool:
    """Return True when load-blocked OR feedback tracker signals throttle."""
    if dispatch_blocked(running_tasks, load_avg):
        return True
    if feedback is not None and feedback.should_throttle():
        return True
    return False
