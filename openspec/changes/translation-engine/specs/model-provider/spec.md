# Spec: model-provider

Change: `translation-engine` · Capability: `model-provider`
Phase: spec · Status: draft · Artifact store: openspec

---

## ADDED Requirements

### Requirement: TranslationProvider is a Protocol; domain never imports a provider SDK

The domain module `ports.py` SHALL define `TranslationProvider` as a `typing.Protocol` with the three methods `translate`, `count_tokens`, and `price`. No adapter SDK (`anthropic`, `ollama`, `litellm`) SHALL be imported anywhere inside the `borgesica/domain/` package. This is verified structurally.

#### Scenario: domain directory contains no provider SDK imports

Given the full contents of `borgesica/domain/`,

When a static analysis check (or a dedicated unit test using `ast.parse`) inspects every `.py` file for imports of `anthropic`, `openai`, `ollama`, `litellm`, or `instructor`,

Then 0 such import statements SHALL be found in any domain file.

#### Scenario: FakeTranslationProvider satisfies the Protocol

Given a hand-written `FakeTranslationProvider` class in the test suite that implements `translate`, `count_tokens`, and `price`,

When Python's `isinstance(fake, TranslationProvider)` is evaluated using `runtime_checkable` or equivalent Protocol check,

Then it SHALL return `True`.

---

### Requirement: Anthropic adapter fulfills the TranslationUnit contract via tool-calling / structured output

The `AnthropicProvider` adapter SHALL call the Anthropic API and return a valid `TranslationUnit` (Pydantic v2 model). The adapter SHALL use tool-calling or native structured output as the primary mechanism. `instructor` MAY be used internally within `anthropic_provider.py` only.

#### Scenario: adapter returns a valid TranslationUnit

Given a real Anthropic API key in the environment (integration test, CI-gated),

When `AnthropicProvider.translate(system, user, model="claude-haiku-4-5")` is called with a short English text,

Then the return value SHALL be a `TranslationUnit` instance where `translation` is non-empty, `summary_update` is 3–5 sentences, and `pydantic.ValidationError` is NOT raised during construction.

---

### Requirement: adapter degrades gracefully on imperfect structured output

Provider adapters SHALL implement a three-tier fallback for structured output:
1. **Tier 1** (preferred): native tool-calling / structured output.
2. **Tier 2**: constrained decoding / JSON mode (where the provider supports it).
3. **Tier 3** (floor): prompt-and-parse with retry — extract JSON from the response text, parse into `TranslationUnit`, retry up to 2 additional times on `ValidationError` or `JSONDecodeError`.

If all 3 tiers fail for a single chunk, the adapter SHALL raise `MalformedOutput`. The orchestrator handles that exception by marking the chunk `FAILED` and the job `PAUSED`.

#### Scenario: Tier 3 parse succeeds on the second attempt

Given a `FakeProvider` in Tier-3 mode that returns invalid JSON on the first call and valid JSON on the second call,

When the adapter processes a single chunk,

Then the adapter SHALL NOT raise and SHALL return a valid `TranslationUnit` with the second attempt's data.

#### Scenario: all 3 tiers fail — MalformedOutput is raised

Given a `FakeProvider` in Tier-3 mode that returns invalid JSON on all 3 attempts (initial + 2 retries),

When the adapter processes a single chunk,

Then `MalformedOutput` SHALL be raised and the adapter SHALL NOT make a 4th call.

---

### Requirement: adapter retries on transient errors with exponential backoff

For HTTP 5xx errors and network timeouts, the adapter SHALL retry up to 3 times (initial attempt + 2 retries) with exponential backoff and jitter. For HTTP 429 (rate limit), the adapter SHALL honor the `Retry-After` header (or use a 60-second default if absent) before retrying. After the maximum retries are exhausted, the adapter SHALL raise a `ProviderError` (or re-raise the underlying exception), which the orchestrator surfaces as a `PAUSED` job.

#### Scenario: 429 rate-limit triggers Retry-After wait, not immediate retry

Given a `FakeProvider` that returns a 429 with `Retry-After: 2` on the first call and a valid response on the second,

When the adapter processes a chunk,

Then the adapter SHALL wait ≥ 2 seconds before the second call and SHALL return a valid `TranslationUnit`.

#### Scenario: 3 consecutive 5xx errors — ProviderError raised

Given a `FakeProvider` that returns 5xx on all 3 attempts,

When the adapter processes a chunk,

Then `ProviderError` SHALL be raised after exactly 3 attempts (no 4th call).

---

### Requirement: TranslationProvider.translate returns TranslationResult with real Usage (M4-6)

`TranslationProvider.translate(system, user, model)` SHALL return a `TranslationResult` instance
containing two fields:
- `unit: TranslationUnit` — the validated structured translation output.
- `usage: Usage` — the REAL token counts for this call, populated from the provider response
  (`input_tokens` from prompt tokens, `output_tokens` from completion tokens).

Adapters MUST populate `usage` from the actual API response, not from an estimate. If a provider
response does not include token usage (e.g. streaming, or a fake client in tests), the adapter
SHALL fall back to `Usage()` (zeros) rather than raising.

Callers (orchestrator, quality harness, glossary extractor) access the translation via
`result.unit`; they access cost accounting data via `result.usage`.

#### Scenario: adapter returns TranslationResult with populated usage on a successful call

Given a real (or faithfully-faked) provider response that includes token counts,

When `provider.translate(system, user, model)` succeeds,

Then the return value SHALL be an instance of `TranslationResult` where `result.unit` is a valid
`TranslationUnit` and `result.usage.input_tokens >= 0` and `result.usage.output_tokens >= 0`.

#### Scenario: fake client with no usage falls back to zero Usage

Given a test double that returns a response with no `.usage` attribute,

When the adapter's `_extract_usage(response)` is called,

Then `Usage(input_tokens=0, output_tokens=0)` SHALL be returned and no exception SHALL be raised.

---

### Requirement: model-agnostic selection — user provides model string, engine does not hardcode a default

`JobConfig.model` is a required string with no engine-level default. The engine SHALL use whatever model string the caller provides and pass it unchanged to `TranslationProvider.translate` and `count_tokens`. The engine SHALL NOT validate that the string names a known model — that is the adapter's concern.

#### Scenario: engine passes model string to provider unchanged

Given `JobConfig(model="claude-opus-4-5-20251101")`,

When the orchestrator calls `provider.translate(system, user, model)`,

Then `model` SHALL equal `"claude-opus-4-5-20251101"` exactly (no rewriting, prefixing, or defaulting).

---

### Requirement: provider tiers are documented, not enforced

The engine SHALL ship a `MODELS.md` (or equivalent documentation file) listing three provider tiers with example model IDs:
- **Max quality**: frontier models (e.g. `claude-sonnet-4-6`, or equivalent frontier equivalent at time of writing).
- **Best value**: mid-tier models (e.g. `claude-haiku-4-5`, `gemini-flash`).
- **Private/free/offline**: local models via Ollama (e.g. capable 30B+ parameter model).

The engine itself SHALL NOT enforce or restrict model choice. Tiered recommendations are guidance, not policy.

#### Scenario: documentation file exists with the three tiers

Given the project root,

When a file named `MODELS.md` (or `docs/models.md`) is checked,

Then it SHALL exist and SHALL contain references to all three tiers.
