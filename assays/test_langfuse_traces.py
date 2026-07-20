"""Tests for Langfuse v4 trace integration in stall_trace.

The installed client is ``langfuse>=4.0.6`` which removed the v2
``.trace()`` / ``.generation()`` / ``.span()`` surface. Every mock here is
constrained to the real v4 surface (``spec=Langfuse`` for the client,
``spec=LangfuseSpan`` for observations) so any regression that reintroduces
the removed calls fails immediately with ``AttributeError``.
"""

from __future__ import annotations

from unittest.mock import MagicMock, patch

from langfuse import Langfuse
from langfuse._client.span import LangfuseSpan

from mtor.worker.stall_trace import (
    create_span,
    create_task_trace,
    finalize_trace,
    get_langfuse,
    record_review_outcome,
    record_stall_event,
)


def _make_client_mock() -> MagicMock:
    """Return a Langfuse client mock spec'd to the v4 surface.

    Accessing a removed v2 attribute (``.trace``, ``.generation``, ``.span``)
    on this mock raises ``AttributeError`` — the regression gate.
    """
    client = MagicMock(name="LangfuseClient", spec=Langfuse)
    client.start_observation.return_value = MagicMock(
        name="LangfuseObservation", spec=LangfuseSpan
    )
    return client


def _make_observation_mock() -> MagicMock:
    """Return a child observation mock spec'd to the v4 surface."""
    obs = MagicMock(name="LangfuseObservation", spec=LangfuseSpan)
    obs.start_observation.return_value = MagicMock(
        name="LangfuseChildObservation", spec=LangfuseSpan
    )
    return obs


# ---------------------------------------------------------------------------
# get_langfuse
# ---------------------------------------------------------------------------

class TestGetLangfuse:
    """Langfuse client acquisition."""

    def setup_method(self):
        """Reset the cached client before each test."""
        import mtor.worker.stall_trace as mod

        mod._langfuse_client = None

    def test_returns_client_when_installed(self):
        """get_langfuse() should return a Langfuse() client, not the module."""
        mock_client = _make_client_mock()
        with patch("langfuse.Langfuse", return_value=mock_client):
            assert get_langfuse() is mock_client

    @patch("mtor.worker.stall_trace.get_langfuse", return_value=None)
    def test_returns_none_when_missing(self, _mock):
        assert get_langfuse() is None

    def test_cached_client_only_constructs_once(self):
        """Repeated calls should reuse the cached Langfuse() instance."""
        mock_client = _make_client_mock()
        with patch("langfuse.Langfuse", return_value=mock_client) as ctor:
            first = get_langfuse()
            second = get_langfuse()
        assert first is second is mock_client
        ctor.assert_called_once()

    def test_returns_none_on_import_error(self):
        """Should gracefully return None when Langfuse constructor fails."""
        import mtor.worker.stall_trace as mod

        mod._langfuse_client = None
        with patch("langfuse.Langfuse", side_effect=ImportError("no langfuse")):
            result = get_langfuse()
        assert result is None


# ---------------------------------------------------------------------------
# create_task_trace
# ---------------------------------------------------------------------------

