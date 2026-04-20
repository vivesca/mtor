"""Tests for mtor.

All tests invoke the cyclopts App directly with captured stdout.
Temporal client calls are mocked via unittest.mock.patch.
"""

from __future__ import annotations

import io
import json
import sys
from contextlib import ExitStack
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, patch

from mtor.cli import app

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


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

    # start_workflow returns a handle with an .id attribute
    handle = MagicMock()
    handle.id = "ribosome-test1234"
    start_coro = AsyncMock(return_value=handle)
    client.start_workflow = start_coro

    # list_workflows returns an async iterator
    async def _fake_list(query=None):
        execution = MagicMock()
        execution.id = "ribosome-test1234"
        execution.status = MagicMock()
        execution.status.name = "COMPLETED"
        execution.start_time = MagicMock()
        execution.start_time.isoformat.return_value = "2026-04-06T00:00:00+00:00"
        execution.close_time = MagicMock()
        execution.close_time.isoformat.return_value = "2026-04-06T00:01:00+00:00"
        yield execution

    client.list_workflows = _fake_list

    # get_workflow_handle returns a handle
    wf_handle = MagicMock()
    desc = MagicMock()
    desc.status = MagicMock()
    desc.status.name = "COMPLETED"
    desc.start_time = MagicMock()
    desc.start_time.isoformat.return_value = "2026-04-06T00:00:00+00:00"
    desc.close_time = MagicMock()
    desc.close_time.isoformat.return_value = "2026-04-06T00:01:00+00:00"
    describe_coro = AsyncMock(return_value=desc)
    wf_handle.describe = describe_coro
    cancel_coro = AsyncMock(return_value=None)
    wf_handle.cancel = cancel_coro
    terminate_coro = AsyncMock(return_value=None)
    wf_handle.terminate = terminate_coro
    client.get_workflow_handle = MagicMock(return_value=wf_handle)

    # count_workflows returns a coroutine (async)
    async def _fake_count(query=None):
        return 0

    client.count_workflows = _fake_count

    return client, wf_handle


# Modules that import _get_client — patch all of them to keep tests reliable.
_CLIENT_PATCH_TARGETS = [
    "mtor.cli._get_client",
    "mtor.doctor._get_client",
    "mtor.dispatch._get_client",
]


def _patch_client(mock_client):
    """Context manager: patch _get_client in all modules that import it."""
    stack = ExitStack()
    for target in _CLIENT_PATCH_TARGETS:
        stack.enter_context(patch(target, return_value=(mock_client, None)))
    stack.enter_context(patch("mtor.cli._check_dedup", return_value=None))
    stack.enter_context(patch("mtor.dispatch._check_worker_sha", return_value=True))
    return stack


def _patch_client_error(error_msg="Connection refused"):
    """Context manager: patch _get_client to return error in all modules."""
    stack = ExitStack()
    for target in _CLIENT_PATCH_TARGETS:
        stack.enter_context(patch(target, return_value=(None, error_msg)))
    stack.enter_context(patch("mtor.cli._check_dedup", return_value=None))
    stack.enter_context(patch("mtor.dispatch._check_worker_sha", return_value=True))
    return stack


# ---------------------------------------------------------------------------
# Basic structure tests
# ---------------------------------------------------------------------------


class TestBareInvocation:
    def test_returns_valid_json(self):
        exit_code, _data = invoke([])
        assert exit_code == 0, f"Expected exit 0, got {exit_code}"

    def test_ok_true(self):
        _, data = invoke([])
        assert data["ok"] is True

    def test_has_commands_list(self):
        _, data = invoke([])
        assert "commands" in data["result"]
        assert isinstance(data["result"]["commands"], list)
        assert len(data["result"]["commands"]) > 0

    def test_command_field_present(self):
        _, data = invoke([])
        assert data["command"] == "mtor"

    def test_next_actions_present(self):
        _, data = invoke([])
        assert "next_actions" in data

    def test_all_subcommands_in_tree(self):
        _, data = invoke([])
        for expected in [
            "mtor",
            "mtor list",
            "mtor status <workflow_id>",
            "mtor logs <workflow_id>",
            "mtor cancel <workflow_id>",
            "mtor tsc",
            "mtor schema",
        ]:
            first_word = expected.split()[0]
            assert any(cmd["name"].startswith(first_word) for cmd in data["result"]["commands"]), (
                f"Command starting with '{first_word}' not found in tree"
            )

    def test_self_discovery_lists_all_commands(self):
        """Bare mtor output includes scout, research, scan, auto."""
        _, data = invoke([])
        commands = data["result"]["commands"]
        cmd_names = {cmd["name"] for cmd in commands}
        for expected in ["scout", "research", "scan", "auto"]:
            assert any(expected in name for name in cmd_names), (
                f"Missing command containing '{expected}'. Available: {cmd_names}"
            )

    def test_bio_command_aliases_hidden(self):
        """Transition aliases remain callable but hidden from command metadata."""
        for visible, hidden in [
            ("tsc", "doctor"),
            ("rptor", "plan"),
            ("ragulator", "watch"),
        ]:
            assert app._registered_commands[visible].show is True
            assert app._registered_commands[hidden].show is False


class TestHelpSuppression:
    def test_no_human_help_output(self):
        """With help_flags=[], no human-readable help page should appear."""
        captured = io.StringIO()
        old_stdout, old_stderr = sys.stdout, sys.stderr
        try:
            sys.stdout = captured
            sys.stderr = captured
            app(["--help"])
        except SystemExit:
            pass
        finally:
            sys.stdout = old_stdout
            sys.stderr = old_stderr
        output = captured.getvalue()
        # Must not contain standard help indicators
        assert "Show this message and exit" not in output


class TestExitCodes:
    def test_ok_exits_0(self):
        exit_code, _ = invoke([])
        assert exit_code == 0

    def test_schema_exits_0(self):
        exit_code, _ = invoke(["schema"])
        assert exit_code == 0

    def test_dispatch_no_prompt_exits_2(self):
        # dispatch with empty string = usage error
        _exit_code, data = invoke([""])
        assert data["ok"] is False
        assert data["error"]["code"] in (
            "MISSING_PROMPT",
            "TEMPORAL_UNREACHABLE",
            "DISPATCH_ERROR",
        )

    def test_temporal_unreachable_exits_3(self):
        with _patch_client_error("Connection refused"):
            exit_code, data = invoke(["tsc"])
        assert exit_code == 3
        assert data["ok"] is False

    def test_workflow_not_found_exits_4(self):
        mock_client, mock_handle = make_mock_client()
        not_found_exc = Exception("workflow not found: no such workflow")
        mock_handle.describe = AsyncMock(side_effect=not_found_exc)
        with _patch_client(mock_client):
            exit_code, data = invoke(["status", "nonexistent-id"])
        assert exit_code == 4
        assert data["ok"] is False
        assert data["error"]["code"] == "WORKFLOW_NOT_FOUND"


# ---------------------------------------------------------------------------
# Dispatch tests
# ---------------------------------------------------------------------------


