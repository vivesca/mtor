"""Tests for mtor.infra (rictor CLI) — check_health, deploy, clean."""

from __future__ import annotations

import io
import json
import sys
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

from mtor.cli import app


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


SERVICE_OK = (
    "__MTOR_WORKER__\n"
    "ActiveState=active\nSubState=running\nMainPID=123\n"
    "__TEMPORAL_WORKER_USER__\n"
    "ActiveState=inactive\nSubState=dead\nMainPID=0\n"
    "__TEMPORAL_WORKER_SYSTEM__\n"
    "ActiveState=inactive\nSubState=dead\nMainPID=0\n"
)
SERVICE_MTOR_INACTIVE = (
    "__MTOR_WORKER__\n"
    "ActiveState=failed\nSubState=failed\nMainPID=0\n"
    "__TEMPORAL_WORKER_USER__\n"
    "ActiveState=inactive\nSubState=dead\nMainPID=0\n"
    "__TEMPORAL_WORKER_SYSTEM__\n"
    "ActiveState=inactive\nSubState=dead\nMainPID=0\n"
)
SERVICE_TEMPORAL_ACTIVE = (
    "__MTOR_WORKER__\n"
    "ActiveState=active\nSubState=running\nMainPID=123\n"
    "__TEMPORAL_WORKER_USER__\n"
    "ActiveState=active\nSubState=running\nMainPID=456\n"
    "__TEMPORAL_WORKER_SYSTEM__\n"
    "ActiveState=inactive\nSubState=dead\nMainPID=0\n"
)
SERVICE_SYSTEM_TEMPORAL_ACTIVE = (
    "__MTOR_WORKER__\n"
    "ActiveState=active\nSubState=running\nMainPID=123\n"
    "__TEMPORAL_WORKER_USER__\n"
    "ActiveState=inactive\nSubState=dead\nMainPID=0\n"
    "__TEMPORAL_WORKER_SYSTEM__\n"
    "ActiveState=active\nSubState=running\nMainPID=456\n"
)
SERVICE_SYSTEM_MTOR_ACTIVE = (
    "__MTOR_WORKER__\n"
    "ActiveState=active\nSubState=running\nMainPID=123\n"
    "__TEMPORAL_WORKER_USER__\n"
    "ActiveState=inactive\nSubState=dead\nMainPID=0\n"
    "__TEMPORAL_WORKER_SYSTEM__\n"
    "ActiveState=inactive\nSubState=dead\nMainPID=0\n"
    "__MTOR_WORKER_SYSTEM__\n"
    "ActiveState=active\nSubState=running\nMainPID=789\n"
)
SERVICE_SYSTEM_MTOR_ABSENT = (
    "__MTOR_WORKER__\n"
    "ActiveState=active\nSubState=running\nMainPID=123\n"
    "__TEMPORAL_WORKER_USER__\n"
    "ActiveState=inactive\nSubState=dead\nMainPID=0\n"
    "__TEMPORAL_WORKER_SYSTEM__\n"
    "ActiveState=inactive\nSubState=dead\nMainPID=0\n"
    "__MTOR_WORKER_SYSTEM__\n"
)
WORKER_ROOT = "123 77 op run --env-file /home/vivesca/germline/loci/env.op -- python3 -m mtor.worker\n"
DEPLOY_SHA = "dddddddd11111111222222223333333344444444"


def invoke(args: list[str] | None = None) -> tuple[int, dict]:
    """Invoke CLI and return (exit_code, parsed_json)."""
    captured = io.StringIO()
    old_stdout = sys.stdout
    exit_code = 0
    try:
        sys.stdout = captured
        app(args or [])
    except SystemExit as exc:
        exit_code = exc.code if isinstance(exc.code, int) else 1
    finally:
        sys.stdout = old_stdout

    output = captured.getvalue()
    try:
        data = json.loads(output)
    except json.JSONDecodeError as exc:
        raise AssertionError(
            f"Output is not valid JSON. Exit={exit_code}\nOutput: {output!r}\nException: {exc}"
        ) from exc
    return exit_code, data


# ---------------------------------------------------------------------------
# check_health tests
# ---------------------------------------------------------------------------


