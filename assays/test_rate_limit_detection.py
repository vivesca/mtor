"""Tests for rate-limit detection and auto-throttle in translocase.

Runs via: cd ~/code/mtor && uv run pytest assays/test_rate_limit_detection.py -x
"""

from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))


class TestDetectRateLimitError:
    """_detect_rate_limit_error scans text for 429/quota signals."""

    def test_http_429(self):
        from mtor.worker.translocase import _detect_rate_limit_error

        is_rl, wait = _detect_rate_limit_error("Error: 429 Too Many Requests")
        assert is_rl is True

    def test_rate_limit_keyword(self):
        from mtor.worker.translocase import _detect_rate_limit_error

        assert _detect_rate_limit_error("api rate limit exceeded")[0] is True

    def test_rate_limited_keyword(self):
        from mtor.worker.translocase import _detect_rate_limit_error

        assert _detect_rate_limit_error("You have been rate limited")[0] is True

    def test_rate_limit_hyphenated(self):
        from mtor.worker.translocase import _detect_rate_limit_error

        assert _detect_rate_limit_error("rate-limit policy triggered")[0] is True

    def test_rate_limit_underscore(self):
        from mtor.worker.translocase import _detect_rate_limit_error

        assert _detect_rate_limit_error("rate_limit_exceeded")[0] is True

    def test_quota_exceeded(self):
        from mtor.worker.translocase import _detect_rate_limit_error

        assert _detect_rate_limit_error("quota exceeded for this billing period")[0] is True

    def test_quota_exhausted(self):
        from mtor.worker.translocase import _detect_rate_limit_error

        assert _detect_rate_limit_error("API quota exhausted")[0] is True

    def test_quota_reached(self):
        from mtor.worker.translocase import _detect_rate_limit_error

        assert _detect_rate_limit_error("quota reached")[0] is True

    def test_resource_exhausted(self):
        from mtor.worker.translocase import _detect_rate_limit_error

        assert _detect_rate_limit_error("resource exhausted: too many concurrent requests")[0] is True

    def test_resource_depleted(self):
        from mtor.worker.translocase import _detect_rate_limit_error

        assert _detect_rate_limit_error("resource depleted")[0] is True

    def test_too_many_requests(self):
        from mtor.worker.translocase import _detect_rate_limit_error

        assert _detect_rate_limit_error("Too Many Requests")[0] is True

    def test_api_throttling(self):
        from mtor.worker.translocase import _detect_rate_limit_error

        assert _detect_rate_limit_error("API throttling active")[0] is True

    def test_requests_per_minute(self):
        from mtor.worker.translocase import _detect_rate_limit_error

        assert _detect_rate_limit_error("exceeded requests per minute limit")[0] is True

    def test_requests_per_second(self):
        from mtor.worker.translocase import _detect_rate_limit_error

        assert _detect_rate_limit_error("requests per second cap hit")[0] is True

    def test_request_was_throttled(self):
        from mtor.worker.translocase import _detect_rate_limit_error

        assert _detect_rate_limit_error("request was throttled")[0] is True

    def test_normal_error_not_detected(self):
        from mtor.worker.translocase import _detect_rate_limit_error

        is_rl, wait = _detect_rate_limit_error("SyntaxError: invalid syntax")
        assert is_rl is False
        assert wait is None

    def test_empty_string(self):
        from mtor.worker.translocase import _detect_rate_limit_error

        assert _detect_rate_limit_error("")[0] is False

    def test_suggested_wait_extracted(self):
        from mtor.worker.translocase import _detect_rate_limit_error

        is_rl, wait = _detect_rate_limit_error("429: retry after 60 seconds")
        assert is_rl is True
        assert wait == 60.0

    def test_no_wait_returns_none(self):
        from mtor.worker.translocase import _detect_rate_limit_error

        is_rl, wait = _detect_rate_limit_error("rate limit exceeded")
        assert is_rl is True
        assert wait is None


class TestExtractWaitSeconds:
    """_extract_wait_seconds parses retry-after durations from error text."""

    def test_retry_after_bare_number(self):
        from mtor.worker.translocase import _extract_wait_seconds

        assert _extract_wait_seconds("Retry-After: 30") == 30.0

    def test_retry_after_with_seconds_unit(self):
        from mtor.worker.translocase import _extract_wait_seconds

        assert _extract_wait_seconds("retry after 60 seconds") == 60.0

    def test_retry_in_minutes(self):
        from mtor.worker.translocase import _extract_wait_seconds

        assert _extract_wait_seconds("retry in 2 minutes") == 120.0

    def test_retry_after_min_unit(self):
        from mtor.worker.translocase import _extract_wait_seconds

        assert _extract_wait_seconds("retry after 5 min") == 300.0

    def test_cooldown_hours(self):
        from mtor.worker.translocase import _extract_wait_seconds

        assert _extract_wait_seconds("cooldown: 1h") == 3600.0

    def test_wait_seconds(self):
        from mtor.worker.translocase import _extract_wait_seconds

        assert _extract_wait_seconds("wait: 45s") == 45.0

    def test_no_match_returns_none(self):
        from mtor.worker.translocase import _extract_wait_seconds

        assert _extract_wait_seconds("no timing info here") is None

    def test_fractional_seconds(self):
        from mtor.worker.translocase import _extract_wait_seconds

        assert _extract_wait_seconds("retry after 2.5 seconds") == 2.5