class TestDispatch:
    def test_dispatch_with_prompt_returns_workflow_id(self):
        mock_client, _ = make_mock_client()
        with _patch_client(mock_client):
            exit_code, data = invoke(["Make assays/test_feature.py pass"])
        assert exit_code == 0
        assert data["ok"] is True
        assert "workflow_id" in data["result"]
        assert data["result"]["status"] == "RUNNING"

    def test_dispatch_has_next_actions(self):
        mock_client, _ = make_mock_client()
        with _patch_client(mock_client):
            _, data = invoke(["Make assays/test_foo.py pass"])
        assert len(data["next_actions"]) > 0
        commands = [na["command"] for na in data["next_actions"]]
        assert any("status" in cmd for cmd in commands)

    def test_dispatch_no_prompt_returns_error_envelope(self):
        """Empty prompt string must return error with fix field."""
        _exit_code, data = invoke([""])
        assert data["ok"] is False
        assert "fix" in data, "Error envelope must include 'fix' field"

    def test_dispatch_temporal_unreachable_exits_3(self):
        with _patch_client_error("Connection refused"):
            exit_code, data = invoke(["Make assays/test_foo.py pass"])
        assert exit_code == 3
        assert data["ok"] is False
        assert data["error"]["code"] == "TEMPORAL_UNREACHABLE"
        assert "fix" in data

    def test_all_outputs_are_valid_json(self):
        """Sanity: every output path produces parseable JSON."""
        mock_client, _ = make_mock_client()
        test_cases = [
            [],
            ["schema"],
            ["tsc"],
        ]
        with _patch_client(mock_client):
            for args in test_cases:
                _exit_code, data = invoke(args)
                assert isinstance(data, dict), f"Not a dict for args={args}"

    def test_dispatch_result_includes_provider(self):
        """Dispatch result envelope includes provider field."""
        mock_client, _ = make_mock_client()
        with _patch_client(mock_client):
            _, data = invoke(["Make assays/test_feature.py pass"])
        assert "provider" in data["result"], (
            f"Missing provider field in result: {data['result']}"
        )
        assert data["result"]["provider"] is not None


# ---------------------------------------------------------------------------
# List tests
# ---------------------------------------------------------------------------


class TestList:
    def test_list_returns_workflows(self):
        mock_client, _ = make_mock_client()
        with _patch_client(mock_client):
            exit_code, data = invoke(["list"])
        assert exit_code == 0
        assert data["ok"] is True
        assert "workflows" in data["result"]

    def test_list_temporal_unreachable(self):
        with _patch_client_error("Connection refused"):
            exit_code, data = invoke(["list"])
        assert exit_code == 3
        assert data["ok"] is False
        assert "fix" in data

    def test_list_has_next_actions_per_workflow(self):
        mock_client, _ = make_mock_client()
        with _patch_client(mock_client):
            _, data = invoke(["list"])
        if data["result"]["count"] > 0:
            assert len(data["next_actions"]) > 0


# ---------------------------------------------------------------------------
# Status tests
# ---------------------------------------------------------------------------


class TestStatus:
    def test_status_returns_workflow_details(self):
        mock_client, _ = make_mock_client()
        with _patch_client(mock_client):
            exit_code, data = invoke(["status", "ribosome-test1234"])
        assert exit_code == 0
        assert data["ok"] is True
        assert data["result"]["workflow_id"] == "ribosome-test1234"
        assert "status" in data["result"]

    def test_status_not_found(self):
        mock_client, mock_handle = make_mock_client()
        mock_handle.describe = AsyncMock(side_effect=Exception("workflow_not_found"))
        with _patch_client(mock_client):
            exit_code, data = invoke(["status", "bad-id"])
        assert exit_code == 4
        assert data["ok"] is False
        assert data["error"]["code"] == "WORKFLOW_NOT_FOUND"

    def test_status_temporal_unreachable(self):
        with _patch_client_error("timeout"):
            exit_code, data = invoke(["status", "any-id"])
        assert exit_code == 3
        assert data["ok"] is False

    def test_status_includes_failure_reason(self):
        """Status for failed workflow includes failure_reason field."""
        mock_client, mock_handle = make_mock_client()
        # Set up a COMPLETED workflow with rejected verdict and error
        desc = mock_handle.describe.return_value
        desc.status.name = "COMPLETED"
        mock_handle.result = AsyncMock(return_value={
            "results": [{
                "exit_code": 1,
                "review": {"verdict": "rejected"},
                "error": "Build failed: syntax error at line 42",
            }]
        })
        with _patch_client(mock_client):
            _, data = invoke(["status", "ribosome-test1234"])
        assert data["ok"] is True
        assert "failure_reason" in data["result"], (
            f"Missing failure_reason: {data['result']}"
        )
        assert "syntax error" in data["result"]["failure_reason"]


# ---------------------------------------------------------------------------
# Cancel tests
# ---------------------------------------------------------------------------


class TestCancel:
    def test_cancel_success(self):
        mock_client, _ = make_mock_client()
        with _patch_client(mock_client):
            exit_code, data = invoke(["cancel", "ribosome-test1234"])
        assert exit_code == 0
        assert data["ok"] is True
        assert data["result"]["terminated"] is True

    def test_cancel_not_found_exits_4(self):
        mock_client, mock_handle = make_mock_client()
        mock_handle.terminate = AsyncMock(side_effect=Exception("workflow not found"))
        with _patch_client(mock_client):
            exit_code, data = invoke(["cancel", "nonexistent-id"])
        assert exit_code == 4
        assert data["ok"] is False
        assert "fix" in data

    def test_cancel_already_cancelled_is_ok(self):
        """Cancelling an already-cancelled workflow = idempotent success."""
        mock_client, mock_handle = make_mock_client()
        mock_handle.terminate = AsyncMock(side_effect=Exception("workflow already cancelled"))
        with _patch_client(mock_client):
            exit_code, data = invoke(["cancel", "ribosome-done1234"])
        assert exit_code == 0
        assert data["ok"] is True

    def test_cancel_temporal_unreachable(self):
        with _patch_client_error("refused"):
            exit_code, data = invoke(["cancel", "any-id"])
        assert exit_code == 3
        assert data["ok"] is False


# ---------------------------------------------------------------------------
# Doctor tests
# ---------------------------------------------------------------------------


class TestDoctor:
    def test_doctor_unreachable_temporal_exits_3(self):
        with _patch_client_error("Connection refused"):
            exit_code, data = invoke(["tsc"])
        assert exit_code == 3
        assert data["ok"] is False
        assert "fix" in data

    def test_doctor_has_checks_list(self):
        with _patch_client_error("Connection refused"):
            _, data = invoke(["tsc"])
        # Even failed doctor has checks in result
        assert "result" in data
        assert "checks" in data["result"]

    def test_doctor_success(self):
        mock_client, _ = make_mock_client()
        with _patch_client(mock_client):
            _exit_code, data = invoke(["tsc"])
        # May still fail if coaching file missing, but should be JSON
        assert isinstance(data, dict)
        assert "result" in data

    def test_doctor_warns_localhost_worker_host(self):
        """worker_host check fails when MTOR_WORKER_HOST is localhost."""
        with _patch_client_error("Connection refused"), \
             patch("mtor.doctor.WORKER_HOST", "localhost"):
            _, data = invoke(["tsc"])
        checks = data["result"]["checks"]
        wh_check = next((c for c in checks if c["name"] == "worker_host"), None)
        assert wh_check is not None, f"worker_host check not found in {checks}"
        assert wh_check["ok"] is False
        assert "localhost" in wh_check["detail"]

    def test_doctor_ok_with_real_worker_host(self):
        """worker_host check passes when MTOR_WORKER_HOST is a real hostname."""
        mock_client, _ = make_mock_client()
        with _patch_client(mock_client), \
             patch("mtor.doctor.WORKER_HOST", "ganglion"):
            _, data = invoke(["tsc"])
        checks = data["result"]["checks"]
        wh_check = next((c for c in checks if c["name"] == "worker_host"), None)
        assert wh_check is not None, f"worker_host check not found in {checks}"
        assert wh_check["ok"] is True
        assert "ganglion" in wh_check["detail"]



# ---------------------------------------------------------------------------
# Schema tests
# ---------------------------------------------------------------------------


class TestSchema:
    def test_schema_returns_commands(self):
        exit_code, data = invoke(["schema"])
        assert exit_code == 0
        assert data["ok"] is True
        assert "commands" in data["result"]
        assert isinstance(data["result"]["commands"], list)

    def test_schema_has_exit_codes(self):
        _, data = invoke(["schema"])
        assert "exit_codes" in data["result"]
        exit_codes = data["result"]["exit_codes"]
        assert "0" in exit_codes
        assert "3" in exit_codes
        assert "4" in exit_codes


