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

from borgesica.adapters.providers.anthropic_provider import (
    _MAX_OUTPUT_TOKENS,
    _PRICE_TABLE,
    AnthropicProvider,
)
from borgesica.domain.errors import MalformedOutput, ProviderError
from borgesica.domain.models import TranslationResult, TranslationUnit
from borgesica.domain.ports import TranslationProvider


# ---------------------------------------------------------------------------
# Fake HTTP client helpers
# ---------------------------------------------------------------------------


def _valid_tool_use_response(
    translation: str = "Hola mundo",
    input_tokens: int | None = None,
    output_tokens: int | None = None,
) -> object:
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
    if input_tokens is not None or output_tokens is not None:
        msg.usage = MagicMock(
            input_tokens=input_tokens or 0, output_tokens=output_tokens or 0
        )
    return msg


def _text_response(
    text: str,
    input_tokens: int | None = None,
    output_tokens: int | None = None,
) -> object:
    """Build a fake Anthropic message with a plain text content block (fallback path)."""
    import json

    content_block = MagicMock()
    content_block.type = "text"
    content_block.text = text

    msg = MagicMock()
    msg.content = [content_block]
    msg.stop_reason = "end_turn"
    if input_tokens is not None or output_tokens is not None:
        msg.usage = MagicMock(
            input_tokens=input_tokens or 0, output_tokens=output_tokens or 0
        )
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
        """First response has no tool_use → text fallback parsed → TranslationResult returned."""
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
        assert isinstance(result, TranslationResult)
        assert result.unit.translation == "Hola mundo"

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
        """Happy path: tool_use block on first attempt → TranslationResult returned directly."""
        responses = [_valid_tool_use_response("Buenos días")]
        client = make_fake_client(responses)
        provider = AnthropicProvider(client=client)
        result = provider.translate("system", "Good morning", "claude-3-5-haiku-20241022")
        assert isinstance(result, TranslationResult)
        assert result.unit.translation == "Buenos días"
        assert result.usage.input_tokens >= 0, "usage.input_tokens must be non-negative"
        assert result.usage.output_tokens >= 0, "usage.output_tokens must be non-negative"
        assert client.messages.create.call_count == 1


# ---------------------------------------------------------------------------
# Test 2c: Billed-but-failed usage accrual (budget guard regression).
#
# A real book run billed $1.68 on Anthropic's console but Borgésica recorded
# $0.196: when a call returns HTTP 200 with unparseable content, Anthropic
# bills the call, but the old code discarded that attempt's usage entirely.
# The FIX: translate() must SUM usage across every attempt that obtained a
# response (billed), whether or not that attempt's parse succeeded.
# ---------------------------------------------------------------------------


