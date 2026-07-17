"""Behavior-level proof that CLI-visible workflow IDs resume deferred tasks."""

from __future__ import annotations

import asyncio
from datetime import timedelta
import uuid

import pytest
from temporalio import activity
from temporalio.api.enums.v1 import EventType, IndexedValueType
from temporalio.api.operatorservice.v1 import AddSearchAttributesRequest
from temporalio.testing import WorkflowEnvironment
from temporalio.worker import Worker, UnsandboxedWorkflowRunner

from mtor.worker.workflow import TranslationWorkflow


@activity.defn(name="translate")
async def deferred_translate(
    task: str,
    provider: str,
    mode: str,
    repo: str | None,
    harness: str,
) -> dict:
    return {
        "task": task,
        "provider": provider,
        "success": True,
        "exit_code": 0,
        "deferred": True,
        "deferred_tool": "operator",
        "session_id": "session-1",
    }


async def _run_decision(decision: str) -> dict:
    task_queue = f"test-deferred-{uuid.uuid4().hex[:8]}"
    workflow_id = f"ribosome-deferred-{uuid.uuid4().hex[:8]}"
    spec = {
        "task_id": workflow_id,
        "task": "Pause for an operator decision",
        "provider": "zhipu",
        "mode": "build",
        "harness": "",
    }
    async with await WorkflowEnvironment.start_time_skipping() as environment:
        await environment.client.operator_service.add_search_attributes(
            AddSearchAttributesRequest(
                namespace="default",
                search_attributes={
                    "mtor_verdict": IndexedValueType.INDEXED_VALUE_TYPE_KEYWORD
                },
            )
        )
        with environment.auto_time_skipping_disabled():
            first_worker = Worker(
                environment.client,
                task_queue=task_queue,
                workflows=[TranslationWorkflow],
                activities=[deferred_translate],
                workflow_runner=UnsandboxedWorkflowRunner(),
                max_cached_workflows=0,
            )
            async with first_worker:
                handle = await environment.client.start_workflow(
                    TranslationWorkflow.run,
                    args=[[spec]],
                    id=workflow_id,
                    task_queue=task_queue,
                    run_timeout=timedelta(seconds=30),
                )
                deadline = asyncio.get_running_loop().time() + 5
                while asyncio.get_running_loop().time() < deadline:
                    history = await handle.fetch_history()
                    if any(
                        event.event_type == EventType.EVENT_TYPE_ACTIVITY_TASK_COMPLETED
                        for event in history.events
                    ):
                        break
                    await asyncio.sleep(0.02)
                else:
                    raise AssertionError("deferred activity did not complete")

            # Queue the operator decision while no worker is running. A fresh
            # worker must replay the same stable identity and consume the signal.
            signal = (
                TranslationWorkflow.approve_task
                if decision == "approve"
                else TranslationWorkflow.reject_task
            )
            await handle.signal(signal, workflow_id)
            async with Worker(
                environment.client,
                task_queue=task_queue,
                workflows=[TranslationWorkflow],
                activities=[deferred_translate],
                workflow_runner=UnsandboxedWorkflowRunner(),
                max_cached_workflows=0,
            ):
                return await handle.result()


@pytest.mark.parametrize(
    ("decision", "verdict", "approved"),
    [
        ("approve", "deferred_approved", True),
        ("reject", "deferred_rejected", False),
    ],
)
def test_workflow_id_resumes_the_matching_deferred_task(
    decision: str, verdict: str, approved: bool
) -> None:
    result = asyncio.run(_run_decision(decision))
    review = result["results"][0]["review"]
    assert review["verdict"] == verdict
    assert review["approved"] is approved
