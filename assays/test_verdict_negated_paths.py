"""Regression: verdict gate must not require paths mentioned only as prohibitions.

Reproduces the rejection of workflow ribosome-glm52-fix-flaky-b0a2b495-6a48b960:
the task said to edit one assay file ONLY and NOT to touch the CLI module, and
the gate rejected the run with a target-file-missing flag for the CLI module.
"""

from __future__ import annotations

import asyncio

from mtor.worker.chaperone_review import (
    _negated_task_paths,
    _requested_task_paths,
    chaperone,
)

CLI = "mtor/cli.py"
TARGET = "assays/test_mtor.py"
OTHER = "assays/test_other.py"
SPEC_PY = "mtor/spec.py"
WATCH = "mtor/watch.py"


def _result(task: str, stat: str) -> dict:
    return {
        "success": True,
        "exit_code": 0,
        "stdout": "Done. Changes committed.",
        "stderr": "",
        "task": task,
        "provider": "zhipu",
        "post_diff": {
            "stat": stat,
            "numstat": "",
            "commits": ["abc1234 fix"],
            "commit_count": 1,
        },
        "pre_diff": {"stat": "", "numstat": ""},
        "branch_name": "",
        "cost_info": "",
    }


def test_verdict_negated_path_not_required():
    task = (
        f"One-file, one-edit task. Edit `{TARGET}` ONLY. "
        f"Do not touch `{CLI}` or any other file."
    )
    review = asyncio.run(chaperone(_result(task, f" {TARGET} | 12 +++++++-----\n")))
    assert not any(f.startswith("target_file_missing") for f in review["flags"])


def test_verdict_missing_requested_path_still_flagged():
    task = f"Modify {CLI} to add a new flag."
    review = asyncio.run(chaperone(_result(task, f" {OTHER} | 3 +++\n")))
    assert f"target_file_missing: {CLI}" in review["flags"]


def test_verdict_negated_task_paths_helper():
    negated = _negated_task_paths(
        f"Update {SPEC_PY}. Do not touch {CLI} or {WATCH}."
    )
    assert negated == {CLI, WATCH}


def test_requested_paths_ignore_verifier_and_repository_context():
    task = (
        "Modify mtor/dispatch.py to add the helper.\n"
        "Run: uv run pytest assays/test_spec_gate_path_preflight.py\n"
        "Canonical repository: /home/vivesca/code/mtor.\n"
        "Do NOT modify: mtor/cli.py."
    )

    assert _requested_task_paths(task) == {"mtor/dispatch.py"}


def test_verdict_test_only_path_not_required():
    task = (
        "Modify mtor/dispatch.py to add the helper.\n"
        "Run: uv run pytest assays/test_spec_gate_path_preflight.py"
    )
    review = asyncio.run(
        chaperone(_result(task, " mtor/dispatch.py | 12 ++++++++++++\n"))
    )

    assert not any(f.startswith("target_file_missing") for f in review["flags"])
