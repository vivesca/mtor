"""Tests for Langfuse task tracing in stall_trace.py."""

from unittest.mock import MagicMock, patch
import pytest


def test_create_task_trace_returns_trace():
    """create_task_trace returns a Langfuse trace object when langfuse is available."""
    from mtor.worker.stall_trace import create_task_trace

    mock_client = MagicMock()
    mock_trace = MagicMock()
    mock_client.return_value.trace.return_value = mock_trace

    with patch("mtor.worker.stall_trace.get_langfuse") as mock_get:
        mock_module = MagicMock()
        mock_module.Langfuse = mock_client
        mock_get.return_value = mock_module

        result = create_task_trace("wf-123", "abc", "zhipu", "build")

    assert result is mock_trace
    mock_client.return_value.trace.assert_called_once()
    call_kwargs = mock_client.return_value.trace.call_args[1]
    assert call_kwargs["session_id"] == "wf-123"
    assert call_kwargs["metadata"]["provider"] == "zhipu"
    assert call_kwargs["metadata"]["task_id"] == "abc"
    assert call_kwargs["metadata"]["mode"] == "build"


def test_create_task_trace_returns_none_without_langfuse():
    """create_task_trace returns None when langfuse is not installed."""
    from mtor.worker.stall_trace import create_task_trace

    with patch("mtor.worker.stall_trace.get_langfuse", return_value=None):
        result = create_task_trace("wf-123", "abc", "zhipu", "build")

    assert result is None


def test_create_task_trace_returns_none_on_exception():
    """create_task_trace returns None when Langfuse raises."""
    from mtor.worker.stall_trace import create_task_trace

    with patch("mtor.worker.stall_trace.get_langfuse") as mock_get:
        mock_module = MagicMock()
        mock_module.Langfuse.side_effect = RuntimeError("connection failed")
        mock_get.return_value = mock_module

        result = create_task_trace("wf-123", "abc", "zhipu", "build")

    assert result is None


def test_finalize_trace_adds_outcome_span():
    """finalize_trace adds an outcome span with result metadata."""
    from mtor.worker.stall_trace import finalize_trace

    mock_trace = MagicMock()
    result = {
        "success": True,
        "exit_code": 0,
        "provider": "zhipu",
        "merged": True,
        "post_diff": {"commit_count": 2},
    }

    finalize_trace(mock_trace, result)

    mock_trace.span.assert_called_once()
    span_kwargs = mock_trace.span.call_args[1]
    assert span_kwargs["name"] == "outcome"
    assert span_kwargs["metadata"]["success"] is True
    assert span_kwargs["metadata"]["commit_count"] == 2
    mock_trace.client.flush.assert_called_once()


def test_finalize_trace_noop_on_none():
    """finalize_trace is a no-op when trace is None."""
    from mtor.worker.stall_trace import finalize_trace

    # Should not raise
    finalize_trace(None, {"success": False})


def test_finalize_trace_handles_exception():
    """finalize_trace swallows exceptions gracefully."""
    from mtor.worker.stall_trace import finalize_trace

    mock_trace = MagicMock()
    mock_trace.span.side_effect = RuntimeError("flush failed")

    # Should not raise
    finalize_trace(mock_trace, {"success": False})


def test_record_stall_event_attaches_to_trace():
    """record_stall_event attaches to provided trace instead of creating orphan."""
    from mtor.worker.stall_trace import record_stall_event

    mock_trace = MagicMock()

    with patch("mtor.worker.stall_trace.get_langfuse") as mock_get:
        mock_get.return_value = MagicMock()
        record_stall_event("wf-123", "frozen", "killed", {"tick": 5}, trace=mock_trace)

    mock_trace.span.assert_called_once()
    span_kwargs = mock_trace.span.call_args[1]
    assert span_kwargs["name"] == "stall-detected"
    assert span_kwargs["metadata"]["pattern"] == "frozen"
