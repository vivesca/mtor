"""PlanWorkflow — DAG-aware orchestration of child TranslationWorkflows.

Executes plan specs respecting dependency ordering:
  - Specs with satisfied deps run in parallel
  - Completion signals unblock waiting dependents
  - Uses Temporal signals for inter-workflow communication
"""

from __future__ import annotations

import asyncio

from temporalio import workflow

with workflow.unsafe.imports_passed_through():
    from mtor import TASK_QUEUE


# ---------------------------------------------------------------------------
# Pure helper functions (testable without Temporal)
# ---------------------------------------------------------------------------


def find_ready_specs(
    specs: list[dict],
    completed: set[str],
    pending: set[str],
) -> list[dict]:
    """Return specs whose deps are all completed and that are still pending.

    Sorted alphabetically by name for deterministic ordering.
    """
    ready = []
    for spec in specs:
        name = spec.get("name", "")
        if name not in pending:
            continue
        deps = spec.get("depends_on", [])
        if all(dep in completed for dep in deps):
            ready.append(spec)
    return sorted(ready, key=lambda s: s.get("name", ""))


# ---------------------------------------------------------------------------
# PlanWorkflow
# ---------------------------------------------------------------------------


@workflow.defn
class PlanWorkflow:
    """Orchestrate multiple specs as child TranslationWorkflows.

    Respects DAG dependencies:
      - Only starts child workflows when all deps are completed
      - Completion of a child unblocks its dependents
      - Specs at the same dependency level run in parallel
    """

    def __init__(self) -> None:
        self._completed: set[str] = set()
        self._failed: set[str] = set()
        self._results: dict[str, dict] = {}
        self._pending: set[str] = set()
        self._specs: list[dict] = []

    @workflow.signal
    async def spec_completed(self, spec_name: str) -> None:
        """Signal that a child spec has completed."""
        self._completed.add(spec_name)

    def _find_ready(self) -> list[dict]:
        """Find specs that are pending and have all deps completed."""
        return find_ready_specs(self._specs, self._completed, self._pending)

    async def _execute_child(self, spec: dict) -> dict:
        """Execute a spec as a child TranslationWorkflow."""
        # Imports inside workflow code must pass through the Temporal sandbox
        # (determinism rule); mirrors the module-level TASK_QUEUE import.
        with workflow.unsafe.imports_passed_through():
            from mtor.worker.workflow import TranslationWorkflow

        name = spec.get("name", "")
        task = spec.get("task", "")
        provider = spec.get("provider", "zhipu")

        child_spec = {
            "task": task,
            "provider": provider,
            "mode": spec.get("mode", "raw"),
        }

        result = await workflow.execute_child_workflow(
            TranslationWorkflow.run,
            args=[[child_spec]],
            id=f"plan-child-{name}",
            task_queue=TASK_QUEUE,
        )

        # Only unblock dependents when the child actually produced a success.
        # A child that completes with zero successes (all tasks rejected/failed)
        # must NOT satisfy `dep in completed`, or dependents would build on a
        # failed prerequisite. Track failures separately so run() can terminate
        # their now-unreachable dependents instead of waiting forever.
        if isinstance(result, dict) and result.get("succeeded", 0) > 0:
            self._completed.add(name)
        else:
            self._failed.add(name)
        return result

    @workflow.run
    async def run(self, plan_specs: list[dict]) -> dict:
        """Execute plan specs respecting DAG dependencies.

        Each wave of ready specs is dispatched and awaited before the next is
        computed, so nothing is in flight when no spec is ready. A pending set
        with no ready spec therefore means the remainder is permanently blocked
        — an upstream spec failed, a ``depends_on`` name is absent from the plan
        (typo / external dep), or the deps form a cycle. Those are recorded as
        blocked and the plan returns promptly, instead of parking on a
        multi-hour ``wait_condition`` whose timeout would abort the whole run.
        """
        self._specs = plan_specs
        self._pending = {s.get("name", "") for s in plan_specs}
        known = {s.get("name", "") for s in plan_specs}

        all_results: list[dict] = []

        while self._pending:
            ready = self._find_ready()

            if not ready:
                for spec in sorted(self._specs, key=lambda s: s.get("name", "")):
                    name = spec.get("name", "")
                    if name not in self._pending:
                        continue
                    blocking = [
                        dep for dep in spec.get("depends_on", [])
                        if dep not in self._completed
                    ]
                    missing = [dep for dep in blocking if dep not in known]
                    failed = [dep for dep in blocking if dep in self._failed]
                    if missing:
                        verdict = "unsatisfiable_dependency"
                        reason = f"depends_on not in plan: {missing}"
                    elif failed:
                        verdict = "predecessor_failed"
                        reason = f"blocked by failed deps: {failed}"
                    else:
                        verdict = "blocked"
                        reason = f"unresolved deps (cycle?): {blocking}"
                    self._pending.discard(name)
                    self._failed.add(name)
                    all_results.append({
                        "name": name,
                        "result": {
                            "total": 0,
                            "succeeded": 0,
                            "blocked": True,
                            "verdict": verdict,
                            "reason": reason,
                        },
                    })
                break

            # Start ready specs in parallel
            for spec in ready:
                self._pending.discard(spec.get("name", ""))

            results = await asyncio.gather(*[self._execute_child(s) for s in ready])

            for spec, result in zip(ready, results):
                name = spec.get("name", "")
                self._results[name] = result
                all_results.append({
                    "name": name,
                    "result": result,
                })

        succeeded = sum(
            1 for r in self._results.values()
            if isinstance(r, dict) and r.get("succeeded", 0) > 0
        )
        blocked = sum(1 for r in all_results if r.get("result", {}).get("blocked"))

        return {
            "total": len(plan_specs),
            "succeeded": succeeded,
            "blocked": blocked,
            "results": all_results,
        }
