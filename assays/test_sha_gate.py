"""SHA gate tests — auto-deploy on drift between local and worker HEAD.

Tests the _check_worker_sha() function and --skip-sha-check CLI flag
integration across default, scout, and research subcommands.
"""

from __future__ import annotations

import io
import json
import sys
from contextlib import ExitStack
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from mtor.backend import TemporalBackend
from mtor.cli import app


# ---------------------------------------------------------------------------
# Helpers (mirrors test_mtor.py patterns)
# ---------------------------------------------------------------------------


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


def make_mock_client():
    """Build a minimal async mock Temporal client."""
    client = MagicMock()
    handle = MagicMock()
    handle.id = "ribosome-test1234"
    client.start_workflow = AsyncMock(return_value=handle)

    async def _fake_list(query=None):
        yield MagicMock()

    client.list_workflows = _fake_list

    wf_handle = MagicMock()
    desc = MagicMock()
    desc.status = MagicMock()
    desc.status.name = "COMPLETED"
    wf_handle.describe = AsyncMock(return_value=desc)
    wf_handle.cancel = AsyncMock(return_value=None)
    client.get_workflow_handle = MagicMock(return_value=wf_handle)

    async def _fake_count(query=None):
        return 0

    client.count_workflows = _fake_count

    return client, wf_handle


_CLIENT_PATCH_TARGETS = [
    "mtor.cli._get_client",
    "mtor.dispatch._get_client",
]
_BACKEND_PATCH_TARGETS = ["mtor.cli._get_backend", "mtor.doctor._get_backend"]


def _patch_client(mock_client):
    """Context manager: patch _get_client in all modules."""
    stack = ExitStack()
    for target in _CLIENT_PATCH_TARGETS:
        stack.enter_context(patch(target, return_value=(mock_client, None)))
    for target in _BACKEND_PATCH_TARGETS:
        stack.enter_context(
            patch(target, return_value=(TemporalBackend(mock_client), None))
        )
    stack.enter_context(patch("mtor.cli._check_dedup_only", return_value=None))
    stack.enter_context(patch("mtor.cli._record_dispatch"))
    return stack


# ---------------------------------------------------------------------------
# Unit tests for _check_worker_sha
# ---------------------------------------------------------------------------


