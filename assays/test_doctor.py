"""Tests for mtor.doctor module."""

import json
import os
import time
import unittest
from unittest.mock import MagicMock, patch

import pytest

from mtor.doctor import (
    ProbeResult,
    _probe_provider,
    format_health_display,
)

integration = pytest.mark.skipif(
    not os.environ.get("MTOR_INTEGRATION"),
    reason="integration: doctor() runs real env checks (lockfile/billing over ~/code/mtor) that leak past mocks on a clean runner. Set MTOR_INTEGRATION=1 to run.",
)


class TestCheckTemporalReachableSuccess(unittest.TestCase):
    """Test successful Temporal connection check."""

    @patch("mtor.doctor._get_client")
    def test_check_temporal_reachable_success(self, mock_get_client):
        """Test when Temporal is reachable."""
        from mtor.doctor import doctor
        from mtor.infra import HealthReport

        mock_client = MagicMock()
        mock_get_client.return_value = (mock_client, None)

        with patch("mtor.doctor.WORKER_HOST", "test-worker"):
            with patch("mtor.doctor.TEMPORAL_HOST", "localhost:7233"):
                with patch("mtor.doctor.COACHING_PATH", None):
                    with patch("subprocess.run") as mock_subprocess:
                        mock_subprocess.return_value = MagicMock(
                            returncode=0, stdout='[{"name": "zhipu", "health": "OK"}]'
                        )
                        with patch("mtor.doctor._probe_provider") as mock_probe:
                            mock_probe.return_value = ProbeResult(
                                provider="zhipu", ok=True, latency_ms=100, detail="OK"
                            )
                            with patch("sys.exit") as mock_exit:
                                with patch("sys.stdout.write"):
                                    with patch("sys.stderr.write"):
                                        with patch(
                                            "mtor.doctor._check_coding_plan_lane",
                                            return_value={
                                                "name": "coding_plan_lane",
                                                "ok": True,
                                                "detail": "ok",
                                            },
                                        ):
                                            with patch(
                                                "mtor.doctor._check_opencode_config_file",
                                                return_value={
                                                    "name": "opencode_config_local",
                                                    "ok": True,
                                                    "detail": "ok",
                                                },
                                            ):
                                                with patch(
                                                    "mtor.doctor._check_worker_opencode_config",
                                                    return_value={
                                                        "name": "opencode_config_worker",
                                                        "ok": True,
                                                        "detail": "ok",
                                                    },
                                                ):
                                                    with patch(
                                                        "mtor.doctor._get_provider_module",
                                                        return_value=None,
                                                    ):
                                                        with patch(
                                                            "mtor.infra.check_health",
                                                            return_value=HealthReport(
                                                                ok=True, checks=[]
                                                            ),
                                                        ):
                                                            # Execute doctor
                                                            doctor()
                                                            # Should not exit with error if all checks pass
                                                            mock_exit.assert_not_called()

        # Verify client was requested
        mock_get_client.assert_called_once()


class TestCheckTemporalUnreachable(unittest.TestCase):
    """Test when Temporal is unreachable."""

    @patch("mtor.doctor._get_client")
    def test_check_temporal_unreachable(self, mock_get_client):
        """Test when Temporal cannot be reached."""
        from mtor.doctor import doctor

        mock_get_client.return_value = (None, ConnectionError("Connection refused"))

        with patch("mtor.doctor.WORKER_HOST", "test-worker"):
            with patch("mtor.doctor.TEMPORAL_HOST", "localhost:7233"):
                with patch("mtor.doctor.COACHING_PATH", None):
                    with patch("subprocess.run") as mock_subprocess:
                        mock_subprocess.return_value = MagicMock(
                            returncode=0, stdout="[]"
                        )
                        with patch("mtor.doctor._probe_provider") as mock_probe:
                            mock_probe.return_value = ProbeResult(
                                provider="zhipu", ok=True, latency_ms=100, detail="OK"
                            )
                            with patch("sys.exit") as mock_exit:
                                with patch("sys.stdout.write") as mock_stdout:
                                    with patch("sys.stderr.write"):
                                        with patch(
                                            "mtor.doctor._check_opencode_config_file",
                                            return_value={
                                                "name": "opencode_config_local",
                                                "ok": True,
                                                "detail": "ok",
                                            },
                                        ):
                                            with patch(
                                                "mtor.doctor._check_worker_opencode_config",
                                                return_value={
                                                    "name": "opencode_config_worker",
                                                    "ok": True,
                                                    "detail": "ok",
                                                },
                                            ):
                                                # Execute doctor
                                                doctor()
                                                # Should exit with code 3
                                                mock_exit.assert_called_once_with(3)

                                                # Check that the payload has ok=False
                                                args = mock_stdout.call_args[0][0]
                                                payload = json.loads(args.strip())
                                                self.assertFalse(payload["ok"])
                                                self.assertEqual(
                                                    payload["error"]["code"],
                                                    "HEALTH_CHECK_FAILED",
                                                )