class TestCheckHealth:
    """Tests for infra check_health function and CLI command."""

    def test_test_infra_check_reports_health(self):
        """check_health returns a report with ok boolean and checks list."""
        from mtor.infra import check_health

        report = check_health(
            worker_host="localhost",
            repo_dir=str(Path(__file__).resolve().parent.parent),
        )
        d = report.to_dict()
        assert "ok" in d
        assert isinstance(d["ok"], bool)
        assert "checks" in d
        assert isinstance(d["checks"], list)
        assert len(d["checks"]) > 0
        # Each check has name, ok, detail
        for check in d["checks"]:
            assert "name" in check
            assert "ok" in check
            assert "detail" in check

    def test_check_health_cli_command(self):
        """mtor rictor check returns valid JSON envelope."""
        with patch("mtor.cli._check_health") as mock_ch:
            from mtor.infra import HealthReport

            mock_ch.return_value = HealthReport(
                ok=True,
                checks=[{"name": "test", "ok": True, "detail": "all good"}],
            )
            exit_code, data = invoke(["rictor", "check"])
        assert exit_code == 0
        assert data["ok"] is True
        assert "checks" in data["result"]

    def test_check_health_detects_missing_repo(self, tmp_path):
        """check_health reports failure for nonexistent repo."""
        from mtor.infra import check_health

        report = check_health(
            worker_host="localhost",
            repo_dir=str(tmp_path / "nonexistent"),
        )
        assert report.ok is False
        repo_check = next(c for c in report.checks if c["name"] == "repo_dir")
        assert repo_check["ok"] is False

    def test_check_health_git_failure_makes_report_fail(self, tmp_path):
        """git status failure is reflected in aggregate health."""
        from mtor.infra import check_health

        tmp_path.mkdir(exist_ok=True)

        def fake_run(cmd, **kwargs):
            result = MagicMock()
            result.returncode = 0
            result.stdout = ""
            result.stderr = ""
            if cmd[:2] == ["git", "status"]:
                result.returncode = 128
                result.stderr = "not a git repository"
            return result

        with patch("mtor.infra.subprocess.run", side_effect=fake_run):
            report = check_health(worker_host="localhost", repo_dir=str(tmp_path))

        git_check = next(c for c in report.checks if c["name"] == "git_clean")
        assert report.ok is False
        assert git_check["ok"] is False

    def test_check_health_ssh_unreachable(self):
        """check_health reports SSH failure for unreachable host."""
        from mtor.infra import check_health

        with patch("mtor.infra.subprocess.run") as mock_run:
            mock_run.side_effect = OSError("connection refused")
            report = check_health(worker_host="unreachable-host")
        ssh_check = next(c for c in report.checks if c["name"] == "worker_ssh")
        assert ssh_check["ok"] is False

    def test_check_health_localhost_skips_ssh(self):
        """check_health skips SSH checks for localhost."""
        from mtor.infra import check_health

        report = check_health(
            worker_host="localhost",
            repo_dir=str(Path(__file__).resolve().parent.parent),
        )
        ssh_check = next(c for c in report.checks if c["name"] == "worker_ssh")
        assert "Skipped" in str(ssh_check["detail"])

    def test_split_marked_sections_handles_temporal_worker_marker(self):
        """Marked output splits mtor-worker and temporal-worker sections."""
        from mtor.infra import _split_marked_sections

        sections = _split_marked_sections(SERVICE_OK)

        assert sections == {
            "mtor_worker": "ActiveState=active\nSubState=running\nMainPID=123",
            "temporal_worker_user": "ActiveState=inactive\nSubState=dead\nMainPID=0",
            "temporal_worker_system": "ActiveState=inactive\nSubState=dead\nMainPID=0",
        }

    def test_check_health_detects_single_worker_service(self, tmp_path):
        """Remote health passes with mtor-worker active and temporal-worker inactive."""
        from mtor.infra import check_health

        (tmp_path / ".git").mkdir()

        def fake_run(cmd, **kwargs):
            result = MagicMock()
            result.returncode = 0
            result.stderr = ""
            joined = " ".join(cmd)
            if "echo ok" in joined:
                result.stdout = "ok\n"
            elif "df -h" in joined:
                result.stdout = "42%\n"
            elif "systemctl --user show mtor-worker.service" in joined:
                result.stdout = SERVICE_OK
            elif "ps -eo" in joined:
                result.stdout = WORKER_ROOT
            elif cmd[:2] == ["git", "status"]:
                result.stdout = ""
            else:
                result.stdout = ""
            return result

        with patch("mtor.infra.subprocess.run", side_effect=fake_run):
            report = check_health(worker_host="ganglion", repo_dir=str(tmp_path))

        service_check = next(
            c for c in report.checks if c["name"] == "worker_service_singleton"
        )
        process_check = next(
            c for c in report.checks if c["name"] == "worker_process_singleton"
        )
        assert service_check["ok"] is True
        assert process_check["ok"] is True

    def test_check_health_fails_when_mtor_worker_inactive(self, tmp_path):
        """Remote health fails when the authoritative worker service is inactive."""
        from mtor.infra import check_health

        (tmp_path / ".git").mkdir()

        def fake_run(cmd, **kwargs):
            result = MagicMock()
            result.returncode = 0
            result.stderr = ""
            joined = " ".join(cmd)
            if "echo ok" in joined:
                result.stdout = "ok\n"
            elif "df -h" in joined:
                result.stdout = "42%\n"
            elif "systemctl --user show mtor-worker.service" in joined:
                result.stdout = SERVICE_MTOR_INACTIVE
            elif "ps -eo" in joined:
                result.stdout = WORKER_ROOT
            else:
                result.stdout = ""
            return result

        with patch("mtor.infra.subprocess.run", side_effect=fake_run):
            report = check_health(worker_host="ganglion", repo_dir=str(tmp_path))

        service_check = next(
            c for c in report.checks if c["name"] == "worker_service_singleton"
        )
        assert report.ok is False
        assert service_check["ok"] is False
        assert "mtor-worker.service" in str(service_check["detail"])

    def test_check_health_fails_when_temporal_worker_active(self, tmp_path):
        """Remote health fails when the obsolete temporal-worker unit is active."""
        from mtor.infra import check_health

        (tmp_path / ".git").mkdir()

        def fake_run(cmd, **kwargs):
            result = MagicMock()
            result.returncode = 0
            result.stderr = ""
            joined = " ".join(cmd)
            if "echo ok" in joined:
                result.stdout = "ok\n"
            elif "df -h" in joined:
                result.stdout = "42%\n"
            elif "systemctl --user show mtor-worker.service" in joined:
                result.stdout = SERVICE_TEMPORAL_ACTIVE
            elif "ps -eo" in joined:
                result.stdout = WORKER_ROOT
            else:
                result.stdout = ""
            return result

        with patch("mtor.infra.subprocess.run", side_effect=fake_run):
            report = check_health(worker_host="ganglion", repo_dir=str(tmp_path))

        service_check = next(
            c for c in report.checks if c["name"] == "worker_service_singleton"
        )
        assert report.ok is False
        assert service_check["ok"] is False
        assert "temporal-worker.service" in str(service_check["detail"])

    def test_check_health_fails_when_system_temporal_worker_active(self, tmp_path):
        """Remote health fails when the obsolete system temporal-worker unit is active."""
        from mtor.infra import check_health

        (tmp_path / ".git").mkdir()

        def fake_run(cmd, **kwargs):
            result = MagicMock()
            result.returncode = 0
            result.stderr = ""
            joined = " ".join(cmd)
            if "echo ok" in joined:
                result.stdout = "ok\n"
            elif "df -h" in joined:
                result.stdout = "42%\n"
            elif "systemctl --user show mtor-worker.service" in joined:
                result.stdout = SERVICE_SYSTEM_TEMPORAL_ACTIVE
            elif "ps -eo" in joined:
                result.stdout = WORKER_ROOT
            else:
                result.stdout = ""
            return result

        with patch("mtor.infra.subprocess.run", side_effect=fake_run):
            report = check_health(worker_host="ganglion", repo_dir=str(tmp_path))

        service_check = next(
            c for c in report.checks if c["name"] == "worker_service_singleton"
        )
        assert report.ok is False
        assert service_check["ok"] is False
        assert "temporal_system" in str(service_check["detail"])

    def test_check_health_fails_when_system_scope_mtor_worker_active(self, tmp_path):
        """Remote health fails when a duplicate mtor-worker runs in system scope."""
        from mtor.infra import check_health

        (tmp_path / ".git").mkdir()

        def fake_run(cmd, **kwargs):
            result = MagicMock()
            result.returncode = 0
            result.stderr = ""
            joined = " ".join(cmd)
            if "echo ok" in joined:
                result.stdout = "ok\n"
            elif "df -h" in joined:
                result.stdout = "42%\n"
            elif "systemctl --user show mtor-worker.service" in joined:
                result.stdout = SERVICE_SYSTEM_MTOR_ACTIVE
            elif "ps -eo" in joined:
                result.stdout = WORKER_ROOT
            else:
                result.stdout = ""
            return result

        with patch("mtor.infra.subprocess.run", side_effect=fake_run):
            report = check_health(worker_host="ganglion", repo_dir=str(tmp_path))

        service_check = next(
            c for c in report.checks if c["name"] == "worker_service_singleton"
        )
        assert report.ok is False
        assert service_check["ok"] is False
        assert "mtor_system" in str(service_check["detail"])

    def test_check_health_passes_when_system_scope_mtor_worker_absent(self, tmp_path):
        """Remote health passes when no system-scope mtor-worker is present."""
        from mtor.infra import check_health

        (tmp_path / ".git").mkdir()

        def fake_run(cmd, **kwargs):
            result = MagicMock()
            result.returncode = 0
            result.stderr = ""
            joined = " ".join(cmd)
            if "echo ok" in joined:
                result.stdout = "ok\n"
            elif "df -h" in joined:
                result.stdout = "42%\n"
            elif "systemctl --user show mtor-worker.service" in joined:
                result.stdout = SERVICE_SYSTEM_MTOR_ABSENT
            elif "ps -eo" in joined:
                result.stdout = WORKER_ROOT
            elif cmd[:2] == ["git", "status"]:
                result.stdout = ""
            else:
                result.stdout = ""
            return result

        with patch("mtor.infra.subprocess.run", side_effect=fake_run):
            report = check_health(worker_host="ganglion", repo_dir=str(tmp_path))

        service_check = next(
            c for c in report.checks if c["name"] == "worker_service_singleton"
        )
        assert service_check["ok"] is True

    def test_check_health_fails_on_duplicate_worker_roots(self, tmp_path):
        """Remote health fails when more than one mtor.worker root is active."""
        from mtor.infra import check_health

        (tmp_path / ".git").mkdir()

        def fake_run(cmd, **kwargs):
            result = MagicMock()
            result.returncode = 0
            result.stderr = ""
            joined = " ".join(cmd)
            if "echo ok" in joined:
                result.stdout = "ok\n"
            elif "df -h" in joined:
                result.stdout = "42%\n"
            elif "systemctl --user show mtor-worker.service" in joined:
                result.stdout = SERVICE_OK
            elif "ps -eo" in joined:
                result.stdout = WORKER_ROOT + (
                    "124 77 op run --env-file /home/vivesca/germline/loci/env.op -- python3 -m mtor.worker\n"
                )
            elif cmd[:2] == ["git", "status"]:
                result.stdout = ""
            else:
                result.stdout = ""
            return result

        with patch("mtor.infra.subprocess.run", side_effect=fake_run):
            report = check_health(worker_host="ganglion", repo_dir=str(tmp_path))

        process_check = next(
            c for c in report.checks if c["name"] == "worker_process_singleton"
        )
        assert report.ok is False
        assert process_check["ok"] is False
        assert "found 2" in str(process_check["detail"])

    def test_check_health_fails_when_worker_root_orphaned(self, tmp_path):
        """Remote health fails when the only worker root is orphaned under PID 1."""
        from mtor.infra import check_health

        (tmp_path / ".git").mkdir()

        def fake_run(cmd, **kwargs):
            result = MagicMock()
            result.returncode = 0
            result.stderr = ""
            joined = " ".join(cmd)
            if "echo ok" in joined:
                result.stdout = "ok\n"
            elif "df -h" in joined:
                result.stdout = "42%\n"
            elif "systemctl --user show mtor-worker.service" in joined:
                result.stdout = SERVICE_OK
            elif "ps -eo" in joined:
                result.stdout = "123 1 op run --env-file /home/vivesca/germline/loci/env.op -- python3 -m mtor.worker\n"
            else:
                result.stdout = ""
            return result

        with patch("mtor.infra.subprocess.run", side_effect=fake_run):
            report = check_health(worker_host="ganglion", repo_dir=str(tmp_path))

        process_check = next(
            c for c in report.checks if c["name"] == "worker_process_singleton"
        )
        assert report.ok is False
        assert process_check["ok"] is False
        assert "orphaned" in str(process_check["detail"])

    def test_check_health_reports_worker_head_match(self, tmp_path):
        """Remote health includes a passing worker_repo_head check."""
        from mtor.infra import check_health

        (tmp_path / ".git").mkdir()
        sha = "aaaaaaaa11111111222222223333333344444444"

        def fake_run(cmd, **kwargs):
            result = MagicMock()
            result.returncode = 0
            result.stderr = ""
            joined = " ".join(cmd)
            if cmd[:3] == ["git", "rev-parse", "HEAD"]:
                result.stdout = f"{sha}\n"
            elif "git rev-parse HEAD" in joined:
                result.stdout = f"{sha}\n"
            elif "echo ok" in joined:
                result.stdout = "ok\n"
            elif "df -h" in joined:
                result.stdout = "42%\n"
            elif "systemctl --user show mtor-worker.service" in joined:
                result.stdout = SERVICE_OK
            elif "ps -eo" in joined:
                result.stdout = WORKER_ROOT
            else:
                result.stdout = ""
            return result

        with patch("mtor.infra.subprocess.run", side_effect=fake_run):
            report = check_health(
                worker_host="remote-test-host",
                repo_dir=str(tmp_path),
                remote_repo_dir="/home/vivesca/code/mtor",
            )

        head_check = next(c for c in report.checks if c["name"] == "worker_repo_head")
        assert head_check["ok"] is True
        assert "aaaaaaaa" in str(head_check["detail"])

    def test_check_health_fails_when_worker_head_differs(self, tmp_path):
        """Remote health fails when worker HEAD differs from local HEAD."""
        from mtor.infra import check_health

        (tmp_path / ".git").mkdir()

        def fake_run(cmd, **kwargs):
            result = MagicMock()
            result.returncode = 0
            result.stderr = ""
            joined = " ".join(cmd)
            if cmd[:3] == ["git", "rev-parse", "HEAD"]:
                result.stdout = "aaaaaaaa11111111222222223333333344444444\n"
            elif "git rev-parse HEAD" in joined:
                result.stdout = "bbbbbbbb11111111222222223333333344444444\n"
            elif "echo ok" in joined:
                result.stdout = "ok\n"
            elif "df -h" in joined:
                result.stdout = "42%\n"
            elif "systemctl --user show mtor-worker.service" in joined:
                result.stdout = SERVICE_OK
            elif "ps -eo" in joined:
                result.stdout = WORKER_ROOT
            else:
                result.stdout = ""
            return result

        with patch("mtor.infra.subprocess.run", side_effect=fake_run):
            report = check_health(
                worker_host="remote-test-host",
                repo_dir=str(tmp_path),
                remote_repo_dir="/home/vivesca/code/mtor",
            )

        head_check = next(c for c in report.checks if c["name"] == "worker_repo_head")
        assert report.ok is False
        assert head_check["ok"] is False
        assert "aaaaaaaa" in str(head_check["detail"])
        assert "bbbbbbbb" in str(head_check["detail"])

    def test_check_health_accepts_worker_descendant_of_deployed_sha(self, tmp_path):
        """Deploy health is pinned to the deployed SHA, not moving local HEAD."""
        from mtor.infra import check_health

        (tmp_path / ".git").mkdir()
        worker_sha = "eeeeeeee11111111222222223333333344444444"
        calls = []

        def fake_run(cmd, **kwargs):
            calls.append(cmd)
            result = MagicMock(returncode=0, stderr="", stdout="")
            joined = " ".join(cmd)
            if "git rev-parse HEAD" in joined:
                result.stdout = f"{worker_sha}\n"
            elif "echo ok" in joined:
                result.stdout = "ok\n"
            elif "df -h" in joined:
                result.stdout = "42%\n"
            elif "systemctl --user show mtor-worker.service" in joined:
                result.stdout = SERVICE_OK
            elif "ps -eo" in joined:
                result.stdout = WORKER_ROOT
            return result

        with patch("mtor.infra.subprocess.run", side_effect=fake_run):
            report = check_health(
                worker_host="remote-test-host",
                repo_dir=str(tmp_path),
                remote_repo_dir="/home/vivesca/code/mtor",
                expected_sha=DEPLOY_SHA,
            )

        head_check = next(c for c in report.checks if c["name"] == "worker_repo_head")
        assert head_check["ok"] is True
        assert DEPLOY_SHA[:8] in str(head_check["detail"])
        assert worker_sha[:8] in str(head_check["detail"])
        assert ["git", "rev-parse", "HEAD"] not in calls

    def test_check_health_rejects_worker_missing_deployed_sha(self, tmp_path):
        """Deploy health fails when the worker does not contain the pinned SHA."""
        from mtor.infra import check_health

        (tmp_path / ".git").mkdir()

        def fake_run(cmd, **kwargs):
            result = MagicMock(returncode=0, stderr="", stdout="")
            joined = " ".join(cmd)
            if "git rev-parse HEAD" in joined:
                result.stdout = "eeeeeeee11111111222222223333333344444444\n"
            elif "git merge-base --is-ancestor" in joined:
                result.returncode = 1
            elif "echo ok" in joined:
                result.stdout = "ok\n"
            elif "df -h" in joined:
                result.stdout = "42%\n"
            elif "systemctl --user show mtor-worker.service" in joined:
                result.stdout = SERVICE_OK
            elif "ps -eo" in joined:
                result.stdout = WORKER_ROOT
            return result

        with patch("mtor.infra.subprocess.run", side_effect=fake_run):
            report = check_health(
                worker_host="remote-test-host",
                repo_dir=str(tmp_path),
                remote_repo_dir="/home/vivesca/code/mtor",
                expected_sha=DEPLOY_SHA,
            )

        head_check = next(c for c in report.checks if c["name"] == "worker_repo_head")
        assert report.ok is False
        assert head_check["ok"] is False
        assert "does not contain" in str(head_check["detail"])


