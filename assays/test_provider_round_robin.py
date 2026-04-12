"""Tests for concurrency-aware round-robin provider routing in mtor.worker.provider."""

from __future__ import annotations

import time
from collections import Counter

from mtor.worker.provider import (
    PROVIDER_LIMITS,
    PROVIDER_PRIORITY,
    RR_KEY,
    _active_count,
    _is_available,
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
        """Calls rotate through all providers in priority order."""
        health = _all_closed()
        results = [select_provider(health) for _ in range(len(PROVIDER_PRIORITY))]
        assert results == PROVIDER_PRIORITY

    def test_wraps_around(self):
        """After a full cycle the rotation restarts from the first provider."""
        health = _all_closed()
        for _ in range(len(PROVIDER_PRIORITY)):
            select_provider(health)
        assert select_provider(health) == PROVIDER_PRIORITY[0]

    def test_multiple_cycles(self):
        """Two full cycles produce the same pattern twice."""
        health = _all_closed()
        n = len(PROVIDER_PRIORITY)
        results = [select_provider(health) for _ in range(n * 2)]
        assert results == PROVIDER_PRIORITY * 2


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
        health[RR_KEY] = 2  # third provider next
        assert select_provider(health) == PROVIDER_PRIORITY[2]
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
            "gemini": {"state": "open", "cooldown_until": now + 3600},
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
            "gemini": {"state": "open", "cooldown_until": now + 3600},
        }
        assert select_provider(health) == "infini"
        assert select_provider(health) == "infini"

    def test_half_open_included_in_rotation(self):
        """half_open providers participate in round-robin."""
        now = time.time()
        health = {
            "zhipu": {"state": "closed"},
            "infini": {"state": "half_open"},
            "volcano": {"state": "closed"},
            "gemini": {"state": "open", "cooldown_until": now + 3600},
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
            "gemini": {"state": "open", "cooldown_until": now + 3600},
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
        assert select_provider(health) == PROVIDER_PRIORITY[0]


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
            "gemini": {"state": "open", "cooldown_until": now + 900},
        }
        assert select_provider(health) == "infini"


# ---------------------------------------------------------------------------
# Concurrency-aware routing
# ---------------------------------------------------------------------------

class TestConcurrencyAwareRouting:
    """select_provider respects per-provider concurrency limits via _active_count."""

    def setup_method(self):
        """Reset active counts before each test."""
        for p in PROVIDER_PRIORITY:
            _active_count[p] = 0

    def teardown_method(self):
        """Reset active counts after each test."""
        for p in PROVIDER_PRIORITY:
            _active_count[p] = 0

    def test_distributes_7_tasks_across_3_providers(self):
        """7 tasks with limit 2 each: first 6 distribute 2-2-2, 7th gets least loaded."""
        now = time.time()
        health = {
            "zhipu": {"state": "closed"},
            "infini": {"state": "closed"},
            "volcano": {"state": "closed"},
            "gemini": {"state": "open", "cooldown_until": now + 3600},
        }

        selections = []
        for _ in range(7):
            provider = select_provider(health)
            selections.append(provider)
            _active_count[provider] += 1

        # First 6 tasks: 2 each for zhipu, infini, volcano
        first6 = Counter(selections[:6])
        assert first6["zhipu"] == 2
        assert first6["infini"] == 2
        assert first6["volcano"] == 2

        # gemini never selected (unhealthy)
        counts = Counter(selections)
        assert counts.get("gemini", 0) == 0

        # 7th task goes to a valid provider (least-loaded fallback)
        assert selections[6] in ("zhipu", "infini", "volcano")

    def test_unhealthy_provider_skipped(self):
        """Open provider with active cooldown is never selected."""
        now = time.time()
        health = {
            "zhipu": {"state": "open", "cooldown_until": now + 3600},
            "infini": {"state": "closed"},
            "volcano": {"state": "closed"},
            "gemini": {"state": "open", "cooldown_until": now + 3600},
        }

        for _ in range(4):
            result = select_provider(health)
            assert result not in ("zhipu", "gemini")

    def test_all_at_limit_falls_back_to_least_loaded(self):
        """When all providers are at concurrency limit, least-loaded healthy is returned."""
        for p in PROVIDER_PRIORITY:
            _active_count[p] = PROVIDER_LIMITS.get(p, 2)

        health = _all_closed()
        result = select_provider(health)
        # All at limit — fallback picks least loaded (all tied → first in priority)
        assert result in PROVIDER_PRIORITY

    def test_provider_at_limit_skipped(self):
        """Provider at its concurrency limit is excluded from available."""
        _active_count["zhipu"] = PROVIDER_LIMITS["zhipu"]  # at limit
        _active_count["infini"] = 0

        health = {
            "zhipu": {"state": "closed"},
            "infini": {"state": "closed"},
            "volcano": {"state": "closed"},
            "gemini": {"state": "closed"},
        }

        result = select_provider(health)
        assert result != "zhipu"

    def test_active_count_drives_selection(self):
        """Provider with lower active count is preferred over higher-count provider."""
        _active_count["zhipu"] = 1
        _active_count["infini"] = 0

        health = {
            "zhipu": {"state": "closed"},
            "infini": {"state": "closed"},
            "volcano": {"state": "closed"},
            "gemini": {"state": "closed"},
        }

        result = select_provider(health)
        assert result == "infini"

    def test_is_available_closed(self):
        """Closed provider is available."""
        assert _is_available("zhipu", {"zhipu": {"state": "closed"}}) is True

    def test_is_available_open_in_cooldown(self):
        """Open provider in cooldown is not available."""
        now = time.time()
        assert _is_available("zhipu", {
            "zhipu": {"state": "open", "cooldown_until": now + 3600},
        }) is False

    def test_is_available_open_cooldown_expired(self):
        """Open provider with expired cooldown is available."""
        now = time.time()
        assert _is_available("zhipu", {
            "zhipu": {"state": "open", "cooldown_until": now - 10},
        }) is True

    def test_is_available_half_open(self):
        """Half-open provider is available."""
        assert _is_available("zhipu", {
            "zhipu": {"state": "half_open"},
        }) is True

    def test_is_available_no_health_entry(self):
        """Provider with no health entry defaults to closed (available)."""
        assert _is_available("zhipu", {}) is True
