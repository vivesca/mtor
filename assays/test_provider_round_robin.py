"""Tests for round-robin provider routing in mtor.worker.provider."""

from __future__ import annotations

import time


from mtor.worker.provider import (
    PROVIDER_PRIORITY,
    RR_KEY,
    select_provider,
)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _all_closed() -> dict:
    """Health dict with every provider in closed state."""
    return {p: {"state": "closed"} for p in PROVIDER_PRIORITY}


# ---------------------------------------------------------------------------
# Round-robin cycling
# ---------------------------------------------------------------------------

class TestRoundRobinCycle:
    """Consecutive calls with all providers closed should cycle evenly."""

    def test_cycles_through_all_providers(self):
        """Three calls rotate zhipu → infini → volcano."""
        health = _all_closed()
        results = [select_provider(health) for _ in range(3)]
        assert results == ["zhipu", "infini", "volcano"]

    def test_wraps_around(self):
        """After a full cycle the rotation restarts from the first provider."""
        health = _all_closed()
        for _ in range(3):
            select_provider(health)
        # 4th call wraps back
        assert select_provider(health) == "zhipu"

    def test_multiple_cycles(self):
        """Two full cycles produce the same pattern twice."""
        health = _all_closed()
        results = [select_provider(health) for _ in range(6)]
        assert results == ["zhipu", "infini", "volcano",
                           "zhipu", "infini", "volcano"]


# ---------------------------------------------------------------------------
# Index persistence
# ---------------------------------------------------------------------------

class TestRoundRobinPersistence:
    """Round-robin index is stored in the health dict."""

    def test_index_increments(self):
        """RR_KEY increments after each selection."""
        health = _all_closed()
        select_provider(health)
        assert health[RR_KEY] == 1
        select_provider(health)
        assert health[RR_KEY] == 2

    def test_index_defaults_to_zero(self):
        """Missing RR_KEY starts at index 0."""
        health = _all_closed()
        assert RR_KEY not in health
        result = select_provider(health)
        assert result == PROVIDER_PRIORITY[0]
        assert health[RR_KEY] == 1

    def test_index_resumes_from_existing(self):
        """Pre-existing RR_KEY is honoured on next call."""
        health = _all_closed()
        health[RR_KEY] = 2  # volcano next
        assert select_provider(health) == "volcano"
        assert health[RR_KEY] == 3


# ---------------------------------------------------------------------------
# Interaction with circuit breaker
# ---------------------------------------------------------------------------

class TestRoundRobinWithCircuitBreaker:
    """Round-robin only cycles through *available* providers."""

    def test_skips_open_providers(self):
        """Open providers in cooldown are excluded from rotation."""
        now = time.time()
        health = {
            "zhipu": {"state": "open", "cooldown_until": now + 3600},
            "infini": {"state": "closed"},
            "volcano": {"state": "closed"},
        }
        # Only infini and volcano are available
        r1 = select_provider(health)
        r2 = select_provider(health)
        assert {r1, r2} == {"infini", "volcano"}

    def test_single_available_always_returns_it(self):
        """When only one provider is available, always return it."""
        now = time.time()
        health = {
            "zhipu": {"state": "open", "cooldown_until": now + 3600},
            "infini": {"state": "closed"},
            "volcano": {"state": "open", "cooldown_until": now + 3600},
        }
        assert select_provider(health) == "infini"
        assert select_provider(health) == "infini"

    def test_half_open_included_in_rotation(self):
        """half_open providers participate in round-robin."""
        health = {
            "zhipu": {"state": "closed"},
            "infini": {"state": "half_open"},
            "volcano": {"state": "closed"},
        }
        results = [select_provider(health) for _ in range(4)]
        # zhipu, infini, volcano, zhipu — cycling through 3 available
        assert results == ["zhipu", "infini", "volcano", "zhipu"]

    def test_expired_cooldown_included(self):
        """Open provider with expired cooldown is available for rotation."""
        now = time.time()
        health = {
            "zhipu": {"state": "open", "cooldown_until": now - 10},
            "infini": {"state": "closed"},
        }
        # zhipu and infini both available
        r1 = select_provider(health)
        r2 = select_provider(health)
        assert {r1, r2} == {"zhipu", "infini"}


# ---------------------------------------------------------------------------
# Override behaviour
# ---------------------------------------------------------------------------

class TestOverrideBypass:
    """Override skips round-robin and does not advance the index."""

    def test_override_returns_directly(self):
        health = _all_closed()
        assert select_provider(health, override="volcano") == "volcano"

    def test_override_does_not_advance_index(self):
        health = _all_closed()
        select_provider(health, override="volcano")
        assert RR_KEY not in health

    def test_override_then_rr_resumes_from_zero(self):
        health = _all_closed()
        select_provider(health, override="volcano")
        # RR index still at 0 → zhipu next
        assert select_provider(health) == "zhipu"


# ---------------------------------------------------------------------------
# All-open fallback (no round-robin)
# ---------------------------------------------------------------------------

class TestAllOpenFallback:
    """When all providers are in cooldown, earliest cooldown wins."""

    def test_all_open_no_rr(self):
        now = time.time()
        health = {
            "zhipu": {"state": "open", "cooldown_until": now + 300},
            "infini": {"state": "open", "cooldown_until": now + 60},
            "volcano": {"state": "open", "cooldown_until": now + 600},
        }
        assert select_provider(health) == "infini"
