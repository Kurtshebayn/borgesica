"""Unit tests for OllamaProvider (M4-4).

No network calls — all tests use a FakeOpenAIClient injected via _client param.

Test mapping (tasks.md M4-4):
  1.  Protocol conformance — OllamaProvider satisfies TranslationProvider
  2.  Import purity — ollama_provider NOT in sys.modules under default DI wiring
  3.  Preset assertions — base_url, default_model, price table (zero-cost)

Strict TDD: tests written FIRST (RED), then implementation makes them GREEN.
"""
from __future__ import annotations

import json
import sys
from dataclasses import dataclass, field

import pytest

from borgesica.domain.ports import TranslationProvider


# ---------------------------------------------------------------------------
# Fake client (mirrors test_openai_compatible_provider.py exactly)
# ---------------------------------------------------------------------------

_VALID_UNIT_DATA = {
    "translation": "Hola mundo",
    "summary_update": "Un resumen de prueba.",
    "glossary_additions": [],
}

_TOOL_CALL_RESPONSE = {
    "id": "chatcmpl-fake",
    "object": "chat.completion",
    "choices": [
        {
            "index": 0,
            "message": {
                "role": "assistant",
                "content": None,
                "tool_calls": [
                    {
                        "id": "call_fake",
                        "type": "function",
                        "function": {
                            "name": "submit_translation",
                            "arguments": json.dumps(_VALID_UNIT_DATA),
                        },
                    }
                ],
            },
            "finish_reason": "tool_calls",
        }
    ],
}


@dataclass
class FakeOpenAIClient:
    """Minimal fake openai.OpenAI client for Ollama unit tests."""

    responses: list
    call_log: list = field(default_factory=list)
    _call_index: int = field(default=0, init=False, repr=False)

    def __post_init__(self):
        self.chat = _FakeChat(self)

    def _next_response(self, kwargs: dict):
        self.call_log.append(kwargs)
        resp = self.responses[self._call_index % len(self.responses)]
        self._call_index += 1
        if isinstance(resp, Exception):
            raise resp
        return _dict_to_chat_completion(resp)


@dataclass
class _FakeChat:
    _client: FakeOpenAIClient

    def __post_init__(self):
        self.completions = _FakeCompletions(self._client)


@dataclass
class _FakeCompletions:
    _client: FakeOpenAIClient

    def create(self, **kwargs):
        return self._client._next_response(kwargs)


def _dict_to_chat_completion(data: dict):
    """Convert a plain dict to a SimpleNamespace mirroring openai.ChatCompletion."""
    from types import SimpleNamespace

    def to_ns(obj):
        if isinstance(obj, dict):
            return SimpleNamespace(**{k: to_ns(v) for k, v in obj.items()})
        if isinstance(obj, list):
            return [to_ns(i) for i in obj]
        return obj

    return to_ns(data)


# ---------------------------------------------------------------------------
# Test 1: Protocol conformance
# ---------------------------------------------------------------------------


class TestOllamaProviderProtocol:
    def test_ollama_provider_satisfies_translation_provider_protocol(self):
        """OllamaProvider must pass runtime isinstance check against TranslationProvider."""
        from borgesica.adapters.providers.ollama_provider import OllamaProvider

        fake_client = FakeOpenAIClient(responses=[_TOOL_CALL_RESPONSE])
        provider = OllamaProvider(_client=fake_client)
        assert isinstance(provider, TranslationProvider)


# ---------------------------------------------------------------------------
# Test 2: Import purity — not in sys.modules under default Anthropic DI wiring
# ---------------------------------------------------------------------------


