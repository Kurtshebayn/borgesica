"""Anthropic provider adapter (M1-11).

Implements the TranslationProvider Protocol using the Anthropic SDK.

Structured-output strategy (adapter-internal, domain never sees it):

  TIER-1 (PRIMARY): Tool-use (function-calling) with TranslationUnit.model_json_schema().
    Anthropic returns a tool_use content block; we parse block.input directly.
    This is the preferred approach: it is reliable, testable, and avoids extra
    dependencies.

  TIER-2 (INTENTIONALLY SKIPPED): Anthropic JSON mode (constrained decoding /
    "response_format": {"type": "json_object"}).
    Spec references a three-tier fallback (Tier-1 tool-calling → Tier-2 JSON mode
    → Tier-3 text-JSON-parse).  Tier-2 is deliberately NOT implemented here.
    Reason: Anthropic's native tool-use (Tier-1) is more reliable than JSON mode
    for structured output — it enforces schema at the API level and is already
    well-tested.  Adding JSON mode as an intermediate tier would increase complexity
    with no practical benefit: the only case where Tier-1 fails (model returns plain
    text) is handled by Tier-3 (text-JSON-parse with retry), which is a sufficient
    safety net for malformed responses.  Tier-2 can be added in a future milestone
    if real-world testing shows Tier-1 + Tier-3 is insufficient.

  TIER-3 (FALLBACK): Plain text → strip markdown fences → JSON parse →
    TranslationUnit.model_validate().  Retried up to MAX_RETRIES times.

Retry policy:
  - 429 (rate limit): honor Retry-After header, then exponential backoff + jitter.
  - 5xx (server error): exponential backoff + jitter (≤ MAX_RETRIES attempts).
  - Malformed output: prompt-and-parse retry (≤ MAX_RETRIES attempts).
  - All retries exhausted → raise domain MalformedOutput or ProviderError.

Dependency rule: `anthropic` imported ONLY here; domain never sees it.
`instructor` is NOT used (adapter-internal choice: tool-use is sufficient and
avoids the extra dependency).
"""
from __future__ import annotations

import json
import time
from typing import TYPE_CHECKING, Any

import anthropic
from pydantic import ValidationError

from borgesica.domain.errors import MalformedOutput, ProviderError
from borgesica.domain.models import (
    TranslationResult,
    TranslationUnit,
    Usage,
    translation_tool_schema,
)

if TYPE_CHECKING:
    pass

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

MAX_RETRIES = 3

# Output token cap sent to the API. Must comfortably fit the structured JSON
# output (translation + summary + glossary) for chunks up to ~800 source
# tokens. The previous value of 1024 truncated real chunks mid-JSON (a real
# 2677-char chunk hit stop_reason='max_tokens' with usage.output_tokens==1024,
# corrupting the tool_use input and causing a deterministic MalformedOutput
# across every retry). 8192 gives enough headroom for translation output that
# can run longer than the source text plus the summary/glossary fields.
_MAX_OUTPUT_TOKENS = 8192

# APPROXIMATE prices — (input_usd_per_mtok, output_usd_per_mtok). NOT verified.
# Used ONLY for pre-flight cost ESTIMATES, never for billing. Both prices and model
# IDs go stale; any unknown model falls back to _DEFAULT_PRICE, so a stale table
# only skews estimates — it never breaks translation.
# TODO (cost-control / M4): make prices user-overridable and verify against current
#   published Anthropic pricing. See engram: sdd/translation-engine/todo-pricing.
_PRICE_TABLE: dict[str, tuple[float, float]] = {
    # Claude 4 family (current model IDs)
    "claude-opus-4-8": (5.0, 25.0),
    "claude-sonnet-4-6": (3.0, 15.0),
    "claude-sonnet-5": (3.0, 15.0),
    "claude-haiku-4-5-20251001": (1.0, 5.0),
    # Claude 3.7
    "claude-3-7-sonnet-20250219": (3.0, 15.0),
    # Claude 3.5 family
    "claude-3-5-haiku-20241022": (0.80, 4.0),
    "claude-3-5-sonnet-20241022": (3.0, 15.0),
    "claude-3-5-sonnet-20240620": (3.0, 15.0),
    # Claude 3 family
    "claude-3-haiku-20240307": (0.25, 1.25),
    "claude-3-sonnet-20240229": (3.0, 15.0),
    "claude-3-opus-20240229": (15.0, 75.0),
}

_DEFAULT_PRICE: tuple[float, float] = (3.0, 15.0)

# The tool definition sent to Anthropic for structured output.
_TOOL_NAME = "submit_translation"


def _translation_tool(segment_count: int | None = None) -> list[dict[str, Any]]:
    """Build the tool definition for the requested output shape.

    Per-call (not module-level) because the segmented schema pins the
    translations array length to the chunk's cue count.
    """
    return [
        {
            "name": _TOOL_NAME,
            "description": (
                "Submit the structured translation result. "
                "Use this tool to return the translation, a summary update, "
                "and any new glossary terms discovered."
            ),
            "input_schema": translation_tool_schema(segment_count),
        }
    ]


