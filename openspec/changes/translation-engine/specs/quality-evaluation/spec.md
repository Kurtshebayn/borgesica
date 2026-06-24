# Spec: quality-evaluation

Change: `translation-engine` · Capability: `quality-evaluation`
Phase: spec · Status: draft · Artifact store: openspec · Milestone: M4

---

## ADDED Requirements

### Requirement: golden-sample fixture suite covers SRT and EPUB/prose translation

The `tests/golden/` directory SHALL contain curated (source, expected-translation) fixture pairs for at minimum:
- 5 SRT cues testing: standard dialogue, text with inline tags, text that triggers 3-line reflow, a cue with a proper noun in the glossary, a cue with a region-specific expression requiring neutralization.
- 3 prose paragraphs (suitable for EPUB/PDF) testing: literary register consistency, glossary term consistency across two paragraphs, a sentence containing a named entity.

Fixtures SHALL be static files committed to version control. They SHALL NOT depend on a live LLM during unit test runs (Tier 1 evaluation is deterministic).

#### Scenario: golden fixtures directory and files exist

Given the project repository,

When `tests/golden/` is listed,

Then the directory SHALL contain at least 8 fixture files (5 SRT + 3 prose) in a documented format (e.g. YAML or JSON with `source`, `expected`, `glossary`, `notes` fields).

---

### Requirement: LLM-as-judge harness scores translation on four named dimensions

The quality evaluation harness (Tier 2) SHALL use a separate judge LLM call to score each translation on four dimensions, each rated 1–5:
1. **accuracy** — semantic fidelity to the source.
2. **fluency** — grammatical correctness and natural flow in Spanish.
3. **neutral-register** — absence of voseo, regional slang, and leísmo; register consistency.
4. **glossary-consistency** — locked glossary terms appear verbatim in the translated text.

The harness SHALL return a `QualityScore` model with `accuracy: int`, `fluency: int`, `neutral_register: int`, `glossary_consistency: int`, each in range [1, 5].

Pass thresholds (advisory, for CI gating): all four dimensions SHALL score ≥ 4/5.

#### Scenario: harness produces a QualityScore with all four dimensions

Given a source text, a translation, a glossary, and a judge LLM (integration test, CI-gated),

When the harness evaluates the translation,

Then the result SHALL be a `QualityScore` instance where all four fields are integers in [1, 5] and `pydantic.ValidationError` is NOT raised.

#### Scenario: glossary-consistency dimension detects a missing locked term

Given a source text containing the proper noun `"Thornwood"`, a glossary with a locked entry `{term: "Thornwood", translation: "Thornwood"}`, and a translation that renders it as `"Tornáverde"` instead,

When the LLM-as-judge evaluates `glossary-consistency`,

Then the `glossary_consistency` score SHALL be ≤ 2 out of 5.

#### Scenario: CI advisory gate fails when any dimension scores below 4

Given a `QualityScore` where `neutral_register == 3`,

When the CI advisory check is run,

Then the check SHALL report a FAIL advisory (not a hard build failure) for the `neutral-register` dimension.

---

### Requirement: Tier 3 back-translation regression check is optional and gated

The harness SHALL support an optional `--back-translate` flag (or equivalent config) that, when enabled:
1. Translates the Spanish output back to English using the same provider.
2. Computes a BLEU score or semantic similarity score between the original English and the back-translated English.
3. Reports the score alongside the Tier 2 judge scores.

This tier is NOT run in CI by default and SHALL NOT block any job or test suite if disabled.

#### Scenario: back-translation is skipped when flag is absent

Given a default CI run (no `--back-translate` flag),

When the quality harness runs,

Then 0 additional provider calls for back-translation SHALL be made.

---

### Requirement: Ollama local adapter satisfies the TranslationProvider Protocol (M4)

The `OllamaProvider` adapter SHALL implement the `TranslationProvider` Protocol and return valid `TranslationUnit` objects using constrained decoding / JSON mode where available, falling back to the same Tier-3 prompt-and-parse strategy as other adapters.

#### Scenario: OllamaProvider returns a valid TranslationUnit with a capable local model

Given a running Ollama instance (integration test, env-gated by `OLLAMA_HOST`),

When `OllamaProvider.translate(system, user, model="llama3:latest")` is called with a short English text,

Then the return value SHALL be a `TranslationUnit` instance where `translation` is non-empty and `pydantic.ValidationError` is NOT raised.

#### Scenario: OllamaProvider is not imported by default

Given that no user code requests the Ollama adapter explicitly,

When `api.py` wires up the default DI configuration for an Anthropic job,

Then `ollama_provider.py` SHALL NOT be present in `sys.modules`.
