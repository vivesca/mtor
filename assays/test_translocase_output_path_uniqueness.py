"""Regression test: output filenames must not collide across concurrent tasks."""

from __future__ import annotations

from mtor.worker.translocase import _derive_output_tid


def test_two_concurrent_tasks_without_task_marker_get_distinct_ids_via_workflow_id():
    tid_a = _derive_output_tid(None, "workflow-aaa-111")
    tid_b = _derive_output_tid(None, "workflow-bbb-222")

    assert tid_a != tid_b


def test_task_marker_takes_priority_over_workflow_id_for_cache_stability():
    import re

    match = re.search(r"\[t-([0-9a-fA-F]+)\]", "do the thing [t-deadbeef]")

    assert _derive_output_tid(match, "workflow-xyz") == "deadbeef"
