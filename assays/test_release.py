"""Assays for the mtor release workflow.

Spec: ~/epigenome/chromatin/loci/plans/mtor-pypi-release-workflow.md
"""

from __future__ import annotations

import io
import json
import subprocess
from pathlib import Path
from typing import Any

from mtor.cli import app


def invoke(args: list[str]) -> tuple[int, dict[str, Any]]:
    captured = io.StringIO()
    exit_code = 0
    import sys

    old_stdout = sys.stdout
    try:
        sys.stdout = captured
        app(args)
    except SystemExit as exc:
        exit_code = exc.code if isinstance(exc.code, int) else 1
    finally:
        sys.stdout = old_stdout

    return exit_code, json.loads(captured.getvalue())


def write_release_files(repo_root: Path, version: str = "0.7.4") -> None:
    (repo_root / "mtor").mkdir()
    (repo_root / "pyproject.toml").write_text(
        "\n".join(
            [
                "[project]",
                'name = "mtor"',
                'dynamic = [ "version" ]',
                "",
                "[tool.hatch.version]",
                'path = "mtor/__init__.py"',
                "",
            ]
        ),
        encoding="utf-8",
    )
    (repo_root / "mtor" / "__init__.py").write_text(
        f'VERSION = "{version}"\n',
        encoding="utf-8",
    )


def test_release_refuses_dirty_repo(tmp_path: Path, monkeypatch):
    write_release_files(tmp_path)
    monkeypatch.chdir(tmp_path)

    calls: list[list[str]] = []

    def fake_run(args: list[str], **kwargs):
        calls.append(args)
        if args == ["git", "status", "--porcelain"]:
            return subprocess.CompletedProcess(
                args, 0, stdout=" M mtor/cli.py\n", stderr=""
            )
        raise AssertionError(f"Unexpected subprocess call: {args}")

    monkeypatch.setattr("mtor.cli.subprocess.run", fake_run)

    exit_code, data = invoke(["release"])
    assert exit_code == 1
    assert data["ok"] is False
    assert data["error"]["code"] == "DIRTY_REPO"
    assert calls == [["git", "status", "--porcelain"]]


def test_release_bumps_version(tmp_path: Path, monkeypatch):
    write_release_files(tmp_path)
    monkeypatch.chdir(tmp_path)

    calls: list[list[str]] = []

    def fake_run(args: list[str], **kwargs):
        calls.append(args)
        if args == ["git", "status", "--porcelain"]:
            return subprocess.CompletedProcess(args, 0, stdout="", stderr="")
        return subprocess.CompletedProcess(args, 0, stdout="", stderr="")

    monkeypatch.setattr("mtor.cli.subprocess.run", fake_run)

    exit_code, data = invoke(["release"])
    assert exit_code == 0
    assert data["ok"] is True
    assert data["result"]["version"] == "0.7.5"
    assert data["result"]["tag"] == "v0.7.5"
    assert data["result"]["next_step"] == "ganglion: uv tool upgrade mtor"
    assert 'version = "0.7.5"' in (tmp_path / "pyproject.toml").read_text(
        encoding="utf-8"
    )
    assert 'VERSION = "0.7.5"' in (tmp_path / "mtor" / "__init__.py").read_text(
        encoding="utf-8"
    )
    assert calls == [
        ["git", "status", "--porcelain"],
        ["git", "add", "pyproject.toml", "mtor/__init__.py"],
        ["git", "commit", "-m", "chore: release v0.7.5"],
        ["git", "tag", "-a", "--no-sign", "v0.7.5", "-m", "release v0.7.5"],
        ["git", "push", "origin", "v0.7.5"],
        ["uv", "build"],
        ["uv", "publish"],
    ]
