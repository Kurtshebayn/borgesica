"""Live integration test for OllamaProvider (M4-4).

Gated by OLLAMA_HOST environment variable.
Marked @pytest.mark.integration — skipped in the default test run.

Run with:
    OLLAMA_HOST=localhost:11434 INTEGRATION=1 pytest tests/integration/test_ollama_provider_live.py -v

A running Ollama instance with at least one model pulled (e.g. "llama3") is
required for this test to pass.
"""
from __future__ import annotations

import os

import pytest
from pydantic import ValidationError

from borgesica.adapters.providers.ollama_provider import OllamaProvider
from borgesica.domain.models import TranslationResult, TranslationUnit


@pytest.mark.integration
class TestOllamaProviderLive:
    @pytest.mark.skipif(
        not os.environ.get("OLLAMA_HOST"),
        reason="OLLAMA_HOST not set — skipping live Ollama integration test",
    )
    def test_real_ollama_translate_returns_valid_unit(self):
        """Live call to a running Ollama instance → valid TranslationUnit.

        OLLAMA_HOST must be set to a reachable Ollama server (e.g. "localhost:11434").
        The test uses whatever model is specified via the OLLAMA_MODEL env var,
        defaulting to "llama3".
        """
        model = os.environ.get("OLLAMA_MODEL", "llama3")
        provider = OllamaProvider()  # picks up OLLAMA_HOST automatically

        try:
            result = provider.translate(
                system=(
                    "You are a translation engine. "
                    "Translate the given text to neutral Spanish (español neutro). "
                    "Return a JSON object with keys: translation, summary_update, glossary_additions."
                ),
                user="The quick brown fox jumps over the lazy dog.",
                model=model,
            )
        except ValidationError as exc:
            pytest.fail(f"OllamaProvider returned output that failed ValidationError: {exc}")

        assert isinstance(result, TranslationResult), "Expected a TranslationResult instance"
        assert result.unit.translation.strip(), "translation must be non-empty"
        assert result.unit.summary_update is not None
