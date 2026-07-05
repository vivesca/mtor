"""Tests for per-task opencode XDG_DATA_HOME/XDG_STATE_HOME isolation."""

from mtor.worker import translocase


def test_opencode_xdg_env_uses_per_task_dir():
    env = translocase._opencode_xdg_env("wf-abc123")
    assert "XDG_DATA_HOME" in env
    assert "XDG_STATE_HOME" in env
    assert "wf-abc123" in env["XDG_DATA_HOME"]
    assert "wf-abc123" in env["XDG_STATE_HOME"]
    assert env["XDG_DATA_HOME"] != env["XDG_STATE_HOME"]


def test_opencode_xdg_env_scoped_by_workflow_id():
    one = translocase._opencode_xdg_env("wf-one")
    two = translocase._opencode_xdg_env("wf-two")
    assert one["XDG_DATA_HOME"] != two["XDG_DATA_HOME"]


def test_opencode_xdg_gc_removes_old_dirs(tmp_path, monkeypatch):
    monkeypatch.setattr(translocase, "OPENCODE_XDG_BASE", tmp_path)
    target = tmp_path / "old-task"
    target.mkdir()
    import os

    old = _time_now() - 100000
    os.utime(target, (old, old))
    translocase._gc_opencode_xdg_dirs()
    assert not target.exists()


def test_opencode_xdg_gc_skips_recent_dirs(tmp_path, monkeypatch):
    monkeypatch.setattr(translocase, "OPENCODE_XDG_BASE", tmp_path)
    target = tmp_path / "recent-task"
    target.mkdir()
    translocase._gc_opencode_xdg_dirs()
    assert target.exists()


def test_opencode_xdg_gc_handles_missing_base_dir(tmp_path, monkeypatch):
    monkeypatch.setattr(translocase, "OPENCODE_XDG_BASE", tmp_path / "does-not-exist")
    translocase._gc_opencode_xdg_dirs()


def _time_now():
    import time

    return time.time()
