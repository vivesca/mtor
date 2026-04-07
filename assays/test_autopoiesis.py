"""Tests for autopoiesis — error-classified auto-heal in TranslationWorkflow.

When a translation task fails, the workflow classifies the error and
automatically retries with adjusted parameters for retriable error types.

Error classifications:
  - rate_limit: HTTP 429 / quota exhaustion → retry with fallback provider
  - transient: network errors → retry once (same params)
  - timeout: process killed after time limit → retry with raw mode fallback
  - permanent: syntax errors, destruction patterns → no retry

Run: cd ~/code/mtor && uv run pytest assays/test_autopoiesis.py -v
"""

from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from mtor.worker.autopoiesis import HealAction, classify_error, get_heal_action


def _result(stderr: str = "", stdout: str = "", exit_code: int = 1, **kw) -> dict:
    return {"stderr": stderr, "stdout": stdout, "exit_code": exit_code, **kw}


def _review(flags: list[str] | None = None, **kw) -> dict:
    return {"flags": flags or [], **kw}


# ── classify_error ─────────────────────────────────────────────────────


class TestClassifyRateLimit:
    def test_http_429(self):
        assert (
            classify_error(_result(stderr="HTTP 429 Too Many Requests"), _review())
            == "rate_limit"
        )

    def test_quota_exceeded(self):
        assert classify_error(_result(stderr="AccountQuotaExceeded"), _review()) == "rate_limit"

    def test_rate_limit_in_stdout(self):
        assert (
            classify_error(_result(stdout="Error: rate limit exceeded"), _review())
            == "rate_limit"
        )

    def test_usage_limit(self):
        assert classify_error(_result(stderr="usage limit reached"), _review()) == "rate_limit"

    def test_too_many_requests(self):
        assert classify_error(_result(stderr="too many requests"), _review()) == "rate_limit"

    def test_priority_over_timeout(self):
        """Rate limit pattern is more specific than timeout — wins."""
        assert (
            classify_error(
                _result(stderr="timeout after retry. HTTP 429 rate limit hit."),
                _review(),
            )
            == "rate_limit"
        )


class TestClassifyPermanent:
    def test_destruction_flag(self):
        assert (
            classify_error(
                _result(exit_code=0, stdout="Done."),
                _review(flags=["destruction: rm -rf"]),
            )
            == "permanent"
        )

    def test_syntax_error_flag(self):
        assert (
            classify_error(
                _result(stderr="SyntaxError: bad syntax"),
                _review(flags=["errors: SyntaxError"]),
            )
            == "permanent"
        )

    def test_import_error_flag(self):
        assert (
            classify_error(
                _result(stderr="ImportError: no module"),
                _review(flags=["errors: ImportError"]),
            )
            == "permanent"
        )


class TestClassifyTimeout:
    def test_timeout_string(self):
        assert classify_error(_result(stderr="timeout after 30m"), _review()) == "timeout"

    def test_timed_out(self):
        assert classify_error(_result(stderr="Process timed out"), _review()) == "timeout"

    def test_negative_exit_code(self):
        """Negative exit code (SIGKILL etc.) without other patterns → timeout."""
        assert classify_error(_result(exit_code=-9), _review()) == "timeout"

    def test_deadline_exceeded(self):
        assert (
            classify_error(_result(stderr="context deadline exceeded"), _review()) == "timeout"
        )


class TestClassifyTransient:
    def test_connection_error(self):
        assert (
            classify_error(_result(stderr="ConnectionError: connection refused"), _review())
            == "transient"
        )

    def test_network_reset(self):
        assert (
            classify_error(_result(stderr="network error: connection reset by peer"), _review())
            == "transient"
        )

    def test_broken_pipe(self):
        assert classify_error(_result(stderr="broken pipe"), _review()) == "transient"

    def test_econnrefused(self):
        assert classify_error(_result(stderr="ECONNREFUSED"), _review()) == "transient"


class TestClassifyOther:
    def test_generic_failure_is_unknown(self):
        assert classify_error(_result(stderr="something bad"), _review()) == "unknown"

    def test_success_is_unknown(self):
        """Not an error → unknown (won't trigger heal)."""
        assert (
            classify_error(_result(exit_code=0, stdout="Done."), _review(flags=[])) == "unknown"
        )


# ── get_heal_action ────────────────────────────────────────────────────


class TestGetHealAction:
    def test_rate_limit_returns_fallback(self):
        action = get_heal_action("rate_limit", "zhipu")
        assert action is not None
        assert action.strategy == "fallback_provider"
        assert action.max_retries == 1
        assert action.fallback_provider is not None

    def test_transient_returns_retry_same(self):
        action = get_heal_action("transient", "zhipu")
        assert action is not None
        assert action.strategy == "retry_same"
        assert action.max_retries == 1

    def test_timeout_returns_raw_fallback(self):
        action = get_heal_action("timeout", "zhipu")
        assert action is not None
        assert action.strategy == "fallback_raw"
        assert action.max_retries == 1

    def test_permanent_returns_none(self):
        assert get_heal_action("permanent", "zhipu") is None

    def test_unknown_returns_none(self):
        assert get_heal_action("unknown", "zhipu") is None


# ── HealAction ─────────────────────────────────────────────────────────


class TestHealAction:
    def test_fields(self):
        action = HealAction(strategy="retry_same", max_retries=1, fallback_provider=None)
        assert action.strategy == "retry_same"
        assert action.max_retries == 1
        assert action.fallback_provider is None

    def test_str_includes_strategy(self):
        action = HealAction(
            strategy="fallback_provider", max_retries=1, fallback_provider="infini"
        )
        s = str(action)
        assert "fallback_provider" in s
        assert "infini" in s
