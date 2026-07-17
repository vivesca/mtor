"""Behavioral contract for the first durable-backend seam."""

from __future__ import annotations

import asyncio
import os
import subprocess
import sys
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from mtor.backend import (
    BackendAdapter,
    BackendConfigurationError,
    Decision,
    StopMode,
    Submission,
    TemporalBackend,
    _coerce_temporal_client_for_compatibility,
    connect_backend,
    require_temporal_backend,
)


@pytest.mark.parametrize("selected", [None, "temporal", " TEMPORAL "])
def test_default_and_explicit_temporal_select_the_same_adapter(
    monkeypatch, selected
) -> None:
    if selected is None:
        monkeypatch.delenv("MTOR_DURABLE_BACKEND", raising=False)
    else:
        monkeypatch.setenv("MTOR_DURABLE_BACKEND", selected)
    native_client = MagicMock(name="TemporalClient")
    client_class = MagicMock(name="Client")
    client_class.connect = AsyncMock(return_value=native_client)

    with patch.dict(
        sys.modules,
        {
            "temporalio": MagicMock(),
            "temporalio.client": MagicMock(Client=client_class),
        },
    ):
        backend, error = connect_backend()

    assert error is None
    assert isinstance(backend, TemporalBackend)
    assert backend.native_client is native_client
    client_class.connect.assert_awaited_once()


@pytest.mark.parametrize("selected", ["", "dbos", "unknown"])
def test_unsupported_backend_fails_before_temporal_connection(
    monkeypatch, selected
) -> None:
    monkeypatch.setenv("MTOR_DURABLE_BACKEND", selected)
    client_class = MagicMock(name="Client")
    client_class.connect = AsyncMock()

    with patch.dict(
        sys.modules,
        {
            "temporalio": MagicMock(),
            "temporalio.client": MagicMock(Client=client_class),
        },
    ):
        backend, error = connect_backend()

    assert backend is None
    assert error and "unsupported durable backend" in error
    client_class.connect.assert_not_called()


def test_future_supported_backend_cannot_fall_back_to_temporal(monkeypatch) -> None:
    from mtor import backend as backend_module

    monkeypatch.setattr(
        backend_module, "SUPPORTED_BACKENDS", frozenset({"temporal", "dbos"})
    )
    monkeypatch.setenv("MTOR_DURABLE_BACKEND", "dbos")
    temporal_factory = MagicMock(side_effect=AssertionError("Temporal selected"))
    monkeypatch.setitem(backend_module._BACKEND_FACTORIES, "temporal", temporal_factory)

    with pytest.raises(BackendConfigurationError, match="Temporal worker cannot run"):
        require_temporal_backend()

    backend, error = connect_backend()

    assert backend is None
    assert error and "has no configured factory" in error
    temporal_factory.assert_not_called()


def test_factory_rejects_adapter_registered_under_the_wrong_name(monkeypatch) -> None:
    from mtor import backend as backend_module

    monkeypatch.setattr(
        backend_module, "SUPPORTED_BACKENDS", frozenset({"temporal", "dbos"})
    )
    monkeypatch.setenv("MTOR_DURABLE_BACKEND", "dbos")
    monkeypatch.setitem(
        backend_module._BACKEND_FACTORIES,
        "dbos",
        lambda: TemporalBackend(MagicMock()),
    )

    backend, error = connect_backend()

    assert backend is None
    assert error == "factory for 'dbos' returned backend 'temporal'"


def test_factory_rejects_non_adapter_result(monkeypatch) -> None:
    from mtor import backend as backend_module

    monkeypatch.setattr(
        backend_module, "SUPPORTED_BACKENDS", frozenset({"temporal", "dbos"})
    )
    monkeypatch.setenv("MTOR_DURABLE_BACKEND", "dbos")
    monkeypatch.setitem(backend_module._BACKEND_FACTORIES, "dbos", object)

    backend, error = connect_backend()

    assert backend is None
    assert error == "factory for 'dbos' returned an invalid backend adapter"


def test_compatibility_wrapper_rejects_marked_non_temporal_backend() -> None:
    class StructuralBackend(BackendAdapter):
        name = "dbos"

        async def submit(self, request):
            return request.task_id

        async def inspect(self, task_id):
            return None

        async def result(self, task_id):
            return None

        async def decide(self, task_id, decision):
            return None

        async def stop(self, task_id, mode, *, reason):
            return None

    with pytest.raises(TypeError, match="cannot treat 'dbos' backend"):
        _coerce_temporal_client_for_compatibility(StructuralBackend())


