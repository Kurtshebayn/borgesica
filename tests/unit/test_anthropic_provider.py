"""Unit tests for AnthropicProvider (M1-11).

No network calls — all tests fake the Anthropic HTTP layer by injecting a
FakeAnthropicClient.  Only the integration test (test 9, marked @integration)
makes a real API call and is skipped when ANTHROPIC_API_KEY is not set.
"""
from __future__ import annotations

import os
import time
from dataclasses import dataclass, field
from unittest.mock import MagicMock, patch

import pytest

from borgesica.adapters.providers.anthropic_provider import AnthropicProvider
from borgesica.domain.errors import MalformedOutput, ProviderError
from borgesica.domain.models import TranslationUnit
from borgesica.domain.ports import TranslationProvider


# ---------------------------------------------------------------------------
# Fake HTTP client helpers
# ---------------------------------------------------------------------------


def _valid_tool_use_response(translation: str = "Hola mundo") -> object:
    """Build a fake Anthropic message response with a tool_use content block."""
    import json

    unit_data = {
        "translation": translation,
        "summary_update": "One sentence summary.",
        "glossary_additions": [],
    }

    content_block = MagicMock()
    content_block.type = "tool_use"
    content_block.input = unit_data

    msg = MagicMock()
    msg.content = [content_block]
    msg.stop_reason = "tool_use"
    return msg


def _text_response(text: str) -> object:
    """Build a fake Anthropic message with a plain text content block (fallback path)."""
    import json

    content_block = MagicMock()
    content_block.type = "text"
    content_block.text = text

    msg = MagicMock()
    msg.content = [content_block]
    msg.stop_reason = "end_turn"
    return msg


def _make_api_error(error_class, status_code: int, retry_after: int | None = None) -> Exception:
    """Build a real Anthropic error instance using a fake httpx.Response."""
    import httpx
    headers = {}
    if retry_after is not None:
        headers["Retry-After"] = str(retry_after)
    request = httpx.Request("POST", "https://api.anthropic.com/v1/messages")
    response = httpx.Response(status_code=status_code, headers=headers, request=request)
    return error_class("fake error", response=response, body=None)


def make_fake_client(responses: list) -> MagicMock:
    """Return a fake anthropic.Anthropic client whose messages.create cycles through responses.

    Each response is either a return value (MagicMock) or an Exception subclass to raise.
    """
    call_count = [0]

    def _create(**kwargs):
        idx = call_count[0]
        call_count[0] += 1
        resp = responses[idx % len(responses)]
        if isinstance(resp, Exception) or (isinstance(resp, type) and issubclass(resp, Exception)):
            raise resp
        if callable(resp) and not isinstance(resp, MagicMock):
            return resp()
        return resp

    client = MagicMock()
    client.messages.create.side_effect = _create
    # Stub count_tokens method
    client.messages.count_tokens = MagicMock(return_value=MagicMock(input_tokens=50))
    return client


# ---------------------------------------------------------------------------
# Test 1: Protocol conformance
# ---------------------------------------------------------------------------


class TestProtocolConformance:
    def test_anthropic_provider_satisfies_protocol(self):
        """AnthropicProvider must pass runtime isinstance check against TranslationProvider."""
        provider = AnthropicProvider(api_key="fake-key")
        assert isinstance(provider, TranslationProvider)


# ---------------------------------------------------------------------------
# Test 2: Tier-3 fallback — invalid JSON → valid JSON on second attempt
# ---------------------------------------------------------------------------


class TestGracefulDegradation:
    def test_invalid_tool_use_then_valid_text_fallback(self):
        """First response has no tool_use → text fallback parsed → TranslationUnit returned."""
        import json

        valid_json = json.dumps({
            "translation": "Hola mundo",
            "summary_update": "Un resumen.",
            "glossary_additions": [],
        })

        responses = [
            _text_response("not valid json"),   # first attempt fails
            _text_response(valid_json),          # second attempt succeeds
        ]
        client = make_fake_client(responses)
        provider = AnthropicProvider(client=client)
        result = provider.translate("system", "Hello world", "claude-3-5-haiku-20241022")
        assert isinstance(result, TranslationUnit)
        assert result.translation == "Hola mundo"

    def test_all_three_tiers_fail_raises_malformed_output(self):
        """3 bad responses → MalformedOutput raised; exactly 3 attempts made."""
        responses = [
            _text_response("bad json 1"),
            _text_response("bad json 2"),
            _text_response("bad json 3"),
        ]
        client = make_fake_client(responses)
        provider = AnthropicProvider(client=client)

        with pytest.raises(MalformedOutput):
            provider.translate("system", "Hello world", "claude-3-5-haiku-20241022")

        assert client.messages.create.call_count == 3

    def test_valid_tool_use_on_first_attempt(self):
        """Happy path: tool_use block on first attempt → TranslationUnit returned directly."""
        responses = [_valid_tool_use_response("Buenos días")]
        client = make_fake_client(responses)
        provider = AnthropicProvider(client=client)
        result = provider.translate("system", "Good morning", "claude-3-5-haiku-20241022")
        assert result.translation == "Buenos días"
        assert client.messages.create.call_count == 1


