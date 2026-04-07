"""Temporal client connection logic."""

from __future__ import annotations

from mtor import TEMPORAL_HOST


def _get_client():
    """Connect to Temporal server. Returns (client, None) or (None, error_msg)."""
    try:
        import asyncio

        from temporalio.client import Client

        async def _connect():
            return await Client.connect(TEMPORAL_HOST)

        client = asyncio.run(_connect())
        return client, None
    except ImportError:
        return None, "temporalio SDK not installed"
    except Exception as exc:
        return None, str(exc)
