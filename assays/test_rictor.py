"""Tests for mtor.infra (rictor CLI) — check_health, deploy, clean."""

from __future__ import annotations

import io
import json
import sys
from pathlib import Path
from unittest.mock import MagicMock, patch

from mtor.cli import app


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


SERVICE_OK = (
    "__MTOR_WORKER__\n"
    "ActiveState=active\nSubState=running\nMainPID=123\n"
    "__TEMPORAL_WORKER__\n"
    "ActiveState=inactive\nSubState=dead\nMainPID=0\n"
)
SERVICE_MTOR_INACTIVE = (
    "__MTOR_WORKER__\n"
    "ActiveState=failed\nSubState=failed\nMainPID=0\n"
    "__TEMPORAL_WORKER__\n"
    "ActiveState=inactive\nSubState=dead\nMainPID=0\n"
)
SERVICE_TEMPORAL_ACTIVE = (
    "__MTOR_WORKER__\n"
    "ActiveState=active\nSubState=running\nMainPID=123\n"
    "__TEMPORAL_WORKER__\n"
    "ActiveState=active\nSubState=running\nMainPID=456\n"
)
WORKER_ROOT = "123 77 op run --env-file /home/vivesca/germline/loci/env.op -- python3 -m mtor.worker\n"


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
            "temporal_worker": "ActiveState=inactive\nSubState=dead\nMainPID=0",
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

        service_check = next(c for c in report.checks if c["name"] == "worker_service_singleton")
        process_check = next(c for c in report.checks if c["name"] == "worker_process_singleton")
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

        service_check = next(c for c in report.checks if c["name"] == "worker_service_singleton")
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

        service_check = next(c for c in report.checks if c["name"] == "worker_service_singleton")
        assert report.ok is False
        assert service_check["ok"] is False
        assert "temporal-worker.service" in str(service_check["detail"])

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

        process_check = next(c for c in report.checks if c["name"] == "worker_process_singleton")
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
                result.stdout = (
                    "123 1 op run --env-file /home/vivesca/germline/loci/env.op -- python3 -m mtor.worker\n"
                )
            else:
                result.stdout = ""
            return result

        with patch("mtor.infra.subprocess.run", side_effect=fake_run):
            report = check_health(worker_host="ganglion", repo_dir=str(tmp_path))

        process_check = next(c for c in report.checks if c["name"] == "worker_process_singleton")
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


# ---------------------------------------------------------------------------
# deploy tests
# ---------------------------------------------------------------------------


class TestDeploy:
    """Tests for infra deploy function and CLI command."""

    def test_test_infra_deploy_syncs_code(self):
        """deploy runs push, merge, restart, health-check steps."""
        from mtor.infra import DeployResult, deploy

        def fake_run(cmd, **kwargs):
            result = MagicMock()
            result.returncode = 0
            result.stdout = ""
            result.stderr = ""
            return result

        with patch("mtor.infra.subprocess.run", side_effect=fake_run), \
             patch("mtor.infra.time.sleep"), \
             patch("mtor.infra.check_health") as mock_ch:
            from mtor.infra import HealthReport

            mock_ch.return_value = HealthReport(
                ok=True, checks=[{"name": "worker_ssh", "ok": True, "detail": "ok"}]
            )
            result = deploy(worker_host="test-host", repo_dir="/fake/repo")

        assert isinstance(result, DeployResult)
        assert result.healthy is True
        assert result.error is None
        # Verify all 4 steps present
        step_names = [s["step"] for s in result.steps]
        assert "push" in step_names
        assert "merge" in step_names
        assert "restart" in step_names
        assert "health_check" in step_names
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
            # Third call is the restart
            call_count[0] += 1
            if call_count[0] == 3 and "restart" in " ".join(cmd):
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

        with patch("mtor.infra.subprocess.run", side_effect=fake_run), \
             patch("mtor.infra.time.sleep"), \
             patch("mtor.infra.check_health") as mock_ch:
            mock_ch.return_value = HealthReport(ok=True, checks=[])
            result = deploy(
                worker_host="test-host",
                repo_dir="/Users/terry/code/mtor",
                remote_repo_dir="/home/vivesca/code/mtor",
            )

        assert result.healthy is True
        merge_cmd = next(cmd for cmd in calls if cmd[:2] == ["ssh", "test-host"] and "bash" in cmd)
        assert "cd /home/vivesca/code/mtor" in merge_cmd[-1]
        assert "/Users/terry/code/mtor" not in merge_cmd[-1]

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
        """deploy passes local and worker repo paths into check_health."""
        from mtor.infra import HealthReport, deploy

        def fake_run(cmd, **kwargs):
            result = MagicMock()
            result.returncode = 0
            result.stdout = ""
            result.stderr = ""
            return result

        with patch("mtor.infra.subprocess.run", side_effect=fake_run), \
             patch("mtor.infra.time.sleep"), \
             patch("mtor.infra.check_health") as mock_ch:
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
        )


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
