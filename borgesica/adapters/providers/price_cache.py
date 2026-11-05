"""Refreshable model-price cache.

The built-in price tables in the provider modules go stale: models appear and
prices change faster than anyone edits a dict. This module downloads current
listings into a small local JSON file that the providers consult.

Sources (each refreshed independently — one failing keeps the other's data):

* OpenRouter ``/api/v1/models`` — OpenRouter is its own billing source, so its
  listing is AUTHORITATIVE for the ``openrouter`` provider.
* LiteLLM's community ``model_prices_and_context_window.json`` — covers OpenAI,
  Anthropic, DeepSeek and Gemini. Checked on 2026-11-04 against the providers'
  own pricing pages: 20 of 20 models (input, output and cache-read) matched, so
  it is treated as authoritative too. DeepSeek's two-rate (peak/off-peak)
  pricing is published as the PEAK rate, the safe side for a budget guard.

Prices only feed pre-flight estimates and the budget guard, never billing, so
every failure mode degrades to "use the built-in table". Values that are not
finite and strictly positive are discarded: a zero would make the budget guard
believe a job is free.
"""

from __future__ import annotations

import json
import math
import os
import tempfile
import threading
import time
import urllib.request
from collections.abc import Callable
from pathlib import Path
from typing import Any

OPENROUTER_URL = "https://openrouter.ai/api/v1/models"
LITELLM_URL = (
    "https://raw.githubusercontent.com/BerriAI/litellm/main/"
    "model_prices_and_context_window.json"
)

ENV_PATH = "BORGESICA_PRICES_FILE"

# Providers whose prices come from the LiteLLM listing.
LITELLM_PROVIDERS = ("openai", "anthropic", "deepseek", "gemini")

# LiteLLM namespaces DeepSeek/Gemini keys ("deepseek/x", "gemini/x"); OpenAI and
# Anthropic use bare ids and are told apart by the ``litellm_provider`` field.
_LITELLM_PREFIX = {"deepseek": "deepseek/", "gemini": "gemini/"}

# A refreshed price this far from the built-in one is reported as drift.
DRIFT_THRESHOLD = 0.25

_TIMEOUT_SECONDS = 30

Prices = dict[str, tuple[float, float]]
CachePrices = dict[str, float]


def default_cache_path() -> Path:
    """``$BORGESICA_PRICES_FILE`` or ``~/.borgesica/prices.json``."""
    configured = os.environ.get(ENV_PATH)
    if configured:
        return Path(configured)
    return Path.home() / ".borgesica" / "prices.json"


# ---------------------------------------------------------------------------
# Parsing
# ---------------------------------------------------------------------------


def _per_mtok(value: Any) -> float | None:
    """USD-per-token (str or number) -> USD per Mtok, or None if unusable."""
    try:
        number = float(value)
    except (TypeError, ValueError):
        return None
    if not math.isfinite(number) or number <= 0:
        return None
    return round(number * 1_000_000, 6)


def _entry(payload: dict[str, Any], in_key: str, out_key: str, cache_key: str):
    input_price = _per_mtok(payload.get(in_key))
    output_price = _per_mtok(payload.get(out_key))
    if input_price is None or output_price is None:
        return None
    return input_price, output_price, _per_mtok(payload.get(cache_key))


def _section(rows: list[tuple[str, float, float, float | None]]) -> dict[str, Any]:
    return {
        "prices": {model: [i, o] for model, i, o, _ in rows},
        "cache": {model: c for model, _, _, c in rows if c is not None},
    }


def parse_openrouter(payload: Any) -> dict[str, Any]:
    """Normalise an OpenRouter ``/models`` response."""
    rows = []
    for model in (payload or {}).get("data", []):
        entry = _entry(
            model.get("pricing") or {}, "prompt", "completion", "input_cache_read"
        )
        if entry is not None and model.get("id"):
            rows.append((model["id"], *entry))
    return _section(rows)


def parse_litellm(payload: Any, provider: str) -> dict[str, Any]:
    """Normalise one provider's slice of the LiteLLM price listing."""
    prefix = _LITELLM_PREFIX.get(provider)
    rows = []
    for key, value in (payload or {}).items():
        if not isinstance(value, dict) or value.get("mode") != "chat":
            continue
        if prefix is not None:
            if not key.startswith(prefix):
                continue
            model = key[len(prefix):]
        else:
            if "/" in key or value.get("litellm_provider") != provider:
                continue
            model = key
        entry = _entry(
            value, "input_cost_per_token", "output_cost_per_token",
            "cache_read_input_token_cost",
        )
        if entry is not None:
            rows.append((model, *entry))
    return _section(rows)


# ---------------------------------------------------------------------------
# Refresh (network + disk)
# ---------------------------------------------------------------------------


def _http_json(url: str) -> Any:
    request = urllib.request.Request(url, headers={"User-Agent": "borgesica"})
    with urllib.request.urlopen(request, timeout=_TIMEOUT_SECONDS) as response:  # noqa: S310
        return json.load(response)


def _read(path: Path) -> dict[str, Any]:
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}
    return data if isinstance(data, dict) else {}