class TestBilledUsageAccrual:
    def test_summed_usage_across_malformed_then_success(self):
        """Attempt 1: HTTP 200, billed usage (100, 50), but unparseable content
        (malformed tool_use → falls through as None-unit, no exception).
        Attempt 2: succeeds with usage (80, 40).
        Final TranslationResult.usage must be the SUM: (180, 90), not just the
        final attempt's (80, 40) alone.
        """
        responses = [
            _text_response("not valid json", input_tokens=100, output_tokens=50),
            _valid_tool_use_response("Hola", input_tokens=80, output_tokens=40),
        ]
        client = make_fake_client(responses)
        provider = AnthropicProvider(client=client)
        result = provider.translate("system", "Hello", "claude-3-5-haiku-20241022")

        assert isinstance(result, TranslationResult)
        assert result.usage.input_tokens == 180
        assert result.usage.output_tokens == 90

    def test_summed_usage_across_validation_error_then_success(self):
        """Attempt 1: tool_use block present but its input fails TranslationUnit
        validation (ValidationError path inside _parse_response) — response.usage
        must still be accrued because the HTTP call succeeded (billed).
        Attempt 2: succeeds. Final usage = sum of both.
        """
        bad_content_block = MagicMock()
        bad_content_block.type = "tool_use"
        bad_content_block.input = {"translation": None}  # fails TranslationUnit validation

        bad_msg = MagicMock()
        bad_msg.content = [bad_content_block]
        bad_msg.stop_reason = "tool_use"
        bad_msg.usage = MagicMock(input_tokens=120, output_tokens=60)

        responses = [
            bad_msg,
            _valid_tool_use_response("Hola", input_tokens=70, output_tokens=35),
        ]
        client = make_fake_client(responses)
        provider = AnthropicProvider(client=client)
        result = provider.translate("system", "Hello", "claude-3-5-haiku-20241022")

        assert result.usage.input_tokens == 190
        assert result.usage.output_tokens == 95

    def test_malformed_output_carries_summed_billed_usage_on_total_failure(self):
        """All attempts malformed but billed (each has response.usage) → the
        raised MalformedOutput.usage must equal the SUM of all billed attempts'
        usage, not zero.
        """
        responses = [
            _text_response("bad json 1", input_tokens=10, output_tokens=5),
            _text_response("bad json 2", input_tokens=20, output_tokens=10),
            _text_response("bad json 3", input_tokens=30, output_tokens=15),
        ]
        client = make_fake_client(responses)
        provider = AnthropicProvider(client=client)

        with pytest.raises(MalformedOutput) as exc_info:
            provider.translate("system", "Hello world", "claude-3-5-haiku-20241022")

        assert exc_info.value.usage.input_tokens == 60
        assert exc_info.value.usage.output_tokens == 30

    def test_rate_limit_exhausted_raises_zero_usage_provider_error(self):
        """429s that exhaust retries never obtain a billed response — no usage
        should be accrued. RateLimitError itself is not raised as ProviderError
        by the current design (it retries until max_retries, then falls through
        to MalformedOutput with last_error=RateLimitError) — assert MalformedOutput
        carries zero usage in that case, since no response.usage was ever read.
        """
        import anthropic

        rate_limit_err = _make_api_error(anthropic.RateLimitError, 429, retry_after=0)
        client = MagicMock()
        client.messages.create.side_effect = rate_limit_err
        client.messages.count_tokens = MagicMock(return_value=MagicMock(input_tokens=50))

        provider = AnthropicProvider(client=client)

        with patch("borgesica.adapters.providers.anthropic_provider.time.sleep", lambda s: None):
            with pytest.raises(MalformedOutput) as exc_info:
                provider.translate("system", "Hello", "claude-3-5-haiku-20241022")

        assert exc_info.value.usage.input_tokens == 0
        assert exc_info.value.usage.output_tokens == 0

    def test_5xx_exhausted_raises_zero_usage_provider_error(self):
        """3x 5xx exhausts retries → ProviderError.usage must be zero (no billed
        response was ever obtained on any 5xx attempt)."""
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
            with pytest.raises(ProviderError) as exc_info:
                provider.translate("system", "Hello", "claude-3-5-haiku-20241022")

        assert exc_info.value.usage.input_tokens == 0
        assert exc_info.value.usage.output_tokens == 0


# ---------------------------------------------------------------------------
# Test 2b: max_tokens must be large enough to avoid mid-JSON truncation.
#
# Regression test: a real 2677-char chunk hit stop_reason='max_tokens' with the
# old hardcoded max_tokens=1024, truncating the tool_use input mid-key and
# causing a deterministic ValidationError (MalformedOutput) across all retries.
# ---------------------------------------------------------------------------


