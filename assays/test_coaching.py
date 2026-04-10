"""Tests for mtor.coaching — coaching loader for ribosome providers."""

from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from mtor.coaching import load_coaching


class TestLoadCoaching:
    """Tests for coaching.load_coaching."""

    def test_load_coaching_normal(self, tmp_path):
        """Load a coaching file within budget returns its content."""
        coaching_file = tmp_path / "coaching.md"
        coaching_file.write_text("## ribosome coaching\n- No hallucinated imports.")
        result = load_coaching(str(coaching_file), "zhipu", max_kb=10)
        assert result == "## ribosome coaching\n- No hallucinated imports."

    def test_load_coaching_over_budget(self, tmp_path):
        """Coaching file exceeding max_kb returns None."""
        coaching_file = tmp_path / "coaching.md"
        # 20KB file → exceeds 10KB default
        coaching_file.write_text("x" * 20 * 1024)
        result = load_coaching(str(coaching_file), "zhipu", max_kb=10)
        assert result is None

    def test_load_coaching_goose_returns_none(self, tmp_path):
        """Provider 'goose' always returns None (has its own instructions)."""
        coaching_file = tmp_path / "coaching.md"
        coaching_file.write_text("some coaching text")
        result = load_coaching(str(coaching_file), "goose", max_kb=10)
        assert result is None

    def test_load_coaching_codex_variant(self, tmp_path):
        """Provider 'codex' wraps coaching in <system-reminder> envelope."""
        coaching_file = tmp_path / "coaching.md"
        coaching_file.write_text("Be concise.")
        result = load_coaching(str(coaching_file), "codex", max_kb=10)
        assert result is not None
        assert result.startswith("<system-reminder>\n")
        assert result.endswith("\n</system-reminder>")
        assert "Be concise." in result

    def test_load_coaching_missing_file(self):
        """Missing coaching file returns None."""
        result = load_coaching("/nonexistent/path/coaching.md", "zhipu")
        assert result is None
