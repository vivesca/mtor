"""Hermetic tests for the mtor ganglion landing-target banner helper."""

from mtor.worker.git_ops import _format_landing_banner


def test_build_run_shows_branch_and_base():
    """Build run (worktree set, mode=build): banner carries branch + short SHA."""
    banner = _format_landing_banner(
        repo_root="/home/vivesca/code/mtor",
        work_dir="/home/vivesca/code/mtor/.worktrees/ribosome-abc",
        branch_name="ribosome-abc",
        worktree_path="/home/vivesca/code/mtor/.worktrees/ribosome-abc",
        mode="build",
        pre_sha="0123456789ab4567",
    )
    assert banner.startswith("[translocase]")
    assert "landing target:" in banner
    assert "/home/vivesca/code/mtor" in banner
    assert "ribosome-abc" in banner
    assert "0123456789ab" in banner
    assert "0123456789ab4567" not in banner
    assert "/home/vivesca/code/mtor/.worktrees/ribosome-abc" in banner
    assert "\n" not in banner


def test_scout_run_marks_read_only_main():
    """Scout run (no worktree): banner shows read-only main, hides build branch."""
    banner = _format_landing_banner(
        repo_root="/home/vivesca/code/mtor",
        work_dir="/home/vivesca/code/mtor",
        branch_name="ribosome-abc",
        worktree_path=None,
        mode="scout",
        pre_sha="0123456789ab4567",
    )
    assert "main (read-only scout)" in banner
    assert "ribosome-abc" not in banner


def test_missing_base_sha_does_not_break():
    """Missing pre_sha degrades to 'unknown' literal, never raises."""
    banner = _format_landing_banner(
        repo_root="/home/vivesca/code/mtor",
        work_dir="/home/vivesca/code/mtor/.worktrees/ribosome-abc",
        branch_name="ribosome-abc",
        worktree_path="/home/vivesca/code/mtor/.worktrees/ribosome-abc",
        mode="build",
        pre_sha=None,
    )
    assert "unknown" in banner
    assert "\n" not in banner


def test_grep_prefix_matches_existing_worker_lines():
    """Banner uses the [translocase] prefix used by other worker stderr lines."""
    banner = _format_landing_banner(
        repo_root="/r",
        work_dir="/w",
        branch_name="ribosome-x",
        worktree_path="/w",
        mode="build",
        pre_sha="abcdef123456",
    )
    assert banner.startswith("[translocase]")