class TestMaxOutputTokens:
    def test_translate_requests_max_output_tokens_constant(self):
        """The `max_tokens` sent to the SDK must equal the module's _MAX_OUTPUT_TOKENS
        constant (8192), not the old truncating value of 1024.
        """
        responses = [_valid_tool_use_response("Buenos días")]
        client = make_fake_client(responses)
        provider = AnthropicProvider(client=client)
        provider.translate("system", "Good morning", "claude-3-5-haiku-20241022")

        _, kwargs = client.messages.create.call_args
        assert kwargs["max_tokens"] == _MAX_OUTPUT_TOKENS
        assert _MAX_OUTPUT_TOKENS == 8192


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

        assert isinstance(result, TranslationResult)
        assert result.unit.translation is not None
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

    def test_price_haiku_4_5_matches_current_published_rate(self):
        """Haiku 4.5 is billed at ($1.00, $5.00)/Mtok (current Anthropic rate).
        The table previously carried a stale ($0.80, $4.00) — verify the fix.
        """
        provider = AnthropicProvider(api_key="fake-key")
        assert provider.price("claude-haiku-4-5-20251001") == (1.00, 5.00)

    def test_price_opus_4_8_matches_current_published_rate(self):
        """Opus 4.8 is billed at ($5.00, $25.00)/Mtok (current Anthropic rate).
        The table previously carried a stale ($15.00, $75.00) — verify the fix.
        """
        provider = AnthropicProvider(api_key="fake-key")
        assert provider.price("claude-opus-4-8") == (5.00, 25.00)

    def test_price_sonnet_5_is_a_tracked_table_entry(self):
        """claude-sonnet-5 (the desktop app's Anthropic default model, see
        desktop/src/wizard.ts defaultModelForProvider) must be a real entry in
        _PRICE_TABLE, not merely rely on the (3.0, 15.0) unknown-model
        fallback. Asserting through price() alone can't tell the two apart
        (the fallback happens to equal the Sonnet-tier rate), so this checks
        the table directly."""
        assert "claude-sonnet-5" in _PRICE_TABLE

    def test_price_sonnet_5_matches_current_published_rate(self):
        """claude-sonnet-5 is billed at the same Sonnet-tier rate
        ($3.00, $15.00)/Mtok every Sonnet generation in this table carries."""
        provider = AnthropicProvider(api_key="fake-key")
        assert provider.price("claude-sonnet-5") == (3.00, 15.00)

    def test_price_table_is_user_overridable(self):
        """A caller can inject a price_table to override/extend the built-in
        one — pricing goes stale, so it must not be hardcoded-only."""
        provider = AnthropicProvider(
            api_key="fake-key",
            price_table={"claude-opus-4-8": (7.5, 30.0), "future-model": (2.0, 8.0)},
        )
        assert provider.price("claude-opus-4-8") == (7.5, 30.0)
        assert provider.price("future-model") == (2.0, 8.0)

    def test_price_table_defaults_to_builtin_when_not_injected(self):
        """No override → the built-in table still applies (backward compat)."""
        provider = AnthropicProvider(api_key="fake-key")
        assert provider.price("claude-haiku-4-5-20251001") == (1.00, 5.00)


class TestRetryWasteFactor:
    def test_anthropic_declares_a_modest_waste_factor(self):
        """Anthropic's native tool-calling rarely falls through, so its
        retry-waste ceiling factor is modest (< the OpenAI-compatible default)."""
        provider = AnthropicProvider(api_key="fake-key")
        assert provider.retry_waste_factor < 3.0
        assert provider.retry_waste_factor >= 1.0


# ---------------------------------------------------------------------------
# Test 8: Segmented output (SRT cue arrays — segment_count contract)
# ---------------------------------------------------------------------------


def _segmented_tool_use_response() -> object:
    """Fake Anthropic response whose tool_use input carries a translations array."""
    content_block = MagicMock()
    content_block.type = "tool_use"
    content_block.input = {
        "translations": ["Hola", "mundo"],
        "summary_update": "Dos segmentos.",
        "glossary_additions": [],
    }
    msg = MagicMock()
    msg.content = [content_block]
    msg.stop_reason = "tool_use"
    return msg


class TestSegmentedOutput:
    def test_segment_count_sends_segmented_input_schema(self):
        """translate(..., segment_count=2) sends an input_schema demanding a
        'translations' array of exactly 2 strings — no 'translation' string."""
        client = make_fake_client([_segmented_tool_use_response()])
        provider = AnthropicProvider(client=client)

        provider.translate("system", "a\n\nb", "claude-3-5-haiku-20241022", segment_count=2)

        kwargs = client.messages.create.call_args.kwargs
        schema = kwargs["tools"][0]["input_schema"]
        props = schema["properties"]
        assert "translation" not in props
        assert props["translations"]["minItems"] == 2
        assert props["translations"]["maxItems"] == 2
        assert "translations" in schema["required"]

    def test_segmented_tool_use_parses_translations_array(self):
        client = make_fake_client([_segmented_tool_use_response()])
        provider = AnthropicProvider(client=client)

        result = provider.translate(
            "system", "a\n\nb", "claude-3-5-haiku-20241022", segment_count=2
        )

        assert result.unit.translations == ["Hola", "mundo"]
        assert result.unit.translation == "Hola\n\nmundo"

    def test_no_segment_count_sends_legacy_schema(self):
        """Without segment_count the input_schema keeps the prose contract."""
        client = make_fake_client([_valid_tool_use_response()])
        provider = AnthropicProvider(client=client)

        provider.translate("system", "Hello", "claude-3-5-haiku-20241022")

        kwargs = client.messages.create.call_args.kwargs
        schema = kwargs["tools"][0]["input_schema"]
        assert "translations" not in schema["properties"]
        assert "translation" in schema["required"]


# ---------------------------------------------------------------------------
# Domain purity — anthropic import ONLY in adapters/providers/
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
        assert isinstance(result, TranslationResult)
        assert result.unit.translation.strip()
        assert result.unit.summary_update.strip()