class TestThrottleWait:
    """_throttle_wait calculates exponential backoff + jitter."""

    def test_first_attempt_base(self):
        from mtor.worker.translocase import (
            _THROTTLE_BASE_SECONDS,
            _throttle_wait,
        )

        wait = _throttle_wait(0)
        assert _THROTTLE_BASE_SECONDS * 0.9 <= wait <= _THROTTLE_BASE_SECONDS * 1.1

    def test_second_attempt_doubles(self):
        from mtor.worker.translocase import (
            _THROTTLE_BASE_SECONDS,
            _throttle_wait,
        )

        wait = _throttle_wait(1)
        expected = _THROTTLE_BASE_SECONDS * 2
        assert expected * 0.9 <= wait <= expected * 1.1

    def test_third_attempt_quadruples(self):
        from mtor.worker.translocase import (
            _THROTTLE_BASE_SECONDS,
            _throttle_wait,
        )

        wait = _throttle_wait(2)
        expected = _THROTTLE_BASE_SECONDS * 4
        assert expected * 0.9 <= wait <= expected * 1.1

    def test_suggested_overrides_backoff(self):
        from mtor.worker.translocase import _throttle_wait

        wait = _throttle_wait(5, suggested_seconds=60.0)
        assert 54.0 <= wait <= 66.0  # 60 ± 10%

    def test_suggested_capped_at_max(self):
        from mtor.worker.translocase import (
            _THROTTLE_MAX_SECONDS,
            _throttle_wait,
        )

        wait = _throttle_wait(0, suggested_seconds=9999.0)
        assert wait <= _THROTTLE_MAX_SECONDS * 1.1

    def test_backoff_capped_at_max(self):
        from mtor.worker.translocase import (
            _THROTTLE_MAX_SECONDS,
            _throttle_wait,
        )

        wait = _throttle_wait(20)  # would be 30 * 2^20 without cap
        assert wait <= _THROTTLE_MAX_SECONDS * 1.1

    def test_minimum_wait_is_one(self):
        from mtor.worker.translocase import _throttle_wait

        wait = _throttle_wait(0, suggested_seconds=0.1)
        assert wait >= 1.0

    def test_jitter_produces_variance(self):
        from mtor.worker.translocase import _throttle_wait

        waits = {_throttle_wait(2) for _ in range(20)}
        assert len(waits) > 1  # jitter should cause variation


class TestRateLimitPatternNoFalsePositives:
    """Verify the regex does not fire on normal output."""

    def test_exit_code_42_no_match(self):
        from mtor.worker.translocase import _RATE_LIMIT_PATTERNS

        assert not _RATE_LIMIT_PATTERNS.search("Exit code: 42")

    def test_normal_git_output(self):
        from mtor.worker.translocase import _RATE_LIMIT_PATTERNS

        assert not _RATE_LIMIT_PATTERNS.search("On branch main\nnothing to commit")

    def test_python_traceback(self):
        from mtor.worker.translocase import _RATE_LIMIT_PATTERNS

        assert not _RATE_LIMIT_PATTERNS.search(
            "Traceback (most recent call last):\n  File 'test.py', line 1"
        )

    def test_case_insensitive(self):
        from mtor.worker.translocase import _RATE_LIMIT_PATTERNS

        assert _RATE_LIMIT_PATTERNS.search("RATE LIMIT EXCEEDED")
        assert _RATE_LIMIT_PATTERNS.search("Rate Limit Exceeded")


# ---------------------------------------------------------------------------
# Retry-loop control flow (regression guard for 2026-04-11 runaway)
# ---------------------------------------------------------------------------


class TestRetryLoopExit:
    """translocase.translate()'s while-True retry loop must exit on non-rate-limit.

    Incident 2026-04-11: a single bad spec input (claude CLI rejecting a
    leading `---` as an unknown flag) caused translocase's retry loop to run
    59,356 iterations in 10 minutes, writing a 208 MB log. Root cause: the
    while True: loop had no unconditional break on the non-rate-limited
    path — only `continue` on rate-limit. Every fast subprocess failure
    looped at full CPU speed.

    These tests are structural — they read the translocase source directly
    and assert that the fix is present. They do not run the full workflow
    (which would require a live Temporal activity context).
    """

    def _read_translate_source(self) -> str:
        import inspect

        from mtor.worker import translocase

        return inspect.getsource(translocase.translate)

    def test_retry_loop_has_unconditional_break(self):
        """There must be at least one `break` at indent 8 in translate()."""
        source = self._read_translate_source()
        lines = source.splitlines()
        # Source from inspect.getsource is indented so that `async def` is at col 0
        # and the while-loop body is at col 8. An unconditional break on the
        # normal-exit path lives at col 8 (same indent as the while body).
        found_break = False
        in_while = False
        for line in lines:
            stripped = line.strip()
            if stripped.startswith("while True:"):
                in_while = True
                continue
            if in_while and line.startswith("    ") and not line.startswith("        "):
                # De-indented past the while body — loop ended
                break
            if in_while and line.startswith("        break") and not line.startswith("            "):
                found_break = True
                break
        assert found_break, (
            "translocase.translate()'s `while True:` loop is missing an "
            "unconditional break for the non-rate-limited exit path. Any "
            "fast-failing subprocess will loop at full CPU speed. See the "
            "2026-04-11 runaway incident."
        )

    def test_retry_loop_still_has_rate_limit_continue(self):
        """The rate-limit `continue` must still exist — the fix must not
        regress the circuit-breaker retry path."""
        source = self._read_translate_source()
        assert "continue" in source, (
            "rate-limit continue is missing — circuit-breaker retry path broken"
        )
        assert "EXIT_RATE_LIMITED" in source, (
            "EXIT_RATE_LIMITED gate missing — retry-on-rate-limit disabled"
        )
