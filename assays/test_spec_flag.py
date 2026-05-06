"""Tests for --spec flag on the default handler (mtor <prompt>).

Verifies:
  1. --spec reads a temp file and passes its contents as the task prompt.
  2. --spec + positional task concatenates spec contents before the task.
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
# Helpers (adapted from test_mtor.py)
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
    handle.id = "ribosome-spec-test1234"
    start_coro = AsyncMock(return_value=handle)
    client.start_workflow = start_coro

    return client, handle


_CLIENT_PATCH_TARGETS = [
    "mtor.cli._get_client",
    "mtor.dispatch._get_client",
]


def _patch_client(mock_client):
    """Patch _get_client + guards so dispatch proceeds without real Temporal."""
    stack = ExitStack()
    for target in _CLIENT_PATCH_TARGETS:
        stack.enter_context(patch(target, return_value=(mock_client, None)))
    stack.enter_context(patch("mtor.cli._check_dedup", return_value=None))
    stack.enter_context(patch("mtor.dispatch._check_worker_sha", return_value=True))
    stack.enter_context(patch("mtor.cli._is_frozen", return_value=False))
    stack.enter_context(patch("mtor.cli._is_paused", return_value=False))
    return stack


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------


class TestFrontmatterStripping:
    """--spec must strip YAML frontmatter before handing prompt to harness.

    Incident 2026-04-11: a spec with standard YAML frontmatter
    (`---\\ntitle: ...\\n---`) was passed through to the claude CLI harness
    by the ribosome bash effector. claude interpreted `---` as a flag,
    exited with `Unknown flag: ---`, and the ribosome retry loop
    misclassified the empty output as rate-limited and retried 59,356
    times, generating a 208 MB log before the worker was restarted.

    Frontmatter stripping already exists at dispatch.py:201, but only
    when the positional prompt arg is a file path. On the `--spec <path>`
    code path the spec is read in cli.py first and never passes through
    that check. Strip at cli.py:209 so the invariant is: anything that
    reaches downstream from here has NO leading `---` line.
    """

    def test_spec_with_frontmatter_strips_before_dispatch(self, tmp_path: Path):
        spec_file = tmp_path / "with-fm.md"
        spec_file.write_text(
            "---\n"
            "title: test task\n"
            "status: ready\n"
            "repo: /tmp/fake\n"
            "tests:\n"
            '  run: "pytest"\n'
            "---\n"
            "\n"
            "# Task\n"
            "\n"
            "Do the real work here.\n"
        )

        client, handle = _make_mock_client()
        with _patch_client(client):
            exit_code, data = invoke(["--spec", str(spec_file)])

        assert exit_code == 0, data
        # The dispatched task must NOT start with `---`. Any leading `---`
        # would be reinterpreted as a CLI flag by the downstream claude
        # harness and trigger the retry-loop runaway.
        dispatched_task = client.start_workflow.call_args.kwargs["args"][0][0]["task"]
        assert not dispatched_task.lstrip().startswith("---"), (
            f"frontmatter leaked into dispatched task: {dispatched_task[:100]!r}"
        )
        # And the body should still be present.
        assert "Do the real work here" in dispatched_task
        # Frontmatter field names themselves should not appear as leading tokens
        # in the task body — if they do, the strip regex was too narrow.
        assert not dispatched_task.lstrip().startswith("title:")
        assert not dispatched_task.lstrip().startswith("status:")

    def test_strip_preserves_body_after_frontmatter(self, tmp_path: Path):
        """The regex must not eat past the closing `---` into the body."""
        spec_file = tmp_path / "boundary.md"
        spec_file.write_text(
            "---\n"
            "status: ready\n"
            "repo: /tmp/fake\n"
            "tests:\n"
            '  run: "pytest"\n'
            "---\n"
            "\n"
            "First paragraph of the task body.\n"
            "\n"
            "Second paragraph with --- a triple-dash mid-sentence that the "
            "regex must not treat as a new frontmatter block.\n"
        )

        client, handle = _make_mock_client()
        with _patch_client(client):
            exit_code, data = invoke(["--spec", str(spec_file)])

        assert exit_code == 0, data
        dispatched_task = client.start_workflow.call_args.kwargs["args"][0][0]["task"]
        assert "First paragraph of the task body." in dispatched_task
        assert "Second paragraph" in dispatched_task
        assert "triple-dash mid-sentence" in dispatched_task
        assert not dispatched_task.lstrip().startswith("---")


class TestSpecOnly:
    """--spec with no positional task: file contents become the prompt."""

    def test_spec_reads_file_as_prompt(self, tmp_path: Path):
        spec_file = tmp_path / "task.md"
        spec_file.write_text(
            "---\nstatus: ready\nrepo: .\ntests:\n  run: pytest\n---\n\n"
            "Implement the foo module"
        )

        client, handle = _make_mock_client()
        with _patch_client(client):
            exit_code, data = invoke(["--spec", str(spec_file)])

        assert exit_code == 0, data
        assert data["ok"] is True

        # Verify the dispatched task includes the spec contents
        call_args = client.start_workflow.call_args
        dispatched_task = call_args.kwargs["args"][0][0]["task"]
        assert "Implement the foo module" in dispatched_task

    def test_spec_only_no_positional(self, tmp_path: Path):
        """When only --spec is given (no positional arg), dispatch proceeds."""
        spec_file = tmp_path / "spec.md"
        spec_file.write_text(
            "---\nstatus: ready\nrepo: .\ntests:\n  run: pytest\n---\n\n"
            "Add logging to bar.py"
        )

        client, handle = _make_mock_client()
        with _patch_client(client):
            exit_code, data = invoke(["--spec", str(spec_file)])

        assert exit_code == 0
        assert data["ok"] is True
        assert data["result"]["workflow_id"] == "ribosome-spec-test1234"


class TestSpecPlusTask:
    """--spec + positional task: spec contents prepended with newline."""

    def test_spec_and_task_concatenated(self, tmp_path: Path):
        spec_file = tmp_path / "task.md"
        spec_file.write_text(
            "---\nstatus: ready\nrepo: .\ntests:\n  run: pytest\n---\n\n"
            "Context: the project uses pytest"
        )

        client, handle = _make_mock_client()
        with _patch_client(client):
            exit_code, data = invoke(
                ["--spec", str(spec_file), "Fix the failing test"]
            )

        assert exit_code == 0, data
        assert data["ok"] is True

        call_args = client.start_workflow.call_args
        dispatched_task = call_args.kwargs["args"][0][0]["task"]
        # Spec content comes first, then positional task
        assert dispatched_task.startswith("Context: the project uses pytest")
        assert "Fix the failing test" in dispatched_task

    def test_spec_task_ordering(self, tmp_path: Path):
        """Spec content appears before the positional task with newline separator."""
        spec_file = tmp_path / "notes.md"
        spec_file.write_text(
            "---\nstatus: ready\nrepo: .\ntests:\n  run: pytest\n---\n\n"
            "SPEC_BODY"
        )

        client, handle = _make_mock_client()
        with _patch_client(client):
            exit_code, data = invoke(
                ["--spec", str(spec_file), "TASK_ARG"]
            )

        assert exit_code == 0
        call_args = client.start_workflow.call_args
        dispatched_task = call_args.kwargs["args"][0][0]["task"]
        # Verify spec body first, then newline, then task arg
        assert "SPEC_BODY\nTASK_ARG" in dispatched_task
