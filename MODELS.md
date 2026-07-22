# Supported Models

Borgésica does not restrict or validate model strings — any identifier supported
by your configured provider can be passed via `--model` or `JobConfig.model`.
The engine passes the string through unchanged to the adapter.

## Tiers

### Max Quality

Best for literary work, complex prose, or when the quality of the neutral-Spanish
output is the primary concern. Higher cost and latency.

| Model | Provider | Notes |
|-------|----------|-------|
| `claude-opus-4-8` | Anthropic | Top-quality reasoning; best for reflective mode |
| `claude-3-opus-20240229` | Anthropic | Previous-generation Opus, still available |

### Best Value

Strong quality at significantly lower cost. Recommended default for most jobs.

| Model | Provider | Notes |
|-------|----------|-------|
| `claude-sonnet-5` | Anthropic | Current Sonnet generation; default for the desktop app's Anthropic provider |
| `claude-sonnet-4-6` | Anthropic | Previous Sonnet generation, still solid |
| `claude-haiku-4-5-20251001` | Anthropic | Cheaper than Sonnet; noticeably lower translation quality in practice |
| `claude-3-7-sonnet-20250219` | Anthropic | Previous Sonnet generation, still solid |
| `claude-3-5-sonnet-20241022` / `claude-3-5-sonnet-20240620` | Anthropic | Older Sonnet snapshots, still solid |
| `deepseek-v4-flash` | DeepSeek (`https://api.deepseek.com`) | ~$0.14/Mtok input (cache miss), ~$0.0028/Mtok cache hit, ~$0.28/Mtok output — roughly 10× cheaper than Sonnet, with very good translation quality. The recommended default for most jobs. |
| `deepseek-v4-pro` | DeepSeek (`https://api.deepseek.com`) | ~$0.435/Mtok input, ~$0.87/Mtok output. Higher quality than Flash; still significantly cheaper than Anthropic frontier models. |

> **NOTE — DeepSeek model-id deprecation (2026-07-24)**: the legacy identifiers `deepseek-chat` and `deepseek-reasoner` continue to work as aliases after that date but are deprecated. Use `deepseek-v4-flash` and `deepseek-v4-pro` for new jobs. Borgésica passes model strings unchanged so no library update is needed — update your `--model` argument only.

### OpenAI (ChatGPT)

Reuses the same `OpenAICompatibleProvider` adapter as DeepSeek via a dedicated `.openai()`
preset (`--provider openai`, `OPENAI_API_KEY`). Retry-waste ceiling factor is tuned to `1.5`
(vs `3.0` for DeepSeek) — GPT models are Tier-1-reliable, so tool-calling rarely falls
through to the JSON-mode/prompt-parse fallback tiers.

| Model | Provider | Notes |
|-------|----------|-------|
| `gpt-5.6-luna` | OpenAI (`https://api.openai.com`) | $1.00/Mtok input, $6.00/Mtok output. Default model for `--provider openai`. |
| `gpt-5.6-terra` | OpenAI (`https://api.openai.com`) | $2.50/Mtok input, $15.00/Mtok output. Mid-tier quality/cost. |
| `gpt-5.6-sol` | OpenAI (`https://api.openai.com`) | $5.00/Mtok input, $30.00/Mtok output. Highest-quality GPT tier. |

> **NOTE — o-series (reasoning) models not supported**: `o1`, `o3`, `o4-mini`, and other
> o-series models require the `max_completion_tokens` parameter instead of `max_tokens`,
> which this adapter does not send. Selecting an o-series `--model` is not special-cased or
> validated — the raw OpenAI API error is surfaced unmodified. This is an explicit,
> documented scope boundary, not a bug.

### Private / Free / Offline

Runs locally through Ollama, with no API key and no connection required — supported today,
including from the desktop app's provider selector. Quality varies a lot by model size: in
practice, small (~9-14B) local models produced very low translation quality compared to any
hosted option above.

| Model | Provider | Notes |
|-------|----------|-------|
| `llama3.2:latest` | Ollama | Strong general-purpose open-weights model |
| `mistral:latest` | Ollama | Fast and capable; good multilingual baseline |
| `gemma2:9b` | Ollama | Google Gemma; competitive with larger models |
| `Tower-Plus-9B-GGUF:Q4_K_M` | Ollama | Tested in practice; translation quality was very low at this size |
| `qwen3:14b` | Ollama | Tested in practice; translation quality was very low at this size |

## Note on model strings

Borgésica intentionally accepts any model string — this makes it forward-compatible
with new models without requiring a library update. If a model string is unrecognized,
the `price()` method returns a conservative default rate of $3.00/$15.00 per Mtok,
and cost estimates will be approximate. Token counting falls back to a word-count
heuristic (`words × 1.3`) for unknown models.