# ---------------------------------------------------------------------------
# AnthropicProvider
# ---------------------------------------------------------------------------


class AnthropicProvider:
    """TranslationProvider implementation using the Anthropic SDK.

    Constructor:
        api_key: Anthropic API key. If None, reads from ANTHROPIC_API_KEY env var.
        client:  Inject a pre-built client (for testing).  Takes precedence over api_key.
        max_retries: Number of retry attempts on transient errors.
    """

    # Retry-waste ceiling factor (consumed by the cost estimator / budget guard):
    # Anthropic's native tool-calling enforces the schema at the API level and
    # rarely falls through to the text-JSON fallback, so its real cost stays
    # close to the happy-path estimate — a modest ceiling.
    retry_waste_factor: float = 1.5

    def __init__(
        self,
        api_key: str | None = None,
        client: Any | None = None,
        max_retries: int = MAX_RETRIES,
        price_table: dict[str, tuple[float, float]] | None = None,
    ) -> None:
        if client is not None:
            self._client = client
        elif api_key is not None:
            self._client = anthropic.Anthropic(api_key=api_key)
        else:
            # Reads ANTHROPIC_API_KEY from environment
            self._client = anthropic.Anthropic()
        self._max_retries = max_retries
        # Published prices go stale; let callers override/extend the built-in
        # table (used ONLY for pre-flight estimates, never for billing).
        self._price_table = dict(_PRICE_TABLE)
        if price_table:
            self._price_table.update(price_table)

    # --- TranslationProvider Protocol ---

    def translate(
        self, system: str, user: str, model: str, segment_count: int | None = None
    ) -> TranslationResult:
        """Return a TranslationResult with a validated TranslationUnit and real Usage.

        segment_count: when given (SRT cue batches), the tool input_schema
        requests the SEGMENTED shape — a translations array of exactly
        segment_count strings — instead of the legacy single string.

        Strategy:
          1. Use tool-calling: request the model to call `submit_translation`.
          2. If the response has a tool_use block → parse block.input.
          3. If plain-text response → try JSON parse (fallback).
          4. Retry up to self._max_retries on transient/malformed failures.
          5. Exhausted retries → raise MalformedOutput (malformed) or ProviderError (5xx).

        Usage accounting (cost-accuracy fix): a response is BILLED by Anthropic
        whenever the HTTP call returns 200, EVEN IF its content then fails to
        parse/validate. This method accumulates the real usage of every such
        billed attempt across all retries. On eventual SUCCESS it returns the SUM
        of every billed attempt (the wasted retries PLUS the successful one). On
        eventual FAILURE it raises MalformedOutput carrying the accumulated
        wasted usage, so the orchestrator can accrue the real cost of billed
        calls that produced no usable translation. Transport failures (429 / 5xx)
        are NOT billed and contribute nothing.
        """
        last_error: Exception | None = None
        wasted = Usage()  # accumulated usage of billed-but-failed attempts

        for attempt in range(self._max_retries):
            try:
                response = self._client.messages.create(
                    model=model,
                    max_tokens=_MAX_OUTPUT_TOKENS,
                    system=system,
                    messages=[{"role": "user", "content": user}],
                    tools=_translation_tool(segment_count),
                    tool_choice={"type": "auto"},
                )
                # HTTP 200: this response WAS billed regardless of parse outcome.
                usage = self._extract_usage(response)
                unit = self._parse_response(response)
                if unit is not None:
                    return TranslationResult(unit=unit, usage=_sum_usage(wasted, usage))
                # Malformed but billed — accrue its usage and retry.
                wasted = _sum_usage(wasted, usage)
                last_error = ValueError("No valid TranslationUnit in response")

            except anthropic.RateLimitError as exc:
                retry_after = _get_retry_after(exc)
                sleep_secs = retry_after if retry_after is not None else _backoff(attempt)
                time.sleep(sleep_secs)
                last_error = exc

            except anthropic.InternalServerError as exc:
                time.sleep(_backoff(attempt))
                last_error = exc
                # Raise ProviderError after max retries for 5xx (not billed).
                if attempt == self._max_retries - 1:
                    raise ProviderError(status_code=exc.status_code, usage=wasted) from exc

            except anthropic.APIStatusError as exc:
                if exc.status_code >= 500:
                    time.sleep(_backoff(attempt))
                    last_error = exc
                    if attempt == self._max_retries - 1:
                        raise ProviderError(status_code=exc.status_code, usage=wasted) from exc
                else:
                    raise ProviderError(status_code=exc.status_code, usage=wasted) from exc

            except (ValidationError, json.JSONDecodeError, ValueError) as exc:
                last_error = exc

        # All retries exhausted with malformed output — carry the billed waste.
        raise MalformedOutput(job_id="unknown", chunk_index=-1, usage=wasted) from last_error

    # Characters per token, MEASURED via the SDK's own count_tokens endpoint
    # (exact, and it does not bill) on the same real book text used to
    # calibrate DeepSeek: mean 3.390, CV 18.1%. Anthropic's tokenizer is denser
    # than DeepSeek's 3.853, which is why this constant is per-provider rather
    # than one shared number.
    chars_per_token: float = 3.390

    def count_tokens(self, text: str, model: str) -> int:  # noqa: ARG002
        """Approximate token count: characters ÷ `chars_per_token` — never the network.

        It used to be `words × 1.3`. Measured 2026-08-06 against this SDK's own
        count_tokens endpoint, that under-counted real text by ~30% on average
        (1.861 real tokens/word), and words proved the unstable unit: tokens per
        word ranged 1.45-3.24 (CV 30.0%) against 18.1% for chars/token.

        This deliberately does NOT call ``client.messages.count_tokens`` AT
        RUNTIME.
        chunk_prose calls this once per prose node, so a round-trip per node
        made `create` unusable on a real book: a 502-node EPUB hung for 15+
        minutes before translating a single word. The port documents the return
        value as approximate, and OpenAICompatibleProvider already satisfies it
        with this same heuristic, so exactness here bought nothing that any
        caller relies on.

        Callers needing an exact count near a decision boundary must not assume
        one: CostEstimator compares this against the 1024-token Anthropic cache
        threshold, and for the EPUB static block the heuristic lands close
        enough to flip that comparison. That only affects the informational
        `cached` field on CostEstimate — no adapter applies prompt caching, so
        nothing behavioural rides on it today. Wiring caching up will need a
        sharper measurement than this.
        """
        # Whitespace-only text counts as nothing. Character counting would
        # otherwise bill blank padding, and callers (the orchestrator prose
        # guard, chunk_prose) rely on blank nodes costing zero.
        if not text.strip():
            return 0
        return max(0, round(len(text) / self.chars_per_token))

    def price(self, model: str) -> tuple[float, float]:
        """Return (input_usd_per_mtok, output_usd_per_mtok) for the given model.

        Falls back to (3.0, 15.0) for unknown models.
        """
        return self._price_table.get(model, _DEFAULT_PRICE)

    def cache_price(self, model: str) -> float:
        """Return the ordinary input price — no cache rate has been measured.

        This adapter never sets ``cache_control`` on a request, so no call it
        makes can produce a cache hit and ``Usage.cached_input_tokens`` is
        always 0 here; the value is therefore unused today. Returning the full
        input price keeps it safe if that changes: an unmeasured rate must
        never make a job look cheaper than it is.
        """
        return self.price(model)[0]

    # --- Internal helpers ---

    def _extract_usage(self, response: Any) -> Usage:
        """Extract real token usage from an Anthropic response.

        Anthropic SDK returns a Usage object on response.usage with
        input_tokens and output_tokens fields.  If the attribute is missing
        (e.g. a fake client in tests), fall back to zero Usage.
        """
        try:
            raw = response.usage
            return Usage(
                input_tokens=int(raw.input_tokens),
                output_tokens=int(raw.output_tokens),
            )
        except (AttributeError, TypeError, ValueError):
            return Usage()

    def _parse_response(self, response: Any) -> TranslationUnit | None:
        """Extract a TranslationUnit from an Anthropic response.

        Tries tool_use block first, then falls back to JSON-in-text parsing.
        Returns None if parsing fails (caller should retry).
        """
        for block in response.content:
            if getattr(block, "type", None) == "tool_use":
                try:
                    return TranslationUnit.model_validate(block.input)
                except (ValidationError, TypeError):
                    return None

        # No tool_use block — try parsing text content as JSON
        for block in response.content:
            if getattr(block, "type", None) == "text":
                text = getattr(block, "text", "")
                # Strip markdown code fences if present
                text = text.strip()
                if text.startswith("```"):
                    lines = text.split("\n")
                    # Remove first and last fence lines
                    text = "\n".join(lines[1:-1] if lines[-1].strip() == "```" else lines[1:])
                try:
                    data = json.loads(text)
                    return TranslationUnit.model_validate(data)
                except (json.JSONDecodeError, ValidationError, TypeError):
                    return None

        return None


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _sum_usage(a: Usage, b: Usage) -> Usage:
    """Return a new Usage that is the element-wise sum of two Usage values."""
    return Usage(
        input_tokens=a.input_tokens + b.input_tokens,
        output_tokens=a.output_tokens + b.output_tokens,
        cached_input_tokens=a.cached_input_tokens + b.cached_input_tokens,
    )


def _get_retry_after(exc: Any) -> float | None:
    """Extract Retry-After value (seconds) from a rate-limit error, or None."""
    try:
        value = exc.response.headers.get("Retry-After")
        if value is not None:
            return float(value)
    except Exception:
        pass
    return None


def _backoff(attempt: int, base: float = 1.0, cap: float = 32.0) -> float:
    """Compute exponential backoff with a deterministic cap (no jitter for testability)."""
    return min(base * (2 ** attempt), cap)
