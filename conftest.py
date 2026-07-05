from __future__ import annotations

import subprocess

import pytest

from mtor.worker import chaperone_review


@pytest.fixture(autouse=True)
def _isolate_chaperone_review_artifacts(tmp_path, monkeypatch):
    monkeypatch.setattr(
        chaperone_review,
        "REVIEW_LOG",
        tmp_path / "ribosome-reviews.jsonl",
    )
    monkeypatch.setattr(
        chaperone_review,
        "DOSSIER_DIR",
        tmp_path / "ribosome-dossiers",
    )


@pytest.fixture(autouse=True)
def _block_real_ganglion_subprocess_calls(monkeypatch):
    _real_run = subprocess.run

    def _guarded_run(*args, **kwargs):
        command = args[0] if args else kwargs.get("args")
        if isinstance(command, str):
            joined = command.lower()
        else:
            joined = " ".join(str(part) for part in command).lower()
        if "ganglion" in joined or ("mtor-worker" in joined and "restart" in joined):
            raise RuntimeError(
                f"BLOCKED: real subprocess call to ganglion/mtor-worker during pytest: "
                f"{command!r}. Mock mtor.infra.restart_worker (or the correct "
                f"module-qualified subprocess reference) in this test instead."
            )
        return _real_run(*args, **kwargs)

    monkeypatch.setattr(subprocess, "run", _guarded_run)
