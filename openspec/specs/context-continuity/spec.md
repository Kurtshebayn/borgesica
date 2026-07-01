# Spec: context-continuity

Capability: `context-continuity` · Status: canonical

---

### Requirement: glossary is seeded before any translation spend and user-editable before run

After `create_job` completes, the glossary SHALL be persisted to the checkpoint store with `job.status = JobStatus.CREATED`. No provider translation call SHALL be made until `run_job` or `resume_job` is explicitly called. The public API SHALL expose `get_glossary(job_id)` and `update_glossary(job_id, entries)` so a caller can inspect and edit entries between `create_job` and `run_job`.

#### Scenario: glossary is available immediately after create_job

Given a source `.srt` file and a valid `JobConfig`,

When `engine.create_job(path, config)` returns,

Then `engine.get_glossary(job_id)` SHALL return a `Glossary` object (possibly empty if no terms were extracted), and the checkpoint store SHALL have a persisted glossary row for `job_id`.

#### Scenario: update_glossary locks an entry before run

Given a job in `JobStatus.CREATED` state with a glossary entry `{term: "Riverdale", translation: "Riverdale", locked: False}`,

When `engine.update_glossary(job_id, [GlossaryEntry(term="Riverdale", translation="Riverdale", locked=True)])` is called,

Then `engine.get_glossary(job_id).entries` SHALL contain an entry with `term="Riverdale"` and `locked=True`.

#### Scenario: translation does not start before run_job is called

Given a job created with `create_job`,

When only `create_job` has been called (no `run_job`),

Then the provider's `translate` method SHALL have been called 0 times.

---

### Requirement: glossary is injected into every chunk prompt, locked entries are authoritative

The `ContextManager` SHALL render the glossary as a compact table (≤ 300 tokens) and include it in the system prompt for every chunk. Locked entries SHALL appear first in the rendered table with an explicit `[LOCKED]` marker. The system prompt instruction SHALL state that locked terms MUST be used verbatim and are not open to reinterpretation.

#### Scenario: glossary render fits within token budget

Given a `Glossary` with 50 entries averaging 8 tokens each (400 tokens total),

When `Glossary.render(budget_tokens=300)` is called,

Then the returned string SHALL be ≤ 300 tokens (measured by the same `count_tokens` method used in cost estimation), and locked entries SHALL appear before unlocked ones.

#### Scenario: all locked terms appear in the rendered table regardless of budget

Given a `Glossary` with 10 locked entries (80 tokens) and 40 unlocked entries (400 tokens), and `budget_tokens=300`,

When `Glossary.render(budget_tokens=300)` is called,

Then all 10 locked entries SHALL appear in the output. Unlocked entries are truncated as needed to stay within budget.

---

### Requirement: rolling summary threads across chunks sequentially

For each translated chunk, the `TranslationUnit.summary_update` SHALL replace the previous `RollingSummary.text` and be persisted to the checkpoint store with `chunk_index = N`. The summary included in chunk N's prompt SHALL be `RollingSummary.text` from chunk N-1 (the highest persisted summary index before N). The summary prompt budget is ≤ 200 tokens; if `summary_update` exceeds 200 tokens, the adapter SHALL be instructed via the system prompt to produce summaries of 3–5 sentences.

#### Scenario: summary from chunk N-1 is used in chunk N's prompt

Given a job where chunk 0 has been translated and produced `summary_update = "Context: detective story, noir tone."`,

When chunk 1 is being translated,

Then the system prompt for chunk 1 SHALL contain `"Context: detective story, noir tone."` as the rolling summary section.

#### Scenario: first chunk uses empty summary

Given a newly created job with no previously completed chunks,

When chunk 0 is translated,

Then the rolling summary section of the system prompt SHALL be empty or contain a default placeholder (e.g. `"No prior context."`), and no `KeyError` or exception SHALL be raised.

#### Scenario: summary is replaced, not appended

Given summaries persisted for chunks 0–4,

When chunk 5 produces `summary_update = "New summary text."`,

Then `RollingSummary.text` for chunk 5 SHALL equal `"New summary text."` (NOT a concatenation of previous summaries).

---

### Requirement: mid-run glossary additions are staged as UNLOCKED, applied to subsequent chunks, never block the job

When a `TranslationUnit` returned by the provider contains non-empty `glossary_additions`, each new term SHALL be:
1. Checked against existing locked entries — if a locked entry with the same `term` already exists, the addition SHALL be silently discarded.
2. If not already present (locked or unlocked), the term SHALL be added to the glossary with `locked=False` and persisted.
3. The new UNLOCKED entry SHALL be included in the rendered glossary for all subsequent chunks in the same run.
4. The job SHALL NOT pause or wait for user confirmation mid-run.
5. All UNLOCKED entries added during the run SHALL be surfaced in the final glossary available via `get_glossary` after the job completes, where a caller can review and lock them for future runs.

#### Scenario: new term is staged and applied from next chunk onwards

Given chunk 3 returns `glossary_additions = [GlossaryEntry(term="Thornwood", translation="Thornwood", locked=False)]`,

When chunks 4, 5, 6 are translated,

Then the rendered glossary in those chunks' system prompts SHALL include an entry for `"Thornwood"`.

#### Scenario: new term does not override an existing locked entry

Given a locked entry `{term: "Victor", translation: "Víctor", locked: True}`,

When chunk 7 returns `glossary_additions = [GlossaryEntry(term: "Victor", translation: "Victor", locked: False)]`,

Then the locked entry SHALL remain `{term: "Victor", translation: "Víctor", locked: True}`, and the addition SHALL be discarded without error.

#### Scenario: job does not pause when mid-run additions arrive

Given a running job that encounters mid-run additions at chunk 10 of a 100-chunk job,

When `glossary_additions` is non-empty in the `TranslationUnit`,

Then the orchestrator SHALL persist the additions and continue to chunk 11 immediately, with 0 seconds of pause or user prompt.

#### Scenario: UNLOCKED additions are visible in get_glossary after job completes

Given a job that completed with 3 mid-run additions (all unlocked),

When `engine.get_glossary(job_id)` is called after the job is `DONE`,

Then the returned `Glossary` SHALL contain those 3 entries with `locked=False`.

---

### Requirement: neutral Spanish is defined operationally and enforced via system prompt and eval rubric

"Neutral Spanish" (`target_lang = "es-neutral"`) is defined operationally as:
- No voseo verb conjugations (use `tú` or impersonal forms).
- No region-specific slang or localisms (e.g. no `vos`, `che`, `tío`, `órale`, `pata`, `bacán`, `macanudo`, etc.).
- No leísmo when the speaker is the direct object (follow RAE standard).
- Consistent register: a register established in the system prompt (formal prose or informal dialogue) SHALL not drift within a single job.
- Consistent use of spelled-out numbers/units per the established register.

These rules SHALL appear verbatim in the static instruction block of the system prompt. The eval harness SHALL score `neutral-register` as a named dimension, with a score ≥ 4/5 on the LLM-as-judge rubric considered passing.

#### Scenario: system prompt contains neutral-Spanish rules

Given any job configuration with `target_lang = "es-neutral"`,

When the `ContextManager` builds the system prompt,

Then the system prompt string SHALL contain all five neutral-Spanish constraints listed above (detectable by substring search in a unit test against `FakeTranslationProvider`'s captured system prompt).

#### Scenario: eval harness scores neutral-register dimension

Given a golden sample with known neutral-Spanish translation,

When the LLM-as-judge rubric is run on an engine output,

Then the rubric SHALL produce a `neutral-register` score in the range [1, 5], and a score of ≥ 4 SHALL be the pass threshold for CI advisory gating.
