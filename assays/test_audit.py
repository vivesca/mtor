"""Tests for the mtor audit command."""

from __future__ import annotations

import io
import json
import sys
from pathlib import Path

from mtor.cli import app


def invoke(args: list[str]) -> tuple[int, dict]:
    captured = io.StringIO()
    old_stdout = sys.stdout
    exit_code = 0
    try:
        sys.stdout = captured
        app(args)
    except SystemExit as exc:
        exit_code = exc.code if isinstance(exc.code, int) else 1
    finally:
        sys.stdout = old_stdout
    return exit_code, json.loads(captured.getvalue())


def _write_jsonl(path: Path, rows: list[dict | str]) -> None:
    lines = []
    for row in rows:
        lines.append(row if isinstance(row, str) else json.dumps(row))
    path.write_text("\n".join(lines) + "\n", encoding="utf-8")


def test_summarize_audit_counts_reviews_and_runs(tmp_path):
    from mtor.audit import summarize_audit

    runs = tmp_path / "runs.jsonl"
    reviews = tmp_path / "reviews.jsonl"
    _write_jsonl(
        runs,
        [
            {"provider": "zhipu", "exit_code": 0},
            {"provider": "volcano", "exit_code": 1},
            "{bad json",
        ],
    )
    _write_jsonl(
        reviews,
        [
            {"provider": "zhipu", "verdict": "approved"},
            {"provider": "infini", "verdict": "rejected"},
            "{bad json",
        ],
    )

    result = summarize_audit(runs, reviews)

    assert result["runs_total"] == 2
    assert result["reviews_total"] == 2
    assert result["bad_runs"] == 1
    assert result["bad_reviews"] == 1
    assert result["run_providers"] == {"zhipu": 1, "volcano": 1}


def test_summarize_audit_parses_compact_diffstat_buckets(tmp_path):
    from mtor.audit import summarize_audit

    runs = tmp_path / "runs.jsonl"
    reviews = tmp_path / "reviews.jsonl"
    _write_jsonl(runs, [])
    _write_jsonl(
        reviews,
        [
            {"verdict": "approved", "diff": ""},
            {
                "verdict": "rejected",
                "diff": " foo.py | 15 +++++\n bar.py | 10 -----\n 2 files changed, 25 insertions(+)",
            },
            {
                "verdict": "approved_with_flags",
                "diff": " foo.py | 41 +---",
                "flags": ["file_shrunk: foo.py +1/-40"],
            },
        ],
    )

    result = summarize_audit(runs, reviews)
    buckets = result["buckets"]

    assert buckets["approved_empty_diff"] == 1
    assert buckets["rejected_nontrivial_diff"] == 1
    assert buckets["approved_with_blocking_flags"] == 1
    assert result["top_flags"] == {"file_shrunk: foo.py +1/-40": 1}


def test_blocking_flags_bucket_is_a_destruction_tripwire(tmp_path):
    """An approve-class verdict carrying a destruction flag is the invariant
    violation the blocking-flags bucket exists to catch — and must not be
    diluted into the warnings bucket."""
    from mtor.audit import summarize_audit

    runs = tmp_path / "runs.jsonl"
    reviews = tmp_path / "reviews.jsonl"
    _write_jsonl(runs, [])
    _write_jsonl(
        reviews,
        [
            # Invariant violation: approve-class verdict + destruction flag.
            {"verdict": "approved", "diff": " x.sh | 1 +", "flags": ["destruction: rm -rf"]},
            {
                "verdict": "approved_with_flags",
                "diff": " y.sh | 1 +",
                "flags": ["destruction: deleted all"],
            },
        ],
    )

    buckets = summarize_audit(runs, reviews)["buckets"]

    assert buckets["approved_with_blocking_flags"] == 2
    assert buckets["approved_with_warnings"] == 0


def test_non_blocking_warnings_do_not_count_as_blocking(tmp_path):
    """approved_with_flags carrying only warnings (placeholders, hardcoded
    paths) is informational — it must land in approved_with_warnings, NOT the
    blocking tripwire. This is the false alarm the old bucket produced."""
    from mtor.audit import summarize_audit

    runs = tmp_path / "runs.jsonl"
    reviews = tmp_path / "reviews.jsonl"
    _write_jsonl(runs, [])
    _write_jsonl(
        reviews,
        [
            {"verdict": "approved_with_flags", "diff": " a.py | 5 +", "flags": ["placeholders: TODO"]},
            {"verdict": "approved", "diff": " b.py | 5 +", "flags": ["hardcoded_home_path"]},
        ],
    )

    buckets = summarize_audit(runs, reviews)["buckets"]

    assert buckets["approved_with_warnings"] == 2
    assert buckets["approved_with_blocking_flags"] == 0


def test_summarize_audit_lists_large_logs_without_reading_contents(tmp_path):
    from mtor.audit import summarize_audit

    runs = tmp_path / "runs.jsonl"
    reviews = tmp_path / "reviews.jsonl"
    logs = tmp_path / "logs"
    logs.mkdir()
    _write_jsonl(runs, [])
    _write_jsonl(reviews, [])
    (logs / "small.log").write_bytes(b"x" * 3)
    (logs / "large.log").write_bytes(b"x" * 8)

    result = summarize_audit(runs, reviews, logs, limit=1)

    assert result["large_logs"] == [{"filename": "large.log", "size": 8}]


def test_audit_cli_returns_json_envelope(tmp_path):
    runs = tmp_path / "runs.jsonl"
    reviews = tmp_path / "reviews.jsonl"
    logs = tmp_path / "logs"
    logs.mkdir()
    _write_jsonl(runs, [{"provider": "zhipu"}])
    _write_jsonl(reviews, [{"verdict": "approved"}])

    exit_code, data = invoke([
        "audit",
        "--runs",
        str(runs),
        "--reviews",
        str(reviews),
        "--logs-dir",
        str(logs),
    ])

    assert exit_code == 0
    assert data["ok"] is True
    assert data["result"]["reviews_total"] == 1
