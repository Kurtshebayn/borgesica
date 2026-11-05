"""Refreshable price cache: parsing, safety filters, merge precedence, CLI."""

from __future__ import annotations

import json
import time

import pytest

from borgesica.__main__ import main
from borgesica.adapters.providers import price_cache as pc
from borgesica.adapters.providers.anthropic_provider import AnthropicProvider
from borgesica.adapters.providers.openai_compatible_provider import (
    OpenAICompatibleProvider,
)

OPENROUTER = {
    "data": [
        {"id": "x/new", "pricing": {"prompt": "0.0000002", "completion": "0.0000006",
                                    "input_cache_read": "0.00000005"}},
        {"id": "xiaomi/mimo-v2.6-pro", "pricing": {"prompt": "0.000001", "completion": "0.000002"}},
        {"id": "x/free", "pricing": {"prompt": "0", "completion": "0"}},
        {"id": "x/bad", "pricing": {"prompt": "abc", "completion": "1"}},
        {"id": "x/neg", "pricing": {"prompt": "-1", "completion": "1"}},
    ]
}

LITELLM = {
    "gpt-9": {"mode": "chat", "litellm_provider": "openai",
              "input_cost_per_token": 1e-6, "output_cost_per_token": 2e-6},
    "gpt-5.6-luna": {"mode": "chat", "litellm_provider": "openai",
                     "input_cost_per_token": 5e-7, "output_cost_per_token": 3e-6},
    "azure/gpt-9": {"mode": "chat", "litellm_provider": "azure",
                    "input_cost_per_token": 9e-6, "output_cost_per_token": 9e-6},
    "deepseek/deepseek-v9": {"mode": "chat", "litellm_provider": "deepseek",
                             "input_cost_per_token": 3e-7, "output_cost_per_token": 1.2e-6,
                             "cache_read_input_token_cost": 6e-9},
    "gemini/gemini-9": {"mode": "chat", "litellm_provider": "gemini",
                        "input_cost_per_token": 4e-7, "output_cost_per_token": 3e-6},
    "claude-future": {"mode": "chat", "litellm_provider": "anthropic",
                      "input_cost_per_token": 2e-6, "output_cost_per_token": 1e-5},
    "text-embed": {"mode": "embedding", "litellm_provider": "openai",
                   "input_cost_per_token": 1e-7, "output_cost_per_token": 0},
}


def _fetch(url):
    return OPENROUTER if url == pc.OPENROUTER_URL else LITELLM


def test_parse_openrouter_converts_to_per_mtok_and_drops_unusable():
    out = pc.parse_openrouter(OPENROUTER)
    assert out["prices"]["x/new"] == [0.2, 0.6]  # exact: no float noise
    assert out["cache"]["x/new"] == 0.05
    assert not {"x/free", "x/bad", "x/neg"} & set(out["prices"])


def test_parse_litellm_slices_per_provider():
    assert set(pc.parse_litellm(LITELLM, "openai")["prices"]) == {"gpt-9", "gpt-5.6-luna"}
    assert set(pc.parse_litellm(LITELLM, "deepseek")["prices"]) == {"deepseek-v9"}
    assert set(pc.parse_litellm(LITELLM, "gemini")["prices"]) == {"gemini-9"}
    assert set(pc.parse_litellm(LITELLM, "anthropic")["prices"]) == {"claude-future"}


def test_refresh_writes_file_and_load_reads_it(tmp_path):
    path = tmp_path / "p.json"
    outcome = pc.refresh(path, fetch=_fetch)
    assert all(v.startswith("ok") for v in outcome.values())
    prices, cache = pc.load_refreshed("deepseek", path)
    assert prices == {"deepseek-v9": (0.3, 1.2)} and cache == {"deepseek-v9": 0.006}


def test_failed_source_keeps_previous_data(tmp_path):
    path = tmp_path / "p.json"
    pc.refresh(path, fetch=_fetch)

    def flaky(url):
        if url == pc.LITELLM_URL:
            raise OSError("offline")
        return OPENROUTER

    outcome = pc.refresh(path, fetch=flaky)
    assert outcome["openai"].startswith("failed")
    assert pc.load_refreshed("openai", path)[0]["gpt-9"] == (1.0, 2.0)


def test_total_failure_writes_nothing(tmp_path):
    path = tmp_path / "p.json"

    def down(url):
        raise OSError("offline")

    assert not any(v.startswith("ok") for v in pc.refresh(path, fetch=down).values())
    assert not path.exists()


@pytest.mark.parametrize("content", ["", "not json", "[]", '{"providers": 3}'])
def test_load_tolerates_missing_or_corrupt_file(tmp_path, content):
    path = tmp_path / "p.json"
    if content:
        path.write_text(content)
    assert pc.load_refreshed("openai", path) == ({}, {})


def test_load_survives_unresolvable_home(monkeypatch):
    monkeypatch.delenv(pc.ENV_PATH)
    monkeypatch.delenv("USERPROFILE", raising=False)
    monkeypatch.delenv("HOME", raising=False)
    monkeypatch.delenv("HOMEDRIVE", raising=False)
    monkeypatch.delenv("HOMEPATH", raising=False)
    assert pc.load_refreshed("openai") == ({}, {})


