"""Tests for production harness observation aggregation."""

from __future__ import annotations

from mtor.harness_report import harness_from_workflow_id, summarize_harness_samples


def test_harness_from_workflow_id_uses_dispatch_prefix():
    assert harness_from_workflow_id("pi-glm52-inspect-code-a1-b2") == "pi"
    assert harness_from_workflow_id("claude-glm52-fix-code-a1-b2") == "claude"
    assert harness_from_workflow_id("unstructured") == "unknown"


def test_summary_tracks_readiness_outcomes_latency_retries_and_interventions():
    samples = [
        {
            "harness": "pi",
            "status": "COMPLETED",
            "success": True,
            "verdict": "approved",
            "attempt_count": 1,
            "duration_seconds": 20,
        },
        {
            "harness": "pi",
            "status": "COMPLETED",
            "success": True,
            "verdict": "rejected",
            "attempt_count": 2,
            "duration_seconds": 50,
        },
        {
            "harness": "pi",
            "status": "RUNNING",
            "success": None,
            "verdict": None,
            "attempt_count": 1,
            "duration_seconds": None,
        },
        {
            "harness": "claude",
            "status": "FAILED",
            "success": False,
            "verdict": None,
            "attempt_count": 1,
            "duration_seconds": 90,
        },
    ]

    result = summarize_harness_samples(samples, min_samples=3)

    assert result["pi"] == {
        "total_count": 3,
        "running_count": 1,
        "terminal_count": 2,
        "reviewed_count": 2,
        "approved_count": 1,
        "approval_rate": 0.5,
        "intervention_count": 1,
        "retry_count": 1,
        "median_duration_seconds": 35.0,
        "p95_duration_seconds": 50.0,
        "minimum_samples": 3,
        "remaining_samples": 1,
        "decision_ready": False,
    }
    assert result["claude"]["intervention_count"] == 1
    assert result["claude"]["approval_rate"] == 0.0
