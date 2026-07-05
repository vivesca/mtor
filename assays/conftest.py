"""Session-wide safety net: no test may issue a real subprocess call that
touches the production ganglion host or its mtor-worker service.

A refactor moved `_check_worker_sha()`'s and `deploy()`'s worker-restart step
to call the shared `mtor.infra.restart_worker()` helper. Existing tests
mocked `mtor.dispatch.subprocess` / `mtor.cli.subprocess`, which no longer
intercepts that call -- it now runs inside `mtor.infra`, using `mtor.infra`'s
own `subprocess` reference. On a host with real SSH access to ganglion
(unlike CI, which fails on DNS resolution), those "hermetic" tests silently
executed REAL restarts of the production mtor-worker service (confirmed live
2026-07-05: 4+ real restart cycles from a single local `pytest assays/` run,
no in-flight ribosome killed only by luck of timing). This fixture is the
backstop: even if a future refactor reintroduces an unmocked call path, any
test invoking the real `subprocess.run` with ganglion- or mtor-worker-shaped
arguments fails loudly instead of touching production.
"""

from __future__ import annotations

import subprocess

import pytest

_DANGEROUS_MARKERS = ("ganglion", "mtor-worker", "systemctl --user")


@pytest.fixture(autouse=True)
def _block_real_worker_calls(monkeypatch):
    real_run = subprocess.run

    def _guarded_run(cmd, *args, **kwargs):
        if isinstance(cmd, (list, tuple)):
            cmd_str = " ".join(str(part) for part in cmd)
        else:
            cmd_str = str(cmd)
        if any(marker in cmd_str for marker in _DANGEROUS_MARKERS):
            raise RuntimeError(
                "BLOCKED by assays/conftest.py safety fixture: a test attempted "
                f"a REAL subprocess call touching the production worker: {cmd_str!r}. "
                "A mock is missing in the call chain -- patch the function or "
                "module that actually issues this command (e.g. restart_worker "
                "at its import site), not just a sibling `subprocess` reference."
            )
        return real_run(cmd, *args, **kwargs)

    monkeypatch.setattr(subprocess, "run", _guarded_run)
