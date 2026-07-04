"""Graceful drain on SIGTERM — worker shutdown must not kill in-flight ribosome tasks."""

from __future__ import annotations

import asyncio
import os
import signal
from datetime import timedelta
from unittest.mock import AsyncMock, MagicMock, patch

from mtor.worker.translocase import _worker_shutdown_requested, main


class FakeWorker:
    """Stands in for temporalio Worker: run() blocks until shutdown()."""

    def __init__(self, *args, **kwargs):
        self.kwargs = kwargs
        self.shutdown_called = False
        self._release = None

    async def run(self):
        self._release = asyncio.Event()
        await self._release.wait()

    async def shutdown(self):
        self.shutdown_called = True
        if self._release is not None:
            self._release.set()


class ImmediateWorker(FakeWorker):
    """run() returns immediately — worker exits on its own."""

    async def run(self):
        return None


def _fake_client_cls():
    fake = MagicMock()
    fake.connect = AsyncMock(return_value=MagicMock())
    return fake


def test_main_sets_graceful_shutdown_timeout(monkeypatch):
    monkeypatch.delenv("MTOR_GRACEFUL_SHUTDOWN_SECONDS", raising=False)
    captured = {}

    def make_worker(*args, **kwargs):
        captured.update(kwargs)
        return ImmediateWorker(*args, **kwargs)

    with patch("mtor.worker.translocase.Client", _fake_client_cls()), \
         patch("mtor.worker.translocase.Worker", side_effect=make_worker), \
         patch("mtor.worker.translocase._gc_worktrees"):
        asyncio.run(main())

    assert captured["graceful_shutdown_timeout"] == timedelta(seconds=540)


def test_main_drain_seconds_env_override(monkeypatch):
    monkeypatch.setenv("MTOR_GRACEFUL_SHUTDOWN_SECONDS", "60")
    captured = {}

    def make_worker(*args, **kwargs):
        captured.update(kwargs)
        return ImmediateWorker(*args, **kwargs)

    with patch("mtor.worker.translocase.Client", _fake_client_cls()), \
         patch("mtor.worker.translocase.Worker", side_effect=make_worker), \
         patch("mtor.worker.translocase._gc_worktrees"):
        asyncio.run(main())

    assert captured["graceful_shutdown_timeout"] == timedelta(seconds=60)


def test_sigterm_triggers_graceful_drain(monkeypatch):
    monkeypatch.delenv("MTOR_GRACEFUL_SHUTDOWN_SECONDS", raising=False)
    workers = []

    def make_worker(*args, **kwargs):
        worker = FakeWorker(*args, **kwargs)
        workers.append(worker)
        return worker

    async def scenario():
        task = asyncio.create_task(main())
        await asyncio.sleep(0.2)  # let main() install signal handlers
        os.kill(os.getpid(), signal.SIGTERM)
        await asyncio.wait_for(task, timeout=5)

    with patch("mtor.worker.translocase.Client", _fake_client_cls()), \
         patch("mtor.worker.translocase.Worker", side_effect=make_worker), \
         patch("mtor.worker.translocase._gc_worktrees"):
        asyncio.run(scenario())

    assert workers and workers[0].shutdown_called is True


def test_worker_shutdown_requested_false_outside_activity():
    assert _worker_shutdown_requested() is False
