"""Fail-closed dispatch admission for the authoritative worker service."""

from __future__ import annotations

import subprocess
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from mtor.dispatch import _dispatch_prompt
from mtor.infra import probe_worker_admission


def _completed(stdout: str = "", *, returncode: int = 0, stderr: str = ""):
    return subprocess.CompletedProcess(
        args=["ssh"],
        returncode=returncode,
        stdout=stdout,
        stderr=stderr,
    )


def _service(active: str, sub: str, pid: int) -> str:
    return f"ActiveState={active}\nSubState={sub}\nMainPID={pid}\n"


@pytest.mark.parametrize(
    ("stdout", "expected_state", "expected_ok"),
    [
        (_service("active", "running", 123), "active", True),
        (_service("deactivating", "stop-sigterm", 123), "deactivating", False),
        (_service("inactive", "dead", 0), "inactive", False),
        (_service("failed", "failed", 0), "failed", False),
        (_service("active", "exited", 0), "unknown", False),
    ],
)
def test_probe_worker_admission_classifies_systemd_state(
    stdout, expected_state, expected_ok
):
    with patch(
        "mtor.infra.subprocess.run",
        return_value=_completed(stdout),
    ) as run:
        admission = probe_worker_admission("test-worker")

    assert admission["state"] == expected_state
    assert admission["ok"] is expected_ok
    assert admission["active_state"] in stdout
    command = run.call_args.args[0]
    assert command[:2] == ["ssh", "test-worker"]
    assert "ActiveState,SubState,MainPID" in command[-1]


@pytest.mark.parametrize(
    "failure",
    [
        _completed(returncode=1, stderr="unit unavailable"),
        subprocess.TimeoutExpired(cmd="systemctl", timeout=10),
    ],
)
def test_probe_worker_admission_fails_closed_when_state_is_unavailable(failure):
    effect = failure if isinstance(failure, BaseException) else None
    result = None if effect else failure
    with patch(
        "mtor.infra.subprocess.run",
        side_effect=effect,
        return_value=result,
    ):
        admission = probe_worker_admission("test-worker")

    assert admission["ok"] is False
    assert admission["state"] == "unknown"
    assert admission["detail"]


def _admission(state: str) -> dict[str, object]:
    values = {
        "active": (True, "active", "running", 123),
        "deactivating": (False, "deactivating", "stop-sigterm", 123),
        "inactive": (False, "inactive", "dead", 0),
        "failed": (False, "failed", "failed", 0),
        "unknown": (False, "unknown", "unknown", 0),
    }
    ok, active, sub, pid = values[state]
    return {
        "ok": ok,
        "state": state,
        "active_state": active,
        "sub_state": sub,
        "main_pid": pid,
        "detail": f"worker state is {state}",
    }


@pytest.mark.parametrize(
    ("state", "expected_code"),
    [
        ("deactivating", "WORKER_DRAINING"),
        ("inactive", "WORKER_INACTIVE"),
        ("failed", "WORKER_FAILED"),
        ("unknown", "WORKER_STATE_UNKNOWN"),
    ],
)
def test_dispatch_blocks_non_accepting_worker_before_sha_gate(state, expected_code):
    with (
        patch("mtor.dispatch.probe_worker_admission", return_value=_admission(state)),
        patch("mtor.dispatch._check_worker_sha") as sha_gate,
        patch("mtor.dispatch._get_client") as get_client,
        patch("mtor.dispatch._err", return_value=1) as emit_error,
        pytest.raises(SystemExit) as exc,
    ):
        _dispatch_prompt("Improve worker admission safely")

    assert exc.value.code == 1
    assert emit_error.call_args.args[2] == expected_code
    sha_gate.assert_not_called()
    get_client.assert_not_called()


def test_dispatch_rechecks_worker_immediately_before_start_workflow():
    client = MagicMock()
    client.start_workflow = AsyncMock()
    with (
        patch(
            "mtor.dispatch.probe_worker_admission",
            side_effect=[_admission("active"), _admission("deactivating")],
        ) as admission_probe,
        patch("mtor.dispatch._check_worker_sha", return_value=True),
        patch(
            "mtor.dispatch._worker_target_repo_state",
            return_value={"ok": True},
        ),
        patch("mtor.dispatch._get_client", return_value=(client, None)),
        patch("mtor.dispatch._err", return_value=1) as emit_error,
        pytest.raises(SystemExit) as exc,
    ):
        _dispatch_prompt("Improve worker admission safely")

    assert exc.value.code == 1
    assert admission_probe.call_count == 2
    assert emit_error.call_args.args[2] == "WORKER_DRAINING"
    client.start_workflow.assert_not_called()
