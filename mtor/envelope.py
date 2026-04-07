"""JSON envelope helpers — ok/error shapes for all CLI output."""

from __future__ import annotations

import json
from typing import Any

from porin import emit_err
from porin import ok as _porin_ok


def _ok(
    command: str,
    result: dict[str, Any],
    next_actions: list[dict] | None = None,
    version: str | None = None,
) -> None:
    print(json.dumps(_porin_ok(command, result, next_actions, version)))


def _err(
    command: str,
    message: str,
    code: str,
    fix: str,
    next_actions: list[dict] | None = None,
    exit_code: int = 1,
) -> int:
    emit_err(command, message, code, fix, next_actions)
    return exit_code


def _extract_first_result(wf_result: dict) -> dict | None:
    """Extract the first task result from the batch envelope."""
    results = wf_result.get("results")
    if isinstance(results, list) and results:
        return results[0]
    # Flat result (direct task output, not batch envelope)
    if "exit_code" in wf_result:
        return wf_result
    return None
