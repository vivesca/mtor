"""Behavioral contract for backend-neutral workflow visibility reads."""

from __future__ import annotations

import asyncio
import json
from datetime import UTC, datetime, timedelta, timezone
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

import pytest
from temporalio.common import (
    SearchAttributeKey,
    SearchAttributePair,
    TypedSearchAttributes,
)

from mtor.backend import TemporalBackend, VisibilityQuery, WorkflowSnapshot


def _execution(
    task_id: str,
    status: str,
    *,
    metadata=None,
    typed_metadata=None,
    start_time=None,
    close_time=None,
):
    return SimpleNamespace(
        id=task_id,
        status=SimpleNamespace(name=status),
        start_time=start_time,
        close_time=close_time,
        search_attributes=metadata or {},
        typed_search_attributes=typed_metadata or TypedSearchAttributes.empty,
    )


def test_temporal_list_compiles_query_and_normalizes_snapshots() -> None:
    queries = []
    typed_metadata = TypedSearchAttributes(
        [
            SearchAttributePair(
                SearchAttributeKey.for_keyword("mtor_provider"), "zhipu"
            ),
            SearchAttributePair(
                SearchAttributeKey.for_keyword("mtor_verdict"), "approved"
            ),
            SearchAttributePair(
                SearchAttributeKey.for_keyword("mtor_spec"), "/tmp/spec.md"
            ),
            SearchAttributePair(
                SearchAttributeKey.for_keyword("unrelated_attribute"), "ignored"
            ),
        ]
    )
    executions = [
        _execution(
            "task-1",
            "RUNNING",
            metadata={"mtor_provider": ["legacy-value"]},
            typed_metadata=typed_metadata,
            start_time=datetime(2026, 7, 17, 8, tzinfo=UTC),
        ),
        _execution("task-2", "COMPLETED", metadata={"mtor_mode": "build"}),
        _execution("task-3", "FAILED"),
    ]
    native_client = MagicMock()

    async def list_workflows(*, query):
        queries.append(query)
        for execution in executions:
            yield execution

    native_client.list_workflows = list_workflows
    backend = TemporalBackend(native_client)
    started_after = datetime(2026, 7, 17, 16, 30, tzinfo=timezone(timedelta(hours=8)))

    snapshots = asyncio.run(
        backend.list_workflows(
            VisibilityQuery(
                status="running",
                started_after=started_after,
                metadata=(
                    ("mtor_provider", "zhipu"),
                    ("mtor_mode", "build"),
                ),
            ),
            limit=2,
        )
    )

    assert queries == [
        "ExecutionStatus = 'Running' AND StartTime > '2026-07-17T08:30:00Z' "
        "AND mtor_provider = 'zhipu' AND mtor_mode = 'build'"
    ]
    assert [snapshot.task_id for snapshot in snapshots] == ["task-1", "task-2"]
    assert snapshots[0].status == "RUNNING"
    assert snapshots[0].metadata == (
        ("mtor_provider", "zhipu"),
        ("mtor_spec", "/tmp/spec.md"),
        ("mtor_verdict", "approved"),
    )
    assert dict(snapshots[0].metadata)["mtor_spec"] == "/tmp/spec.md"
    assert dict(snapshots[1].metadata)["mtor_mode"] == "build"


def test_temporal_list_without_filters_passes_none_and_preserves_order() -> None:
    queries = []
    executions = [
        _execution("task-2", "COMPLETED", metadata=object()),
        _execution("task-1", "RUNNING"),
    ]
    native_client = MagicMock()

    async def list_workflows(*, query):
        queries.append(query)
        for execution in executions:
            yield execution

    native_client.list_workflows = list_workflows

    snapshots = asyncio.run(
        TemporalBackend(native_client).list_workflows(VisibilityQuery())
    )

    assert queries == [None]
    assert [snapshot.task_id for snapshot in snapshots] == ["task-2", "task-1"]
    assert snapshots[0].metadata == ()


@pytest.mark.parametrize("native_result", [7, SimpleNamespace(count=7)])
def test_temporal_count_returns_plain_integer(native_result) -> None:
    native_client = MagicMock()
    native_client.count_workflows = AsyncMock(return_value=native_result)
    backend = TemporalBackend(native_client)

    count = asyncio.run(backend.count_workflows(VisibilityQuery(status="COMPLETED")))

    assert count == 7
    native_client.count_workflows.assert_awaited_once_with(
        query="ExecutionStatus = 'Completed'"
    )


@pytest.mark.parametrize(
    ("status", "temporal_status"),
    [
        ("RUNNING", "Running"),
        ("COMPLETED", "Completed"),
        ("FAILED", "Failed"),
        ("CANCELED", "Canceled"),
        ("TERMINATED", "Terminated"),
        ("CONTINUED_AS_NEW", "ContinuedAsNew"),
        ("TIMED_OUT", "TimedOut"),
    ],
)
def test_temporal_count_compiles_every_supported_status(
    status, temporal_status
) -> None:
    native_client = MagicMock()
    native_client.count_workflows = AsyncMock(return_value=0)

    asyncio.run(
        TemporalBackend(native_client).count_workflows(VisibilityQuery(status=status))
    )

    native_client.count_workflows.assert_awaited_once_with(
        query=f"ExecutionStatus = '{temporal_status}'"
    )


def test_invalid_count_query_fails_before_native_access() -> None:
    native_client = MagicMock()
    native_client.count_workflows = AsyncMock()

    with pytest.raises(ValueError, match="unsupported visibility status"):
        asyncio.run(
            TemporalBackend(native_client).count_workflows(
                VisibilityQuery(status="unknown")
            )
        )

    native_client.count_workflows.assert_not_awaited()