class TestCheckWorkerSha:
    """Unit tests for _check_worker_sha in dispatch.py."""

    def test_match_returns_true_no_deploy(self):
        """When local and remote SHA match, returns True without deploying."""
        from mtor.dispatch import _check_worker_sha

        with (
            patch("mtor.dispatch.subprocess") as mock_sp,
            patch("mtor.dispatch._check_worker_checkout"),
        ):
            mock_sp.run.side_effect = [
                MagicMock(returncode=0, stdout="abc123def456\n"),
                MagicMock(returncode=0, stdout="abc123def456\n"),
            ]
            result = _check_worker_sha()
        assert result is True
        assert mock_sp.run.call_count == 2  # local + remote only

    def test_mismatch_auto_deploys_and_returns_true(self):
        """SHA mismatch triggers push + merge + restart, then returns True."""
        from mtor.dispatch import _check_worker_sha

        with (
            patch("mtor.dispatch.subprocess") as mock_sp,
            patch("mtor.dispatch.time") as mock_time,
            patch("mtor.dispatch._check_worker_checkout"),
            patch("mtor.dispatch._count_active_ribosomes", return_value=0),
            patch("mtor.dispatch.restart_worker") as mock_restart,
        ):
            mock_sp.run.side_effect = [
                MagicMock(returncode=0, stdout="aaa111\n"),  # local SHA
                MagicMock(returncode=0, stdout="bbb222\n"),  # remote SHA (diff)
                MagicMock(
                    returncode=1, stdout=""
                ),  # contains-local probe: NOT contained → deploy
                MagicMock(returncode=0, stdout=""),  # push
                MagicMock(returncode=0, stdout=""),  # merge
                MagicMock(
                    returncode=0, stdout="HEAD:aaa111\nCONTAINS:1\n"
                ),  # worker HEAD contains pushed
            ]
            result = _check_worker_sha()
        assert result is True
        assert mock_sp.run.call_count == 6
        mock_restart.assert_called_once()
        # Worker advanced on first attempt → no backoff sleep, only post-restart settle.
        mock_time.sleep.assert_called_once_with(3)

    def test_worker_ahead_of_pushed_sha_passes(self):
        """Worker fast-forwarded PAST the pushed SHA (via its git-sync timer or a
        concurrent push) still passes: the gate asserts CONTAINMENT, not exact
        equality. Regression guard for the spurious "worker HEAD did not advance"
        failure that previously forced --skip-sha-check."""
        from mtor.dispatch import _check_worker_sha

        with (
            patch("mtor.dispatch.subprocess") as mock_sp,
            patch("mtor.dispatch.time") as mock_time,
            patch("mtor.dispatch._check_worker_checkout"),
            patch("mtor.dispatch._count_active_ribosomes", return_value=0),
            patch("mtor.dispatch.restart_worker") as mock_restart,
        ):
            mock_sp.run.side_effect = [
                MagicMock(returncode=0, stdout="aaa111\n"),  # local SHA (pushed)
                MagicMock(returncode=0, stdout="bbb222\n"),  # remote SHA (differs)
                MagicMock(
                    returncode=1, stdout=""
                ),  # contains-local probe: NOT contained → deploy
                MagicMock(returncode=0, stdout=""),  # push
                MagicMock(returncode=0, stdout=""),  # merge
                # Worker overshot to ccc999 (a newer origin/main commit) that
                # still CONTAINS the pushed aaa111 → ancestry holds, gate passes.
                MagicMock(returncode=0, stdout="HEAD:ccc999\nCONTAINS:1\n"),
            ]
            result = _check_worker_sha()
        assert result is True
        assert mock_sp.run.call_count == 6
        mock_restart.assert_called_once()
        # No backoff sleep needed; only the post-restart settle ran.
        mock_time.sleep.assert_called_once_with(3)

        # The post-merge containment probe carried the pushed SHA into merge-base.
        probe_cmd = mock_sp.run.call_args_list[5][0][0][-1]
        assert "git merge-base --is-ancestor" in probe_cmd
        assert "aaa111" in probe_cmd

    def test_worker_contains_local_short_circuits_no_deploy(self):
        """When local and worker HEADs differ but the worker already CONTAINS
        local HEAD (worker is ahead via its git-sync timer), the gate returns
        True WITHOUT pushing or restarting — this avoids a doomed non-ff push
        when local is behind origin, and a pointless restart (germline is read
        fresh per task from a worktree, never cached in the worker process)."""
        from mtor.dispatch import _check_worker_sha

        with (
            patch("mtor.dispatch.subprocess") as mock_sp,
            patch("mtor.dispatch._check_worker_checkout"),
        ):
            mock_sp.run.side_effect = [
                MagicMock(returncode=0, stdout="aaa\n"),  # local HEAD (behind)
                MagicMock(returncode=0, stdout="ccc\n"),  # worker HEAD (ahead, differs)
                MagicMock(
                    returncode=0, stdout=""
                ),  # contains-local probe: CONTAINED (rc 0)
            ]
            result = _check_worker_sha()
        assert result is True
        # No push, no merge, no restart — only the three lookups ran.
        assert mock_sp.run.call_count == 3
        assert not any(
            "push" in str(c.args[0]) or "restart" in str(c.args[0])
            for c in mock_sp.run.call_args_list
        )
        # The short-circuit probe asked whether the worker contains local HEAD.
        contains_cmd = mock_sp.run.call_args_list[2][0][0][-1]
        assert "git merge-base --is-ancestor" in contains_cmd
        assert "aaa" in contains_cmd

    def test_skip_returns_true_immediately(self):
        """skip=True returns True without any subprocess calls."""
        from mtor.dispatch import _check_worker_sha

        with patch("mtor.dispatch.subprocess") as mock_sp:
            result = _check_worker_sha(skip=True)
        assert result is True
        mock_sp.run.assert_not_called()

    def test_local_git_failure_raises(self):
        """If local git rev-parse fails, raises RuntimeError."""
        from mtor.dispatch import _check_worker_sha

        with patch("mtor.dispatch.subprocess") as mock_sp:
            mock_sp.run.return_value = MagicMock(
                returncode=1, stderr="fatal: not a git repo"
            )
            with pytest.raises(RuntimeError, match="local git HEAD"):
                _check_worker_sha()

    def test_remote_git_failure_raises(self):
        """If SSH to worker fails, raises RuntimeError."""
        from mtor.dispatch import _check_worker_sha

        with patch("mtor.dispatch.subprocess") as mock_sp:
            mock_sp.run.side_effect = [
                MagicMock(returncode=0, stdout="abc123\n"),
                MagicMock(returncode=1, stderr="Connection refused"),
            ]
            with pytest.raises(RuntimeError, match="worker git HEAD"):
                _check_worker_sha()

    def test_push_failure_raises(self):
        """If git push fails during auto-deploy, raises RuntimeError."""
        from mtor.dispatch import _check_worker_sha

        with (
            patch("mtor.dispatch.subprocess") as mock_sp,
            patch("mtor.dispatch.time"),
            patch("mtor.dispatch._check_worker_checkout"),
        ):
            mock_sp.run.side_effect = [
                MagicMock(returncode=0, stdout="aaa\n"),
                MagicMock(returncode=0, stdout="bbb\n"),
                MagicMock(
                    returncode=1, stdout=""
                ),  # contains-local probe: NOT contained → deploy
                MagicMock(returncode=1, stderr="remote rejected"),
            ]
            with pytest.raises(RuntimeError, match="push failed"):
                _check_worker_sha()

    def test_restart_failure_raises(self):
        """If worker restart fails during auto-deploy, raises RuntimeError."""
        from mtor.dispatch import _check_worker_sha

        with (
            patch("mtor.dispatch.subprocess") as mock_sp,
            patch("mtor.dispatch.time"),
            patch("mtor.dispatch._check_worker_checkout"),
            patch("mtor.dispatch._count_active_ribosomes", return_value=0),
            patch(
                "mtor.dispatch.restart_worker",
                side_effect=RuntimeError("worker restart failed: systemctl failed"),
            ),
        ):
            mock_sp.run.side_effect = [
                MagicMock(returncode=0, stdout="aaa\n"),
                MagicMock(returncode=0, stdout="bbb\n"),
                MagicMock(
                    returncode=1, stdout=""
                ),  # contains-local probe: NOT contained → deploy
                MagicMock(returncode=0, stdout=""),  # push ok
                MagicMock(returncode=0, stdout=""),  # merge ok
                MagicMock(
                    returncode=0, stdout="HEAD:aaa\nCONTAINS:1\n"
                ),  # worker HEAD contains pushed
            ]
            with pytest.raises(RuntimeError, match="restart failed"):
                _check_worker_sha()

    def test_deploy_pushes_head_to_main_and_ff_merges(self):
        """Auto-deploy pushes HEAD:main to origin, then ff-merges on the worker.

        The earlier mechanism force-pushed to a unique ``deploy-sync-<pid>-<ts>``
        branch on the worker and merged/deleted it (hence the old os.getpid +
        time.time patches). That was replaced (commit 5f43ad9) by an
        origin-mediated deploy: push HEAD straight to origin/main, then
        ``git fetch origin main && git merge --ff-only origin/main`` on the
        worker. No temp branch, no pid, no force-push.
        """
        from mtor.dispatch import _check_worker_sha

        with (
            patch("mtor.dispatch.subprocess") as mock_sp,
            patch("mtor.dispatch.time"),  # silence time.sleep(3) after restart
            patch("mtor.dispatch._check_worker_checkout"),
            patch("mtor.dispatch._count_active_ribosomes", return_value=0),
            patch("mtor.dispatch.restart_worker") as mock_restart,
        ):
            mock_sp.run.side_effect = [
                MagicMock(returncode=0, stdout="aaa\n"),  # local HEAD
                MagicMock(returncode=0, stdout="bbb\n"),  # worker HEAD (differs)
                MagicMock(
                    returncode=1, stdout=""
                ),  # contains-local probe: NOT contained → deploy
                MagicMock(returncode=0, stdout=""),  # push ok
                MagicMock(returncode=0, stdout=""),  # merge ok
                MagicMock(
                    returncode=0, stdout="HEAD:aaa\nCONTAINS:1\n"
                ),  # worker HEAD post-merge (contains)
            ]
            _check_worker_sha()

        # Third call probes whether the worker already contains local HEAD
        # (skip-deploy short-circuit); here it does not, so deploy proceeds.
        contains_cmd = mock_sp.run.call_args_list[2][0][0][-1]
        assert "git merge-base --is-ancestor" in contains_cmd

        # Fourth call pushes local HEAD straight to origin/main — no temp branch.
        push_args = mock_sp.run.call_args_list[3][0][0]
        assert "push" in push_args
        assert "origin" in push_args
        assert "HEAD:main" in push_args
        assert not any("deploy-sync" in str(a) for a in push_args)

        # Fifth call fast-forwards the worker checkout from origin/main.
        merge_cmd = mock_sp.run.call_args_list[4][0][0][-1]
        assert "git fetch origin main" in merge_cmd
        assert "git merge --ff-only origin/main" in merge_cmd

        # Sixth call re-reads the worker HEAD and confirms it CONTAINS the
        # pushed SHA (ancestry), tolerating a worker that fast-forwarded past it.
        verify_cmd = mock_sp.run.call_args_list[5][0][0][-1]
        assert "git rev-parse HEAD" in verify_cmd
        assert "git merge-base --is-ancestor" in verify_cmd

        # The worker restart now goes through the shared restart_worker() helper,
        # not an inline subprocess call.
        mock_restart.assert_called_once()

    def test_merge_failure_raises(self):
        """If worker merge fails during auto-deploy, raises RuntimeError."""
        from mtor.dispatch import _check_worker_sha

        with (
            patch("mtor.dispatch.subprocess") as mock_sp,
            patch("mtor.dispatch.time"),
            patch("mtor.dispatch._check_worker_checkout"),
        ):
            mock_sp.run.side_effect = [
                MagicMock(returncode=0, stdout="aaa\n"),
                MagicMock(returncode=0, stdout="bbb\n"),
                MagicMock(
                    returncode=1, stdout=""
                ),  # contains-local probe: NOT contained → deploy
                MagicMock(returncode=0, stdout=""),  # push ok
                MagicMock(returncode=1, stderr="merge conflict"),  # merge fail
            ]
            with pytest.raises(RuntimeError, match="merge failed"):
                _check_worker_sha()

    def test_worker_head_not_advancing_raises_without_restart(self):
        """If the ff-merge exits 0 but the worker HEAD never reaches the pushed
        SHA (GitHub push-propagation lag that never resolves within the retry
        window), raise BEFORE restarting — never restart on stale code, never
        return True claiming "in sync"."""
        from mtor.dispatch import _check_worker_sha

        with (
            patch("mtor.dispatch.subprocess") as mock_sp,
            patch("mtor.dispatch.time") as mock_time,
            patch("mtor.dispatch._check_worker_checkout"),
        ):
            mock_sp.run.side_effect = [
                MagicMock(returncode=0, stdout="aaa\n"),  # local SHA (pushed)
                MagicMock(returncode=0, stdout="bbb\n"),  # remote SHA (differs)
                MagicMock(
                    returncode=1, stdout=""
                ),  # contains-local probe: NOT contained → deploy
                MagicMock(returncode=0, stdout=""),  # push ok
                MagicMock(returncode=0, stdout=""),  # merge attempt 1 (no-op, exits 0)
                MagicMock(
                    returncode=0, stdout="HEAD:bbb\nCONTAINS:0\n"
                ),  # worker HEAD lacks pushed SHA
                MagicMock(returncode=0, stdout=""),  # merge attempt 2
                MagicMock(
                    returncode=0, stdout="HEAD:bbb\nCONTAINS:0\n"
                ),  # worker HEAD lacks pushed SHA
                MagicMock(returncode=0, stdout=""),  # merge attempt 3
                MagicMock(
                    returncode=0, stdout="HEAD:bbb\nCONTAINS:0\n"
                ),  # worker HEAD lacks pushed SHA
            ]
            with pytest.raises(RuntimeError, match="does not contain pushed SHA"):
                _check_worker_sha()

        # Fail closed: the worker was never restarted on stale code.
        assert not any(
            "restart" in str(call.args[0]) for call in mock_sp.run.call_args_list
        )
        # 4 (local/remote/contains-probe/push) + 3 × (merge + probe) = 10 calls, no restart.
        assert mock_sp.run.call_count == 10
        # Two backoff sleeps between attempts; the post-restart settle never ran.
        assert mock_time.sleep.call_count == 2
        assert (3,) not in [c.args for c in mock_time.sleep.call_args_list]

    def test_repo_param_uses_git_minus_c(self):
        """When repo is provided, local SHA lookup uses git -C <repo>."""
        from pathlib import Path

        from mtor.dispatch import _check_worker_sha

        with (
            patch("mtor.dispatch.subprocess") as mock_sp,
            patch("mtor.dispatch._check_worker_checkout"),
        ):
            mock_sp.run.side_effect = [
                MagicMock(returncode=0, stdout="deadbeef\n"),
                MagicMock(returncode=0, stdout="deadbeef\n"),
            ]
            result = _check_worker_sha(repo="~/code/mtor")
        assert result is True
        local_call = mock_sp.run.call_args_list[0]
        cmd = local_call[0][0]
        assert cmd == ["git", "-C", str(Path("~/code/mtor").expanduser()), "rev-parse", "HEAD"]

    def test_repo_param_expands_tilde(self):
        """Tilde in repo path is expanded before passing to git -C."""
        from mtor.dispatch import _check_worker_sha

        with (
            patch("mtor.dispatch.subprocess") as mock_sp,
            patch("mtor.dispatch._check_worker_checkout"),
        ):
            mock_sp.run.side_effect = [
                MagicMock(returncode=0, stdout="deadbeef\n"),
                MagicMock(returncode=0, stdout="deadbeef\n"),
            ]
            result = _check_worker_sha(repo="~/germline")
        assert result is True
        local_call = mock_sp.run.call_args_list[0]
        cmd = local_call[0][0]
        expanded = cmd[2]
        assert not expanded.startswith("~"), f"tilde not expanded: {expanded!r}"
        assert "germline" in expanded


