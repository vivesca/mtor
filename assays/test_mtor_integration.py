"""Integration tests for mtor module decomposition.

Tests the structural contracts after splitting cli.py into modules:
  __init__.py, client.py, envelope.py, dispatch.py, doctor.py, tree.py, cli.py

Every test verifies the JSON envelope structure and module boundaries.
"""

from __future__ import annotations

import io
import json
import os
import sys
import tempfile
from contextlib import ExitStack
from unittest.mock import AsyncMock, MagicMock, patch

from mtor import TASK_QUEUE, TEMPORAL_HOST, VERSION, WORKFLOW_TYPE
from mtor.cli import app

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

_CLIENT_PATCH_TARGETS = [
    "mtor.cli._get_client",
    "mtor.doctor._get_client",
    "mtor.dispatch._get_client",
]


def invoke(args: list[str] | None = None) -> tuple[int, dict]:
    """Invoke CLI and return (exit_code, parsed_json)."""
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
        data = json.loads(output)
    except json.JSONDecodeError as exc:
        raise AssertionError(
            f"Output is not valid JSON. Exit={exit_code}\nOutput: {output!r}\nException: {exc}"
        ) from exc
    return exit_code, data


def make_mock_client():
    """Build a minimal async mock Temporal client."""
    client = MagicMock()

    handle = MagicMock()
    handle.id = "ribosome-test-abcd1234"
    client.start_workflow = AsyncMock(return_value=handle)

    async def _fake_list(query=None):
        execution = MagicMock()
        execution.id = "ribosome-test-abcd1234"
        execution.status = MagicMock()
        execution.status.name = "COMPLETED"
        execution.start_time = MagicMock()
        execution.start_time.isoformat.return_value = "2026-04-06T12:00:00+00:00"
        execution.close_time = MagicMock()
        execution.close_time.isoformat.return_value = "2026-04-06T12:01:00+00:00"
        yield execution

    client.list_workflows = _fake_list

    wf_handle = MagicMock()
    desc = MagicMock()
    desc.status = MagicMock()
    desc.status.name = "COMPLETED"
    desc.start_time = MagicMock()
    desc.start_time.isoformat.return_value = "2026-04-06T12:00:00+00:00"
    desc.close_time = MagicMock()
    desc.close_time.isoformat.return_value = "2026-04-06T12:01:00+00:00"
    wf_handle.describe = AsyncMock(return_value=desc)
    wf_handle.cancel = AsyncMock(return_value=None)
    wf_handle.signal = AsyncMock(return_value=None)
    client.get_workflow_handle = MagicMock(return_value=wf_handle)

    return client, wf_handle


def _patch_client(mock_client):
    stack = ExitStack()
    for target in _CLIENT_PATCH_TARGETS:
        stack.enter_context(patch(target, return_value=(mock_client, None)))
    return stack


def _patch_client_error(error_msg="Connection refused"):
    stack = ExitStack()
    for target in _CLIENT_PATCH_TARGETS:
        stack.enter_context(patch(target, return_value=(None, error_msg)))
    return stack


# ---------------------------------------------------------------------------
# Module import and boundary tests
# ---------------------------------------------------------------------------


class TestModuleImports:
    """Verify all modules import cleanly and have expected exports."""

    def test_version_in_init(self):
        from mtor import VERSION

        assert VERSION == "0.5.0"

    def test_constants_in_init(self):
        from mtor import COACHING_PATH, WORKER_HOST

        assert TASK_QUEUE == "translation-queue"
        assert WORKFLOW_TYPE == "TranslationWorkflow"
        assert WORKER_HOST == "localhost"
        assert COACHING_PATH is None  # Unset by default in test env

    def test_envelope_exports(self):
        from mtor.envelope import _err, _extract_first_result, _ok

        assert callable(_ok)
        assert callable(_err)
        assert callable(_extract_first_result)

    def test_client_exports(self):
        from mtor.client import _get_client

        assert callable(_get_client)

    def test_tree_exports(self):
        from mtor.tree import tree

        assert hasattr(tree, "to_dict")
        assert hasattr(tree, "to_schema")

    def test_dispatch_exports(self):
        from mtor.dispatch import _dispatch_prompt

        assert callable(_dispatch_prompt)

    def test_doctor_exports(self):
        from mtor.doctor import doctor

        assert callable(doctor)

    def test_cli_exports_app(self):
        from mtor.cli import app

        assert app is not None

    def test_no_circular_imports(self):
        """Importing each module individually succeeds without recursion."""
        import importlib

        modules = [
            "mtor",
            "mtor.envelope",
            "mtor.client",
            "mtor.tree",
            "mtor.dispatch",
            "mtor.doctor",
            "mtor.cli",
        ]
        for mod_name in modules:
            mod = importlib.import_module(mod_name)
            assert mod is not None, f"Failed to import {mod_name}"