# ---------------------------------------------------------------------------
# deploy tests
# ---------------------------------------------------------------------------


class TestDeploy:
    """Tests for infra deploy function and CLI command."""

    def setup_method(self):
        """Keep legacy deploy fakes focused on post-resolution subprocesses."""
        self._sha_patcher = patch(
            "mtor.infra._resolve_deploy_sha", return_value=DEPLOY_SHA
        )
        self._sha_patcher.start()

    def teardown_method(self):
        self._sha_patcher.stop()

    def test_test_infra_deploy_syncs_code(self):
        """deploy runs push, merge, restart, health-check steps."""
        from mtor.infra import DeployResult, deploy

        def fake_run(cmd, **kwargs):
            result = MagicMock()
            result.returncode = 0
            result.stdout = ""
            result.stderr = ""
            return result

        with (
            patch("mtor.infra.subprocess.run", side_effect=fake_run),
            patch("mtor.infra.time.sleep"),
            patch("mtor.infra.check_health") as mock_ch,
        ):
            from mtor.infra import HealthReport

            mock_ch.return_value = HealthReport(
                ok=True, checks=[{"name": "worker_ssh", "ok": True, "detail": "ok"}]
            )
            result = deploy(worker_host="test-host", repo_dir="/fake/repo")

        assert isinstance(result, DeployResult)
        assert result.healthy is True
        assert result.error is None
        # Verify required steps present
        step_names = [s["step"] for s in result.steps]
        assert "push" in step_names
        assert "merge" in step_names
        assert "verify_worker_head" in step_names
        assert "sync_environment" in step_names
        assert "restart" in step_names
        assert "health_check" in step_names
        assert step_names.count("orphan_cleanup") == 2
        # All steps should be ok
        assert all(s["ok"] for s in result.steps)

    def test_deploy_cli_command(self):
        """mtor rictor deploy returns valid JSON envelope."""
        with patch("mtor.cli._deploy") as mock_deploy:
            from mtor.infra import DeployResult

            mock_deploy.return_value = DeployResult(
                steps=[{"step": "push", "ok": True}],
                healthy=True,
            )
            exit_code, data = invoke(["rictor", "deploy"])
        assert exit_code == 0
        assert data["ok"] is True
        assert "steps" in data["result"]
        assert data["result"]["healthy"] is True

    def test_deploy_push_failure(self):
        """deploy returns error when push fails."""
        from mtor.infra import deploy

        def fake_run(cmd, **kwargs):
            result = MagicMock()
            if "push" in cmd:
                result.returncode = 1
                result.stderr = "remote rejected"
            else:
                result.returncode = 0
            result.stdout = ""
            return result

        with patch("mtor.infra.subprocess.run", side_effect=fake_run):
            result = deploy(worker_host="test-host", repo_dir="/fake/repo")

        assert result.healthy is False
        assert "push failed" in result.error

    def test_deploy_restart_failure(self):
        """deploy returns error when restart fails."""
        from mtor.infra import deploy

        call_count = [0]

        def fake_run(cmd, **kwargs):
            result = MagicMock()
            result.returncode = 0
            result.stdout = ""
            result.stderr = ""
            call_count[0] += 1
            if cmd == ["ssh", "test-host", "systemctl --user restart mtor-worker"]:
                result.returncode = 1
                result.stderr = "service not found"
            return result

        with patch("mtor.infra.subprocess.run", side_effect=fake_run):
            result = deploy(worker_host="test-host", repo_dir="/fake/repo")

        assert result.healthy is False
        assert "restart failed" in result.error.lower()

    def test_deploy_uses_remote_repo_for_worker_merge(self):
        """deploy merges inside the worker checkout, not the local macOS path."""
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
            patch("mtor.infra.subprocess.run", side_effect=fake_run),
            patch("mtor.infra.time.sleep"),
            patch("mtor.infra.check_health") as mock_ch,
        ):
            mock_ch.return_value = HealthReport(ok=True, checks=[])
            result = deploy(
                worker_host="test-host",
                repo_dir="/Users/terry/code/mtor",
                remote_repo_dir="/home/vivesca/code/mtor",
            )

        assert result.healthy is True
        merge_cmd = next(
            cmd
            for cmd in calls
            if cmd[:2] == ["ssh", "test-host"] and "git fetch origin main" in cmd[-1]
        )
        assert "cd /home/vivesca/code/mtor" in merge_cmd[-1]
        assert "/Users/terry/code/mtor" not in merge_cmd[-1]
        assert len(merge_cmd) == 3

    def test_deploy_fails_closed_on_worker_merge_error(self):
        """deploy does not report healthy when the worker merge fails."""
        from mtor.infra import deploy

        call_count = [0]

        def fake_run(cmd, **kwargs):
            call_count[0] += 1
            result = MagicMock()
            result.returncode = 0
            result.stdout = ""
            result.stderr = ""
            if call_count[0] == 2:
                result.returncode = 1
                result.stderr = "cd: no such file or directory"
            return result

        with patch("mtor.infra.subprocess.run", side_effect=fake_run):
            result = deploy(
                worker_host="test-host",
                repo_dir="/Users/terry/code/mtor",
                remote_repo_dir="/missing/mtor",
            )

        assert result.healthy is False
        assert result.steps[-1] == {"step": "merge", "ok": False}
        assert "merge failed" in result.error

    def test_deploy_health_uses_remote_repo_head_check(self):
        """deploy pins publication and health to one immutable commit."""
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
            patch("mtor.infra.subprocess.run", side_effect=fake_run),
            patch("mtor.infra.time.sleep"),
            patch("mtor.infra.check_health") as mock_ch,
        ):
            mock_ch.return_value = HealthReport(ok=True, checks=[])
            deploy(
                worker_host="test-host",
                repo_dir="/Users/terry/code/mtor",
                remote_repo_dir="/home/vivesca/code/mtor",
            )

        mock_ch.assert_called_once_with(
            worker_host="test-host",
            repo_dir="/Users/terry/code/mtor",
            remote_repo_dir="/home/vivesca/code/mtor",
            expected_sha=DEPLOY_SHA,
        )
        assert [
            "git",
            "push",
            "origin",
            f"{DEPLOY_SHA}:refs/heads/main",
        ] in calls
        checkout_commands = [
            cmd
            for cmd in calls
            if cmd[:2] == ["ssh", "test-host"]
            and any(
                operation in cmd[-1]
                for operation in (
                    "git fetch origin main",
                    "git merge-base --is-ancestor",
                    "uv sync --frozen",
                )
            )
        ]
        assert len(checkout_commands) == 3
        assert all(len(cmd) == 3 for cmd in checkout_commands)

    def test_deploy_fails_before_restart_when_worker_lacks_sha(self):
        """deploy never restarts a checkout that lacks the published commit."""
        from mtor.infra import deploy

        calls = []

        def fake_run(cmd, **kwargs):
            calls.append(cmd)
            result = MagicMock(returncode=0, stdout="", stderr="")
            if "git merge-base --is-ancestor" in " ".join(cmd):
                result.returncode = 1
            return result

        with (
            patch("mtor.infra.subprocess.run", side_effect=fake_run),
            patch("mtor.infra.time.sleep"),
        ):
            result = deploy(worker_host="test-host", repo_dir="/fake/repo")

        assert result.healthy is False
        assert "does not contain deployed SHA" in str(result.error)
        assert result.steps[-1]["step"] == "verify_worker_head"
        assert result.steps[-1]["attempts"] == 3
        assert not any(
            cmd == ["ssh", "test-host", "systemctl --user restart mtor-worker"]
            for cmd in calls
        )

    def test_deploy_syncs_frozen_environment_before_restart(self):
        """deploy refreshes generated entry points before restarting the worker."""
        from mtor.infra import HealthReport, deploy

        events = []

        def fake_run(cmd, **kwargs):
            result = MagicMock(returncode=0, stdout="", stderr="")
            joined = " ".join(cmd)
            if "uv sync --frozen" in joined:
                events.append("sync")
            elif cmd == ["ssh", "test-host", "systemctl --user restart mtor-worker"]:
                events.append("restart")
            return result

        with (
            patch("mtor.infra.subprocess.run", side_effect=fake_run),
            patch("mtor.infra.time.sleep"),
            patch(
                "mtor.infra.check_health",
                return_value=HealthReport(ok=True, checks=[]),
            ),
        ):
            result = deploy(worker_host="test-host", repo_dir="/fake/repo")

        assert result.healthy is True
        assert events == ["sync", "restart"]

    def test_deploy_fails_before_restart_when_environment_sync_fails(self):
        """deploy leaves the service untouched when frozen reconciliation fails."""
        from mtor.infra import deploy

        calls = []

        def fake_run(cmd, **kwargs):
            calls.append(cmd)
            result = MagicMock(returncode=0, stdout="", stderr="")
            if "uv sync --frozen" in " ".join(cmd):
                result.returncode = 1
                result.stderr = "lock mismatch"
            return result

        with patch("mtor.infra.subprocess.run", side_effect=fake_run):
            result = deploy(worker_host="test-host", repo_dir="/fake/repo")

        assert result.healthy is False
        assert "environment sync failed" in str(result.error)
        assert result.steps[-1] == {"step": "sync_environment", "ok": False}
        assert not any(
            cmd == ["ssh", "test-host", "systemctl --user restart mtor-worker"]
            for cmd in calls
        )

    def test_deploy_records_orphan_cleanup_after_restart(self):
        """deploy terminates orphan worker roots and records the cleanup step."""
        from mtor.infra import HealthReport, deploy

        calls = []

        def fake_run(cmd, **kwargs):
            calls.append(cmd)
            result = MagicMock()
            result.returncode = 0
            result.stdout = ""
            result.stderr = ""
            if (
                cmd[:4] == ["ssh", "test-host", "bash", "-lc"]
                and "list_orphans" in cmd[-1]
            ):
                result.stdout = "found=1\nterminated=1\nremaining=0\npids=321\n"
            return result

        with (
            patch("mtor.infra.subprocess.run", side_effect=fake_run),
            patch("mtor.infra.time.sleep"),
            patch("mtor.infra.check_health") as mock_ch,
        ):
            mock_ch.return_value = HealthReport(ok=True, checks=[])
            result = deploy(worker_host="test-host", repo_dir="/fake/repo")

        assert result.healthy is True
        cleanup_steps = [
            step for step in result.steps if step["step"] == "orphan_cleanup"
        ]
        assert len(cleanup_steps) == 2
        assert [step["attempt"] for step in cleanup_steps] == [1, 2]
        for cleanup_step in cleanup_steps:
            assert cleanup_step["ok"] is True
            assert cleanup_step["found"] == 1
            assert cleanup_step["terminated"] == 1
            assert cleanup_step["remaining"] == 0
            assert cleanup_step["pids"] == ["321"]
        assert any(
            "kill -TERM $pids" in cmd[-1] for cmd in calls if "list_orphans" in cmd[-1]
        )

    def test_deploy_runs_settle_cleanup_settle_cleanup_before_health(self):
        """deploy retires legacy worker, waits after restart, cleans twice, then checks health."""
        from mtor.infra import HealthReport, deploy

        events = []

        def fake_run(cmd, **kwargs):
            result = MagicMock()
            result.returncode = 0
            result.stdout = ""
            result.stderr = ""
            if (
                cmd[:4] == ["ssh", "test-host", "bash", "-lc"]
                and "disable --now temporal-worker" in cmd[-1]
            ):
                events.append("retire")
                result.stdout = "active=failed\nenabled=disabled\n"
            elif cmd == ["ssh", "test-host", "systemctl --user restart mtor-worker"]:
                events.append("restart")
            elif (
                cmd[:4] == ["ssh", "test-host", "bash", "-lc"]
                and "list_orphans" in cmd[-1]
            ):
                events.append("cleanup")
                result.stdout = "found=0\nterminated=0\nremaining=0\npids=\n"
            return result

        def fake_sleep(seconds):
            events.append(("sleep", seconds))

        def fake_health(**kwargs):
            events.append("health")
            return HealthReport(ok=True, checks=[])

        with (
            patch("mtor.infra.subprocess.run", side_effect=fake_run),
            patch("mtor.infra.time.sleep", side_effect=fake_sleep),
            patch("mtor.infra.check_health", side_effect=fake_health),
        ):
            result = deploy(worker_host="test-host", repo_dir="/fake/repo")

        assert result.healthy is True
        assert events == [
            "retire",
            "restart",
            ("sleep", 3),
            "cleanup",
            ("sleep", 3),
            "cleanup",
            "health",
        ]

    def test_deploy_records_legacy_temporal_retirement_before_restart(self):
        """deploy disables the obsolete system temporal-worker before restart."""
        from mtor.infra import HealthReport, deploy

        events = []

        def fake_run(cmd, **kwargs):
            result = MagicMock()
            result.returncode = 0
            result.stdout = ""
            result.stderr = ""
            if (
                cmd[:4] == ["ssh", "test-host", "bash", "-lc"]
                and "disable --now temporal-worker" in cmd[-1]
            ):
                events.append("retire")
                result.stdout = "active=failed\nenabled=disabled\n"
            elif cmd == ["ssh", "test-host", "systemctl --user restart mtor-worker"]:
                events.append("restart")
            elif (
                cmd[:4] == ["ssh", "test-host", "bash", "-lc"]
                and "list_orphans" in cmd[-1]
            ):
                result.stdout = "found=0\nterminated=0\nremaining=0\npids=\n"
            return result

        with (
            patch("mtor.infra.subprocess.run", side_effect=fake_run),
            patch("mtor.infra.time.sleep"),
            patch("mtor.infra.check_health") as mock_ch,
        ):
            mock_ch.return_value = HealthReport(ok=True, checks=[])
            result = deploy(worker_host="test-host", repo_dir="/fake/repo")

        assert result.healthy is True
        assert events == ["retire", "restart"]
        retire_step = next(
            step
            for step in result.steps
            if step["step"] == "retire_legacy_temporal_worker"
        )
        assert retire_step["ok"] is True
        assert retire_step["active"] == "failed"
        assert retire_step["enabled"] == "disabled"

    def test_deploy_fails_before_restart_when_legacy_temporal_retirement_fails(self):
        """deploy aborts if the obsolete system temporal-worker remains enabled."""
        from mtor.infra import deploy

        calls = []

        def fake_run(cmd, **kwargs):
            calls.append(cmd)
            result = MagicMock()
            result.returncode = 0
            result.stdout = ""
            result.stderr = ""
            if (
                cmd[:4] == ["ssh", "test-host", "bash", "-lc"]
                and "disable --now temporal-worker" in cmd[-1]
            ):
                result.returncode = 1
                result.stdout = "active=active\nenabled=enabled\n"
            return result

        with patch("mtor.infra.subprocess.run", side_effect=fake_run):
            result = deploy(worker_host="test-host", repo_dir="/fake/repo")

        assert result.healthy is False
        assert "Legacy temporal-worker retirement failed" in str(result.error)
        assert not any(
            cmd == ["ssh", "test-host", "systemctl --user restart mtor-worker"]
            for cmd in calls
        )

    def test_deploy_orphan_cleanup_targets_only_ppid_one_roots(self):
        """cleanup command filters to orphaned op-run worker roots under PID 1."""
        from mtor.infra import HealthReport, deploy

        cleanup_commands = []

        def fake_run(cmd, **kwargs):
            result = MagicMock()
            result.returncode = 0
            result.stdout = ""
            result.stderr = ""
            if (
                cmd[:4] == ["ssh", "test-host", "bash", "-lc"]
                and "list_orphans" in cmd[-1]
            ):
                cleanup_commands.append(cmd[-1])
                result.stdout = "found=0\nterminated=0\nremaining=0\npids=\n"
            return result

        with (
            patch("mtor.infra.subprocess.run", side_effect=fake_run),
            patch("mtor.infra.time.sleep"),
            patch("mtor.infra.check_health") as mock_ch,
        ):
            mock_ch.return_value = HealthReport(ok=True, checks=[])
            result = deploy(worker_host="test-host", repo_dir="/fake/repo")

        assert result.healthy is True
        assert len(cleanup_commands) == 2
        for cleanup_command in cleanup_commands:
            assert "$2 == 1" in cleanup_command
            assert 'index($0, "op run")' in cleanup_command
            assert 'index($0, "python3 -m mtor.worker")' in cleanup_command

    def test_deploy_fails_if_orphan_cleanup_leaves_remaining_roots(self):
        """deploy does not report healthy when cleanup cannot remove all orphans."""
        from mtor.infra import HealthReport, deploy

        def fake_run(cmd, **kwargs):
            result = MagicMock()
            result.returncode = 0
            result.stdout = ""
            result.stderr = ""
            if (
                cmd[:4] == ["ssh", "test-host", "bash", "-lc"]
                and "list_orphans" in cmd[-1]
            ):
                result.stdout = "found=2\nterminated=1\nremaining=1\npids=321,322\n"
            return result

        with (
            patch("mtor.infra.subprocess.run", side_effect=fake_run),
            patch("mtor.infra.time.sleep"),
            patch("mtor.infra.check_health") as mock_ch,
        ):
            mock_ch.return_value = HealthReport(ok=True, checks=[])
            result = deploy(worker_host="test-host", repo_dir="/fake/repo")

        cleanup_step = next(
            step for step in result.steps if step["step"] == "orphan_cleanup"
        )
        health_step = next(
            step for step in result.steps if step["step"] == "health_check"
        )
        assert result.healthy is False
        assert cleanup_step["ok"] is False
        assert cleanup_step["remaining"] == 1
        assert health_step["ok"] is True


