"""Tests for mtor.worker.stall_trace."""
from unittest.mock import Mock, patch

from mtor.worker import stall_trace


def test_get_langfuse_unavailable():
    """Test get_langfuse returns None when Langfuse is not available."""
    # Save original client and reset for test
    original_client = stall_trace._langfuse_client
    stall_trace._langfuse_client = None
    
    with patch("langfuse.Langfuse", side_effect=ImportError("No module")):
        result = stall_trace.get_langfuse()
        assert result is None
    
    # Restore original
    stall_trace._langfuse_client = original_client


def test_get_langfuse_cached():
    """Test get_langfuse reuses cached client."""
    original_client = stall_trace._langfuse_client
    mock_client = Mock()
    stall_trace._langfuse_client = mock_client
    
    result = stall_trace.get_langfuse()
    assert result is mock_client
    
    # Restore original
    stall_trace._langfuse_client = original_client


def test_create_task_trace_no_langfuse():
    """Test create_task_trace returns None when Langfuse unavailable."""
    with patch.object(stall_trace, "get_langfuse", return_value=None):
        trace = stall_trace.create_task_trace("test task", "github", "workflow-123")
        assert trace is None


def test_create_task_trace_success():
    """Test create_task_trace creates trace correctly when Langfuse available."""
    mock_lf = Mock()
    mock_trace = Mock()
    mock_lf.trace.return_value = mock_trace
    
    with patch.object(stall_trace, "get_langfuse", return_value=mock_lf):
        result = stall_trace.create_task_trace("test task", "github", "workflow-123")
        
        mock_lf.trace.assert_called_once()
        call_args = mock_lf.trace.call_args
        assert call_args.kwargs["name"] == "ribosome-workflow-123"
        assert call_args.kwargs["metadata"]["provider"] == "github"
        assert call_args.kwargs["metadata"]["task"] == "test task"
        assert call_args.kwargs["metadata"]["workflow_id"] == "workflow-123"
        assert call_args.kwargs["metadata"]["input"] == "test task"
        assert call_args.kwargs["tags"] == ["github"]
        assert result == mock_trace


def test_create_task_trace_handles_exception():
    """Test create_task_trace returns None when trace creation fails."""
    mock_lf = Mock()
    mock_lf.trace.side_effect = RuntimeError("Something went wrong")
    
    with patch.object(stall_trace, "get_langfuse", return_value=mock_lf):
        result = stall_trace.create_task_trace("test task", "github")
        assert result is None


def test_finalize_trace_none():
    """Test finalize_trace is no-op when trace is None."""
    # Should not raise
    stall_trace.finalize_trace(None, {})


def test_finalize_trace_success():
    """Test finalize_trace adds generation span and flushes."""
    mock_trace = Mock()
    mock_lf = Mock()
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
        "satisfaction": 5
    }
    
    with patch.object(stall_trace, "get_langfuse", return_value=mock_lf):
        stall_trace.finalize_trace(mock_trace, result_dict)
        
        mock_trace.generation.assert_called_once()
        call_args = mock_trace.generation.call_args
        assert call_args.kwargs["name"] == "ribosome-execution"
        assert call_args.kwargs["model"] == "openai"
        assert call_args.kwargs["input"] == "test task"
        assert call_args.kwargs["output"] == "all good"
        assert call_args.kwargs["metadata"]["exit_code"] == 0
        assert call_args.kwargs["metadata"]["success"] is True
        assert call_args.kwargs["metadata"]["verdict"] == "approve"
        assert call_args.kwargs["metadata"]["flags"] == ["safe", "merge"]
        mock_lf.flush.assert_called_once()


def test_finalize_trace_handles_exception():
    """Test finalize_trace gracefully handles exceptions."""
    mock_trace = Mock()
    mock_trace.generation.side_effect = RuntimeError("Oops")
    
    with patch.object(stall_trace, "get_langfuse", return_value=Mock()):
        # Should not raise
        stall_trace.finalize_trace(mock_trace, {})


def test_create_span_none():
    """Test create_span is no-op when trace is None."""
    # Should not raise
    stall_trace.create_span(None, "test-span")


def test_create_span_success():
    """Test create_span adds span to trace correctly."""
    mock_trace = Mock()
    
    stall_trace.create_span(mock_trace, "processing-step", duration=10, status="waiting")
    
    mock_trace.span.assert_called_once_with(name="processing-step", metadata={"duration": 10, "status": "waiting"})


def test_create_span_handles_exception():
    """Test create_span gracefully handles exceptions."""
    mock_trace = Mock()
    mock_trace.span.side_effect = RuntimeError("Failed to create span")
    
    # Should not raise
    stall_trace.create_span(mock_trace, "test-span")
