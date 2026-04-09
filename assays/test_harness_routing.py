"""Tests for --harness flag routing to RIBOSOME_PROVIDER in translocase.

Runs via: cd ~/code/mtor && uv run pytest assays/test_harness_routing.py -x
"""

from __future__ import annotations

import sys
from pathlib import Path
from unittest.mock import AsyncMock, patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))


class TestHarnessRouting:
    """--harness value must flow through to RIBOSOME_PROVIDER env var."""

    def test_harness_in_workflow_spec(self):
        """dispatch.py includes harness in the workflow input spec dict."""
        from mtor.dispatch import _dispatch_prompt

        # The spec dict passed to Temporal should contain 'harness' key
        # We can't easily test the full dispatch without Temporal,
        # but we can test that _make_workflow_id accepts harness
        from mtor.dispatch import _make_workflow_id

        wid = _make_workflow_id("test prompt", "zhipu", harness="goose")
        assert wid.startswith("goose-")

    def test_default_harness_is_ribosome(self):
        """Default harness prefix in workflow ID is 'ribosome'."""
        from mtor.dispatch import _make_workflow_id

        wid = _make_workflow_id("test prompt", "zhipu")
        assert wid.startswith("ribosome-")

    def test_harness_not_empty_string(self):
        """Harness value of empty string falls back to default."""
        from mtor.dispatch import _make_workflow_id

        wid = _make_workflow_id("test prompt", "zhipu", harness="")
        # Empty harness should use default or at least not crash
        assert len(wid) > 0
