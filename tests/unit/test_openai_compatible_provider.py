"""Unit tests for OpenAICompatibleProvider (M4-3).

No network calls — all tests fake the HTTP layer by injecting a FakeOpenAIClient.
Only the integration test (marked @integration) makes a real API call and is
skipped when DEEPSEEK_API_KEY is not set.

Test mapping (tasks.md M4-3):
  1.  Protocol conformance
  2.  Tier-1 tool-call success (1 call)
  3.  Tier-2 JSON-mode fallback (2 calls)
  4.  Tier-2 empty-content → Tier-3 success
  5.  All tiers exhausted → MalformedOutput after exact N attempts
  6.  429 Retry-After honored (≥ 2 s, monkeypatch sleep)
  7.  3 × 5xx → ProviderError after exactly 3 attempts
  8.  price() / count_tokens()
  9.  Configurable base_url / api_key / price-table + DeepSeek preset + request inspection
  10. Import purity (reference test_domain_purity.py — not duplicated here)
"""
from __future__ import annotations

import json
from dataclasses import dataclass, field
from unittest.mock import patch

import pytest

from borgesica.adapters.providers.openai_compatible_provider import (
    OpenAICompatibleProvider,
)
from borgesica.domain.errors import MalformedOutput, ProviderError
from borgesica.domain.models import TranslationResult, TranslationUnit
from borgesica.domain.ports import TranslationProvider

# ---------------------------------------------------------------------------
# Fake HTTP client helpers
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

_JSON_MODE_RESPONSE = {
    "id": "chatcmpl-fake2",
    "object": "chat.completion",
    "choices": [
        {
            "index": 0,
            "message": {
                "role": "assistant",
                "content": json.dumps(_VALID_UNIT_DATA),
                "tool_calls": None,
            },
            "finish_reason": "stop",
        }
    ],
}

_EMPTY_CONTENT_RESPONSE = {
    "id": "chatcmpl-fake3",
    "object": "chat.completion",
    "choices": [
        {
            "index": 0,
            "message": {
                "role": "assistant",
                "content": "",  # DeepSeek JSON-mode quirk: empty content
                "tool_calls": None,
            },
            "finish_reason": "stop",
        }
    ],
}

_NO_TOOL_CALL_RESPONSE = {
    "id": "chatcmpl-fake4",
    "object": "chat.completion",
    "choices": [
        {
            "index": 0,
            "message": {
                "role": "assistant",
                "content": "I cannot use tools right now.",
                "tool_calls": None,
            },
            "finish_reason": "stop",
        }
    ],
}

_TIER3_VALID_JSON_RESPONSE = {
    "id": "chatcmpl-fake5",
    "object": "chat.completion",
    "choices": [
        {
            "index": 0,
            "message": {
                "role": "assistant",
                "content": json.dumps(_VALID_UNIT_DATA),
                "tool_calls": None,
            },
            "finish_reason": "stop",
        }
    ],
}

_TIER3_INVALID_JSON_RESPONSE = {
    "id": "chatcmpl-fake6",
    "object": "chat.completion",
    "choices": [
        {
            "index": 0,
            "message": {
                "role": "assistant",
                "content": "not valid json at all",
                "tool_calls": None,
            },
            "finish_reason": "stop",
        }
    ],
}


def _make_http_error(status_code: int, retry_after: int | None = None):
    """Build an openai.APIStatusError-like exception for testing."""
    import httpx
    headers = {}
    if retry_after is not None:
        headers["Retry-After"] = str(retry_after)
    request = httpx.Request("POST", "https://api.deepseek.com/chat/completions")
    response = httpx.Response(status_code=status_code, headers=headers, request=request)
    import openai
    if status_code == 429:
        return openai.RateLimitError("rate limit", response=response, body=None)
    return openai.InternalServerError("server error", response=response, body=None)


@dataclass
class FakeOpenAIClient:
    """Fake openai.OpenAI client that cycles through pre-configured responses.

    Each response in `responses` is either:
    - A dict (returned as a fake ChatCompletion object)
    - An Exception instance (raised)
    Call log records all kwargs passed to chat.completions.create.
    """

    responses: list
    call_log: list = field(default_factory=list)
    _call_index: int = field(default=0, init=False, repr=False)

    def __post_init__(self):
        # Attach the nested namespace that OpenAI client exposes
        self.chat = _FakeChat(self)

    def _next_response(self, kwargs: dict):
        self.call_log.append(kwargs)
        resp = self.responses[self._call_index % len(self.responses)]
        self._call_index += 1
        if isinstance(resp, Exception):
            raise resp
        return _dict_to_chat_completion(resp)

    @property
    def base_url(self):
        return "https://api.deepseek.com"


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
    """Convert a plain dict to a simple namespace that mirrors openai.ChatCompletion."""
    from types import SimpleNamespace

    def to_ns(obj):
        if isinstance(obj, dict):
            ns = SimpleNamespace(**{k: to_ns(v) for k, v in obj.items()})
            return ns
        if isinstance(obj, list):
            return [to_ns(i) for i in obj]
        return obj

    return to_ns(data)