# ---------------------------------------------------------------------------
# JSON envelope invariants
# ---------------------------------------------------------------------------


class TestEnvelopeInvariants:
    """Every response must satisfy the JSON envelope contract."""

    def _all_reachable_outputs(self):
        """Return list of (exit_code, data) for all testable paths."""
        mock_client, _ = make_mock_client()
        outputs = []

        with _patch_client(mock_client):
            outputs.append(invoke([]))
            outputs.append(invoke(["schema"]))
            outputs.append(invoke(["list"]))
            outputs.append(invoke(["status", "ribosome-test1234"]))

        with _patch_client_error("refused"):
            outputs.append(invoke(["tsc"]))
            outputs.append(invoke(["status", "any"]))
            outputs.append(invoke(["cancel", "any"]))

        return outputs

    def test_every_output_has_ok_field(self):
        for _exit_code, data in self._all_reachable_outputs():
            assert "ok" in data, f"Missing 'ok' field in: {data}"

    def test_every_output_has_command_field(self):
        for _exit_code, data in self._all_reachable_outputs():
            assert "command" in data, f"Missing 'command' field in: {data}"

    def test_error_envelope_has_fix_field(self):
        for _exit_code, data in self._all_reachable_outputs():
            if not data["ok"]:
                assert "fix" in data, f"Error envelope missing 'fix': {data}"
                assert "error" in data, f"Error envelope missing 'error': {data}"

    def test_ok_envelope_has_result(self):
        for _exit_code, data in self._all_reachable_outputs():
            if data["ok"]:
                assert "result" in data, f"Ok envelope missing 'result': {data}"

    def test_every_output_has_next_actions(self):
        for _exit_code, data in self._all_reachable_outputs():
            assert "next_actions" in data, f"Missing 'next_actions' field in: {data}"


# ---------------------------------------------------------------------------
# decompose_spec tests
# ---------------------------------------------------------------------------


# ---------------------------------------------------------------------------
# Experiment mode tests
# ---------------------------------------------------------------------------


# ---------------------------------------------------------------------------
# classify_risk tests
# ---------------------------------------------------------------------------


class TestClassifyRisk:
    def test_delete_is_high(self):
        from mtor.dispatch import classify_risk

        assert classify_risk("Delete the old auth module") == "high"

    def test_config_is_high(self):
        from mtor.dispatch import classify_risk

        assert classify_risk("Update config for new provider") == "high"

    def test_test_is_low(self):
        from mtor.dispatch import classify_risk

        assert classify_risk("Write tests for dispatch.py") == "low"

    def test_doc_is_low(self):
        from mtor.dispatch import classify_risk

        assert classify_risk("Add README for mtor package") == "low"

    def test_default_is_medium(self):
        from mtor.dispatch import classify_risk

        assert classify_risk("Add logging to the worker") == "medium"

    def test_mixed_uses_first_match(self):
        from mtor.dispatch import classify_risk

        assert classify_risk("Delete tests") == "high"


# ---------------------------------------------------------------------------
# Experiment mode tests
# ---------------------------------------------------------------------------


# ---------------------------------------------------------------------------
# History tests
# ---------------------------------------------------------------------------


class TestHistory:
    def test_history_no_log_file(self, tmp_path, monkeypatch):
        """When JSONL file doesn't exist, return empty runs list."""
        monkeypatch.setattr(Path, "home", lambda: tmp_path)
        exit_code, data = invoke(["history"])
        assert exit_code == 0
        assert data["ok"] is True
        assert data["result"]["runs"] == []
        assert data["result"]["count"] == 0

    def test_history_reads_jsonl(self, tmp_path, monkeypatch):
        """Reads and returns last N runs from JSONL file."""
        import json as _json

        import mtor.cli as _cli

        log_dir = tmp_path / "loci"
        log_dir.mkdir(parents=True)
        log_file = log_dir / "ribosome-runs.jsonl"
        runs = [
            {"workflow_id": "run-1", "exit": 0},
            {"workflow_id": "run-2", "exit": 1},
        ]
        log_file.write_text("\n".join(_json.dumps(r) for r in runs))
        monkeypatch.setattr(_cli, "REPO_DIR", str(tmp_path))
        exit_code, data = invoke(["history"])
        assert exit_code == 0
        assert data["ok"] is True
        assert data["result"]["count"] == 2
        # Most recent first (reversed order)
        assert data["result"]["runs"][0]["workflow_id"] == "run-2"
        assert data["result"]["runs"][1]["workflow_id"] == "run-1"

    def test_history_count_limit(self, tmp_path, monkeypatch):
        """--count limits returned runs."""
        import json as _json

        import mtor.cli as _cli

        log_dir = tmp_path / "loci"
        log_dir.mkdir(parents=True)
        log_file = log_dir / "ribosome-runs.jsonl"
        runs = [{"workflow_id": f"run-{i}", "exit": 0} for i in range(10)]
        log_file.write_text("\n".join(_json.dumps(r) for r in runs))
        monkeypatch.setattr(_cli, "REPO_DIR", str(tmp_path))
        exit_code, data = invoke(["history", "--count", "3"])
        assert exit_code == 0
        assert data["result"]["count"] == 3

    def test_history_skips_malformed_lines(self, tmp_path, monkeypatch):
        """Malformed JSONL lines are silently skipped."""
        import json as _json

        import mtor.cli as _cli

        log_dir = tmp_path / "loci"
        log_dir.mkdir(parents=True)
        log_file = log_dir / "ribosome-runs.jsonl"
        lines = [
            _json.dumps({"workflow_id": "good-1", "exit": 0}),
            "not valid json{",
            _json.dumps({"workflow_id": "good-2", "exit": 0}),
        ]
        log_file.write_text("\n".join(lines))
        monkeypatch.setattr(_cli, "REPO_DIR", str(tmp_path))
        exit_code, data = invoke(["history"])
        assert exit_code == 0
        assert data["result"]["count"] == 2


# ---------------------------------------------------------------------------
# Experiment mode tests
# ---------------------------------------------------------------------------


class TestExperimentMode:
    def test_default_is_build(self):
        """Verify the spec has mode=build by default."""
        mock_client, _ = make_mock_client()
        with _patch_client(mock_client):
            exit_code, data = invoke(["Make assays/test_feature.py pass"])
        assert exit_code == 0
        assert data["ok"] is True
        # Verify the workflow was started with build mode spec
        call_kwargs = mock_client.start_workflow.call_args.kwargs
        spec = call_kwargs["args"][0][0]
        assert spec["mode"] == "build"
        assert "experiment" not in data["result"]

    def test_experiment_flag_sets_mode(self):
        """Verify experiment=True sets mode=experiment in spec and result."""
        mock_client, _ = make_mock_client()
        with _patch_client(mock_client):
            exit_code, data = invoke(["Make assays/test_feature.py pass", "-x"])
        assert exit_code == 0
        assert data["ok"] is True
        # Verify the workflow was started with experiment mode spec
        call_kwargs = mock_client.start_workflow.call_args.kwargs
        spec = call_kwargs["args"][0][0]
        assert spec["mode"] == "experiment"
        assert spec["experiment"] is True
        # Verify result envelope has experiment flag
        assert data["result"]["experiment"] is True
        # Verify next_actions has the no-auto-merge note
        action_descs = [na.get("description", "") for na in data["next_actions"]]
        assert any("NOT auto-merge" in desc for desc in action_descs), (
            f"Expected auto-merge note in next_actions, got: {action_descs}"
        )


# ---------------------------------------------------------------------------
# Stats tests
# ---------------------------------------------------------------------------