def test_compatibility_wrapper_accepts_raw_temporal_mock() -> None:
    native_client = MagicMock()
    native_client.start_workflow = AsyncMock()

    backend = _coerce_temporal_client_for_compatibility(native_client)

    assert backend.native_client is native_client


def test_temporal_adapter_preserves_lifecycle_contract() -> None:
    from temporalio.common import WorkflowIDConflictPolicy, WorkflowIDReusePolicy

    native_client = MagicMock()
    started_handle = MagicMock(id="task-123")
    native_client.start_workflow = AsyncMock(return_value=started_handle)
    handle = MagicMock()
    status = MagicMock(name="status")
    status.name = "RUNNING"
    description = MagicMock(status=status, start_time=None, close_time=None)
    handle.describe = AsyncMock(return_value=description)
    handle.result = AsyncMock(return_value={"succeeded": 1})
    handle.signal = AsyncMock()
    handle.terminate = AsyncMock()
    handle.cancel = AsyncMock()
    native_client.get_workflow_handle.return_value = handle
    backend = TemporalBackend(native_client)

    async def exercise():
        started = await backend.submit(
            Submission(
                task_id="task-123",
                stages=({"task_id": "task-123", "task": "test"},),
                metadata=(
                    ("mtor_provider", "zhipu"),
                    ("mtor_mode", "build"),
                ),
            )
        )
        snapshot = await backend.inspect("task-123")
        result = await backend.result("task-123")
        await backend.decide("task-123", Decision.APPROVE)
        await backend.decide("task-123", Decision.REJECT)
        await backend.stop(
            "task-123", StopMode.IMMEDIATE, reason="Terminated via mtor CLI"
        )
        await backend.stop("task-123", StopMode.COOPERATIVE, reason="batch cleanup")
        return started, snapshot, result

    started, snapshot, result = asyncio.run(exercise())

    assert started == "task-123"
    assert snapshot.task_id == "task-123"
    assert snapshot.status == "RUNNING"
    assert result == {"succeeded": 1}
    start_kwargs = native_client.start_workflow.call_args.kwargs
    assert start_kwargs["args"] == [[{"task_id": "task-123", "task": "test"}]]
    assert start_kwargs["id"] == "task-123"
    assert start_kwargs["task_queue"] == "translation-queue"
    assert (
        start_kwargs["id_reuse_policy"]
        is WorkflowIDReusePolicy.ALLOW_DUPLICATE_FAILED_ONLY
    )
    assert start_kwargs["id_conflict_policy"] is WorkflowIDConflictPolicy.USE_EXISTING
    attributes = {
        pair.key.name: pair.value for pair in start_kwargs["search_attributes"]
    }
    assert attributes == {"mtor_provider": "zhipu", "mtor_mode": "build"}
    assert handle.signal.await_args_list == [
        (("approve_task", "task-123"),),
        (("reject_task", "task-123"),),
    ]
    handle.terminate.assert_awaited_once_with(reason="Terminated via mtor CLI")
    handle.cancel.assert_awaited_once_with()


def test_temporal_adapter_rejects_unknown_stop_mode_before_client_access() -> None:
    native_client = MagicMock()
    backend = TemporalBackend(native_client)

    async def exercise() -> None:
        with pytest.raises(ValueError, match="unsupported stop mode"):
            await backend.stop(
                "task-123",
                "immediate",
                reason="should not run",  # type: ignore[arg-type]
            )

    asyncio.run(exercise())

    native_client.get_workflow_handle.assert_not_called()


def test_invalid_backend_blocks_dispatch_preflights(monkeypatch) -> None:
    from mtor import dispatch

    monkeypatch.setenv("MTOR_DURABLE_BACKEND", "dbos")
    admission = MagicMock(side_effect=AssertionError("worker admission touched"))
    sha_gate = MagicMock(side_effect=AssertionError("SHA gate touched"))
    connection = MagicMock(side_effect=AssertionError("backend connection touched"))
    monkeypatch.setattr(dispatch, "_require_worker_admission", admission)
    monkeypatch.setattr(dispatch, "_check_worker_sha", sha_gate)
    monkeypatch.setattr(dispatch, "_get_client", connection)

    with pytest.raises(SystemExit) as exc_info:
        dispatch._dispatch_prompt("test unsupported backend", mode="build")

    assert exc_info.value.code == 3
    admission.assert_not_called()
    sha_gate.assert_not_called()
    connection.assert_not_called()