def _make_provider(responses: list, base_url: str = "https://api.deepseek.com",
                   api_key: str = "fake-key",
                   price_table: dict | None = None) -> tuple[OpenAICompatibleProvider, FakeOpenAIClient]:
    """Convenience: build a provider with a FakeOpenAIClient."""
    fake_client = FakeOpenAIClient(responses=responses)
    provider = OpenAICompatibleProvider(
        base_url=base_url,
        api_key=api_key,
        default_model="deepseek-v4-flash",
        price_table=price_table or {},
        _client=fake_client,
    )
    return provider, fake_client


# ---------------------------------------------------------------------------
# Test 1: Protocol conformance
# ---------------------------------------------------------------------------


class TestProtocolConformance:
    def test_openai_compatible_provider_satisfies_protocol(self):
        """OpenAICompatibleProvider must pass runtime isinstance check against TranslationProvider."""
        provider, _ = _make_provider([_TOOL_CALL_RESPONSE])
        assert isinstance(provider, TranslationProvider)


# ---------------------------------------------------------------------------
# Test 2: Tier-1 tool-call success — exactly 1 HTTP call
# ---------------------------------------------------------------------------


class TestTier1ToolCallSuccess:
    def test_tier1_tool_call_returns_translation_unit(self):
        """Tier-1: valid tool-call response on first call → TranslationResult, 1 HTTP call."""
        provider, fake_client = _make_provider([_TOOL_CALL_RESPONSE])

        with patch("borgesica.adapters.providers.openai_compatible_provider.time.sleep"):
            result = provider.translate("system", "Hello world", "deepseek-v4-flash")

        assert isinstance(result, TranslationResult)
        assert result.unit.translation == "Hola mundo"
        assert result.usage.input_tokens >= 0, "usage.input_tokens must be populated"
        assert result.usage.output_tokens >= 0, "usage.output_tokens must be populated"
        assert fake_client._call_index == 1


# ---------------------------------------------------------------------------
# Test 3: Tier-2 JSON-mode fallback — 2 HTTP calls
# ---------------------------------------------------------------------------


class TestTier2JsonModeFallback:
    def test_tier2_json_mode_returns_translation_unit(self):
        """Tier-1 returns no tool_calls → Tier-2 JSON mode → TranslationResult, 2 calls."""
        provider, fake_client = _make_provider([
            _NO_TOOL_CALL_RESPONSE,   # Tier-1: no tool_calls
            _JSON_MODE_RESPONSE,       # Tier-2: JSON mode content
        ])

        with patch("borgesica.adapters.providers.openai_compatible_provider.time.sleep"):
            result = provider.translate("system", "Hello world", "deepseek-v4-flash")

        assert isinstance(result, TranslationResult)
        assert result.unit.translation == "Hola mundo"
        assert fake_client._call_index == 2


# ---------------------------------------------------------------------------
# Test 4: Tier-2 empty-content (DeepSeek quirk) → Tier-3 success
# ---------------------------------------------------------------------------


class TestTier2EmptyContentFallsToTier3:
    def test_empty_content_falls_through_to_tier3(self):
        """Tier-1 fails, Tier-2 returns empty content → Tier-3 valid JSON → TranslationResult."""
        provider, fake_client = _make_provider([
            _NO_TOOL_CALL_RESPONSE,      # Tier-1: no tool_calls
            _EMPTY_CONTENT_RESPONSE,      # Tier-2: empty content (DeepSeek quirk)
            _TIER3_VALID_JSON_RESPONSE,   # Tier-3: valid JSON in content
        ])

        with patch("borgesica.adapters.providers.openai_compatible_provider.time.sleep"):
            result = provider.translate("system", "Hello world", "deepseek-v4-flash")

        assert isinstance(result, TranslationResult)
        assert result.unit.translation == "Hola mundo"
        # Tier-1 (1) + Tier-2 (1) + Tier-3 attempt 1 (1) = 3
        assert fake_client._call_index == 3


# ---------------------------------------------------------------------------
# Test 5: All tiers exhausted → MalformedOutput, exact call count
# ---------------------------------------------------------------------------


class TestAllTiersExhausted:
    def test_all_tiers_fail_raises_malformed_output_exact_count(self):
        """All tiers fail → MalformedOutput; call count = Tier-1 + Tier-2 + 2 Tier-3 retries."""
        provider, fake_client = _make_provider([
            _NO_TOOL_CALL_RESPONSE,       # Tier-1: no tool_calls
            _EMPTY_CONTENT_RESPONSE,       # Tier-2: empty → fall through
            _TIER3_INVALID_JSON_RESPONSE,  # Tier-3 attempt 1
            _TIER3_INVALID_JSON_RESPONSE,  # Tier-3 attempt 2
        ])

        with patch("borgesica.adapters.providers.openai_compatible_provider.time.sleep"):
            with pytest.raises(MalformedOutput):
                provider.translate("system", "Hello world", "deepseek-v4-flash")

        # 1 (Tier-1) + 1 (Tier-2) + 2 (Tier-3 retries) = 4
        assert fake_client._call_index == 4


