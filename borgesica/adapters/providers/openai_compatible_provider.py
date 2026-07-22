"""OpenAI-compatible provider adapter (M4-3).

Implements the TranslationProvider Protocol for any endpoint that speaks the
OpenAI /chat/completions wire format.  Default preset targets DeepSeek, but the
same adapter works with OpenRouter, Mistral, Together AI, or any other OpenAI-
compatible host by passing a different base_url.

Structured-output strategy (adapter-internal):

  TIER-1 (PRIMARY): Tool/function-calling with TranslationUnit.model_json_schema().
    The endpoint returns a tool_calls entry in the assistant message; we parse
    the function arguments JSON directly.

  TIER-2 (JSON MODE): response_format={"type": "json_object"}.
    The prompt must contain the word "json" (OpenAI requirement).  We parse the
    content field as JSON → TranslationUnit.
    DeepSeek quirk handled here: when the endpoint returns an EMPTY content
    string in JSON mode (known DeepSeek behaviour), we detect it and fall
    through to Tier-3 instead of crashing or looping.

  TIER-3 (PROMPT-AND-PARSE FALLBACK): Plain instruction in the prompt to output
    JSON, then parse the text content.  Retried up to MAX_TIER3_RETRIES times.

  All tiers exhausted → raise MalformedOutput (no extra calls beyond the defined
  maximum).

Retry / resilience:
  - 429 (rate-limit): honor Retry-After header; exponential backoff + jitter.
  - 5xx (server error): exponential backoff + jitter, ≤ MAX_5XX_RETRIES attempts
    total, then ProviderError.
  - 4xx on Tier-1/Tier-2: falls through to the next tier instead of raising.
    Endpoints reject capabilities per model (e.g. Ollama returns 400 when the
    model does not support tool-calling, or when JSON mode is unavailable);
    that is a capability signal, not a fatal transport error. Tier-3 uses
    plain chat completions, so a 4xx there IS fatal → ProviderError.
  - Malformed output falls through tiers; does NOT trigger the 5xx retry path.

Dependency rule: `openai` imported ONLY here; domain never sees it.
The openai SDK is used because it natively supports a custom base_url and covers
all OpenAI-compatible endpoints without manual HTTP plumbing.  This mirrors the
AnthropicProvider symmetrically (vendor SDK per provider).

Injectable HTTP client: pass `_client` to the constructor to inject a test
double.  The real client is created via `openai.OpenAI(base_url=..., api_key=...)`.
"""
from __future__ import annotations

import json
import random
import time
from typing import Any

import openai
from pydantic import ValidationError

from borgesica.domain.errors import MalformedOutput, ProviderError
from borgesica.domain.models import (
    TranslationResult,
    TranslationUnit,
    Usage,
    translation_tool_schema,
)

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

MAX_5XX_RETRIES = 3
MAX_TIER3_RETRIES = 2  # Tier-3 is retried at most 2 times (2 HTTP calls)

# Output token cap sent to the API for all three tiers. Must comfortably fit
# the structured JSON output (translation + summary + glossary) for chunks up
# to ~800 source tokens. The previous value of 1024 truncated real chunks
# mid-JSON (a real 2677-char chunk hit finish_reason='length' with usage at
# the 1024 cap, corrupting the structured output and causing a deterministic
# MalformedOutput across every retry, all tiers). 8192 gives enough headroom
# for translation output that can run longer than the source text plus the
# summary/glossary fields. OllamaProvider subclasses this provider and
# inherits the constant/fix without overriding it.
_MAX_OUTPUT_TOKENS = 8192

# Tool / function definition sent for Tier-1 structured output.
_TOOL_NAME = "submit_translation"


def _translation_tools(segment_count: int | None = None) -> list[dict[str, Any]]:
    """Build the Tier-1 tool definition for the requested output shape.

    Per-call (not module-level) because the segmented schema pins the
    translations array length to the chunk's cue count.
    """
    return [
        {
            "type": "function",
            "function": {
                "name": _TOOL_NAME,
                "description": (
                    "Submit the structured translation result. "
                    "Use this function to return the translation, a summary update, "
                    "and any new glossary terms discovered."
                ),
                "parameters": translation_tool_schema(segment_count),
            },
        }
    ]

