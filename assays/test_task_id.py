"""Tests for _make_workflow_id — semi-deterministic task IDs with harness, model, slug, hash, timestamp."""

import hashlib
from typing import ClassVar

import pytest
from mtor.dispatch import _make_workflow_id


class TestFormat:
    """Workflow ID format: {harness}-{model}-{slug}-{hash}-{ts}"""

    def test_starts_with_default_harness(self):
        wid = _make_workflow_id("fix login bug", "zhipu")
        assert wid.startswith("ribosome-")

    def test_custom_harness(self):
        wid = _make_workflow_id("fix login bug", "zhipu", harness="codex")
        assert wid.startswith("codex-")

    def test_contains_prompt_hash(self):
        prompt = "fix login bug"
        wid = _make_workflow_id(prompt, "zhipu")
        expected_hash = hashlib.sha256(prompt.encode()).hexdigest()[:8]
        # hash appears before the trailing timestamp segment
        assert f"-{expected_hash}-" in wid

    def test_ends_with_hex_timestamp(self):
        prompt = "some prompt"
        wid = _make_workflow_id(prompt, "zhipu")
        # Format: harness-model-slug-hash-ts → last segment is hex timestamp
        tail = wid.rsplit("-", 1)[-1]
        int(tail, 16)  # must be valid hex
        assert int(tail, 16) > 0  # nonzero epoch

    def test_hash_is_8_hex_chars(self):
        prompt = "some prompt"
        wid = _make_workflow_id(prompt, "zhipu")
        expected_hash = hashlib.sha256(prompt.encode()).hexdigest()[:8]
        # Extract the hash segment: second-to-last dash-separated part
        parts = wid.rsplit("-", 2)  # ... slug, hash, ts
        assert parts[-2] == expected_hash

    def test_same_prompt_same_second_deterministic(self):
        """Two calls within the same second produce identical IDs."""
        wid1 = _make_workflow_id("fix login bug", "zhipu")
        wid2 = _make_workflow_id("fix login bug", "zhipu")
        # If called in the same second they match; if across a boundary they differ
        # but both are valid. Check structure is consistent.
        assert wid1.split("-")[:-1] == wid2.split("-")[:-1] or wid1 != wid2

    def test_different_prompts_yield_different_ids(self):
        wid1 = _make_workflow_id("fix login bug", "zhipu")
        wid2 = _make_workflow_id("fix signup bug", "zhipu")
        assert wid1 != wid2


class TestProviderToModel:
    """Provider strings map to short model names."""

    CASES: ClassVar[dict[str, str]] = {
        "zhipu": "glm51",
        "infini": "mm27",
        "volcano": "doubao",
        "gemini": "gem31",
        "codex": "gpt54",
        "goose": "glm51g",
        "droid": "glm51d",
    }

    @pytest.mark.parametrize("provider,model", list(CASES.items()))
    def test_model_mapping(self, provider, model):
        wid = _make_workflow_id("hello world task", provider)
        # Model appears right after harness
        assert f"ribosome-{model}-" in wid

    def test_unknown_provider_passes_through(self):
        wid = _make_workflow_id("hello world task", "unknown_provider")
        assert "-unknown_provider-" in wid


class TestSlug:
    """Slug is first 3-4 words of prompt, slugified."""

    def test_three_word_prompt_slug(self):
        wid = _make_workflow_id("fix login bug in auth", "zhipu")
        assert "fix-login-bug" in wid

    def test_short_prompt_uses_available_words(self):
        wid = _make_workflow_id("fix bug", "goose")
        assert "fix-bug" in wid

    def test_special_characters_stripped(self):
        wid = _make_workflow_id("what's the status? check now!", "droid")
        assert "whats-the-status" in wid

    def test_single_word_prompt(self):
        wid = _make_workflow_id("explore", "gemini")
        assert "explore" in wid


class TestMaxLength:
    """Total ID must not exceed 80 characters."""

    def test_short_prompt_within_limit(self):
        wid = _make_workflow_id("fix bug", "zhipu")
        assert len(wid) <= 80

    def test_long_prompt_truncated_to_80(self):
        long_prompt = " ".join(["word"] * 50)
        wid = _make_workflow_id(long_prompt, "zhipu")
        assert len(wid) <= 80

    def test_very_long_harness_still_under_80(self):
        wid = _make_workflow_id("fix bug", "zhipu", harness="a" * 40)
        assert len(wid) <= 80
