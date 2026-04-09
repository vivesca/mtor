"""Tests for spec-required gate on default_handler.

Validates:
  1. Build mode without --spec exits SPEC_REQUIRED (code 2)
  2. Build mode with --spec dispatches normally
  3. --no-tests skips spec validation
  4. --no-tests without --spec still exits SPEC_REQUIRED
  5. No prompt (bare invocation) still shows help/tree

Runs via: cd ~/code/mtor && uv run pytest assays/test_spec_gate_cli.py -v
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
# Helpers (adapted from test_spec_flag.py)
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


def _make_mock_client():
    """Build a minimal async mock Temporal client."""
    client = MagicMock()

    handle = MagicMock()
    handle.id = "ribosome-gate-test1234"
    start_coro = AsyncMock(return_value=handle)
    client.start_workflow = start_coro

    return client, handle


_CLIENT_PATCH_TARGETS = [
    "mtor.cli._get_client",
    "mtor.dispatch._get_client",
]


def _patch_dispatch(mock_client):
    """Patch _get_client + guards so dispatch proceeds without real Temporal."""
    stack = ExitStack()
    for target in _CLIENT_PATCH_TARGETS:
        stack.enter_context(patch(target, return_value=(mock_client, None)))
    stack.enter_context(patch("mtor.cli._check_dedup", return_value=None))
    stack.enter_context(patch("mtor.dispatch._check_worker_sha", return_value=True))
    stack.enter_context(patch("mtor.cli._is_frozen", return_value=False))
    stack.enter_context(patch("mtor.cli._is_paused", return_value=False))
    stack.enter_context(patch("mtor.dispatch.validate_spec", return_value=[]))
    return stack


def _write_spec(tmp_path: Path, frontmatter: str, body: str = "# Spec\n") -> Path:
    """Write a spec file with given frontmatter."""
    spec = tmp_path / "test-spec.md"
    spec.write_text(f"---\n{frontmatter}\n---\n{body}")
    return spec


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------


class TestBuildRequiresSpec:
    """Build mode (default) without --spec must exit SPEC_REQUIRED."""

    def test_prompt_without_spec_rejected(self):
        """mtor 'prompt' without --spec exits SPEC_REQUIRED."""
        exit_code, data = invoke(["Fix the bug"])
        assert exit_code == 2
        assert data["ok"] is False
        assert data["error"]["code"] == "SPEC_REQUIRED"
        assert "--spec" in data["error"]["message"]

    def test_prompt_without_spec_no_double_dash(self):
        """Positional prompt alone triggers the gate."""
        exit_code, data = invoke(["Refactor the module"])
        assert exit_code == 2
        assert data["error"]["code"] == "SPEC_REQUIRED"

    def test_bare_invocation_not_rejected(self):
        """mtor with no args (bare) does NOT trigger SPEC_REQUIRED — shows help/tree."""
        # Bare invocation prints to stdout and returns 0 (help or tree)
        captured = io.StringIO()
        old_stdout = sys.stdout

        try:
            sys.stdout = captured
            app([])
        except SystemExit:
            pass  # exit code not used in bare invocation test
        finally:
            sys.stdout = old_stdout
        # Should NOT be SPEC_REQUIRED — either help_print or JSON tree
        output = captured.getvalue()
        if output.strip():
            try:
                data = json.loads(output)
                # tree output is ok:true
                assert data.get("ok") is True
            except json.JSONDecodeError:
                # help_print output — fine
                pass


class TestBuildWithSpecAccepted:
    """Build mode with --spec dispatches normally."""

    def test_spec_prompt_dispatches(self, tmp_path: Path):
        """mtor --spec spec.md 'prompt' passes the gate and dispatches."""
        spec_file = _write_spec(tmp_path, (
            "status: ready\n"
            f"repo: {tmp_path}\n"
            "tests:\n"
            f"  run: \"cd {tmp_path} && uv run pytest\"\n"
        ))

        client, handle = _make_mock_client()
        with _patch_dispatch(client):
            exit_code, data = invoke(["--spec", str(spec_file), "Implement foo"])

        assert exit_code == 0, data
        assert data["ok"] is True
        assert data["result"]["workflow_id"] == "ribosome-gate-test1234"

    def test_spec_only_dispatches(self, tmp_path: Path):
        """mtor --spec spec.md (no positional prompt) dispatches with spec as prompt."""
        spec_file = _write_spec(tmp_path, (
            "status: ready\n"
            f"repo: {tmp_path}\n"
            "tests:\n"
            f"  run: \"cd {tmp_path} && uv run pytest\"\n"
        ), body="# Spec\nImplement the bar module")

        client, handle = _make_mock_client()
        with _patch_dispatch(client):
            exit_code, data = invoke(["--spec", str(spec_file)])

        assert exit_code == 0, data
        assert data["ok"] is True


class TestNoTestsFlag:
    """--no-tests skips spec validation but still requires --spec."""

    def test_no_tests_without_spec_rejected(self):
        """--no-tests alone does NOT bypass the --spec requirement."""
        exit_code, data = invoke(["--no-tests", "Fix the bug"])
        assert exit_code == 2
        assert data["error"]["code"] == "SPEC_REQUIRED"

    def test_no_tests_skips_validation(self, tmp_path: Path):
        """--no-tests + --spec skips validate_spec even if spec is invalid."""
        # Write a spec with NO tests: field (would normally fail validation)
        spec_file = _write_spec(tmp_path, (
            "status: ready\n"
            f"repo: {tmp_path}\n"
            "files:\n"
            "  - mtor/foo.py\n"
        ))

        client, handle = _make_mock_client()
        with _patch_dispatch(client):
            exit_code, data = invoke(
                ["--spec", str(spec_file), "--no-tests", "Implement foo"]
            )

        assert exit_code == 0, data
        assert data["ok"] is True

    def test_no_tests_spec_file_path_does_not_exist(self, tmp_path: Path):
        """--no-tests with nonexistent spec file still errors (file read failure)."""
        nonexistent = tmp_path / "no-such-file.md"
        # Should fail when trying to read the spec file, not at validation
        try:
            exit_code, data = invoke(
                ["--spec", str(nonexistent), "--no-tests", "Fix stuff"]
            )
        except (FileNotFoundError, OSError):
            pass  # Expected — file doesn't exist
        else:
            # If it returns, it should NOT be SPEC_REQUIRED
            if exit_code != 0 and isinstance(data, dict):
                assert data.get("error", {}).get("code") != "SPEC_REQUIRED"

    def test_without_no_tests_invalid_spec_rejected(self, tmp_path: Path):
        """Without --no-tests, invalid spec still fails validation."""
        spec_file = _write_spec(tmp_path, (
            "status: ready\n"
            f"repo: {tmp_path}\n"
            "files:\n"
            "  - mtor/foo.py\n"
        ))

        client, handle = _make_mock_client()
        with ExitStack() as stack:
            for target in _CLIENT_PATCH_TARGETS:
                stack.enter_context(patch(target, return_value=(client, None)))
            stack.enter_context(patch("mtor.cli._check_dedup", return_value=None))
            stack.enter_context(patch("mtor.cli._is_frozen", return_value=False))
            stack.enter_context(patch("mtor.cli._is_paused", return_value=False))

            exit_code, data = invoke(
                ["--spec", str(spec_file), "Implement foo"]
            )

        assert exit_code == 1, data
        assert data["ok"] is False
        assert data["error"]["code"] == "SPEC_INVALID"


class TestSpecRequiredErrorShape:
    """Verify the SPEC_REQUIRED error envelope has correct shape."""

    def test_error_envelope_structure(self):
        """SPEC_REQUIRED error has ok, error.code, error.message, error.fix, next_actions."""
        exit_code, data = invoke(["Build the feature"])
        assert exit_code == 2
        assert data["ok"] is False
        assert "error" in data
        err = data["error"]
        assert err["code"] == "SPEC_REQUIRED"
        assert "message" in err
        assert "--spec" in err["message"]
        assert "fix" in err
        assert "next_actions" in data
        # Should suggest mtor init and mtor plan
        actions_text = " ".join(a.get("label", "") for a in data["next_actions"])
        assert any(kw in actions_text for kw in ["init", "plan"]), (
            f"Expected init/plan in next_actions, got: {data['next_actions']}"
        )