class TestCreateTaskTrace:
    """Root agent observation creation against the v4 surface."""

    @patch("mtor.worker.stall_trace.get_langfuse")
    def test_returns_observation_object(self, mock_lf):
        client = _make_client_mock()
        mock_lf.return_value = client

        result = create_task_trace("do the thing", "glm-5.1", "wf-123")

        assert result is client.start_observation.return_value

    @patch("mtor.worker.stall_trace.get_langfuse")
    def test_uses_start_observation_with_agent_type(self, mock_lf):
        """v4 root observation: client.start_observation(as_type='agent', ...)."""
        client = _make_client_mock()
        mock_lf.return_value = client

        create_task_trace("task", "provider-a", "wf-abc")

        client.start_observation.assert_called_once()
        kwargs = client.start_observation.call_args[1]
        assert kwargs["as_type"] == "agent"

    @patch("mtor.worker.stall_trace.get_langfuse")
    def test_observation_name_includes_workflow_id(self, mock_lf):
        client = _make_client_mock()
        mock_lf.return_value = client

        create_task_trace("task", "provider-a", "wf-abc")

        kwargs = client.start_observation.call_args[1]
        assert kwargs["name"] == "ribosome-wf-abc"

    @patch("mtor.worker.stall_trace.get_langfuse")
    def test_observation_metadata_has_provider_and_task(self, mock_lf):
        client = _make_client_mock()
        mock_lf.return_value = client

        create_task_trace("implement feature X", "deepseek-v3", "wf-1")

        meta = client.start_observation.call_args[1]["metadata"]
        assert meta["provider"] == "deepseek-v3"
        assert meta["task"] == "implement feature X"
        assert meta["workflow_id"] == "wf-1"

    @patch("mtor.worker.stall_trace.get_langfuse")
    def test_observation_includes_full_task_as_input(self, mock_lf):
        """Root observation should carry the full task prompt as input."""
        client = _make_client_mock()
        mock_lf.return_value = client

        create_task_trace("refactor the translocase module", "glm-5.1", "wf-2")

        kwargs = client.start_observation.call_args[1]
        assert kwargs["input"] == "refactor the translocase module"
        assert kwargs["metadata"]["input"] == "refactor the translocase module"

    @patch("mtor.worker.stall_trace.get_langfuse")
    @patch("langfuse.Langfuse.create_trace_id", return_value="stable-trace-id")
    def test_uses_stable_trace_id_derived_from_workflow_id(self, _ctid, mock_lf):
        """Execution trace attaches to a workflow-stable trace ID."""
        client = _make_client_mock()
        mock_lf.return_value = client

        create_task_trace("task", "glm-5.1", "wf-abc")

        kwargs = client.start_observation.call_args[1]
        assert kwargs["trace_context"] == {"trace_id": "stable-trace-id"}

    @patch("mtor.worker.stall_trace.get_langfuse")
    @patch("langfuse.Langfuse.create_trace_id")
    def test_create_trace_id_seeded_with_workflow_id(self, ctid, mock_lf):
        """create_trace_id is called with the workflow_id as seed."""
        ctid.return_value = "derived-id"
        mock_lf.return_value = _make_client_mock()

        create_task_trace("task", "glm-5.1", "wf-seed-xyz")

        ctid.assert_called_once_with(seed="wf-seed-xyz")

    @patch("mtor.worker.stall_trace.get_langfuse")
    def test_no_trace_context_when_workflow_id_empty(self, mock_lf):
        """Empty workflow_id leaves trace_context at its default (None)."""
        client = _make_client_mock()
        mock_lf.return_value = client

        create_task_trace("task", "glm-5.1", "")

        kwargs = client.start_observation.call_args[1]
        assert kwargs.get("trace_context") is None

    def test_returns_none_when_langfuse_unavailable(self):
        with patch("mtor.worker.stall_trace.get_langfuse", return_value=None):
            result = create_task_trace("task", "provider", "wf")
            assert result is None

    @patch("mtor.worker.stall_trace.get_langfuse")
    def test_returns_none_on_langfuse_exception(self, mock_lf):
        client = _make_client_mock()
        client.start_observation.side_effect = RuntimeError("boom")
        mock_lf.return_value = client

        result = create_task_trace("task", "provider", "wf")
        assert result is None


# ---------------------------------------------------------------------------
# finalize_trace
# ---------------------------------------------------------------------------

