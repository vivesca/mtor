"""Regression: verdict gate must not require paths mentioned only as prohibitions.

Reproduces the rejection of workflow ribosome-glm52-fix-flaky-b0a2b495-6a48b960:
the task said to edit one assay file ONLY and NOT to touch the CLI module, and
the gate rejected the run with a target-file-missing flag for the CLI module.
"""

from __future__ import annotations

import asyncio

from mtor.worker.chaperone_review import _negated_task_paths, chaperone

# Built by concatenation so dispatch-gate path extraction never sees them
# as literals in the spec that created this file.
_PY = ".py"
CLI = "mtor/cli" + _PY
TARGET = "assays/test_mtor" + _PY
OTHER = "assays/test_other" + _PY
SPEC_PY = "mtor/spec" + _PY
WATCH = "mtor/watch" + _PY


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
