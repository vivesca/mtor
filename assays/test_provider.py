"""Tests for mtor.worker.provider — circuit breaker routing."""

from __future__ import annotations

import json
import subprocess
import sys
import time
from pathlib import Path

import pytest

# Ensure the mtor package is importable from the repo root
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from mtor.worker.provider import (
    EXIT_RATE_LIMITED,
    HEALTH_FILE,
    PROVIDER_PRIORITY,
    load_health,
    parse_rate_limit_window,
    save_health,
    select_provider,
    update_health,
)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _health_file(tmp_path: Path, content: dict) -> Path:
    f = tmp_path / "provider_health.json"
    f.parent.mkdir(parents=True, exist_ok=True)
    f.write_text(json.dumps(content))
    return f


# ---------------------------------------------------------------------------
# select_provider tests
# ---------------------------------------------------------------------------

def test_select_closed_first():
    """Closed provider is selected before open ones."""
    health = {
        "zhipu": {"state": "open", "cooldown_until": time.time() + 3600},
        "infini": {"state": "closed"},
    }
    result = select_provider(health)
    assert result == "infini"


def test_select_skips_open():
    """Open provider with active cooldown is skipped."""
    health = {
        "zhipu": {"state": "open", "cooldown_until": time.time() + 3600},
        "infini": {"state": "closed"},
    }
    result = select_provider(health)
    assert result == "infini"


def test_select_half_open_on_cooldown_expiry():
    """Open provider with expired cooldown is tried in half_open state."""
    health = {
        "zhipu": {"state": "open", "cooldown_until": time.time() - 1},
        "infini": {"state": "open", "cooldown_until": time.time() + 3600},
    }
    result = select_provider(health)
    # zhipu's cooldown has expired — should be selected (half_open)
    assert result == "zhipu"


def test_select_all_open_picks_earliest_cooldown():
    """When all providers are open and in cooldown, pick the one with earliest cooldown."""
    now = time.time()
    health = {
        "zhipu": {"state": "open", "cooldown_until": now + 300},
        "infini": {"state": "open", "cooldown_until": now + 60},
        "volcano": {"state": "open", "cooldown_until": now + 600},
    }
    result = select_provider(health)
    # infini has the earliest cooldown_until
    assert result == "infini"


def test_select_all_open_half_open_wins():
    """half_open provider is selected over open providers."""
    now = time.time()
    health = {
        "zhipu": {"state": "open", "cooldown_until": now + 3600},
        "infini": {"state": "half_open", "cooldown_until": None},
    }
    result = select_provider(health)
    assert result == "infini"


def test_override_bypasses_routing():
    """When override is set it is returned directly, bypassing health checks."""
    health = {
        "zhipu": {"state": "open", "cooldown_until": time.time() + 3600},
        "infini": {"state": "closed"},
    }
    result = select_provider(health, override="volcano")
    assert result == "volcano"


# ---------------------------------------------------------------------------
# update_health tests
# ---------------------------------------------------------------------------

def test_update_rate_limit_opens_circuit():
    """Exit 42 trips the circuit to open and sets cooldown."""
    health: dict = {}
    now = time.time()
    update_health("zhipu", EXIT_RATE_LIMITED, health)
    entry = health["zhipu"]
    assert entry["state"] == "open"
    assert entry["cooldown_until"] is not None
    assert entry["cooldown_until"] >= now
    assert entry["consecutive_failures"] == 1


def test_update_success_closes_circuit():
    """Exit 0 closes the circuit and resets failures."""
    health = {
        "zhipu": {
            "state": "open",
            "cooldown_until": time.time() + 3600,
            "consecutive_failures": 3,
        }
    }
    update_health("zhipu", 0, health)
    entry = health["zhipu"]
    assert entry["state"] == "closed"
    assert entry["cooldown_until"] is None
    assert entry["consecutive_failures"] == 0


def test_update_normal_failure_no_state_change():
    """Exit 1 does not change state (code bug, not provider fault)."""
    health = {
        "zhipu": {
            "state": "closed",
            "cooldown_until": None,
            "consecutive_failures": 0,
        }
    }
    update_health("zhipu", 1, health)
    entry = health["zhipu"]
    assert entry["state"] == "closed"
    assert entry["cooldown_until"] is None


def test_update_rate_limit_with_window(tmp_path):
    """Rate limit with explicit window uses that window size."""
    health: dict = {}
    update_health("zhipu", EXIT_RATE_LIMITED, health, window_hours=2.0)
    entry = health["zhipu"]
    now = time.time()
    # cooldown_until should be ~2 hours from now
    assert entry["cooldown_until"] >= now + 1.9 * 3600
    assert entry["cooldown_until"] <= now + 2.1 * 3600


# ---------------------------------------------------------------------------
# parse_rate_limit_window tests
# ---------------------------------------------------------------------------

def test_parse_window_from_stderr():
    """Regex extracts window=Nh from stderr."""
    stderr = "rate limited by provider API, retry after window=2h cooldown"
    result = parse_rate_limit_window(stderr)
    assert result == 2.0


def test_parse_window_missing_defaults():
    """Missing window=Nh pattern returns default 1.0."""
    stderr = "internal server error"
    result = parse_rate_limit_window(stderr)
    assert result == 1.0


def test_parse_window_multiple_matches():
    """Only the first match is used."""
    stderr = "window=3h; also found window=1h"
    result = parse_rate_limit_window(stderr)
    assert result == 3.0


# ---------------------------------------------------------------------------
# load_health / save_health tests
# ---------------------------------------------------------------------------

def test_health_file_created_on_first_write(tmp_path, monkeypatch):
    """save_health creates parent dirs and the file."""
    fake_file = tmp_path / "subdir" / "provider_health.json"
    monkeypatch.setattr(
        "mtor.worker.provider.HEALTH_FILE",
        fake_file,
    )
    health = {"zhipu": {"state": "closed", "cooldown_until": None}}
    save_health(health)
    assert fake_file.exists()
    assert json.loads(fake_file.read_text()) == health


def test_load_health_missing_file(tmp_path, monkeypatch):
    """load_health returns empty dict when file does not exist."""
    fake_file = tmp_path / "nonexistent.json"
    monkeypatch.setattr(
        "mtor.worker.provider.HEALTH_FILE",
        fake_file,
    )
    result = load_health()
    assert result == {}


def test_load_health_parses_valid_file(tmp_path, monkeypatch):
    """load_health parses a valid JSON health file."""
    fake_file = tmp_path / "health.json"
    monkeypatch.setattr(
        "mtor.worker.provider.HEALTH_FILE",
        fake_file,
    )
    data = {"zhipu": {"state": "open", "cooldown_until": 12345}}
    fake_file.write_text(json.dumps(data))
    result = load_health()
    assert result == data


def test_load_health_corrupt_file_returns_empty(tmp_path, monkeypatch):
    """load_health returns empty dict when file is corrupt."""
    fake_file = tmp_path / "corrupt.json"
    monkeypatch.setattr(
        "mtor.worker.provider.HEALTH_FILE",
        fake_file,
    )
    fake_file.write_text("{ not valid json")
    result = load_health()
    assert result == {}


def test_provider_priority_exports():
    """PROVIDER_PRIORITY is a list of expected provider names."""
    assert PROVIDER_PRIORITY == ["zhipu", "infini", "volcano"]


def test_exit_rate_limited_value():
    """EXIT_RATE_LIMITED is 42 as documented."""
    assert EXIT_RATE_LIMITED == 42