class TestCheckWorkerAlive(unittest.TestCase):
    """Test worker alive check."""

    @patch("mtor.doctor._get_client")
    def test_check_worker_alive(self, mock_get_client):
        """Test worker liveness probe."""
        from mtor.doctor import doctor
        from mtor.infra import HealthReport

        # Create mock client that works with async iteration
        mock_client = MagicMock()

        # Mock async list_workflows
        async def mock_list():
            yield 1

        mock_client.list_workflows = mock_list

        mock_get_client.return_value = (mock_client, None)

        with patch("mtor.doctor.WORKER_HOST", "test-worker"):
            with patch("mtor.doctor.TEMPORAL_HOST", "localhost:7233"):
                with patch("mtor.doctor.COACHING_PATH", None):
                    with patch("subprocess.run") as mock_subprocess:
                        mock_subprocess.return_value = MagicMock(
                            returncode=0, stdout='[{"name": "zhipu", "health": "OK"}]'
                        )
                        with patch("mtor.doctor._probe_provider") as mock_probe:
                            mock_probe.return_value = ProbeResult(
                                provider="zhipu", ok=True, latency_ms=100, detail="OK"
                            )
                            with patch("sys.exit") as mock_exit:
                                with patch("sys.stdout.write"):
                                    with patch("sys.stderr.write"):
                                        with patch(
                                            "mtor.doctor._check_coding_plan_lane",
                                            return_value={
                                                "name": "coding_plan_lane",
                                                "ok": True,
                                                "detail": "ok",
                                            },
                                        ):
                                            with patch(
                                                "mtor.doctor._check_opencode_config_file",
                                                return_value={
                                                    "name": "opencode_config_local",
                                                    "ok": True,
                                                    "detail": "ok",
                                                },
                                            ):
                                                with patch(
                                                    "mtor.doctor._check_worker_opencode_config",
                                                    return_value={
                                                        "name": "opencode_config_worker",
                                                        "ok": True,
                                                        "detail": "ok",
                                                    },
                                                ):
                                                    with patch(
                                                        "mtor.doctor._get_provider_module",
                                                        return_value=None,
                                                    ):
                                                        with patch(
                                                            "mtor.infra.check_health",
                                                            return_value=HealthReport(
                                                                ok=True, checks=[]
                                                            ),
                                                        ):
                                                            doctor()
                                                            # Should work fine if all pass
                                                            mock_exit.assert_not_called()


class TestCheckProviderApiProbe(unittest.TestCase):
    """Test provider API probing."""

    def test_check_provider_api_probe_no_api_key(self):
        """Test when API key is not set."""
        # Clear any existing env vars
        original_env = os.environ.pop("ZHIPU_API_KEY", None)

        try:
            result = _probe_provider("zhipu")
            self.assertIsInstance(result, ProbeResult)
            self.assertEqual(result.provider, "zhipu")
            self.assertFalse(result.ok)
            self.assertIsNone(result.latency_ms)
            self.assertEqual(result.detail, "ZHIPU_API_KEY not set")
        finally:
            if original_env:
                os.environ["ZHIPU_API_KEY"] = original_env

    @patch("urllib.request.urlopen")
    def test_check_provider_api_probe_success(self, mock_urlopen):
        """Test successful probe."""
        os.environ["ZHIPU_API_KEY"] = "test-key"

        mock_response = MagicMock()
        mock_response.read.return_value = b'{"id": "test"}'
        mock_urlopen.return_value.__enter__.return_value = mock_response

        result = _probe_provider("zhipu")
        self.assertIsInstance(result, ProbeResult)
        self.assertTrue(result.ok)
        self.assertIsNotNone(result.latency_ms)
        self.assertIn("OK", result.detail)

        del os.environ["ZHIPU_API_KEY"]


