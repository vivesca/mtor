#!/usr/bin/env python3
"""Temporal workflow for translation batch dispatch.

TranslationWorkflow accepts a list of tasks, dispatches them as activities
respecting per-provider concurrency, and reviews results before reporting.

Supports two execution modes:
  - "raw" (default): subprocess via `claude --print` (fast, battle-tested)
  - "graph": LangGraph agent with plan→execute→verify→review (structured, auditable)
    NOTE: graph mode is disabled — translate_graph (LangGraph experiment) has been dropped.
"""

from __future__ import annotations

import asyncio
from datetime import timedelta

from temporalio import workflow
from temporalio.common import RetryPolicy, SearchAttributeKey

with workflow.unsafe.imports_passed_through():
    from pathlib import Path

    from mtor.worker.translocase import chaperone, merge_approved, translate, watch_cycle

# #6: Search attributes (registered on server)
SA_PROVIDER = SearchAttributeKey.for_keyword("TranslationProvider")
SA_VERDICT = SearchAttributeKey.for_keyword("TranslationVerdict")
SA_TASK_ID = SearchAttributeKey.for_keyword("TranslationTaskId")

# Retry policy: 2 attempts max (translation tasks mutate files, not safely retriable)
_RETRY_POLICY = RetryPolicy(
    maximum_attempts=2,
    initial_interval=timedelta(seconds=10),
    backoff_coefficient=2.0,
    maximum_interval=timedelta(minutes=5),
)

# Review has no retries — it's local and fast
_REVIEW_RETRY = RetryPolicy(maximum_attempts=1)


