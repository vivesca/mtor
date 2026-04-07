"""Tests for v2 stall detection — streaming-json action parsing.

v1 (current): diff-hash only. Kills after 5 consecutive identical hashes (~2.5 min).
v2 (this): parse CC's streaming-json output for 5 OpenHands stall patterns.
  Diff-hash becomes one signal among several, not the sole kill trigger.

NOTE: stall_detector module is not yet in mtor/worker — tests are stubs pending migration.
"""

from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "mtor" / "worker"))

# Skip all tests in this module if stall_detector is not available
_stall_detector_available = False
try:
    from mtor.worker.stall_detector import StallDetector, detect_stall_pattern

    _stall_detector_available = True
except ImportError:
    StallDetector = None  # type: ignore[assignment, misc]
    detect_stall_pattern = None  # type: ignore[assignment, misc]


import pytest

if not _stall_detector_available:
    pytestmark = pytest.mark.skip(reason="mtor.worker.stall_detector not yet available")


class TestStallPatternDetection:
    """Detect the 5 OpenHands stall patterns from streaming-json events."""

    def test_detect_repeated_action(self):
        """Same tool call 4+ times in a row = stall."""
        events = [
            {"type": "tool_use", "name": "Read", "input": {"path": "/foo/bar.py"}},
            {"type": "tool_use", "name": "Read", "input": {"path": "/foo/bar.py"}},
            {"type": "tool_use", "name": "Read", "input": {"path": "/foo/bar.py"}},
            {"type": "tool_use", "name": "Read", "input": {"path": "/foo/bar.py"}},
        ]
        result = detect_stall_pattern(events)  # type: ignore[union-attr]
        assert result is not None
        assert result["pattern"] == "repeated_action"

    def test_no_stall_on_different_actions(self):
        """Different tool calls are not a stall."""
        events = [
            {"type": "tool_use", "name": "Read", "input": {"path": "/foo/a.py"}},
            {"type": "tool_use", "name": "Edit", "input": {"path": "/foo/a.py"}},
            {"type": "tool_use", "name": "Read", "input": {"path": "/foo/a.py"}},
            {"type": "tool_use", "name": "Bash", "input": {"command": "pytest"}},
        ]
        result = detect_stall_pattern(events)  # type: ignore[union-attr]
        assert result is None

    def test_detect_repeated_error(self):
        """Same error message 3+ times = stall."""
        events = [
            {"type": "tool_result", "error": "ModuleNotFoundError: No module named 'foo'"},
            {"type": "tool_result", "error": "ModuleNotFoundError: No module named 'foo'"},
            {"type": "tool_result", "error": "ModuleNotFoundError: No module named 'foo'"},
        ]
        result = detect_stall_pattern(events)  # type: ignore[union-attr]
        assert result is not None
        assert result["pattern"] == "repeated_error"

    def test_detect_ping_pong(self):
        """Alternating between 2 actions 6+ cycles = stall."""
        events = []
        for _ in range(6):
            events.append({"type": "tool_use", "name": "Edit", "input": {"old": "a", "new": "b"}})
            events.append({"type": "tool_use", "name": "Edit", "input": {"old": "b", "new": "a"}})
        result = detect_stall_pattern(events)  # type: ignore[union-attr]
        assert result is not None
        assert result["pattern"] == "ping_pong"

    def test_detect_monologue(self):
        """Reasoning without acting 3+ times = stall."""
        events = [
            {"type": "text", "content": "Let me think about this..."},
            {"type": "text", "content": "Actually, I should consider..."},
            {"type": "text", "content": "On second thought, maybe..."},
        ]
        result = detect_stall_pattern(events)  # type: ignore[union-attr]
        assert result is not None
        assert result["pattern"] == "monologue"


class TestGraduatedResponse:
    """Stall detection uses graduated response — warn first, kill second."""

    def test_first_detection_is_warning(self):
        """First stall detection returns 'warn', not 'kill'."""
        detector = StallDetector()  # type: ignore[operator]
        action = detector.on_stall_detected({"pattern": "repeated_action"})
        assert action == "warn"

    def test_second_detection_is_kill(self):
        """Second stall detection after warning returns 'kill'."""
        detector = StallDetector()  # type: ignore[operator]
        detector.on_stall_detected({"pattern": "repeated_action"})
        action = detector.on_stall_detected({"pattern": "repeated_action"})
        assert action == "kill"

    def test_different_pattern_resets_warning(self):
        """A different stall pattern resets the warning counter."""
        detector = StallDetector()  # type: ignore[operator]
        detector.on_stall_detected({"pattern": "repeated_action"})
        action = detector.on_stall_detected({"pattern": "repeated_error"})
        assert action == "warn"  # Different pattern, reset


class TestDiffHashAsSupplementary:
    """Diff-hash is one signal, not the sole trigger."""

    def test_frozen_diff_alone_does_not_kill(self):
        """Frozen diff hash without other stall signals = warn only."""
        detector = StallDetector()  # type: ignore[operator]
        for _ in range(5):
            detector.record_diff_hash("abc123")
        action = detector.evaluate()
        assert action in ("warn", None)  # Not immediate kill

    def test_frozen_diff_plus_pattern_kills(self):
        """Frozen diff hash + detected stall pattern = kill."""
        detector = StallDetector()  # type: ignore[operator]
        for _ in range(5):
            detector.record_diff_hash("abc123")
        detector.on_stall_detected({"pattern": "repeated_action"})
        action = detector.evaluate()
        assert action == "kill"
