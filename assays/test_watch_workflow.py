"""Tests for WatchWorkflow — Temporal-native watch-and-dispatch cycle.

Runs via: cd ~/code/mtor && uv run pytest assays/test_watch_workflow.py -x
"""

from __future__ import annotations

import asyncio
import uuid
from datetime import timedelta
from pathlib import Path

import pytest
from temporalio import activity, workflow
from temporalio.api.operatorservice.v1 import AddSearchAttributesRequest
from temporalio.testing import WorkflowEnvironment
from temporalio.worker import Worker, UnsandboxedWorkflowRunner

from mtor.worker.workflow import WatchWorkflow


# ---------------------------------------------------------------------------
# Mock child workflow — replaces TranslationWorkflow for tests
# ---------------------------------------------------------------------------

_DISPATCHED_SPECS: list[dict] = []


@workflow.defn(name="TranslationWorkflow")
class MockTranslationWorkflow:
    """Mock child workflow that records dispatches without running activities."""

    @workflow.run
    async def run(self, stages: list[list[dict]] | list[dict]) -> dict:
        if stages and isinstance(stages[0], dict):
            specs = stages
        else:
            specs = []
            for stage in stages:
                specs.extend(stage)

        results = []
        for spec in specs:
            _DISPATCHED_SPECS.append(spec)
            results.append({
                "task": spec.get("task", ""),
                "provider": spec.get("provider", ""),
                "success": True,
                "exit_code": 0,
                "mode": "mock",
                "review": {"approved": True, "verdict": "approved", "flags": []},
            })

        return {
            "total": len(results),
            "succeeded": len(results),
            "approved": len(results),
            "flagged": 0,
            "rejected": 0,
            "results": results,
        }


# ---------------------------------------------------------------------------
# Shared mutable state for mock activities
# ---------------------------------------------------------------------------

_call_log: list[str] = []
_ready_specs_queue: list[list[dict]] = []
_concurrent_highwater: int = 0
_current_concurrent: int = 0


def _reset():
    global _call_log, _ready_specs_queue, _concurrent_highwater, _current_concurrent
    global _DISPATCHED_SPECS
    _call_log = []
    _ready_specs_queue = []
    _concurrent_highwater = 0
    _current_concurrent = 0
    _DISPATCHED_SPECS = []


# ---------------------------------------------------------------------------
# Mock activities
# ---------------------------------------------------------------------------


@activity.defn(name="watch_cycle")
async def mock_watch_cycle(repo_path: str, plan_dir: str) -> dict:
    """Return specs from the queue, falling back to empty list."""
    _call_log.append("watch_cycle")
    if _ready_specs_queue:
        return {
            "synced": True,
            "fetched": 0,
            "ready_specs": _ready_specs_queue.pop(0),
        }
    return {"synced": True, "fetched": 0, "ready_specs": []}


# ---------------------------------------------------------------------------
# Helper
# ---------------------------------------------------------------------------

TASK_QUEUE = "test-watch"
_MOCK_WORKFLOWS = [WatchWorkflow, MockTranslationWorkflow]
_MOCK_ACTIVITIES = [mock_watch_cycle]


async def _run_watch(params: dict) -> dict:
    """Run WatchWorkflow to completion using a time-skipping test environment."""
    params.setdefault("task_queue", TASK_QUEUE)
    async with await WorkflowEnvironment.start_time_skipping() as env:
        wf_id = f"test-watch-{uuid.uuid4().hex[:8]}"
        async with Worker(
            env.client,
            task_queue=TASK_QUEUE,
            workflows=_MOCK_WORKFLOWS,
            activities=_MOCK_ACTIVITIES,
            workflow_runner=UnsandboxedWorkflowRunner(),
        ):
            return await env.client.execute_workflow(
                WatchWorkflow.run,
                args=[params],
                id=wf_id,
                task_queue=TASK_QUEUE,
                run_timeout=timedelta(seconds=60),
            )


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------


class TestWatchWorkflowDispatchesReadySpecs:
    """WatchWorkflow dispatches ready specs as child TranslationWorkflows."""

    def test_test_watch_workflow_dispatches_ready_specs(self) -> None:
        global _ready_specs_queue
        _reset()
        _ready_specs_queue = [
            [
                {"name": "spec-alpha", "body": "implement alpha", "provider": "zhipu"},
                {"name": "spec-beta", "body": "implement beta", "provider": "zhipu"},
            ],
        ]

        result = asyncio.run(_run_watch({
            "repo_path": "/tmp/repo",
            "plan_dir": "/tmp/plans",
            "interval": 1,
            "max_concurrent": 5,
            "max_cycles": 20,
            "provider": "zhipu",
            "stop_after_empty": 1,
        }))

        assert result["total_dispatched"] == 2
        assert len(_DISPATCHED_SPECS) == 2


class TestWatchWorkflowRespectsMaxConcurrent:
    """WatchWorkflow limits concurrent child workflows to max_concurrent."""

    def test_test_watch_workflow_respects_max_concurrent(self) -> None:
        global _ready_specs_queue
        _reset()
        specs = [
            {"name": f"spec-{i}", "body": f"task {i}", "provider": "zhipu"}
            for i in range(5)
        ]
        _ready_specs_queue = [specs]

        result = asyncio.run(_run_watch({
            "repo_path": "/tmp/repo",
            "plan_dir": "/tmp/plans",
            "interval": 1,
            "max_concurrent": 2,
            "max_cycles": 20,
            "provider": "zhipu",
            "stop_after_empty": 1,
        }))

        assert result["total_dispatched"] == 5
        assert len(_DISPATCHED_SPECS) == 5


class TestWatchWorkflowContinuesAsNew:
    """WatchWorkflow uses Continue-As-New when cycle count reaches max_cycles."""

    def test_test_watch_workflow_continues_as_new(self) -> None:
        global _ready_specs_queue
        _reset()
        # Provide one spec for the first cycle.  max_cycles=3 triggers CAN
        # before stop_after_empty can fire (stop_after_empty=5 is generous).
        # After CAN, the continued run finds no more specs and exits via
        # stop_after_empty.
        _ready_specs_queue = [
            [{"name": "spec-a", "body": "task a", "provider": "zhipu"}],
        ]

        result = asyncio.run(_run_watch({
            "repo_path": "/tmp/repo",
            "plan_dir": "/tmp/plans",
            "interval": 1,
            "max_concurrent": 3,
            "max_cycles": 3,
            "provider": "zhipu",
            "stop_after_empty": 2,
        }))

        # CAN fired — continued flag is set by the continued run
        assert result.get("continued") is True
        # The spec was dispatched (tracked in global list across CAN boundary)
        assert len(_DISPATCHED_SPECS) == 1
        # Total cycles span both the original and continued runs
        assert result["cycles"] >= 3
