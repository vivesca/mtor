"""Translate activity heartbeat timeout is 3m, gated behind workflow.patched()."""

from __future__ import annotations

import inspect

from mtor.worker import workflow


def test_translate_heartbeat_gated_by_patch():
    """New executions get a 3m heartbeat; replays keep 15m via patched() gate."""
    source = inspect.getsource(workflow.TranslationWorkflow._execute_one)

    assert 'workflow.patched("translate-heartbeat-3m")' in source
    assert "timedelta(minutes=3)" in source
    assert "timedelta(minutes=15)" in source


def test_translate_activity_uses_gated_heartbeat_variable():
    """The execute_activity call must use the gated variable, not a literal."""
    source = inspect.getsource(workflow.TranslationWorkflow._execute_one)

    assert "heartbeat_timeout=heartbeat_timeout" in source
    assert "heartbeat_timeout=timedelta(minutes=15)" not in source
