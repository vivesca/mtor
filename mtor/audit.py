"""Read-only audit summaries for ribosome run and review ledgers."""

from __future__ import annotations

import json
import re
from collections import Counter
from pathlib import Path
from typing import Any


BUCKET_NAMES = (
    "approved_with_blocking_flags",
    "approved_empty_diff",
    "rejected_nontrivial_diff",
    "incomplete_with_branch",
    "success_no_commit",
    "exit0_empty_diff",
)


def _read_jsonl(path: Path) -> tuple[list[dict[str, Any]], int]:
    rows: list[dict[str, Any]] = []
    bad = 0
    if not path.exists():
        return rows, bad

    for line in path.read_text(encoding="utf-8").splitlines():
        if not line.strip():
            continue
        try:
            value = json.loads(line)
        except json.JSONDecodeError:
            bad += 1
            continue
        if isinstance(value, dict):
            rows.append(value)
        else:
            bad += 1
    return rows, bad


def _counter_values(rows: list[dict[str, Any]], *keys: str) -> dict[str, int]:
    counts: Counter[str] = Counter()
    for row in rows:
        for key in keys:
            value = row.get(key)
            if value is not None and value != "":
                counts[str(value)] += 1
                break
    return dict(counts)


def _diff_changed_lines(diff: str) -> int:
    if not diff.strip():
        return 0

    changed = 0
    for match in re.finditer(r"(\d+)\s+(?:insertion|insertions|deletion|deletions)", diff):
        changed += int(match.group(1))
    if changed:
        return changed

    for line in diff.splitlines():
        if "|" not in line:
            continue
        match = re.search(r"\|\s+(\d+)\s+", line)
        if match:
            changed += int(match.group(1))
    return changed


def _has_commit(row: dict[str, Any]) -> bool:
    return any(row.get(key) for key in ("commit", "commit_sha", "merged_commit", "merge_commit"))


def _flags(row: dict[str, Any]) -> list[str]:
    value = row.get("flags", [])
    if isinstance(value, list):
        return [str(flag) for flag in value]
    if isinstance(value, str):
        return [part.strip() for part in re.split(r"[,;]\s*", value) if part.strip()]
    return []


def _large_logs(logs_dir: Path | None, limit: int) -> list[dict[str, Any]]:
    if logs_dir is None or not logs_dir.exists():
        return []
    entries = []
    for path in logs_dir.iterdir():
        if not path.is_file():
            continue
        stat = path.stat()
        entries.append({"filename": path.name, "size": stat.st_size})
    entries.sort(key=lambda item: item["size"], reverse=True)
    return entries[:limit]


def summarize_audit(
    runs_path: Path,
    reviews_path: Path,
    logs_dir: Path | None = None,
    *,
    limit: int = 10,
) -> dict:
    """Summarize run/review ledgers without external services or shelling out."""
    runs, bad_runs = _read_jsonl(runs_path)
    reviews, bad_reviews = _read_jsonl(reviews_path)

    buckets = {name: 0 for name in BUCKET_NAMES}
    top_flags: Counter[str] = Counter()

    for review in reviews:
        verdict = str(review.get("verdict") or review.get("status") or "").lower()
        diff = str(review.get("diff") or review.get("patch") or "")
        flags = _flags(review)
        top_flags.update(flags)

        if verdict == "approved_with_flags" or (verdict == "approved" and flags):
            buckets["approved_with_blocking_flags"] += 1
        if verdict in {"approved", "accepted", "approved_with_flags"} and not diff.strip():
            buckets["approved_empty_diff"] += 1
        if verdict in {"rejected", "failed"} and _diff_changed_lines(diff) > 20:
            buckets["rejected_nontrivial_diff"] += 1
        if review.get("exit_code") == 0 and not diff.strip():
            buckets["exit0_empty_diff"] += 1

    for run in runs:
        status = str(run.get("status") or "").lower()
        if run.get("branch") and status in {"incomplete", "failed", "running", "canceled", "cancelled"}:
            buckets["incomplete_with_branch"] += 1
        if run.get("success") is True and not _has_commit(run):
            buckets["success_no_commit"] += 1
        if run.get("exit_code") == 0 and not str(run.get("diff") or "").strip():
            buckets["exit0_empty_diff"] += 1

    return {
        "runs_total": len(runs),
        "reviews_total": len(reviews),
        "bad_runs": bad_runs,
        "bad_reviews": bad_reviews,
        "run_providers": _counter_values(runs, "provider"),
        "review_providers": _counter_values(reviews, "provider"),
        "verdicts": _counter_values(reviews, "verdict", "status"),
        "exit_codes": _counter_values(runs + reviews, "exit_code"),
        "top_flags": dict(top_flags.most_common(limit)),
        "buckets": buckets,
        "large_logs": _large_logs(logs_dir, limit),
    }
