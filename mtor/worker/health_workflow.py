"""HealthWorkflow — Temporal workflow for tracking provider health state.

Circuit breaker signals:
  - rate_limit(provider): trips circuit to "open" with cooldown
  - success(provider): closes circuit, resets failures
  - health query: returns current provider health state dict
"""

from __future__ import annotations

import time

from temporalio import workflow


DEFAULT_COOLDOWN_SECONDS = 3600  # 1 hour


@workflow.defn
class HealthWorkflow:
    """Track provider health state with circuit breaker signals and queries.

    Each provider has an entry with:
      - state: "closed" | "open" | "half_open"
      - cooldown_until: epoch float or None
      - consecutive_failures: int
    """

    def __init__(self) -> None:
        self._health: dict[str, dict] = {}

    # ------------------------------------------------------------------
    # Internal state helpers (callable without Temporal runtime)
    # ------------------------------------------------------------------

    def _apply_rate_limit(self, provider: str) -> None:
        """Open circuit for *provider* (rate limited)."""
        if provider not in self._health:
            self._health[provider] = {
                "state": "closed",
                "cooldown_until": None,
                "consecutive_failures": 0,
            }
        entry = self._health[provider]
        entry["state"] = "open"
        entry["cooldown_until"] = time.time() + DEFAULT_COOLDOWN_SECONDS
        entry["consecutive_failures"] = entry.get("consecutive_failures", 0) + 1

    def _apply_success(self, provider: str) -> None:
        """Close circuit for *provider* (successful response)."""
        if provider not in self._health:
            self._health[provider] = {
                "state": "closed",
                "cooldown_until": None,
                "consecutive_failures": 0,
            }
        entry = self._health[provider]
        entry["state"] = "closed"
        entry["cooldown_until"] = None
        entry["consecutive_failures"] = 0

    # ------------------------------------------------------------------
    # Temporal signals / queries
    # ------------------------------------------------------------------

    @workflow.signal
    async def rate_limit(self, provider: str) -> None:
        """Signal: trip circuit to open for *provider*."""
        self._apply_rate_limit(provider)

    @workflow.signal
    async def success(self, provider: str) -> None:
        """Signal: close circuit for *provider*."""
        self._apply_success(provider)

    @workflow.query
    def health(self) -> dict:
        """Query: return current provider health state dict."""
        return dict(self._health)

    @workflow.run
    async def run(self, params: dict | None = None) -> dict:
        """Run indefinitely, processing signals until externally terminated."""
        initial = (params or {}).get("initial_health", {})
        self._health = dict(initial)
        await workflow.wait_condition(lambda: False)