def test_invalid_backend_blocks_dispatch_explanation_load_probe(monkeypatch) -> None:
    from mtor import dispatch

    monkeypatch.setenv("MTOR_DURABLE_BACKEND", "dbos")
    load_plan = MagicMock(side_effect=AssertionError("worker load probe touched"))
    monkeypatch.setattr(dispatch, "_worker_load_plan", load_plan)

    with pytest.raises(BackendConfigurationError, match="unsupported durable backend"):
        dispatch._dispatch_explanation("explain unsupported backend")

    load_plan.assert_not_called()


def test_invalid_backend_explain_cli_returns_configuration_error(monkeypatch) -> None:
    from mtor import cli, dispatch

    monkeypatch.setenv("MTOR_DURABLE_BACKEND", "dbos")
    explanation = MagicMock(side_effect=AssertionError("explanation built"))
    monkeypatch.setattr(dispatch, "_dispatch_explanation", explanation)

    with pytest.raises(SystemExit) as exc_info:
        cli.default_handler("explain unsupported backend", explain=True)

    assert exc_info.value.code == 3
    explanation.assert_not_called()


@pytest.mark.parametrize(
    "operation",
    [
        "status",
        "logs",
        "logs_active",
        "cancel",
        "approve",
        "deny",
        "watch_start",
        "watch_once",
        "watch_stop",
        "setup_search_attrs",
        "reconcile",
        "dispatch_all",
        "deploy",
        "rictor_deploy",
    ],
)
def test_invalid_backend_blocks_operator_side_effects(
    monkeypatch, tmp_path, operation
) -> None:
    from mtor import cli, infra, reconcile

    monkeypatch.setenv("MTOR_DURABLE_BACKEND", "dbos")
    connection = MagicMock(side_effect=AssertionError("backend connection touched"))
    subprocess_run = MagicMock(side_effect=AssertionError("subprocess touched"))
    reaper = MagicMock(side_effect=AssertionError("process reaper touched"))
    local_watch = MagicMock(side_effect=AssertionError("local watch touched"))
    active_logs = MagicMock(side_effect=AssertionError("active logs touched"))
    search_setup = MagicMock(side_effect=AssertionError("search setup touched"))
    spec_reconcile = MagicMock(side_effect=AssertionError("reconcile touched"))
    spec_scan = MagicMock(side_effect=AssertionError("spec scan touched"))
    remote_deploy = MagicMock(side_effect=AssertionError("deploy touched"))
    monkeypatch.setattr(cli, "_get_client", connection)
    monkeypatch.setattr(cli.subprocess, "run", subprocess_run)
    monkeypatch.setattr(cli, "_reap_worker_processes", reaper)
    monkeypatch.setattr(cli, "run_watch", local_watch)
    monkeypatch.setattr(cli, "_active_logs", active_logs)
    monkeypatch.setattr(infra, "setup_search_attributes", search_setup)
    monkeypatch.setattr(reconcile, "reconcile_all", spec_reconcile)
    monkeypatch.setattr(cli, "scan_specs", spec_scan)
    monkeypatch.setattr(cli, "_deploy", remote_deploy)

    calls = {
        "status": lambda: cli.status("task-123"),
        "logs": lambda: cli.logs("task-123"),
        "logs_active": lambda: cli.logs(active=True),
        "cancel": lambda: cli.cancel("task-123"),
        "approve": lambda: cli.approve("task-123"),
        "deny": lambda: cli.deny("task-123"),
        "watch_start": lambda: cli.ragulator("start"),
        "watch_once": lambda: cli.ragulator("start", once=True),
        "watch_stop": lambda: cli._stop_watch_workflow(
            "mtor ragulator stop", "watch-123"
        ),
        "setup_search_attrs": cli.setup_search_attrs,
        "reconcile": lambda: cli.reconcile(dir=tmp_path),
        "dispatch_all": lambda: cli.dispatch_all(dir=tmp_path),
        "deploy": cli.deploy,
        "rictor_deploy": cli.rictor_deploy,
    }

    with pytest.raises(SystemExit) as exc_info:
        calls[operation]()

    assert exc_info.value.code == 3
    connection.assert_not_called()
    subprocess_run.assert_not_called()
    reaper.assert_not_called()
    local_watch.assert_not_called()
    active_logs.assert_not_called()
    search_setup.assert_not_called()
    spec_reconcile.assert_not_called()
    spec_scan.assert_not_called()
    remote_deploy.assert_not_called()


