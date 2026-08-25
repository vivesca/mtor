"""Assays for doctor.py provider probe billing/auth classification.

Closes the diagnostic blind spot from 2026-05-06 zhipu detour: HTTPError handler
discarded response body, so 402 Coding-plan-expired was indistinguishable from
401/403/connection. Tests pin the classification rules and the body-reading path.
"""

from __future__ import annotations

import json
import os
import urllib.error
from io import BytesIO
from unittest.mock import patch

import pytest

from mtor.doctor import (
    ProbeResult,
    _check_coding_plan_lane,
    _check_opencode_config_payload,
    _classify_response_error,
    _probe_provider,
)

integration = pytest.mark.skipif(
    not os.environ.get("MTOR_INTEGRATION"),
    reason="integration: asserts against the real ~/code/mtor lockfile state. Set MTOR_INTEGRATION=1 to run.",
)


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
    assert (
        _classify_response_error(403, "Subscription expired, renew your plan")
        == "billing"
    )


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
        with patch(
            "urllib.request.urlopen", side_effect=urllib.error.URLError("DNS failure")
        ):
            result = _probe_provider("zhipu")
    assert result.ok is False
    assert result.classification == "connection"


def test_probe_dataclass_has_classification_default():
    """ProbeResult.classification defaults to 'unknown' when not supplied — back-compat for callers that built ProbeResult without the new field."""
    pr = ProbeResult(provider="zhipu", ok=True, latency_ms=10.0, detail="OK")
    assert pr.classification == "unknown"


# --- Coding-plan lane check ------------------------------------------------


@integration
def test_coding_plan_lane_pass_with_real_lockfile():
    """coding_plan_lane check should pass with the real lockfile."""
    result = _check_coding_plan_lane()
    assert result["ok"] is True
    assert result["budget_lane"] == "zhipu-coding-plan"
    assert result["base_url"] == "https://open.bigmodel.cn/api/anthropic"


def test_coding_plan_lane_rejects_api_z_ai(tmp_path):
    """coding_plan_lane check should reject api.z.ai mirror."""
    bad_config = {
        "_schema": 2,
        "providers": {
            "zhipu": {
                "key_env": "ZHIPU_API_KEY",
                "base_url": "https://api.z.ai/api/anthropic",
                "models": {
                    "opus": "glm-5.1",
                    "sonnet": "glm-5.1",
                    "haiku": "glm-4.5-air",
                },
            }
        },
    }
    config_file = tmp_path / "bad-lock.json"
    config_file.write_text(json.dumps(bad_config))
    result = _check_coding_plan_lane(str(config_file))
    assert result["ok"] is False
    assert "api.z.ai" in result["detail"]


# --- OpenCode config lane check --------------------------------------------


def _opencode_config() -> dict:
    return {
        "provider": {
            "zhipuai-coding-plan": {
                "options": {
                    "baseURL": "https://open.bigmodel.cn/api/coding/paas/v4",
                    "apiKey": "{env:ZHIPU_API_KEY}",
                }
            }
        },
        "model": "zhipuai-coding-plan/glm-5.3",
        "small_model": "zhipuai-coding-plan/glm-4.5-air",
        "permission": {"*": "allow", "external_directory": {"*": "allow"}},
    }


def test_opencode_config_payload_accepts_bigmodel_coding_plan():
    result = _check_opencode_config_payload(_opencode_config(), source="test")
    assert result["ok"] is True
    assert result["provider"] == "zhipuai-coding-plan"
    assert result["model"] == "zhipuai-coding-plan/glm-5.3"


def test_opencode_config_payload_rejects_old_provider_name():
    config = _opencode_config()
    config["provider"] = {
        "zhipu-coding": {
            "options": {
                "baseURL": "https://open.bigmodel.cn/api/coding/paas/v4",
                "apiKey": "{env:ZHIPU_API_KEY}",
            }
        }
    }

    result = _check_opencode_config_payload(config, source="test")

    assert result["ok"] is False
    assert "zhipu-coding" in result["detail"]


def test_opencode_config_payload_local_accepts_ask_permissions():
    """Local interactive config may default permissions to ask instead of allow."""
    config = _opencode_config()
    config["permission"] = {"*": "ask", "external_directory": {"*": "ask"}}

    result = _check_opencode_config_payload(config, source="local")

    assert result["ok"] is True


def test_opencode_config_payload_worker_rejects_ask_permissions():
    """Unattended worker config must keep allow; ask fails with the diagnostic."""
    config = _opencode_config()
    config["permission"] = {"*": "ask", "external_directory": {"*": "ask"}}

    result = _check_opencode_config_payload(config, source="worker")

    assert result["ok"] is False
    assert "permissions do not allow external_directory" in result["detail"]


def test_opencode_config_payload_rejects_literal_secrets():
    config = _opencode_config()
    config["provider"]["zhipuai-coding-plan"]["options"]["apiKey"] = "literal-secret"
    config["mcp"] = {
        "zread": {
            "headers": {
                "Authorization": "Bearer literal-secret-value",
            }
        }
    }

    result = _check_opencode_config_payload(config, source="test")

    assert result["ok"] is False
    assert "apiKey is not an env placeholder" in result["detail"]
    assert "Authorization is not an env placeholder" in result["detail"]
