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
    _MAX_OUTPUT_TOKENS,
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


def _with_usage(response: dict, prompt_tokens: int, completion_tokens: int) -> dict:
    """Return a copy of `response` with a `usage` block attached (billed usage)."""
    import copy

    out = copy.deepcopy(response)
    out["usage"] = {
        "prompt_tokens": prompt_tokens,
        "completion_tokens": completion_tokens,
    }
    return out


_NO_TOOL_CALL_RESPONSE_BILLED = _with_usage(_NO_TOOL_CALL_RESPONSE, 100, 50)
_EMPTY_CONTENT_RESPONSE_BILLED = _with_usage(_EMPTY_CONTENT_RESPONSE, 90, 45)
_TIER3_INVALID_JSON_RESPONSE_BILLED_1 = _with_usage(_TIER3_INVALID_JSON_RESPONSE, 10, 5)
_TIER3_INVALID_JSON_RESPONSE_BILLED_2 = _with_usage(_TIER3_INVALID_JSON_RESPONSE, 20, 10)
_TIER3_VALID_JSON_RESPONSE_BILLED = _with_usage(_TIER3_VALID_JSON_RESPONSE, 70, 35)


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
    if 400 <= status_code < 500:
        return openai.BadRequestError("does not support tools", response=response, body=None)
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
# Test 4a: 4xx on Tier-1/Tier-2 falls through to the next tier instead of
# raising ProviderError.
#
# Regression test: Ollama returns HTTP 400 when the model does not support
# tool-calling (e.g. Tower-Plus-9B GGUF). The old behaviour raised
# ProviderError immediately, killing the whole fallback chain — the chunk was
# skipped and the output shipped UNTRANSLATED. A 4xx on Tier-1/Tier-2 is a
# capability signal, not a fatal transport error.
# ---------------------------------------------------------------------------


class TestClientErrorFallsThrough:
    def test_tier1_400_falls_through_to_tier2(self):
        """Tier-1 raises 400 (no tool support) → Tier-2 JSON mode succeeds, 2 calls."""
        provider, fake_client = _make_provider([
            _make_http_error(400),     # Tier-1: endpoint rejects tools
            _JSON_MODE_RESPONSE,       # Tier-2: JSON mode content
        ])

        with patch("borgesica.adapters.providers.openai_compatible_provider.time.sleep"):
            result = provider.translate("system", "Hello world", "tower-plus-9b")

        assert isinstance(result, TranslationResult)
        assert result.unit.translation == "Hola mundo"
        assert fake_client._call_index == 2

    def test_tier1_and_tier2_400_fall_through_to_tier3(self):
        """400 on both Tier-1 and Tier-2 → Tier-3 prompt-and-parse succeeds, 3 calls."""
        provider, fake_client = _make_provider([
            _make_http_error(400),        # Tier-1: endpoint rejects tools
            _make_http_error(400),        # Tier-2: endpoint rejects JSON mode
            _TIER3_VALID_JSON_RESPONSE,   # Tier-3: valid JSON in content
        ])

        with patch("borgesica.adapters.providers.openai_compatible_provider.time.sleep"):
            result = provider.translate("system", "Hello world", "tower-plus-9b")

        assert isinstance(result, TranslationResult)
        assert result.unit.translation == "Hola mundo"
        assert fake_client._call_index == 3

    def test_tier3_400_still_raises_provider_error(self):
        """A 4xx on Tier-3 (plain chat) means the request itself is broken → ProviderError."""
        provider, _ = _make_provider([
            _make_http_error(400),  # Tier-1
            _make_http_error(400),  # Tier-2
            _make_http_error(400),  # Tier-3: plain chat rejected → fatal
        ])

        with patch("borgesica.adapters.providers.openai_compatible_provider.time.sleep"):
            with pytest.raises(ProviderError):
                provider.translate("system", "Hello world", "tower-plus-9b")


# ---------------------------------------------------------------------------
# Test 4b: max_tokens must be large enough to avoid mid-JSON truncation, in
# ALL THREE tiers.
#
# Regression test: a real 2677-char chunk hit finish_reason='length' with the
# old hardcoded max_tokens=1024, truncating structured output mid-key and
# causing a deterministic ValidationError (MalformedOutput) across all retries.
# ---------------------------------------------------------------------------


