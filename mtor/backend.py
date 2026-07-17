"""Durable-backend boundary for operator lifecycle commands.

Temporal remains the only enabled backend. The adapter gives core commands a
backend-neutral surface; legacy visibility operations remain explicitly
Temporal-specific until later migration slices.
"""

from __future__ import annotations

import asyncio
import os
from dataclasses import dataclass
from enum import Enum
from typing import Any, Protocol, runtime_checkable


DEFAULT_BACKEND = "temporal"
SUPPORTED_BACKENDS = frozenset({DEFAULT_BACKEND})


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
class WorkflowSnapshot:
    """Normalized lifecycle state returned to operator commands."""

    task_id: str
    status: str
    start_time: Any = None
    close_time: Any = None


@runtime_checkable
class DurableBackend(Protocol):
    """Minimal lifecycle contract required by the core operator surface."""

    name: str

    async def submit(self, request: Submission) -> str: ...

    async def inspect(self, task_id: str) -> WorkflowSnapshot: ...

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
        status_obj = getattr(description, "status", None)
        status = getattr(status_obj, "name", None) or "UNKNOWN"
        return WorkflowSnapshot(
            task_id=task_id,
            status=status,
            start_time=getattr(description, "start_time", None),
            close_time=getattr(description, "close_time", None),
        )

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


def _coerce_temporal_client_for_compatibility(client: Any) -> TemporalBackend:
    """Wrap legacy raw Temporal clients while their call sites are migrated."""
    if isinstance(client, TemporalBackend):
        return client
    if isinstance(client, BackendAdapter) or isinstance(client, DurableBackend):
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
