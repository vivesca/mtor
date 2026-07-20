"""Tests for mtor scout/research --repo flag and dispatch repo normalization."""

import inspect
from pathlib import Path

from mtor import cli as cli_module
from mtor.dispatch import _normalize_spec_repo_for_worker


def test_scout_cli_accepts_repo_flag():
    signature = inspect.signature(cli_module.scout)
    assert "repo" in signature.parameters
    assert signature.parameters["repo"].default is None
    assert "harness" in signature.parameters
    assert signature.parameters["harness"].default == ""


def test_research_cli_accepts_repo_flag():
    signature = inspect.signature(cli_module.research)
    assert "repo" in signature.parameters
    assert signature.parameters["repo"].default is None
    assert "harness" in signature.parameters
    assert signature.parameters["harness"].default == ""


def test_dispatch_repo_normalized_for_worker():
    absolute_repo = str(Path.home() / "code" / "mtor")
    normalized = _normalize_spec_repo_for_worker(absolute_repo)
    assert isinstance(normalized, str)
    assert normalized.startswith("~/")

    already_tilde = "~/code/mtor"
    assert _normalize_spec_repo_for_worker(already_tilde) == already_tilde
