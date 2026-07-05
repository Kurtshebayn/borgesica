# Delta for translation-quality

Change: `nav-toc-translation` · Capability: `translation-quality`
Phase: spec · Status: draft · Artifact store: openspec

---

## MODIFIED Requirements

### Requirement: quality_mode controls how many model passes run per chunk

`JobConfig.quality_mode` SHALL be `"fast"` (default) or `"reflective"`.
- `"fast"` SHALL perform exactly ONE `TranslationProvider.translate` pass per chunk.
- `"reflective"` SHALL perform a three-step orchestration per chunk: (1) translate (draft), (2) critique the draft for calques / unnatural phrasing / confusing imagery, (3) revise into a final translation that is faithful AND natural. The reflection loop lives in the orchestrator; the `TranslationProvider` port is unchanged.

**Exception — nav-label chunks always bypass reflective mode.** A chunk identifiable as a nav-doc label (isolated `chapter_index` bucket / nav-label meta marker, per book-translation's nav-doc emission requirement) SHALL always perform exactly ONE `TranslationProvider.translate` pass, regardless of `JobConfig.quality_mode`. This applies even when `quality_mode="reflective"` for the rest of the job. Nav labels are short (1-3 word) factual strings; the critique/revise cycle (3x cost) yields no quality gain for them.

(Previously: `quality_mode` applied uniformly to every chunk in the job with no per-chunk-kind exception; nav-label chunks did not exist as a distinct chunk category.)

#### Scenario: fast mode performs one pass

- GIVEN a job with `quality_mode="fast"` and a `FakeTranslationProvider` counting calls
- WHEN a single (body) chunk is translated
- THEN `provider.translate` SHALL have been called exactly once for that chunk

#### Scenario: reflective mode performs translate, critique, and revise (body chunks)

- GIVEN a job with `quality_mode="reflective"` and a `FakeTranslationProvider` counting calls
- WHEN a single body-prose chunk is translated
- THEN `provider.translate` SHALL have been called at least 3 times for that chunk (draft, critique, revise), and the persisted `Chunk.translated_text` SHALL be the output of the revise step, not the draft

#### Scenario: reflective revision is what gets persisted (body chunks)

- GIVEN a reflective job where the draft for a body chunk is `"viga de madera con cerradura"` and the revise step returns `"tranca de madera que cierra la puerta"`
- WHEN the chunk completes
- THEN `Chunk.translated_text` SHALL equal the revised text, and the draft text SHALL NOT appear in the final output

#### Scenario: nav-label chunk bypasses reflective mode even when the job is reflective

- GIVEN a job with `quality_mode="reflective"` and a nav-label chunk (isolated nav `chapter_index` bucket) alongside ordinary body chunks, using a `FakeTranslationProvider` counting calls per chunk
- WHEN the orchestrator processes the nav-label chunk
- THEN `provider.translate` SHALL have been called exactly ONCE for that chunk (no critique/revise calls), while body chunks in the SAME job SHALL still receive the full 3-call reflective sequence

---

### Requirement: reflection is orchestrator-level and provider-agnostic

The critique and revise steps SHALL be performed through the same `TranslationProvider` interface as the draft, with no additional port methods. Any provider (Anthropic, Ollama, Fake) SHALL support reflective mode without changes to the provider adapter. The orchestrator SHALL determine whether to apply reflective mode per chunk (see "quality_mode controls how many model passes run per chunk" for the nav-label exception) using information already present on the chunk (its isolated `chapter_index` bucket or an equivalent nav-label marker) — no new provider-facing signal is required.

(Previously: reflection mode was a job-level, not chunk-level, switch; there was no per-chunk bypass concept.)

#### Scenario: reflective mode works with any provider via the same port

- GIVEN a `FakeTranslationProvider` that implements only the `TranslationProvider` Protocol
- WHEN a reflective-mode job runs (mixing body chunks and a nav-label chunk)
- THEN no method outside the `TranslationProvider` Protocol SHALL be required, and the job SHALL complete successfully, with body chunks reflective and the nav-label chunk single-pass
