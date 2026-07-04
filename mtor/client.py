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


def _pending_activity_records(desc: Any) -> list[Any]:
    """Pending activities from a describe result, preferring the raw proto.

    temporalio's WorkflowExecutionDescription does not expose pending_activities
    directly — only raw_description carries them — so reading
    desc.pending_activities silently yields nothing and every running workflow
    classifies as queued.
    """
    raw = getattr(desc, "raw_description", None)
    if raw is not None:
        try:
            records = list(getattr(raw, "pending_activities", None) or [])
        except TypeError:
            records = []
        if records:
            return records
    try:
        return list(getattr(desc, "pending_activities", None) or [])
    except TypeError:
        return []


def _pending_activity_timestamp(activity: Any, field: str) -> datetime | None:
    """Read a timestamp field off a pending-activity record.

    Tolerates absent fields, non-datetime values, and protobuf Timestamps —
    including unset proto Timestamps, which read as epoch zero and mean "no data".
    """
    value = getattr(activity, field, None)
    if value is not None and hasattr(value, "ToDatetime"):
        if not getattr(value, "seconds", 0) and not getattr(value, "nanos", 0):
            return None
        value = value.ToDatetime()
    if not isinstance(value, datetime):
        return None
    if value.tzinfo is None:
        return value.replace(tzinfo=UTC)
    return value


async def workflow_execution_state(
    client: Any,
    workflow_id: str,
    *,
    now: datetime | None = None,
    heartbeat_fresh_seconds: int = 150,
    heartbeat_stale_seconds: int = 900,
) -> dict[str, Any]:
    """Classify a running workflow as queued or executing from live Temporal evidence.

    Two evidence sources, freshest wins: activity heartbeat recency and activity
    attempt-start recency. The SDK throttles heartbeat RPCs to
    min(0.8 * heartbeat_timeout, 60s), so server-recorded heartbeats lag the 30s
    local heartbeat ticks by up to ~60s — the fresh window must absorb that lag.
    Attempt-start recency covers the window right after a worker restart, when a
    retried attempt is executing but its first throttled heartbeat has not been
    recorded yet and last_heartbeat_time still shows the dead attempt.
    """
    current_time = now or datetime.now(UTC)
    handle = client.get_workflow_handle(workflow_id)
    desc = await handle.describe()
    pending_activities = _pending_activity_records(desc)

    latest_heartbeat = None
    latest_started = None
    for activity in pending_activities:
        heartbeat_time = _pending_activity_timestamp(activity, "last_heartbeat_time")
        if heartbeat_time and (latest_heartbeat is None or heartbeat_time > latest_heartbeat):
            latest_heartbeat = heartbeat_time
        started_time = _pending_activity_timestamp(activity, "last_started_time")
        if started_time and (latest_started is None or started_time > latest_started):
            latest_started = started_time

    if latest_heartbeat is None and latest_started is None:
        return {"execution_state": "queued"}

    ages = []
    result: dict[str, Any] = {}
    if latest_heartbeat is not None:
        heartbeat_age_seconds = max(0.0, (current_time - latest_heartbeat).total_seconds())
        ages.append(heartbeat_age_seconds)
        result["last_heartbeat_iso"] = latest_heartbeat.isoformat()
        result["heartbeat_age_seconds"] = round(heartbeat_age_seconds, 1)
    if latest_started is not None:
        started_age_seconds = max(0.0, (current_time - latest_started).total_seconds())
        ages.append(started_age_seconds)
        result["last_started_iso"] = latest_started.isoformat()
        result["started_age_seconds"] = round(started_age_seconds, 1)

    evidence_age_seconds = min(ages)
    result["execution_state"] = (
        "executing" if evidence_age_seconds <= heartbeat_fresh_seconds else "queued"
    )
    result["heartbeat_stale"] = evidence_age_seconds > heartbeat_stale_seconds
    return result