class TestFormatReportAllPass(unittest.TestCase):
    """Test formatting report when all checks pass."""

    def test_format_report_all_pass(self):
        """Test format_health_display with all checks passing."""
        checks = [
            {
                "name": "temporal_reachable",
                "ok": True,
                "detail": "Connected to localhost:7233",
            },
            {"name": "worker_host", "ok": True, "detail": "MTOR_WORKER_HOST=worker"},
            {"name": "worker_alive", "ok": True, "detail": "Worker service responsive"},
        ]

        output = format_health_display(checks)

        # Check output structure
        self.assertIn("mtor doctor — health report", output)
        self.assertIn("ALL CHECKS PASSED", output)
        self.assertIn("✔ temporal_reachable: Connected to localhost:7233", output)
        self.assertNotIn("SOME CHECKS FAILED", output)

        # Verify all checks have pass mark
        for line in output.splitlines():
            if (
                "temporal_reachable" in line
                or "worker_host" in line
                or "worker_alive" in line
            ):
                self.assertIn("✔", line)

    def test_provider_routing_displays_glm52(self):
        """Doctor display makes the primary GLM-5.2/Zhipu route visible."""
        checks = [
            {
                "name": "provider_routing",
                "ok": True,
                "detail": "priority: zhipu(glm-5.2, limit=3)",
            },
        ]

        output = format_health_display(checks)

        self.assertIn("provider_routing", output)
        self.assertIn("zhipu(glm-5.2", output)


class TestFormatReportWithFailures(unittest.TestCase):
    """Test formatting report when some checks fail."""

    def test_format_report_with_failures(self):
        """Test format_health_display with failing checks."""
        checks = [
            {
                "name": "temporal_reachable",
                "ok": True,
                "detail": "Connected to localhost:7233",
            },
            {"name": "worker_host", "ok": False, "detail": "MTOR_WORKER_HOST not set"},
            {"name": "worker_alive", "ok": False, "detail": "Worker probe failed"},
        ]

        output = format_health_display(checks)

        # Check output contains failure indicators
        self.assertIn("mtor doctor — health report", output)
        self.assertIn("SOME CHECKS FAILED", output)
        self.assertIn("✘ worker_host: MTOR_WORKER_HOST not set", output)
        self.assertIn("✘ worker_alive: Worker probe failed", output)
        self.assertIn("✔ temporal_reachable", output)
        self.assertNotIn("ALL CHECKS PASSED", output)


class TestReconcileRunningWorkflows(unittest.TestCase):
    """Test reconcile_running_workflows classification logic."""

    def _make_fake_workflow(self, wf_id: str):
        wf = MagicMock()
        wf.id = wf_id
        return wf

    def _mock_client(self, workflow_ids: list[str]) -> MagicMock:
        client = MagicMock()
        workflows = [self._make_fake_workflow(wid) for wid in workflow_ids]

        async def _list_running(**_kwargs):
            for wf in workflows:
                yield wf

        client.list_workflows = _list_running
        return client

    def test_reconcile_classifications(self):
        """Three RUNNING workflows: alive, stale, abandoned."""
        now = time.time()
        fresh_mtime = str(now - 10)
        stale_mtime = str(now - 500)

        ssh_outputs = {
            "wf-alive-123": fresh_mtime,
            "wf-stale-456": stale_mtime,
            "wf-abandoned-789": "MISSING",
        }

        def _fake_run(cmd, **_kwargs):
            result = MagicMock()
            result.returncode = 0
            cmd_str = " ".join(cmd) if isinstance(cmd, list) else cmd
            for wf_id, output in ssh_outputs.items():
                if wf_id in cmd_str:
                    result.stdout = output
                    return result
            result.stdout = "MISSING"
            return result

        from mtor.doctor import reconcile_running_workflows, HEARTBEAT_STALE_THRESHOLD

        client = self._mock_client(["wf-alive-123", "wf-stale-456", "wf-abandoned-789"])

        with patch("mtor.doctor.subprocess.run", side_effect=_fake_run):
            with patch("mtor.doctor.WORKER_HOST", "ganglion"):
                classifications = reconcile_running_workflows(client)

        by_id = {c["workflow_id"]: c for c in classifications}

        # alive: heartbeat within threshold
        alive = by_id["wf-alive-123"]
        self.assertEqual(alive["classification"], "alive")
        self.assertLessEqual(alive["last_heartbeat_age_s"], HEARTBEAT_STALE_THRESHOLD)
        self.assertNotIn("next_action", alive)

        # stale: heartbeat exists but too old
        stale = by_id["wf-stale-456"]
        self.assertEqual(stale["classification"], "stale")
        self.assertGreater(stale["last_heartbeat_age_s"], HEARTBEAT_STALE_THRESHOLD)
        self.assertEqual(stale["next_action"], "mtor cancel wf-stale-456")

        # abandoned: no heartbeat file
        abandoned = by_id["wf-abandoned-789"]
        self.assertEqual(abandoned["classification"], "abandoned")
        self.assertNotIn("last_heartbeat_age_s", abandoned)
        self.assertEqual(abandoned["next_action"], "mtor cancel wf-abandoned-789")