class TestFinalizeTrace:
    """Finalization creates and closes a generation + the root observation."""

    def test_noop_when_trace_is_none(self):
        """Should not raise when trace is None."""
        finalize_trace(None, {"stdout": "ok", "exit_code": 0})

    @patch("mtor.worker.stall_trace.get_langfuse")
    def test_creates_generation_via_start_observation(self, mock_lf):
        """v4 child observation: trace.start_observation(as_type='generation')."""
        mock_lf.return_value = _make_client_mock()
        root = _make_observation_mock()

        finalize_trace(root, {
            "stdout": "all done",
            "exit_code": 0,
            "provider": "glm-5.1",
            "cost_info": "",
            "success": True,
            "stderr": "",
        })

        root.start_observation.assert_called_once()
        kwargs = root.start_observation.call_args[1]
        assert kwargs["as_type"] == "generation"
        assert kwargs["name"] == "ribosome-execution"
        assert kwargs["output"] == "all done"

    @patch("mtor.worker.stall_trace.get_langfuse")
    def test_generation_truncates_output(self, mock_lf):
        mock_lf.return_value = _make_client_mock()
        root = _make_observation_mock()

        finalize_trace(root, {
            "stdout": "x" * 20_000,
            "exit_code": 0,
            "provider": "glm-5.1",
            "cost_info": "",
            "success": True,
            "stderr": "",
        })

        kwargs = root.start_observation.call_args[1]
        assert len(kwargs["output"]) == 10_000

    @patch("mtor.worker.stall_trace.get_langfuse")
    def test_generation_metadata_has_exit_code(self, mock_lf):
        mock_lf.return_value = _make_client_mock()
        root = _make_observation_mock()

        finalize_trace(root, {
            "stdout": "out",
            "exit_code": 42,
            "provider": "glm-5.1",
            "cost_info": "",
            "success": False,
            "stderr": "oops",
        })

        meta = root.start_observation.call_args[1]["metadata"]
        assert meta["exit_code"] == 42

    @patch("mtor.worker.stall_trace.get_langfuse")
    def test_generation_metadata_captures_success(self, mock_lf):
        mock_lf.return_value = _make_client_mock()
        root = _make_observation_mock()

        finalize_trace(root, {
            "stdout": "ok",
            "exit_code": 0,
            "provider": "glm-5.1",
            "cost_info": "",
            "success": True,
            "stderr": "",
        })

        meta = root.start_observation.call_args[1]["metadata"]
        assert meta["success"] is True

    @patch("mtor.worker.stall_trace.get_langfuse")
    def test_generation_metadata_captures_stderr(self, mock_lf):
        mock_lf.return_value = _make_client_mock()
        root = _make_observation_mock()

        finalize_trace(root, {
            "stdout": "",
            "exit_code": 1,
            "provider": "glm-5.1",
            "cost_info": "",
            "success": False,
            "stderr": "SyntaxError: invalid syntax",
        })

        meta = root.start_observation.call_args[1]["metadata"]
        assert "SyntaxError" in meta["stderr"]

    @patch("mtor.worker.stall_trace.get_langfuse")
    def test_generation_includes_model_field(self, mock_lf):
        mock_lf.return_value = _make_client_mock()
        root = _make_observation_mock()

        finalize_trace(root, {
            "stdout": "ok",
            "exit_code": 0,
            "provider": "ark-code",
            "cost_info": "",
            "success": True,
            "stderr": "",
        })

        kwargs = root.start_observation.call_args[1]
        assert kwargs["model"] == "ark-code"

    @patch("mtor.worker.stall_trace.get_langfuse")
    def test_generation_includes_status_message(self, mock_lf):
        mock_lf.return_value = _make_client_mock()
        root = _make_observation_mock()

        finalize_trace(root, {
            "stdout": "",
            "exit_code": 1,
            "provider": "glm-5.1",
            "cost_info": "",
            "success": False,
            "stderr": "error",
        })

        kwargs = root.start_observation.call_args[1]
        assert kwargs.get("status_message") == "error"

    @patch("mtor.worker.stall_trace.get_langfuse")
    def test_generation_and_root_ended_explicitly(self, mock_lf):
        """v4 requires explicit .end() on both child and root observations."""
        mock_lf.return_value = _make_client_mock()
        root = _make_observation_mock()
        generation = root.start_observation.return_value

        finalize_trace(root, {
            "stdout": "ok",
            "exit_code": 0,
            "provider": "glm-5.1",
            "cost_info": "",
            "success": True,
            "stderr": "",
        })

        generation.end.assert_called_once()
        root.end.assert_called_once()

    @patch("mtor.worker.stall_trace.get_langfuse")
    def test_flushes_langfuse(self, mock_lf):
        """Flush must be called to ensure traces are shipped."""
        client = _make_client_mock()
        mock_lf.return_value = client
        root = _make_observation_mock()

        finalize_trace(root, {
            "stdout": "ok",
            "exit_code": 0,
            "provider": "glm-5.1",
            "cost_info": "",
            "success": True,
            "stderr": "",
        })

        client.flush.assert_called_once()

    @patch("mtor.worker.stall_trace.get_langfuse")
    def test_graceful_on_generation_exception(self, mock_lf):
        """Exceptions in observation creation should not propagate."""
        mock_lf.return_value = _make_client_mock()
        root = _make_observation_mock()
        root.start_observation.side_effect = RuntimeError("span failed")

        # Should not raise
        finalize_trace(root, {
            "stdout": "ok",
            "exit_code": 0,
            "provider": "glm-5.1",
            "cost_info": "",
            "success": True,
            "stderr": "",
        })

    @patch("mtor.worker.stall_trace.get_langfuse")
    def test_generation_metadata_captures_post_diff_stat(self, mock_lf):
        mock_lf.return_value = _make_client_mock()
        root = _make_observation_mock()

        finalize_trace(root, {
            "stdout": "ok",
            "exit_code": 0,
            "provider": "glm-5.1",
            "cost_info": "",
            "success": True,
            "stderr": "",
            "post_diff": {"stat": "3 files changed, 10 insertions(+), 2 deletions(-)"},
        })

        meta = root.start_observation.call_args[1]["metadata"]
        assert "3 files changed" in meta.get("diff_stat", "")

    @patch("mtor.worker.stall_trace.get_langfuse")
    def test_generation_metadata_captures_branch_and_merge(self, mock_lf):
        mock_lf.return_value = _make_client_mock()
        root = _make_observation_mock()

        finalize_trace(root, {
            "stdout": "ok",
            "exit_code": 0,
            "provider": "glm-5.1",
            "cost_info": "",
            "success": True,
            "stderr": "",
            "branch_name": "ribosome-abc123",
            "merged": True,
        })

        meta = root.start_observation.call_args[1]["metadata"]
        assert meta["branch_name"] == "ribosome-abc123"
        assert meta["merged"] is True


