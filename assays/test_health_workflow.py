"""Tests for mtor.worker.health_workflow — circuit breaker logic via internal helpers."""
import asyncio
from unittest.mock import patch

from mtor.worker.health_workflow import DEFAULT_COOLDOWN_SECONDS, HealthWorkflow


# ---------------------------------------------------------------------------
# _apply_rate_limit
# ---------------------------------------------------------------------------

def test_rate_limit_creates_entry():
    """rate_limit on unknown provider creates an open entry with cooldown."""
    wf = HealthWorkflow()
    with patch("mtor.worker.health_workflow.time") as mock_time:
        mock_time.time.return_value = 1000.0
        wf._apply_rate_limit("acme")

    entry = wf._health["acme"]
    assert entry["state"] == "open"
    assert entry["cooldown_until"] == 1000.0 + DEFAULT_COOLDOWN_SECONDS
    assert entry["consecutive_failures"] == 1


def test_rate_limit_increments_failures():
    """Repeated rate_limit calls increment consecutive_failures."""
    wf = HealthWorkflow()
    with patch("mtor.worker.health_workflow.time") as mock_time:
        mock_time.time.return_value = 0.0
        wf._apply_rate_limit("acme")
        wf._apply_rate_limit("acme")

    assert wf._health["acme"]["consecutive_failures"] == 2


def test_rate_limit_preserves_other_providers():
    """Rate-limiting one provider does not disturb another."""
    wf = HealthWorkflow()
    with patch("mtor.worker.health_workflow.time") as mock_time:
        mock_time.time.return_value = 0.0
        wf._apply_rate_limit("alpha")
        wf._apply_rate_limit("beta")

    assert wf._health["alpha"]["state"] == "open"
    assert wf._health["beta"]["state"] == "open"
    assert wf._health["alpha"]["consecutive_failures"] == 1
    assert wf._health["beta"]["consecutive_failures"] == 1


# ---------------------------------------------------------------------------
# _apply_success
# ---------------------------------------------------------------------------

def test_success_creates_closed_entry():
    """success on unknown provider creates a closed entry."""
    wf = HealthWorkflow()
    wf._apply_success("acme")

    entry = wf._health["acme"]
    assert entry["state"] == "closed"
    assert entry["cooldown_until"] is None
    assert entry["consecutive_failures"] == 0


def test_success_resets_open_circuit():
    """success closes an open circuit and clears failures/cooldown."""
    wf = HealthWorkflow()
    with patch("mtor.worker.health_workflow.time") as mock_time:
        mock_time.time.return_value = 500.0
        wf._apply_rate_limit("acme")

    # Verify circuit is open
    assert wf._health["acme"]["state"] == "open"

    wf._apply_success("acme")
    entry = wf._health["acme"]
    assert entry["state"] == "closed"
    assert entry["cooldown_until"] is None
    assert entry["consecutive_failures"] == 0


def test_success_idempotent():
    """Calling success twice leaves the entry closed with zero failures."""
    wf = HealthWorkflow()
    wf._apply_success("acme")
    wf._apply_success("acme")

    entry = wf._health["acme"]
    assert entry["state"] == "closed"
    assert entry["consecutive_failures"] == 0


# ---------------------------------------------------------------------------
# health query
# ---------------------------------------------------------------------------

def test_health_returns_copy():
    """health() returns a shallow copy, not the internal dict."""
    wf = HealthWorkflow()
    wf._apply_success("acme")
    result = wf.health()

    assert result == wf._health
    assert result is not wf._health


def test_health_empty():
    """health() on fresh workflow returns empty dict."""
    wf = HealthWorkflow()
    assert wf.health() == {}


# ---------------------------------------------------------------------------
# run (initial health seeding)
# ---------------------------------------------------------------------------

async def _run_workflow(wf, params):
    """Helper: run the workflow with wait_condition patched to resolve immediately."""
    with patch("mtor.worker.health_workflow.workflow") as mock_wf:
        async def _resolve(_fn):
            return None
        mock_wf.wait_condition.side_effect = _resolve
        await wf.run(params)


def test_run_seeds_initial_health():
    """run() seeds _health from params['initial_health']."""
    wf = HealthWorkflow()
    initial = {"acme": {"state": "open", "cooldown_until": 99.0, "consecutive_failures": 3}}
    asyncio.run(_run_workflow(wf, {"initial_health": initial}))

    assert wf._health == initial


def test_run_default_empty_health():
    """run() with no params starts with empty health dict."""
    wf = HealthWorkflow()
    asyncio.run(_run_workflow(wf, None))

    assert wf._health == {}
