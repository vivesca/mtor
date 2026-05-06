"""Assays for doctor.py provider probe billing/auth classification.

Closes the diagnostic blind spot from 2026-05-06 zhipu detour: HTTPError handler
discarded response body, so 402 Coding-plan-expired was indistinguishable from
401/403/connection. Tests pin the classification rules and the body-reading path.
"""
from __future__ import annotations

import urllib.error
from io import BytesIO
from unittest.mock import patch

import pytest

from mtor.doctor import ProbeResult, _classify_response_error, _probe_provider


# --- Classifier unit tests --------------------------------------------------


def test_classify_402_as_billing():
    assert _classify_response_error(402, "") == "billing"


def test_classify_401_as_auth():
    assert _classify_response_error(401, "") == "auth"


def test_classify_429_as_quota():
    assert _classify_response_error(429, "") == "quota"


def test_classify_403_as_auth():
    assert _classify_response_error(403, "") == "auth"


def test_classify_chinese_billing_text_overrides_status():
    body = '{"code":10022,"msg":"Coding plan 套餐已到期，请续费。"}'
    assert _classify_response_error(200, body) == "billing"


def test_classify_english_billing_text():
    assert _classify_response_error(403, "Subscription expired, renew your plan") == "billing"


def test_classify_english_auth_text():
    assert _classify_response_error(500, "Invalid API key supplied") == "auth"


def test_classify_unknown_status():
    assert _classify_response_error(503, "Service unavailable") == "unknown"


def test_classify_connection_when_status_none():
    assert _classify_response_error(None, "") == "connection"


# --- Probe integration tests with mocked urlopen ----------------------------


def _mock_http_error(code: int, body: str, reason: str = "") -> urllib.error.HTTPError:
    body_bytes = body.encode("utf-8")
    return urllib.error.HTTPError(
        url="http://test",
        code=code,
        msg=reason or f"Status {code}",
        hdrs=None,
        fp=BytesIO(body_bytes),
    )


def test_probe_402_with_chinese_billing_body_classifies_billing():
    """The exact failure mode from 2026-05-06 — 402 with Chinese billing text."""
    err = _mock_http_error(
        402,
        '{"code":10022,"msg":"Coding plan 套餐已到期，请续费。","success":false}',
        "Payment Required",
    )
    with patch.dict("os.environ", {"ZHIPU_API_KEY": "fake"}):
        with patch("urllib.request.urlopen", side_effect=err):
            result = _probe_provider("zhipu")
    assert result.ok is False
    assert result.classification == "billing"
    assert "套餐" in result.detail or "402" in result.detail


def test_probe_missing_key_classifies_auth():
    with patch.dict("os.environ", {}, clear=True):
        result = _probe_provider("zhipu")
    assert result.ok is False
    assert result.classification == "auth"
    assert "ZHIPU_API_KEY" in result.detail


def test_probe_url_error_classifies_connection():
    with patch.dict("os.environ", {"ZHIPU_API_KEY": "fake"}):
        with patch("urllib.request.urlopen", side_effect=urllib.error.URLError("DNS failure")):
            result = _probe_provider("zhipu")
    assert result.ok is False
    assert result.classification == "connection"


def test_probe_dataclass_has_classification_default():
    """ProbeResult.classification defaults to 'unknown' when not supplied — back-compat for callers that built ProbeResult without the new field."""
    pr = ProbeResult(provider="zhipu", ok=True, latency_ms=10.0, detail="OK")
    assert pr.classification == "unknown"