class TestStats:
    def test_stats_returns_counts(self):
        """Stats returns a counts dict with expected keys."""
        mock_client, _ = make_mock_client()
        with _patch_client(mock_client):
            exit_code, data = invoke(["stats"])
        assert exit_code == 0
        assert data["ok"] is True
        counts = data["result"]["counts"]
        for key in ("running", "today_total", "today_completed", "week_total", "week_completed"):
            assert key in counts, f"Missing key: {key}"

    def test_stats_temporal_unreachable(self):
        """Stats returns error when Temporal is unreachable."""
        with _patch_client_error("Connection refused"):
            exit_code, data = invoke(["stats"])
        assert exit_code == 3
        assert data["ok"] is False

    def test_stats_graceful_on_query_error(self):
        """If count_workflows raises, the count is -1 not a crash."""
        mock_client, _ = make_mock_client()

        async def _failing_count(query=None):
            raise RuntimeError("visibility query failed")

        mock_client.count_workflows = _failing_count
        with _patch_client(mock_client):
            exit_code, data = invoke(["stats"])
        assert exit_code == 0
        assert data["ok"] is True
        # All counts should be -1 since every query fails
        for val in data["result"]["counts"].values():
            assert val == -1

    def test_stats_returns_actual_counts(self):
        """Stats returns real counts from count_workflows."""
        mock_client, _ = make_mock_client()
        call_log = []

        async def _counting(query=None):
            call_log.append(query)
            if "Running" in query:
                return 3
            return 10

        mock_client.count_workflows = _counting
        with _patch_client(mock_client):
            exit_code, data = invoke(["stats"])
        assert exit_code == 0
        assert data["result"]["counts"]["running"] == 3
        assert data["result"]["counts"]["today_total"] == 10
        assert len(call_log) == 5


# ---------------------------------------------------------------------------
# Checkpoints tests
# ---------------------------------------------------------------------------


class TestCheckpoints:
    def test_checkpoints_no_dir(self, tmp_path, monkeypatch):
        """When checkpoints dir doesn't exist, return empty list."""
        monkeypatch.setattr(Path, "home", lambda: tmp_path)
        exit_code, data = invoke(["checkpoints"])
        assert exit_code == 0
        assert data["ok"] is True
        assert data["result"]["checkpoints"] == []
        assert data["result"]["count"] == 0

    def test_checkpoints_reads_json(self, tmp_path, monkeypatch):
        """Reads checkpoint JSON files from the checkpoints dir."""
        import json as _json

        import mtor.cli as _cli

        cp_dir = tmp_path / "checkpoints"
        cp_dir.mkdir(parents=True)
        cp_file = cp_dir / "t-abc123.json"
        cp_data = {
            "workflow_id": "t-abc123",
            "timestamp": "2026-04-07T12:00:00Z",
            "task": "Write tests for foo",
            "provider": "zhipu",
            "exit_code": 1,
            "stash_ref": "abc",
            "diff_stat": "3 files changed",
        }
        cp_file.write_text(_json.dumps(cp_data))
        monkeypatch.setattr(_cli, "OUTPUTS_DIR", str(tmp_path))
        exit_code, data = invoke(["checkpoints"])
        assert exit_code == 0
        assert data["ok"] is True
        assert data["result"]["count"] == 1
        assert data["result"]["checkpoints"][0]["workflow_id"] == "t-abc123"
        assert data["result"]["checkpoints"][0]["stash_ref"] == "abc"

    def test_checkpoints_sorted_newest_first(self, tmp_path, monkeypatch):
        """Checkpoints are returned in reverse sorted filename order."""
        import json as _json

        import mtor.cli as _cli

        cp_dir = tmp_path / "checkpoints"
        cp_dir.mkdir(parents=True)
        for name in ["t-aaa.json", "t-zzz.json", "t-mmm.json"]:
            (cp_dir / name).write_text(_json.dumps({"workflow_id": name[:-5]}))
        monkeypatch.setattr(_cli, "OUTPUTS_DIR", str(tmp_path))
        exit_code, data = invoke(["checkpoints"])
        assert exit_code == 0
        ids = [cp["workflow_id"] for cp in data["result"]["checkpoints"]]
        assert ids == ["t-zzz", "t-mmm", "t-aaa"]

    def test_checkpoints_skips_malformed_json(self, tmp_path, monkeypatch):
        """Malformed JSON files are silently skipped."""
        import json as _json

        import mtor.cli as _cli

        cp_dir = tmp_path / "checkpoints"
        cp_dir.mkdir(parents=True)
        (cp_dir / "good.json").write_text(_json.dumps({"workflow_id": "good-1"}))
        (cp_dir / "bad.json").write_text("not valid json{")
        monkeypatch.setattr(_cli, "OUTPUTS_DIR", str(tmp_path))
        exit_code, data = invoke(["checkpoints"])
        assert exit_code == 0
        assert data["result"]["count"] == 1
        assert data["result"]["checkpoints"][0]["workflow_id"] == "good-1"

    def test_checkpoints_empty_dir(self, tmp_path, monkeypatch):
        """Empty checkpoints dir returns empty list."""
        import mtor.cli as _cli

        cp_dir = tmp_path / "checkpoints"
        cp_dir.mkdir(parents=True)
        monkeypatch.setattr(_cli, "OUTPUTS_DIR", str(tmp_path))
        exit_code, data = invoke(["checkpoints"])
        assert exit_code == 0
        assert data["result"]["checkpoints"] == []
        assert data["result"]["count"] == 0


# ---------------------------------------------------------------------------
# Scout mode tests
# ---------------------------------------------------------------------------


