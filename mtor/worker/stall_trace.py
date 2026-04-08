"""stall_trace — Langfuse trace integration for stall events (v3)."""

import sys as _sys


def get_langfuse():
    """Return Langfuse client or None if unavailable."""
    try:
        import langfuse
        return langfuse
    except ImportError:
        return None
