"""Durable-backend boundary for operator lifecycle commands.

Temporal remains the only enabled backend. The adapter gives core commands a
backend-neutral lifecycle surface and structured visibility reads; mutation-
coupled and diagnostic visibility remain Temporal-specific for later slices.
"""

from __future__ import annotations

import asyncio
import os
from dataclasses import dataclass
from datetime import UTC, datetime
from enum import Enum
from typing import Any, Protocol, runtime_checkable


DEFAULT_BACKEND = "temporal"
SUPPORTED_BACKENDS = frozenset({DEFAULT_BACKEND})
VISIBILITY_METADATA_KEYS = frozenset(
    {"mtor_provider", "mtor_verdict", "mtor_mode", "mtor_spec", "mtor_risk"}
)


class BackendConfigurationError(RuntimeError):
    """The requested durable backend is not safe to start in this build."""


class Decision(str, Enum):
    APPROVE = "approve"
    REJECT = "reject"


class StopMode(str, Enum):
    IMMEDIATE = "immediate"
    COOPERATIVE = "cooperative"


def selected_backend_name(name: str | None = None) -> str:
    """Return the validated backend name, failing closed on unknown values."""
    selected = name if name is not None else os.environ.get("MTOR_DURABLE_BACKEND")
    normalized = DEFAULT_BACKEND if selected is None else selected.strip().lower()
    if normalized not in SUPPORTED_BACKENDS:
        supported = ", ".join(sorted(SUPPORTED_BACKENDS))
        raise BackendConfigurationError(
            f"unsupported durable backend {normalized or '<empty>'!r}; "
            f"supported backends: {supported}"
        )
    return normalized


def require_temporal_backend(name: str | None = None) -> str:
    """Validate that this Temporal worker process is allowed to start."""
    selected = selected_backend_name(name)
    if selected != "temporal":
        raise BackendConfigurationError(
            f"Temporal worker cannot run backend {selected!r}"
        )
    return selected


@dataclass(frozen=True)
class Submission:
    """Backend-neutral request to start one staged translation run."""

    task_id: str
    stages: tuple[dict[str, Any], ...]
    metadata: tuple[tuple[str, str], ...] = ()


@dataclass(frozen=True)
class VisibilityQuery:
    """Backend-neutral conjunction used by workflow list and count reads."""

    status: str | None = None
    started_after: datetime | None = None
    metadata: tuple[tuple[str, str], ...] = ()


@dataclass(frozen=True)
class WorkflowSnapshot:
    """Normalized lifecycle state returned to operator commands."""

    task_id: str
    status: str
    start_time: datetime | None = None
    close_time: datetime | None = None
    metadata: tuple[tuple[str, str], ...] = ()


@runtime_checkable
class DurableBackend(Protocol):
    """Minimal lifecycle contract required by the core operator surface."""

    name: str

    async def submit(self, request: Submission) -> str: ...

    async def inspect(self, task_id: str) -> WorkflowSnapshot: ...

    async def list_workflows(
        self, query: VisibilityQuery, *, limit: int | None = None
    ) -> tuple[WorkflowSnapshot, ...]: ...

    async def count_workflows(self, query: VisibilityQuery) -> int: ...

    async def result(self, task_id: str) -> Any: ...

    async def decide(self, task_id: str, decision: Decision) -> None: ...

    async def stop(self, task_id: str, mode: StopMode, *, reason: str) -> None: ...


class BackendAdapter:
    """Required marker base for adapters constructed by the backend factory."""


