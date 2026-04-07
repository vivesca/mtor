#!/usr/bin/env python3
"""Autopoiesis — error-classified auto-heal for TranslationWorkflow.

When a translation task fails, classify the error and determine whether
an automatic retry with adjusted parameters is appropriate.

Error classifications:
  - rate_limit: HTTP 429 / quota exhaustion → fallback provider
  - transient: network errors → retry once (same params)
  - timeout: process killed / deadline → raw mode fallback
  - permanent: syntax errors, destruction → no retry
  - unknown: unclassified failures → no retry
"""

from __future__ import annotations

import re
from dataclasses import dataclass


# ── Error classification patterns ──────────────────────────────────────

_RATE_LIMIT_RE = re.compile(
    r"429|rate.?\s*limit|quota.?\s*exceeded|too.?\s*many.?\s*requests|"
    r"AccountQuotaExceeded|20013|request.?\s*limit.?\s*exceeded|"
    r"usage.?\s*limit|hit your.*limit|quota will reset",
    re.IGNORECASE,
)

_TRANSIENT_RE = re.compile(
    r"ConnectionError|ConnectionRefusedError|connection refused|"
    r"network.*error|connection reset by peer|broken pipe|"
    r"ECONNREFUSED|ECONNRESET|TemporaryFailure",
    re.IGNORECASE,
)

_TIMEOUT_RE = re.compile(
    r"timed?\s*out|timeout|deadline exceeded",
    re.IGNORECASE,
)


def classify_error(result: dict, review: dict) -> str:
    """Classify a failed result into an error category for auto-heal.

    Returns one of: "rate_limit", "transient", "timeout", "permanent", "unknown"
    """
    stderr = result.get("stderr", "")
    stdout = result.get("stdout", "")
    combined = f"{stdout}\n{stderr}"
    exit_code = result.get("exit_code", -1)
    flags = review.get("flags", [])

    # Priority 1: Rate limit (most actionable — fallback provider available)
    if _RATE_LIMIT_RE.search(combined):
        return "rate_limit"

    # Priority 2: Destruction patterns (permanent — retrying won't help)
    if any(f.startswith("destruction") for f in flags):
        return "permanent"

    # Priority 3: Syntax/import errors in flags (permanent — code is wrong)
    if any("SyntaxError" in f or "ImportError" in f for f in flags):
        return "permanent"

    # Priority 4: Timeout (killed process, deadline)
    if _TIMEOUT_RE.search(combined):
        return "timeout"

    # Negative exit code without other patterns → timeout (likely SIGKILL/SIGTERM)
    if exit_code < 0:
        return "timeout"

    # Priority 5: Transient network errors
    if _TRANSIENT_RE.search(combined):
        return "transient"

    return "unknown"


# ── Heal actions ───────────────────────────────────────────────────────

PROVIDER_FALLBACK_CHAIN: dict[str, list[str]] = {
    "zhipu": ["infini", "volcano"],
    "infini": ["zhipu", "volcano"],
    "volcano": ["zhipu", "infini"],
    "gemini": ["zhipu", "codex"],
    "codex": ["zhipu", "gemini"],
}


@dataclass
class HealAction:
    """Describes the auto-heal strategy for a retriable error."""

    strategy: str  # "fallback_provider" | "retry_same" | "fallback_raw"
    max_retries: int
    fallback_provider: str | None = None

    def __str__(self) -> str:
        parts = [f"HealAction(strategy={self.strategy}, max_retries={self.max_retries}"]
        if self.fallback_provider:
            parts.append(f", fallback_provider={self.fallback_provider}")
        parts.append(")")
        return "".join(parts)


def get_heal_action(error_class: str, provider: str) -> HealAction | None:
    """Return the auto-heal strategy for an error class, or None if not retriable."""
    if error_class == "rate_limit":
        chain = PROVIDER_FALLBACK_CHAIN.get(provider, [])
        fallback = chain[0] if chain else None
        return HealAction(
            strategy="fallback_provider", max_retries=1, fallback_provider=fallback
        )

    if error_class == "transient":
        return HealAction(strategy="retry_same", max_retries=1)

    if error_class == "timeout":
        return HealAction(strategy="fallback_raw", max_retries=1)

    # permanent and unknown: no auto-heal
    return None