def test_load_revalidates_entries(tmp_path):
    path = tmp_path / "p.json"
    path.write_text(json.dumps({"providers": {"openai": {"prices": {
        "zero": [0, 1], "ok": [1, 2], "nan": ["x", 1], "short": [1]}}}}))
    assert pc.load_refreshed("openai", path)[0] == {"ok": (1.0, 2.0)}


def test_refreshed_overrides_builtin_unless_gap_fill_only(tmp_path):
    path = tmp_path / "p.json"
    pc.refresh(path, fetch=_fetch)
    builtin = {"gpt-5.6-luna": (1.0, 6.0)}
    merged, _ = pc.with_refreshed("openai", builtin, path=path)
    assert merged["gpt-5.6-luna"] == (0.5, 3.0)   # refreshed wins
    assert merged["gpt-9"] == (1.0, 2.0)          # gap filled
    kept, _ = pc.with_refreshed("openai", builtin, authoritative=False, path=path)
    assert kept["gpt-5.6-luna"] == (1.0, 6.0)     # opt-out keeps built-in
    assert kept["gpt-9"] == (1.0, 2.0)


def test_drift_reports_only_large_differences(tmp_path):
    path = tmp_path / "p.json"
    pc.refresh(path, fetch=_fetch)
    found = pc.drift("openai", {"gpt-5.6-luna": (1.0, 6.0), "gpt-9": (1.0, 2.1)}, path)
    assert [m for m, _, _ in found] == ["gpt-5.6-luna"]


def test_providers_use_refreshed_prices_and_explicit_extra_wins(tmp_path, monkeypatch):
    path = tmp_path / "p.json"
    pc.refresh(path, fetch=_fetch)
    monkeypatch.setenv(pc.ENV_PATH, str(path))
    openai = OpenAICompatibleProvider.openai(api_key="k", _client=object())
    assert openai.price("gpt-9") == (1.0, 2.0)
    assert openai.price("gpt-5.6-luna") == (0.5, 3.0)
    override = OpenAICompatibleProvider.openai(
        api_key="k", extra_price_table={"gpt-9": (7.0, 8.0)}, _client=object()
    )
    assert override.price("gpt-9") == (7.0, 8.0)
    router = OpenAICompatibleProvider.openrouter(api_key="k", _client=object())
    assert router.price("x/new") == (0.2, 0.6)
    deepseek = OpenAICompatibleProvider.deepseek(api_key="k", _client=object())
    assert deepseek.cache_price("deepseek-v9") == 0.006
    assert AnthropicProvider(client=object()).price("claude-future") == (2.0, 10.0)


def test_providers_unchanged_without_cache():
    # conftest points the cache at a nonexistent file
    p = OpenAICompatibleProvider.openai(api_key="k", _client=object())
    assert p.price("gpt-5.6-luna") == (0.20, 1.20)  # built-in table
    assert p.price("gpt-9") == (3.0, 15.0)  # _FALLBACK_PRICE


def test_cli_prices_refresh_needs_no_key_and_reports(monkeypatch, capsys):
    for var in ("ANTHROPIC_API_KEY", "DEEPSEEK_API_KEY", "OPENAI_API_KEY"):
        monkeypatch.delenv(var, raising=False)
    real = pc.refresh
    monkeypatch.setattr(pc, "refresh", lambda *a, **k: real(fetch=_fetch))
    assert main(["prices", "refresh"]) == 0
    out = capsys.readouterr().out
    assert "openrouter: ok" in out
    assert "openai/gpt-5.6-luna" in out  # drifted from the built-in row
    assert "xiaomi/mimo-v2.6-pro" in out


def test_is_stale(tmp_path):
    path = tmp_path / "p.json"
    assert pc.is_stale(path)                      # missing
    path.write_text(json.dumps({"fetched_at": 0, "providers": {}}))
    assert pc.is_stale(path)                      # ancient
    path.write_text(json.dumps({"fetched_at": time.time(), "providers": {}}))
    assert not pc.is_stale(path)                  # fresh
    path.write_text("garbage")
    assert pc.is_stale(path)                      # unreadable


def test_background_refresh_runs_when_stale_and_honours_opt_out(tmp_path, monkeypatch):
    path = tmp_path / "p.json"
    thread = pc.start_background_refresh(path, fetch=_fetch)  # conftest sets opt-out
    assert thread is None and not path.exists()

    monkeypatch.setenv(pc.ENV_AUTO_REFRESH, "1")
    thread = pc.start_background_refresh(path, fetch=_fetch)
    thread.join(timeout=10)
    assert path.exists()
    assert pc.start_background_refresh(path, fetch=_fetch) is None  # now fresh


def test_background_refresh_swallows_failures(tmp_path, monkeypatch):
    monkeypatch.setenv(pc.ENV_AUTO_REFRESH, "1")

    def down(url):
        raise OSError("offline")

    thread = pc.start_background_refresh(tmp_path / "p.json", fetch=down)
    thread.join(timeout=10)
    assert not thread.is_alive()


def test_cli_prices_without_action_prints_help():
    assert main(["prices"]) == 1
