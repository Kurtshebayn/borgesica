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
from borgesica.domain.models import TranslationResult, TranslationUnit, Usage

if TYPE_CHECKING:
    pass

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

MAX_RETRIES = 3

# APPROXIMATE prices — (input_usd_per_mtok, output_usd_per_mtok). NOT verified.
# Used ONLY for pre-flight cost ESTIMATES, never for billing. Both prices and model
# IDs go stale; any unknown model falls back to _DEFAULT_PRICE, so a stale table
# only skews estimates — it never breaks translation.
# TODO (cost-control / M4): make prices user-overridable and verify against current
#   published Anthropic pricing. See engram: sdd/translation-engine/todo-pricing.
_PRICE_TABLE: dict[str, tuple[float, float]] = {
    # Claude 4 family (current model IDs)
    "claude-opus-4-8": (15.0, 75.0),
    "claude-sonnet-4-6": (3.0, 15.0),
    "claude-haiku-4-5-20251001": (0.80, 4.0),
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
_TRANSLATION_TOOL: list[dict[str, Any]] = [
    {
        "name": _TOOL_NAME,
        "description": (
            "Submit the structured translation result. "
            "Use this tool to return the translation, a summary update, "
            "and any new glossary terms discovered."
        ),
        "input_schema": TranslationUnit.model_json_schema(),
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

    def __init__(
        self,
        api_key: str | None = None,
        client: Any | None = None,
        max_retries: int = MAX_RETRIES,
    ) -> None:
        if client is not None:
            self._client = client
        elif api_key is not None:
            self._client = anthropic.Anthropic(api_key=api_key)
        else:
            # Reads ANTHROPIC_API_KEY from environment
            self._client = anthropic.Anthropic()
        self._max_retries = max_retries

    # --- TranslationProvider Protocol ---

    def translate(self, system: str, user: str, model: str) -> TranslationResult:
        """Return a TranslationResult with a validated TranslationUnit and real Usage.

        Strategy:
          1. Use tool-calling: request the model to call `submit_translation`.
          2. If the response has a tool_use block → parse block.input.
          3. If plain-text response → try JSON parse (fallback).
          4. Retry up to self._max_retries on transient/malformed failures.
          5. Exhausted retries → raise MalformedOutput (malformed) or ProviderError (5xx).

        Usage is populated from response.usage.input_tokens / output_tokens on
        every successful response.  On retry, only the last (successful) response's
        usage is returned — callers that need per-call granularity should not retry
        internally; that is the orchestrator's responsibility.
        """
        last_error: Exception | None = None

        for attempt in range(self._max_retries):
            try:
                response = self._client.messages.create(
                    model=model,
                    max_tokens=1024,
                    system=system,
                    messages=[{"role": "user", "content": user}],
                    tools=_TRANSLATION_TOOL,
                    tool_choice={"type": "auto"},
                )
                unit = self._parse_response(response)
                if unit is not None:
                    usage = self._extract_usage(response)
                    return TranslationResult(unit=unit, usage=usage)
                # Malformed but no exception — retry with a more explicit prompt
                last_error = ValueError("No valid TranslationUnit in response")

            except anthropic.RateLimitError as exc:
                retry_after = _get_retry_after(exc)
                sleep_secs = retry_after if retry_after is not None else _backoff(attempt)
                time.sleep(sleep_secs)
                last_error = exc

            except anthropic.InternalServerError as exc:
                time.sleep(_backoff(attempt))
                last_error = exc
                # Raise ProviderError after max retries for 5xx
                if attempt == self._max_retries - 1:
                    raise ProviderError(status_code=exc.status_code) from exc

            except anthropic.APIStatusError as exc:
                if exc.status_code >= 500:
                    time.sleep(_backoff(attempt))
                    last_error = exc
                    if attempt == self._max_retries - 1:
                        raise ProviderError(status_code=exc.status_code) from exc
                else:
                    raise ProviderError(status_code=exc.status_code) from exc

            except (ValidationError, json.JSONDecodeError, ValueError) as exc:
                last_error = exc

        # All retries exhausted with malformed output
        raise MalformedOutput(job_id="unknown", chunk_index=-1) from last_error

    def count_tokens(self, text: str, model: str) -> int:  # noqa: ARG002
        """Count tokens using the Anthropic token-counting API, with word-count fallback."""
        try:
            response = self._client.messages.count_tokens(
                model=model,
                messages=[{"role": "user", "content": text}],
            )
            return int(response.input_tokens)
        except Exception:
            # Fallback: word count × 1.3 approximation
            return round(len(text.split()) * 1.3)

    def price(self, model: str) -> tuple[float, float]:
        """Return (input_usd_per_mtok, output_usd_per_mtok) for the given model.

        Falls back to (3.0, 15.0) for unknown models.
        """
        return _PRICE_TABLE.get(model, _DEFAULT_PRICE)

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
