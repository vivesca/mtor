"""Tests for mtor plan DAG phase 3 — PlanWorkflow: child spawning, dependency ordering, signals."""

from __future__ import annotations

from mtor.worker.plan_workflow import PlanWorkflow, find_ready_specs


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------


class TestPlanWorkflowSpawnsChildren:
    def test_test_plan_workflow_spawns_children(self):
        """PlanWorkflow identifies all independent specs as children to spawn."""
        specs = [
            {"name": "a", "task": "build a", "provider": "zhipu", "depends_on": []},
            {"name": "b", "task": "build b", "provider": "zhipu", "depends_on": []},
            {"name": "c", "task": "build c", "provider": "zhipu", "depends_on": []},
        ]

        wf = PlanWorkflow()
        wf._specs = specs
        wf._pending = {"a", "b", "c"}
        wf._completed = set()

        ready = wf._find_ready()

        assert len(ready) == 3
        ready_names = {s["name"] for s in ready}
        assert ready_names == {"a", "b", "c"}


class TestPlanWorkflowRespectsDependencies:
    def test_test_plan_workflow_respects_dependencies(self):
        """Specs with unsatisfied dependencies are not identified as ready."""
        specs = [
            {"name": "a", "task": "build a", "provider": "zhipu", "depends_on": []},
            {"name": "b", "task": "build b", "provider": "zhipu", "depends_on": ["a"]},
        ]

        wf = PlanWorkflow()
        wf._specs = specs
        wf._pending = {"a", "b"}
        wf._completed = set()

        ready = wf._find_ready()

        assert len(ready) == 1
        assert ready[0]["name"] == "a"


class TestSignalUnblocksDependent:
    def test_test_signal_unblocks_dependent(self):
        """After marking 'a' completed, spec 'b' (depends on 'a') becomes ready."""
        specs = [
            {"name": "a", "task": "build a", "provider": "zhipu", "depends_on": []},
            {"name": "b", "task": "build b", "provider": "zhipu", "depends_on": ["a"]},
        ]

        wf = PlanWorkflow()
        wf._specs = specs
        wf._pending = {"b"}  # a already removed from pending
        wf._completed = set()

        # Simulate completion of 'a' — signal unblocks 'b'
        wf._completed.add("a")

        ready = wf._find_ready()

        assert len(ready) == 1
        assert ready[0]["name"] == "b"


class TestIndependentCompletionSignalsBlocked:
    def test_test_independent_completion_signals_blocked(self):
        """Completing 'a' only unblocks its dependents, not b's dependents."""
        specs = [
            {"name": "a", "task": "build a", "provider": "zhipu", "depends_on": []},
            {"name": "b", "task": "build b", "provider": "zhipu", "depends_on": []},
            {"name": "c", "task": "build c", "provider": "zhipu", "depends_on": ["a"]},
            {"name": "d", "task": "build d", "provider": "zhipu", "depends_on": ["b"]},
        ]

        wf = PlanWorkflow()
        wf._specs = specs
        wf._pending = {"b", "c", "d"}  # a already removed from pending
        wf._completed = set()

        # Signal a completed — should unblock c but not d
        wf._completed.add("a")

        ready = wf._find_ready()
        ready_names = {s["name"] for s in ready}

        # b has no deps → ready, c depends on a (done) → ready
        # d depends on b (not done) → NOT ready
        assert "b" in ready_names
        assert "c" in ready_names
        assert "d" not in ready_names