# ---------------------------------------------------------------------------
# clean tests
# ---------------------------------------------------------------------------


class TestClean:
    """Tests for infra clean function and CLI command."""

    def test_clean_removes_old_files(self, tmp_path):
        """clean removes output files older than threshold."""
        import time

        from mtor.infra import clean

        outputs = tmp_path / "outputs"
        outputs.mkdir()

        # Create an old file
        old_file = outputs / "old-log.txt"
        old_file.write_text("old output")
        # Backdate mtime
        old_time = time.time() - 10 * 86400  # 10 days ago
        import os

        os.utime(old_file, (old_time, old_time))

        # Create a recent file
        recent_file = outputs / "recent-log.txt"
        recent_file.write_text("recent output")

        result = clean(outputs_dir=str(outputs), older_than_days=7)
        assert result.outputs_removed == 1
        assert not old_file.exists()
        assert recent_file.exists()

    def test_clean_removes_old_checkpoints(self, tmp_path):
        """clean removes checkpoint files older than threshold."""
        import os
        import time

        from mtor.infra import clean

        outputs = tmp_path / "outputs"
        cp_dir = outputs / "checkpoints"
        cp_dir.mkdir(parents=True)

        old_cp = cp_dir / "old-checkpoint.json"
        old_cp.write_text('{"id": "old"}')
        old_time = time.time() - 10 * 86400
        os.utime(old_cp, (old_time, old_time))

        result = clean(outputs_dir=str(outputs), older_than_days=7)
        assert result.checkpoints_removed == 1
        assert not old_cp.exists()

    def test_clean_nothing_to_clean(self, tmp_path):
        """clean with no old files returns zero counts."""
        from mtor.infra import clean

        outputs = tmp_path / "outputs"
        outputs.mkdir()
        (outputs / "recent.txt").write_text("fresh")

        result = clean(outputs_dir=str(outputs), older_than_days=7)
        assert result.outputs_removed == 0
        assert result.checkpoints_removed == 0
        assert result.errors == []

    def test_clean_cli_command(self, tmp_path):
        """mtor rictor clean returns valid JSON envelope."""
        with patch("mtor.cli._clean") as mock_clean:
            from mtor.infra import CleanResult

            mock_clean.return_value = CleanResult(
                outputs_removed=3, checkpoints_removed=1
            )
            exit_code, data = invoke(["rictor", "clean"])
        assert exit_code == 0
        assert data["ok"] is True
        assert data["result"]["outputs_removed"] == 3
        assert data["result"]["checkpoints_removed"] == 1

    def test_clean_nonexistent_dir(self, tmp_path):
        """clean with missing directory returns zeros, no errors."""
        from mtor.infra import clean

        result = clean(outputs_dir=str(tmp_path / "nope"), older_than_days=7)
        assert result.outputs_removed == 0
        assert result.checkpoints_removed == 0
        assert result.errors == []