def refresh(
    path: Path | None = None,
    fetch: Callable[[str], Any] = _http_json,
) -> dict[str, str]:
    """Download both listings and rewrite the cache file.

    Returns ``{source: "ok (N models)" | "failed: <reason>"}``. A source that
    fails (or yields nothing) keeps the entries from the previous refresh.
    """
    path = path or default_cache_path()
    cache = _read(path)
    previous = cache.get("providers")
    providers: dict[str, Any] = dict(previous) if isinstance(previous, dict) else {}
    outcome: dict[str, str] = {}

    def update(names: tuple[str, ...], url: str, parse: Callable[[Any, str], dict]):
        try:
            payload = fetch(url)
        except Exception as exc:  # noqa: BLE001 - any transport/parse failure
            for name in names:
                outcome[name] = f"failed: {type(exc).__name__}: {exc}"
            return
        for name in names:
            section = parse(payload, name)
            if section["prices"]:
                providers[name] = section
                outcome[name] = f"ok ({len(section['prices'])} models)"
            else:
                outcome[name] = "failed: listing contained no usable prices"

    update(("openrouter",), OPENROUTER_URL, lambda p, _n: parse_openrouter(p))
    update(LITELLM_PROVIDERS, LITELLM_URL, parse_litellm)

    if any(v.startswith("ok") for v in outcome.values()):
        path.parent.mkdir(parents=True, exist_ok=True)
        document = {"fetched_at": int(time.time()), "providers": providers}
        fd, tmp = tempfile.mkstemp(dir=path.parent, suffix=".tmp")
        try:
            with os.fdopen(fd, "w", encoding="utf-8") as handle:
                json.dump(document, handle)
            os.replace(tmp, path)
        except BaseException:
            Path(tmp).unlink(missing_ok=True)
            raise
    return outcome


# ---------------------------------------------------------------------------
# Startup refresh
# ---------------------------------------------------------------------------

ENV_AUTO_REFRESH = "BORGESICA_PRICES_AUTO_REFRESH"
STALE_AFTER_SECONDS = 24 * 60 * 60


def is_stale(path: Path | None = None, max_age: float = STALE_AFTER_SECONDS) -> bool:
    """True when the cache is missing, unreadable or older than ``max_age``."""
    try:
        path = path or default_cache_path()
    except (RuntimeError, OSError):
        return False  # nowhere to write: do not try
    fetched_at = _read(path).get("fetched_at")
    if not isinstance(fetched_at, (int, float)):
        return True
    return time.time() - fetched_at > max_age


def start_background_refresh(
    path: Path | None = None,
    fetch: Callable[[str], Any] = _http_json,
) -> threading.Thread | None:
    """Refresh in a daemon thread when the cache is stale; never blocks or raises.

    Providers read the cache when they are built, so a refresh started at boot
    benefits the NEXT launch. Disabled with ``BORGESICA_PRICES_AUTO_REFRESH=0``.
    Returns the thread (for tests) or None when nothing was started.
    """
    if os.environ.get(ENV_AUTO_REFRESH, "1") == "0" or not is_stale(path):
        return None

    def run() -> None:
        try:
            refresh(path, fetch=fetch)
        except Exception:  # noqa: BLE001 - best effort, estimates keep working
            pass

    thread = threading.Thread(target=run, name="borgesica-price-refresh", daemon=True)
    thread.start()
    return thread


# ---------------------------------------------------------------------------
# Lookup
# ---------------------------------------------------------------------------


def load_refreshed(
    provider: str, path: Path | None = None
) -> tuple[Prices, CachePrices]:
    """Refreshed (prices, cache prices) for ``provider``; empty when unavailable.

    Never raises: a missing, unreadable or malformed file just means "no
    refreshed data". Entries are re-validated so a hand-edited or corrupted file
    cannot smuggle in a zero or negative price.
    """
    try:
        path = path or default_cache_path()
    except (RuntimeError, OSError):  # no home directory resolvable
        return {}, {}
    providers = _read(path).get("providers")
    section = providers.get(provider) if isinstance(providers, dict) else None
    if not isinstance(section, dict):
        return {}, {}
    prices: Prices = {}
    for model, pair in (section.get("prices") or {}).items():
        try:
            i, o = float(pair[0]), float(pair[1])
        except (TypeError, ValueError, IndexError, KeyError):
            continue
        if all(math.isfinite(x) and x > 0 for x in (i, o)):
            prices[model] = (i, o)
    cache: CachePrices = {}
    for model, value in (section.get("cache") or {}).items():
        try:
            c = float(value)
        except (TypeError, ValueError):
            continue
        if math.isfinite(c) and c > 0:
            cache[model] = c
    return prices, cache


def with_refreshed(
    provider: str,
    prices: Prices,
    cache_prices: CachePrices | None = None,
    *,
    authoritative: bool = True,
    path: Path | None = None,
) -> tuple[Prices, CachePrices]:
    """Combine the built-in tables with refreshed data (inputs untouched).

    By default refreshed data overrides built-in entries (the built-ins are the
    offline fallback). With ``authoritative=False`` it only adds models the
    built-in table does not know.
    """
    fresh_prices, fresh_cache = load_refreshed(provider, path)
    merged_prices = dict(prices)
    merged_cache = dict(cache_prices or {})
    for model, price in fresh_prices.items():
        if authoritative or model not in merged_prices:
            merged_prices[model] = price
    for model, value in fresh_cache.items():
        if authoritative or model not in merged_cache:
            merged_cache[model] = value
    return merged_prices, merged_cache


def drift(
    provider: str,
    builtin: Prices,
    path: Path | None = None,
) -> list[tuple[str, tuple[float, float], tuple[float, float]]]:
    """Models whose refreshed price differs from ``builtin`` by > DRIFT_THRESHOLD.

    Returns ``(model, builtin_price, refreshed_price)`` tuples.
    """
    fresh, _ = load_refreshed(provider, path)
    out = []
    for model, old in builtin.items():
        new = fresh.get(model)
        if new is None:
            continue
        if any(abs(n - o) / o > DRIFT_THRESHOLD for o, n in zip(old, new)):
            out.append((model, old, new))
    return out
