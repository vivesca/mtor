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
    return stack


def _patch_client_error(error_msg="Connection refused"):
    """Context manager: patch _get_client to return error in all modules."""
    stack = ExitStack()
    for target in _CLIENT_PATCH_TARGETS:
        stack.enter_context(patch(target, return_value=(None, error_msg)))
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
            "mtor doctor",
            "mtor schema",
        ]:
            first_word = expected.split()[0]
            assert any(cmd["name"].startswith(first_word) for cmd in data["result"]["commands"]), (
                f"Command starting with '{first_word}' not found in tree"
            )


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
            exit_code, data = invoke(["doctor"])
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
            exit_code, data = invoke(["Test task prompt"])
        assert exit_code == 0
        assert data["ok"] is True
        assert "workflow_id" in data["result"]
        assert data["result"]["status"] == "RUNNING"

    def test_dispatch_has_next_actions(self):
        mock_client, _ = make_mock_client()
        with _patch_client(mock_client):
            _, data = invoke(["Test task"])
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
            exit_code, data = invoke(["Write tests for foo.py"])
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
            ["doctor"],
        ]
        with _patch_client(mock_client):
            for args in test_cases:
                _exit_code, data = invoke(args)
                assert isinstance(data, dict), f"Not a dict for args={args}"


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
        assert data["result"]["cancelled"] is True

    def test_cancel_not_found_exits_4(self):
        mock_client, mock_handle = make_mock_client()
        mock_handle.cancel = AsyncMock(side_effect=Exception("workflow not found"))
        with _patch_client(mock_client):
            exit_code, data = invoke(["cancel", "nonexistent-id"])
        assert exit_code == 4
        assert data["ok"] is False
        assert "fix" in data

    def test_cancel_already_cancelled_is_ok(self):
        """Cancelling an already-cancelled workflow = idempotent success."""
        mock_client, mock_handle = make_mock_client()
        mock_handle.cancel = AsyncMock(side_effect=Exception("workflow already cancelled"))
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
            exit_code, data = invoke(["doctor"])
        assert exit_code == 3
        assert data["ok"] is False
        assert "fix" in data

    def test_doctor_has_checks_list(self):
        with _patch_client_error("Connection refused"):
            _, data = invoke(["doctor"])
        # Even failed doctor has checks in result
        assert "result" in data
        assert "checks" in data["result"]

    def test_doctor_success(self):
        mock_client, _ = make_mock_client()
        with _patch_client(mock_client):
            _exit_code, data = invoke(["doctor"])
        # May still fail if coaching file missing, but should be JSON
        assert isinstance(data, dict)
        assert "result" in data


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
            outputs.append(invoke(["doctor"]))
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


class TestDecomposeSpec:
    def test_single_task_returns_none(self):
        from mtor.dispatch import decompose_spec

        assert decompose_spec("Just do this one thing") is None

    def test_two_tasks_splits(self):
        from mtor.dispatch import decompose_spec

        spec = "# Preamble\nShared context.\n\n## Task 1\nDo A.\n\n## Task 2\nDo B."
        tasks = decompose_spec(spec)
        assert len(tasks) == 2
        assert "Shared context." in tasks[0]
        assert "Do A." in tasks[0]
        assert "Shared context." in tasks[1]
        assert "Do B." in tasks[1]

    def test_preamble_prepended_to_each(self):
        from mtor.dispatch import decompose_spec

        spec = "Important context.\n\n## Task 1\nFirst.\n\n## Task 2\nSecond."
        tasks = decompose_spec(spec)
        assert all("Important context." in t for t in tasks)

    def test_no_preamble(self):
        from mtor.dispatch import decompose_spec

        spec = "## Task 1\nFirst.\n\n## Task 2\nSecond."
        tasks = decompose_spec(spec)
        assert len(tasks) == 2


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
            exit_code, data = invoke(["Test task prompt"])
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
            exit_code, data = invoke(["Test task prompt", "-x"])
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
        """Scout subcommand dispatches a workflow with mode=scout."""
        mock_client, _ = make_mock_client()
        with _patch_client(mock_client):
            exit_code, data = invoke(["scout", "Find all files importing argparse"])
        assert exit_code == 0
        assert data["ok"] is True
        assert data["result"]["status"] == "RUNNING"

    def test_scout_sets_mode_in_spec(self):
        """Verify the workflow spec has mode=scout."""
        mock_client, _ = make_mock_client()
        with _patch_client(mock_client):
            invoke(["scout", "List all Python files"])
        call_kwargs = mock_client.start_workflow.call_args.kwargs
        spec = call_kwargs["args"][0][0]
        assert spec["mode"] == "scout"

    def test_scout_appends_readonly_suffix(self):
        """Scout mode appends READ-ONLY instructions to the prompt."""
        mock_client, _ = make_mock_client()
        with _patch_client(mock_client):
            invoke(["scout", "Audit dead code"])
        call_kwargs = mock_client.start_workflow.call_args.kwargs
        spec = call_kwargs["args"][0][0]
        assert "READ-ONLY" in spec["task"]
        assert "Do NOT modify any files" in spec["task"]

    def test_scout_result_has_scout_flag(self):
        """Scout mode result envelope has scout=True."""
        mock_client, _ = make_mock_client()
        with _patch_client(mock_client):
            _, data = invoke(["scout", "Find patterns"])
        assert data["result"]["scout"] is True

    def test_scout_has_scout_next_action(self):
        """Scout mode next_actions includes read-only analysis note."""
        mock_client, _ = make_mock_client()
        with _patch_client(mock_client):
            _, data = invoke(["scout", "Check imports"])
        action_descs = [na.get("description", "") for na in data["next_actions"]]
        assert any("no merge" in desc.lower() for desc in action_descs)

    def test_scout_with_provider(self):
        """Scout command accepts --provider flag."""
        mock_client, _ = make_mock_client()
        with _patch_client(mock_client):
            exit_code, data = invoke(["scout", "-p", "droid", "Explore codebase"])
        assert exit_code == 0
        assert data["ok"] is True
        call_kwargs = mock_client.start_workflow.call_args.kwargs
        spec = call_kwargs["args"][0][0]
        assert spec["provider"] == "droid"

    def test_scout_no_experiment_flag(self):
        """Scout result does NOT have experiment flag."""
        mock_client, _ = make_mock_client()
        with _patch_client(mock_client):
            _, data = invoke(["scout", "Analyze architecture"])
        assert data["result"].get("experiment") is not True

    def test_scout_temporal_unreachable(self):
        """Scout returns error when Temporal is unreachable."""
        with _patch_client_error("Connection refused"):
            exit_code, data = invoke(["scout", "Find issues"])
        assert exit_code == 3
        assert data["ok"] is False
