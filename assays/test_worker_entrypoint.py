"""Console entry-point coverage for the async Temporal worker."""

from __future__ import annotations

import subprocess
import sys
from pathlib import Path

from mtor.worker import translocase


def test_sync_entrypoint_awaits_async_main(monkeypatch) -> None:
    completed = False

    async def fake_main() -> None:
        nonlocal completed
        completed = True

    monkeypatch.setattr(translocase, "main", fake_main)

    translocase.run()

    assert completed is True


def test_installed_worker_help_uses_sync_entrypoint() -> None:
    script = Path(sys.executable).with_name("mtor-worker")
    assert script.is_file()

    result = subprocess.run(
        [script, "--help"],
        capture_output=True,
        text=True,
        check=False,
    )
    combined = result.stdout + result.stderr

    assert result.returncode == 0
    assert "Temporal" in result.stdout
    assert "coroutine object" not in combined
    assert "never awaited" not in combined