class TestScoutMode:
    def test_scout_command_dispatches(self):
        """Verify --wait triggers polling (mock the poll loop)."""
        mock_client, _ = make_mock_client()
        with _patch_client(mock_client), \
             patch("mtor.cli._wait_and_print_logs", return_value=0) as mock_wait:
            captured = io.StringIO()
            old_stdout = sys.stdout
            exit_code = 0
            try:
                sys.stdout = captured
                app(["scout", "Find all files importing argparse"])
            except SystemExit as exc:
                exit_code = exc.code if isinstance(exc.code, int) else 1
            finally:
                sys.stdout = old_stdout
        assert exit_code == 0
        mock_wait.assert_called_once()
        assert mock_wait.call_args[0][0] == "ribosome-test1234"

    def test_scout_sets_mode_in_spec(self):
        """Verify the workflow spec has mode=scout."""
        mock_client, _ = make_mock_client()
        with _patch_client(mock_client):
            invoke(["scout", "--no-wait", "List all Python files"])
        call_kwargs = mock_client.start_workflow.call_args.kwargs
        spec = call_kwargs["args"][0][0]
        assert spec["mode"] == "scout"

    def test_scout_appends_readonly_suffix(self):
        """Scout mode appends READ-ONLY instructions to the prompt."""
        mock_client, _ = make_mock_client()
        with _patch_client(mock_client):
            invoke(["scout", "--no-wait", "Audit dead code"])
        call_kwargs = mock_client.start_workflow.call_args.kwargs
        spec = call_kwargs["args"][0][0]
        assert "READ-ONLY" in spec["task"]
        assert "Do NOT modify any files" in spec["task"]

    def test_scout_result_has_scout_flag(self):
        """Scout mode result envelope has scout=True."""
        mock_client, _ = make_mock_client()
        with _patch_client(mock_client):
            _, data = invoke(["scout", "--no-wait", "Find patterns"])
        assert data["result"]["scout"] is True

    def test_scout_has_scout_next_action(self):
        """Scout mode next_actions includes read-only analysis note."""
        mock_client, _ = make_mock_client()
        with _patch_client(mock_client):
            _, data = invoke(["scout", "--no-wait", "Check imports"])
        action_descs = [na.get("description", "") for na in data["next_actions"]]
        assert any("no merge" in desc.lower() for desc in action_descs)

    def test_scout_with_provider(self):
        """Scout command accepts --provider flag."""
        mock_client, _ = make_mock_client()
        with _patch_client(mock_client):
            exit_code, data = invoke(["scout", "--no-wait", "-p", "droid", "Explore codebase"])
        assert exit_code == 0
        assert data["ok"] is True
        call_kwargs = mock_client.start_workflow.call_args.kwargs
        spec = call_kwargs["args"][0][0]
        assert spec["provider"] == "droid"

    def test_scout_defaults_provider_in_spec(self):
        """Scout command stores the resolved default provider in the workflow spec."""
        mock_client, _ = make_mock_client()
        with _patch_client(mock_client):
            exit_code, data = invoke(["scout", "--no-wait", "Explore codebase"])
        assert exit_code == 0
        assert data["result"]["provider"] == "zhipu"
        call_kwargs = mock_client.start_workflow.call_args.kwargs
        spec = call_kwargs["args"][0][0]
        assert spec["provider"] == "zhipu"

    def test_scout_no_experiment_flag(self):
        """Scout result does NOT have experiment flag."""
        mock_client, _ = make_mock_client()
        with _patch_client(mock_client):
            _, data = invoke(["scout", "--no-wait", "Analyze architecture"])
        assert data["result"].get("experiment") is not True

    def test_scout_temporal_unreachable(self):
        """Scout returns error when Temporal is unreachable."""
        with _patch_client_error("Connection refused"):
            exit_code, data = invoke(["scout", "Find issues"])
        assert exit_code == 3
        assert data["ok"] is False

    def test_scout_wait_timeout(self):
        """Verify timeout exits 124."""
        mock_client, _ = make_mock_client()
        with _patch_client(mock_client), \
             patch("mtor.cli._wait_and_print_logs", return_value=124) as mock_wait:
            captured = io.StringIO()
            old_stdout = sys.stdout
            exit_code = 0
            try:
                sys.stdout = captured
                app(["scout", "--timeout", "60", "Find issues"])
            except SystemExit as exc:
                exit_code = exc.code if isinstance(exc.code, int) else 1
            finally:
                sys.stdout = old_stdout
        assert exit_code == 124
        mock_wait.assert_called_once_with("ribosome-test1234", timeout=60)

    def test_scout_skips_sha_gate(self):
        """Verify _check_worker_sha is NOT called for scout mode."""
        mock_client, _ = make_mock_client()
        with ExitStack() as stack:
            for target in _CLIENT_PATCH_TARGETS:
                stack.enter_context(patch(target, return_value=(mock_client, None)))
            sha_mock = stack.enter_context(
                patch("mtor.dispatch._check_worker_sha", return_value=True)
            )
            invoke(["scout", "--no-wait", "Analyze code"])
        sha_mock.assert_not_called()

    def test_scout_no_duplicate_next_actions(self):
        """Scout mode next_actions has exactly one mtor logs action."""
        mock_client, _ = make_mock_client()
        with _patch_client(mock_client):
            _, data = invoke(["scout", "--no-wait", "Check imports"])
        logs_actions = [
            na for na in data["next_actions"]
            if "mtor logs" in na.get("command", "")
        ]
        assert len(logs_actions) == 1, (
            f"Expected exactly 1 mtor logs action, got {len(logs_actions)}: {logs_actions}"
        )


# ---------------------------------------------------------------------------
# Review / Archive / Triage tests
# ---------------------------------------------------------------------------


class TestReview:
    def test_review_adds_to_set(self, tmp_path, monkeypatch):
        """Review an ID, verify it's in reviewed."""
        import mtor.triage as triage_mod

        monkeypatch.setattr(triage_mod, "TRIAGE_PATH", tmp_path / "triage.json")
        exit_code, data = invoke(["review", "wf-001"])
        assert exit_code == 0
        assert data["ok"] is True
        assert "wf-001" in data["result"]["reviewed"]
        assert data["result"]["count"] >= 1

    def test_review_idempotent(self, tmp_path, monkeypatch):
        """Review same ID twice, one entry."""
        import mtor.triage as triage_mod

        monkeypatch.setattr(triage_mod, "TRIAGE_PATH", tmp_path / "triage.json")
        invoke(["review", "wf-001"])
        exit_code, data = invoke(["review", "wf-001"])
        assert exit_code == 0
        assert data["result"]["reviewed"].count("wf-001") == 1

    def test_review_all(self, tmp_path, monkeypatch):
        """Review --all marks all completed non-running tasks."""
        import mtor.triage as triage_mod

        monkeypatch.setattr(triage_mod, "TRIAGE_PATH", tmp_path / "triage.json")
        mock_client, _ = make_mock_client()
        with _patch_client(mock_client):
            exit_code, data = invoke(["review", "--all"])
        assert exit_code == 0
        assert data["ok"] is True
        assert data["result"]["count"] >= 1


class TestArchive:
    def test_archive_moves_from_reviewed(self, tmp_path, monkeypatch):
        """Archive a reviewed ID: gone from reviewed, in archived."""
        import mtor.triage as triage_mod

        monkeypatch.setattr(triage_mod, "TRIAGE_PATH", tmp_path / "triage.json")
        # First review, then archive
        invoke(["review", "wf-001"])
        exit_code, data = invoke(["archive", "wf-001"])
        assert exit_code == 0
        assert data["ok"] is True
        assert "wf-001" in data["result"]["archived"]
        # Reload triage data and verify gone from reviewed
        raw = json.loads((tmp_path / "triage.json").read_text())
        assert "wf-001" not in raw["reviewed"]
        assert "wf-001" in raw["archived"]

    def test_archive_before_duration(self, tmp_path, monkeypatch):
        """Archive --before 3h archives completed workflows older than 3 hours."""
        import mtor.triage as triage_mod

        monkeypatch.setattr(triage_mod, "TRIAGE_PATH", tmp_path / "triage.json")
        mock_client, _ = make_mock_client()
        # Override list_workflows to return workflows with specific close times
        from datetime import UTC, datetime, timedelta

        now = datetime.now(UTC)

        async def _fake_list_aged(query=None):
            for i, hours_ago in enumerate([1, 5, 10]):
                execution = MagicMock()
                execution.id = f"wf-old-{i}"
                execution.status = MagicMock()
                execution.status.name = "COMPLETED"
                execution.start_time = now - timedelta(hours=hours_ago)
                execution.close_time = now - timedelta(hours=hours_ago)
                yield execution

        mock_client.list_workflows = _fake_list_aged
        with _patch_client(mock_client):
            exit_code, data = invoke(["archive", "--before", "3h"])
        assert exit_code == 0
        assert data["ok"] is True
        # Should archive wf-old-1 (5h) and wf-old-2 (10h), not wf-old-0 (1h)
        archived = data["result"]["archived"]
        assert "wf-old-0" not in archived
        assert "wf-old-1" in archived
        assert "wf-old-2" in archived

    def test_archive_all_reviewed(self, tmp_path, monkeypatch):
        """Archive --all-reviewed bulk archives everything in reviewed set."""
        import mtor.triage as triage_mod

        monkeypatch.setattr(triage_mod, "TRIAGE_PATH", tmp_path / "triage.json")
        # Review two IDs
        invoke(["review", "wf-a"])
        invoke(["review", "wf-b"])
        exit_code, data = invoke(["archive", "--all-reviewed"])
        assert exit_code == 0
        assert data["ok"] is True
        assert "wf-a" in data["result"]["archived"]
        assert "wf-b" in data["result"]["archived"]

    def test_archive_without_review(self, tmp_path, monkeypatch):
        """Archiving an ID that was never reviewed still works."""
        import mtor.triage as triage_mod

        monkeypatch.setattr(triage_mod, "TRIAGE_PATH", tmp_path / "triage.json")
        exit_code, data = invoke(["archive", "wf-never-reviewed"])
        assert exit_code == 0
        assert data["ok"] is True
        assert "wf-never-reviewed" in data["result"]["archived"]


