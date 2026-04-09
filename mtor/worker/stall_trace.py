"""stall_trace — Langfuse trace integration for stall events (v3)."""



def get_langfuse():
    """Return Langfuse client or None if unavailable."""
    try:
        import langfuse
        return langfuse
    except ImportError:
        return None


def create_task_trace(task: str, provider: str, workflow_id: str = "") -> object | None:
    """Create a Langfuse trace for a ribosome task execution.

    Returns the trace object, or None if Langfuse is unavailable.
    No-op if langfuse package is not installed.
    """
    lf = get_langfuse()
    if lf is None:
        return None
    try:
        return lf.trace(
            name=f"ribosome-{workflow_id or 'unknown'}",
            metadata={
                "provider": provider,
                "task": task[:200],
                "workflow_id": workflow_id,
                "input": task,
            },
            tags=[provider],
        )
    except Exception:
        return None


def finalize_trace(trace, result: dict) -> None:
    """Finalize a Langfuse trace with execution results.

    Adds a generation span with output and metadata, then flushes.
    No-op if trace is None (Langfuse not installed or creation failed).
    """
    if trace is None:
        return
    try:
        trace.generation(
            name="ribosome-execution",
            model=result.get("provider", ""),
            input=result.get("task", ""),
            output=result.get("stdout", "")[:10000],
            status_message=result.get("stderr", ""),
            metadata={
                "exit_code": result.get("exit_code", -1),
                "cost_info": result.get("cost_info", "")[:500],
                "provider": result.get("provider", ""),
                "success": result.get("success"),
                "stderr": result.get("stderr", ""),
                "mode": result.get("mode", ""),
                "branch_name": result.get("branch_name", ""),
                "merged": result.get("merged"),
                "diff_stat": result.get("post_diff", {}).get("stat", ""),
            },
        )
        lf = get_langfuse()
        if lf is not None:
            lf.flush()
    except Exception:
        pass  # graceful no-op