# ---------------------------------------------------------------------------
# Test 3 (see above): all 3 tiers fail → MalformedOutput (covered above)
# ---------------------------------------------------------------------------


# ---------------------------------------------------------------------------
# Test 4: 429 with Retry-After header → adapter waits
# ---------------------------------------------------------------------------


class TestRateLimitHandling:
    def test_429_retry_after_honored(self):
        """429 with Retry-After: 2 → adapter sleeps ≥ 2 seconds before retry."""
        import anthropic

        slept_for = []

        def fake_sleep(secs):
            slept_for.append(secs)

        rate_limit_err = _make_api_error(anthropic.RateLimitError, 429, retry_after=2)

        call_count = [0]

        def _create(**kwargs):
            call_count[0] += 1
            if call_count[0] == 1:
                raise rate_limit_err
            return _valid_tool_use_response()

        client = MagicMock()
        client.messages.create.side_effect = _create
        client.messages.count_tokens = MagicMock(return_value=MagicMock(input_tokens=50))

        provider = AnthropicProvider(client=client)

        with patch("borgesica.adapters.providers.anthropic_provider.time.sleep", fake_sleep):
            result = provider.translate("system", "Hello", "claude-3-5-haiku-20241022")

        assert result.translation is not None
        assert any(s >= 2 for s in slept_for), f"Expected sleep >= 2, got {slept_for}"


# ---------------------------------------------------------------------------
# Test 5: 3× 5xx → ProviderError after exactly 3 attempts
# ---------------------------------------------------------------------------


class TestServerErrorHandling:
    def test_three_5xx_raises_provider_error(self):
        """3 × 5xx → ProviderError raised after exactly 3 attempts."""
        import anthropic

        call_count = [0]

        def _create(**kwargs):
            call_count[0] += 1
            raise _make_api_error(anthropic.InternalServerError, 500)

        client = MagicMock()
        client.messages.create.side_effect = _create
        client.messages.count_tokens = MagicMock(return_value=MagicMock(input_tokens=50))

        provider = AnthropicProvider(client=client)

        with patch("borgesica.adapters.providers.anthropic_provider.time.sleep", lambda s: None):
            with pytest.raises(ProviderError):
                provider.translate("system", "Hello", "claude-3-5-haiku-20241022")

        assert call_count[0] == 3


# ---------------------------------------------------------------------------
# Test 6: count_tokens delegates to client or approximation
# ---------------------------------------------------------------------------


class TestCountTokens:
    def test_count_tokens_returns_int(self):
        """count_tokens returns an int ≥ 0."""
        client = MagicMock()
        client.messages.count_tokens = MagicMock(return_value=MagicMock(input_tokens=42))
        provider = AnthropicProvider(client=client)
        result = provider.count_tokens("Hello world", "claude-3-5-haiku-20241022")
        assert isinstance(result, int)
        assert result >= 0

    def test_count_tokens_fallback_when_client_fails(self):
        """If client.count_tokens raises, falls back to word-count approximation."""
        client = MagicMock()
        client.messages.count_tokens.side_effect = Exception("not supported")
        provider = AnthropicProvider(client=client)
        result = provider.count_tokens("Hello world foo bar", "claude-3-5-haiku-20241022")
        # Approximation: word count × 1.3 rounded
        assert result > 0


# ---------------------------------------------------------------------------
# Test 7: price returns tuple of two floats
# ---------------------------------------------------------------------------


class TestPrice:
    def test_price_known_model(self):
        """price() for a known model returns a tuple of two positive floats."""
        provider = AnthropicProvider(api_key="fake-key")
        result = provider.price("claude-haiku-4-5-20251001")
        assert isinstance(result, tuple)
        assert len(result) == 2
        in_price, out_price = result
        assert isinstance(in_price, float)
        assert isinstance(out_price, float)
        assert in_price > 0
        assert out_price > 0

    def test_price_unknown_model_returns_default(self):
        """Unknown model returns default pricing (3.0, 15.0)."""
        provider = AnthropicProvider(api_key="fake-key")
        result = provider.price("some-future-model-xyz")
        assert result == (3.0, 15.0)


# ---------------------------------------------------------------------------
# Test 8: Domain purity — anthropic import ONLY in adapters/providers/
# (already covered by test_domain_purity.py from M1-2)
# ---------------------------------------------------------------------------


# ---------------------------------------------------------------------------
# Test 9: Integration test — real API call (SKIPPED if no API key)
# ---------------------------------------------------------------------------


@pytest.mark.integration
class TestAnthropicProviderLive:
    @pytest.mark.skipif(
        not os.environ.get("ANTHROPIC_API_KEY"),
        reason="ANTHROPIC_API_KEY not set — skipping live integration test",
    )
    def test_real_translate_returns_valid_unit(self):
        """Live call: short English text → valid TranslationUnit with non-empty translation."""
        provider = AnthropicProvider()  # uses env var
        result = provider.translate(
            system="You are a translation engine. Translate the given text to neutral Spanish.",
            user="The quick brown fox jumps over the lazy dog.",
            model="claude-3-5-haiku-20241022",
        )
        assert isinstance(result, TranslationUnit)
        assert result.translation.strip()
        assert result.summary_update.strip()