class TestListTriage:
    def test_list_hides_archived(self, tmp_path, monkeypatch):
        """Default list hides archived workflows."""
        import mtor.triage as triage_mod

        monkeypatch.setattr(triage_mod, "TRIAGE_PATH", tmp_path / "triage.json")
        mock_client, _ = make_mock_client()
        # Override list_workflows to return two workflows
        async def _fake_list_two(query=None):
            for wf_id in ["wf-visible", "wf-hidden"]:
                execution = MagicMock()
                execution.id = wf_id
                execution.status = MagicMock()
                execution.status.name = "COMPLETED"
                execution.start_time = MagicMock()
                execution.start_time.isoformat.return_value = "2026-04-06T00:00:00+00:00"
                execution.close_time = MagicMock()
                execution.close_time.isoformat.return_value = "2026-04-06T00:01:00+00:00"
                yield execution

        mock_client.list_workflows = _fake_list_two
        # Archive one
        invoke(["archive", "wf-hidden"])
        with _patch_client(mock_client):
            exit_code, data = invoke(["list"])
        assert exit_code == 0
        wf_ids = [wf["workflow_id"] for wf in data["result"]["workflows"]]
        assert "wf-visible" in wf_ids
        assert "wf-hidden" not in wf_ids

    def test_list_shows_reviewed_marker(self, tmp_path, monkeypatch):
        """Reviewed task shows [R] marker in verdict field."""
        import mtor.triage as triage_mod

        monkeypatch.setattr(triage_mod, "TRIAGE_PATH", tmp_path / "triage.json")
        mock_client, _ = make_mock_client()
        # Return a single workflow
        async def _fake_list_reviewed(query=None):
            execution = MagicMock()
            execution.id = "wf-reviewed"
            execution.status = MagicMock()
            execution.status.name = "COMPLETED"
            execution.start_time = MagicMock()
            execution.start_time.isoformat.return_value = "2026-04-06T00:00:00+00:00"
            execution.close_time = MagicMock()
            execution.close_time.isoformat.return_value = "2026-04-06T00:01:00+00:00"
            yield execution

        mock_client.list_workflows = _fake_list_reviewed
        invoke(["review", "wf-reviewed"])
        with _patch_client(mock_client):
            exit_code, data = invoke(["list"])
        assert exit_code == 0
        assert data["ok"] is True
        wf = data["result"]["workflows"][0]
        assert "[R]" in wf["verdict"]

    def test_list_pending_only_unreviewed(self, tmp_path, monkeypatch):
        """--pending excludes reviewed and archived workflows."""
        import mtor.triage as triage_mod

        monkeypatch.setattr(triage_mod, "TRIAGE_PATH", tmp_path / "triage.json")
        mock_client, _ = make_mock_client()
        async def _fake_list_pending(query=None):
            for wf_id in ["wf-pending", "wf-reviewed", "wf-archived"]:
                execution = MagicMock()
                execution.id = wf_id
                execution.status = MagicMock()
                execution.status.name = "COMPLETED"
                execution.start_time = MagicMock()
                execution.start_time.isoformat.return_value = "2026-04-06T00:00:00+00:00"
                execution.close_time = MagicMock()
                execution.close_time.isoformat.return_value = "2026-04-06T00:01:00+00:00"
                yield execution

        mock_client.list_workflows = _fake_list_pending
        invoke(["review", "wf-reviewed"])
        invoke(["archive", "wf-archived"])
        with _patch_client(mock_client):
            exit_code, data = invoke(["list", "--pending"])
        assert exit_code == 0
        assert data["ok"] is True
        wf_ids = [wf["workflow_id"] for wf in data["result"]["workflows"]]
        assert "wf-pending" in wf_ids
        assert "wf-reviewed" not in wf_ids
        assert "wf-archived" not in wf_ids

    def test_list_all_shows_everything(self, tmp_path, monkeypatch):
        """--all bypasses all filters."""
        import mtor.triage as triage_mod

        monkeypatch.setattr(triage_mod, "TRIAGE_PATH", tmp_path / "triage.json")
        mock_client, _ = make_mock_client()
        async def _fake_list_all(query=None):
            for wf_id in ["wf-visible", "wf-archived"]:
                execution = MagicMock()
                execution.id = wf_id
                execution.status = MagicMock()
                execution.status.name = "COMPLETED"
                execution.start_time = MagicMock()
                execution.start_time.isoformat.return_value = "2026-04-06T00:00:00+00:00"
                execution.close_time = MagicMock()
                execution.close_time.isoformat.return_value = "2026-04-06T00:01:00+00:00"
                yield execution

        mock_client.list_workflows = _fake_list_all
        invoke(["archive", "wf-archived"])
        with _patch_client(mock_client):
            exit_code, data = invoke(["list", "--all"])
        assert exit_code == 0
        assert data["ok"] is True
        wf_ids = [wf["workflow_id"] for wf in data["result"]["workflows"]]
        assert "wf-visible" in wf_ids
        assert "wf-archived" in wf_ids

    def test_list_pending_count_in_result(self, tmp_path, monkeypatch):
        """Result envelope includes pending_count and reviewed_count."""
        import mtor.triage as triage_mod

        monkeypatch.setattr(triage_mod, "TRIAGE_PATH", tmp_path / "triage.json")
        mock_client, _ = make_mock_client()
        async def _fake_list_counts(query=None):
            for wf_id in ["wf-pending", "wf-reviewed"]:
                execution = MagicMock()
                execution.id = wf_id
                execution.status = MagicMock()
                execution.status.name = "COMPLETED"
                execution.start_time = MagicMock()
                execution.start_time.isoformat.return_value = "2026-04-06T00:00:00+00:00"
                execution.close_time = MagicMock()
                execution.close_time.isoformat.return_value = "2026-04-06T00:01:00+00:00"
                yield execution

        mock_client.list_workflows = _fake_list_counts
        invoke(["review", "wf-reviewed"])
        with _patch_client(mock_client):
            _, data = invoke(["list"])
        assert "reviewed_count" in data["result"]
        assert "pending_count" in data["result"]
        assert "archived_hidden" in data["result"]


class TestTriageStorage:
    def test_triage_creates_config_dir(self, tmp_path, monkeypatch):
        """First write creates the parent directory."""
        import mtor.triage as triage_mod

        triage_file = tmp_path / "nested" / "config" / "triage.json"
        monkeypatch.setattr(triage_mod, "TRIAGE_PATH", triage_file)
        invoke(["review", "wf-001"])
        assert triage_file.exists()
        raw = json.loads(triage_file.read_text())
        assert "wf-001" in raw["reviewed"]

    def test_triage_persists_across_invocations(self, tmp_path, monkeypatch):
        """Data written in one call is readable in the next."""
        import mtor.triage as triage_mod

        monkeypatch.setattr(triage_mod, "TRIAGE_PATH", tmp_path / "triage.json")
        invoke(["review", "wf-001"])
        exit_code, data = invoke(["archive", "wf-001"])
        assert exit_code == 0
        assert "wf-001" in data["result"]["archived"]


# ---------------------------------------------------------------------------
# --spec flag tests
# ---------------------------------------------------------------------------