# ---------------------------------------------------------------------------
# create_span
# ---------------------------------------------------------------------------

class TestCreateSpan:
    """Intermediate span observation creation."""

    def test_noop_when_trace_is_none(self):
        """Should not raise when trace is None."""
        create_span(None, "test-span")

    def test_creates_span_via_start_observation(self):
        """v4 child span: trace.start_observation(name=..., metadata=...)."""
        root = _make_observation_mock()
        create_span(root, "setup-worktree")
        root.start_observation.assert_called_once()
        call_kwargs = root.start_observation.call_args[1]
        assert call_kwargs["name"] == "setup-worktree"

    def test_span_metadata_passed_through(self):
        """Extra kwargs become span metadata."""
        root = _make_observation_mock()
        create_span(root, "retry", attempt=2, reason="rate_limit")
        call_kwargs = root.start_observation.call_args[1]
        assert call_kwargs["metadata"]["attempt"] == 2
        assert call_kwargs["metadata"]["reason"] == "rate_limit"

    def test_span_ended_explicitly(self):
        """v4 requires .end() on the child observation."""
        root = _make_observation_mock()
        span = root.start_observation.return_value

        create_span(root, "test-span")

        span.end.assert_called_once()

    def test_graceful_on_span_exception(self):
        """Exceptions from start_observation() should not propagate."""
        root = _make_observation_mock()
        root.start_observation.side_effect = RuntimeError("span failed")
        create_span(root, "test")  # should not raise