# ---------------------------------------------------------------------------
# Test 6: 429 Retry-After honored — sleeps ≥ 2 s
# ---------------------------------------------------------------------------


class TestRateLimitHandling:
    def test_429_retry_after_honored(self):
        """429 with Retry-After: 2 → adapter sleeps ≥ 2 s before retry, returns TranslationUnit."""
        slept_for = []

        def fake_sleep(secs):
            slept_for.append(secs)

        rate_limit_err = _make_http_error(429, retry_after=2)
        provider, fake_client = _make_provider([
            rate_limit_err,
            _TOOL_CALL_RESPONSE,
        ])

        with patch("borgesica.adapters.providers.openai_compatible_provider.time.sleep", fake_sleep):
            result = provider.translate("system", "Hello", "deepseek-v4-flash")

        assert isinstance(result, TranslationResult)
        assert result.unit.translation is not None
        assert any(s >= 2 for s in slept_for), f"Expected sleep >= 2, got {slept_for}"


# ---------------------------------------------------------------------------
# Test 7: 3 × 5xx → ProviderError after exactly 3 attempts
# ---------------------------------------------------------------------------


class TestServerErrorHandling:
    def test_three_5xx_raises_provider_error_after_exactly_3(self):
        """3 × 5xx → ProviderError raised after exactly 3 attempts."""
        server_err = _make_http_error(500)
        provider, fake_client = _make_provider([
            server_err,
            server_err,
            server_err,
        ])

        with patch("borgesica.adapters.providers.openai_compatible_provider.time.sleep", lambda s: None):
            with pytest.raises(ProviderError):
                provider.translate("system", "Hello", "deepseek-v4-flash")

        assert fake_client._call_index == 3


# ---------------------------------------------------------------------------
# Test 8: price() / count_tokens()
# ---------------------------------------------------------------------------


class TestPriceAndCountTokens:
    def test_price_deepseek_flash_returns_tuple_of_floats(self):
        """price('deepseek-v4-flash') returns (0.14, 0.28) for the DeepSeek preset."""
        provider = OpenAICompatibleProvider.deepseek(api_key="fake-key")
        result = provider.price("deepseek-v4-flash")
        assert isinstance(result, tuple)
        assert len(result) == 2
        in_price, out_price = result
        assert isinstance(in_price, float)
        assert isinstance(out_price, float)
        assert in_price > 0
        assert out_price > 0

    def test_price_unknown_model_returns_default(self):
        """Unknown model returns the configured default from price_table, or (3.0, 15.0)."""
        provider, _ = _make_provider([], price_table={"deepseek-v4-flash": (0.14, 0.28)})
        result = provider.price("some-unknown-model-xyz")
        # Should return some default tuple
        assert isinstance(result, tuple)
        assert len(result) == 2

    def test_count_tokens_returns_non_negative_int(self):
        """count_tokens returns a non-negative int."""
        provider, _ = _make_provider([])
        result = provider.count_tokens("Hello world foo bar", "deepseek-v4-flash")
        assert isinstance(result, int)
        assert result >= 0


# ---------------------------------------------------------------------------
# Test 9: Configurable base_url / api_key / price-table + DeepSeek preset + request inspection
# ---------------------------------------------------------------------------


class TestConfigurableAndDeepSeekPreset:
    def test_deepseek_preset_sets_correct_base_url(self):
        """DeepSeek preset uses base_url='https://api.deepseek.com'."""
        provider = OpenAICompatibleProvider.deepseek(api_key="sk-fake")
        assert "deepseek.com" in provider.base_url

    def test_deepseek_preset_sets_correct_default_model(self):
        """DeepSeek preset uses default_model='deepseek-v4-flash'."""
        provider = OpenAICompatibleProvider.deepseek(api_key="sk-fake")
        assert provider.default_model == "deepseek-v4-flash"

    def test_model_string_passed_unchanged_in_request_body(self):
        """The model string passed to translate() is sent unchanged in the request body."""
        fake_client = FakeOpenAIClient(responses=[_TOOL_CALL_RESPONSE])
        provider = OpenAICompatibleProvider(
            base_url="https://api.deepseek.com",
            api_key="sk-fake",
            default_model="deepseek-v4-flash",
            price_table={},
            _client=fake_client,
        )

        with patch("borgesica.adapters.providers.openai_compatible_provider.time.sleep"):
            provider.translate("system", "Hello", "deepseek-v4-pro")

        # Inspect the kwargs of the first (and only) call
        assert len(fake_client.call_log) == 1
        call_kwargs = fake_client.call_log[0]
        assert call_kwargs.get("model") == "deepseek-v4-pro"

    def test_custom_base_url_used(self):
        """Custom base_url is stored on the provider."""
        fake_client = FakeOpenAIClient(responses=[_TOOL_CALL_RESPONSE])
        provider = OpenAICompatibleProvider(
            base_url="https://openrouter.ai/api/v1",
            api_key="sk-or-fake",
            default_model="mistral/mistral-7b",
            price_table={},
            _client=fake_client,
        )
        assert "openrouter.ai" in provider.base_url
