"""Tests for mtor receptor route.

Runs via: cd ~/code/mtor && uv run pytest assays/test_receptor_route.py -v
"""

from __future__ import annotations

import io
import json
import sys
from contextlib import ExitStack
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, patch

from mtor.cli import app
from mtor.dispatch import _dispatch_explanation, validate_receptor_spec


def invoke(args: list[str]) -> tuple[int, dict]:
    captured = io.StringIO()
    old_stdout = sys.stdout
    exit_code = 0
    try:
        sys.stdout = captured
        app(args)
    except SystemExit as exc:
        exit_code = exc.code if isinstance(exc.code, int) else 1
    finally:
        sys.stdout = old_stdout

    output = captured.getvalue()
    try:
        return exit_code, json.loads(output)
    except json.JSONDecodeError as exc:
        raise AssertionError(f"Output is not JSON: {output!r}") from exc


def write_spec(
    tmp_path: Path,
    *,
    repo: Path,
    scope: str,
    body: str = "# Spec\nUpdate receptor docs.",
) -> Path:
    test_file = repo / "assays" / "test_receptor_route_fixture.py"
    test_file.parent.mkdir(parents=True, exist_ok=True)
    test_file.write_text("def test_fixture():\n    assert True\n", encoding="utf-8")

    spec = tmp_path / "receptor-spec.md"
    spec.write_text(
        "---\n"
        "status: ready\n"
        f"repo: {repo}\n"
        "scope:\n"
        f"  - {scope}\n"
        "tests:\n"
        f"  run: \"cd {repo} && uv run pytest assays/test_receptor_route_fixture.py -v\"\n"
        "---\n"
        f"{body}\n",
        encoding="utf-8",
    )
    return spec


class TestValidateReceptorSpec:
    def test_accepts_membrane_receptor_scope(self, tmp_path: Path):
        repo = tmp_path / "repo"
        repo.mkdir()
        spec = write_spec(
            tmp_path,
            repo=repo,
            scope="membrane/receptors/kami/SKILL.md",
        )

        assert validate_receptor_spec(spec) == []

    def test_rejects_non_receptor_scope(self, tmp_path: Path):
        repo = tmp_path / "repo"
        repo.mkdir()
        spec = write_spec(tmp_path, repo=repo, scope="mtor/cli.py")

        errors = validate_receptor_spec(spec)

        assert any("membrane/receptors/" in error for error in errors)

    def test_rejects_missing_scope(self, tmp_path: Path):
        repo = tmp_path / "repo"
        repo.mkdir()
        spec = tmp_path / "missing-scope.md"
        spec.write_text(
            "---\n"
            "status: ready\n"
            f"repo: {repo}\n"
            "tests:\n"
            f"  run: \"cd {repo} && uv run pytest assays/test_missing.py -v\"\n"
            "---\n"
            "# Spec\n",
            encoding="utf-8",
        )

        errors = validate_receptor_spec(spec)

        assert any("scope" in error.lower() for error in errors)


class TestReceptorDispatchPlan:
    def test_explain_uses_receptor_mode(self, tmp_path: Path):
        repo = tmp_path / "repo"
        repo.mkdir()
        spec = write_spec(
            tmp_path,
            repo=repo,
            scope="membrane/receptors/kami/SKILL.md",
        )

        plan = _dispatch_explanation(
            "Update receptor",
            mode="receptor",
            spec_path=spec,
            paused=False,
            frozen=False,
        )

        assert plan["search_attributes"]["mtor_mode"] == "receptor"
        assert plan["provider"]["selected"] == "zhipu"


class TestReceptorCommand:
    def test_schema_lists_receptor_command(self):
        exit_code, data = invoke(["schema"])

        assert exit_code == 0
        names = {command["name"] for command in data["result"]["commands"]}
        assert "mtor receptor [prompt]" in names

    def test_receptor_requires_spec(self):
        exit_code, data = invoke(["receptor", "Update Kami"])

        assert exit_code == 2
        assert data["ok"] is False
        assert data["error"]["code"] == "SPEC_REQUIRED"

    def test_receptor_rejects_non_receptor_scope(self, tmp_path: Path):
        repo = tmp_path / "repo"
        repo.mkdir()
        spec = write_spec(tmp_path, repo=repo, scope="mtor/cli.py")

        exit_code, data = invoke(["receptor", "--spec", str(spec), "Update route"])

        assert exit_code == 1
        assert data["ok"] is False
        assert data["error"]["code"] == "RECEPTOR_SPEC_INVALID"

    def test_receptor_dispatches_with_mode(self, tmp_path: Path):
        repo = tmp_path / "repo"
        repo.mkdir()
        spec = write_spec(
            tmp_path,
            repo=repo,
            scope="membrane/receptors/kami/SKILL.md",
        )
        client = MagicMock()
        handle = MagicMock()
        handle.id = "ribosome-receptor-test"
        client.start_workflow = AsyncMock(return_value=handle)

        with ExitStack() as stack:
            stack.enter_context(patch("mtor.dispatch._get_client", return_value=(client, None)))
            stack.enter_context(patch("mtor.dispatch._check_worker_sha", return_value=True))
            stack.enter_context(patch("mtor.dispatch.validate_spec", return_value=[]))
            exit_code, data = invoke(["receptor", "--spec", str(spec), "Update route"])

        assert exit_code == 0
        assert data["ok"] is True
        assert data["result"]["workflow_id"] == "ribosome-receptor-test"
        workflow_spec = client.start_workflow.call_args.kwargs["args"][0][0]
        assert workflow_spec["mode"] == "receptor"