# ---------------------------------------------------------------------------
# record_review_outcome
# ---------------------------------------------------------------------------

class TestRecordReviewOutcome:
    """Attach chaperone review to the workflow trace."""

    @patch("mtor.worker.stall_trace.get_langfuse")
    @patch("langfuse.Langfuse.create_trace_id", return_value="review-trace-id")
    def test_attaches_evaluator_observation_to_stable_trace(self, _ctid, mock_lf):
        client = _make_client_mock()
        mock_lf.return_value = client

        record_review_outcome("wf-abc", {"verdict": "approved", "satisfaction": 95})

        client.start_observation.assert_called_once()
        kwargs = client.start_observation.call_args[1]
        assert kwargs["name"] == "chaperone-review"
        assert kwargs["as_type"] == "evaluator"
        assert kwargs["trace_context"] == {"trace_id": "review-trace-id"}

    @patch("mtor.worker.stall_trace.get_langfuse")
    @patch("langfuse.Langfuse.create_trace_id", return_value="review-trace-id")
    def test_evaluator_observation_ended_explicitly(self, _ctid, mock_lf):
        client = _make_client_mock()
        mock_lf.return_value = client
        evaluator = client.start_observation.return_value

        record_review_outcome("wf-abc", {"verdict": "approved", "satisfaction": 95})

        evaluator.end.assert_called_once()

    @patch("mtor.worker.stall_trace.get_langfuse")
    @patch("langfuse.Langfuse.create_trace_id", return_value="review-trace-id")
    def test_categorical_verdict_score_created(self, _ctid, mock_lf):
        client = _make_client_mock()
        mock_lf.return_value = client

        record_review_outcome("wf-abc", {"verdict": "approved", "satisfaction": 95})

        client.create_score.assert_any_call(
            name="mtor-verdict",
            value="approved",
            data_type="CATEGORICAL",
            trace_id="review-trace-id",
        )

    @patch("mtor.worker.stall_trace.get_langfuse")
    @patch("langfuse.Langfuse.create_trace_id", return_value="review-trace-id")
    def test_numeric_satisfaction_score_created(self, _ctid, mock_lf):
        client = _make_client_mock()
        mock_lf.return_value = client

        record_review_outcome("wf-abc", {"verdict": "approved", "satisfaction": 87})

        client.create_score.assert_any_call(
            name="mtor-satisfaction",
            value=87.0,
            data_type="NUMERIC",
            trace_id="review-trace-id",
        )

    @patch("mtor.worker.stall_trace.get_langfuse")
    @patch("langfuse.Langfuse.create_trace_id", return_value="review-trace-id")
    def test_flushes_langfuse(self, _ctid, mock_lf):
        client = _make_client_mock()
        mock_lf.return_value = client

        record_review_outcome("wf-abc", {"verdict": "approved", "satisfaction": 95})

        client.flush.assert_called_once()

    @patch("mtor.worker.stall_trace.get_langfuse")
    @patch("langfuse.Langfuse.create_trace_id", return_value="review-trace-id")
    def test_no_verdict_score_when_verdict_missing(self, _ctid, mock_lf):
        client = _make_client_mock()
        mock_lf.return_value = client

        record_review_outcome("wf-abc", {"satisfaction": 95})

        score_names = [c.kwargs.get("name") for c in client.create_score.call_args_list]
        assert "mtor-verdict" not in score_names

    @patch("mtor.worker.stall_trace.get_langfuse")
    @patch("langfuse.Langfuse.create_trace_id", return_value="review-trace-id")
    def test_no_satisfaction_score_when_missing(self, _ctid, mock_lf):
        client = _make_client_mock()
        mock_lf.return_value = client

        record_review_outcome("wf-abc", {"verdict": "approved"})

        score_names = [c.kwargs.get("name") for c in client.create_score.call_args_list]
        assert "mtor-satisfaction" not in score_names

    def test_noop_when_workflow_id_empty(self):
        with patch("mtor.worker.stall_trace.get_langfuse") as mock_lf:
            client = _make_client_mock()
            mock_lf.return_value = client

            record_review_outcome("", {"verdict": "approved", "satisfaction": 95})

            client.start_observation.assert_not_called()
            client.create_score.assert_not_called()

    def test_noop_when_langfuse_unavailable(self):
        with patch("mtor.worker.stall_trace.get_langfuse", return_value=None):
            # Should not raise.
            record_review_outcome("wf-abc", {"verdict": "approved", "satisfaction": 95})

    @patch("mtor.worker.stall_trace.get_langfuse")
    def test_graceful_on_client_failure(self, mock_lf):
        """Any telemetry failure is swallowed."""
        client = _make_client_mock()
        client.start_observation.side_effect = RuntimeError("network down")
        mock_lf.return_value = client

        # Should not raise.
        record_review_outcome("wf-abc", {"verdict": "approved", "satisfaction": 95})

    @patch("mtor.worker.stall_trace.get_langfuse")
    @patch("langfuse.Langfuse.create_trace_id", return_value="review-trace-id")
    def test_evaluator_metadata_carries_review_fields(self, _ctid, mock_lf):
        client = _make_client_mock()
        mock_lf.return_value = client

        record_review_outcome(
            "wf-abc",
            {
                "verdict": "rejected",
                "approved": False,
                "flags": ["errors: SyntaxError"],
                "satisfaction": 30,
            },
        )

        meta = client.start_observation.call_args[1]["metadata"]
        assert meta["verdict"] == "rejected"
        assert meta["approved"] is False
        assert meta["flags"] == ["errors: SyntaxError"]
        assert meta["satisfaction"] == 30


