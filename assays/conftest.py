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

_DANGEROUS_RESTART_SUBSTRING = "systemctl --user restart mtor-worker"


def _is_dangerous_command(cmd) -> tuple[bool, str]:
    """True if *cmd* is a real restart of the production worker, or a real
    ssh call whose target is the literal worker host "ganglion".

    Narrower than blanket substring matching on "ganglion" or "mtor-worker":
    those also match local test fixtures (e.g. a scratch git repo cloned into
    a tmp dir literally named "ganglion" in test_sync.py/test_watch.py) and
    pre-existing unmocked local health-probe calls (systemctl --user show,
    not restart) elsewhere in the suite -- neither of which touches
    production and neither of which this fixture is meant to catch.
    """
    if isinstance(cmd, (list, tuple)):
        parts = [str(part) for part in cmd]
    else:
        parts = [str(cmd)]
    joined = " ".join(parts)
    if _DANGEROUS_RESTART_SUBSTRING in joined:
        return True, joined
    if parts and parts[0] == "ssh" and "ganglion" in parts:
        return True, joined
    return False, joined


@pytest.fixture(autouse=True)
def _block_real_worker_calls(monkeypatch):
    real_run = subprocess.run

    def _guarded_run(cmd, *args, **kwargs):
        dangerous, cmd_str = _is_dangerous_command(cmd)
        if dangerous:
            raise RuntimeError(
                "BLOCKED by assays/conftest.py safety fixture: a test attempted "
                f"a REAL subprocess call touching the production worker: {cmd_str!r}. "
                "A mock is missing in the call chain -- patch the function or "
                "module that actually issues this command (e.g. restart_worker "
                "at its import site), not just a sibling `subprocess` reference."
            )
        return real_run(cmd, *args, **kwargs)

    monkeypatch.setattr(subprocess, "run", _guarded_run)