@workflow.defn
class TranslationWorkflow:
    """Dispatch a batch of translation tasks, review results, report aggregate."""

    def __init__(self) -> None:
        self._approval_signals: dict[str, str] = {}  # task_id -> "approve"|"reject"

    @workflow.signal
    async def approve_task(self, task_id: str) -> None:
        """Signal to approve a held task."""
        self._approval_signals[task_id] = "approve"

    @workflow.signal
    async def reject_task(self, task_id: str) -> None:
        """Signal to reject a held task."""
        self._approval_signals[task_id] = "reject"

    async def _execute_one(self, spec: dict) -> dict:
        """Execute a single spec, then review the result."""
        task = spec.get("task", "")
        provider = spec.get("provider", "zhipu")
        dispatch_mode = spec.get("mode", "raw")

        # #3: Version guard — new code paths gated behind patched()
        use_review_v2 = workflow.patched("review-v2-slim-payload")

        try:
            # Raw subprocess mode (default)
            result = await workflow.execute_activity(
                translate,
                args=[task, provider, dispatch_mode],
                start_to_close_timeout=timedelta(hours=2),
                heartbeat_timeout=timedelta(minutes=5),
                retry_policy=_RETRY_POLICY,
            )
            # SRP defer: if activity returned deferred, wait for approval signal
            if result.get("deferred"):
                task_id = spec.get("task", "")[:50]
                review = {
                    "approved": False,
                    "verdict": "deferred",
                    "flags": [f"deferred:{result.get('deferred_tool', 'unknown')}"],
                    "session_id": result.get("session_id", ""),
                }
                try:
                    await workflow.wait_condition(
                        lambda tid=task_id: tid in self._approval_signals,
                        timeout=timedelta(minutes=30),
                    )
                    decision = self._approval_signals.get(task_id, "reject")
                    if decision == "approve":
                        review = {**review, "verdict": "deferred_approved", "approved": True}
                    else:
                        review = {**review, "verdict": "deferred_rejected"}
                except TimeoutError:
                    review = {**review, "verdict": "deferred_timeout"}
            else:
                try:
                    review = await workflow.execute_activity(
                        chaperone,
                        args=[result],
                        start_to_close_timeout=timedelta(minutes=2),
                        retry_policy=_REVIEW_RETRY,
                    )
                except Exception:
                    review = {
                        "approved": result.get("success", False),
                        "flags": ["review_failed"],
                        "verdict": "review_error",
                    }

        except Exception as exc:
            result = {
                "task": task[:200],
                "provider": provider,
                "success": False,
                "exit_code": -1,
                "stdout": "",
                "stderr": str(exc)[:2000],
            }
            review = {"approved": False, "flags": ["activity_failed"], "verdict": "rejected"}

        # #6: Upsert search attributes
        workflow.upsert_search_attributes(
            [
                SA_PROVIDER.value_set(provider),
                SA_VERDICT.value_set(review.get("verdict", "unknown")),
                SA_TASK_ID.value_set(task[:50]),
            ]
        )

        # #5: If flagged, wait for approval signal (timeout 1h, auto-approve)
        if review.get("verdict") == "approved_with_flags":
            task_id = spec.get("task", "")[:50]
            try:
                await workflow.wait_condition(
                    lambda: task_id in self._approval_signals,
                    timeout=timedelta(hours=1),
                )
                decision = self._approval_signals.get(task_id, "approve")
                if decision == "reject":
                    review = {**review, "approved": False, "verdict": "rejected_by_signal"}
            except TimeoutError:
                pass  # auto-approve after 1h

        # Wire use_review_v2: pass output_path in review for new executions
        # so dispatch can log it.  Old replays keep current behavior (no output_path).
        if use_review_v2:
            review = {**review, "output_path": result.get("output_path", "")}

        # Merge to main only after chaperone approves (verdict gate)
        if review.get("approved") and result.get("branch_name"):
            try:
                merge_result = await workflow.execute_activity(
                    merge_approved,
                    args=[{
                        "repo_root": str(Path.home() / "germline"),
                        "branch_name": result["branch_name"],
                    }],
                    start_to_close_timeout=timedelta(minutes=2),
                )
                result = {**result, "merged": merge_result.get("merged", False)}
            except Exception as exc:
                result = {**result, "merged": False, "merge_error": str(exc)[:200]}

        return {
            **result,
            "review": review,
            "mode": "raw",
            "requeue_prompt": review.get("requeue_prompt", ""),
        }

    @workflow.run
    async def run(self, stages: list[list[dict]] | list[dict]) -> dict:
        """Execute staged task specs.

        Input shape:
          - list[list[dict]]: each inner list is a stage; specs in a stage run
            in parallel via asyncio.gather; stages run sequentially.  If any
            spec in a stage is rejected by the reviewer, downstream stages
            are skipped (verdict=predecessor_failed).
          - list[dict]: legacy flat input, auto-wrapped as a single stage.
        """
        # Backwards-compat shim
        if stages and isinstance(stages[0], dict):
            staged: list[list[dict]] = [stages]
        else:
            staged = stages

        all_results: list[dict] = []
        stage_count = len(staged)
        for stage_idx, stage_specs in enumerate(staged):
            stage_results = await asyncio.gather(*[self._execute_one(s) for s in stage_specs])
            all_results.extend(stage_results)

            stage_failed = any(not r.get("review", {}).get("approved") for r in stage_results)
            if stage_failed and stage_idx < stage_count - 1:
                for skipped_stage in staged[stage_idx + 1 :]:
                    for spec in skipped_stage:
                        all_results.append(
                            {
                                "task": spec.get("task", "")[:200],
                                "provider": spec.get("provider", "zhipu"),
                                "success": False,
                                "exit_code": -1,
                                "mode": "skipped",
                                "review": {
                                    "approved": False,
                                    "verdict": "predecessor_failed",
                                    "flags": [f"skipped_stage_{stage_idx + 1}"],
                                },
                            }
                        )
                break

        succeeded = sum(1 for r in all_results if r.get("success"))
        approved = sum(1 for r in all_results if r.get("review", {}).get("approved"))
        flagged = sum(
            1 for r in all_results if r.get("review", {}).get("verdict") == "approved_with_flags"
        )
        rejected = sum(1 for r in all_results if not r.get("review", {}).get("approved"))

        return {
            "total": len(all_results),
            "succeeded": succeeded,
            "approved": approved,
            "flagged": flagged,
            "rejected": rejected,
            "results": [
                {
                    "task": r.get("task", "")[:100],
                    "provider": r.get("provider", ""),
                    "success": r.get("success", False),
                    "exit_code": r.get("exit_code", -1),
                    "mode": r.get("mode", "raw"),
                    "review": r.get("review", {}),
                }
                for r in all_results
            ],
        }


