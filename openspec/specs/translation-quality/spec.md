# Spec: translation-quality

Capability: `translation-quality` · Status: canonical

---

### Requirement: the system prompt always carries an explicit translation philosophy

The static (cacheable) instruction block built by `ContextManager` SHALL always include a translation-philosophy section instructing the model to: translate the MEANING and the physical IMAGE of the source (not a word-for-word mapping); avoid literal calques that would confuse a Spanish reader; prioritize naturalness while remaining faithful to the source. This applies regardless of `quality_mode`.

#### Scenario: philosophy is present in every system prompt

Given any `JobConfig`,

When `ContextManager` assembles the system prompt for a chunk,

Then the system prompt string SHALL contain the translation-philosophy instructions (detectable by substring search in a unit test against the `FakeTranslationProvider`'s captured system prompt), including an explicit instruction to avoid literal calques.

---

### Requirement: quality_mode controls how many model passes run per chunk

`JobConfig.quality_mode` SHALL be `"fast"` (default) or `"reflective"`.
- `"fast"` SHALL perform exactly ONE `TranslationProvider.translate` pass per chunk.
- `"reflective"` SHALL perform a three-step orchestration per chunk: (1) translate (draft), (2) critique the draft for calques / unnatural phrasing / confusing imagery, (3) revise into a final translation that is faithful AND natural. The reflection loop lives in the orchestrator; the `TranslationProvider` port is unchanged.

#### Scenario: fast mode performs one pass

Given a job with `quality_mode="fast"` and a `FakeTranslationProvider` counting calls,

When a single chunk is translated,

Then `provider.translate` SHALL have been called exactly once for that chunk.

#### Scenario: reflective mode performs translate, critique, and revise

Given a job with `quality_mode="reflective"` and a `FakeTranslationProvider` counting calls,

When a single chunk is translated,

Then `provider.translate` SHALL have been called at least 3 times for that chunk (draft, critique, revise), and the persisted `Chunk.translated_text` SHALL be the output of the revise step, not the draft.

#### Scenario: reflective revision is what gets persisted

Given a reflective job where the draft for a chunk is `"viga de madera con cerradura"` and the revise step returns `"tranca de madera que cierra la puerta"`,

When the chunk completes,

Then `Chunk.translated_text` SHALL equal the revised text, and the draft text SHALL NOT appear in the final output.

---

### Requirement: reflection is orchestrator-level and provider-agnostic

The critique and revise steps SHALL be performed through the same `TranslationProvider` interface as the draft, with no additional port methods. Any provider (Anthropic, Ollama, Fake) SHALL support reflective mode without changes to the provider adapter.

#### Scenario: reflective mode works with any provider via the same port

Given a `FakeTranslationProvider` that implements only the `TranslationProvider` Protocol,

When a reflective-mode job runs,

Then no method outside the `TranslationProvider` Protocol SHALL be required, and the job SHALL complete successfully.

---

### Requirement: the calque failure case is a golden evaluation sample

The quality-evaluation harness SHALL include a golden sample covering the known calque failure: a source containing "a slot for a locking wooden beam across the door" SHALL be judged on whether the output conveys a barred/locked door (e.g. "tranca") rather than a literal "viga ... con cerradura". This is advisory (LLM-as-judge), not an exact-match assertion.

#### Scenario: calque golden sample is scored, not exact-matched

Given the calque golden sample and an engine output,

When the LLM-as-judge rubric runs,

Then it SHALL produce a `meaning-fidelity` / `naturalness` score in [1, 5], and a literal "viga de madera con cerradura"-style rendering SHALL score below the pass threshold (< 4).
