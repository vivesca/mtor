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


@patch("temporalio.client.Client.connect")
def test_setup_search_attrs_cli(mock_connect):
    """Verify setup-search-attrs command calls operator_service."""
    client = MagicMock()
    client.operator_service.add_search_attributes = AsyncMock()
    mock_connect.return_value = client

    exit_code, data = invoke(["rictor", "setup-search-attrs"])

    assert exit_code == 0
    assert client.operator_service.add_search_attributes.called
    assert data["result"]["status"] == "success"


@patch("mtor.dispatch._worker_sha_plan")
@patch("mtor.dispatch._get_client")
def test_spec_explain_is_read_only_and_includes_dispatch_plan(mock_get_client, mock_worker_sha, tmp_path):
    """Spec explanation returns dispatch inputs without starting Temporal or mutating the spec."""
    repo = tmp_path / "repo"
    repo.mkdir()
    (repo / "assays").mkdir()
    (repo / "assays" / "test_mtor.py").write_text("def test_ok():\n    assert True\n")
    spec_path = tmp_path / "spec.md"
    spec_text = f"""---
title: Explain Test
status: ready
repo: {repo}
scope:
  - mtor/dispatch.py
exclude:
  - secrets.txt
tests:
  run: "uv run pytest assays/test_mtor.py -q"
---

# Explain this dispatch

Update dispatch explanation.
"""
    spec_path.write_text(spec_text)
    mock_worker_sha.return_value = {
        "skipped": False,
        "in_sync": False,
        "auto_deploy_would_occur": True,
        "local_sha": "local",
        "worker_sha": "remote",
        "error": "",
    }

    exit_code, data = invoke(["--spec", str(spec_path), "--explain"])

    assert exit_code == 0
    assert data["ok"] is True
    result = data["result"]
    assert result["would_dispatch"] is True
    assert result["validation"]["ok"] is True
    assert result["spec"]["path"] == str(spec_path)
    assert result["spec"]["scope"] == ["mtor/dispatch.py"]
    assert result["worker_sha"]["auto_deploy_would_occur"] is True
    assert result["workflow_id"].startswith("ribosome-")
    assert result["search_attributes"]["mtor_spec"] == str(spec_path)
    assert result["planned_spec_frontmatter_mutation"]["status"] == "dispatched"
    assert "workflow_id:" not in spec_path.read_text()
    assert not mock_get_client.called


@patch("mtor.dispatch._worker_sha_plan")
@patch("mtor.dispatch._get_client")
def test_prompt_explain_reports_dedup_and_risk_without_dispatch(mock_get_client, mock_worker_sha):
    mock_worker_sha.return_value = {
        "skipped": False,
        "in_sync": True,
        "auto_deploy_would_occur": False,
        "local_sha": "same",
        "worker_sha": "same",
        "error": "",
    }

    exit_code, data = invoke(["Write tests for mtor dispatch explanation", "--explain"])

    assert exit_code == 0
    result = data["result"]
    assert result["prompt_hash"]
    assert result["dedup"]["key"]
    assert result["dedup"]["blocked"] is False
    assert result["risk"] == "low"
    assert result["provider"]["selected"] == "zhipu"
    assert result["would_dispatch"] is True
    assert not mock_get_client.called


@patch("mtor.dispatch._worker_sha_plan")
def test_explain_reports_pause_and_freeze_as_blocked_plan(mock_worker_sha):
    mock_worker_sha.return_value = {
        "skipped": False,
        "in_sync": True,
        "auto_deploy_would_occur": False,
        "local_sha": "same",
        "worker_sha": "same",
        "error": "",
    }

    with patch("mtor.cli._is_paused", return_value=True), patch("mtor.cli._is_frozen", return_value=True):
        exit_code, data = invoke(["Improve mtor robustness safely", "--explain"])

    assert exit_code == 0
    result = data["result"]
    assert result["would_dispatch"] is False
    assert "paused" in result["blocked_reasons"]
    assert "frozen" in result["blocked_reasons"]
    assert result["pause"]["paused"] is True
    assert result["freeze"]["frozen"] is True