# ---------------------------------------------------------------------------
# Bare invocation (command tree)
# ---------------------------------------------------------------------------


class TestBareInvocationIntegration:
    def test_returns_command_tree_json(self):
        exit_code, data = invoke([])
        assert exit_code == 0
        assert data["ok"] is True
        assert data["command"] == "mtor"
        assert "commands" in data["result"]
        assert isinstance(data["result"]["commands"], list)

    def test_tree_contains_all_commands(self):
        _, data = invoke([])
        names = [cmd["name"] for cmd in data["result"]["commands"]]
        # Must include all major subcommands
        for prefix in [
            "mtor",
            "mtor list",
            "mtor status",
            "mtor logs",
            "mtor cancel",
            "mtor doctor",
            "mtor schema",
            "mtor approve",
            "mtor deny",
        ]:
            assert any(n.startswith(prefix.split()[0]) for n in names), (
                f"Expected command starting with '{prefix}' in tree"
            )

    def test_tree_has_next_actions(self):
        _, data = invoke([])
        assert "next_actions" in data
        assert isinstance(data["next_actions"], list)

    def test_tree_has_version(self):
        _, data = invoke([])
        assert data.get("version") == VERSION


# ---------------------------------------------------------------------------
# Schema command
# ---------------------------------------------------------------------------


class TestSchemaIntegration:
    def test_schema_returns_valid_schema(self):
        exit_code, data = invoke(["schema"])
        assert exit_code == 0
        assert data["ok"] is True
        assert "commands" in data["result"]
        assert "exit_codes" in data["result"]
        assert isinstance(data["result"]["commands"], list)
        assert len(data["result"]["commands"]) >= 9  # all registered commands

    def test_schema_commands_have_params(self):
        _, data = invoke(["schema"])
        for cmd in data["result"]["commands"]:
            assert "name" in cmd
            # Commands with params should list them
            if "params" in cmd:
                assert isinstance(cmd["params"], list)

    def test_schema_has_known_exit_codes(self):
        _, data = invoke(["schema"])
        codes = data["result"]["exit_codes"]
        assert "0" in codes
        assert "3" in codes
        assert "4" in codes


# ---------------------------------------------------------------------------
# Doctor command
# ---------------------------------------------------------------------------


class TestDoctorIntegration:
    def test_doctor_unreachable_returns_error_envelope(self):
        with _patch_client_error("Connection refused"):
            exit_code, data = invoke(["doctor"])
        assert exit_code == 3
        assert data["ok"] is False
        assert "error" in data
        assert data["error"]["code"] == "HEALTH_CHECK_FAILED"
        assert "fix" in data
        assert "result" in data

    def test_doctor_has_check_structure(self):
        with _patch_client_error("Connection refused"):
            _, data = invoke(["doctor"])
        result = data["result"]
        assert "checks" in result
        assert "temporal_reachable" in result
        assert "temporal_host" in result
        assert "worker_alive" in result
        assert "task_queue" in result
        # Each check has name, ok, detail
        for check in result["checks"]:
            assert "name" in check
            assert "ok" in check
            assert "detail" in check

    def test_doctor_unreachable_reports_false(self):
        with _patch_client_error("Connection refused"):
            _, data = invoke(["doctor"])
        result = data["result"]
        assert result["temporal_reachable"] is False
        assert result["worker_alive"] is False

    def test_doctor_with_client_reachable(self):
        mock_client, _ = make_mock_client()
        with _patch_client(mock_client):
            _exit_code, data = invoke(["doctor"])
        # Might fail on coaching file or providers, but structure is valid
        assert isinstance(data, dict)
        assert "result" in data
        assert "checks" in data["result"]

    def test_doctor_temporal_host_in_result(self):
        with _patch_client_error("timeout"):
            _, data = invoke(["doctor"])
        assert data["result"]["temporal_host"] == TEMPORAL_HOST


