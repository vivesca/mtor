"""Tests for mtor.worker.stall_trace.

Updated for the Langfuse v4 surface: the v2 ``.trace()`` / ``.generation()``
/ ``.span()`` calls have been removed. The new API is
``client.start_observation(as_type=..., trace_context=..., ...)`` returning
a root observation, plus ``observation.start_observation(...)`` for
children, with explicit ``.end()`` on every observation.
"""

from unittest.mock import MagicMock, Mock, patch

from langfuse import Langfuse
from langfuse._client.span import LangfuseSpan

from mtor.worker import stall_trace


def _make_client() -> Mock:
    """Mock client spec'd to v4 so removed v2 attrs raise AttributeError."""
    client = MagicMock(spec=Langfuse)
    client.start_observation.return_value = MagicMock(spec=LangfuseSpan)
    return client


def _make_observation() -> Mock:
    """Mock observation spec'd to v4 so removed v2 attrs raise AttributeError."""
    obs = MagicMock(spec=LangfuseSpan)
    obs.start_observation.return_value = MagicMock(spec=LangfuseSpan)
    return obs


def test_get_langfuse_unavailable():
    """Test get_langfuse returns None when Langfuse is not available."""
    original_client = stall_trace._langfuse_client
    stall_trace._langfuse_client = None

    with patch("langfuse.Langfuse", side_effect=ImportError("No module")):
        result = stall_trace.get_langfuse()
        assert result is None

    stall_trace._langfuse_client = original_client


def test_get_langfuse_cached():
    """Test get_langfuse reuses cached client."""
    original_client = stall_trace._langfuse_client
    mock_client = Mock()
    stall_trace._langfuse_client = mock_client

    result = stall_trace.get_langfuse()
    assert result is mock_client

    stall_trace._langfuse_client = original_client


def test_create_task_trace_no_langfuse():
    """Test create_task_trace returns None when Langfuse unavailable."""
    with patch.object(stall_trace, "get_langfuse", return_value=None):
        trace = stall_trace.create_task_trace("test task", "github", "workflow-123")
        assert trace is None


def test_create_task_trace_success():
    """v4: create_task_trace calls client.start_observation(as_type='agent')."""
    mock_lf = _make_client()
    mock_obs = _make_observation()
    mock_lf.start_observation.return_value = mock_obs

    with patch.object(stall_trace, "get_langfuse", return_value=mock_lf):
        result = stall_trace.create_task_trace("test task", "github", "workflow-123")

        mock_lf.start_observation.assert_called_once()
        call_args = mock_lf.start_observation.call_args
        assert call_args.kwargs["name"] == "ribosome-workflow-123"
        assert call_args.kwargs["as_type"] == "agent"
        assert call_args.kwargs["input"] == "test task"
        assert call_args.kwargs["metadata"]["provider"] == "github"
        assert call_args.kwargs["metadata"]["task"] == "test task"
        assert call_args.kwargs["metadata"]["workflow_id"] == "workflow-123"
        assert call_args.kwargs["metadata"]["input"] == "test task"
        assert result is mock_obs


def test_create_task_trace_handles_exception():
    """Test create_task_trace returns None when observation creation fails."""
    mock_lf = _make_client()
    mock_lf.start_observation.side_effect = RuntimeError("Something went wrong")

    with patch.object(stall_trace, "get_langfuse", return_value=mock_lf):
        result = stall_trace.create_task_trace("test task", "github")
        assert result is None


def test_finalize_trace_none():
    """Test finalize_trace is no-op when trace is None."""
    # Should not raise
    stall_trace.finalize_trace(None, {})


def test_finalize_trace_success():
    """v4: finalize_trace adds a generation observation and flushes."""
    mock_root = _make_observation()
    mock_lf = _make_client()
    result_dict = {
        "verdict": "approve",
        "flags": ["safe", "merge"],
        "exit_code": 0,
        "provider": "openai",
        "task": "test task",
        "stdout": "all good",
        "stderr": "",
        "success": True,
        "mode": "sequential",
        "branch_name": "feature/test",
        "merged": True,
        "post_diff": {"stat": "10 files changed"},
        "cost_info": "$0.05",
        "satisfaction": 5,
    }

    with patch.object(stall_trace, "get_langfuse", return_value=mock_lf):
        stall_trace.finalize_trace(mock_root, result_dict)

        mock_root.start_observation.assert_called_once()
        call_args = mock_root.start_observation.call_args
        assert call_args.kwargs["name"] == "ribosome-execution"
        assert call_args.kwargs["as_type"] == "generation"
        assert call_args.kwargs["model"] == "openai"
        assert call_args.kwargs["input"] == "test task"
        assert call_args.kwargs["output"] == "all good"
        assert call_args.kwargs["metadata"]["exit_code"] == 0
        assert call_args.kwargs["metadata"]["success"] is True
        assert call_args.kwargs["metadata"]["verdict"] == "approve"
        assert call_args.kwargs["metadata"]["flags"] == ["safe", "merge"]
        # v4: both generation and root are ended explicitly.
        mock_root.start_observation.return_value.end.assert_called_once()
        mock_root.end.assert_called_once()
        mock_lf.flush.assert_called_once()


def test_finalize_trace_handles_exception():
    """Test finalize_trace gracefully handles exceptions."""
    mock_root = _make_observation()
    mock_root.start_observation.side_effect = RuntimeError("Oops")

    with patch.object(stall_trace, "get_langfuse", return_value=_make_client()):
        # Should not raise
        stall_trace.finalize_trace(mock_root, {})


def test_create_span_none():
    """Test create_span is no-op when trace is None."""
    # Should not raise
    stall_trace.create_span(None, "test-span")


def test_create_span_success():
    """v4: create_span adds a child observation via start_observation + end."""
    mock_root = _make_observation()

    stall_trace.create_span(mock_root, "processing-step", duration=10, status="waiting")

    mock_root.start_observation.assert_called_once_with(
        name="processing-step", metadata={"duration": 10, "status": "waiting"}
    )
    # v4: the child observation is ended explicitly.
    mock_root.start_observation.return_value.end.assert_called_once()


def test_create_span_handles_exception():
    """Test create_span gracefully handles exceptions."""
    mock_root = _make_observation()
    mock_root.start_observation.side_effect = RuntimeError("Failed to create span")

    # Should not raise
    stall_trace.create_span(mock_root, "test-span")
