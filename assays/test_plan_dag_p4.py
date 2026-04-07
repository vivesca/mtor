"""Tests for mtor plan DAG phase 4 — HealthWorkflow: circuit breaker signals and queries."""

from __future__ import annotations

from mtor.worker.health_workflow import HealthWorkflow


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------


class TestHealthWorkflowTracksState:
    def test_test_health_workflow_tracks_state(self):
        """HealthWorkflow tracks provider health state across signal sequences."""
        wf = HealthWorkflow()

        # Initially no state
        assert wf.health() == {}

        # Rate limit zhipu → open
        wf._apply_rate_limit("zhipu")
        assert wf._health["zhipu"]["state"] == "open"
        assert wf._health["zhipu"]["consecutive_failures"] == 1

        # Rate limit again → failure count increases
        wf._apply_rate_limit("zhipu")
        assert wf._health["zhipu"]["consecutive_failures"] == 2

        # Success on a different provider → independent state
        wf._apply_success("infini")
        assert wf._health["zhipu"]["state"] == "open"
        assert wf._health["infini"]["state"] == "closed"


class TestHealthQueryReturnsCurrent:
    def test_test_health_query_returns_current(self):
        """health() query returns the current provider health dict."""
        wf = HealthWorkflow()
        wf._apply_rate_limit("zhipu")
        wf._apply_success("infini")

        result = wf.health()

        assert "zhipu" in result
        assert result["zhipu"]["state"] == "open"
        assert "infini" in result
        assert result["infini"]["state"] == "closed"


def test_rate_limit_signal_opens_circuit():
    """Rate limit signal transitions provider to open state with cooldown."""
    wf = HealthWorkflow()
    wf._apply_rate_limit("volcano")

    assert wf._health["volcano"]["state"] == "open"
    assert wf._health["volcano"]["consecutive_failures"] == 1
    assert wf._health["volcano"]["cooldown_until"] is not None


def test_success_signal_closes_circuit():
    """Success signal closes circuit and resets failure counters."""
    wf = HealthWorkflow()

    # First open the circuit
    wf._apply_rate_limit("infini")
    wf._apply_rate_limit("infini")
    assert wf._health["infini"]["state"] == "open"
    assert wf._health["infini"]["consecutive_failures"] == 2

    # Close it via success
    wf._apply_success("infini")

    assert wf._health["infini"]["state"] == "closed"
    assert wf._health["infini"]["consecutive_failures"] == 0
    assert wf._health["infini"]["cooldown_until"] is None
