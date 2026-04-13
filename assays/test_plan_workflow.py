"""Tests for PlanWorkflow pure functions and workflow logic."""

import pytest

from mtor.worker.plan_workflow import find_ready_specs, PlanWorkflow


class TestFindReadySpecs:
    """Tests for find_ready_specs pure function."""

    def test_empty_pending_returns_empty(self):
        """When no specs are pending, returns empty list."""
        specs = [
            {"name": "a", "depends_on": []},
            {"name": "b", "depends_on": ["a"]},
        ]
        result = find_ready_specs(specs, completed=set(), pending=set())
        assert result == []

    def test_no_deps_ready_immediately(self):
        """Specs with empty deps are ready when pending."""
        specs = [
            {"name": "a", "depends_on": []},
            {"name": "b", "depends_on": []},
        ]
        result = find_ready_specs(specs, completed=set(), pending={"a", "b"})
        assert len(result) == 2
        assert [s["name"] for s in result] == ["a", "b"]

    def test_deps_not_ready_skips(self):
        """Specs whose deps are not completed are not returned."""
        specs = [
            {"name": "a", "depends_on": []},
            {"name": "b", "depends_on": ["a"]},
        ]
        result = find_ready_specs(specs, completed=set(), pending={"a", "b"})
        assert len(result) == 1
        assert result[0]["name"] == "a"

    def test_deps_ready_includes(self):
        """Spec whose deps are all completed is returned when pending."""
        specs = [
            {"name": "a", "depends_on": []},
            {"name": "b", "depends_on": ["a"]},
            {"name": "c", "depends_on": ["a", "b"]},
        ]
        result = find_ready_specs(
            specs, completed={"a"}, pending={"b", "c"}
        )
        assert len(result) == 1
        assert result[0]["name"] == "b"

    def test_all_deps_ready_returns_all(self):
        """Multiple specs with ready deps are all returned."""
        specs = [
            {"name": "z", "depends_on": ["a"]},
            {"name": "a", "depends_on": []},
            {"name": "m", "depends_on": ["a"]},
        ]
        result = find_ready_specs(
            specs, completed=set(), pending={"a", "z", "m"}
        )
        assert len(result) == 1
        assert result[0]["name"] == "a"

        result = find_ready_specs(
            specs, completed={"a"}, pending={"z", "m"}
        )
        assert len(result) == 2
        assert [s["name"] for s in result] == ["m", "z"]

    def test_skips_non_pending(self):
        """Already started specs are not returned again."""
        specs = [
            {"name": "a", "depends_on": []},
            {"name": "b", "depends_on": []},
        ]
        result = find_ready_specs(
            specs, completed=set(), pending={"a"}
        )
        assert len(result) == 1
        assert result[0]["name"] == "a"

    def test_handles_missing_name(self):
        """Gracefully handles specs without name key."""
        specs = [
            {"depends_on": []},
            {"name": "b", "depends_on": []},
        ]
        result = find_ready_specs(
            specs, completed=set(), pending={"", "b"}
        )
        assert len(result) == 2
        # spec.get("name", "") ensures name is empty string, not None
        assert [s.get("name", "") for s in result] == ["", "b"]

    def test_sorted_ordering_for_determinism(self):
        """Ready specs are sorted alphabetically by name for determinism."""
        specs = [
            {"name": "z", "depends_on": []},
            {"name": "a", "depends_on": []},
            {"name": "m", "depends_on": []},
        ]
        result = find_ready_specs(specs, set(), {"z", "a", "m"})
        assert [s["name"] for s in result] == ["a", "m", "z"]


class TestPlanWorkflow:
    """Unit tests for PlanWorkflow with mocked Temporal."""

    def test_init(self):
        """Workflow initializes with empty state."""
        wf = PlanWorkflow()
        assert wf._completed == set()
        assert wf._pending == set()
        assert wf._results == {}
        assert wf._specs == []

    @pytest.mark.anyio
    async def test_spec_completed_signal_adds_to_completed(self):
        """spec_completed signal adds the spec name to completed set."""
        wf = PlanWorkflow()
        await wf.spec_completed("test-spec")
        assert "test-spec" in wf._completed

    def test_find_ready_delegates_to_helper(self):
        """_find_ready calls find_ready_specs with correct args."""
        wf = PlanWorkflow()
        wf._specs = [
            {"name": "a", "depends_on": []},
            {"name": "b", "depends_on": ["a"]},
        ]
        wf._completed = {"a"}
        wf._pending = {"b"}
        ready = wf._find_ready()
        assert len(ready) == 1
        assert ready[0]["name"] == "b"

    @pytest.mark.anyio
    async def test_run_completes_parallel_dag(self):
        """Full run executes a DAG with parallel nodes correctly."""
        wf = PlanWorkflow()
        plan_specs = [
            {"name": "a", "depends_on": [], "task": "task a", "provider": "zhipu"},
            {"name": "b", "depends_on": [], "task": "task b", "provider": "zhipu"},
            {"name": "c", "depends_on": ["a", "b"], "task": "task c", "provider": "zhipu"},
        ]

        # Mock _execute_child to return immediately
        original_execute_child = wf._execute_child
        try:
            call_order = []

            async def mock_execute(spec):
                name = spec.get("name", "")
                call_order.append(name)
                # Real _execute_child adds to completed after execution
                wf._completed.add(name)
                return {"succeeded": 1, "output": f"output {spec['name']}"}

            wf._execute_child = mock_execute

            result = await wf.run(plan_specs)

            assert result["total"] == 3
            assert result["succeeded"] == 3
            assert len(result["results"]) == 3

            # Should run a + b in parallel first, then c
            assert len(call_order) == 3
            assert set(call_order[:2]) == {"a", "b"}
            assert call_order[2] == "c"

            assert wf._results["a"] == {"succeeded": 1, "output": "output a"}
            assert wf._results["b"] == {"succeeded": 1, "output": "output b"}
            assert wf._results["c"] == {"succeeded": 1, "output": "output c"}
        finally:
            wf._execute_child = original_execute_child

    @pytest.mark.anyio
    async def test_run_counts_failures_correctly(self):
        """Run result counts correctly reflects succeeded vs failed."""
        wf = PlanWorkflow()
        plan_specs = [
            {"name": "pass", "depends_on": [], "task": "task", "provider": "zhipu"},
            {"name": "fail", "depends_on": [], "task": "task", "provider": "zhipu"},
        ]

        call_results = {
            "pass": {"succeeded": 1, "output": "ok"},
            "fail": {"succeeded": 0, "error": "failed"},
        }

        async def mock_execute(spec):
            name = spec["name"]
            wf._completed.add(name)
            return call_results[spec["name"]]

        original_execute_child = wf._execute_child
        wf._execute_child = mock_execute

        try:
            result = await wf.run(plan_specs)
            assert result["total"] == 2
            assert result["succeeded"] == 1
        finally:
            wf._execute_child = original_execute_child
