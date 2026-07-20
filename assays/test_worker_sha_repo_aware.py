"""Hermetic tests for repo-aware worker-side probes in _check_worker_sha.

The worker-side SSH probes (HEAD lookup, fetch+ff-merge, containment) must
target the repo resolved by _worker_addressable_repo_path(repo), not a
hardcoded ~/germline. Previously every worker probe was hardcoded to
~/germline, so dispatching a non-germline repo compared that repo's local
HEAD against germline's worker HEAD — germline could never contain a
foreign-repo commit, so no non-germline repo could be dispatched.

Run via: cd ~/code/mtor && uv run pytest assays/test_worker_sha_repo_aware.py -v
"""

from __future__ import annotations

from unittest.mock import MagicMock, patch

import pytest

from mtor.dispatch import (
    _check_worker_sha,
    _worker_addressable_repo_path,
    _worker_sha_plan,
)


def _ssh_remote_commands(mock_run):
    """Extract the remote shell-command strings from ssh subprocess.run calls.

    Local git lookups are ``["git", "-C", ..., "rev-parse", "HEAD"]`` (first
    element ``"git"``); worker probes are ``["ssh", WORKER_HOST, "<cmd>"]``
    (first element ``"ssh"``). Returns only the ``<cmd>`` tail of ssh calls,
    so assertions target worker-side probes specifically and never the
    local ``git -C`` lookup.
    """
    cmds = []
    for call in mock_run.call_args_list:
        argv = call[0][0]
        if argv and argv[0] == "ssh":
            cmds.append(argv[-1])
    return cmds


def _in_sync_side_effect():
    """Two-call sequence: local HEAD and worker HEAD both report the same SHA.

    With local == worker, _check_worker_sha returns True after the second
    call without entering the auto-deploy / containment path.
    """
    return [
        MagicMock(returncode=0, stdout="f45cdc62deadbeef\n"),
        MagicMock(returncode=0, stdout="f45cdc62deadbeef\n"),
    ]


class TestWorkerShaRepoAware:
    """Worker-side SSH probes must target the repo from _worker_addressable_repo_path."""

    def test_non_germline_repo_probes_target_repo(self):
        """repo=~/code/mtor: worker probes cd into /home/vivesca/code/mtor, never germline."""
        with (
            patch("mtor.dispatch.subprocess") as mock_sp,
            patch("mtor.dispatch._check_worker_checkout"),
        ):
            mock_sp.run.side_effect = _in_sync_side_effect()
            result = _check_worker_sha(repo="~/code/mtor")

        assert result is True
        ssh_cmds = _ssh_remote_commands(mock_sp.run)
        assert ssh_cmds, "expected at least one worker-side ssh probe"
        # At least one worker probe cds into the mtor worker checkout.
        assert any("/home/vivesca/code/mtor" in cmd for cmd in ssh_cmds), (
            f"worker probe did not target mtor repo: {ssh_cmds!r}"
        )
        # No worker-side probe should reference germline — the target repo is mtor.
        assert not any("germline" in cmd for cmd in ssh_cmds), (
            f"worker probe leaked germline cd: {ssh_cmds!r}"
        )

    def test_default_repo_still_uses_germline(self):
        """repo=None: worker probes fall back to /home/vivesca/germline (default behaviour)."""
        with (
            patch("mtor.dispatch.subprocess") as mock_sp,
            patch("mtor.dispatch._check_worker_checkout"),
        ):
            mock_sp.run.side_effect = _in_sync_side_effect()
            result = _check_worker_sha(repo=None)

        assert result is True
        ssh_cmds = _ssh_remote_commands(mock_sp.run)
        assert ssh_cmds, "expected at least one worker-side ssh probe"
        # The default germline worker checkout is used (behaviour preserved).
        assert any("/home/vivesca/germline" in cmd for cmd in ssh_cmds), (
            f"default repo did not probe germline: {ssh_cmds!r}"
        )

    def test_unaddressable_repo_falls_back_to_germline(self):
        """repo='.' is not worker-addressable; probes fall back to germline, not an empty cd."""
        # Sanity: the helper maps "." to "" (unaddressable).
        assert _worker_addressable_repo_path(".") == ""

        with (
            patch("mtor.dispatch.subprocess") as mock_sp,
            patch("mtor.dispatch._check_worker_checkout"),
        ):
            mock_sp.run.side_effect = _in_sync_side_effect()
            result = _check_worker_sha(repo=".")

        assert result is True
        ssh_cmds = _ssh_remote_commands(mock_sp.run)
        assert ssh_cmds, "expected at least one worker-side ssh probe"
        # Fallback is the germline worker dir — never an empty cd target.
        for cmd in ssh_cmds:
            assert "/home/vivesca/germline" in cmd, (
                f"unaddressable repo did not fall back to germline: {cmd!r}"
            )
            assert "cd ''" not in cmd, f"worker probe used an empty cd target: {cmd!r}"

    def test_unrecognized_real_repo_raises_clear_error(self):
        """repo='~/projects/foo' is a real path but not under ~/code or
        ~/germline -- must raise immediately with a clear message, NOT fall
        back to germline silently (unlike the '.' / '~' sentinel case above,
        which is intentional and must keep working)."""
        assert _worker_addressable_repo_path("~/projects/foo") == ""

        with patch("mtor.dispatch.subprocess") as mock_sp:
            with pytest.raises(RuntimeError, match="not addressable on the worker"):
                _check_worker_sha(repo="~/projects/foo")

        # Fails BEFORE any subprocess call -- no confusing SHA-mismatch retry loop,
        # no silent germline push.
        mock_sp.run.assert_not_called()


