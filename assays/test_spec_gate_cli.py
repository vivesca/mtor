"""Tests for spec-required gate on default_handler.

Validates:
  1. Prompt dispatch without --spec remains supported
  2. Build mode with --spec dispatches normally
  3. Spec without tests field is always rejected (no bypass)
  4. No prompt (bare invocation) still shows help/tree

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
    """Invoke CLI and return (exit_code, parsed_json).

    Fixtures here use pytest tmp_path repos, which the prompt path-locality
    preflight correctly classifies as host-local scratch; the override flag
    keeps these gate tests exercising their own concern.
    """
    captured = io.StringIO()
    old_stdout = sys.stdout
    exit_code = 0
    try:
        sys.stdout = captured
        app([*(args or []), "--allow-local-paths"])
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
    stack.enter_context(patch("mtor.cli._check_dedup_only", return_value=None))
    stack.enter_context(patch("mtor.cli._record_dispatch"))
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


class TestPromptDispatchWithoutSpec:
    """Build mode (default) still supports direct prompt dispatch."""

    def test_prompt_without_spec_dispatches(self):
        """mtor 'prompt' without --spec dispatches for quick direct tasks."""
        client, _handle = _make_mock_client()
        with _patch_dispatch(client):
            exit_code, data = invoke(["Fix the bug"])
        assert exit_code == 0
        assert data["ok"] is True
        assert data["result"]["workflow_id"] == "ribosome-gate-test1234"

    def test_prompt_without_spec_no_double_dash(self):
        """Positional prompt alone is treated as a direct dispatch prompt."""
        client, _handle = _make_mock_client()
        with _patch_dispatch(client):
            exit_code, data = invoke(["Refactor the module"])
        assert exit_code == 0
        assert data["ok"] is True

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
        spec_file = _write_spec(
            tmp_path,
            (
                "status: ready\n"
                f"repo: {tmp_path}\n"
                "tests:\n"
                f'  run: "cd {tmp_path} && uv run pytest"\n'
            ),
        )

        client, handle = _make_mock_client()
        with _patch_dispatch(client):
            exit_code, data = invoke(["--spec", str(spec_file), "Implement foo"])

        assert exit_code == 0, data
        assert data["ok"] is True
        assert data["result"]["workflow_id"] == "ribosome-gate-test1234"

    def test_spec_only_dispatches(self, tmp_path: Path):
        """mtor --spec spec.md (no positional prompt) dispatches with spec as prompt."""
        spec_file = _write_spec(
            tmp_path,
            (
                "status: ready\n"
                f"repo: {tmp_path}\n"
                "tests:\n"
                f'  run: "cd {tmp_path} && uv run pytest"\n'
            ),
            body="# Spec\nImplement the bar module",
        )

        client, handle = _make_mock_client()
        with _patch_dispatch(client):
            exit_code, data = invoke(["--spec", str(spec_file)])

        assert exit_code == 0, data
        assert data["ok"] is True


class TestSpecValidationAlwaysEnforced:
    """Spec validation is mandatory — no bypass flag exists."""

    def test_invalid_spec_missing_tests_rejected(self, tmp_path: Path):
        """Spec without tests field is rejected."""
        spec_file = _write_spec(
            tmp_path, (f"status: ready\nrepo: {tmp_path}\nfiles:\n  - mtor/foo.py\n")
        )

        client, handle = _make_mock_client()
        with ExitStack() as stack:
            for target in _CLIENT_PATCH_TARGETS:
                stack.enter_context(patch(target, return_value=(client, None)))
            stack.enter_context(patch("mtor.cli._check_dedup", return_value=None))
            stack.enter_context(patch("mtor.cli._check_dedup_only", return_value=None))
            stack.enter_context(patch("mtor.cli._record_dispatch"))
            stack.enter_context(patch("mtor.cli._is_frozen", return_value=False))
            stack.enter_context(patch("mtor.cli._is_paused", return_value=False))

            exit_code, data = invoke(["--spec", str(spec_file), "Implement foo"])

        assert exit_code == 1, data
        assert data["ok"] is False
        assert data["error"]["code"] == "SPEC_INVALID"


class TestPromptQualityErrorShape:
    """Verify prompt quality errors keep the standard envelope shape."""

    def test_error_envelope_structure(self):
        """PROMPT_TOO_SHORT error has ok, error.code, fix, next_actions."""
        exit_code, data = invoke(["fix"])
        assert exit_code == 2
        assert data["ok"] is False
        assert "error" in data
        err = data["error"]
        assert err["code"] == "PROMPT_TOO_SHORT"
        assert "message" in err
        assert "fix" in data
        assert "prompt" in data["fix"]
        assert "next_actions" in data
        actions_text = " ".join(
            f"{a.get('command', '')} {a.get('description', '')}"
            for a in data["next_actions"]
        )
        assert "--help" in actions_text, (
            f"Expected help in next_actions, got: {data['next_actions']}"
        )
