"""Tests for coaching file loading with size gate.

Runs via: cd ~/code/mtor && uv run pytest assays/test_coaching.py -v
"""

from __future__ import annotations

import pytest


class TestLoadCoaching:
    """Coaching file loading and validation."""

    def test_load_normal_file(self, tmp_path):
        """Normal coaching file is loaded and wrapped in tags."""
        from mtor.coaching import load_coaching

        coaching = tmp_path / "coaching.md"
        coaching.write_text("## Rules\n- Do X\n- Don't Y\n")

        result = load_coaching(coaching, harness="claude")
        assert result is not None
        assert "<coaching-notes>" in result
        assert "</coaching-notes>" in result
        assert "## Rules" in result

    def test_load_over_budget_raises(self, tmp_path):
        """Coaching file over 10KB raises ValueError."""
        from mtor.coaching import load_coaching

        coaching = tmp_path / "coaching.md"
        coaching.write_text("x" * 11_000)  # ~11KB

        with pytest.raises(ValueError, match="10"):
            load_coaching(coaching, harness="claude")

    def test_goose_returns_none(self, tmp_path):
        """Goose harness skips coaching (injects natively)."""
        from mtor.coaching import load_coaching

        coaching = tmp_path / "coaching.md"
        coaching.write_text("some coaching")

        assert load_coaching(coaching, harness="goose") is None

    def test_droid_returns_none(self, tmp_path):
        """Droid harness skips coaching (injects natively)."""
        from mtor.coaching import load_coaching

        coaching = tmp_path / "coaching.md"
        coaching.write_text("some coaching")

        assert load_coaching(coaching, harness="droid") is None

    def test_codex_variant(self, tmp_path):
        """Codex harness uses _codex variant if it exists."""
        from mtor.coaching import load_coaching

        main = tmp_path / "coaching.md"
        main.write_text("main coaching")
        codex = tmp_path / "coaching_codex.md"
        codex.write_text("codex coaching")

        result = load_coaching(main, harness="codex")
        assert result is not None
        assert "codex coaching" in result
        assert "main coaching" not in result

    def test_missing_file_returns_none(self):
        """Nonexistent coaching file returns None."""
        from pathlib import Path

        from mtor.coaching import load_coaching

        result = load_coaching(Path("/nonexistent/coaching.md"), harness="claude")
        assert result is None

    def test_none_path_returns_none(self):
        """None coaching path returns None."""
        from mtor.coaching import load_coaching

        assert load_coaching(None, harness="claude") is None

    def test_under_budget_no_error(self, tmp_path):
        """File under 10KB loads without error."""
        from mtor.coaching import load_coaching

        coaching = tmp_path / "coaching.md"
        coaching.write_text("x" * 5_000)  # 5KB

        result = load_coaching(coaching, harness="claude")
        assert result is not None
