"""Tests for Langfuse-backed cross-workflow stall analytics."""

from types import SimpleNamespace
from unittest.mock import MagicMock, patch

from mtor.worker.stall_trace import most_common_stall_pattern, stall_rate


def _observation(trace_id: str, pattern: str):
    return SimpleNamespace(trace_id=trace_id, metadata={"pattern": pattern})


@patch("mtor.worker.stall_trace.get_langfuse")
def test_stall_rate_counts_each_stalled_trace_once(mock_get_langfuse):
    client = MagicMock()
    client.api.trace.list.return_value = SimpleNamespace(
        meta=SimpleNamespace(total_items=4)
    )
    client.api.observations.get_many.return_value = SimpleNamespace(
        data=[
            _observation("trace-a", "heartbeat_gap"),
            _observation("trace-a", "repeated_action"),
            _observation("trace-b", "heartbeat_gap"),
        ],
        meta=SimpleNamespace(cursor=None),
    )
    mock_get_langfuse.return_value = client

    assert stall_rate(24) == 0.5
    client.api.trace.list.assert_called_once()
    client.api.observations.get_many.assert_called_once()


@patch("mtor.worker.stall_trace.get_langfuse")
def test_stall_rate_skips_observation_query_when_no_traces(mock_get_langfuse):
    client = MagicMock()
    client.api.trace.list.return_value = SimpleNamespace(
        meta=SimpleNamespace(total_items=0)
    )
    mock_get_langfuse.return_value = client

    assert stall_rate(24) == 0.0
    client.api.observations.get_many.assert_not_called()


@patch("mtor.worker.stall_trace.get_langfuse")
def test_most_common_pattern_follows_cursor_pages_and_breaks_ties(mock_get_langfuse):
    client = MagicMock()
    client.api.observations.get_many.side_effect = [
        SimpleNamespace(
            data=[_observation("trace-a", "ping_pong")],
            meta=SimpleNamespace(cursor="next"),
        ),
        SimpleNamespace(
            data=[
                _observation("trace-b", "repeated_action"),
                _observation("trace-c", "ping_pong"),
                _observation("trace-d", "repeated_action"),
            ],
            meta=SimpleNamespace(cursor=None),
        ),
    ]
    mock_get_langfuse.return_value = client

    assert most_common_stall_pattern(24) == "ping_pong"
    assert client.api.observations.get_many.call_count == 2


@patch("mtor.worker.stall_trace.get_langfuse")
def test_stall_queries_fail_closed(mock_get_langfuse):
    client = MagicMock()
    client.api.trace.list.side_effect = RuntimeError("offline")
    client.api.observations.get_many.side_effect = RuntimeError("offline")
    mock_get_langfuse.return_value = client

    assert stall_rate(24) == 0.0
    assert most_common_stall_pattern(24) is None


@patch("mtor.worker.stall_trace.get_langfuse")
def test_nonpositive_window_avoids_remote_queries(mock_get_langfuse):
    mock_get_langfuse.return_value = MagicMock()

    assert stall_rate(0) == 0.0
    assert most_common_stall_pattern(-1) is None

    mock_get_langfuse.assert_called()
