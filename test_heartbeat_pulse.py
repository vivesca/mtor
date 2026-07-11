"""Heartbeat resilience: a dedicated pulse task that nothing can starve.

Wave-1 2026-07-11 post-mortem: activity.heartbeat() was sent at the tail of
each 30 s stall-check tick, after a git-diff computation on the shared event
loop; a >3 min delivery stall let Temporal SIGKILL a healthy worker.
"""

import asyncio
from datetime import timedelta

from mtor.worker.translocase import _heartbeat_pulse
from mtor.worker.workflow import _resolve_heartbeat_timeout


def test_pulse_beats_repeatedly():
    beats = []

    async def run():
        task = asyncio.create_task(
            _heartbeat_pulse("label", interval_s=0.01, beat=lambda: beats.append(1))
        )
        await asyncio.sleep(0.06)
        task.cancel()

    asyncio.run(run())
    assert len(beats) >= 3


def test_pulse_survives_beat_exceptions():
    calls = []

    def flaky_beat():
        calls.append(1)
        raise RuntimeError("transient RPC failure")

    async def run():
        task = asyncio.create_task(
            _heartbeat_pulse("label", interval_s=0.01, beat=flaky_beat)
        )
        await asyncio.sleep(0.05)
        task.cancel()

    asyncio.run(run())
    assert len(calls) >= 2


def test_resolve_heartbeat_timeout_prefers_newest_patch():
    assert _resolve_heartbeat_timeout(lambda marker: True) == timedelta(minutes=6)
    assert _resolve_heartbeat_timeout(
        lambda marker: marker == "translate-heartbeat-3m"
    ) == timedelta(minutes=3)
    assert _resolve_heartbeat_timeout(lambda marker: False) == timedelta(minutes=15)
