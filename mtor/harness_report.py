"""Pure aggregation helpers for production harness observations."""

from __future__ import annotations

import math
from collections import defaultdict
from statistics import median
from typing import Any


APPROVED_VERDICTS = frozenset({"approved", "approved_with_flags"})


def harness_from_workflow_id(workflow_id: str) -> str:
    """Return the harness prefix embedded by mTOR's workflow ID builder."""
    return workflow_id.split("-", 1)[0] if "-" in workflow_id else "unknown"


def _percentile_nearest_rank(values: list[float], percentile: float) -> float | None:
    if not values:
        return None
    ordered = sorted(values)
    rank = max(1, math.ceil(percentile * len(ordered)))
    return round(ordered[rank - 1], 2)


def summarize_harness_samples(
    samples: list[dict[str, Any]], *, min_samples: int
) -> dict[str, dict[str, Any]]:
    """Aggregate outcome, latency, retry, and intervention signals by harness."""
    grouped: dict[str, list[dict[str, Any]]] = defaultdict(list)
    for sample in samples:
        grouped[str(sample.get("harness") or "unknown")].append(sample)

    summaries: dict[str, dict[str, Any]] = {}
    for harness, rows in sorted(grouped.items()):
        terminal = [row for row in rows if row.get("status") != "RUNNING"]
        durations = [
            float(row["duration_seconds"])
            for row in terminal
            if row.get("duration_seconds") is not None
        ]
        approved = sum(
            str(row.get("verdict") or "") in APPROVED_VERDICTS for row in terminal
        )
        reviewed = sum(bool(row.get("verdict")) for row in terminal)
        interventions = sum(
            row.get("status") != "COMPLETED"
            or row.get("success") is False
            or (
                bool(row.get("verdict"))
                and str(row.get("verdict")) not in APPROVED_VERDICTS
            )
            for row in terminal
        )
        retries = sum(max(0, int(row.get("attempt_count") or 1) - 1) for row in rows)
        terminal_count = len(terminal)
        summaries[harness] = {
            "total_count": len(rows),
            "running_count": len(rows) - terminal_count,
            "terminal_count": terminal_count,
            "reviewed_count": reviewed,
            "approved_count": approved,
            "approval_rate": (
                round(approved / terminal_count, 3) if terminal_count else None
            ),
            "intervention_count": interventions,
            "retry_count": retries,
            "median_duration_seconds": (
                round(float(median(durations)), 2) if durations else None
            ),
            "p95_duration_seconds": _percentile_nearest_rank(durations, 0.95),
            "minimum_samples": min_samples,
            "remaining_samples": max(0, min_samples - terminal_count),
            "decision_ready": terminal_count >= min_samples,
        }
    return summaries
