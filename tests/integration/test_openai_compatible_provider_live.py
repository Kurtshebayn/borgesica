"""Live integration test for OpenAICompatibleProvider (M4-3, openai-provider).

Gated by DEEPSEEK_API_KEY / OPENAI_API_KEY environment variables (each test
skips independently when its key is unset).
Marked @pytest.mark.integration — skipped in the default test run.

Run with:
    DEEPSEEK_API_KEY=<key> INTEGRATION=1 pytest tests/integration/test_openai_compatible_provider_live.py -v
    OPENAI_API_KEY=<key> INTEGRATION=1 pytest tests/integration/test_openai_compatible_provider_live.py -v
"""
from __future__ import annotations

import os

import pytest
from pydantic import ValidationError

from borgesica.adapters.providers.openai_compatible_provider import OpenAICompatibleProvider
from borgesica.domain.models import TranslationResult, TranslationUnit


@pytest.mark.integration
class TestOpenAICompatibleProviderLive:
    @pytest.mark.skipif(
        not os.environ.get("DEEPSEEK_API_KEY"),
        reason="DEEPSEEK_API_KEY not set — skipping live DeepSeek integration test",
    )
    def test_real_deepseek_translate_returns_valid_unit(self):
        """Live call to DeepSeek → valid TranslationUnit with non-empty translation."""
        api_key = os.environ["DEEPSEEK_API_KEY"]
        provider = OpenAICompatibleProvider.deepseek(api_key=api_key)

        result = provider.translate(
            system=(
                "You are a translation engine. "
                "Translate the given text to neutral Spanish (español neutro). "
                "Return a JSON object with keys: translation, summary_update, glossary_additions."
            ),
            user="The quick brown fox jumps over the lazy dog.",
            model="deepseek-v4-flash",
        )

        assert isinstance(result, TranslationResult), "Expected a TranslationResult instance"
        assert result.unit.translation.strip(), "translation must be non-empty"
        assert result.unit.summary_update is not None
        assert result.usage.input_tokens >= 0, "usage.input_tokens must be non-negative"
        assert result.usage.output_tokens >= 0, "usage.output_tokens must be non-negative"
        # No ValidationError should be raised (validated inside TranslationUnit.model_validate)

    @pytest.mark.skipif(
        not os.environ.get("OPENAI_API_KEY"),
        reason="OPENAI_API_KEY not set — skipping live OpenAI integration test",
    )
    def test_real_openai_translate_returns_valid_unit(self):
        """Live call to OpenAI (.openai() preset) → valid TranslationUnit with
        non-empty translation (spec: 'Preset produces a working provider')."""
        api_key = os.environ["OPENAI_API_KEY"]
        provider = OpenAICompatibleProvider.openai(api_key=api_key)

        result = provider.translate(
            system=(
                "You are a translation engine. "
                "Translate the given text to neutral Spanish (español neutro). "
                "Return a JSON object with keys: translation, summary_update, glossary_additions."
            ),
            user="The quick brown fox jumps over the lazy dog.",
            model="gpt-5.6-luna",
        )

        assert isinstance(result, TranslationResult), "Expected a TranslationResult instance"
        assert result.unit.translation.strip(), "translation must be non-empty"
        assert result.unit.summary_update is not None
        assert result.usage.input_tokens >= 0, "usage.input_tokens must be non-negative"
        assert result.usage.output_tokens >= 0, "usage.output_tokens must be non-negative"
        # No ValidationError should be raised (validated inside TranslationUnit.model_validate)
