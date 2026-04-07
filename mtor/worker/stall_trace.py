"""stall_trace — Langfuse trace integration for stall events (v3)."""

import sys as _sys


def get_langfuse():
    """Return Langfuse client or None if unavailable."""
    try:
        import langfuse
        return langfuse
    except ImportError:
        return None


def record_stall_event(workflow_id: str, pattern: str, action_taken: str, details: dict) -> None:
    """Record a stall event as a Langfuse span for observability."""
    lf = get_langfuse()
    if lf is None:
        return
    try:
        trace = lf.trace()
        trace.span(
            name="stall-detected",
            metadata={
                "workflow_id": workflow_id,
                "pattern": pattern,
                "action": action_taken,
                **details,
            },
        )
    except Exception:
        pass  # graceful no-op


def stall_rate(window_hours: int = 24) -> float:
    """Fraction of recent workflows that hit stalls. Placeholder until trace history available."""
    return 0.0


def most_common_stall_pattern(window_hours: int = 24) -> str | None:
    """Dominant stall pattern name from recent traces. Placeholder until trace history available."""
    return None