class TestGermlineGateScoping:
    """The germline checkout gate fires only for germline-targeted dispatches."""

    def test_repo_dispatch_skips_germline_checkout_gate(self):
        """repo=~/code/mtor must not block on germline checkout hygiene —
        a clean detached germline (ci-mad2 CI) is irrelevant to repo targets."""
        with (
            patch("mtor.dispatch.subprocess") as mock_sp,
            patch("mtor.dispatch._check_worker_checkout") as gate,
        ):
            mock_sp.run.side_effect = _in_sync_side_effect()
            result = _check_worker_sha(repo="~/code/mtor")

        assert result is True
        gate.assert_not_called()

    def test_germline_default_still_runs_checkout_gate(self):
        """repo=None falls back to germline and keeps the hygiene gate."""
        with (
            patch("mtor.dispatch.subprocess") as mock_sp,
            patch("mtor.dispatch._check_worker_checkout") as gate,
        ):
            mock_sp.run.side_effect = _in_sync_side_effect()
            result = _check_worker_sha(repo=None)

        assert result is True
        gate.assert_called_once()

    def test_germline_repo_path_still_runs_checkout_gate(self):
        """repo=~/germline targets germline explicitly and keeps the gate."""
        with (
            patch("mtor.dispatch.subprocess") as mock_sp,
            patch("mtor.dispatch._check_worker_checkout") as gate,
        ):
            mock_sp.run.side_effect = _in_sync_side_effect()
            result = _check_worker_sha(repo="~/germline")

        assert result is True
        gate.assert_called_once()


class TestWorkerShaPushRepoAware:
    """The auto-deploy push step must target the repo's LOCAL checkout, not
    always ~/germline — pushing germline's HEAD to germline's origin does
    nothing to get a non-germline target repo's commit onto its own
    origin/main."""

    def test_non_germline_repo_pushes_from_local_repo_path(self):
        """repo=~/code/mtor, out of sync: the local `git push` must -C into
        the mtor local checkout (mirroring local_cmd's -C above), never
        ~/germline."""
        from pathlib import Path

        with (
            patch("mtor.dispatch.subprocess") as mock_sp,
            patch("mtor.dispatch.time"),
            patch("mtor.dispatch._check_worker_checkout") as gate,
            patch("mtor.dispatch._count_active_ribosomes", return_value=0),
            patch("mtor.dispatch.restart_worker") as mock_restart,
        ):
            mock_sp.run.side_effect = [
                MagicMock(returncode=0, stdout="aaa111\n"),  # local SHA
                MagicMock(returncode=0, stdout="bbb222\n"),  # worker SHA (diff)
                MagicMock(
                    returncode=1, stdout=""
                ),  # contains-local probe: NOT contained → deploy
                MagicMock(returncode=0, stdout=""),  # push
                MagicMock(returncode=0, stdout=""),  # merge
                MagicMock(
                    returncode=0, stdout="HEAD:aaa111\nCONTAINS:1\n"
                ),  # worker HEAD contains pushed
            ]
            result = _check_worker_sha(repo="~/code/mtor")

        assert result is True
        gate.assert_not_called()
        mock_restart.assert_called_once()

        push_cmd = mock_sp.run.call_args_list[3][0][0]
        assert push_cmd[0] == "git"
        assert "push" in push_cmd
        push_dir = push_cmd[push_cmd.index("-C") + 1]
        assert push_dir == str(Path("~/code/mtor").expanduser())
        assert "germline" not in push_dir

    def test_default_repo_push_still_uses_germline(self):
        """repo=None: push still targets ~/germline (behaviour preserved)."""
        from pathlib import Path

        with (
            patch("mtor.dispatch.subprocess") as mock_sp,
            patch("mtor.dispatch.time"),
            patch("mtor.dispatch._check_worker_checkout"),
            patch("mtor.dispatch._count_active_ribosomes", return_value=0),
            patch("mtor.dispatch.restart_worker") as mock_restart,
        ):
            mock_sp.run.side_effect = [
                MagicMock(returncode=0, stdout="aaa111\n"),
                MagicMock(returncode=0, stdout="bbb222\n"),
                MagicMock(returncode=1, stdout=""),
                MagicMock(returncode=0, stdout=""),  # push
                MagicMock(returncode=0, stdout=""),  # merge
                MagicMock(returncode=0, stdout="HEAD:aaa111\nCONTAINS:1\n"),
            ]
            result = _check_worker_sha(repo=None)

        assert result is True
        mock_restart.assert_called_once()
        push_cmd = mock_sp.run.call_args_list[3][0][0]
        push_dir = push_cmd[push_cmd.index("-C") + 1]
        assert push_dir == str(Path.home() / "germline")