class TemporalBackend(BackendAdapter):
    """Temporal implementation of the explicit operator lifecycle surface."""

    name = "temporal"

    def __init__(self, native_client: Any) -> None:
        self.native_client = native_client

    @staticmethod
    def _metadata(execution: Any) -> tuple[tuple[str, str], ...]:
        def _normalize(items: Any) -> tuple[tuple[str, str], ...]:
            normalized: dict[str, str] = {}
            for key, value in items:
                try:
                    name = getattr(key, "name", None) or str(key)
                    if name not in VISIBILITY_METADATA_KEYS:
                        continue
                    if isinstance(value, (list, tuple)):
                        value = value[0] if value else ""
                    normalized[name] = "" if value is None else str(value)
                except Exception:
                    continue
            return tuple(sorted(normalized.items()))

        typed = getattr(execution, "typed_search_attributes", None)
        if typed:
            try:
                normalized = _normalize((pair.key, pair.value) for pair in typed)
                if normalized:
                    return normalized
            except Exception:
                pass

        attributes = getattr(execution, "search_attributes", None)
        if attributes:
            try:
                return _normalize(attributes.items())
            except Exception:
                pass
        return ()

    @classmethod
    def _snapshot(cls, execution: Any, *, task_id: str) -> WorkflowSnapshot:
        status_obj = getattr(execution, "status", None)
        status = getattr(status_obj, "name", None)
        if not status and isinstance(status_obj, str):
            status = status_obj
        return WorkflowSnapshot(
            task_id=task_id,
            status=status or "UNKNOWN",
            start_time=getattr(execution, "start_time", None),
            close_time=getattr(execution, "close_time", None),
            metadata=cls._metadata(execution),
        )

    async def submit(self, request: Submission) -> str:
        from temporalio.common import (
            SearchAttributeKey,
            SearchAttributePair,
            TypedSearchAttributes,
            WorkflowIDConflictPolicy,
            WorkflowIDReusePolicy,
        )

        from mtor import TASK_QUEUE, WORKFLOW_TYPE

        pairs = [
            SearchAttributePair(SearchAttributeKey.for_keyword(key), value)
            for key, value in request.metadata
        ]
        stages = [dict(stage) for stage in request.stages]
        handle = await self.native_client.start_workflow(
            WORKFLOW_TYPE,
            args=[stages],
            id=request.task_id,
            task_queue=TASK_QUEUE,
            id_reuse_policy=WorkflowIDReusePolicy.ALLOW_DUPLICATE_FAILED_ONLY,
            id_conflict_policy=WorkflowIDConflictPolicy.USE_EXISTING,
            search_attributes=TypedSearchAttributes(pairs),
        )
        return handle.id

    async def inspect(self, task_id: str) -> WorkflowSnapshot:
        handle = self.native_client.get_workflow_handle(task_id)
        description = await handle.describe()
        return self._snapshot(description, task_id=task_id)

    async def list_workflows(
        self, query: VisibilityQuery, *, limit: int | None = None
    ) -> tuple[WorkflowSnapshot, ...]:
        if limit is not None and limit <= 0:
            raise ValueError("visibility limit must be positive")

        snapshots: list[WorkflowSnapshot] = []
        temporal_query = _temporal_visibility_query(query)
        async for execution in self.native_client.list_workflows(query=temporal_query):
            snapshots.append(self._snapshot(execution, task_id=execution.id))
            if limit is not None and len(snapshots) >= limit:
                break
        return tuple(snapshots)

    async def count_workflows(self, query: VisibilityQuery) -> int:
        result = await self.native_client.count_workflows(
            query=_temporal_visibility_query(query)
        )
        return int(getattr(result, "count", result))

    async def result(self, task_id: str) -> Any:
        handle = self.native_client.get_workflow_handle(task_id)
        return await handle.result()

    async def decide(self, task_id: str, decision: Decision) -> None:
        signal_name = {
            Decision.APPROVE: "approve_task",
            Decision.REJECT: "reject_task",
        }[decision]
        handle = self.native_client.get_workflow_handle(task_id)
        await handle.signal(signal_name, task_id)

    async def stop(self, task_id: str, mode: StopMode, *, reason: str) -> None:
        if mode is StopMode.IMMEDIATE:
            handle = self.native_client.get_workflow_handle(task_id)
            await handle.terminate(reason=reason)
            return
        if mode is StopMode.COOPERATIVE:
            handle = self.native_client.get_workflow_handle(task_id)
            await handle.cancel()
            return
        raise ValueError(f"unsupported stop mode: {mode!r}")


def _temporal_visibility_query(query: VisibilityQuery) -> str | None:
    """Compile one structured visibility query to Temporal SQL syntax."""
    parts: list[str] = []
    if query.status:
        status_values = {
            "RUNNING": "Running",
            "COMPLETED": "Completed",
            "FAILED": "Failed",
            "CANCELED": "Canceled",
            "TERMINATED": "Terminated",
            "CONTINUED_AS_NEW": "ContinuedAsNew",
            "TIMED_OUT": "TimedOut",
        }
        normalized = query.status.strip().upper()
        try:
            temporal_status = status_values[normalized]
        except KeyError as exc:
            raise ValueError(
                f"unsupported visibility status: {query.status!r}"
            ) from exc
        parts.append(f"ExecutionStatus = '{temporal_status}'")

    if query.started_after is not None:
        started_after = query.started_after
        if started_after.tzinfo is None:
            started_after = started_after.replace(tzinfo=UTC)
        started_after = started_after.astimezone(UTC)
        parts.append(f"StartTime > '{started_after.strftime('%Y-%m-%dT%H:%M:%SZ')}'")

    for key, value in query.metadata:
        if key not in VISIBILITY_METADATA_KEYS:
            raise ValueError(f"unsupported visibility metadata key: {key!r}")
        if "'" in value:
            raise ValueError("visibility metadata values cannot contain a quote")
        parts.append(f"{key} = '{value}'")

    return " AND ".join(parts) or None


def _coerce_temporal_client_for_compatibility(client: Any) -> TemporalBackend:
    """Wrap legacy raw Temporal clients while their call sites are migrated."""
    if isinstance(client, TemporalBackend):
        return client
    if isinstance(client, BackendAdapter):
        raise TypeError(f"cannot treat {client.name!r} backend as a Temporal client")
    return TemporalBackend(client)


def _connect_temporal_backend() -> TemporalBackend:
    try:
        from temporalio.client import Client

        try:
            asyncio.get_running_loop()
        except RuntimeError:
            pass
        else:
            raise BackendConfigurationError(
                "cannot connect synchronously from a running event loop"
            )

        async def _connect() -> Any:
            from mtor import TEMPORAL_HOST

            return await Client.connect(TEMPORAL_HOST)

        native_client = asyncio.run(_connect())
        return TemporalBackend(native_client)
    except ImportError as exc:
        raise BackendConfigurationError("temporalio SDK not installed") from exc


_BACKEND_FACTORIES = {"temporal": _connect_temporal_backend}


def connect_backend() -> tuple[DurableBackend | None, str | None]:
    """Connect through an explicit name-to-factory map without fallback."""
    try:
        name = selected_backend_name()
        factory = _BACKEND_FACTORIES.get(name)
        if factory is None:
            raise BackendConfigurationError(
                f"durable backend {name!r} has no configured factory"
            )
        adapter = factory()
        if not isinstance(adapter, BackendAdapter) or not isinstance(
            adapter, DurableBackend
        ):
            raise BackendConfigurationError(
                f"factory for {name!r} returned an invalid backend adapter"
            )
        if adapter.name != name:
            raise BackendConfigurationError(
                f"factory for {name!r} returned backend {adapter.name!r}"
            )
        return adapter, None
    except Exception as exc:
        return None, str(exc)