# ---------------------------------------------------------------------------
# infra module import tests
# ---------------------------------------------------------------------------


class TestInfraModule:
    """Basic module-level sanity tests."""

    def test_resolve_deploy_sha_captures_verified_commit(self):
        from mtor.infra import _resolve_deploy_sha

        result = MagicMock(returncode=0, stdout=f"{DEPLOY_SHA}\n", stderr="")
        with patch("mtor.infra.subprocess.run", return_value=result) as run:
            resolved = _resolve_deploy_sha("/fake/repo")

        assert resolved == DEPLOY_SHA
        run.assert_called_once_with(
            ["git", "rev-parse", "--verify", "HEAD^{commit}"],
            capture_output=True,
            text=True,
            timeout=10,
            cwd="/fake/repo",
        )

    def test_resolve_deploy_sha_rejects_empty_git_result(self):
        from mtor.infra import _resolve_deploy_sha

        result = MagicMock(returncode=0, stdout="", stderr="")
        with (
            patch("mtor.infra.subprocess.run", return_value=result),
            pytest.raises(RuntimeError, match="empty commit"),
        ):
            _resolve_deploy_sha("/fake/repo")

    def test_check_health_is_importable(self):
        from mtor.infra import check_health

        assert callable(check_health)

    def test_deploy_is_importable(self):
        from mtor.infra import deploy

        assert callable(deploy)

    def test_clean_is_importable(self):
        from mtor.infra import clean

        assert callable(clean)

    def test_health_report_to_dict(self):
        from mtor.infra import HealthReport

        report = HealthReport(ok=True, checks=[{"name": "x", "ok": True, "detail": ""}])
        d = report.to_dict()
        assert d["ok"] is True
        assert len(d["checks"]) == 1

    def test_deploy_result_to_dict(self):
        from mtor.infra import DeployResult

        result = DeployResult(steps=[], healthy=False, error="boom")
        d = result.to_dict()
        assert d["healthy"] is False
        assert d["error"] == "boom"

    def test_clean_result_to_dict(self):
        from mtor.infra import CleanResult

        result = CleanResult(outputs_removed=5, checkpoints_removed=2)
        d = result.to_dict()
        assert d["outputs_removed"] == 5
        assert d["checkpoints_removed"] == 2
