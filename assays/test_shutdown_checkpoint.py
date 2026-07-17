"""Regression proof for the worker-shutdown worktree loss window."""

from __future__ import annotations

import asyncio
import inspect
import json
from pathlib import Path
import subprocess

import pytest

from mtor.worker import git_ops, translocase


def _git(repo: Path, *args: str) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        ["git", "-C", str(repo), *args],
        capture_output=True,
        text=True,
        check=False,
    )


def _make_repo(tmp_path: Path) -> Path:
    repo = tmp_path / "repo"
    repo.mkdir()
    _git(repo, "init", "-q", "-b", "main")
    _git(repo, "config", "user.email", "assay@example.com")
    _git(repo, "config", "user.name", "assay")
    (repo / "tracked.py").write_text("value = 1\n")
    _git(repo, "add", "tracked.py")
    _git(repo, "commit", "-q", "-m", "base")
    return repo


def test_shutdown_checkpoint_survives_retry_worktree_recreation(
    tmp_path: Path, monkeypatch
) -> None:
    repo = _make_repo(tmp_path)
    worktree = repo / ".worktrees" / "ribosome-shutdown"
    _git(repo, "worktree", "add", "-q", str(worktree), "-b", "ribosome-shutdown")
    (worktree / "tracked.py").write_text("value = 2\n")
    (worktree / "new_assay.py").write_text("preserved = True\n")

    checkpoint_dir = tmp_path / "checkpoints"
    monkeypatch.setattr(git_ops, "_CHECKPOINT_DIR", checkpoint_dir)
    saved = asyncio.run(
        translocase._checkpoint_shutdown_worktree(
            str(worktree),
            "ribosome-shutdown",
            task="preserve work during restart",
            provider="zhipu",
            exit_code=-15,
        )
    )
    assert saved

    recreated = git_ops._create_worktree(str(repo), "ribosome-shutdown")
    assert Path(recreated).is_dir()
    entries = [
        json.loads(path.read_text())
        for path in checkpoint_dir.glob("*-ribosome-shutdown.json")
    ]
    assert len(entries) == 2
    entry = next(item for item in entries if item["exit_code"] == -15)
    assert entry["stash_ref"] == saved
    assert _git(repo, "show-ref", "--verify", entry["git_ref"]).returncode == 0
    patch = Path(entry["patch_file"]).read_text()
    assert "value = 2" in patch
    assert "new_assay.py" in patch


def test_worker_shutdown_branch_delegates_to_ordered_retry_preparation() -> None:
    source = inspect.getsource(translocase.translate)
    prepared = source.index("if await _prepare_cancelled_retry(")
    reraised = source.index("\n                    raise\n", prepared)
    assert prepared < reraised


def test_timeout_reaps_and_checkpoints_before_returning_failure() -> None:
    source = inspect.getsource(translocase.translate)
    timeout_result = source.index('"stderr": "timeout after 30m"')
    checkpoint = source.rfind("_checkpoint_uncommitted_worktree", 0, timeout_result)
    reaper = source.rfind("_graceful_kill_group", 0, checkpoint)
    assert 0 <= reaper < checkpoint < timeout_result


def test_cancelled_retry_reaps_process_group_before_checkpoint(monkeypatch) -> None:
    events: list[str] = []

    class Proc:
        returncode = -15

    async def reap(proc, **kwargs) -> None:
        events.append("reaped")

    async def checkpoint(*args, **kwargs) -> str:
        assert events == ["reaped"]
        events.append("checkpointed")
        return "saved"

    monkeypatch.setattr(translocase, "_graceful_kill_group", reap)
    monkeypatch.setattr(translocase, "_checkpoint_shutdown_worktree", checkpoint)
    monkeypatch.setattr(translocase, "_worker_shutdown_requested", lambda: True)

    prepared = asyncio.run(
        translocase._prepare_cancelled_retry(
            Proc(),
            "/tmp/worktree",
            "wf-ordered",
            task="preserve",
            provider="zhipu",
        )
    )

    assert prepared is True
    assert events == ["reaped", "checkpointed"]


def test_operator_cancellation_also_checkpoints_before_returning(monkeypatch) -> None:
    events: list[str] = []

    class Proc:
        returncode = -15

    async def reap(proc, **kwargs) -> None:
        events.append("reaped")

    async def checkpoint(*args, **kwargs) -> str:
        assert events == ["reaped"]
        events.append("checkpointed")
        return "saved"

    monkeypatch.setattr(translocase, "_graceful_kill_group", reap)
    monkeypatch.setattr(translocase, "_checkpoint_shutdown_worktree", checkpoint)
    monkeypatch.setattr(translocase, "_worker_shutdown_requested", lambda: False)

    should_retry = asyncio.run(
        translocase._prepare_cancelled_retry(
            Proc(),
            "/tmp/worktree",
            "wf-operator-cancel",
            task="preserve explicit cancellation",
            provider="zhipu",
        )
    )

    assert should_retry is False
    assert events == ["reaped", "checkpointed"]