class TestOllamaProviderImportPurity:
    def test_ollama_provider_not_in_sys_modules_by_default(self):
        """With the default Anthropic DI configuration, importing anthropic_provider
        SHALL NOT pull ollama_provider into sys.modules.

        Mirrors the PyMuPDF import-purity test in test_pdf_reader.py exactly.
        The default DI path (api.py / __main__.py) wires AnthropicProvider;
        OllamaProvider is explicitly excluded from the default import graph.

        Note: we temporarily evict ollama_provider from sys.modules before the
        check to prevent pollution from other tests in this session that import it.
        After the assertion, we restore the module so other tests still work.
        """
        ollama_key = "borgesica.adapters.providers.ollama_provider"

        # Temporarily remove ollama_provider from sys.modules to get a clean slate
        evicted = sys.modules.pop(ollama_key, None)
        try:
            # Importing the default provider (Anthropic) must NOT pull in ollama_provider.
            import importlib
            import borgesica.adapters.providers.anthropic_provider as _anthro  # noqa: F401
            importlib.reload(_anthro)  # ensure a fresh import traversal

            assert ollama_key not in sys.modules, (
                "ollama_provider was unexpectedly imported into sys.modules. "
                "Check that no default import path references it."
            )
        finally:
            # Restore so other tests in this session aren't broken
            if evicted is not None:
                sys.modules[ollama_key] = evicted


# ---------------------------------------------------------------------------
# Test 3: Preset assertions — base_url, default_model, price=zero
# ---------------------------------------------------------------------------


class TestOllamaProviderPresets:
    def test_default_base_url_points_to_ollama_v1(self):
        """Default base_url must be the Ollama OpenAI-compatible endpoint."""
        from borgesica.adapters.providers.ollama_provider import OllamaProvider

        provider = OllamaProvider(_client=FakeOpenAIClient(responses=[]))
        assert provider.base_url == "http://localhost:11434/v1"

    def test_default_model_is_llama3(self):
        """Default model must be 'llama3'."""
        from borgesica.adapters.providers.ollama_provider import OllamaProvider

        provider = OllamaProvider(_client=FakeOpenAIClient(responses=[]))
        assert provider.default_model == "llama3"

    def test_price_returns_zero_tuple(self):
        """price() must return (0.0, 0.0) — local inference has no per-token API cost."""
        from borgesica.adapters.providers.ollama_provider import OllamaProvider

        provider = OllamaProvider(_client=FakeOpenAIClient(responses=[]))
        result = provider.price("llama3")
        assert result == (0.0, 0.0), f"Expected (0.0, 0.0), got {result!r}"

    def test_price_any_model_returns_zero(self):
        """price() returns (0.0, 0.0) for any model string (zero-cost table)."""
        from borgesica.adapters.providers.ollama_provider import OllamaProvider

        provider = OllamaProvider(_client=FakeOpenAIClient(responses=[]))
        result = provider.price("mistral:7b-instruct")
        assert result == (0.0, 0.0), f"Expected (0.0, 0.0), got {result!r}"

    def test_ollama_host_env_overrides_base_url(self, monkeypatch):
        """When OLLAMA_HOST is set, base_url uses that host instead of localhost."""
        monkeypatch.setenv("OLLAMA_HOST", "192.168.1.42:11434")
        from borgesica.adapters.providers import ollama_provider as _mod

        # Re-import with env set — create a fresh instance (not the module default)
        import importlib
        importlib.reload(_mod)
        OllamaProvider = _mod.OllamaProvider  # noqa: N806
        provider = OllamaProvider(_client=FakeOpenAIClient(responses=[]))
        assert "192.168.1.42:11434" in provider.base_url
        # Clean up: reload back to default state
        monkeypatch.delenv("OLLAMA_HOST", raising=False)
        importlib.reload(_mod)


def test_ollama_does_not_send_reasoning_effort():
    """Ollama must NOT inherit the OpenAI-compatible reasoning knob.

    The parent disables reasoning by default because deepseek-v4-flash spends
    its whole output budget on a reasoning trace. Ollama talks to local models
    through an OpenAI-compatible shim that rejects unknown parameters with a
    400 rather than ignoring them, so the knob must be omitted entirely.
    """
    from borgesica.adapters.providers.ollama_provider import OllamaProvider

    fake_client = FakeOpenAIClient(responses=[_TOOL_CALL_RESPONSE])
    provider = OllamaProvider(_client=fake_client)

    provider.translate("system", "Hello world", "llama3")

    assert provider.reasoning_effort is None
    assert "reasoning_effort" not in fake_client.call_log[0]
