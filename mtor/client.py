"""Temporal client connection logic."""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Any

from mtor import TEMPORAL_HOST


def _get_client():
    """Connect to Temporal server. Returns (client, None) or (None, error_msg)."""
    try:
        import asyncio

        from temporalio.client import Client

        try:
            asyncio.get_running_loop()
            return None, "cannot connect synchronously from a running event loop"
        except RuntimeError:
            pass

        async def _connect():
            return await Client.connect(TEMPORAL_HOST)

        client = asyncio.run(_connect())
        return client, None
    except ImportError:
        return None, "temporalio SDK not installed"
    except Exception as exc:
        return None, str(exc)


async def workflow_execution_state(
    client: Any,
    workflow_id: str,
    *,
    now: datetime | None = None,
    heartbeat_fresh_seconds: int = 60,
) -> dict[str, Any]:
    """Classify a running workflow as queued or executing from activity heartbeat data."""
    current_time = now or datetime.now(UTC)
    handle = client.get_workflow_handle(workflow_id)
    desc = await handle.describe()
    pending_activities = getattr(desc, "pending_activities", None) or []

    latest_heartbeat = None
    for activity in pending_activities:
        heartbeat_time = getattr(activity, "last_heartbeat_time", None)
        if heartbeat_time is None:
            continue
        if heartbeat_time.tzinfo is None:
            heartbeat_time = heartbeat_time.replace(tzinfo=UTC)
        if latest_heartbeat is None or heartbeat_time > latest_heartbeat:
            latest_heartbeat = heartbeat_time

    if latest_heartbeat is None:
        return {"execution_state": "queued"}

    heartbeat_age_seconds = max(0.0, (current_time - latest_heartbeat).total_seconds())
    return {
        "execution_state": "executing" if heartbeat_age_seconds <= heartbeat_fresh_seconds else "queued",
        "last_heartbeat_iso": latest_heartbeat.isoformat(),
        "heartbeat_age_seconds": round(heartbeat_age_seconds, 1),
    }
