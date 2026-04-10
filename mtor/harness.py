"""Harness — provider-specific command builders and subprocess runners.

Each provider (claude, codex, goose, etc.) maps to a *harness type* that knows
how to build the CLI command and run it.  The ``run_harness`` function is the
public entry point; it looks up the harness for a provider, builds the command,
and executes it via subprocess.
"""

from __future__ import annotations

import subprocess
import sys
from dataclasses import dataclass, field
from typing import Any

# ---------------------------------------------------------------------------
# Provider → harness mapping
# ---------------------------------------------------------------------------

PROVIDER_HARNESS_MAP: dict[str, str] = {
    "claude": "claude",
    "zhipu": "claude",
    "infini": "claude",
    "volcano": "claude",
    "gemini": "claude",
    "codex": "codex",
    "goose": "goose",
    "droid": "droid",
}

# ---------------------------------------------------------------------------
# Harness result dataclass
# ---------------------------------------------------------------------------


@dataclass
class HarnessResult:
    """Captured output from a harness subprocess run."""

    stdout: str
    stderr: str
    exit_code: int
    provider: str
    command: list[str] = field(default_factory=list)


# ---------------------------------------------------------------------------
# Command builders (one per harness type)
# ---------------------------------------------------------------------------


def _build_claude_command(task: str) -> list[str]:
    """Build a ``claude --print`` command for the task."""
    return ["claude", "--print", "-p", task]


def _build_codex_command(task: str) -> list[str]:
    """Build a ``codex`` CLI command for the task."""
    return ["codex", "--full-auto", task]


def _build_goose_command(task: str) -> list[str]:
    """Build a ``goose`` CLI command for the task."""
    return ["goose", "run", "--name", "ribosome", "--task", task]


def _build_droid_command(task: str) -> list[str]:
    """Build a ``droid`` CLI command for the task."""
    return ["droid", "run", task]


_COMMAND_BUILDERS: dict[str, Any] = {
    "claude": _build_claude_command,
    "codex": _build_codex_command,
    "goose": _build_goose_command,
    "droid": _build_droid_command,
}


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------


def get_harness(provider: str) -> str:
    """Return the harness name for *provider*, or raise ``ValueError``."""
    harness = PROVIDER_HARNESS_MAP.get(provider)
    if harness is None:
        raise ValueError(f"Unknown provider: {provider!r}")
    return harness


def build_command(provider: str, task: str) -> list[str]:
    """Build the CLI command for *provider* to execute *task*."""
    harness = get_harness(provider)
    builder = _COMMAND_BUILDERS.get(harness)
    if builder is None:
        raise ValueError(f"No command builder for harness: {harness!r}")
    return builder(task)


def run_harness(
    provider: str,
    task: str,
    *,
    work_dir: str | None = None,
    env: dict[str, str] | None = None,
    timeout: int = 3600,
) -> HarnessResult:
    """Execute *task* via the harness for *provider*.

    Builds the command, runs it as a subprocess, and returns a
    :class:`HarnessResult` with captured stdout/stderr.
    """
    cmd = build_command(provider, task)

    try:
        proc = subprocess.run(
            cmd,
            capture_output=True,
            text=True,
            cwd=work_dir,
            env=env,
            timeout=timeout,
        )
        return HarnessResult(
            stdout=proc.stdout,
            stderr=proc.stderr,
            exit_code=proc.returncode,
            provider=provider,
            command=cmd,
        )
    except subprocess.TimeoutExpired:
        print(f"[harness] timeout after {timeout}s for provider {provider}", file=sys.stderr)
        return HarnessResult(
            stdout="",
            stderr=f"timeout after {timeout}s",
            exit_code=-1,
            provider=provider,
            command=cmd,
        )
    except FileNotFoundError:
        print(f"[harness] command not found: {cmd[0]}", file=sys.stderr)
        return HarnessResult(
            stdout="",
            stderr=f"command not found: {cmd[0]}",
            exit_code=-1,
            provider=provider,
            command=cmd,
        )