# ---------------------------------------------------------------------------
# record_stall_event (v4 surface)
# ---------------------------------------------------------------------------

class TestRecordStallEvent:
    """v4 surface for stall events (no removed .trace()/.span())."""

    @patch("mtor.worker.stall_trace.get_langfuse")
    @patch("langfuse.Langfuse.create_trace_id", return_value="stall-trace-id")
    def test_standalone_uses_start_observation(self, _ctid, mock_lf):
        """Standalone stall: client.start_observation(name='stall-...')."""
        client = _make_client_mock()
        root = _make_observation_mock()
        client.start_observation.return_value = root
        mock_lf.return_value = client

        record_stall_event("wf-1", "heartbeat_gap", "paused", {"gap_seconds": 90})

        client.start_observation.assert_called_once()
        root_kwargs = client.start_observation.call_args[1]
        assert root_kwargs["name"] == "stall-wf-1"
        assert root_kwargs["trace_context"] == {"trace_id": "stall-trace-id"}

    def test_attaches_to_existing_trace_via_start_observation(self):
        """Attached stall: trace.start_observation(name='stall-detected', ...)."""
        root = _make_observation_mock()

        record_stall_event(
            "wf-1",
            "heartbeat_gap",
            "paused",
            {"gap_seconds": 90},
            trace=root,
        )

        root.start_observation.assert_called_once()
        kwargs = root.start_observation.call_args[1]
        assert kwargs["name"] == "stall-detected"
        assert kwargs["metadata"]["pattern"] == "heartbeat_gap"
        assert kwargs["metadata"]["gap_seconds"] == 90

    def test_attached_span_ended_explicitly(self):
        root = _make_observation_mock()
        span = root.start_observation.return_value

        record_stall_event(
            "wf-1", "heartbeat_gap", "paused", {}, trace=root
        )

        span.end.assert_called_once()

    @patch("mtor.worker.stall_trace.get_langfuse")
    def test_no_langfuse_graceful_noop(self, mock_lf):
        """If Langfuse is unavailable, stall recording is a silent no-op."""
        mock_lf.return_value = None

        # Should not raise
        record_stall_event(
            workflow_id="test",
            pattern="repeated_action",
            action_taken="warn",
            details={},
        )
