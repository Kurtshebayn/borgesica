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
from borgesica.domain.models import TranslationUnit

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

MAX_5XX_RETRIES = 3
MAX_TIER3_RETRIES = 2  # Tier-3 is retried at most 2 times (2 HTTP calls)

# Tool / function definition sent for Tier-1 structured output.
_TOOL_NAME = "submit_translation"
_TRANSLATION_TOOLS: list[dict[str, Any]] = [
    {
        "type": "function",
        "function": {
            "name": _TOOL_NAME,
            "description": (
                "Submit the structured translation result. "
                "Use this function to return the translation, a summary update, "
                "and any new glossary terms discovered."
            ),
            "parameters": TranslationUnit.model_json_schema(),
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

    # ------------------------------------------------------------------
    # TranslationProvider Protocol
    # ------------------------------------------------------------------

    def translate(self, system: str, user: str, model: str) -> TranslationUnit:
        """Return a validated TranslationUnit.

        Tier-1 → Tier-2 → Tier-3 fallback chain.
        429 retries the SAME call (with Retry-After sleep) before falling through.
        5xx errors share a global counter; ProviderError after MAX_5XX_RETRIES.
        All tiers exhausted → MalformedOutput.
        """
        server_err_count = 0
        last_error: Exception | None = None

        # ---- TIER-1: tool/function calling (with inline 429 retry) ----
        unit = self._call_with_retry(
            self._tier1_tool_call, system, user, model,
            server_err_count_ref=[server_err_count],
        )
        if isinstance(unit, _Propagate):
            server_err_count = unit.server_err_count
            last_error = unit.last_error
        elif unit is not None:
            return unit
        # unit is None → Tier-1 returned no tool_call → fall through to Tier-2

        # ---- TIER-2: JSON mode ----
        result = self._call_with_retry(
            self._tier2_json_mode, system, user, model,
            server_err_count_ref=[server_err_count],
        )
        if isinstance(result, _Propagate):
            server_err_count = result.server_err_count
            last_error = result.last_error
        elif result is not None:
            tier2_unit, empty_content = result  # type: ignore[misc]
            if tier2_unit is not None:
                return tier2_unit
            # None + empty_content → DeepSeek quirk → fall through to Tier-3

        # ---- TIER-3: prompt-and-parse fallback (up to MAX_TIER3_RETRIES calls) ----
        for attempt in range(MAX_TIER3_RETRIES):
            t3_result = self._call_with_retry(
                lambda s, u, m: self._tier3_prompt_parse(s, u, m, attempt),
                system, user, model,
                server_err_count_ref=[server_err_count],
            )
            if isinstance(t3_result, _Propagate):
                server_err_count = t3_result.server_err_count
                last_error = t3_result.last_error
            elif t3_result is not None:
                return t3_result  # type: ignore[return-value]
            else:
                last_error = ValueError("Tier-3: no valid JSON in response")

        raise MalformedOutput(job_id="unknown", chunk_index=-1) from last_error

    # ------------------------------------------------------------------
    # Internal call wrapper (429 + 5xx handling)
    # ------------------------------------------------------------------

    def _call_with_retry(self, fn, system, user, model, server_err_count_ref):
        """Call fn(system, user, model); handle 429 (sleep + retry once) and 5xx.

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

    def _tier1_tool_call(self, system: str, user: str, model: str) -> TranslationUnit | None:
        """TIER-1: function/tool-calling.

        Returns a TranslationUnit if the response contains a valid tool_call,
        None otherwise (caller falls through to Tier-2).
        """
        response = self._client.chat.completions.create(
            model=model,
            messages=[
                {"role": "system", "content": system},
                {"role": "user", "content": user},
            ],
            tools=_TRANSLATION_TOOLS,
            tool_choice="auto",
            max_tokens=1024,
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
                        return TranslationUnit.model_validate(data)
                    except (json.JSONDecodeError, ValidationError):
                        return None
        return None

    def _tier2_json_mode(
        self, system: str, user: str, model: str
    ) -> tuple[TranslationUnit | None, bool]:
        """TIER-2: JSON mode (response_format={'type': 'json_object'}).

        Returns (TranslationUnit | None, empty_content: bool).
        The DeepSeek quirk: when content is empty, returns (None, True) so the
        caller can fall through to Tier-3 (do NOT treat as a parse error).
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
            max_tokens=1024,
        )
        choice = response.choices[0]
        content = getattr(choice.message, "content", None) or ""
        if not content.strip():
            # DeepSeek JSON-mode quirk: empty content — signal caller to fall through
            return None, True
        try:
            data = json.loads(content)
            unit = TranslationUnit.model_validate(data)
            return unit, False
        except (json.JSONDecodeError, ValidationError):
            return None, False

    def _tier3_prompt_parse(
        self, system: str, user: str, model: str, attempt: int
    ) -> TranslationUnit | None:
        """TIER-3: prompt-and-parse fallback.

        Instructs the model to output JSON in the user prompt and parses the
        text response.  Called up to MAX_TIER3_RETRIES times.
        """
        json_instruction = (
            "\n\nYou MUST respond ONLY with a valid JSON object matching this schema:\n"
            f"{json.dumps(TranslationUnit.model_json_schema(), indent=2)}\n"
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
            max_tokens=1024,
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
            return TranslationUnit.model_validate(data)
        except (json.JSONDecodeError, ValidationError):
            return None


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