# Default price table — (input_usd_per_mtok, output_usd_per_mtok).
# NOT used for billing; only for pre-flight cost estimates.
_DEFAULT_DEEPSEEK_PRICE_TABLE: dict[str, tuple[float, float]] = {
    "deepseek-v4-flash": (0.14, 0.28),
    "deepseek-v4-pro": (0.435, 0.87),
}

_FALLBACK_PRICE: tuple[float, float] = (3.0, 15.0)

# OpenAI price table — (input_usd_per_mtok, output_usd_per_mtok).
# NOT used for billing; only for pre-flight cost estimates. o-series
# (o1/o3/o4-mini) models are intentionally NOT listed — they remain out of
# scope for other, unverified API differences (unrelated to the
# max_completion_tokens handling .openai() already covers for gpt-5.6).
_OPENAI_PRICE_TABLE: dict[str, tuple[float, float]] = {
    "gpt-5.6-sol": (5.00, 30.00),
    "gpt-5.6-terra": (2.50, 15.00),
    "gpt-5.6-luna": (1.00, 6.00),
}


# ---------------------------------------------------------------------------
# OpenAICompatibleProvider
# ---------------------------------------------------------------------------


class OpenAICompatibleProvider:
    """TranslationProvider implementation for any OpenAI /chat/completions endpoint.

    Constructor args:
        base_url:      e.g. "https://api.deepseek.com" or "https://openrouter.ai/api/v1"
        api_key:       Provider API key.
        default_model: Model string sent when the caller does not override.
        price_table:   Mapping of model_id → (input_usd_per_mtok, output_usd_per_mtok).
                       Used only for cost estimates.
        _client:       Injectable transport for testing (replaces the real openai.OpenAI).
    """

    # Retry-waste ceiling factor (consumed by the cost estimator / budget guard):
    # OpenAI-compatible endpoints fall through tier-1 tool → tier-2 JSON → tier-3,
    # and each tier that returns HTTP 200 is billed while re-sending the full
    # prompt. On the segmented SRT schema this fallthrough is common, so real
    # cost runs ~3x the happy-path estimate (job 0b86d4f2: est $0.012 vs real
    # $0.0395) — a heavy ceiling. Subclasses (Ollama) inherit it; local models
    # are free, so the factor is inert there.
    retry_waste_factor: float = 3.0

    # Completion-tokens parameter name sent to the API for the output-length
    # cap. OpenAI's newer gpt-5.6 family (sol/terra/luna) rejects the legacy
    # `max_tokens` kwarg and requires `max_completion_tokens` instead; the
    # `.openai()` preset overrides this as an INSTANCE attribute (same pattern
    # as retry_waste_factor). DeepSeek/Ollama/plain instances keep this class
    # default and are unaffected.
    _completion_tokens_param_name: str = "max_tokens"

    def __init__(
        self,
        base_url: str,
        api_key: str,
        default_model: str,
        price_table: dict[str, tuple[float, float]],
        _client: Any | None = None,
    ) -> None:
        self._base_url = base_url
        self._api_key = api_key
        self._default_model = default_model
        self._price_table = price_table

        if _client is not None:
            self._client = _client
        else:
            self._client = openai.OpenAI(
                base_url=base_url,
                api_key=api_key,
            )

    # ------------------------------------------------------------------
    # Properties exposed for test inspection
    # ------------------------------------------------------------------

    @property
    def base_url(self) -> str:
        return self._base_url

    @property
    def default_model(self) -> str:
        return self._default_model

    # ------------------------------------------------------------------
    # Factory / preset
    # ------------------------------------------------------------------

    @classmethod
    def deepseek(
        cls,
        api_key: str,
        default_model: str = "deepseek-v4-flash",
        extra_price_table: dict[str, tuple[float, float]] | None = None,
        _client: Any | None = None,
    ) -> "OpenAICompatibleProvider":
        """Preset for DeepSeek (https://api.deepseek.com).

        Prices: deepseek-v4-flash ≈ ($0.14, $0.28)/Mtok,
                deepseek-v4-pro   ≈ ($0.435, $0.87)/Mtok.
        The model string passed to translate() is forwarded unchanged.
        """
        table = dict(_DEFAULT_DEEPSEEK_PRICE_TABLE)
        if extra_price_table:
            table.update(extra_price_table)
        return cls(
            base_url="https://api.deepseek.com",
            api_key=api_key,
            default_model=default_model,
            price_table=table,
            _client=_client,
        )

    @classmethod
    def openai(
        cls,
        api_key: str,
        default_model: str = "gpt-5.6-luna",
        extra_price_table: dict[str, tuple[float, float]] | None = None,
        _client: Any | None = None,
    ) -> "OpenAICompatibleProvider":
        """Preset for OpenAI (https://api.openai.com/v1).

        Prices: gpt-5.6-sol   = ($5.00, $30.00)/Mtok,
                gpt-5.6-terra = ($2.50, $15.00)/Mtok,
                gpt-5.6-luna  = ($1.00, $6.00)/Mtok.
        The model string passed to translate() is forwarded unchanged.

        retry_waste_factor is overridden to 1.5 as an INSTANCE attribute (set
        here, after construction) — GPT models are Tier-1-reliable, so the
        class-level 3.0 default (tuned for DeepSeek's heavier tier-fallthrough)
        would over-estimate the worst-case cost ceiling. This does NOT touch
        the class attribute: DeepSeek/Ollama instances keep 3.0.

        _completion_tokens_param_name is overridden to "max_completion_tokens"
        as an INSTANCE attribute — the whole gpt-5.6 family (sol/terra/luna)
        rejects the legacy max_tokens kwarg. DeepSeek/Ollama/plain instances
        keep the class default "max_tokens" (unaffected by this override).

        o-series (o1/o3/o4-mini) models remain out of scope for OTHER,
        unverified API differences — this override does not enable them;
        selecting one surfaces the raw OpenAI API error.
        """
        table = dict(_OPENAI_PRICE_TABLE)
        if extra_price_table:
            table.update(extra_price_table)
        provider = cls(
            base_url="https://api.openai.com/v1",
            api_key=api_key,
            default_model=default_model,
            price_table=table,
            _client=_client,
        )
        provider.retry_waste_factor = 1.5  # instance attr; GPT is Tier-1-reliable
        provider._completion_tokens_param_name = "max_completion_tokens"  # instance attr
        return provider

    # ------------------------------------------------------------------
    # TranslationProvider Protocol
    # ------------------------------------------------------------------

    def translate(
        self, system: str, user: str, model: str, segment_count: int | None = None
    ) -> TranslationResult:
        """Return a TranslationResult with a validated TranslationUnit and real Usage.

        segment_count: when given (SRT cue batches), Tier-1 and Tier-3 request
        the SEGMENTED schema — a translations array of exactly segment_count
        strings.  Tier-2 (JSON mode) has no schema channel; it relies on the
        system prompt's segment instructions.

        Tier-1 → Tier-2 → Tier-3 fallback chain.
        429 retries the SAME call (with Retry-After sleep) before falling through.
        5xx errors share a global counter; ProviderError after MAX_5XX_RETRIES.
        All tiers exhausted → MalformedOutput.

        Usage accounting (cost-accuracy fix): every tier that receives an HTTP
        200 is BILLED, even when its content then fails to parse/validate. This
        method accumulates the real usage of every billed-but-failed tier across
        the whole 3-tier chain. On eventual SUCCESS it returns the SUM of all
        wasted tiers PLUS the successful one. On eventual FAILURE it raises
        MalformedOutput carrying the accumulated wasted usage. Transport failures
        (429 / 5xx) are NOT billed and contribute nothing.
        """
        server_err_count = 0
        last_error: Exception | None = None
        wasted = Usage()  # accumulated usage of billed-but-failed tiers

        # ---- TIER-1: tool/function calling (with inline 429 retry) ----
        t1 = self._call_with_retry(
            lambda s, u, m: self._tier1_tool_call(s, u, m, segment_count),
            system, user, model,
            server_err_count_ref=[server_err_count],
            fall_through_on_client_error=True,
        )
        if isinstance(t1, _Propagate):
            server_err_count = t1.server_err_count
            last_error = t1.last_error
        elif t1 is not None:
            unit, raw_response = t1
            if unit is not None:
                return TranslationResult(
                    unit=unit, usage=_sum_usage(wasted, self._extract_usage(raw_response))
                )
            # Billed but no valid tool_call → accrue usage, fall through to Tier-2.
            wasted = _sum_usage(wasted, self._extract_usage(raw_response))

        # ---- TIER-2: JSON mode ----
        t2 = self._call_with_retry(
            self._tier2_json_mode, system, user, model,
            server_err_count_ref=[server_err_count],
            fall_through_on_client_error=True,
        )
        if isinstance(t2, _Propagate):
            server_err_count = t2.server_err_count
            last_error = t2.last_error
        elif t2 is not None:
            tier2_unit, empty_content, t2_response = t2  # type: ignore[misc]
            if tier2_unit is not None:
                return TranslationResult(
                    unit=tier2_unit, usage=_sum_usage(wasted, self._extract_usage(t2_response))
                )
            # Billed but empty/invalid content → accrue usage, fall through to Tier-3.
            wasted = _sum_usage(wasted, self._extract_usage(t2_response))

        # ---- TIER-3: prompt-and-parse fallback (up to MAX_TIER3_RETRIES calls) ----
        for attempt in range(MAX_TIER3_RETRIES):
            t3 = self._call_with_retry(
                lambda s, u, m: self._tier3_prompt_parse(s, u, m, attempt, segment_count),
                system, user, model,
                server_err_count_ref=[server_err_count],
            )
            if isinstance(t3, _Propagate):
                server_err_count = t3.server_err_count
                last_error = t3.last_error
            elif t3 is not None:
                t3_unit, t3_response = t3  # type: ignore[misc]
                if t3_unit is not None:
                    return TranslationResult(
                        unit=t3_unit, usage=_sum_usage(wasted, self._extract_usage(t3_response))
                    )
                # Billed but invalid → accrue usage and retry the next Tier-3 attempt.
                wasted = _sum_usage(wasted, self._extract_usage(t3_response))
                last_error = ValueError("Tier-3: no valid JSON in response")
            else:
                last_error = ValueError("Tier-3: no valid JSON in response")

        raise MalformedOutput(job_id="unknown", chunk_index=-1, usage=wasted) from last_error

    # ------------------------------------------------------------------
    # Internal call wrapper (429 + 5xx handling)
    # ------------------------------------------------------------------

    def _call_with_retry(
        self, fn, system, user, model, server_err_count_ref,
        fall_through_on_client_error: bool = False,
    ):
        """Call fn(system, user, model); handle 429 (sleep + retry once) and 5xx.

        fall_through_on_client_error: when True, a 4xx response (other than 429)
        returns _Propagate so the caller falls through to the next tier instead
        of raising ProviderError. Tier-1/Tier-2 set this because endpoints
        reject unsupported capabilities (tools / JSON mode) with a 400 that is
        model-specific, not fatal. Tier-3 (plain chat) keeps the default: a 4xx
        there means the request itself is broken.

        Returns:
          - The return value of fn on success.
          - None if fn returns None (caller falls through to next tier).
          - _Propagate to relay updated server_err_count + last_error to caller.
        """
        server_err_count = server_err_count_ref[0]
        last_error = None

        max_rate_retries = MAX_5XX_RETRIES  # bound rate-limit retries too
        for rate_attempt in range(max_rate_retries):
            try:
                return fn(system, user, model)
            except openai.RateLimitError as exc:
                _sleep_for_retry_after(exc)
                last_error = exc
                # Retry the same call after sleeping (rate-limit is transient)
                continue
            except (openai.InternalServerError, openai.APIStatusError) as exc:
                status = _get_status(exc)
                if status is not None and status >= 500:
                    server_err_count += 1
                    server_err_count_ref[0] = server_err_count
                    time.sleep(_backoff(server_err_count - 1))
                    last_error = exc
                    if server_err_count >= MAX_5XX_RETRIES:
                        raise ProviderError(status_code=status) from exc
                    return _Propagate(server_err_count=server_err_count, last_error=exc)
                elif fall_through_on_client_error and status is not None and 400 <= status < 500:
                    last_error = exc
                    return _Propagate(server_err_count=server_err_count, last_error=exc)
                else:
                    raise ProviderError(status_code=status) from exc
            except (ValidationError, json.JSONDecodeError, ValueError) as exc:
                last_error = exc
                return _Propagate(server_err_count=server_err_count, last_error=exc)

        return _Propagate(server_err_count=server_err_count, last_error=last_error)

    def count_tokens(self, text: str, model: str) -> int:  # noqa: ARG002
        """Approximate token count using word-count heuristic.

        The OpenAI SDK does not expose a token-counting API for arbitrary
        endpoints. Approximation: word count × 1.3 (same as AnthropicProvider
        fallback). For cost estimation this is conservative and sufficient.
        """
        return max(0, round(len(text.split()) * 1.3))

    def price(self, model: str) -> tuple[float, float]:
        """Return (input_usd_per_mtok, output_usd_per_mtok) for the given model.

        Falls back to _FALLBACK_PRICE for unknown models.
        """
        return self._price_table.get(model, _FALLBACK_PRICE)

    # ------------------------------------------------------------------
    # Tier internals
    # ------------------------------------------------------------------

    @staticmethod
    def _extract_usage(response: Any) -> Usage:
        """Extract real token usage from an OpenAI-compatible response.

        The OpenAI wire format exposes usage.prompt_tokens and usage.completion_tokens
        on the top-level response object.  If these attributes are missing (fake client,
        streaming, or endpoint quirk), fall back to zero Usage.
        """
        try:
            raw = response.usage
            return Usage(
                input_tokens=int(raw.prompt_tokens),
                output_tokens=int(raw.completion_tokens),
            )
        except (AttributeError, TypeError, ValueError):
            return Usage()

    def _tier1_tool_call(
        self, system: str, user: str, model: str, segment_count: int | None = None
    ) -> tuple[TranslationUnit | None, Any]:
        """TIER-1: function/tool-calling.

        Always returns (unit_or_None, raw_response): the unit when the response
        contains a valid tool_call, None otherwise (caller falls through to
        Tier-2). The raw response is ALWAYS returned so the caller can extract
        real Usage — this response was billed even when the tool_call is
        missing/invalid.
        """
        response = self._client.chat.completions.create(
            model=model,
            messages=[
                {"role": "system", "content": system},
                {"role": "user", "content": user},
            ],
            tools=_translation_tools(segment_count),
            tool_choice="auto",
            **{self._completion_tokens_param_name: _MAX_OUTPUT_TOKENS},
        )
        choice = response.choices[0]
        message = choice.message
        tool_calls = getattr(message, "tool_calls", None)
        if tool_calls:
            for tc in tool_calls:
                fn = getattr(tc, "function", None)
                if fn is None:
                    continue
                name = getattr(fn, "name", None)
                args_str = getattr(fn, "arguments", None)
                if name == _TOOL_NAME and args_str:
                    try:
                        data = json.loads(args_str)
                        unit = TranslationUnit.model_validate(data)
                        return unit, response
                    except (json.JSONDecodeError, ValidationError):
                        return None, response
        return None, response

    def _tier2_json_mode(
        self, system: str, user: str, model: str
    ) -> tuple[TranslationUnit | None, bool, Any]:
        """TIER-2: JSON mode (response_format={'type': 'json_object'}).

        Returns (TranslationUnit | None, empty_content: bool, raw_response).
        The DeepSeek quirk: when content is empty, returns (None, True, response) so
        the caller can fall through to Tier-3 (do NOT treat as a parse error).
        The raw_response is returned so the caller can extract real Usage.
        """
        # OpenAI JSON-mode requirement: the word "json" must appear in the prompt.
        json_hint = " Respond with a JSON object only."
        response = self._client.chat.completions.create(
            model=model,
            messages=[
                {"role": "system", "content": system + json_hint},
                {"role": "user", "content": user},
            ],
            response_format={"type": "json_object"},
            **{self._completion_tokens_param_name: _MAX_OUTPUT_TOKENS},
        )
        choice = response.choices[0]
        content = getattr(choice.message, "content", None) or ""
        if not content.strip():
            # DeepSeek JSON-mode quirk: empty content — signal caller to fall through
            return None, True, response
        try:
            data = json.loads(content)
            unit = TranslationUnit.model_validate(data)
            return unit, False, response
        except (json.JSONDecodeError, ValidationError):
            return None, False, response

    def _tier3_prompt_parse(
        self, system: str, user: str, model: str, attempt: int,
        segment_count: int | None = None,
    ) -> tuple[TranslationUnit | None, Any]:
        """TIER-3: prompt-and-parse fallback.

        Instructs the model to output JSON in the user prompt and parses the
        text response.  Called up to MAX_TIER3_RETRIES times.
        Always returns (unit_or_None, raw_response): the unit on success, None on
        parse failure. The raw response is ALWAYS returned so the caller can
        accrue real Usage — this response was billed even when parsing failed.
        """
        json_instruction = (
            "\n\nYou MUST respond ONLY with a valid JSON object matching this schema:\n"
            f"{json.dumps(translation_tool_schema(segment_count), indent=2)}\n"
            "Do NOT include any markdown fences or extra text."
        )
        if attempt > 0:
            json_instruction = "\n\nPrevious response was not valid JSON. " + json_instruction

        response = self._client.chat.completions.create(
            model=model,
            messages=[
                {"role": "system", "content": system},
                {"role": "user", "content": user + json_instruction},
            ],
            **{self._completion_tokens_param_name: _MAX_OUTPUT_TOKENS},
        )
        choice = response.choices[0]
        content = getattr(choice.message, "content", None) or ""
        content = content.strip()
        # Strip markdown fences if present
        if content.startswith("```"):
            lines = content.split("\n")
            content = "\n".join(lines[1:-1] if lines[-1].strip() == "```" else lines[1:])
        try:
            data = json.loads(content)
            unit = TranslationUnit.model_validate(data)
            return unit, response
        except (json.JSONDecodeError, ValidationError):
            return None, response


