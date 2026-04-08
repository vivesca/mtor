"""Provider routing with circuit breaker.

Tracks provider health states (closed/open/half_open) and routes requests
to the first available provider in priority order, automatically recovering
from rate-limit trips via a cooldown window.
"""

from __future__ import annotations

import json
import re
import time
from pathlib import Path
from typing import Any

# Exit code emitted by ribosome when rate-limited by the provider API.
EXIT_RATE_LIMITED = 42

# Priority order for provider selection (highest first).
PROVIDER_PRIORITY = ["zhipu", "infini", "volcano"]

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


def select_provider(health: dict[str, Any], override: str | None = None) -> str:
    """Select a provider based on priority and circuit-breaker state.

    If *override* is given it is returned directly (bypasses routing).
    Otherwise each provider in PROVIDER_PRIORITY is checked in order:
      - "closed"  -> return immediately
      - "open"    -> if cooldown has expired, transition to "half_open" and return
      - "half_open" -> return immediately
    If all providers are "open" and still in cooldown, the one with the
    earliest cooldown_until timestamp is returned.
    """
    if override:
        return override

    now = time.time()
    earliest_open: tuple[float, str] | None = None

    for prov in PROVIDER_PRIORITY:
        state = health.get(prov, {}).get("state", "closed")
        if state == "closed":
            return prov
        if state == "open":
            cooldown_until = health[prov].get("cooldown_until")
            if cooldown_until is not None and now >= cooldown_until:
                # Cooldown expired — try it in half_open state
                return prov
            # Track earliest open for fallback
            if cooldown_until is not None:
                if earliest_open is None or cooldown_until < earliest_open[0]:
                    earliest_open = (cooldown_until, prov)
        elif state == "half_open":
            return prov

    # All open and in cooldown — fall back to earliest cooldown
    if earliest_open is not None:
        return earliest_open[1]

    # No health record for any provider — return first in priority
    return PROVIDER_PRIORITY[0]


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


def compute_dispatch_interval(tracker: object, base_interval: int) -> int:
    """Compute dispatch interval, throttled when *tracker* reports high rejection.

    Uses duck-typing: *tracker* must provide ``should_throttle()`` and
    ``rejection_rate()`` (e.g. :class:`mtor.watch.RejectionTracker`).

    When the rejection rate exceeds the tracker's threshold the interval
    is scaled proportionally (up to 5× at 100 % rejection).  Otherwise
    *base_interval* is returned unchanged.
    """
    if not hasattr(tracker, "should_throttle") or not hasattr(
        tracker, "rejection_rate"
    ):
        return base_interval

    if not tracker.should_throttle():
        return base_interval

    rate = tracker.rejection_rate()
    multiplier = 1.0 + rate * 4.0
    return int(base_interval * multiplier)


# ---------------------------------------------------------------------------
# AMPK sensing — dispatch gate based on ganglion load
# ---------------------------------------------------------------------------

# Default thresholds for dispatch blocking.
DEFAULT_MAX_TASKS = 5
DEFAULT_MAX_LOAD_AVG = 4.0


def should_block_dispatch(
    load: object,
    *,
    max_tasks: int = DEFAULT_MAX_TASKS,
    max_load_avg: float = DEFAULT_MAX_LOAD_AVG,
) -> bool:
    """Return True when ganglion load is too high to accept new dispatches.

    Uses duck-typing: *load* must have ``running_tasks`` (int) and
    ``load_avg`` (float) attributes (e.g. :class:`mtor.watch.GanglionLoad`).
    Returns False for inputs that lack these attributes.
    """
    if not hasattr(load, "running_tasks") or not hasattr(load, "load_avg"):
        return False
    return load.running_tasks >= max_tasks or load.load_avg >= max_load_avg