# ---------------------------------------------------------------------------
# Integration tests for --skip-sha-check CLI flag
# ---------------------------------------------------------------------------


class TestSkipShaCheckFlag:
    """Integration tests for --skip-sha-check on dispatch subcommands."""

    def test_default_with_skip_sha_check(self):
        """Default dispatch with --skip-sha-check dispatches successfully."""
        mock_client, _ = make_mock_client()
        with _patch_client(mock_client):
            exit_code, data = invoke(
                ["--skip-sha-check", "Make assays/test_feature.py pass"]
            )
        assert exit_code == 0
        assert data["ok"] is True
        assert data["result"]["status"] == "RUNNING"

    def test_scout_with_skip_sha_check(self):
        """Scout with --skip-sha-check dispatches successfully."""
        mock_client, _ = make_mock_client()
        with _patch_client(mock_client):
            exit_code, data = invoke(
                ["scout", "--no-wait", "--skip-sha-check", "Find all issues"]
            )
        assert exit_code == 0
        assert data["ok"] is True

    def test_research_with_skip_sha_check(self):
        """Research with --skip-sha-check dispatches successfully."""
        mock_client, _ = make_mock_client()
        with _patch_client(mock_client):
            exit_code, data = invoke(
                ["research", "--no-wait", "--skip-sha-check", "Compare frameworks"]
            )
        assert exit_code == 0
        assert data["ok"] is True

    def test_dispatch_without_skip_calls_sha_gate(self):
        """Default dispatch without flag calls _check_worker_sha(skip=False)."""
        from mtor import dispatch as dispatch_mod

        mock_client, _ = make_mock_client()
        with (
            _patch_client(mock_client),
            patch.object(
                dispatch_mod, "_check_worker_sha", return_value=True
            ) as mock_sha,
        ):
            exit_code, data = invoke(["Make assays/test_feature.py pass"])
        assert exit_code == 0
        mock_sha.assert_called_once_with(skip=False, repo=None)

    def test_dispatch_with_skip_passes_skip_true(self):
        """Default dispatch with --skip-sha-check passes skip=True to gate."""
        from mtor import dispatch as dispatch_mod

        mock_client, _ = make_mock_client()
        with (
            _patch_client(mock_client),
            patch.object(
                dispatch_mod, "_check_worker_sha", return_value=True
            ) as mock_sha,
        ):
            exit_code, data = invoke(
                ["--skip-sha-check", "Make assays/test_feature.py pass"]
            )
        assert exit_code == 0
        mock_sha.assert_called_once_with(skip=True, repo=None)

    def test_scout_skips_sha_gate(self):
        """Scout mode skips _check_worker_sha entirely (read-only task)."""
        from mtor import dispatch as dispatch_mod

        mock_client, _ = make_mock_client()
        with (
            _patch_client(mock_client),
            patch.object(
                dispatch_mod, "_check_worker_sha", return_value=True
            ) as mock_sha,
        ):
            invoke(["scout", "--no-wait", "Find all issues"])
        mock_sha.assert_not_called()

    def test_research_skips_sha_gate(self):
        """Research mode skips _check_worker_sha entirely (read-only task)."""
        from mtor import dispatch as dispatch_mod

        mock_client, _ = make_mock_client()
        with (
            _patch_client(mock_client),
            patch.object(
                dispatch_mod, "_check_worker_sha", return_value=True
            ) as mock_sha,
        ):
            invoke(["research", "--no-wait", "Compare frameworks"])
        mock_sha.assert_not_called()

    def test_default_spec_dispatch_passes_repo_to_sha_gate(self, tmp_path):
        """Default dispatch with --spec passes the spec's repo path to SHA gate."""
        from mtor import dispatch as dispatch_mod

        spec_file = tmp_path / "test-spec.md"
        spec_file.write_text(
            "---\n"
            "name: test-spec\n"
            "repo: /custom/repo/path\n"
            "scope:\n"
            "  - mtor\n"
            "tests:\n"
            "  run: pytest assays/test_sha_gate.py -q\n"
            "---\n"
            "Do something useful for the test spec.\n"
        )

        mock_client, _ = make_mock_client()
        with (
            _patch_client(mock_client),
            patch.object(
                dispatch_mod, "_check_worker_sha", return_value=True
            ) as mock_sha,
            patch("mtor.rptor.parse_spec", return_value={"repo": "/custom/repo/path"}),
            patch("mtor.cli.validate_spec", return_value=[]),
            patch("mtor.dispatch.validate_spec", return_value=[]),
        ):
            exit_code, data = invoke(["--spec", str(spec_file)])
        assert exit_code == 0
        mock_sha.assert_called_once_with(skip=False, repo="/custom/repo/path")


