"""stall_trace — Langfuse v4 trace integration for ribosome tasks.

The installed ``langfuse>=4.0.6`` client removed the v2 ``trace()`` /
``generation()`` / ``span()`` surface. This module now uses the v4
observation API: a single root observation created through
``client.start_observation(as_type="agent", ...)`` plus child observations
created through ``observation.start_observation(...)``.

Execution (``create_task_trace``) and review (``record_review_outcome``)
share a stable trace ID derived from the workflow ID via
``Langfuse.create_trace_id(seed=workflow_id)`` so the chaperone review
attaches to the same trace as the task execution.

Every entry point here is a safe no-op when Langfuse is unavailable,
misconfigured, or returns an error. Telemetry must never change the
task or review result.
"""

import sys


_langfuse_client = None


def get_langfuse():
    """Return a cached Langfuse client, or None if unavailable.

    Checks whether the module-level ``get_langfuse`` has been patched
    (e.g. by tests) and delegates to the replacement so that callers
    holding a direct reference still observe the patch.
    """
    global _langfuse_client
    _module = sys.modules.get(__name__)
    _current = getattr(_module, "get_langfuse", None) if _module else None
    if _current is not None and _current is not _original_fn:
        return _current()
    if _langfuse_client is not None:
        return _langfuse_client
    try:
        from langfuse import Langfuse

        _langfuse_client = Langfuse()
        return _langfuse_client
    except Exception:
        return None


_original_fn = get_langfuse


def _derive_trace_id(workflow_id: str) -> str:
    """Derive a stable Langfuse trace ID for a workflow.

    Uses Langfuse 4's deterministic ``create_trace_id(seed=...)`` so the
    execution observation and the later chaperone review attach to the
    same trace. Returns "" when workflow_id is empty or Langfuse is not
    installed.
    """
    if not workflow_id:
        return ""
    try:
        from langfuse import Langfuse

        return Langfuse.create_trace_id(seed=workflow_id)
    except Exception:
        return ""


def create_task_trace(task: str, provider: str, workflow_id: str = "") -> object | None:
    """Create a Langfuse root observation for a ribosome task execution.

    Returns the root agent observation, or None if Langfuse is unavailable.
    No-op if the langfuse package is not installed.
    """
    lf = get_langfuse()
    if lf is None:
        return None
    try:
        trace_id = _derive_trace_id(workflow_id)
        trace_context = {"trace_id": trace_id} if trace_id else None
        return lf.start_observation(
            name=f"ribosome-{workflow_id or 'unknown'}",
            as_type="agent",
            input=task,
            trace_context=trace_context,
            metadata={
                "provider": provider,
                "task": task[:200],
                "workflow_id": workflow_id,
                "input": task,
            },
        )
    except Exception:
        return None


def finalize_trace(trace, result: dict) -> None:
    """Finalize a Langfuse trace with execution results.

    Adds a generation observation with output and metadata, then ends
    both the generation and the root observation explicitly and flushes.
    No-op if trace is None (Langfuse not installed or creation failed).
    """
    if trace is None:
        return
    try:
        verdict = result.get("verdict", "")
        flags = result.get("flags", [])
        exit_code = result.get("exit_code", -1)

        generation = trace.start_observation(
            name="ribosome-execution",
            as_type="generation",
            model=result.get("provider", ""),
            input=result.get("task", ""),
            output=result.get("stdout", "")[:10000],
            status_message=result.get("stderr", ""),
            metadata={
                "exit_code": exit_code,
                "cost_info": result.get("cost_info", "")[:500],
                "provider": result.get("provider", ""),
                "success": result.get("success"),
                "stderr": result.get("stderr", ""),
                "mode": result.get("mode", ""),
                "branch_name": result.get("branch_name", ""),
                "merged": result.get("merged"),
                "diff_stat": result.get("post_diff", {}).get("stat", ""),
                "verdict": verdict,
                "flags": flags,
                "satisfaction": result.get("satisfaction", 0),
            },
        )
        generation.end()
        trace.end()
        lf = get_langfuse()
        if lf is not None:
            lf.flush()
    except Exception:
        pass  # graceful no-op


