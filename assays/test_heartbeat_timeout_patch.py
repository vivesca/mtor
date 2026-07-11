"""Translate activity heartbeat timeout is patch-gated via _resolve_heartbeat_timeout."""

from __future__ import annotations

import inspect
from datetime import timedelta

from mtor.worker import workflow


def test_translate_heartbeat_ladder_gated_by_patch():
    """New executions get 6m; 3m-era and pre-patch replays keep their timeouts."""
    assert workflow._resolve_heartbeat_timeout(lambda marker: True) == timedelta(
        minutes=6
    )
    assert workflow._resolve_heartbeat_timeout(
        lambda marker: marker == "translate-heartbeat-3m"
    ) == timedelta(minutes=3)
    assert workflow._resolve_heartbeat_timeout(lambda marker: False) == timedelta(
        minutes=15
    )


def test_execute_one_uses_resolved_timeout():
    """_execute_one resolves the ladder; no timeout literals at the call site."""
    source = inspect.getsource(workflow.TranslationWorkflow._execute_one)

    assert "_resolve_heartbeat_timeout(workflow.patched)" in source
    assert "heartbeat_timeout=heartbeat_timeout" in source
    assert "heartbeat_timeout=timedelta(minutes=15)" not in source
