"""Tests for Temporal search attributes integration."""

from __future__ import annotations

import io
import json
import subprocess
import sys
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from mtor.cli import app
from mtor.dispatch import _dispatch_prompt, _worker_target_repo_state


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


def _completed(stdout: str = "", stderr: str = "", returncode: int = 0):
    return subprocess.CompletedProcess([], returncode, stdout=stdout, stderr=stderr)


def test_worker_target_repo_state_blocks_worker_head_drift():
    with patch("mtor.dispatch.subprocess.run") as mock_run:
        mock_run.side_effect = [
            _completed(stdout="local-sha\n"),
            _completed(
                stdout=(
                    "BRANCH:main\n"
                    "HEAD:worker-sha\n"
                    "ORIGIN_MAIN:worker-sha\n"
                    "MTOR_STATUS_START\n"
                )
            ),
        ]

        state = _worker_target_repo_state("~/code/mtor")

    assert state["ok"] is False
    assert state["local_sha"] == "local-sha"
    assert state["worker_sha"] == "worker-sha"
    assert "local target HEAD" in state["detail"]


def test_worker_target_repo_state_blocks_origin_head_drift():
    with patch("mtor.dispatch.subprocess.run") as mock_run:
        mock_run.side_effect = [
            _completed(stdout="same-sha\n"),
            _completed(
                stdout=(
                    "BRANCH:main\n"
                    "HEAD:same-sha\n"
                    "ORIGIN_MAIN:origin-sha\n"
                    "MTOR_STATUS_START\n"
                )
            ),
        ]

        state = _worker_target_repo_state("~/code/mtor")

    assert state["ok"] is False
    assert state["origin_sha"] == "origin-sha"
    assert "origin/main" in state["detail"]


def test_worker_target_repo_state_allows_worktrees_noise():
    with patch("mtor.dispatch.subprocess.run") as mock_run:
        mock_run.side_effect = [
            _completed(stdout="same-sha\n"),
            _completed(
                stdout=(
                    "BRANCH:main\n"
                    "HEAD:same-sha\n"
                    "ORIGIN_MAIN:same-sha\n"
                    "MTOR_STATUS_START\n"
                    "?? .worktrees/\n"
                    "?? .worktrees/task-a/file.txt\n"
                )
            ),
        ]

        state = _worker_target_repo_state("~/code/mtor")

    assert state["ok"] is True
    assert state["dirty"] is False


def test_worker_target_repo_state_uses_worker_usable_path_for_users_repo():
    with patch("mtor.dispatch.subprocess.run") as mock_run:
        mock_run.side_effect = [
            _completed(stdout="same-sha\n"),
            _completed(
                stdout=(
                    "BRANCH:main\n"
                    "HEAD:same-sha\n"
                    "ORIGIN_MAIN:same-sha\n"
                    "MTOR_STATUS_START\n"
                )
            ),
        ]

        state = _worker_target_repo_state("/Users/terry/code/chiasma")

    assert state["ok"] is True
    ssh_cmd = mock_run.call_args_list[1].args[0][2]
    assert "cd /home/vivesca/code/chiasma &&" in ssh_cmd
    assert "'~/code/chiasma'" not in ssh_cmd
    assert '"~/code/chiasma"' not in ssh_cmd


@patch("mtor.dispatch._get_client")
@patch("mtor.dispatch._worker_target_repo_state")
@patch("mtor.dispatch._check_worker_sha")
def test_dispatch_prompt_blocks_before_starting_workflow_on_target_repo_drift(
    mock_sha, mock_target_repo, mock_get_client
):
    client = MagicMock()
    client.start_workflow = AsyncMock()
    mock_get_client.return_value = (client, None)
    mock_sha.return_value = True
    mock_target_repo.return_value = {
        "ok": False,
        "skipped": False,
        "local_sha": "local",
        "worker_sha": "worker",
        "origin_sha": "worker",
        "branch": "main",
        "dirty": False,
        "detail": "worker target HEAD differs from local target HEAD",
    }

    with pytest.raises(SystemExit) as exc:
        _dispatch_prompt("Make assays/test_search_attributes.py pass", repo="~/code/mtor")

    assert exc.value.code == 1
    assert not mock_get_client.called
    client.start_workflow.assert_not_called()


@patch("mtor.dispatch._worker_target_repo_state")
@patch("mtor.dispatch._worker_sha_plan")
@patch("mtor.dispatch._get_client")
def test_dispatch_explanation_includes_target_repo_preflight(
    mock_get_client, mock_worker_sha, mock_target_repo
):
    mock_worker_sha.return_value = {
        "skipped": False,
        "in_sync": True,
        "auto_deploy_would_occur": False,
        "local_sha": "same",
        "worker_sha": "same",
        "error": "",
    }
    mock_target_repo.return_value = {
        "ok": False,
        "skipped": False,
        "local_sha": "local",
        "worker_sha": "worker",
        "origin_sha": "worker",
        "branch": "main",
        "dirty": False,
        "detail": "worker target HEAD differs from local target HEAD",
    }

    exit_code, data = invoke(["Improve mtor robustness safely", "--explain"])

    assert exit_code == 0
    result = data["result"]
    assert result["would_dispatch"] is False
    assert "target_repo_preflight_failed" in result["blocked_reasons"]
    assert result["target_repo"] == mock_target_repo.return_value
    assert not mock_get_client.called