if __name__ == "__main__":
    unittest.main()


def test_doctor_fails_when_rictor_topology_fails():
    from mtor.doctor import doctor
    from mtor.infra import HealthReport

    class Client:
        async def list_workflows(self):
            yield object()

    with (
        patch("mtor.doctor._get_client", return_value=(Client(), None)),
        patch("mtor.doctor.COACHING_PATH", None),
        patch("mtor.doctor.WORKER_HOST", "ganglion"),
        patch(
            "mtor.doctor._check_coding_plan_lane",
            return_value={"name": "coding_plan_lane", "ok": True, "detail": "ok"},
        ),
        patch("mtor.doctor._get_provider_module", return_value=None),
        patch(
            "mtor.infra.check_health",
            return_value=HealthReport(
                ok=False,
                checks=[
                    {
                        "name": "worker_process_singleton",
                        "ok": False,
                        "detail": "duplicate",
                    }
                ],
            ),
        ),
        patch(
            "subprocess.run", return_value=MagicMock(returncode=0, stdout="", stderr="")
        ),
        patch("sys.stderr.write"),
        patch("sys.stdout.write") as stdout_write,
        pytest.raises(SystemExit),
    ):
        doctor()

    payload = json.loads(stdout_write.call_args[0][0])
    assert payload["ok"] is False
    rictor = next(
        c for c in payload["result"]["checks"] if c["name"] == "rictor_topology"
    )
    assert rictor["ok"] is False


def test_doctor_env_enables_opencode_runtime_probe():
    from mtor.doctor import doctor
    from mtor.infra import HealthReport

    class Client:
        async def list_workflows(self):
            yield object()

    with (
        patch.dict("os.environ", {"MTOR_PROBE_OPENCODE": "1"}),
        patch("mtor.doctor._get_client", return_value=(Client(), None)),
        patch("mtor.doctor.COACHING_PATH", None),
        patch("mtor.doctor.WORKER_HOST", "ganglion"),
        patch(
            "mtor.doctor._check_coding_plan_lane",
            return_value={"name": "coding_plan_lane", "ok": True, "detail": "ok"},
        ),
        patch(
            "mtor.doctor._check_opencode_config_file",
            return_value={"name": "opencode_config_local", "ok": True, "detail": "ok"},
        ),
        patch(
            "mtor.doctor._check_worker_opencode_config",
            return_value={
                "name": "opencode_config_worker",
                "ok": True,
                "detail": "ok",
            },
        ),
        patch(
            "mtor.doctor._check_worker_opencode_runtime",
            return_value={
                "name": "opencode_runtime_probe",
                "ok": True,
                "detail": "coding-plan-ok",
            },
        ) as runtime_probe,
        patch("mtor.doctor._get_provider_module", return_value=None),
        patch("mtor.infra.check_health", return_value=HealthReport(ok=True, checks=[])),
        patch("sys.stderr.write"),
        patch("sys.stdout.write"),
    ):
        doctor()

    runtime_probe.assert_called_once_with()
