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

**Exception — nav-label chunks always bypass reflective mode.** A chunk identifiable as a nav-doc label (isolated `chapter_index` bucket / nav-label meta marker, per book-translation's nav-doc emission requirement) SHALL always perform exactly ONE `TranslationProvider.translate` pass, regardless of `JobConfig.quality_mode`. This applies even when `quality_mode="reflective"` for the rest of the job. Nav labels are short (1-3 word) factual strings; the critique/revise cycle (3x cost) yields no quality gain for them.

(Previously: `quality_mode` applied uniformly to every chunk in the job with no per-chunk-kind exception; nav-label chunks did not exist as a distinct chunk category.)

#### Scenario: fast mode performs one pass

Given a job with `quality_mode="fast"` and a `FakeTranslationProvider` counting calls,

When a single (body) chunk is translated,

Then `provider.translate` SHALL have been called exactly once for that chunk.

#### Scenario: reflective mode performs translate, critique, and revise (body chunks)

Given a job with `quality_mode="reflective"` and a `FakeTranslationProvider` counting calls,

When a single body-prose chunk is translated,

Then `provider.translate` SHALL have been called at least 3 times for that chunk (draft, critique, revise), and the persisted `Chunk.translated_text` SHALL be the output of the revise step, not the draft.

#### Scenario: reflective revision is what gets persisted (body chunks)

Given a reflective job where the draft for a body chunk is `"viga de madera con cerradura"` and the revise step returns `"tranca de madera que cierra la puerta"`,

When the chunk completes,

Then `Chunk.translated_text` SHALL equal the revised text, and the draft text SHALL NOT appear in the final output.

#### Scenario: nav-label chunk bypasses reflective mode even when the job is reflective

Given a job with `quality_mode="reflective"` and a nav-label chunk (isolated nav `chapter_index` bucket) alongside ordinary body chunks, using a `FakeTranslationProvider` counting calls per chunk,

When the orchestrator processes the nav-label chunk,

Then `provider.translate` SHALL have been called exactly ONCE for that chunk (no critique/revise calls), while body chunks in the SAME job SHALL still receive the full 3-call reflective sequence.

---

### Requirement: reflection is orchestrator-level and provider-agnostic

The critique and revise steps SHALL be performed through the same `TranslationProvider` interface as the draft, with no additional port methods. Any provider (Anthropic, Ollama, Fake) SHALL support reflective mode without changes to the provider adapter. The orchestrator SHALL determine whether to apply reflective mode per chunk (see "quality_mode controls how many model passes run per chunk" for the nav-label exception) using information already present on the chunk (its isolated `chapter_index` bucket or an equivalent nav-label marker) — no new provider-facing signal is required.

(Previously: reflection mode was a job-level, not chunk-level, switch; there was no per-chunk bypass concept.)

#### Scenario: reflective mode works with any provider via the same port

Given a `FakeTranslationProvider` that implements only the `TranslationProvider` Protocol,

When a reflective-mode job runs (mixing body chunks and a nav-label chunk),

Then no method outside the `TranslationProvider` Protocol SHALL be required, and the job SHALL complete successfully, with body chunks reflective and the nav-label chunk single-pass.

---

### Requirement: the calque failure case is a golden evaluation sample

The quality-evaluation harness SHALL include a golden sample covering the known calque failure: a source containing "a slot for a locking wooden beam across the door" SHALL be judged on whether the output conveys a barred/locked door (e.g. "tranca") rather than a literal "viga ... con cerradura". This is advisory (LLM-as-judge), not an exact-match assertion.

#### Scenario: calque golden sample is scored, not exact-matched

Given the calque golden sample and an engine output,

When the LLM-as-judge rubric runs,

Then it SHALL produce a `meaning-fidelity` / `naturalness` score in [1, 5], and a literal "viga de madera con cerradura"-style rendering SHALL score below the pass threshold (< 4).