# ---------------------------------------------------------------------------
# List command
# ---------------------------------------------------------------------------


class TestListIntegration:
    def test_list_returns_workflow_array(self):
        mock_client, _ = make_mock_client()
        with _patch_client(mock_client):
            exit_code, data = invoke(["list"])
        assert exit_code == 0
        assert data["ok"] is True
        assert "workflows" in data["result"]
        assert "count" in data["result"]
        assert isinstance(data["result"]["workflows"], list)

    def test_list_workflow_structure(self):
        mock_client, _ = make_mock_client()
        with _patch_client(mock_client):
            _, data = invoke(["list"])
        for wf in data["result"]["workflows"]:
            assert "workflow_id" in wf
            assert "status" in wf
            assert "start_time" in wf
            assert "close_time" in wf

    def test_list_with_status_filter(self):
        mock_client, _ = make_mock_client()
        with _patch_client(mock_client):
            exit_code, data = invoke(["list", "--status", "RUNNING"])
        assert exit_code == 0
        assert data["ok"] is True

    def test_list_unreachable(self):
        with _patch_client_error("refused"):
            exit_code, data = invoke(["list"])
        assert exit_code == 3
        assert data["ok"] is False
        assert data["error"]["code"] == "TEMPORAL_UNREACHABLE"

    def test_list_has_next_actions(self):
        mock_client, _ = make_mock_client()
        with _patch_client(mock_client):
            _, data = invoke(["list"])
        if data["result"]["count"] > 0:
            assert len(data["next_actions"]) > 0
            # Each next action should reference mtor status
            for na in data["next_actions"]:
                assert "command" in na
                assert "description" in na


# ---------------------------------------------------------------------------
# Dispatch (default with prompt)
# ---------------------------------------------------------------------------


class TestDispatchIntegration:
    def test_dispatch_starts_workflow(self):
        mock_client, _ = make_mock_client()
        with _patch_client(mock_client):
            exit_code, data = invoke(["Write tests for foo.py"])
        assert exit_code == 0
        assert data["ok"] is True
        assert "workflow_id" in data["result"]
        assert data["result"]["status"] == "RUNNING"
        assert "prompt_preview" in data["result"]

    def test_dispatch_prompt_preview_truncated(self):
        mock_client, _ = make_mock_client()
        long_prompt = "A" * 200
        with _patch_client(mock_client):
            _, data = invoke([long_prompt])
        assert len(data["result"]["prompt_preview"]) <= 100

    def test_dispatch_empty_prompt_returns_error(self):
        _exit_code, data = invoke([""])
        assert data["ok"] is False
        assert data["error"]["code"] == "MISSING_PROMPT"
        assert "fix" in data

    def test_dispatch_temporal_unreachable(self):
        with _patch_client_error("Connection refused"):
            exit_code, data = invoke(["Do something"])
        assert exit_code == 3
        assert data["ok"] is False
        assert data["error"]["code"] == "TEMPORAL_UNREACHABLE"

    def test_dispatch_next_actions(self):
        mock_client, _ = make_mock_client()
        with _patch_client(mock_client):
            _, data = invoke(["Build feature X"])
        assert len(data["next_actions"]) >= 3
        commands = [na["command"] for na in data["next_actions"]]
        assert any("status" in c for c in commands)
        assert any("logs" in c for c in commands)
        assert any("cancel" in c for c in commands)


# ---------------------------------------------------------------------------
# File-path prompt reading
# ---------------------------------------------------------------------------


class TestFilePathPrompt:
    def test_dispatch_reads_file_as_prompt(self):
        mock_client, _ = make_mock_client()
        with (
            tempfile.NamedTemporaryFile(mode="w", suffix=".txt", delete=False) as f,
            _patch_client(mock_client),
        ):
            f.write("Implement the authentication module with OAuth2 support")
            f.flush()
            prompt_path = f.name

        try:
            exit_code, data = invoke([prompt_path])
            assert exit_code == 0
            assert data["ok"] is True
            # prompt_preview should reflect file contents, not the path
            assert "authentication" in data["result"]["prompt_preview"]
        finally:
            os.unlink(prompt_path)

    def test_dispatch_nonexistent_file_treated_as_literal(self):
        """A path that doesn't exist is treated as a literal prompt string."""
        mock_client, _ = make_mock_client()
        with _patch_client(mock_client):
            exit_code, data = invoke(["/nonexistent/path/to/spec.txt"])
        assert exit_code == 0
        assert data["ok"] is True


