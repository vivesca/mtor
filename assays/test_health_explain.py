"""Tests for provider circuit-breaker explanations."""

from __future__ import annotations

from unittest.mock import patch

from mtor.doctor import format_health_display
from mtor.worker.provider import EXIT_RATE_LIMITED, select_provider, update_health


def test_rate_limit_transition_logs_once(capsys):
    """Provider health updates log only real circuit transitions."""
    health = {"zhipu": {"state": "closed", "cooldown_until": None, "consecutive_failures": 0}}

    with patch("mtor.worker.provider.time.time", return_value=1000.0):
        update_health("zhipu", EXIT_RATE_LIMITED, health, window_hours=1 / 60)
        update_health("zhipu", EXIT_RATE_LIMITED, health, window_hours=1 / 60)

    captured = capsys.readouterr()
    assert "[provider] zhipu: closed -> open (rate_limited, cooldown 1.0m)" in captured.err
    assert captured.err.count("[provider] zhipu:") == 1


def test_success_transition_logs_recovery(capsys):
    """A successful half-open provider logs recovery to healthy."""
    health = {"zhipu": {"state": "half_open", "cooldown_until": None, "consecutive_failures": 1}}

    update_health("zhipu", 0, health)

    captured = capsys.readouterr()
    assert "[provider] zhipu: half_open -> closed (success)" in captured.err
    assert health["zhipu"]["state"] == "closed"
    assert health["zhipu"]["consecutive_failures"] == 0


def test_select_provider_logs_expired_cooldown(capsys):
    """Expired cooldowns transition to half_open during provider selection."""
    health = {
        "zhipu": {"state": "open", "cooldown_until": 900.0, "consecutive_failures": 1},
        "gemini": {"state": "closed", "cooldown_until": None, "consecutive_failures": 0},
    }

    with patch("mtor.worker.provider.time.time", return_value=1000.0):
        chosen = select_provider(health)

    captured = capsys.readouterr()
    assert chosen in {"zhipu", "gemini"}
    assert health["zhipu"]["state"] == "half_open"
    assert "[provider] zhipu: open -> half_open (cooldown expired)" in captured.err


def test_doctor_provider_display_explains_states():
    """Doctor output renders circuit-breaker terms in operator language."""
    output = format_health_display(
        [{"name": "provider_circuit_breaker", "ok": True, "detail": "zhipu=open"}],
        {
            "zhipu": {
                "state": "open",
                "cooldown_until": 1060.0,
                "consecutive_failures": 2,
                "active_count": 1,
                "limit": 3,
            },
            "gemini": {
                "state": "closed",
                "cooldown_until": None,
                "consecutive_failures": 0,
                "active_count": 0,
                "limit": 1,
            },
        },
    )

    assert "zhipu" in output
    assert "cooldown (reopens in" in output
    assert "2 failures" in output
    assert "gemini" in output
    assert "healthy (0 active, limit 1)" in output