# ---------------------------------------------------------------------------
# Internal sentinel
# ---------------------------------------------------------------------------


class _Propagate:
    """Sentinel returned by _call_with_retry to relay error state to the translate loop."""

    __slots__ = ("server_err_count", "last_error")

    def __init__(self, *, server_err_count: int, last_error: Exception | None) -> None:
        self.server_err_count = server_err_count
        self.last_error = last_error


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _sum_usage(a: Usage, b: Usage) -> Usage:
    """Return a new Usage that is the element-wise sum of two Usage values."""
    return Usage(
        input_tokens=a.input_tokens + b.input_tokens,
        output_tokens=a.output_tokens + b.output_tokens,
    )


def _get_status(exc: Any) -> int | None:
    """Extract HTTP status code from an openai error, or None."""
    try:
        return exc.status_code
    except AttributeError:
        pass
    try:
        return exc.response.status_code
    except AttributeError:
        return None


def _sleep_for_retry_after(exc: Any) -> None:
    """Sleep for the Retry-After header value, or fall back to a short default."""
    try:
        value = exc.response.headers.get("Retry-After")
        if value is not None:
            time.sleep(float(value))
            return
    except Exception:
        pass
    time.sleep(1.0)


def _backoff(attempt: int, base: float = 1.0, cap: float = 32.0) -> float:
    """Exponential backoff with jitter (mirrors AnthropicProvider)."""
    delay = min(base * (2**attempt), cap)
    jitter = random.uniform(0, delay * 0.1)
    return delay + jitter