def test_invalid_backend_blocks_legacy_cli_paths_before_connection(monkeypatch) -> None:
    from mtor import cli

    monkeypatch.setenv("MTOR_DURABLE_BACKEND", "dbos")
    connection = MagicMock(side_effect=AssertionError("backend connection touched"))
    monkeypatch.setattr(cli, "_legacy_get_client", connection)

    with pytest.raises(SystemExit) as exc_info:
        cli.list_cmd()

    assert exc_info.value.code == 3
    connection.assert_not_called()


@pytest.mark.parametrize("operation", ["restart", "deploy"])
def test_invalid_backend_blocks_direct_infrastructure_mutators(
    monkeypatch, operation
) -> None:
    from mtor import infra

    monkeypatch.setenv("MTOR_DURABLE_BACKEND", "dbos")
    subprocess_run = MagicMock(side_effect=AssertionError("subprocess touched"))
    monkeypatch.setattr(infra.subprocess, "run", subprocess_run)

    with pytest.raises(BackendConfigurationError, match="unsupported durable backend"):
        if operation == "restart":
            infra.restart_worker()
        else:
            infra.deploy()

    subprocess_run.assert_not_called()


def test_invalid_backend_blocks_direct_reconciliation_before_scan(monkeypatch) -> None:
    from mtor import reconcile

    monkeypatch.setenv("MTOR_DURABLE_BACKEND", "dbos")
    scan = MagicMock(side_effect=AssertionError("spec directory scanned"))
    monkeypatch.setattr(reconcile, "scan_specs", scan)

    with pytest.raises(BackendConfigurationError, match="unsupported durable backend"):
        reconcile.reconcile_all(MagicMock())

    scan.assert_not_called()


def test_invalid_backend_blocks_worker_before_recovery(monkeypatch) -> None:
    from mtor.worker import translocase

    monkeypatch.setenv("MTOR_DURABLE_BACKEND", "dbos")
    monkeypatch.setattr(translocase.sys, "argv", ["mtor-worker"])
    connect = AsyncMock(side_effect=AssertionError("Temporal connection touched"))
    worker = MagicMock(side_effect=AssertionError("Worker constructed"))
    gc = MagicMock(side_effect=AssertionError("worktree recovery touched"))
    monkeypatch.setattr(translocase.Client, "connect", connect)
    monkeypatch.setattr(translocase, "Worker", worker)
    monkeypatch.setattr(translocase, "_gc_worktrees", gc)

    with pytest.raises(BackendConfigurationError):
        asyncio.run(translocase.main())

    connect.assert_not_called()
    worker.assert_not_called()
    gc.assert_not_called()


def test_start_script_rejects_invalid_backend_before_docker(tmp_path) -> None:
    fake_bin = tmp_path / "bin"
    fake_bin.mkdir()
    docker_marker = tmp_path / "docker-called"
    fake_docker = fake_bin / "docker"
    fake_docker.write_text('#!/bin/sh\ntouch "$DOCKER_MARKER"\n')
    fake_docker.chmod(0o755)
    start_script = Path(__file__).resolve().parents[1] / "start.sh"
    env = {
        **os.environ,
        "HOME": str(tmp_path),
        "DOCKER_MARKER": str(docker_marker),
        "MTOR_DURABLE_BACKEND": "dbos",
        "PATH": f"{fake_bin}:{os.environ['PATH']}",
    }

    completed = subprocess.run(
        ["bash", str(start_script)],
        env=env,
        capture_output=True,
        text=True,
        timeout=10,
        check=False,
    )

    assert completed.returncode == 3
    assert "enables only temporal" in completed.stderr
    assert not docker_marker.exists()
