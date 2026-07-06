from __future__ import annotations

import subprocess

import pytest

from mtor import WORKER_HOST
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
    """Block real subprocess calls that target the ganglion host or restart the
    production mtor-worker during pytest. Two narrow conditions only:

    1. The joined, lowercased command contains the literal substring
       ``"systemctl --user restart mtor-worker"`` (a real production restart).
    2. The command is a real ``ssh`` invocation whose target host is the literal
       token ``"ganglion"``: for a list command, the first element is exactly
       ``"ssh"`` and the string ``"ganglion"`` appears as one of the other argv
       tokens (exact list-membership, not a substring check on the joined
       command); for a single string command (``shell=True``), the lowercased
       string starts with ``"ssh "`` and contains ``"ganglion"`` as a substring.

    A local tmp directory named ``ganglion`` (e.g. ``tmp_path / "ganglion"``)
    appears as a path-shaped argument value inside various git subprocess calls,
    never as the literal second argv element of an ssh command — so it is NOT
    matched.
    """
    _real_run = subprocess.run

    def _guarded_run(*args, **kwargs):
        command = args[0] if args else kwargs.get("args")
        blocked = False
        if isinstance(command, str):
            joined_lower = command.lower()
            if "systemctl --user restart mtor-worker" in joined_lower:
                blocked = True
            elif joined_lower.startswith("ssh ") and WORKER_HOST.lower() in joined_lower:
                blocked = True
        else:
            joined_lower = " ".join(str(part) for part in command).lower()
            if "systemctl --user restart mtor-worker" in joined_lower:
                blocked = True
            elif len(command) > 0 and command[0] == "ssh" and WORKER_HOST in command[1:]:
                blocked = True
        if blocked:
            raise RuntimeError(
                f"BLOCKED: real subprocess call to ganglion/mtor-worker during pytest: "
                f"{command!r}. Mock mtor.infra.restart_worker (or the correct "
                f"module-qualified subprocess reference) in this test instead."
            )
        return _real_run(*args, **kwargs)

    monkeypatch.setattr(subprocess, "run", _guarded_run)