@workflow.defn
class WatchWorkflow:
    """Temporal-native watch: polls for ready specs and dispatches as child workflows.

    Each cycle:
      1. Calls watch_cycle activity (sync from ganglion + scan specs)
      2. Dispatches ready specs as child TranslationWorkflows (batched by max_concurrent)
      3. Waits for interval seconds
      4. Continue-As-New when cycle count reaches max_cycles (preserves state via params)

    Supports a ``stop`` signal for graceful termination and ``stop_after_empty``
    param for auto-stop when no specs are found for N consecutive cycles.
    """

    def __init__(self) -> None:
        self._stop_requested = False

    @workflow.signal
    async def stop(self) -> None:
        """Signal to gracefully stop the watch loop."""
        self._stop_requested = True

    async def _dispatch_spec(self, spec: dict, provider: str, cycle: int, task_queue: str) -> dict:
        """Dispatch a single spec as a child TranslationWorkflow."""
        name = spec.get("name", "unnamed")
        child_input = [{
            "task": spec.get("body", "") or spec.get("name", ""),
            "provider": spec.get("provider", provider),
            "mode": spec.get("mode", "raw"),
        }]

        result = await workflow.execute_child_workflow(
            TranslationWorkflow.run,
            args=[child_input],
            id=f"watch-{name}-c{cycle}",
            task_queue=task_queue,
        )
        return result

    @workflow.run
    async def run(self, params: dict) -> dict:
        """Execute watch loop: sync, scan, dispatch, repeat.

        Params keys:
          repo_path       – git repo to sync (str)
          plan_dir        – directory of .md spec files (str)
          interval        – seconds between cycles (int, default 60)
          max_concurrent  – max child workflows per batch (int, default 3)
          max_cycles      – cycles before Continue-As-New (int, default 100)
          provider        – default provider for child workflows (str, default "zhipu")
          stop_after_empty – stop after N consecutive empty cycles (int, 0=disabled)
          task_queue      – Temporal task queue for child workflows (str)
          _start_cycle    – internal: cycle counter from previous run (int)
          _continued      – internal: True if this is a continued run
        """
        repo_path = params.get("repo_path", "")
        plan_dir = params.get("plan_dir", "")
        interval_seconds = params.get("interval", 60)
        max_concurrent = params.get("max_concurrent", 3)
        max_cycles = params.get("max_cycles", 100)
        provider = params.get("provider", "zhipu")
        stop_after_empty = params.get("stop_after_empty", 0)
        task_queue = params.get("task_queue", "translation-queue")

        total_dispatched = 0
        total_synced = 0
        empty_streak = 0
        cycle = params.get("_start_cycle", 0)
        continued = params.get("_continued", False)
        run_start_cycle = cycle  # track cycles-per-run for CAN threshold

        while not self._stop_requested:
            cycle += 1

            # --- activity: sync + scan ---
            try:
                cycle_result = await workflow.execute_activity(
                    watch_cycle,
                    args=[repo_path, plan_dir],
                    start_to_close_timeout=timedelta(minutes=2),
                    retry_policy=RetryPolicy(maximum_attempts=1),
                )
            except Exception:
                cycle_result = {"synced": False, "fetched": 0, "ready_specs": []}

            if cycle_result.get("synced"):
                total_synced += 1

            ready_specs = cycle_result.get("ready_specs", [])

            # --- dispatch ready specs in batches ---
            if ready_specs:
                empty_streak = 0
                for batch_start in range(0, len(ready_specs), max_concurrent):
                    if self._stop_requested:
                        break
                    batch = ready_specs[batch_start:batch_start + max_concurrent]
                    results = await asyncio.gather(
                        *[self._dispatch_spec(s, provider, cycle, task_queue) for s in batch],
                    )
                    total_dispatched += len(results)
            else:
                empty_streak += 1

            if self._stop_requested:
                break

            # --- Continue-As-New when this run has done max_cycles ---
            cycles_in_run = cycle - run_start_cycle
            if cycles_in_run >= max_cycles:
                workflow.continue_as_new(
                    args=[{
                        **params,
                        "_start_cycle": cycle,
                        "_continued": True,
                    }],
                )

            # --- auto-stop after N empty cycles ---
            if stop_after_empty and empty_streak >= stop_after_empty:
                break

            # --- sleep until next cycle ---
            await asyncio.sleep(interval_seconds)

        return {
            "cycles": cycle,
            "total_dispatched": total_dispatched,
            "total_synced": total_synced,
            "continued": continued,
        }
