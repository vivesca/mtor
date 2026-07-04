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

from mtor.dispatch import _check_worker_sha, _worker_addressable_repo_path


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