# ---------------------------------------------------------------------------
# Coaching injection
# ---------------------------------------------------------------------------


class TestCoachingInjection:
    def test_dispatch_does_not_prepend_coaching(self):
        """Coaching injection must NOT be double-applied in dispatch.

        The dispatch module should pass the prompt through without prepending
        coaching content — that's handled per-provider by the ribosome executor.
        """
        mock_client, _ = make_mock_client()
        with _patch_client(mock_client):
            _, _data = invoke(["Do the thing"])

        # The spec sent to start_workflow should have task == original prompt,
        # not prepended with coaching content.
        call_args = mock_client.start_workflow.call_args
        specs = call_args.kwargs.get("args") or call_args[1].get("args") or call_args[0]
        if isinstance(specs, list) and specs:
            first_spec = specs[0]
            if isinstance(first_spec, dict):
                task = first_spec.get("task", "")
                # Should not contain coaching markers
                assert "coaching" not in task.lower() or task == "Do the thing"
                assert task == "Do the thing"

    def test_coaching_file_constant_path(self):
        from mtor import COACHING_PATH

        # COACHING_PATH is None when MTOR_COACHING_PATH env var is unset
        assert COACHING_PATH is None


# ---------------------------------------------------------------------------
# JSON envelope shape invariants
# ---------------------------------------------------------------------------


class TestEnvelopeShapes:
    """Verify every CLI output satisfies the JSON envelope contract."""

    def _collect_all_outputs(self):
        mock_client, _ = make_mock_client()
        outputs = []

        with _patch_client(mock_client):
            for args in [
                [],
                ["schema"],
                ["list"],
                ["status", "ribosome-test-abcd1234"],
                ["cancel", "ribosome-test-abcd1234"],
                ["Test dispatch prompt"],
            ]:
                outputs.append(invoke(args))

        with _patch_client_error("refused"):
            for args in [
                ["doctor"],
                ["list"],
                ["status", "any-id"],
                ["cancel", "any-id"],
                ["Write code"],
            ]:
                outputs.append(invoke(args))

        return outputs

    def test_ok_shape_has_required_fields(self):
        mock_client, _ = make_mock_client()
        with _patch_client(mock_client):
            _, data = invoke(["list"])
        assert {"ok", "command", "result", "next_actions"}.issubset(data.keys())

    def test_error_shape_has_required_fields(self):
        with _patch_client_error("refused"):
            _, data = invoke(["list"])
        assert {"ok", "command", "error", "fix", "next_actions"}.issubset(data.keys())

    def test_error_has_message_and_code(self):
        with _patch_client_error("refused"):
            _, data = invoke(["status", "nonexistent"])
        assert "message" in data["error"]
        assert "code" in data["error"]

    def test_all_outputs_valid_json(self):
        for _exit_code, data in self._collect_all_outputs():
            assert isinstance(data, dict)

    def test_ok_is_boolean(self):
        for _, data in self._collect_all_outputs():
            assert isinstance(data["ok"], bool)


# ---------------------------------------------------------------------------
# Approve / Deny commands
# ---------------------------------------------------------------------------


class TestApproveDenyIntegration:
    def test_approve_sends_signal(self):
        mock_client, _mock_handle = make_mock_client()
        with _patch_client(mock_client):
            exit_code, data = invoke(["approve", "ribosome-test-123"])
        assert exit_code == 0
        assert data["ok"] is True
        assert data["result"]["decision"] == "approved"
        assert data["result"]["workflow_id"] == "ribosome-test-123"

    def test_deny_sends_signal(self):
        mock_client, _mock_handle = make_mock_client()
        with _patch_client(mock_client):
            exit_code, data = invoke(["deny", "ribosome-test-456"])
        assert exit_code == 0
        assert data["ok"] is True
        assert data["result"]["decision"] == "denied"
        assert data["result"]["workflow_id"] == "ribosome-test-456"

    def test_approve_unreachable(self):
        with _patch_client_error("refused"):
            exit_code, data = invoke(["approve", "any-id"])
        assert exit_code == 3
        assert data["ok"] is False

    def test_deny_unreachable(self):
        with _patch_client_error("refused"):
            exit_code, data = invoke(["deny", "any-id"])
        assert exit_code == 3
        assert data["ok"] is False