def _plan_ssh_remote_commands(mock_run):
    """Extract the remote shell-command strings from ssh subprocess.run calls.

    Same shape as _ssh_remote_commands but factored for the plan suite so the
    plan tests do not depend on the gate-suite helper.
    """
    cmds = []
    for call in mock_run.call_args_list:
        argv = call[0][0]
        if argv and argv[0] == "ssh":
            cmds.append(argv[-1])
    return cmds


class TestWorkerShaPlanRepoAware:
    """_worker_sha_plan() must predict the same repo decision as _check_worker_sha.

    The plan is the --explain safety surface — when it disagrees with the
    real gate, operators either skip the gate unnecessarily or trust a
    misleading "would deploy" verdict. These tests pin the plan's repo
    resolution, containment semantics, and read-only behaviour to the gate.
    """

    def test_non_germline_repo_probes_target_repo(self):
        """repo=~/code/mtor: every worker probe cds into /home/vivesca/code/mtor,
        never germline, and the germline checkout-hygiene probe is not invoked."""
        with (
            patch("mtor.dispatch.subprocess") as mock_sp,
            patch("mtor.dispatch._worker_checkout_state") as checkout_state,
        ):
            mock_sp.run.side_effect = [
                MagicMock(returncode=0, stdout="f45cdc62deadbeef\n"),  # local
                MagicMock(returncode=0, stdout="f45cdc62deadbeef\n"),  # worker
            ]
            plan = _worker_sha_plan(repo="~/code/mtor")

        assert plan["in_sync"] is True
        ssh_cmds = _plan_ssh_remote_commands(mock_sp.run)
        assert ssh_cmds, "expected at least one worker-side ssh probe"
        assert all("/home/vivesca/code/mtor" in cmd for cmd in ssh_cmds), (
            f"worker probe did not target mtor repo: {ssh_cmds!r}"
        )
        assert not any("germline" in cmd for cmd in ssh_cmds), (
            f"worker probe leaked germline cd: {ssh_cmds!r}"
        )
        # Non-germline target: germline checkout hygiene must NOT be reported.
        checkout_state.assert_not_called()

    def test_default_repo_still_uses_germline_and_reports_checkout(self):
        """repo=None: worker probes fall back to /home/vivesca/germline and
        the germline checkout-hygiene report is still produced."""
        with (
            patch("mtor.dispatch.subprocess") as mock_sp,
            patch("mtor.dispatch._worker_checkout_state") as checkout_state,
        ):
            checkout_state.return_value = {
                "ok": True,
                "branch": "main",
                "origin": "git@github.com:vivesca/germline.git",
                "dirty": False,
                "status": "",
                "detail": "",
            }
            mock_sp.run.side_effect = [
                MagicMock(returncode=0, stdout="f45cdc62deadbeef\n"),  # local
                MagicMock(returncode=0, stdout="f45cdc62deadbeef\n"),  # worker
            ]
            plan = _worker_sha_plan(repo=None)

        assert plan["in_sync"] is True
        ssh_cmds = _plan_ssh_remote_commands(mock_sp.run)
        assert ssh_cmds, "expected at least one worker-side ssh probe"
        assert any("/home/vivesca/germline" in cmd for cmd in ssh_cmds), (
            f"default repo did not probe germline: {ssh_cmds!r}"
        )
        checkout_state.assert_called_once()
        # The reported checkout reflects the germline probe, not the OK default.
        assert plan["worker_checkout"]["branch"] == "main"

    def test_exact_equality_reports_in_sync(self):
        """local == worker: in_sync=True, auto_deploy_would_occur=False, and
        no containment probe is issued (single HEAD round-trip)."""
        with (
            patch("mtor.dispatch.subprocess") as mock_sp,
            patch("mtor.dispatch._worker_checkout_state"),
        ):
            mock_sp.run.side_effect = [
                MagicMock(returncode=0, stdout="abc123\n"),  # local
                MagicMock(returncode=0, stdout="abc123\n"),  # worker (equal)
            ]
            plan = _worker_sha_plan(repo=None)

        assert plan["in_sync"] is True
        assert plan["auto_deploy_would_occur"] is False
        assert plan["local_sha"] == "abc123"
        assert plan["worker_sha"] == "abc123"
        assert plan["error"] == ""
        # Only the two HEAD probes — no containment lookup when SHAs match.
        assert mock_sp.run.call_count == 2

    def test_worker_ahead_containment_reports_in_sync(self):
        """Worker HEAD moved past local (sync timer pulled origin) but still
        CONTAINS local — must report in_sync=True, auto_deploy_would_occur=False,
        mirroring _check_worker_sha's containment short-circuit."""
        with (
            patch("mtor.dispatch.subprocess") as mock_sp,
            patch("mtor.dispatch._worker_checkout_state"),
        ):
            mock_sp.run.side_effect = [
                MagicMock(returncode=0, stdout="abc123\n"),  # local
                MagicMock(returncode=0, stdout="def456\n"),  # worker (ahead)
                MagicMock(returncode=0, stdout=""),  # merge-base --is-ancestor: contained
            ]
            plan = _worker_sha_plan(repo=None)

        assert plan["in_sync"] is True
        assert plan["auto_deploy_would_occur"] is False
        assert plan["local_sha"] == "abc123"
        assert plan["worker_sha"] == "def456"
        # The containment probe targeted the same worker repo as the HEAD lookup.
        ssh_cmds = _plan_ssh_remote_commands(mock_sp.run)
        assert any("merge-base --is-ancestor" in cmd for cmd in ssh_cmds), (
            f"expected containment probe: {ssh_cmds!r}"
        )

    def test_non_containment_reports_deploy_would_occur(self):
        """Worker HEAD does NOT contain local (local ahead with unpushed
        commits, or histories diverged) — deployment would occur."""
        with (
            patch("mtor.dispatch.subprocess") as mock_sp,
            patch("mtor.dispatch._worker_checkout_state"),
        ):
            mock_sp.run.side_effect = [
                MagicMock(returncode=0, stdout="abc123\n"),  # local
                MagicMock(returncode=0, stdout="def456\n"),  # worker
                # merge-base --is-ancestor exits non-zero (1 = not ancestor,
                # 128 = local SHA not a worker object) — both mean deploy.
                MagicMock(returncode=1, stdout=""),
            ]
            plan = _worker_sha_plan(repo="~/code/mtor")

        assert plan["in_sync"] is False
        assert plan["auto_deploy_would_occur"] is True

    def test_plan_is_strictly_read_only(self):
        """The plan must never issue a mutating subprocess command against
        either checkout — no push, merge, ff-only, restart, reset, or
        rebase. The dry-run surface must not mutate."""
        with (
            patch("mtor.dispatch.subprocess") as mock_sp,
            patch("mtor.dispatch._worker_checkout_state"),
        ):
            # Worker diverges and does not contain local — the most the plan
            # can do is read; the gate would auto-deploy here, the plan must not.
            mock_sp.run.side_effect = [
                MagicMock(returncode=0, stdout="abc123\n"),  # local HEAD
                MagicMock(returncode=0, stdout="def456\n"),  # worker HEAD
                MagicMock(returncode=1, stdout=""),  # containment: not contained
            ]
            plan = _worker_sha_plan(repo="~/code/mtor")

        assert plan["auto_deploy_would_occur"] is True
        ssh_cmds = _plan_ssh_remote_commands(mock_sp.run)
        mutating_markers = (
            "push ",
            "push\t",
            "merge --ff-only",
            "merge --ff",
            "fetch origin",
            "reset --",
            "rebase ",
            "checkout -B",
            "restart",
        )
        for cmd in ssh_cmds:
            for marker in mutating_markers:
                assert marker not in cmd, (
                    f"read-only plan issued mutating command "
                    f"({marker!r}): {cmd!r}"
                )
