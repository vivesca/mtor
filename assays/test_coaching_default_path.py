"""Tests for COACHING_PATH default resolution."""

from __future__ import annotations

import importlib
from pathlib import Path

import pytest


@pytest.fixture
def home_dir(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """Isolate HOME to a tmp dir so the default lookup is deterministic."""
    monkeypatch.setenv("HOME", str(tmp_path))
    monkeypatch.delenv("MTOR_COACHING_PATH", raising=False)
    return tmp_path


def _reload() -> object:
    import mtor
    importlib.reload(mtor)
    return mtor


def test_explicit_env_var_wins(home_dir: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    explicit = home_dir / "custom_coaching.md"
    explicit.write_text("custom")
    monkeypatch.setenv("MTOR_COACHING_PATH", str(explicit))

    mtor = _reload()
    assert mtor.COACHING_PATH == str(explicit)


def test_default_resolves_when_file_exists(home_dir: Path) -> None:
    default = home_dir / "epigenome" / "marks" / "feedback_ribosome_coaching.md"
    default.parent.mkdir(parents=True)
    default.write_text("default coaching")

    mtor = _reload()
    assert mtor.COACHING_PATH == str(default)


def test_default_is_none_when_file_missing(home_dir: Path) -> None:
    mtor = _reload()
    assert mtor.COACHING_PATH is None


def test_explicit_env_var_wins_over_existing_default(
    home_dir: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    default = home_dir / "epigenome" / "marks" / "feedback_ribosome_coaching.md"
    default.parent.mkdir(parents=True)
    default.write_text("default")
    explicit = home_dir / "explicit.md"
    explicit.write_text("explicit")
    monkeypatch.setenv("MTOR_COACHING_PATH", str(explicit))

    mtor = _reload()
    assert mtor.COACHING_PATH == str(explicit)


@pytest.fixture(autouse=True)
def _restore_module():
    """Reload mtor with real env after each test so other tests see real config."""
    yield
    import mtor
    importlib.reload(mtor)
