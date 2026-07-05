"""Drain-aware worker restart callers, retry policy v2, failure-cause flags."""

from __future__ import annotations

import subprocess

from mtor.infra import restart_worker
from mtor.worker.workflow import (
    _RETRY_POLICY,
    _failure_cause_flag,
    _translate_retry_policy,
)


def _fake_completed(stdout: str = "", stderr: str = "", returncode: int = 0):
    class _Result:
        def __init__(self):
            self.stdout = stdout
            self.stderr = stderr
            self.returncode = returncode

    return _Result()


def _make_fake_run(
    busy_count: str = "0\n",
    restart_kwargs: dict | None = None,
    restart_raises: Exception | None = None,
    calls: list | None = None,
):
    """Build a fake subprocess.run that handles pgrep + systemctl restart."""
    restart_kwargs = restart_kwargs if restart_kwargs is not None else {}
    calls = calls if calls is not None else []

    def fake_run(cmd, **kwargs):
        calls.append({"cmd": list(cmd), "kwargs": dict(kwargs)})
        joined = " ".join(cmd)
        if "pgrep" in joined:
            return _fake_completed(stdout=busy_count, returncode=0)
        if "systemctl --user restart" in joined:
            if restart_raises is not None:
                raise restart_raises
            return _fake_completed(stdout="", stderr="", returncode=0)
        return _fake_completed()

    return fake_run


def test_drainsafe_restart_worker_uses_long_timeout(monkeypatch):
    calls: list = []
    monkeypatch.setattr(
        "mtor.infra.subprocess.run",
        _make_fake_run(calls=calls),
    )

    restart_worker("testhost")

    restart_calls = [
        c for c in calls if "systemctl --user restart" in " ".join(c["cmd"])
    ]
    assert restart_calls, "expected a systemctl restart call"
    assert restart_calls[0]["kwargs"].get("timeout", 0) >= 600
    assert "systemctl --user restart mtor-worker" in " ".join(restart_calls[0]["cmd"])


def test_drainsafe_restart_worker_timeout_raises_runtimeerror(monkeypatch):
    monkeypatch.setattr(
        "mtor.infra.subprocess.run",
        _make_fake_run(
            restart_raises=subprocess.TimeoutExpired(cmd="ssh", timeout=700),
        ),
    )

    raised = False
    msg = ""
    try:
        restart_worker("testhost")
    except RuntimeError as exc:
        raised = True
        msg = str(exc)
    assert raised, "expected RuntimeError on restart timeout"
    assert "drain" in msg.lower(), f"expected 'drain' in message, got: {msg}"


def test_drainsafe_restart_worker_busy_notice(monkeypatch, capsys):
    calls: list = []
    monkeypatch.setattr(
        "mtor.infra.subprocess.run",
        _make_fake_run(busy_count="2\n", calls=calls),
    )

    restart_worker("testhost")

    captured = capsys.readouterr()
    assert "in flight" in captured.err, (
        f"expected busy notice on stderr, got: {captured.err!r}"
    )
    restart_calls = [
        c for c in calls if "systemctl --user restart" in " ".join(c["cmd"])
    ]
    assert restart_calls, (
        "expected the restart command to still run despite the busy notice"
    )


def test_drainsafe_retry_policy_v2_mutating():
    for mode in ("build", "raw"):
        policy = _translate_retry_policy(mode, use_v2=True)
        assert policy.maximum_attempts == 3, (
            f"mode {mode!r}: expected 3 attempts for v2 mutating, got {policy.maximum_attempts}"
        )


def test_drainsafe_retry_policy_v2_readonly():
    for mode in ("scout", "research"):
        policy = _translate_retry_policy(mode, use_v2=True)
        assert policy.maximum_attempts == 4, (
            f"mode {mode!r}: expected 4 attempts for v2 readonly, got {policy.maximum_attempts}"
        )


def test_drainsafe_retry_policy_gate_off_legacy():
    for mode in ("build", "raw", "scout", "research"):
        policy = _translate_retry_policy(mode, use_v2=False)
        assert policy is _RETRY_POLICY, (
            f"mode {mode!r}: expected _RETRY_POLICY with gate off"
        )
        assert policy.maximum_attempts == 2, (
            f"mode {mode!r}: expected legacy 2 attempts with gate off"
        )


def test_drainsafe_failure_cause_flag_classification():
    from temporalio.api.enums.v1 import TimeoutType
    from temporalio.exceptions import (
        CancelledError as TemporalCancelledError,
        TimeoutError as TemporalTimeoutError,
    )

    timeout_exc = TemporalTimeoutError(
        "heartbeat timeout",
        last_heartbeat_details=[],
        type=TimeoutType.TIMEOUT_TYPE_HEARTBEAT,
    )
    assert _failure_cause_flag(timeout_exc) == "cause_timeout_heartbeat"

    cancelled_exc = TemporalCancelledError("cancelled")
    assert _failure_cause_flag(cancelled_exc) == "cause_cancelled"

    assert _failure_cause_flag(None) == ""
    assert _failure_cause_flag(ValueError("plain")) == ""
