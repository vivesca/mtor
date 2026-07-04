"""Tests for the create_pr Path.home() sandbox fix."""

from __future__ import annotations

from pathlib import Path

from mtor.cli import _pr_result_fields
from mtor.worker.git_ops import _create_pr_impl
from mtor.worker.workflow import _summarize_workflow_result


def test_summarize_workflow_result_forwards_pr_fields():
    result = {
        "task": "do the thing",
        "provider": "zhipu",
        "success": True,
        "exit_code": 0,
        "mode": "raw",
        "review": {"verdict": "approved"},
        "branch_name": "ribosome-some-task",
        "merged": False,
        "pr_url": "",
        "pr_number": 0,
        "pr_created": False,
        "pr_error": "boom",
    }
    summary = _summarize_workflow_result(result)
    for key in (
        "branch_name",
        "merged",
        "pr_url",
        "pr_number",
        "pr_created",
        "pr_error",
    ):
        assert key in summary, f"missing {key}"
        assert summary[key] == result[key], f"wrong value for {key}"


def test_create_pr_expands_tilde_repo_root(monkeypatch):
    recorded_cwds: list[str | None] = []

    class FakeCompleted:
        returncode = 0
        stdout = ""
        stderr = ""

    def fake_run(command, *args, **kwargs):
        recorded_cwds.append(kwargs.get("cwd"))
        return FakeCompleted()

    monkeypatch.setattr(
        "mtor.worker.git_ops._run_worker_command",
        fake_run,
    )

    _create_pr_impl("~/germline", "some-branch")

    assert recorded_cwds, "no subprocess calls recorded"
    first_cwd = recorded_cwds[0]
    assert first_cwd is not None
    assert not str(first_cwd).startswith("~"), "tilde was not expanded"
    assert first_cwd == str(Path.home() / "germline")


def test_status_pr_result_fields_present():
    task_result = {
        "branch_name": "ribosome-x",
        "pr_url": "https://github.com/org/repo/pull/1",
        "pr_number": 1,
        "pr_created": True,
        "pr_error": "",
    }
    assert _pr_result_fields(task_result) == {
        "branch_name": "ribosome-x",
        "pr_url": "https://github.com/org/repo/pull/1",
        "pr_number": 1,
        "pr_created": True,
        "pr_error": "",
    }


def test_status_pr_result_fields_absent():
    assert _pr_result_fields({"task": "x"}) == {}