# ---------------------------------------------------------------------------
# Worker checkout hygiene tests
# ---------------------------------------------------------------------------


def _healthy_checkout():
    """Return a dict representing a healthy worker checkout."""
    return {
        "ok": True,
        "branch": "main",
        "origin": "https://github.com/vivesca/germline.git",
        "dirty": False,
        "status": "",
        "detail": "",
    }


class TestWorkerCheckout:
    """Tests for worker checkout hygiene gate."""

    def test_healthy_checkout_allows_in_sync_worker(self):
        """When checkout is healthy and SHAs match, _check_worker_sha returns True."""
        from mtor.dispatch import _check_worker_sha

        with (
            patch("mtor.dispatch.subprocess") as mock_sp,
            patch(
                "mtor.dispatch._worker_checkout_state", return_value=_healthy_checkout()
            ),
        ):
            mock_sp.run.side_effect = [
                MagicMock(returncode=0, stdout="abc123\n"),
                MagicMock(returncode=0, stdout="abc123\n"),
            ]
            result = _check_worker_sha()
        assert result is True

    def test_wrong_branch_raises(self):
        """Wrong branch on worker raises RuntimeError with 'worker checkout unhealthy'."""
        from mtor.dispatch import _check_worker_sha

        unhealthy = {
            "ok": False,
            "branch": "sortase-cleanup",
            "origin": "https://github.com/vivesca/germline.git",
            "dirty": False,
            "status": "",
            "detail": (
                "worker checkout unhealthy: "
                "branch is 'sortase-cleanup', expected 'main'"
            ),
        }
        with (
            patch("mtor.dispatch.subprocess") as mock_sp,
            patch("mtor.dispatch._worker_checkout_state", return_value=unhealthy),
            patch("mtor.dispatch._ci_push_lock_held", return_value=False),
        ):
            mock_sp.run.side_effect = [
                MagicMock(returncode=0, stdout="abc123\n"),
                MagicMock(returncode=0, stdout="abc123\n"),
            ]
            with pytest.raises(RuntimeError, match="worker checkout unhealthy"):
                _check_worker_sha()

    def test_wrong_origin_raises(self):
        """Wrong origin remote raises RuntimeError with 'worker checkout unhealthy'."""
        from mtor.dispatch import _check_worker_sha

        unhealthy = {
            "ok": False,
            "branch": "main",
            "origin": "https://github.com/vivesca/vivesca.git",
            "dirty": False,
            "status": "",
            "detail": (
                "worker checkout unhealthy: "
                "origin is 'https://github.com/vivesca/vivesca.git', "
                "expected 'https://github.com/vivesca/germline.git'"
            ),
        }
        with (
            patch("mtor.dispatch.subprocess") as mock_sp,
            patch("mtor.dispatch._worker_checkout_state", return_value=unhealthy),
            patch("mtor.dispatch._ci_push_lock_held", return_value=False),
        ):
            mock_sp.run.side_effect = [
                MagicMock(returncode=0, stdout="abc123\n"),
                MagicMock(returncode=0, stdout="abc123\n"),
            ]
            with pytest.raises(RuntimeError, match="worker checkout unhealthy"):
                _check_worker_sha()

    def test_dirty_untracked_status_raises(self):
        """Untracked/dirty files on worker raise RuntimeError."""
        from mtor.dispatch import _check_worker_sha

        unhealthy = {
            "ok": False,
            "branch": "main",
            "origin": "https://github.com/vivesca/germline.git",
            "dirty": True,
            "status": "?? stray_file.py\nM  modified.py",
            "detail": (
                "worker checkout unhealthy: "
                "dirty/untracked files: ?? stray_file.py\nM  modified.py"
            ),
        }
        with (
            patch("mtor.dispatch.subprocess") as mock_sp,
            patch("mtor.dispatch._worker_checkout_state", return_value=unhealthy),
            patch("mtor.dispatch._ci_push_lock_held", return_value=False),
        ):
            mock_sp.run.side_effect = [
                MagicMock(returncode=0, stdout="abc123\n"),
                MagicMock(returncode=0, stdout="abc123\n"),
            ]
            with pytest.raises(RuntimeError, match="worker checkout unhealthy"):
                _check_worker_sha()

    def test_worker_sha_plan_reports_unhealthy_checkout(self):
        """_worker_sha_plan includes worker_checkout.ok: false and error when unhealthy."""
        from mtor.dispatch import _worker_sha_plan

        unhealthy = {
            "ok": False,
            "branch": "sortase-cleanup",
            "origin": "https://github.com/vivesca/germline.git",
            "dirty": False,
            "status": "",
            "detail": (
                "worker checkout unhealthy: "
                "branch is 'sortase-cleanup', expected 'main'"
            ),
        }
        with (
            patch("mtor.dispatch.subprocess") as mock_sp,
            patch("mtor.dispatch._worker_checkout_state", return_value=unhealthy),
        ):
            mock_sp.run.side_effect = [
                MagicMock(returncode=0, stdout="abc123\n"),
                MagicMock(returncode=0, stdout="abc123\n"),
            ]
            plan = _worker_sha_plan()
        assert plan["worker_checkout"]["ok"] is False
        assert plan["error"] == unhealthy["detail"]
        assert "sortase-cleanup" in plan["worker_checkout"]["branch"]

    def test_worker_sha_plan_repo_param_uses_git_minus_c(self):
        """_worker_sha_plan(repo=...) uses git -C <repo> for local SHA lookup."""
        from mtor.dispatch import _worker_sha_plan

        with (
            patch("mtor.dispatch.subprocess") as mock_sp,
            patch(
                "mtor.dispatch._worker_checkout_state",
                return_value=_healthy_checkout(),
            ),
        ):
            mock_sp.run.side_effect = [
                MagicMock(returncode=0, stdout="deadbeef\n"),
                MagicMock(returncode=0, stdout="deadbeef\n"),
            ]
            plan = _worker_sha_plan(repo="/custom/repo")
        local_call = mock_sp.run.call_args_list[0]
        cmd = local_call[0][0]
        assert cmd == ["git", "-C", "/custom/repo", "rev-parse", "HEAD"]
        assert plan["local_sha"] == "deadbeef"
        assert plan["in_sync"] is True

    def test_worker_sha_plan_expands_tilde_in_repo(self):
        """_worker_sha_plan(repo='~/germline') passes expanded path to git -C."""
        from mtor.dispatch import _worker_sha_plan

        with (
            patch("mtor.dispatch.subprocess") as mock_sp,
            patch(
                "mtor.dispatch._worker_checkout_state",
                return_value=_healthy_checkout(),
            ),
        ):
            mock_sp.run.side_effect = [
                MagicMock(returncode=0, stdout="deadbeef\n"),
                MagicMock(returncode=0, stdout="deadbeef\n"),
            ]
            plan = _worker_sha_plan(repo="~/germline")
        local_call = mock_sp.run.call_args_list[0]
        cmd = local_call[0][0]
        expanded = cmd[2]
        assert not expanded.startswith("~"), f"tilde not expanded: {expanded!r}"
        assert "germline" in expanded
        assert plan["local_sha"] == "deadbeef"
        assert plan["in_sync"] is True


