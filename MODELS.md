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
| `claude-opus-4-5` | Anthropic | Top-quality reasoning; best for reflective mode |
| `claude-opus-4-0` | Anthropic | Previous generation Max model |

### Best Value

Strong quality at significantly lower cost. Recommended default for most jobs.

| Model | Provider | Notes |
|-------|----------|-------|
| `claude-sonnet-4-5` | Anthropic | Best price/quality for translation tasks |
| `claude-haiku-4-5` | Anthropic | Fastest and cheapest; good for drafts and estimation |
| `claude-3-5-sonnet-20241022` | Anthropic | Previous Sonnet generation, still solid |

### Private / Free / Offline

Run locally with no API key required. Quality varies by model size.
Requires Ollama (M4 — not yet implemented in this release).

| Model | Provider | Notes |
|-------|----------|-------|
| `llama3.2:latest` | Ollama | Strong general-purpose open-weights model |
| `mistral:latest` | Ollama | Fast and capable; good multilingual baseline |
| `gemma2:9b` | Ollama | Google Gemma; competitive with larger models |

## Note on model strings

Borgésica intentionally accepts any model string — this makes it forward-compatible
with new models without requiring a library update. If a model string is unrecognized,
the `price()` method returns a conservative default rate of $3.00/$15.00 per Mtok,
and cost estimates will be approximate. Token counting falls back to a word-count
heuristic (`words × 1.3`) for unknown models.
