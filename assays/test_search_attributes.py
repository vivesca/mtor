"""Tests for Temporal search attributes integration."""

from __future__ import annotations

import io
import json
import sys
from unittest.mock import AsyncMock, MagicMock, patch

from mtor.cli import app


def invoke(args: list[str] | None = None) -> tuple[int, dict]:
    captured = io.StringIO()
    old_stdout = sys.stdout
    exit_code = 0
    try:
        sys.stdout = captured
        app(args or [])
    except SystemExit as exc:
        exit_code = exc.code if isinstance(exc.code, int) else 1
    finally:
        sys.stdout = old_stdout

    output = captured.getvalue()
    try:
        return exit_code, json.loads(output)
    except json.JSONDecodeError:
        return exit_code, {"raw": output}


@patch("mtor.dispatch._get_client")
@patch("mtor.dispatch._check_worker_sha")
def test_dispatch_sets_search_attributes(mock_sha, mock_get_client):
    """Verify that dispatch sets the expected search attributes."""
    client = MagicMock()
    handle = MagicMock()
    handle.id = "test-id"
    client.start_workflow = AsyncMock(return_value=handle)
    mock_get_client.return_value = (client, None)
    mock_sha.return_value = True

    # Use 'scout' command which dispatches with mode='scout'
    # Use --no-wait to avoid polling logs which calls SSH
    exit_code, data = invoke(["scout", "test prompt", "--provider", "zhipu", "--no-wait"])

    assert exit_code == 0
    assert client.start_workflow.called
    kwargs = client.start_workflow.call_args.kwargs
    search_attrs = kwargs.get("search_attributes")
    assert search_attrs is not None

    # Verify search attributes content
    # TypedSearchAttributes is an iterable of SearchAttributePair
    pairs = list(search_attrs)
    keys = {p.key.name: p.value for p in pairs}
    assert keys["mtor_provider"] == "zhipu"
    assert keys["mtor_mode"] == "scout"
    assert "mtor_risk" in keys


@patch("mtor.cli._get_client")
def test_riboseq_uses_search_attribute_filters(mock_get_client):
    """Verify that riboseq (list) uses search attribute filters in Temporal query."""
    client = MagicMock()

    async def _fake_list(query=None):
        if False:
            yield  # make it an async generator
        return
    client.list_workflows = _fake_list
    mock_get_client.return_value = (client, None)

    with patch.object(client, "list_workflows", side_effect=client.list_workflows) as mock_list:
        # Need to mock load_triage as well to avoid FileNotFoundError
        with patch("mtor.cli.load_triage", return_value={}):
            invoke(["riboseq", "--provider", "infini", "--mode", "build", "--verdict", "accepted"])

            assert mock_list.called
            query = mock_list.call_args.kwargs.get("query")
            assert "mtor_provider = 'infini'" in query
            assert "mtor_mode = 'build'" in query
            assert "mtor_verdict = 'accepted'" in query


@patch("mtor.client._get_client")
def test_setup_search_attrs_cli(mock_get_client):
    """Verify setup-search-attrs command calls operator_service."""
    client = MagicMock()
    client.operator_service.add_search_attributes = AsyncMock()
    mock_get_client.return_value = (client, None)

    exit_code, data = invoke(["rictor", "setup-search-attrs"])

    assert exit_code == 0
    assert client.operator_service.add_search_attributes.called
    assert data["result"]["status"] == "success"