def _detached_checkout():
    """Checkout state as seen mid ci-push suite run: detached HEAD."""
    return {
        "ok": False,
        "branch": "HEAD",
        "origin": "https://github.com/vivesca/germline.git",
        "dirty": False,
        "status": "",
        "detail": "worker checkout unhealthy: branch is 'HEAD', expected 'main'",
    }


class TestCiPushLockWait:
    """Preflight must wait out an in-flight ci-push suite run, not hard-fail.

    2026-07-04: two pulse-review dispatches failed with "branch is 'HEAD'"
    purely because a ci-push suite run held /tmp/vivesca-ci-push.lock and had
    the worker checkout detached; the operator had to poll by hand.
    """

    def test_dispatch_during_suite_run_waits_then_succeeds(self):
        """Unhealthy + lock held, then lock released + healthy → no raise."""
        from mtor.dispatch import _check_worker_checkout

        with (
            patch(
                "mtor.dispatch._worker_checkout_state",
                side_effect=[_detached_checkout(), _healthy_checkout()],
            ),
            patch("mtor.dispatch._ci_push_lock_held", side_effect=[True, False]),
            patch("mtor.dispatch.time.sleep") as mock_sleep,
        ):
            _check_worker_checkout()
        assert mock_sleep.called

    def test_unhealthy_without_lock_fails_immediately(self):
        """No ci-push run in flight → the hard fail is immediate, no waiting."""
        from mtor.dispatch import _check_worker_checkout

        with (
            patch(
                "mtor.dispatch._worker_checkout_state",
                return_value=_detached_checkout(),
            ),
            patch("mtor.dispatch._ci_push_lock_held", return_value=False),
            patch("mtor.dispatch.time.sleep") as mock_sleep,
        ):
            with pytest.raises(RuntimeError, match="worker checkout unhealthy"):
                _check_worker_checkout()
        assert not mock_sleep.called

    def test_still_unhealthy_after_wait_raises_with_ci_push_hint(self):
        """Lock never clears and checkout stays broken → clear actionable error."""
        from mtor.dispatch import _check_worker_checkout

        with (
            patch(
                "mtor.dispatch._worker_checkout_state",
                return_value=_detached_checkout(),
            ),
            patch("mtor.dispatch._ci_push_lock_held", return_value=True),
            patch("mtor.dispatch.CI_PUSH_WAIT_S", 1),
            patch("mtor.dispatch.CI_PUSH_POLL_S", 0),
            patch("mtor.dispatch.time.sleep"),
        ):
            with pytest.raises(RuntimeError, match="ci-push suite run"):
                _check_worker_checkout()


