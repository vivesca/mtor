"""Tests for preferential dispatch ordering in watch.py.

Specs are sorted so that priority specs dispatch first.  Under stress
(high rejection rate) self-improvement specs (test infrastructure, CI,
tooling) are boosted to the front of the queue.
"""

from __future__ import annotations


from mtor.watch import (
    SELF_IMPROVEMENT_KEYWORDS,
    PRIORITY_RANK,
    RejectionTracker,
    is_self_improvement,
    prioritize_specs,
)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _spec(
    name: str = "spec-a",
    *,
    priority: str = "medium",
    scope: list[str] | None = None,
    tests: dict | None = None,
) -> dict:
    """Build a minimal spec dict matching the shape returned by parse_spec."""
    return {
        "name": name,
        "status": "ready",
        "priority": priority,
        "scope": scope or [],
        "tests": tests or {},
    }


# ---------------------------------------------------------------------------
# is_self_improvement
# ---------------------------------------------------------------------------

class TestIsSelfImprovement:
    """A spec is self-improvement when its scope or tests reference assay/CI paths."""

    def test_scope_contains_assays(self) -> None:
        assert is_self_improvement(_spec("t", scope=["assays/test_foo.py"]))

    def test_scope_contains_tests_dir(self) -> None:
        assert is_self_improvement(_spec("t", scope=["tests/test_bar.py"]))

    def test_tests_function_is_self_improvement(self) -> None:
        assert is_self_improvement(
            _spec("t", tests={"run": "pytest assays/test_x.py", "functions": ["x"]})
        )

    def test_ci_keyword_in_scope(self) -> None:
        assert is_self_improvement(_spec("t", scope=[".github/workflows/ci.yml"]))

    def test_not_self_improvement_when_production_only(self) -> None:
        spec = _spec("t", scope=["mtor/dispatch.py"])
        assert not is_self_improvement(spec)

    def test_empty_spec_not_self_improvement(self) -> None:
        assert not is_self_improvement(_spec("t"))


# ---------------------------------------------------------------------------
# prioritize_specs — no stress
# ---------------------------------------------------------------------------

class TestPrioritizeNoStress:
    """Without stress the ordering is purely by priority rank."""

    def test_high_before_medium(self) -> None:
        specs = [_spec("low", priority="low"), _spec("hi", priority="high")]
        result = prioritize_specs(specs, stress=False)
        assert result[0]["name"] == "hi"

    def test_medium_before_low(self) -> None:
        specs = [_spec("lo", priority="low"), _spec("mid", priority="medium")]
        result = prioritize_specs(specs, stress=False)
        assert result[0]["name"] == "mid"

    def test_same_priority_preserves_alphabetical(self) -> None:
        specs = [_spec("b-spec", priority="medium"), _spec("a-spec", priority="medium")]
        result = prioritize_specs(specs, stress=False)
        assert result[0]["name"] == "a-spec"

    def test_does_not_mutate_input(self) -> None:
        specs = [_spec("lo", priority="low"), _spec("hi", priority="high")]
        original_order = [s["name"] for s in specs]
        prioritize_specs(specs, stress=False)
        assert [s["name"] for s in specs] == original_order


# ---------------------------------------------------------------------------
# prioritize_specs — under stress
# ---------------------------------------------------------------------------

class TestPrioritizeUnderStress:
    """Under stress, self-improvement specs are promoted ahead of others."""

    def test_self_improvement_first_under_stress(self) -> None:
        prod = _spec("prod-feature", priority="high", scope=["mtor/dispatch.py"])
        test_infra = _spec(
            "improve-tests",
            priority="low",
            scope=["assays/test_dispatch.py"],
        )
        result = prioritize_specs([prod, test_infra], stress=True)
        assert result[0]["name"] == "improve-tests"

    def test_self_improvement_respects_priority_among_self(self) -> None:
        si_low = _spec("si-low", priority="low", scope=["assays/test_a.py"])
        si_high = _spec("si-high", priority="high", scope=["assays/test_b.py"])
        result = prioritize_specs([si_low, si_high], stress=True)
        assert result[0]["name"] == "si-high"

    def test_non_self_improvement_after_all_self_improvement(self) -> None:
        specs = [
            _spec("prod-a", priority="high", scope=["mtor/foo.py"]),
            _spec("si-low", priority="low", scope=["assays/test_foo.py"]),
            _spec("prod-b", priority="medium", scope=["mtor/bar.py"]),
        ]
        result = prioritize_specs(specs, stress=True)
        [s["name"] for s in result if "si" in s["name"]]
        [s["name"] for s in result if "prod" in s["name"]]
        # All self-improvement come before all production
        si_last_idx = max(result.index(s) for s in result if "si" in s["name"])
        prod_first_idx = min(result.index(s) for s in result if "prod" in s["name"])
        assert si_last_idx < prod_first_idx


# ---------------------------------------------------------------------------
# THE key test
# ---------------------------------------------------------------------------

class TestSelfImprovementPrioritizedUnderStress:
    """End-to-end: RejectionTracker triggers stress → prioritize_specs boosts SI."""

    def test_test_self_improvement_prioritized_under_stress(self) -> None:
        # Build rejection tracker with high rejection rate (simulating stress)
        tracker = RejectionTracker(window_size=5, threshold=0.6)
        for _ in range(5):
            tracker.record(rejected=True)

        assert tracker.should_throttle(), "tracker should indicate stress"

        # Mix of specs — only one is self-improvement (but low priority)
        specs = [
            _spec("add-feature", priority="high", scope=["mtor/watch.py"]),
            _spec("fix-bug", priority="medium", scope=["mtor/dispatch.py"]),
            _spec(
                "test-watch-coverage",
                priority="low",
                scope=["assays/test_watch.py"],
            ),
        ]

        result = prioritize_specs(specs, stress=tracker.should_throttle())

        # Self-improvement spec must come first despite being low priority
        assert result[0]["name"] == "test-watch-coverage"
        # Production specs follow in priority order
        assert result[1]["name"] == "add-feature"
        assert result[2]["name"] == "fix-bug"


# ---------------------------------------------------------------------------
# Constants exist and are well-formed
# ---------------------------------------------------------------------------

class TestConstants:
    def test_priority_rank_has_expected_levels(self) -> None:
        assert set(PRIORITY_RANK.keys()) == {"high", "medium", "low"}

    def test_rank_ordering(self) -> None:
        assert PRIORITY_RANK["high"] < PRIORITY_RANK["medium"] < PRIORITY_RANK["low"]

    def test_self_improvement_keywords_not_empty(self) -> None:
        assert len(SELF_IMPROVEMENT_KEYWORDS) > 0