def test_surviving_process_group_prevents_shutdown_checkpoint(monkeypatch) -> None:
    checkpoint_called = False

    class Proc:
        returncode = -15

    async def failed_reap(proc, **kwargs) -> None:
        raise RuntimeError("process group survived SIGKILL")

    async def checkpoint(*args, **kwargs) -> str:
        nonlocal checkpoint_called
        checkpoint_called = True
        return "saved"

    monkeypatch.setattr(translocase, "_graceful_kill_group", failed_reap)
    monkeypatch.setattr(translocase, "_checkpoint_shutdown_worktree", checkpoint)
    monkeypatch.setattr(translocase, "_worker_shutdown_requested", lambda: True)

    with pytest.raises(RuntimeError, match="survived SIGKILL"):
        asyncio.run(
            translocase._prepare_cancelled_retry(
                Proc(),
                "/tmp/worktree",
                "wf-survivor",
                task="preserve",
                provider="zhipu",
            )
        )

    assert checkpoint_called is False


def test_unowned_worktree_process_blocks_cleanup_without_being_killed(
    monkeypatch,
) -> None:
    killed: list[int] = []
    monkeypatch.setattr(
        translocase,
        "_worktree_process_identities",
        lambda path: {4321: "identity"},
    )
    monkeypatch.setattr(translocase, "_attempt_process_identities", lambda attempt: {})
    monkeypatch.setattr(
        translocase,
        "_process_identity",
        lambda pid: "identity",
    )
    monkeypatch.setattr(translocase.os, "kill", lambda pid, sig: killed.append(pid))

    reaped = asyncio.run(
        translocase._reap_detached_descendants(
            {},
            "/tmp/worktree",
            "another-attempt",
            timeout=0.01,
        )
    )

    assert reaped is False
    assert killed == []


def test_owned_tree_is_reaped_before_unowned_worktree_user_blocks_cleanup(
    monkeypatch,
) -> None:
    identities = {111: "owned", 222: "unowned"}
    killed: list[int] = []
    monkeypatch.setattr(
        translocase,
        "_worktree_process_identities",
        lambda path: {222: "unowned"},
    )
    monkeypatch.setattr(translocase, "_attempt_process_identities", lambda attempt: {})
    monkeypatch.setattr(
        translocase,
        "_process_identity",
        lambda pid: identities.get(pid),
    )

    def kill(pid, sig):
        killed.append(pid)
        identities[pid] = None

    monkeypatch.setattr(translocase.os, "kill", kill)

    reaped = asyncio.run(
        translocase._reap_detached_descendants(
            {111: "owned"},
            "/tmp/worktree",
            "attempt",
            timeout=0.1,
        )
    )

    assert reaped is False
    assert killed == [111]


def test_attempt_process_census_does_not_match_sibling_retry(
    tmp_path: Path, monkeypatch
) -> None:
    proc_root = tmp_path / "proc"
    current = proc_root / "101"
    sibling = proc_root / "202"
    current.mkdir(parents=True)
    sibling.mkdir(parents=True)
    (current / "environ").write_bytes(b"RIBOSOME_ATTEMPT_ID=attempt-a\0")
    (sibling / "environ").write_bytes(b"RIBOSOME_ATTEMPT_ID=attempt-b\0")
    monkeypatch.setattr(translocase, "_process_identity", lambda pid: f"id-{pid}")

    identities = translocase._attempt_process_identities("attempt-a", proc_root)

    assert identities == {101: "id-101"}


def test_shutdown_checkpoint_fails_closed_when_dirty_work_cannot_be_saved(
    tmp_path: Path, monkeypatch
) -> None:
    repo = _make_repo(tmp_path)
    worktree = repo / ".worktrees" / "ribosome-checkpoint-failure"
    _git(
        repo,
        "worktree",
        "add",
        "-q",
        str(worktree),
        "-b",
        "ribosome-checkpoint-failure",
    )
    dirty = worktree / "valuable.py"
    dirty.write_text("preserve = True\n")
    monkeypatch.setattr(git_ops, "_checkpoint_worktree", lambda *args, **kwargs: None)

    with pytest.raises(RuntimeError, match="refusing destructive cleanup"):
        asyncio.run(
            translocase._checkpoint_shutdown_worktree(
                str(worktree),
                "ribosome-checkpoint-failure",
                task="preserve failed checkpoint",
                provider="zhipu",
                exit_code=-15,
            )
        )

    assert dirty.read_text() == "preserve = True\n"


