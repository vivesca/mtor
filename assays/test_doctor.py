"""Tests for mtor.doctor module."""

import json
import os
import unittest
from unittest.mock import MagicMock, patch

from mtor.doctor import (
    ProbeResult,
    _probe_provider,
    format_health_display,
)


class TestCheckTemporalReachableSuccess(unittest.TestCase):
    """Test successful Temporal connection check."""

    @patch("mtor.doctor._get_client")
    def test_check_temporal_reachable_success(self, mock_get_client):
        """Test when Temporal is reachable."""
        from mtor.doctor import doctor

        mock_client = MagicMock()
        mock_get_client.return_value = (mock_client, None)

        with patch("mtor.doctor.WORKER_HOST", "test-worker"):
            with patch("mtor.doctor.TEMPORAL_HOST", "localhost:7233"):
                with patch("mtor.doctor.COACHING_PATH", None):
                    with patch("subprocess.run") as mock_subprocess:
                        mock_subprocess.return_value = MagicMock(
                            returncode=0,
                            stdout='[{"name": "zhipu", "health": "OK"}]'
                        )
                        with patch("mtor.doctor._probe_provider") as mock_probe:
                            mock_probe.return_value = ProbeResult(
                                provider="zhipu", ok=True, latency_ms=100, detail="OK"
                            )
                            with patch("sys.exit") as mock_exit:
                                with patch("sys.stdout.write"):
                                    with patch("sys.stderr.write"):
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
                            returncode=0,
                            stdout='[]'
                        )
                        with patch("mtor.doctor._probe_provider") as mock_probe:
                            mock_probe.return_value = ProbeResult(
                                provider="zhipu", ok=True, latency_ms=100, detail="OK"
                            )
                            with patch("sys.exit") as mock_exit:
                                with patch("sys.stdout.write") as mock_stdout:
                                    with patch("sys.stderr.write"):
                                        # Execute doctor
                                        doctor()
                                        # Should exit with code 3
                                        mock_exit.assert_called_once_with(3)

                                        # Check that the payload has ok=False
                                        args = mock_stdout.call_args[0][0]
                                        payload = json.loads(args.strip())
                                        self.assertFalse(payload["ok"])
                                        self.assertEqual(payload["error"]["code"], "HEALTH_CHECK_FAILED")


class TestCheckWorkerAlive(unittest.TestCase):
    """Test worker alive check."""

    @patch("mtor.doctor._get_client")
    def test_check_worker_alive(self, mock_get_client):
        """Test worker liveness probe."""
        from mtor.doctor import doctor

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
                            returncode=0,
                            stdout='[{"name": "zhipu", "health": "OK"}]'
                        )
                        with patch("mtor.doctor._probe_provider") as mock_probe:
                            mock_probe.return_value = ProbeResult(
                                provider="zhipu", ok=True, latency_ms=100, detail="OK"
                            )
                            with patch("sys.exit") as mock_exit:
                                with patch("sys.stdout.write"):
                                    with patch("sys.stderr.write"):
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
            {"name": "temporal_reachable", "ok": True, "detail": "Connected to localhost:7233"},
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
            if "temporal_reachable" in line or "worker_host" in line or "worker_alive" in line:
                self.assertIn("✔", line)


class TestFormatReportWithFailures(unittest.TestCase):
    """Test formatting report when some checks fail."""

    def test_format_report_with_failures(self):
        """Test format_health_display with failing checks."""
        checks = [
            {"name": "temporal_reachable", "ok": True, "detail": "Connected to localhost:7233"},
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


if __name__ == "__main__":
    unittest.main()