def test_visibility_errors_propagate_without_partial_results() -> None:
    native_client = MagicMock()
    native_client.count_workflows = AsyncMock(side_effect=RuntimeError("query failed"))
    backend = TemporalBackend(native_client)

    with pytest.raises(RuntimeError, match="query failed"):
        asyncio.run(backend.count_workflows(VisibilityQuery()))


@pytest.mark.parametrize(
    ("query", "limit", "message"),
    [
        (VisibilityQuery(status="unknown"), None, "unsupported visibility status"),
        (
            VisibilityQuery(metadata=(("unknown", "value"),)),
            None,
            "unsupported visibility metadata key",
        ),
        (
            VisibilityQuery(metadata=(("mtor_provider", "can't"),)),
            None,
            "metadata values cannot contain a quote",
        ),
        (VisibilityQuery(), 0, "visibility limit must be positive"),
    ],
)
def test_invalid_visibility_request_fails_before_native_listing(
    query, limit, message
) -> None:
    native_client = MagicMock()
    backend = TemporalBackend(native_client)

    with pytest.raises(ValueError, match=message):
        asyncio.run(backend.list_workflows(query, limit=limit))

    native_client.list_workflows.assert_not_called()


@pytest.mark.parametrize(
    "operation",
    [
        lambda cli: cli.stats(),
        lambda cli: cli.dossier("task-1"),
        lambda cli: cli.wait("task-1", interval=2),
        lambda cli: cli._query_watch_workflow("mtor ragulator query", "watch-1"),
    ],
)
def test_migrated_visibility_commands_fail_before_backend_connection(
    monkeypatch, operation
) -> None:
    from mtor import cli

    monkeypatch.setenv("MTOR_DURABLE_BACKEND", "dbos")
    connect = MagicMock(side_effect=AssertionError("backend connection touched"))
    monkeypatch.setattr(cli, "_connect_backend", connect)

    with pytest.raises(SystemExit) as exc_info:
        operation(cli)

    assert exc_info.value.code == 3
    connect.assert_not_called()


def test_stats_uses_backend_count_without_legacy_temporal_client(
    monkeypatch, capsys
) -> None:
    from mtor import cli

    backend = MagicMock()
    backend.count_workflows = AsyncMock(return_value=4)
    legacy_client = MagicMock(side_effect=AssertionError("legacy client touched"))
    monkeypatch.delenv("MTOR_DURABLE_BACKEND", raising=False)
    monkeypatch.setattr(cli, "_get_backend", MagicMock(return_value=(backend, None)))
    monkeypatch.setattr(cli, "_legacy_get_client", legacy_client)

    cli.stats()

    envelope = json.loads(capsys.readouterr().out)
    assert set(envelope["result"]["counts"].values()) == {4}
    assert backend.count_workflows.await_count == 5
    legacy_client.assert_not_called()


def test_wait_polls_through_backend_until_completed(monkeypatch, capsys) -> None:
    from mtor import cli

    backend = SimpleNamespace(
        inspect=AsyncMock(
            side_effect=[
                WorkflowSnapshot("task-1", "RUNNING"),
                WorkflowSnapshot("task-1", "CONTINUED_AS_NEW"),
                WorkflowSnapshot(
                    "task-1",
                    "COMPLETED",
                    start_time=datetime(2026, 7, 17, 8, tzinfo=UTC),
                    close_time=datetime(2026, 7, 17, 8, 1, tzinfo=UTC),
                ),
            ]
        ),
        result=AsyncMock(return_value={}),
    )
    sleep = AsyncMock()
    monkeypatch.setattr(cli, "_get_backend", lambda: (backend, None))
    monkeypatch.setattr(cli.asyncio, "sleep", sleep)
    monkeypatch.setattr(cli, "get_verdict_overrides", lambda: {})

    cli.wait("task-1", interval=2)

    envelope = json.loads(capsys.readouterr().out)
    assert envelope["result"]["status"] == "COMPLETED"
    assert envelope["result"]["polls"] == 3
    assert [entry.args for entry in backend.inspect.await_args_list] == [
        ("task-1",),
        ("task-1",),
        ("task-1",),
    ]
    backend.result.assert_awaited_once_with("task-1")
    assert sleep.await_count == 2


def test_stats_keeps_successful_counts_when_one_query_fails(
    monkeypatch, capsys
) -> None:
    from mtor import cli

    backend = SimpleNamespace(
        count_workflows=AsyncMock(
            side_effect=[1, RuntimeError("one query failed"), 3, 4, 5]
        )
    )
    monkeypatch.setattr(cli, "_get_backend", lambda: (backend, None))

    cli.stats()

    envelope = json.loads(capsys.readouterr().out)
    assert envelope["result"]["counts"] == {
        "running": 1,
        "today_total": -1,
        "today_completed": 3,
        "week_total": 4,
        "week_completed": 5,
    }
    assert backend.count_workflows.await_count == 5


def test_doctor_rejects_unsupported_backend_before_any_probe(
    monkeypatch, capsys
) -> None:
    from mtor import doctor

    connect = MagicMock(side_effect=AssertionError("backend connection touched"))
    admission = MagicMock(side_effect=AssertionError("worker admission touched"))
    monkeypatch.setenv("MTOR_DURABLE_BACKEND", "dbos")
    monkeypatch.setattr(doctor, "_get_backend", connect)
    monkeypatch.setattr(doctor, "probe_worker_admission", admission)

    with pytest.raises(SystemExit) as exc_info:
        doctor.doctor()

    envelope = json.loads(capsys.readouterr().out)
    assert exc_info.value.code == 3
    assert envelope["error"]["code"] == "BACKEND_UNSUPPORTED"
    connect.assert_not_called()
    admission.assert_not_called()
