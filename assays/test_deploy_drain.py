"""Deploy drain — rictor deploy waits for in-flight ribosome tasks before restart."""

from __future__ import annotations

from unittest.mock import MagicMock, patch

from mtor.infra import _count_active_ribosomes, _wait_for_ribosome_idle


def _proc(returncode: int, stdout: str = "") -> MagicMock:
    result = MagicMock()
    result.returncode = returncode
    result.stdout = stdout
    result.stderr = ""
    return result


def test_count_active_ribosomes_parses_count():
    with patch("mtor.infra.subprocess.run", return_value=_proc(0, "2\n")):
        assert _count_active_ribosomes("test-host") == 2


def test_count_active_ribosomes_no_match_is_zero():
    with patch("mtor.infra.subprocess.run", return_value=_proc(1)):
        assert _count_active_ribosomes("test-host") == 0


def test_count_active_ribosomes_probe_failure_is_none():
    with patch("mtor.infra.subprocess.run", side_effect=OSError("ssh gone")):
        assert _count_active_ribosomes("test-host") is None


def test_count_active_ribosomes_garbage_output_is_none():
    with patch("mtor.infra.subprocess.run", return_value=_proc(0, "not-a-number")):
        assert _count_active_ribosomes("test-host") is None


def test_wait_for_ribosome_idle_polls_until_zero():
    counts = iter([2, 1, 0])
    with (
        patch(
            "mtor.infra._count_active_ribosomes", side_effect=lambda host: next(counts)
        ),
        patch("mtor.infra.time.sleep") as mock_sleep,
    ):
        result = _wait_for_ribosome_idle("test-host", timeout_seconds=600)
    assert result["idle"] is True
    assert result["active"] == 0
    assert mock_sleep.call_count == 2


def test_wait_for_ribosome_idle_times_out_without_blocking_deploy():
    with (
        patch("mtor.infra._count_active_ribosomes", return_value=3),
        patch("mtor.infra.time.sleep"),
    ):
        result = _wait_for_ribosome_idle("test-host", timeout_seconds=30)
    assert result["ok"] is True
    assert result["idle"] is False
    assert result["active"] == 3


def test_deploy_runs_drain_step_before_restart():
    from mtor.infra import HealthReport, deploy

    calls = []

    def fake_run(cmd, **kwargs):
        calls.append(cmd)
        result = MagicMock()
        result.returncode = 0
        result.stdout = ""
        result.stderr = ""
        return result

    with (
        patch(
            "mtor.infra._resolve_deploy_sha",
            return_value="dddddddd11111111222222223333333344444444",
        ),
        patch("mtor.infra.subprocess.run", side_effect=fake_run),
        patch("mtor.infra.time.sleep"),
        patch("mtor.infra._count_active_ribosomes", return_value=0),
        patch("mtor.infra.check_health") as mock_ch,
    ):
        mock_ch.return_value = HealthReport(ok=True, checks=[])
        result = deploy(worker_host="test-host", repo_dir="/fake/repo")

    step_names = [s["step"] for s in result.steps]
    assert "drain" in step_names
    assert step_names.index("drain") < step_names.index("restart")
    assert result.healthy is True
