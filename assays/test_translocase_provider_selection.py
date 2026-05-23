"""Tests for translocase provider-attempt selection."""

from __future__ import annotations

from mtor.worker.translocase import _select_attempt_provider


def test_first_attempt_uses_requested_provider_even_when_round_robin_would_choose_other():
    health = {"zhipu": {"state": "closed"}, "gemini": {"state": "closed"}, "_rr_index": 1}

    assert _select_attempt_provider(health, "zhipu", set()) == "zhipu"


def test_retry_after_requested_provider_can_fall_back_to_unattempted_provider():
    health = {"zhipu": {"state": "closed"}, "gemini": {"state": "closed"}}

    assert _select_attempt_provider(health, "zhipu", {"zhipu"}) == "zhipu"


def test_unrequested_dispatch_uses_router():
    health = {"zhipu": {"state": "closed"}, "gemini": {"state": "closed"}, "_rr_index": 1}

    assert _select_attempt_provider(health, "", set()) == "zhipu"
