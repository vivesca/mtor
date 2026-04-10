"""Ribosome — Python effector that replaces the bash ribosome script.

Orchestrates task execution: loads coaching notes, builds the harness command,
runs the provider, and handles fallback on failure.
"""

from __future__ import annotations

import os
import sys
from typing import Any

from mtor import COACHING_MAX_KB, COACHING_PATH
from mtor.coaching import load_coaching
from mtor.harness import (
    PROVIDER_HARNESS_MAP,
    run_harness,
)

# Providers that support fallback (ordered by priority).
_FALLBACK_ORDER: list[str] = ["zhipu", "infini", "volcano", "gemini"]


def _inject_coaching(task: str, coaching: str | None) -> str:
    """Prepend *coaching* text to *task* if available."""
    if not coaching:
        return task
    return coaching + "\n\n" + task


def execute(
    task: str,
    provider: str = "zhipu",
    mode: str = "build",
    *,
    coaching_path: str | None = None,
    coaching_max_kb: int = COACHING_MAX_KB,
    work_dir: str | None = None,
    env: dict[str, str] | None = None,
    fallback: bool = True,
) -> dict[str, Any]:
    """Execute a task via the appropriate provider harness.

    1. Load coaching notes for the provider.
    2. Inject coaching into the task prompt.
    3. For ``explore`` mode, append a read-only suffix.
    4. Run the harness.  On failure with *fallback*=True, retry with the
       next provider in ``_FALLBACK_ORDER``.

    Returns a dict with keys: ``success``, ``exit_code``, ``provider``,
    ``stdout``, ``stderr``, ``mode``, ``command``.
    """
    # Resolve coaching path: explicit arg > module constant
    cpath = coaching_path or COACHING_PATH

    # Load coaching for this provider
    coaching = load_coaching(cpath, provider, max_kb=coaching_max_kb)

    # Inject coaching into the task
    full_task = _inject_coaching(task, coaching)

    # Explore mode: append read-only instruction
    if mode == "explore":
        full_task += (
            "\n\nThis is a READ-ONLY exploration task. Do NOT modify any files. "
            "Report your findings as structured output."
        )

    # Build effective env
    effective_env: dict[str, str] | None = None
    if env:
        effective_env = {**os.environ, **env}
    effective_env = effective_env or dict(os.environ)
    effective_env["RIBOSOME_PROVIDER"] = provider

    # Run the harness
    result = run_harness(
        provider,
        full_task,
        work_dir=work_dir,
        env=effective_env,
    )

    # Fallback on failure: try next provider in priority order
    if result.exit_code != 0 and fallback:
        tried = {provider}
        for fb_provider in _FALLBACK_ORDER:
            if fb_provider in tried:
                continue
            if fb_provider not in PROVIDER_HARNESS_MAP:
                continue
            tried.add(fb_provider)
            print(
                f"[ribosome] fallback: trying {fb_provider}",
                file=sys.stderr,
            )
            # Reload coaching for fallback provider
            fb_coaching = load_coaching(cpath, fb_provider, max_kb=coaching_max_kb)
            fb_task = _inject_coaching(task, fb_coaching)
            if mode == "explore":
                fb_task += (
                    "\n\nThis is a READ-ONLY exploration task. Do NOT modify any files. "
                    "Report your findings as structured output."
                )
            fb_env = dict(effective_env)
            fb_env["RIBOSOME_PROVIDER"] = fb_provider
            fb_result = run_harness(
                fb_provider,
                fb_task,
                work_dir=work_dir,
                env=fb_env,
            )
            if fb_result.exit_code == 0:
                result = fb_result
                break

    return {
        "success": result.exit_code == 0,
        "exit_code": result.exit_code,
        "provider": result.provider,
        "stdout": result.stdout,
        "stderr": result.stderr,
        "mode": mode,
        "command": result.command,
    }
