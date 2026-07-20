"""Tests for delegation-experiment dimensions on Langfuse traces."""

from unittest.mock import MagicMock, patch

from mtor.worker.stall_trace import create_task_trace, finalize_trace


@patch("mtor.worker.stall_trace.get_langfuse")
def test_root_trace_records_mode_harness_and_repo(mock_get_langfuse):
    client = MagicMock()
    mock_get_langfuse.return_value = client

    create_task_trace(
        "fix the parser",
        "zhipu",
        "wf-2",
        mode="bugfix",
        harness="claude",
        repo="/work/parser",
    )

    metadata = client.start_observation.call_args.kwargs["metadata"]
    assert metadata["mode"] == "bugfix"
    assert metadata["harness"] == "claude"
    assert metadata["repo"] == "/work/parser"


@patch("mtor.worker.stall_trace.get_langfuse")
def test_root_trace_normalizes_empty_harness_to_ribosome(mock_get_langfuse):
    client = MagicMock()
    mock_get_langfuse.return_value = client

    create_task_trace("task", "zhipu", "wf-3")

    metadata = client.start_observation.call_args.kwargs["metadata"]
    assert metadata["harness"] == "ribosome"


@patch("mtor.worker.stall_trace.get_langfuse")
def test_execution_observation_records_harness(mock_get_langfuse):
    mock_get_langfuse.return_value = MagicMock()
    root = MagicMock()

    finalize_trace(
        root,
        {
            "stdout": "ok",
            "exit_code": 0,
            "provider": "zhipu",
            "cost_info": "",
            "success": True,
            "stderr": "",
            "harness": "claude",
        },
    )

    metadata = root.start_observation.call_args.kwargs["metadata"]
    assert metadata["harness"] == "claude"