# ---------------------------------------------------------------------------
# Unit tests for _worker_checkout_state
# ---------------------------------------------------------------------------


class TestWorkerCheckoutState:
    """Tests for _worker_checkout_state remote command parsing."""

    def test_healthy_worker_returns_ok_true(self):
        """Parses healthy subprocess output and returns ok: True."""
        from mtor.dispatch import _worker_checkout_state

        healthy_output = (
            "BRANCH:main\n"
            "ORIGIN:https://github.com/vivesca/germline.git\n"
            "MTOR_STATUS_START\n"
        )
        mock_result = MagicMock()
        mock_result.returncode = 0
        mock_result.stdout = healthy_output
        mock_result.stderr = ""

        with patch("mtor.dispatch.subprocess") as mock_sp:
            mock_sp.run.return_value = mock_result
            state = _worker_checkout_state()

        assert state["ok"] is True
        assert state["branch"] == "main"
        assert state["origin"] == "https://github.com/vivesca/germline.git"
        assert state["dirty"] is False
        assert state["detail"] == ""


class TestAutoDeployDrainGuard:
    """The auto-deploy restart refuses to kill in-flight ribosome work."""

    def test_auto_deploy_blocked_when_ribosomes_active(self):
        """Active ribosome processes block the restart with a loud error."""
        from mtor.dispatch import _check_worker_sha

        with (
            patch("mtor.dispatch.subprocess") as mock_sp,
            patch("mtor.dispatch.time"),
            patch("mtor.dispatch._check_worker_checkout"),
            patch("mtor.dispatch._count_active_ribosomes", return_value=2),
        ):
            mock_sp.run.side_effect = [
                MagicMock(returncode=0, stdout="aaa111\n"),  # local SHA
                MagicMock(returncode=0, stdout="bbb222\n"),  # remote SHA (diff)
                MagicMock(returncode=1, stdout=""),  # contains-local: NOT contained
                MagicMock(returncode=0, stdout=""),  # push
                MagicMock(returncode=0, stdout=""),  # merge
                MagicMock(
                    returncode=0, stdout="HEAD:aaa111\nCONTAINS:1\n"
                ),  # worker HEAD contains pushed
            ]
            with pytest.raises(RuntimeError, match="auto-deploy blocked"):
                _check_worker_sha()

        # The guard fired before any restart could be attempted.
        for call in mock_sp.run.call_args_list:
            argv = call[0][0]
            assert "systemctl --user restart mtor-worker" not in " ".join(
                str(part) for part in argv
            )

    def test_auto_deploy_fails_open_when_probe_unknown(self):
        """A failed probe (None) does not block: restart proceeds, returns True."""
        from mtor.dispatch import _check_worker_sha

        with (
            patch("mtor.dispatch.subprocess") as mock_sp,
            patch("mtor.dispatch.time"),
            patch("mtor.dispatch._check_worker_checkout"),
            patch("mtor.dispatch._count_active_ribosomes", return_value=None),
            patch("mtor.dispatch.restart_worker") as mock_restart,
        ):
            mock_sp.run.side_effect = [
                MagicMock(returncode=0, stdout="aaa111\n"),  # local SHA
                MagicMock(returncode=0, stdout="bbb222\n"),  # remote SHA (diff)
                MagicMock(returncode=1, stdout=""),  # contains-local: NOT contained
                MagicMock(returncode=0, stdout=""),  # push
                MagicMock(returncode=0, stdout=""),  # merge
                MagicMock(
                    returncode=0, stdout="HEAD:aaa111\nCONTAINS:1\n"
                ),  # worker HEAD contains pushed
            ]
            result = _check_worker_sha()

        assert result is True
        mock_restart.assert_called_once()
