"""Regression tests for _task_file_paths extension-boundary handling."""

from __future__ import annotations

from mtor.worker.chaperone_review import _task_file_paths


def test_verdict_task_paths_jsonl_no_phantom_json():
    task = (
        "Fix the ledger append so ribosome-reviews" + ".jsonl gets one line per verdict"
    )
    paths = _task_file_paths(task)
    assert not any(p.endswith(".json") for p in paths), paths


def test_verdict_task_paths_mdx_no_phantom_md():
    task = "Update the docs page foo" + ".mdx header"
    paths = _task_file_paths(task)
    assert paths == set(), paths


def test_verdict_task_paths_real_paths_still_extracted():
    task = "Fix mtor/worker/chaperone_review.py and update assays/test_verdict_task_paths.py."
    paths = _task_file_paths(task)
    assert "mtor/worker/chaperone_review.py" in paths
    assert "assays/test_verdict_task_paths.py" in paths