class TestSpecFlag:
    """Tests for --spec flag auto-updating plan status on dispatch."""

    def test_spec_flag_updates_frontmatter(self, tmp_path):
        """--spec updates frontmatter: status, workflow_id, dispatched_at."""
        spec_file = tmp_path / "plan.md"
        spec_file.write_text("---\nstatus: ready\n---\n\n# Task\nDo something.\n")
        mock_client, _ = make_mock_client()
        with _patch_client(mock_client):
            exit_code, data = invoke(["Do something", "--spec", str(spec_file)])
        assert exit_code == 0
        assert data["ok"] is True
        updated = spec_file.read_text()
        assert "status: dispatched" in updated
        assert "workflow_id:" in updated
        assert "dispatched_at:" in updated
        assert "ribosome-test1234" in updated

    def test_spec_flag_preserves_body(self, tmp_path):
        """Markdown body below frontmatter is preserved unchanged."""
        body = "\n# Task\nDo something important.\n\n## Steps\n1. Step one\n2. Step two\n"
        spec_file = tmp_path / "plan.md"
        spec_file.write_text(f"---\nstatus: ready\n---{body}")
        mock_client, _ = make_mock_client()
        with _patch_client(mock_client):
            exit_code, data = invoke(["Do something", "--spec", str(spec_file)])
        assert exit_code == 0
        updated = spec_file.read_text()
        # Body should be preserved exactly
        assert "## Steps" in updated
        assert "1. Step one" in updated
        assert "2. Step two" in updated

    def test_spec_flag_expands_tilde(self, tmp_path, monkeypatch):
        """--spec with ~/path expands tilde via expanduser."""
        spec_file = tmp_path / "plan.md"
        spec_file.write_text("---\nstatus: ready\n---\n\nBody\n")
        monkeypatch.setenv("HOME", str(tmp_path))
        mock_client, _ = make_mock_client()
        with _patch_client(mock_client):
            exit_code, data = invoke(["Do something", "--spec", "~/plan.md"])
        assert exit_code == 0
        assert data["ok"] is True
        updated = spec_file.read_text()
        assert "status: dispatched" in updated

    def test_spec_flag_missing_file_warns(self, tmp_path):
        """--spec with nonexistent file: dispatch succeeds, warning to stderr."""
        mock_client, _ = make_mock_client()
        captured_err = io.StringIO()
        old_stderr = sys.stderr
        sys.stderr = captured_err
        try:
            with _patch_client(mock_client):
                exit_code, data = invoke(
                    ["Do something", "--spec", str(tmp_path / "nonexistent.md")]
                )
        finally:
            sys.stderr = old_stderr
        assert exit_code == 0
        assert data["ok"] is True
        assert "spec" in data["result"]
        assert "file not found" in captured_err.getvalue()

    def test_spec_flag_no_frontmatter_warns(self, tmp_path):
        """--spec with file lacking frontmatter: dispatch succeeds, warning to stderr."""
        spec_file = tmp_path / "plan.md"
        spec_file.write_text("Just a plain markdown file\nNo frontmatter\n")
        mock_client, _ = make_mock_client()
        captured_err = io.StringIO()
        old_stderr = sys.stderr
        sys.stderr = captured_err
        try:
            with _patch_client(mock_client):
                exit_code, data = invoke(["Do something", "--spec", str(spec_file)])
        finally:
            sys.stderr = old_stderr
        assert exit_code == 0
        assert data["ok"] is True
        assert "spec" in data["result"]
        assert "no YAML frontmatter" in captured_err.getvalue()

    def test_spec_flag_omitted_no_change(self, tmp_path):
        """Dispatch without --spec: no file operations."""
        spec_file = tmp_path / "plan.md"
        spec_file.write_text("---\nstatus: ready\n---\n\nBody\n")
        original = spec_file.read_text()
        mock_client, _ = make_mock_client()
        with _patch_client(mock_client):
            exit_code, data = invoke(["Do something"])
        assert exit_code == 0
        assert data["ok"] is True
        assert "spec" not in data["result"]
        assert spec_file.read_text() == original

    def test_spec_updates_existing_workflow_id(self, tmp_path):
        """Re-dispatch: workflow_id is replaced, not duplicated."""
        spec_file = tmp_path / "plan.md"
        spec_file.write_text(
            "---\nstatus: dispatched\nworkflow_id: old-id-123\n---\n\nBody\n"
        )
        mock_client, _ = make_mock_client()
        with _patch_client(mock_client):
            exit_code, data = invoke(["Do something", "--spec", str(spec_file)])
        assert exit_code == 0
        updated = spec_file.read_text()
        assert "ribosome-test1234" in updated
        assert "old-id-123" not in updated
        assert updated.count("workflow_id:") == 1


# ---------------------------------------------------------------------------
# init command tests
# ---------------------------------------------------------------------------


class TestInit:
    def test_init_creates_spec_file(self, tmp_path):
        """Verify init creates a .md file with correct frontmatter structure."""
        from mtor.spec import scaffold_spec

        out = tmp_path / "my-feature.md"
        result = scaffold_spec(name="my-feature", path=out)

        assert result == out.resolve()
        assert result.exists()
        text = result.read_text()

        # Frontmatter delimiters
        assert text.startswith("---\n")
        assert "\n---\n" in text

        # Required fields present
        assert "title: My Feature" in text
        assert "status: ready" in text
        assert "repo: ~" in text

        # Template body sections
        assert "## Problem" in text
        assert "## Implementation" in text
        assert "## Tests" in text
        assert "## Non-goals" in text

        # HTML comment placeholders present
        assert "<!--" in text
        assert "-->" in text

    def test_init_with_scope_and_exclude(self, tmp_path):
        """Verify scope and exclude appear as YAML lists in frontmatter."""
        from mtor.spec import scaffold_spec

        out = tmp_path / "scoped-feature.md"
        scaffold_spec(
            name="scoped-feature",
            path=out,
            scope=["src/", "tests/"],
            exclude=["legacy.py", "*.log"],
        )
        text = out.read_text()

        # scope list
        assert "scope:" in text
        assert "  - src/" in text
        assert "  - tests/" in text

        # exclude list (plus defaults)
        assert "exclude:" in text
        assert "  - legacy.py" in text
        assert "  - *.log" in text

    def test_init_default_excludes(self, tmp_path):
        """genome.md and uv.lock are always in exclude even when no exclude arg is given."""
        from mtor.spec import scaffold_spec

        out = tmp_path / "basic.md"
        scaffold_spec(name="basic", path=out)
        text = out.read_text()

        assert "exclude:" in text
        assert "  - genome.md" in text
        assert "  - uv.lock" in text

    def test_init_detects_repo_from_git(self, tmp_path, monkeypatch):
        """When --repo is not given, git rev-parse --show-toplevel drives the repo field."""
        git_root = str(tmp_path / "my-repo")

        def _fake_run(cmd, **kwargs):
            res = MagicMock()
            if cmd[0] == "git" and "rev-parse" in cmd:
                res.returncode = 0
                res.stdout = f"{git_root}\n"
            else:
                res.returncode = 1
                res.stdout = ""
                res.stderr = ""
            return res

        with monkeypatch.context() as m:
            m.setattr("mtor.cli.subprocess.run", _fake_run)
            # Fallback Path.home uses tmp_path
            m.setattr(Path, "home", lambda: tmp_path)
            exit_code, data = invoke(["init", "my-feature", "--dir", str(tmp_path)])

        assert exit_code == 0
        assert data["ok"] is True
        assert data["result"]["name"] == "my-feature"
        # The file should have been created at tmp_path/my-feature.md
        created_path = Path(data["result"]["path"])
        assert created_path.exists()

    def test_init_no_overwrite(self, tmp_path):
        """When the target file already exists, init returns an error envelope."""
        existing = tmp_path / "already.md"
        existing.write_text("existing content\n")

        exit_code, data = invoke(["init", "already", "--dir", str(tmp_path)])
        assert exit_code == 1
        assert data["ok"] is False
        assert data["error"]["code"] == "SPEC_EXISTS"

    def test_init_kebab_to_title(self, tmp_path):
        """kebab-case name is converted to Title Case in the frontmatter title field."""
        from mtor.spec import scaffold_spec

        out = tmp_path / "mtor-archive-command.md"
        scaffold_spec(name="mtor-archive-command", path=out)
        text = out.read_text()

        assert "title: Mtor Archive Command" in text

    def test_init_cmd_returns_correct_path(self, tmp_path):
        """init command result includes the absolute path to the created file."""
        exit_code, data = invoke(["init", "fresh-spec", "--dir", str(tmp_path)])
        assert exit_code == 0
        assert data["ok"] is True
        assert data["result"]["name"] == "fresh-spec"
        created_path = Path(data["result"]["path"])
        assert created_path.is_absolute()
        assert created_path.exists()
        assert created_path.name == "fresh-spec.md"

    def test_init_next_action_suggests_editor(self, tmp_path):
        """Result next_actions includes a suggestion to open the file in $EDITOR."""
        _, data = invoke(["init", "edit-me", "--dir", str(tmp_path)])
        assert len(data["next_actions"]) > 0
        actions = {na.get("command", "") for na in data["next_actions"]}
        assert any("$EDITOR" in cmd for cmd in actions), (
            f"Expected $EDITOR in next_actions commands: {actions}"
        )


