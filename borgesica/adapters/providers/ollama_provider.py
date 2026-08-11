"""OllamaProvider adapter (M4-4).

Ollama exposes an OpenAI-compatible /v1/chat/completions endpoint, so
OllamaProvider is a thin subclass of OpenAICompatibleProvider that presets
Ollama defaults.  This gives the full structured-output tiering for free:

  TIER-1 (PRIMARY): Tool/function-calling — used when the local model supports it.
  TIER-2 (JSON MODE): response_format={"type": "json_object"}.
  TIER-3 (PROMPT-AND-PARSE): Plain JSON instruction + text parse, retried ≤ 2.
                               This is the floor weak/local models typically land on.

The Tier-3 floor is exactly what makes OllamaProvider useful: even models that
don't support tool-calling reliably will eventually produce parseable JSON with
explicit prompt guidance.

Design decision: subclass (not wrapper) because OllamaProvider IS an
OpenAICompatibleProvider with different defaults.  A wrapper would duplicate the
constructor signature without adding clarity.

Dependency: reuses the `openai` SDK already pulled in by the `openai-compat`
optional extra added in M4-3.  No native `ollama` client library is needed.

OLLAMA_HOST env var: if set, its value (e.g. "192.168.1.42:11434") replaces
"localhost:11434" in the default base_url.  This matches Ollama's own
OLLAMA_HOST convention and is evaluated at OllamaProvider instantiation time.
"""
from __future__ import annotations

import os
from typing import Any

from borgesica.adapters.providers.openai_compatible_provider import OpenAICompatibleProvider

# ---------------------------------------------------------------------------
# Zero-cost price table — local inference has no per-token API cost
# ---------------------------------------------------------------------------

_OLLAMA_PRICE_TABLE: dict[str, tuple[float, float]] = {}
_OLLAMA_FALLBACK_PRICE: tuple[float, float] = (0.0, 0.0)


# ---------------------------------------------------------------------------
# OllamaProvider
# ---------------------------------------------------------------------------


class OllamaProvider(OpenAICompatibleProvider):
    """TranslationProvider for a local Ollama instance.

    Uses Ollama's OpenAI-compatible /v1 endpoint (http://localhost:11434/v1
    by default, or http://{OLLAMA_HOST}/v1 if the env var is set).

    All tiered structured-output logic (Tier-1 tool-call → Tier-2 JSON mode →
    Tier-3 prompt-parse) is inherited from OpenAICompatibleProvider.

    Constructor args (all optional — sensible defaults provided):
        base_url:      Override the Ollama endpoint (default: resolved from
                       OLLAMA_HOST env or "http://localhost:11434/v1").
        default_model: Model tag to use (default: "llama3").
        _client:       Injectable transport for unit testing (no network).
    """

    # The parent disables reasoning by default (deepseek-v4-flash spends its
    # entire output budget on a reasoning trace). Local models reached through
    # Ollama's OpenAI-compatible shim reject unknown parameters with a 400
    # instead of ignoring them, so the knob is omitted here entirely.
    reasoning_effort: str | None = None

    def __init__(
        self,
        base_url: str | None = None,
        default_model: str = "llama3",
        _client: Any | None = None,
    ) -> None:
        resolved_base_url = base_url or _resolve_base_url()
        super().__init__(
            base_url=resolved_base_url,
            api_key="ollama",  # Ollama ignores this, but the SDK requires a non-empty value
            default_model=default_model,
            price_table=_OLLAMA_PRICE_TABLE,
            _client=_client,
        )

    # ------------------------------------------------------------------
    # Override price() to return zero-cost regardless of price_table lookup
    # ------------------------------------------------------------------

    def price(self, model: str) -> tuple[float, float]:  # noqa: ARG002
        """Return (0.0, 0.0) for all models — local inference has no per-token API cost."""
        return _OLLAMA_FALLBACK_PRICE

    def cache_price(self, model: str) -> float:  # noqa: ARG002
        """Return 0.0 — local inference bills nothing, cached or not."""
        return 0.0


# ---------------------------------------------------------------------------
# Internal helpers
# ---------------------------------------------------------------------------


def _resolve_base_url() -> str:
    """Resolve the Ollama base URL from OLLAMA_HOST env or the default localhost."""
    host = os.environ.get("OLLAMA_HOST")
    if host:
        # Strip any leading http:// the user may have included for clarity
        host = host.removeprefix("http://").removeprefix("https://")
        return f"http://{host}/v1"
    return "http://localhost:11434/v1"