class TestMaxOutputTokens:
    def test_all_three_tiers_request_max_output_tokens_constant(self):
        """The `max_tokens` sent to the SDK must equal _MAX_OUTPUT_TOKENS (8192)
        on Tier-1, Tier-2, and Tier-3 calls — not the old truncating value of 1024.
        """
        provider, fake_client = _make_provider([
            _NO_TOOL_CALL_RESPONSE,      # Tier-1: no tool_calls
            _EMPTY_CONTENT_RESPONSE,      # Tier-2: empty content (DeepSeek quirk)
            _TIER3_VALID_JSON_RESPONSE,   # Tier-3: valid JSON in content
        ])

        with patch("borgesica.adapters.providers.openai_compatible_provider.time.sleep"):
            provider.translate("system", "Hello world", "deepseek-v4-flash")

        assert len(fake_client.call_log) == 3
        for call_kwargs in fake_client.call_log:
            assert call_kwargs["max_tokens"] == _MAX_OUTPUT_TOKENS
        assert _MAX_OUTPUT_TOKENS == 8192


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
# Test 5b: Billed-but-failed usage accrual (budget guard regression).
#
# When a tier obtains a response (billed by the provider) but that tier's
# parse fails / falls through, its usage must be accrued into a running total
# so the eventual TranslationResult (or raised MalformedOutput) reflects the
# true billed cost — not just the final successful (or zero) usage.
# ---------------------------------------------------------------------------


class TestBilledUsageAccrualAcrossTiers:
    def test_summed_usage_tier1_fallthrough_tier2_fallthrough_tier3_success(self):
        """Tier-1 gets a billed response (100, 50) but no tool_call → falls through.
        Tier-2 gets a billed response (90, 45) but empty content (DeepSeek quirk) →
        falls through. Tier-3 succeeds with usage (70, 35).
        Final usage must be the SUM across all three billed tiers: (260, 130).
        """
        provider, fake_client = _make_provider([
            _NO_TOOL_CALL_RESPONSE_BILLED,
            _EMPTY_CONTENT_RESPONSE_BILLED,
            _TIER3_VALID_JSON_RESPONSE_BILLED,
        ])

        with patch("borgesica.adapters.providers.openai_compatible_provider.time.sleep"):
            result = provider.translate("system", "Hello world", "deepseek-v4-flash")

        assert isinstance(result, TranslationResult)
        assert result.usage.input_tokens == 260
        assert result.usage.output_tokens == 130

    def test_malformed_output_carries_summed_billed_usage_across_all_tiers(self):
        """All tiers exhausted (Tier-1 fallthrough, Tier-2 fallthrough, Tier-3
        fails twice) but each obtained a billed response → the raised
        MalformedOutput.usage must sum every billed attempt's usage.
        """
        provider, fake_client = _make_provider([
            _NO_TOOL_CALL_RESPONSE_BILLED,           # Tier-1: billed (100, 50), no tool_call
            _EMPTY_CONTENT_RESPONSE_BILLED,           # Tier-2: billed (90, 45), empty content
            _TIER3_INVALID_JSON_RESPONSE_BILLED_1,    # Tier-3 attempt 1: billed (10, 5)
            _TIER3_INVALID_JSON_RESPONSE_BILLED_2,    # Tier-3 attempt 2: billed (20, 10)
        ])

        with patch("borgesica.adapters.providers.openai_compatible_provider.time.sleep"):
            with pytest.raises(MalformedOutput) as exc_info:
                provider.translate("system", "Hello world", "deepseek-v4-flash")

        # sum: (100+90+10+20, 50+45+5+10) = (220, 110)
        assert exc_info.value.usage.input_tokens == 220
        assert exc_info.value.usage.output_tokens == 110

    def test_rate_limit_and_5xx_contribute_zero_usage(self):
        """429/5xx paths never obtain a billed response — ProviderError raised
        after 5xx exhaustion must carry zero usage."""
        server_err = _make_http_error(500)
        provider, fake_client = _make_provider([
            server_err,
            server_err,
            server_err,
        ])

        with patch("borgesica.adapters.providers.openai_compatible_provider.time.sleep", lambda s: None):
            with pytest.raises(ProviderError) as exc_info:
                provider.translate("system", "Hello", "deepseek-v4-flash")

        assert exc_info.value.usage.input_tokens == 0
        assert exc_info.value.usage.output_tokens == 0


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
