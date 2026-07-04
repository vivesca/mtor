"""Tests for create_pr activity retry policy wiring in TranslationWorkflow."""

from __future__ import annotations

import inspect

from mtor.worker import workflow


def test_create_pr_activity_has_bounded_retry_policy():
    """The create_pr execute_activity call must pass an explicit retry_policy.

    Without it, Temporal falls back to an unbounded default retry policy,
    which burns silent retries inside the 2-minute start_to_close_timeout
    before the surrounding except block can record a pr_error.
    """
    source = inspect.getsource(workflow.TranslationWorkflow._execute_one)

    # Locate the create_pr execute_activity call and slice to its closing paren.
    create_pr_idx = source.find("create_pr,")
    assert create_pr_idx != -1, "create_pr activity reference not found in _execute_one"

    # The execute_activity call wraps create_pr; find the matching closing paren
    # by tracking paren depth starting from the opening paren before create_pr.
    open_paren_idx = source.rfind("(", 0, create_pr_idx)
    assert open_paren_idx != -1, "opening paren for create_pr call not found"

    depth = 0
    end_idx = None
    for i in range(open_paren_idx, len(source)):
        ch = source[i]
        if ch == "(":
            depth += 1
        elif ch == ")":
            depth -= 1
            if depth == 0:
                end_idx = i
                break
    assert end_idx is not None, "closing paren for create_pr call not found"

    create_pr_call_slice = source[open_paren_idx : end_idx + 1]
    assert "retry_policy" in create_pr_call_slice, (
        "create_pr execute_activity call is missing the retry_policy keyword "
        "argument; it would fall back to Temporal's unbounded default."
    )