def create_span(trace, name: str, **metadata) -> None:
    """Add a span observation to an existing trace. No-op if trace is None."""
    if trace is None:
        return
    try:
        span = trace.start_observation(name=name, metadata=metadata)
        span.end()
    except Exception:
        pass


def record_review_outcome(workflow_id: str, review: dict) -> None:
    """Attach the final chaperone review to a workflow's trace.

    Rederives the stable trace ID from ``workflow_id`` and attaches:
      - a ``chaperone-review`` evaluator observation,
      - a categorical ``mtor-verdict`` score when ``verdict`` is present,
      - a numeric ``mtor-satisfaction`` score when ``satisfaction`` is
        present and numeric.

    Silent no-op on any telemetry failure (missing langfuse, missing
    workflow_id, network error, malformed payload, flush failure) —
    review outcomes must never affect the task or review result.
    """
    if not workflow_id:
        return
    try:
        lf = get_langfuse()
        if lf is None:
            return
        trace_id = _derive_trace_id(workflow_id)
        if not trace_id:
            return
        trace_context = {"trace_id": trace_id}

        evaluator = lf.start_observation(
            name="chaperone-review",
            as_type="evaluator",
            trace_context=trace_context,
            metadata={
                "verdict": review.get("verdict", ""),
                "approved": review.get("approved"),
                "flags": review.get("flags", []),
                "satisfaction": review.get("satisfaction", 0),
            },
        )
        evaluator.end()

        verdict = review.get("verdict", "")
        if verdict:
            lf.create_score(
                name="mtor-verdict",
                value=str(verdict),
                data_type="CATEGORICAL",
                trace_id=trace_id,
            )

        satisfaction = review.get("satisfaction", None)
        if satisfaction is not None:
            try:
                lf.create_score(
                    name="mtor-satisfaction",
                    value=float(satisfaction),
                    data_type="NUMERIC",
                    trace_id=trace_id,
                )
            except (TypeError, ValueError):
                pass

        lf.flush()
    except Exception:
        # Spec mandate: every telemetry path is a safe no-op on failure
        # (missing credentials/package, server failure, malformed payload,
        # flush failure). Broad catch is deliberate at this boundary.
        return None


def record_stall_event(
    workflow_id: str,
    pattern: str,
    action_taken: str,
    details: dict,
    trace=None,
) -> None:
    """Record a stall event as a Langfuse observation for observability.

    Attaches a ``stall-detected`` child observation to ``trace`` when
    given; otherwise creates a standalone root observation named
    ``stall-<workflow_id>`` on the workflow's stable trace. Silent no-op
    if Langfuse is unavailable.
    """
    try:
        if trace is None:
            lf = get_langfuse()
            if lf is None:
                return
            trace_id = _derive_trace_id(workflow_id)
            trace_context = {"trace_id": trace_id} if trace_id else None
            parent = lf.start_observation(
                name=f"stall-{workflow_id}",
                trace_context=trace_context,
            )
            should_end_parent = True
        else:
            parent = trace
            should_end_parent = False

        span = parent.start_observation(
            name="stall-detected",
            metadata={
                "workflow_id": workflow_id,
                "pattern": pattern,
                "action": action_taken,
                **details,
            },
        )
        span.end()
        if should_end_parent:
            parent.end()
    except Exception:
        pass  # graceful no-op


def stall_rate(window_hours: int = 24) -> float:
    """Fraction of recent workflows that hit stalls.

    Placeholder until Langfuse trace-history queries land: returns 0.0.
    """
    return 0.0


def most_common_stall_pattern(window_hours: int = 24) -> str | None:
    """Dominant stall pattern name from recent traces.

    Placeholder until Langfuse trace-history queries land: returns None.
    """
    return None