def test_retry_recreation_preserves_dirty_worktree_when_checkpoint_fails(
    tmp_path: Path, monkeypatch
) -> None:
    repo = _make_repo(tmp_path)
    worktree = repo / ".worktrees" / "ribosome-retry-failure"
    _git(repo, "worktree", "add", "-q", str(worktree), "-b", "ribosome-retry-failure")
    dirty = worktree / "valuable.py"
    dirty.write_text("preserve = True\n")
    monkeypatch.setattr(git_ops, "_checkpoint_worktree", lambda *args, **kwargs: None)

    with pytest.raises(RuntimeError, match="refusing destructive cleanup"):
        git_ops._create_worktree(str(repo), "ribosome-retry-failure")

    assert worktree.is_dir()
    assert dirty.read_text() == "preserve = True\n"


def test_successful_run_preserves_dirt_when_auto_commit_failed(
    tmp_path: Path, monkeypatch
) -> None:
    repo = _make_repo(tmp_path)
    worktree = repo / ".worktrees" / "ribosome-auto-commit-failure"
    _git(
        repo,
        "worktree",
        "add",
        "-q",
        str(worktree),
        "-b",
        "ribosome-auto-commit-failure",
    )
    (worktree / "valuable file.py").write_text("preserve = True\n")
    checkpoint_dir = tmp_path / "checkpoints"
    monkeypatch.setattr(git_ops, "_CHECKPOINT_DIR", checkpoint_dir)

    saved = translocase._checkpoint_uncommitted_worktree(
        str(worktree),
        "ribosome-auto-commit-failure",
        auto_committed=False,
        task="successful subprocess with failed auto-commit",
        provider="zhipu",
        exit_code=0,
    )

    assert saved
    [metadata_path] = list(checkpoint_dir.glob("*-ribosome-auto-commit-failure.json"))
    entry = json.loads(metadata_path.read_text())
    assert entry["exit_code"] == 0
    assert _git(repo, "show", f"{saved}:valuable file.py").stdout == "preserve = True\n"


def test_retry_refuses_plain_nested_directory_without_touching_parent_git(
    tmp_path: Path,
) -> None:
    repo = _make_repo(tmp_path)
    stale = repo / ".worktrees" / "ribosome-plain-directory"
    stale.mkdir(parents=True)
    valuable = stale / "valuable.py"
    valuable.write_text("preserve = True\n")
    parent_lock = repo / ".git" / "index.lock"
    parent_lock.write_text("do not remove")

    with pytest.raises(RuntimeError, match="cannot inspect worktree"):
        git_ops._create_worktree(str(repo), "ribosome-plain-directory")

    assert valuable.read_text() == "preserve = True\n"
    assert parent_lock.read_text() == "do not remove"


def test_retry_refuses_to_touch_worktree_with_live_process(
    tmp_path: Path, monkeypatch
) -> None:
    repo = _make_repo(tmp_path)
    worktree = repo / ".worktrees" / "ribosome-live"
    _git(repo, "worktree", "add", "-q", str(worktree), "-b", "ribosome-live")
    dirty = worktree / "valuable.py"
    dirty.write_text("preserve = True\n")
    checkpoint_called = False

    def checkpoint(*args, **kwargs):
        nonlocal checkpoint_called
        checkpoint_called = True

    monkeypatch.setattr(git_ops, "_worktree_has_live_process", lambda path: True)
    monkeypatch.setattr(git_ops, "_checkpoint_worktree_or_raise", checkpoint)

    with pytest.raises(RuntimeError, match="still has a live process"):
        git_ops._create_worktree(str(repo), "ribosome-live")

    assert checkpoint_called is False
    assert dirty.read_text() == "preserve = True\n"


def test_retry_rechecks_for_process_entering_after_checkpoint(
    tmp_path: Path, monkeypatch
) -> None:
    repo = _make_repo(tmp_path)
    worktree = repo / ".worktrees" / "ribosome-late-process"
    _git(
        repo,
        "worktree",
        "add",
        "-q",
        str(worktree),
        "-b",
        "ribosome-late-process",
    )
    dirty = worktree / "valuable.py"
    dirty.write_text("preserve = True\n")
    calls: list[list[str]] = []

    monkeypatch.setattr(
        git_ops,
        "_worktree_has_live_process",
        lambda path: len(calls) > 0,
    )
    monkeypatch.setattr(
        git_ops,
        "_checkpoint_worktree_or_raise",
        lambda *args, **kwargs: calls.append(["checkpointed"]),
    )

    with pytest.raises(RuntimeError, match="live process"):
        git_ops._create_worktree(str(repo), "ribosome-late-process")

    assert calls == [["checkpointed"]]
    assert worktree.is_dir()
    assert dirty.read_text() == "preserve = True\n"