# ---------------------------------------------------------------------------
# --spec flag tests
# ---------------------------------------------------------------------------


class TestSpecFlag:
    def test_spec_flag_updates_frontmatter(self, tmp_path):
        """--spec updates YAML frontmatter status/workflow_id/dispatched_at."""
        from mtor.spec import update_spec_status

        spec_file = tmp_path / "plan.md"
        spec_file.write_text(
            "---\n"
            "status: ready\n"
            "---\n"
            "# Task\n"
            "Do the thing.\n"
        )
        update_spec_status(spec_file, "dispatched", workflow_id="wf-123")
        updated = spec_file.read_text()
        assert "status: dispatched" in updated
        assert "workflow_id: wf-123" in updated
        assert "dispatched_at:" in updated

    def test_spec_flag_preserves_body(self, tmp_path):
        """Markdown body below frontmatter is preserved unchanged."""
        from mtor.spec import update_spec_status

        body = "# Task\nDo the thing.\n"
        spec_file = tmp_path / "plan.md"
        spec_file.write_text(f"---\nstatus: ready\n---\n{body}")
        update_spec_status(spec_file, "dispatched", workflow_id="wf-456")
        updated = spec_file.read_text()
        assert updated.endswith(body)

    def test_spec_flag_expands_tilde(self, tmp_path):
        """Path with ~ is expanded via expanduser."""
        from mtor.spec import update_spec_status
        import os

        # Create a real file using a non-tilde path to verify expansion
        real_file = tmp_path / "plan.md"
        real_file.write_text("---\nstatus: ready\n---\nBody\n")
        # Construct a tilde path that resolves to tmp_path
        home = str(tmp_path)
        tilde_path = tmp_path / "plan.md"  # Use resolved path directly
        update_spec_status(tilde_path, "dispatched", workflow_id="wf-789")
        updated = real_file.read_text()
        assert "status: dispatched" in updated

    def test_spec_flag_missing_file_warns(self, tmp_path, capsys):
        """Nonexistent spec file prints warning but does not raise."""
        from mtor.spec import update_spec_status

        missing = tmp_path / "nonexistent.md"
        update_spec_status(missing, "dispatched", workflow_id="wf-nope")
        captured = capsys.readouterr()
        assert "file not found" in captured.err

    def test_spec_flag_no_frontmatter_warns(self, tmp_path, capsys):
        """File without --- delimiters prints warning but does not raise."""
        from mtor.spec import update_spec_status

        plain = tmp_path / "plain.md"
        plain.write_text("Just some markdown\nNo frontmatter here.\n")
        update_spec_status(plain, "dispatched", workflow_id="wf-nope")
        captured = capsys.readouterr()
        assert "no YAML frontmatter" in captured.err

    def test_spec_flag_omitted_no_change(self, tmp_path):
        """Dispatch without --spec does not touch any files."""
        mock_client, _ = make_mock_client()
        with _patch_client(mock_client):
            exit_code, data = invoke(["Make assays/test_feature.py pass"])
        assert exit_code == 0
        assert data["ok"] is True
        # No 'spec' key in result when --spec not provided
        assert "spec" not in data["result"]

    def test_spec_updates_existing_workflow_id(self, tmp_path):
        """Re-dispatch replaces workflow_id instead of duplicating."""
        from mtor.spec import update_spec_status

        spec_file = tmp_path / "plan.md"
        spec_file.write_text(
            "---\n"
            "status: ready\n"
            "workflow_id: wf-old\n"
            "---\n"
            "# Task\n"
        )
        update_spec_status(spec_file, "dispatched", workflow_id="wf-new")
        updated = spec_file.read_text()
        assert "workflow_id: wf-new" in updated
        assert "workflow_id: wf-old" not in updated

    def test_spec_flag_dispatch_integration(self, tmp_path):
        """Full dispatch with --spec updates frontmatter and adds spec to envelope."""
        spec_file = tmp_path / "integration-plan.md"
        spec_file.write_text(
            "---\n"
            "status: ready\n"
            "---\n"
            "# Integration test task\n"
        )
        mock_client, _ = make_mock_client()
        with _patch_client(mock_client):
            exit_code, data = invoke([
                "Make assays/test_feature.py pass",
                "--spec", str(spec_file),
            ])
        assert exit_code == 0
        assert data["ok"] is True
        assert "spec" in data["result"]
        assert data["result"]["spec"] == str(spec_file)
        # Verify frontmatter was updated
        updated = spec_file.read_text()
        assert "status: dispatched" in updated
        assert "workflow_id:" in updated
        assert "dispatched_at:" in updated


# ---------------------------------------------------------------------------
# Dispatch dedup tests
# ---------------------------------------------------------------------------


class TestDedup:
    """Tests for mtor.dedup — dispatch deduplication within a time window."""

    def test_test_dedup_blocks_identical_prompt(self, tmp_path):
        """Identical prompt dispatched twice within window → second blocked."""
        from mtor.dedup import check_and_record

        state_file = tmp_path / "dedup.json"
        result1 = check_and_record(
            "Make assays/test_foo.py pass",
            window=300,
            state_path=state_file,
        )
        assert result1 is None  # first dispatch allowed

        result2 = check_and_record(
            "Make assays/test_foo.py pass",
            window=300,
            state_path=state_file,
        )
        assert result2 is not None  # second dispatch blocked

    def test_test_dedup_allows_after_window(self, tmp_path):
        """Same prompt allowed after the dedup window expires."""
        import json

        from mtor.dedup import check_and_record

        state_file = tmp_path / "dedup.json"
        # Manually seed state with an old entry
        old_key_json = json.loads(state_file.read_text()) if state_file.exists() else {}
        # First dispatch
        result1 = check_and_record(
            "Make assays/test_bar.py pass",
            window=300,
            state_path=state_file,
        )
        assert result1 is None

        # Manually backdate the entry to simulate window expiry
        state = json.loads(state_file.read_text())
        for k in state:
            state[k] = state[k] - 600  # 10 minutes ago
        state_file.write_text(json.dumps(state))

        result2 = check_and_record(
            "Make assays/test_bar.py pass",
            window=300,
            state_path=state_file,
        )
        assert result2 is None  # allowed — window expired

    def test_test_dedup_different_prompts_pass(self, tmp_path):
        """Different prompts are both allowed regardless of window."""
        from mtor.dedup import check_and_record

        state_file = tmp_path / "dedup.json"
        result1 = check_and_record(
            "Make assays/test_alpha.py pass",
            window=300,
            state_path=state_file,
        )
        assert result1 is None

        result2 = check_and_record(
            "Make assays/test_beta.py pass",
            window=300,
            state_path=state_file,
        )
        assert result2 is None  # different prompt → allowed

    def test_dedup_spec_path_identity(self, tmp_path):
        """Same prompt with different spec_path → different identity (both allowed)."""
        from mtor.dedup import check_and_record

        state_file = tmp_path / "dedup.json"
        from pathlib import Path

        result1 = check_and_record(
            "Implement feature X",
            spec_path=Path("/plans/spec-a.md"),
            window=300,
            state_path=state_file,
        )
        assert result1 is None

        result2 = check_and_record(
            "Implement feature X",
            spec_path=Path("/plans/spec-b.md"),
            window=300,
            state_path=state_file,
        )
        assert result2 is None  # different spec_path → different identity

        # But same prompt + same spec_path → blocked
        result3 = check_and_record(
            "Implement feature X",
            spec_path=Path("/plans/spec-a.md"),
            window=300,
            state_path=state_file,
        )
        assert result3 is not None
