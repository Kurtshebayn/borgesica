# Tasks: Translation Engine (translation-engine)

Change: `translation-engine` · Phase: tasks · Status: draft · Artifact store: openspec

---

## Legend

- **[T]** = write the failing test first, then implement to green (strict TDD)
- **[I]** = implement only (pure scaffolding, no meaningful behavior to test-drive)
- **seq** = must follow the previous task in the same milestone
- **par** = can run in parallel with other `par` tasks inside the same milestone
- Spec refs use `capability/requirement-keyword` notation

---

## M0 — Scaffolding

All M0 tasks are sequential (each sets up the foundation the next needs).

### M0-1 [x] — pyproject.toml + package skeleton

**Depends on**: nothing  
**Spec**: model-provider/TranslationProvider-Protocol, job-lifecycle/create_job  
**Parallel**: no (foundation)

Create `pyproject.toml` declaring package `borgesica`, Python ≥ 3.11.

Runtime dependencies (pinned to compatible ranges):
- `pydantic>=2.0,<3`
- `srt>=3.5`
- `anthropic>=0.25`

Optional dependency groups:
- `epub = ["ebooklib>=0.18"]`
- `pdf = ["pdfplumber>=0.10"]`
- `pdf-fast = ["pymupdf4llm>=0.0.5"]`  — labeled AGPL in comments
- `spacy = ["spacy>=3.7"]`
- `dev = ["pytest>=8", "pytest-cov", "ruff>=0.4"]`

Create the full directory skeleton (empty `__init__.py` files only):

```
borgesica/
  __init__.py
  api.py                  # stub: TranslatorEngine class, all methods raise NotImplementedError
  domain/
    __init__.py
    models.py             # stub
    ports.py              # stub
    errors.py             # stub
    chunking.py           # stub
    context.py            # stub
    glossary.py           # stub
    orchestrator.py       # stub
    cost.py               # stub
    markup.py             # stub
  adapters/
    __init__.py
    readers/
      __init__.py
      srt_reader.py       # stub
    writers/
      __init__.py
      srt_writer.py       # stub
    providers/
      __init__.py
      anthropic_provider.py  # stub
    checkpoints/
      __init__.py
      sqlite_checkpoint.py   # stub
    extraction/
      __init__.py
      llm_extractor.py    # stub
tests/
  __init__.py
  unit/
    __init__.py
  integration/
    __init__.py
  golden/
    __init__.py
pytest.ini                # testpaths=tests, addopts=--strict-markers -q
```

Deliverable: `pip install -e ".[dev]"` succeeds; `pytest` collects 0 tests with exit code 0.

---

### M0-2 [x] — pytest config + ruff config

**Depends on**: M0-1  
**Parallel**: no

`pytest.ini`:
```ini
[pytest]
testpaths = tests
addopts = --strict-markers -q
markers =
    integration: marks tests as integration (skip unless INTEGRATION=1)
    golden: marks golden/eval tests (skip unless GOLDEN=1)
```

`ruff.toml` (or `[tool.ruff]` in pyproject):
- `target-version = "py311"`
- `line-length = 100`
- `select = ["E", "F", "I"]`

Deliverable: `ruff check borgesica/` exits 0; `pytest` still exits 0.

---

### M0-3 [x] — Test doubles: FakeTranslationProvider + InMemoryCheckpointStore

**Depends on**: M0-1  
**Parallel**: no (models.py must exist as stubs first)

Create `tests/fakes.py`:

```python
# FakeTranslationProvider
# - Implements TranslationProvider Protocol
# - Constructor accepts: canned_unit: TranslationUnit | None = None,
#   fail_on: set[int] = set(), call_log: list = field(default_factory=list)
# - translate(): appends (system, user, model) to call_log; returns canned_unit
#   or raises MalformedOutput if the call count is in fail_on
# - count_tokens(text, model): returns len(text.split()) * 1  (deterministic)
# - price(model): returns (1.0, 5.0)  ← $1/Mtok in, $5/Mtok out
# - Property: call_count  → len(call_log)
# - Supports reset()

# InMemoryCheckpointStore
# - Implements CheckpointStore Protocol
# - All data in plain dicts (no SQLite)
# - All methods are exact mirrors of SQLiteCheckpointStore signatures
# - save_chunk: upsert by (job_id, chunk.index) — idempotent
```

Write one smoke test in `tests/unit/test_fakes.py` verifying:
1. `FakeTranslationProvider` satisfies the Protocol (runtime check via `@runtime_checkable`).
2. `InMemoryCheckpointStore` satisfies the `CheckpointStore` Protocol.
3. `save_chunk` called twice with the same key produces exactly one row.

Deliverable: `pytest tests/unit/test_fakes.py` → 3 tests pass.

---

## M1 — SRT Walking Skeleton

M1 is the engine core. The order within M1 is strictly sequential (each layer depends on the one below). Tasks marked `par` after M1-5 may execute in parallel only after M1-5 is green.

---

### M1-1 [x] — Domain models + enums + errors

**Depends on**: M0-3  
**Spec**: all / domain contract; model-provider/Protocol; job-lifecycle/create_job  
**seq** within M1

Write tests first (`tests/unit/test_models.py`):
- `JobStatus` has all 7 values: CREATED ESTIMATING RUNNING PAUSED DONE FAILED CANCELLED.
- `ChunkStatus` has 4 values: PENDING TRANSLATING DONE FAILED.
- `SourceType` has 3 values: SRT EPUB PDF.
- `GlossaryEntry` validates `locked` defaults to `False`.
- `Glossary` defaults to empty `entries` list.
- `RollingSummary` defaults to `text=""` and `chunk_index=-1`.
- `Chunk` validates `status` defaults to `PENDING`.
- `TranslationUnit` requires `translation` and `summary_update`; `glossary_additions` defaults to `[]`.
- `CostEstimate` has all 5 fields; `within_budget` defaults to `True`.
- `JobConfig` defaults: `target_lang="es-neutral"`, `chunk_size=25`, `line_length=42`, `glossary_strategy="llm"`, `quality_mode="fast"`.
- `Job` accepts all required fields.
- `Progress` has all 5 fields.

Then implement in `borgesica/domain/models.py`.

Also implement `borgesica/domain/errors.py`:
- `BorgésicaError(Exception)` — base
- `BudgetExceeded(BorgésicaError)` — carries `job_id: str`, `cost_so_far: float`
- `MalformedOutput(BorgésicaError)` — carries `job_id: str`, `chunk_index: int`
- `JobNotFoundError(BorgésicaError)` — carries `job_id: str`
- `JobStateError(BorgésicaError)` — carries `job_id: str`, `current_status: str`
- `UnsupportedFormatError(BorgésicaError)` — carries `path: str`, `reason: str`
- `ProviderError(BorgésicaError)` — carries `status_code: int | None`

Add tests for each error class: constructable, inherits from `BorgésicaError`, carries expected attributes.

Deliverable: `pytest tests/unit/test_models.py` → all tests pass.

---

### M1-2 [x] — Ports (Protocols, runtime-checkable)

**Depends on**: M1-1  
**Spec**: model-provider/TranslationProvider-Protocol  
**seq**

Tests (`tests/unit/test_ports.py`):
- `@runtime_checkable` is set on all four Protocols: `DocumentReader`, `DocumentWriter`, `TranslationProvider`, `CheckpointStore`.
- `FakeTranslationProvider` from `tests/fakes.py` passes `isinstance(fake, TranslationProvider)`.
- `InMemoryCheckpointStore` passes `isinstance(store, CheckpointStore)`.
- A class missing one method does NOT pass the Protocol check.

Implement `borgesica/domain/ports.py`:
- `DocumentReader(Protocol)`: `read(path: str, config: JobConfig) -> list[Chunk]`
- `DocumentWriter(Protocol)`: `write(chunks: list[Chunk], src_path: str, out_path: str) -> None`
- `TranslationProvider(Protocol)`: `translate(system: str, user: str, model: str) -> TranslationUnit`; `count_tokens(text: str, model: str) -> int`; `price(model: str) -> tuple[float, float]`
- `CheckpointStore(Protocol)`: all 8 methods from design section 3
- `GlossaryExtractor(Protocol)`: `extract(text: str, config: JobConfig) -> Glossary`
- `ProgressCallback = Callable[[Progress], None]`

Structural import test: `tests/unit/test_domain_purity.py` — parse every `.py` in `borgesica/domain/` with `ast.parse` and assert zero imports of `anthropic`, `openai`, `ollama`, `litellm`, `instructor`, `srt`, `ebooklib`, `pdfplumber`.

Deliverable: `pytest tests/unit/test_ports.py tests/unit/test_domain_purity.py` → all pass.

---

### M1-3 [T] — markup module (strip / reinsert / validate)

**Depends on**: M1-1  
**Spec**: subtitle-translation/inline-tags; book-translation/inline-EPUB-tags  
**seq**

Tests (`tests/unit/test_markup.py`) — all driven by spec scenarios:
1. `strip("The <i>quick</i> fox.")` → `("The quick fox.", [(tag, pos), ...])` with 2 tags.
2. `reinsert("El veloz zorro.", tags)` → output contains `<i>` and `</i>` in valid positions.
3. `validate_tags(original, result)` → `True` when counts match, `False` when they differ.
4. Tags supported: `<i>`, `</i>`, `<b>`, `</b>`, `<u>`, `</u>`, `<em>`, `</em>`, `<strong>`, `</strong>`, `<span ...>`, `</span>`, `<a ...>`, `</a>`.
5. Text with no tags round-trips unchanged.
6. Nested tags preserve order.

Implement `borgesica/domain/markup.py`:
- `strip(text: str) -> tuple[str, list[tuple[str, int]]]`
- `reinsert(plain_translation: str, tags: list[tuple[str, int]], original_plain: str) -> str`
  - Strategy: map tag positions as fraction of original plain length → same fraction in translated plain.
- `validate_tags(original: str, translated: str) -> bool`

Deliverable: `pytest tests/unit/test_markup.py` → all pass.

---

### M1-4 [T] — SRT chunker

**Depends on**: M1-1, M1-3  
**Spec**: subtitle-translation/cues-batched; job-lifecycle/create_job  
**seq**

Tests (`tests/unit/test_chunking.py`) — spec scenarios:
1. 60 cues, `chunk_size=20` → exactly 3 chunks of 20.
2. 55 cues, `chunk_size=25` → chunks of 25, 25, 5.
3. 1 cue → 1 chunk.
4. `chunk.meta` contains `cue_indices: list[int]` for each cue in the batch.
5. No cue is ever split across chunks.

`SrtChunker.chunk(cues: list[Chunk], config: JobConfig) -> list[Chunk]`:
- Accepts individual cue `Chunk` objects (as output by `SrtReader`).
- Groups into batches of `config.chunk_size`.
- Each output `Chunk.source_text` = cues' text joined with `"\n\n"` (SRT block separator).
- Each output `Chunk.meta` = `{"cue_batches": [{"cue_index": int, "start": str, "end": str, "text": str}, ...]}`.
- `Chunk.index` = batch index (0-based).

Note: prose chunker (for EPUB/PDF) goes in M2. Add a `chunk_prose` stub in `chunking.py` that raises `NotImplementedError`.

Deliverable: `pytest tests/unit/test_chunking.py` → all pass.

---

### M1-5 [x] — ContextManager (system prompt assembly)

**Depends on**: M1-1, M1-2  
**Spec**: context-continuity/glossary-injected; context-continuity/rolling-summary; context-continuity/neutral-Spanish; cost-control/prompt-caching; translation-quality/philosophy-prompt  
**seq**

Tests (`tests/unit/test_context.py`):
1. System prompt for `target_lang="es-neutral"` contains all 5 neutral-Spanish constraints (substring test).
2. System prompt always contains translation-philosophy instructions including explicit anti-calque instruction.
3. `Glossary.render(budget_tokens=300)` with 50 entries averaging 8 tokens → output ≤ 300 tokens.
4. Locked entries appear before unlocked in rendered table.
5. All 10 locked entries appear even when total exceeds budget (unlocked truncated).
6. Rolling summary from chunk N-1 is included in chunk N's system prompt.
7. First chunk (no prior summary): prompt contains empty summary or `"No prior context."` placeholder — no exception.
8. Static block length ≥ 1024 tokens → `context_manager.build(...)` returns a structure that signals `cached=True`.
9. Static block < 1024 tokens → `cached=False`.

Implement `borgesica/domain/context.py`:
- `ContextManager` class; constructor accepts `provider: TranslationProvider` (for `count_tokens`).
- `build_system_prompt(config: JobConfig, glossary: Glossary, summary: RollingSummary) -> SystemPrompt`
  where `SystemPrompt` is a dataclass/Pydantic model with `text: str` and `cached: bool` (cache hint to adapter).
- The static instruction block includes (in order):
  1. Task description and output format (JSON / tool schema for `TranslationUnit`).
  2. Neutral-Spanish rules (all 5, verbatim detectable).
  3. Translation philosophy (meaning+image over words, no literal calques, naturalness+fidelity).
- The dynamic block appends: `[GLOSSARY]\n{glossary.render(300)}\n[SUMMARY]\n{summary.text or "No prior context."}`.
- `cached` is `True` iff `provider.count_tokens(static_block, "")` ≥ 1024.

Implement `Glossary.render(budget_tokens: int) -> str` in `models.py`:
- Locked entries first, marked `[LOCKED]`.
- Trim unlocked entries until total token count ≤ `budget_tokens`.

Deliverable: `pytest tests/unit/test_context.py` → all pass.

---

After M1-5 is green, M1-6 through M1-9 may be developed in parallel (they share no write targets with each other).

---

### M1-6 [x] — CostEstimator (par after M1-5)

**Depends on**: M1-5  
**Spec**: cost-control/estimate_cost; cost-control/quality_mode; cost-control/cost-tracked-per-chunk  
**par**

Tests (`tests/unit/test_cost.py`):
1. `estimate_cost` for fast-mode 4-chunk job → exactly 4 provider passes counted.
2. `estimate_cost` for reflective-mode 4-chunk job → exactly 12 passes counted (3 per chunk).
3. `estimate_cost` skips DONE chunks (5-chunk job, 2 DONE → counts 3 pending).
4. Fully DONE job → `usd == 0.0`, `input_tokens == 0`.
5. `within_budget=True` when estimated < `budget_usd`.
6. `within_budget=False` when estimated > `budget_usd`.
7. `budget_usd=None` → `within_budget=True` always.
8. Cost accumulation arithmetic: 100 input + 50 output tokens at $1/$5 per Mtok → `$0.00035` per chunk. 4 chunks → `$0.00140` ± epsilon.
9. Prompt-cache cost decision: **decide here** — prompt cache-write cost is NOT included in the estimate (cache writes are one-time amortized costs; omitting keeps estimates conservative and predictable). Document this in a `# NOTE:` comment in `cost.py`.

Implement `borgesica/domain/cost.py`:
- `CostEstimator` class; constructor accepts `provider: TranslationProvider`.
- `estimate(job: Job, chunks: list[Chunk], config: JobConfig) -> CostEstimate`
  - Count pending chunks.
  - Per pending chunk: `input_tok = count_tokens(system_prompt_approx + chunk.source_text)`, `output_tok = estimated_output`.
  - Multiply by 3 if `quality_mode="reflective"`.
  - Compute `usd` from `provider.price(config.model)`.
  - Set `within_budget` per spec.

Deliverable: `pytest tests/unit/test_cost.py` → all pass.

---

### M1-7 [x] — GlossaryExtractor (LLM default) (par after M1-5)

**Depends on**: M1-1, M1-2  
**Spec**: context-continuity/glossary-seeded; context-continuity/mid-run-additions  
**par**

Tests (`tests/unit/test_glossary.py`):
1. `LlmGlossaryExtractor` satisfies `GlossaryExtractor` Protocol.
2. With `FakeTranslationProvider` returning a canned `TranslationUnit` with `glossary_additions`, `extract()` returns a `Glossary` with those entries.
3. `glossary_strategy="none"` → extractor returns empty `Glossary()`.
4. `GlossaryExtractor` factory function selects the right extractor by `config.glossary_strategy`.

Implement `borgesica/domain/glossary.py`:
- `GlossaryExtractor(Protocol)` — already declared in `ports.py`; implement here as factory.
- `LlmGlossaryExtractor(provider: TranslationProvider)` — calls `provider.translate` with a glossary-extraction prompt; parses `TranslationUnit.glossary_additions` as the seed.
- `NullGlossaryExtractor()` — returns `Glossary()` always.
- `get_extractor(strategy: str, provider: TranslationProvider) -> GlossaryExtractor` factory.

Deliverable: `pytest tests/unit/test_glossary.py` → all pass.

---

### M1-8 [x] — TranslationOrchestrator (core loop) (par after M1-5)

**Depends on**: M1-5, M1-6, M1-7  
**Spec**: job-lifecycle (all scenarios); context-continuity (all); cost-control (all); translation-quality/quality_mode; subtitle-translation/tag-mismatch-retry  
**par** (within M1 after M1-5)

This is the densest task. Write tests first, then implement.

Tests (`tests/unit/test_orchestrator.py`) — ALL spec scenarios for orchestrator behavior:

**Job lifecycle:**
1. `run_job` with 3 chunks + `FakeTranslationProvider` → provider called exactly 3 times, job ends `DONE`.
2. `on_progress` called once per completed chunk (4-chunk job → 4 callbacks), each with correct `chunk_index`.
3. `run_job` on `RUNNING` job → `JobStateError` raised, 0 provider calls.
4. Chunk N persisted `DONE` before chunk N+1 is called (provider raises on call 2 → chunk 0 `DONE`, chunks 1-2 `PENDING`).

**Resume:**
5. Resume with 3 DONE + 2 PENDING → provider called exactly 2 times.
6. Resume rebuilds rolling summary from highest DONE summary (chunk 2's summary appears in chunk 3's prompt).
7. Resume with all chunks DONE → 0 provider calls, job status `DONE`.
8. Resume from `CANCELLED` state → accepted, translates PENDING chunks (confirms resume-from-CANCELLED is valid).

**Budget:**
9. Budget $1.00, 8 chunks done at $0.93, chunk 9 projected at $0.10 → `BudgetExceeded` raised before chunk 9 provider call; job `PAUSED`; `cost_usd == 0.93`.
10. `budget_usd=None` → no budget check, job completes.

**Cancel:**
11. `cancel_job` called after chunk 1 starts → chunk 1 completes+persists, chunk 2 never called, job `CANCELLED`.
12. Cooperative flag checked per chunk (not mid-chunk).

**Reflection:**
13. `quality_mode="fast"` → exactly 1 provider call per chunk.
14. `quality_mode="reflective"` → exactly 3 provider calls per chunk (translate, critique, revise).
15. Reflective: persisted `translated_text` is the REVISE step's output, not the draft.

**Tag retry:**
16. `markup.validate_tags` fails on first attempt, succeeds on second → 2 total provider calls for the chunk; chunk ends `DONE`.
17. `markup.validate_tags` fails on all 3 attempts → chunk `FAILED`, job `PAUSED`.

**Glossary mid-run:**
18. Chunk 3 returns `glossary_additions` with new term → system prompt for chunk 4 contains new term.
19. New term does not override a locked entry with the same `term`.

**Rolling summary:**
20. Summary from chunk N-1 is in chunk N's system prompt (captured from `FakeTranslationProvider.call_log`).
21. First chunk uses empty/placeholder summary.

Implement `borgesica/domain/orchestrator.py`:
- `TranslationOrchestrator` class.
- Constructor: `provider, checkpoint, context_manager, cost_estimator`.
- `run(job: Job, chunks: list[Chunk], glossary: Glossary, config: JobConfig, on_progress, cancel_flag: threading.Event) -> Job`
- Internal per-chunk flow (from design section 6): load summary, build system prompt, strip markup, translate (+ reflection if reflective), validate tags (retry ≤2), reinsert markup, save chunk DONE, update summary, update cost, budget check, fire progress, check cancel flag.
- `cancel_flag` is a `threading.Event`; checked BEFORE each chunk (not inside).

Deliverable: `pytest tests/unit/test_orchestrator.py` → all pass.

---

### M1-9 [x] — SrtReader + SrtWriter adapters (par after M1-5)

**Depends on**: M1-1, M1-3, M1-4  
**Spec**: subtitle-translation (all 5 requirements)  
**par**

Tests (`tests/integration/test_srt_adapters.py`) using real `.srt` fixture files in `tests/integration/fixtures/`:

**SrtReader:**
1. Cue 42 with inline `<i>` tag parsed correctly: `chunk.meta["cue_index"]==42`, correct timestamps, `source_text=="Hello, <i>world</i>."`.
2. Multi-line cue: `source_text=="Line one\nLine two"`.
3. Empty SRT → empty list, no exception.

**SrtWriter + reflow:**
4. Short translation (30 chars) → 1 line, no inserted newline.
5. 57-char translation, `line_length=42` → exactly 2 lines ≤ 42 chars each, split at word boundary.
6. No 2-line split possible → exactly 3 lines ≤ 42 chars each.
7. `line_length=30` respected on a 50-char cue.

**Round-trip:**
8. 10 chunks × 5 cues → output `.srt` has exactly 50 cues with original indices and timestamps.
9. Output parseable by `srt` library (no `SRTParseError`).

Create fixture `.srt` files in `tests/integration/fixtures/`:
- `simple.srt` — 10 cues for basic parsing.
- `tagged.srt` — cues with `<i>`, `<b>`.
- `empty.srt` — zero cues.
- `long_cue.srt` — cue with very long text for reflow tests.

Implement:
- `borgesica/adapters/readers/srt_reader.py` using the `srt` library.
- `borgesica/adapters/writers/srt_writer.py` with `reflow(text: str, line_length: int) -> str` function.

Deliverable: `pytest tests/integration/test_srt_adapters.py` → all pass.

---

### M1-10 [x] — SQLiteCheckpointStore adapter (par after M1-5)

**Depends on**: M1-1, M1-2  
**Spec**: job-lifecycle/idempotent-chunk-save; job-lifecycle/status  
**par**

Tests (`tests/integration/test_sqlite_checkpoint.py`) using SQLite `:memory:`:
1. `save_job` + `load_job` round-trip.
2. `save_chunk` + `load_chunks` round-trip; `meta_json` preserved.
3. `save_chunk` twice with same `(job_id, chunk.index)` → exactly 1 row, no error.
4. `save_chunk` with different `translated_text` same key → overwrites.
5. `save_glossary` + `load_glossary` round-trip; `locked` preserved.
6. `save_summary` + `load_summary` round-trip.
7. `load_job` with unknown `job_id` → returns `None`.
8. All DONE chunks load as `DONE` after restart (new connection, same db file).

Implement `borgesica/adapters/checkpoints/sqlite_checkpoint.py`:
- Schema from design section 5.
- `__init__(db_path: str)` — creates schema on first connect via `CREATE TABLE IF NOT EXISTS`.
- `save_chunk`: `INSERT … ON CONFLICT(job_id, chunk_index) DO UPDATE SET …`.
- All methods use a single `sqlite3.connect(db_path)` per call (stateless — safe for single-user sequential use).

Deliverable: `pytest tests/integration/test_sqlite_checkpoint.py` → all pass.

---

### M1-11 [x] — AnthropicProvider adapter (par after M1-5)

**Depends on**: M1-1, M1-2  
**Spec**: model-provider/Anthropic-adapter; model-provider/graceful-degradation; model-provider/retry-backoff  
**par**

Unit tests (no network — faking the HTTP layer):

`tests/unit/test_anthropic_provider.py`:
1. `AnthropicProvider` satisfies `TranslationProvider` Protocol (runtime check).
2. Tier-3 fallback: `FakeHttpClient` returns invalid JSON on call 1, valid JSON on call 2 → `TranslationUnit` returned on second attempt, no exception.
3. All 3 tiers fail (3 bad responses) → `MalformedOutput` raised, exactly 3 attempts made.
4. 429 with `Retry-After: 2` header → adapter waits ≥ 2 seconds before retry (use `FakeTime` or monkeypatch `time.sleep`).
5. 3× 5xx → `ProviderError` raised after exactly 3 attempts.
6. `count_tokens` delegates to Anthropic `client.count_tokens` or reasonable approximation.
7. `price("claude-haiku-4-5")` returns a tuple of two floats.
8. Domain purity already covered by M1-2's `test_domain_purity.py` — `anthropic` import appears ONLY in `adapters/providers/`.

Integration test (CI-gated by `ANTHROPIC_API_KEY` env):
`tests/integration/test_anthropic_provider_live.py` (marked `@pytest.mark.integration`):
9. Real call with a short English text → valid `TranslationUnit`; `translation` non-empty; `pydantic.ValidationError` not raised.

Implement `borgesica/adapters/providers/anthropic_provider.py`:
- Uses `anthropic` SDK (optionally `instructor` internally — adapter-only import).
- Primary: tool-calling / structured output.
- Tier-3 fallback: parse JSON from text, retry ≤ 2.
- Retry: exponential backoff + jitter for 5xx; honor `Retry-After` for 429.
- `count_tokens`: use `anthropic.client.beta.messages.count_tokens` or `len(text.split()) * 1.3` approximation.
- `price(model: str) -> tuple[float, float]`: lookup table for known models; default `(3.0, 15.0)` for unknown.

Deliverable: `pytest tests/unit/test_anthropic_provider.py` → all pass. Integration test skipped unless key present.

---

### M1-12 [x] — TranslatorEngine public API + DI wiring (seq — requires M1-8 through M1-11)

**Depends on**: M1-8, M1-9, M1-10, M1-11  
**Spec**: job-lifecycle (all); context-continuity/glossary-seeded; context-continuity/update_glossary; cost-control/estimate_cost; model-provider/model-agnostic-selection  
**seq** (integrates all M1 pieces)

Tests (`tests/unit/test_engine.py`) using `FakeTranslationProvider` + `InMemoryCheckpointStore`:
1. `create_job` returns `Job` with `status=CREATED`, `total_chunks=3`, `completed_chunks=0`, `cost_usd=0.0` (75-cue SRT, `chunk_size=25`).
2. All chunks persisted as `PENDING` after `create_job`.
3. `get_glossary` returns a `Glossary` (possibly empty) immediately after `create_job`.
4. `update_glossary` locks an entry before `run_job`; subsequent `get_glossary` returns it locked.
5. Provider `translate` called 0 times after `create_job` only.
6. `run_job` completes; job ends `DONE`; provider called exactly `total_chunks` times.
7. `status(job_id)` returns `Job` matching checkpoint state.
8. `status("unknown-id")` raises `JobNotFoundError`.
9. `estimate_cost` returns `CostEstimate` covering only PENDING chunks.
10. `cancel_job` sets `CANCELLED`; `resume_job` from `CANCELLED` continues from PENDING.
11. `run_job` on `RUNNING` job raises `JobStateError`.
12. Model string passed unchanged to provider (spec: model-agnostic).
13. `on_progress` callback receives `Progress` with accurate `cost_usd` after each chunk.

Implement `borgesica/api.py`:
- `TranslatorEngine` with DI constructor (no defaults — caller provides provider, checkpoint, readers, writers, extractor).
- `create_job`: calls reader → chunker → extractor → checkpoint.save_all.
- `run_job`: validates status, sets RUNNING, delegates to `TranslationOrchestrator.run`.
- `resume_job`: validates status in `{CREATED, PAUSED, CANCELLED}`, delegates to orchestrator.
- `status`: load from checkpoint; raise `JobNotFoundError` if `None`.
- `estimate_cost`: load job+chunks, delegate to `CostEstimator.estimate`.
- `get_glossary` / `update_glossary`: checkpoint round-trip.
- `cancel_job`: sets the `threading.Event` cancel flag; persists `CANCELLED` if job is not running.

Deliverable: `pytest tests/unit/test_engine.py` → all pass.

---

### M1-13 [x] — Thin CLI (borgesica command)

**Depends on**: M1-12  
**Spec**: (implied — user-facing entry point)  
**seq**

Create `borgesica/__main__.py` and declare `borgesica` entry point in `pyproject.toml`.

Subcommands (minimal, enough to drive a real SRT job):
- `borgesica create <srt_file> --model <model> [--chunk-size N] [--budget USD] [--quality-mode fast|reflective]` → prints job ID.
- `borgesica estimate <job_id>` → prints `CostEstimate` as JSON.
- `borgesica run <job_id> [--out <path>]` → streams progress to stdout, exits 0 on success.
- `borgesica resume <job_id>` → same as run but from paused/cancelled state.
- `borgesica status <job_id>` → prints `Job` as JSON.
- `borgesica glossary show <job_id>` → prints glossary as JSON.
- `borgesica glossary update <job_id> <term> <translation> [--lock]` → locks/updates a term.
- `borgesica cancel <job_id>` → sets cancel flag.

CLI wires the default DI: `AnthropicProvider` + `SqliteCheckpointStore(~/.borgesica/jobs.db)` + `SrtReader/SrtWriter`.

No tests required for CLI itself (behavior covered by engine tests). Manual smoke test with a 10-cue fixture is sufficient.

Also create `MODELS.md` in project root (spec: model-provider/documentation):
- Three tiers: Max quality, Best value, Private/free/offline.
- At least one example model ID per tier.
- Note that `borgesica` does not restrict or validate model strings.

---

## M1-FIX — M1 Verify Fix-Up (W-1, W-2, S-1, S-3, W-3)

These tasks address the sdd-verify findings reported after M1 completion.
S-2 is explicitly deferred to M4/cost-control — DO NOT implement here.

### M1-FIX-1 [x] — W-1: SrtChunker must store line_length in chunk meta

**Fixed**: `borgesica/domain/chunking.py` — `SrtChunker.chunk()` now stores `config.line_length` in each batch chunk's meta dict so `SrtWriter` reads the configured value instead of the hardcoded 42.
**TDD**: Full-pipeline integration test added to `tests/integration/test_engine_e2e.py` (`test_engine_e2e_line_length_propagates_through_pipeline`). Test confirmed RED (all 6 cues used 42-char lines despite `line_length=30`) before fix, GREEN after.

---

### M1-FIX-2 [x] — W-2: CostEstimate.cached must reflect static-block caching eligibility

**Fixed**: `borgesica/domain/cost.py` — `CostEstimator.__init__` now accepts an optional `context_manager: ContextManager` param. When present, `estimate()` calls `context_manager.get_static_block(config)` → `provider.count_tokens()` and sets `cached=True` iff count ≥ 1024 (Anthropic minimum). Backward compat: omitting `context_manager` keeps `cached=False`.
`borgesica/api.py` updated to pass `context_manager=self._ctx` to `CostEstimator`.
**TDD**: 3 new tests in `tests/unit/test_cost.py`: `test_cached_true_when_static_block_meets_min`, `test_cached_false_when_static_block_below_min`, `test_cached_false_when_no_context_manager`. All confirmed RED before fix, GREEN after.

---

### M1-FIX-3 [x] — S-1: Strengthen test_three_line_fallback to assert exactly 3 lines

**Fixed**: `tests/integration/test_srt_adapters.py` — `test_three_line_fallback` now uses text that provably cannot fit in 2 lines (5 seven-char words with line_length=20) and asserts `len(lines) == 3` exactly (not `1 <= len(lines) <= 3`). The old text produced 2 lines, making the 3-line fallback path untested.

---

### M1-FIX-4 [x] — S-3: reflow graceful with over-long single token

**Fixed**: `borgesica/adapters/writers/srt_writer.py` — added module-level `logger = logging.getLogger(__name__)`. When a single token exceeds `line_length`, `reflow()` now logs a WARNING before the 3-line fallback. The 3-line cap (NEVER 4+) was already enforced; this adds observability.
**TDD**: 2 new tests: `test_overlong_single_word_never_produces_four_lines` (confirms ≤3 lines) and `test_overlong_single_word_logs_warning` (confirms warning is emitted). Both confirmed RED → GREEN.

---

### M1-FIX-5 [x] — W-3: Document Tier-2 intentional deviation in AnthropicProvider

**Fixed**: `borgesica/adapters/providers/anthropic_provider.py` — module docstring updated with explicit TIER-1 / TIER-2 (INTENTIONALLY SKIPPED) / TIER-3 labels and a clear rationale for why Tier-2 (Anthropic JSON mode) is omitted in favour of Tier-1 (tool-use) + Tier-3 (text-JSON-parse fallback). No code change.

---

## M2 — EPUB Support

M2 begins with **M2-0** (the shared tag-rework — engram `sdd/translation-engine/todo-tag-placement`), a prerequisite for EPUB because EPUB prose is markup-dense and would amplify the proportional-reinsert bug. After M2-0, M2-1 and M2-2 are parallel; M2-3 requires both.

---

### M2-0 [x] — Tag-rework: tags-in-text primary, strip/reinsert fallback (+ pending CLI UTF-8 regression test)

**Depends on**: M1-12 (engine green)
**Spec**: subtitle-translation/inline-tags-in-text; book-translation/inline-EPUB-tags
**seq** (must land before any EPUB task)

Context: the 2026-06-25 live real-API test (Haiku) reproduced inline tags SPANNING cues because `markup.reinsert` places tags by proportional character position across a multi-cue chunk. Fix: keep tags IN the text and instruct the model to carry them with the words; keep `strip`/`reinsert` as the deterministic fallback for weak/local models.

Tests first (strict TDD):

`tests/unit/test_context.py` (extend):
1. System prompt contains an explicit "preserve inline tags / move them with the words / keep exact count" instruction (substring test).

`tests/unit/test_orchestrator.py` (extend):
2. Tags-in-text primary: provider returns a translation that KEEPS the tags and passes `validate_tags` → 1 provider call, chunk `DONE`, and `markup.strip` is NOT applied before the provider call (assert via call_log / spy on the user message containing the raw tags).
3. Mismatch on attempt 1, valid on attempt 2 → 2 provider calls, chunk `DONE` (retry behavior preserved under the new path).
4. All 3 tags-in-text attempts fail → engine falls back to `strip`→translate-plain→`reinsert`; fallback passes `validate_tags` → chunk `DONE` using fallback output.
5. Both tags-in-text (3 attempts) and the strip/reinsert fallback fail → chunk `FAILED`, job `PAUSED`.
6. Cue-spanning regression: a 2-cue chunk where cue 1 = `"We don't have <i>much</i> time"` → translated output keeps `<i>...</i>` WITHIN cue 1's text after the SrtWriter `"\n\n"` split (no tag leaks into cue 2). This is the exact bug from the live test.

`tests/unit/test_markup.py` (extend):
7. `strip`/`reinsert`/`validate_tags` behavior unchanged (no regression to M1 tests); `reinsert` is now exercised as fallback only.

CLI regression (pending from M1, commit 9440760):
8. CLI prints a non-ASCII character (the progress arrow) on a cp1252-configured stdout WITHOUT raising `UnicodeEncodeError` (regression test for the UTF-8 stdout/stderr reconfigure).

Implement:
- `borgesica/domain/context.py` — add the tag-preservation instruction to the static system-prompt block (always-on text is fine; cheap).
- `borgesica/domain/orchestrator.py` — change the per-chunk flow: send `source_text` WITH tags (do NOT strip up front); after translate, `validate_tags(source, translated)`; retry ≤2; on exhaustion, fall back to `strip`→translate-plain→`reinsert`→`validate_tags`; only then FAILED/PAUSED.
- `borgesica/domain/markup.py` — keep `strip`/`reinsert`/`validate_tags` as-is (fallback). Add a `# NOTE:` documenting fallback-only status + the M4 hardening pointer.
- `borgesica/__main__.py` — ensure the UTF-8 stdout/stderr reconfigure is covered by the new regression test; refactor only if needed for testability.

Deliverable: full suite green; the cue-spanning regression (test 6) confirmed RED before the orchestrator change, GREEN after.

---

### M2-1 [x] — EpubReader adapter

**Depends on**: M1-12  
**Spec**: book-translation/EPUB-reader; book-translation/spine-order; book-translation/images  
**par**

Tests (`tests/integration/test_epub_reader.py`) with `ebooklib`-generated fixture EPUBs:
1. Valid non-DRM EPUB → list of chunks, no exception.
2. `encryption.xml` present → `UnsupportedFormatError` with DRM message.
3. Invalid ZIP file with `.epub` extension → `UnsupportedFormatError`.
4. Spine order: chapters are ordered per OPF spine.
5. Images not extracted as chunks (no `Chunk.source_text` contains binary data).
6. `chunk.meta` contains `epub_item_href` and `node_path`.

Implement `borgesica/adapters/readers/epub_reader.py` using `ebooklib`:
- Detect `encryption.xml` → raise `UnsupportedFormatError`.
- Traverse spine; extract `<body>` text nodes only.
- Each text node → one candidate chunk with path metadata.

Deliverable: `pytest tests/integration/test_epub_reader.py` → all pass.

---

### M2-2 [x] — Prose chunker (original contract)

**Depends on**: M1-4  
**Spec**: book-translation/prose-chunking  
**par**

NOTE: M2-2 shipped with the old `list[list[str]]` signature. See **M2-2R** (immediately below) which supersedes the contract and resolves the composition gap with EpubWriter. M2-3 depends on M2-2R, not M2-2.

---

### M2-2R [x] — Prose chunker provenance rework

**Depends on**: M2-1, M2-2  
**Spec**: book-translation/prose-chunking (updated), book-translation/EPUB-writer reinsertion  
**seq** (prerequisite for M2-3)

**Context**: Post-M2 review (decision #291, gap #290) found that M2-1 EpubReader (per-node Chunks with `node_path`) and M2-2 `chunk_prose` (`list[list[str]]`, drops provenance) do NOT compose — a translated prose chunk cannot be mapped back to its source XHTML node, blocking EpubWriter.

**Fix**: Change signature to mirror SrtChunker (SrtReader → SrtChunker → SrtWriter).

**EpubReader change** (`borgesica/adapters/readers/epub_reader.py`):
- Add `chapter_index: int` (0-based per spine document) to each Chunk's meta alongside the existing `epub_item_href` and `node_path`.
- Pass `chapter_index` from the spine traversal loop in `EpubReader.read()`.

**chunk_prose new signature** (`borgesica/domain/chunking.py`):
```python
chunk_prose(node_chunks: list[Chunk], config: JobConfig, provider: TranslationProvider) -> list[Chunk]
```
Behavior:
- Skip empty/whitespace nodes.
- Group by `meta["chapter_index"]`; NEVER cross chapter boundaries.
- Greedy accumulation within `prose_chunk_tokens` budget.
- Over-budget single node → sentence split; over-budget sentence → hard-split + exactly ONE WARNING per sentence (not per fragment).
- Output Chunk meta:
  - `prose_nodes`: ordered `list[{"epub_item_href": str, "node_path": str}]`, one entry per `"\n\n"` segment.
  - `hard_split=True` on hard-split chunks.

**Tests** (`tests/unit/test_prose_chunker.py`) — rewritten, 6 tests covering:
1. Nodes within budget → 1 chunk; `prose_nodes` lists both in order.
2. Over-budget chapter → multiple chunks ≤ budget; `prose_nodes` correct per chunk.
3. Single over-budget node → hard-split + exactly 1 WARNING; all chunks carry `hard_split=True` and `prose_nodes`.
4. Chapter isolation: `== 2` chunks (not `>= 2`) when two chapters each have 1 node under budget.
5. Provenance: `source_text.split("\n\n")` length == `len(meta["prose_nodes"])`; segment `i` aligns to `prose_nodes[i].node_path`.
6. Empty/whitespace nodes skipped.

**Integration test** (`tests/integration/test_epub_reader.py`) — Test 7 added:
- Each chunk carries `chapter_index` as int.
- Nodes from the same spine doc share the same `chapter_index`.
- `chapter_index` is 0-based and increases per spine document.

**SDD docs updated**:
- `design.md`: added "EPUB prose provenance & reinsertion" subsection.
- `specs/book-translation/spec.md`: updated requirements and scenarios to match new contract.
- `tasks.md` (this file): M2-2R added; M2-3 depends on M2-2R.

Deliverable: full suite 196 passed, 1 skipped; ruff exits 0; domain purity green.

---

### M2-3 [x] — EpubWriter adapter (seq — requires M2-1 + M2-2R)

**Depends on**: M2-1, M2-2  
**Spec**: book-translation/EPUB-writer; book-translation/EPUB-tags  
**seq**

Tests (`tests/integration/test_epub_writer.py`):
1. Output EPUB opens with `ebooklib.epub.read_epub`, no exception.
2. Chapter count equals source (12-chapter fixture).
3. Images byte-identical in output.
4. CSS stylesheets byte-identical.
5. No partial output on exception: if writer raises mid-way, `out_path` must not exist or must be incomplete (verify with temp path + `os.replace` strategy).
6. EPUB italic tag `<em>...</em>` round-trips through markup pipeline.

Implement `borgesica/adapters/writers/epub_writer.py`:
- Write to `out_path + ".tmp"` in the SAME directory as `out_path`, then `os.replace(tmp, out_path)` on success. This makes the write atomic on Windows (same-filesystem rename).
- Rebuild XHTML content by reinserting translated text nodes into source document structure.
- Preserve all binary blobs (images, fonts), OPF, NCX/NAV, container.xml.

Deliverable: `pytest tests/integration/test_epub_writer.py` → all pass.

---

## M2-FIX — M2 Verify Fix-Up (W-M2-1, W-M2-2, S-M2-1, S-M2-3)

These tasks address the sdd-verify findings reported after M2 completion.
S-M2-2 (non-UTF-8 chapter encoding) and W-M2-3 (chapter_index gaps on empty spine items) are DEFERRED — tracked but not fixed here.

### M2-FIX-1 [x] — W-M2-1: DRM detection must be case-insensitive

**Fixed**: `borgesica/adapters/readers/epub_reader.py` — `_check_drm()` changed from `"META-INF/encryption.xml" in names` (case-sensitive `in` check) to `any(n.lower() == "meta-inf/encryption.xml" for n in names)`. EPUBs with `META-INF/ENCRYPTION.XML` or any mixed-case variant now raise `UnsupportedFormatError` with the DRM-specific message.
**TDD**: New test `test_drm_detection_case_insensitive` in `tests/integration/test_epub_reader.py`. Fixture EPUB has uppercase `META-INF/ENCRYPTION.XML` entry. Confirmed RED (DID NOT RAISE) before fix, GREEN after. Asserts `"drm"` in message and excludes generic "not a valid epub" wording.

---

### M2-FIX-2 [x] — W-M2-2: End-to-end EPUB engine test

**Added**: `tests/integration/test_epub_engine_e2e.py` with 2 tests covering the full EPUB pipeline via `TranslatorEngine`. Test 1 (`test_epub_engine_e2e_basic`) drives `create_job → run_job → EpubWriter.write` for a 2-chapter fixture EPUB, asserts job ends DONE, output opens with `ebooklib.epub.read_epub`, and translated text lands in the correct chapter document (per-chapter placement with cross-chapter guard). Test 2 (`test_epub_engine_e2e_resumable_done_job`) proves resume on a DONE EPUB job makes 0 extra provider calls.
**Wiring**: `EpubTagFakeProvider` (subclass of `FakeTranslationProvider`) prefixes `"[ES] "` to each `\n\n`-separated segment, preserving tag count so `validate_tags` passes. Engine wired with `{SourceType.EPUB: EpubReader(), SourceType.SRT: SrtReader()}` readers and matching writers; `InMemoryCheckpointStore`; `NullGlossaryExtractor`.

---

### M2-FIX-3 [x] — S-M2-1: Cue-spanning regression test is now a genuine guard

**Hardened**: `tests/unit/test_orchestrator.py::test_cue_spanning_tag_regression` — added assertion `assert "<i>" in provider.call_log[0][1]`. The `call_log` tuple shape is `(system, user, model)`; index `[1]` is the user prompt. This assertion would FAIL against the old strip-before-call flow (where `<i>` was stripped from source before the provider call). All existing assertions preserved.

---

### M2-FIX-4 [x] — S-M2-3: Provenance round-trip test has per-chapter assertions

**Hardened**: `tests/integration/test_epub_writer.py::test_e2e_provenance_round_trip` — replaced the weak `"[ES] " in all_text` check with per-chapter ZIP inspection: chapter-1 translation appears in `ch1.xhtml`, chapter-2 in `ch2.xhtml`, chapter-3 in `ch3.xhtml`, plus cross-chapter guards (ch1 text not in ch2, ch2 text not in ch1). A bug that dumps all translations into one node or the wrong chapter now fails at least one assertion.

---

### DEFERRED — S-M2-2: Non-UTF-8 chapter encoding

Not fixed in this slice. Tracked for a future hardening pass. Non-UTF-8 spine documents (e.g. ISO-8859-1 or windows-1252) may produce mojibake in translated output; requires encoding-detection at the reader or writer level.

### DEFERRED — W-M2-3: chapter_index gaps on empty spine items

Not fixed in this slice. When spine items produce 0 chunks (empty documents, nav-only items, etc.), `chapter_index` increments past them, producing a sparse sequence. Downstream consumers must tolerate gaps. A future fix could make `chapter_index` reflect only non-empty spine documents.

---

## M3 — PDF Support (coarser-grained; design is done)

M3 tasks depend on M2 being complete (prose chunker and EpubWriter patterns are established).

### M3-1 [x] — PdfPlumberReader adapter

**Depends on**: M2-3  
**Spec**: book-translation/PDF-reader  

Tests (`tests/integration/test_pdf_reader.py`):
1. Header/footer stripping: string appearing on ≥ 80% of pages removed from output.
2. Hyphenated line-break rejoined (`incompre-\nhensible` → `incomprehensible`).
3. Default DI config: `pdf_pymupdf_reader.py` NOT in `sys.modules` (import purity test).

Implement `borgesica/adapters/readers/pdf_plumber_reader.py` (pdfplumber, MIT).
Create `borgesica/adapters/readers/pdf_pymupdf_reader.py` as a commented stub with AGPL header — not imported by default anywhere.
Create `borgesica/adapters/writers/pdf_writer.py` stub (`write` raises `NotImplementedError("PDF output not supported in M3 — export as text or use source EPUB")`).

**Implemented**: All 3 tests RED-before / GREEN-after. Suite: 209 passed, 1 skipped. ruff clean. Domain purity green. pdfplumber installed via `pip install -e ".[dev,pdf]"`. fpdf2 added to `dev` extras for fixture generation. Boilerplate detection threshold requires ≥ 3 pages to avoid false positives on single-page PDFs. `__main__.py` wires `SourceType.PDF → PdfPlumberReader` and `SourceType.PDF → PdfWriter`. PyMuPDF stub is a commented-only file — never imported in default paths.

---

## M3-FIX — M3 Verify Fix-Up (W-M3-1, W-M3-2, S-M3-2)

These tasks address the sdd-verify findings reported after M3-1 completion.
W-M3-3 and W-M3-4 are explicitly deferred — tracked but not fixed here.

### W-M3-1 [x] — Document the layout=True deviation (doc + comment only)

**Fixed**: Documentation only — no code behavior changed.
- `openspec/changes/translation-engine/specs/book-translation/spec.md`: added "Deviation (M3-FIX / W-M3-1)" paragraph to the PDF reader requirement explaining that default `extract_text()` is used (not `layout=True`) because layout mode's positional whitespace padding makes verbatim line-frequency header/footer detection unreliable; reading order is still preserved.
- `openspec/changes/translation-engine/design.md`: added "Deviation (M3-FIX / W-M3-1)" paragraph to Decision 4 with the same rationale.
- `borgesica/adapters/readers/pdf_plumber_reader.py`: added a `# NOTE:` comment at the `extract_text()` call site (line ~86) pointing to the rationale and referencing W-M3-1 in spec.md and design.md.

---

### W-M3-2 [x] — Route PDF prose through chunk_prose, not SrtChunker

**Composition choice**: `chunk_prose` generalized to be format-agnostic (no EPUB-specific keys hardcoded). The `node_ref` dict now passes through all meta keys EXCEPT `chapter_index` (the grouping key), making it work for both EPUB (`{epub_item_href, node_path}`) and PDF (`{pdf_page, para_index}`). No EPUB tests broken. `PdfPlumberReader` simplified to emit flat meta (`{pdf_page, chapter_index, para_index}`) instead of a pre-nested `prose_nodes` list — the reader no longer pre-builds what `chunk_prose` is responsible for building.

**Files changed**:
- `borgesica/adapters/readers/pdf_plumber_reader.py`: removed nested `prose_nodes` from emitted meta; now emits flat `{pdf_page, chapter_index, para_index}` per chunk. Updated module docstring.
- `borgesica/domain/chunking.py`: `node_ref` now built as `{k: v for k, v in node.meta.items() if k != "chapter_index"}` — format-agnostic pass-through. Updated `chunk_prose` docstring.
- `borgesica/api.py`: dispatch changed from `if EPUB → chunk_prose else SrtChunker` to `if EPUB or PDF → chunk_prose else SrtChunker`.

**TDD**: `tests/integration/test_pdf_chunking_dispatch.py` — 2 tests.
- `test_pdf_create_job_uses_chunk_prose_not_srt_chunker`: asserts that PDF chunks carry `prose_nodes` (not `cue_batches`), each node has `pdf_page` and `para_index` keys.
- `test_pdf_chunk_prose_nodes_alignment`: asserts `source_text.split("\n\n")` length == `len(prose_nodes)` per chunk.
- RED: Chunk 0 meta had `{'cue_batches': [...], 'line_length': 42}` — SrtChunker confirmed.
- GREEN: after dispatch fix, all chunks carry `prose_nodes` with `pdf_page`/`para_index` locators.

---

### S-M3-2 [x] — Chapter-detection test coverage

**Added**: `tests/integration/test_pdf_chapter_detection.py` — 2 tests.
- `test_chapter_headings_produce_distinct_chapter_index_values`: 2-chapter PDF (each on its own page starting with "Chapter N" heading); asserts ≥2 distinct, 0-based, consecutive `chapter_index` values across chunks; asserts chapter-1 content has lower `chapter_index` than chapter-2 content.
- `test_three_chapter_pdf_produces_three_distinct_chapter_indices`: 3-chapter PDF; asserts indices 0, 1, 2 all present.
- RED: code existed but was completely untested (zero coverage of `_is_chapter_heading` / `chapter_index` increment path).
- GREEN: existing implementation is correct; tests pass immediately once written.

---

### DEFERRED — W-M3-3: integration marker has no conftest.py enforcement hook

Not fixed in this slice. Pre-existing gap since M2: `pytest.ini` declares `integration` and `golden` markers but there is no `conftest.py` skip hook to gate them behind `INTEGRATION=1`. All 53 integration tests currently run in every `pytest` invocation. Tracked for a future infrastructure hardening pass.

### DEFERRED — W-M3-4: hyphen-rejoin over-joins legitimate compound hyphens

Not fixed in this slice. The regex `(\w)-\n(\w)` cannot distinguish hyphenation artifacts (`incompre-\nhensible` → `incomprehensible`, correct) from legitimate compound hyphens split across lines (`well-\nknown` → `wellknown`, incorrect). Fixing this requires a dictionary-backed disambiguation approach. Tracked for a future text-cleanup hardening pass.

---

## M4 — Quality Harness + Provider Breadth (coarser-grained)

M4 depends on M1-12 (engine is complete).

### M4-1 [x] — Golden fixtures + QualityScore model

**Depends on**: M1-12  
**Spec**: quality-evaluation/golden-fixtures; translation-quality/calque-golden-sample  

Create `tests/golden/` fixtures:
- 5 SRT fixture pairs (YAML format: `source`, `expected`, `glossary`, `notes`):
  1. Standard dialogue
  2. Text with inline `<i>` tags
  3. Text that triggers 3-line reflow
  4. Cue with a proper noun in the glossary
  5. Cue with region-specific expression (requires neutralization)
- 3 prose paragraph pairs:
  1. Literary register consistency
  2. Glossary term consistency across two paragraphs
  3. Named entity
- 1 calque sample: source = "a slot for a locking wooden beam across the door"; golden note explains tranca vs. viga idiom.

Add `QualityScore` to `models.py`:
```python
class QualityScore(BaseModel):
    accuracy: int = Field(..., ge=1, le=5)
    fluency: int = Field(..., ge=1, le=5)
    neutral_register: int = Field(..., ge=1, le=5)
    glossary_consistency: int = Field(..., ge=1, le=5)
```

Tests (`tests/unit/test_quality_score.py`): `QualityScore` validates range [1,5]; out-of-range raises `ValidationError`.

**Implemented**: TDD RED→GREEN. `QualityScore` added to `borgesica/domain/models.py` with `ge=1, le=5` constraints on all four fields. 9 golden fixtures authored in `tests/golden/` (5 SRT + 3 prose + 1 calque). Fixture guard test `tests/unit/test_golden_fixtures.py` verifies schema (≥9 files, 4 required fields, glossary is list, source/expected non-empty). `pyyaml>=6.0` added to `dev` extras in `pyproject.toml`. Suite: 232 passed, 1 skipped (up from 213); ruff clean; domain purity green.

---

### M4-2 [x] — LLM-as-judge harness

**Depends on**: M4-1  
**Spec**: quality-evaluation/LLM-as-judge; quality-evaluation/back-translation  
**par**

Tests (marked `@pytest.mark.golden`; skipped unless `GOLDEN=1`):
1. Harness returns `QualityScore` with all 4 dimensions in [1,5].
2. Calque sample scores `naturalness/meaning-fidelity` < 4 for a viga-style rendering.
3. Missing locked term scores `glossary_consistency` ≤ 2.
4. CI advisory fails when any dimension < 4.
5. Back-translation skipped when `--back-translate` not set (0 extra provider calls).

Implement `borgesica/domain/quality.py`:
- `QualityHarness(provider: TranslationProvider)`.
- `evaluate(source: str, translation: str, glossary: Glossary, model: str) -> QualityScore`.
- Optional `back_translate: bool = False`.
- Judgment prompt covers all 4 rubric dimensions.

**Implemented**: TDD RED→GREEN. `borgesica/domain/quality.py` created with `QualityHarness`,
`advisory_gate()`, and `AdvisoryResult`. Deterministic unit tests in `tests/unit/test_quality.py`
(19 tests). Golden advisory tests in `tests/golden/test_judge_golden.py` (3 tests, SKIPPED
without `GOLDEN=1`). Protocol NOT modified — TranslationUnit-as-carrier pattern used with
`# NOTE:` documenting the deliberate reuse. Back-translation uses `difflib.SequenceMatcher`
(stdlib). Suite: 251 passed, 4 skipped. ruff clean. Domain purity green.

**M4-2-FIX**: `evaluate()` is Tier-2 pure (no `back_translate` param, no `object.__setattr__` hack, returns clean `QualityScore`). `back_translation_similarity(source, translation, model) -> float` is a separate Tier-3 method on `QualityHarness` — callers opt in by calling it; skipping costs 0 extra provider calls. Tautological `hasattr(result, "back_similarity") or True` assertion removed; replaced with genuine ratio assertion (`ratio == 1.0` for identical strings). Suite: 250 passed, 4 skipped (net -1 test from 3→2 back-translate tests).

---

### M4-3 [x] — OpenAICompatibleProvider (DeepSeek + other OpenAI-compatible providers)

**Depends on**: M1-11  
**Spec**: quality-evaluation/OpenAICompatibleProvider (new requirement, above Ollama requirement)  
**par**

This task mirrors M1-11 (AnthropicProvider) but targets any OpenAI `/chat/completions`-compatible endpoint. No network in unit tests — all behavior is driven via a `FakeHttpClient`.

Unit tests (`tests/unit/test_openai_compatible_provider.py`):

1. `OpenAICompatibleProvider` satisfies `TranslationProvider` Protocol (runtime `isinstance` check).
2. **Tier-1 tool-call success**: `FakeHttpClient` returns a valid tool-call response on the first call → `TranslationUnit` returned, exactly 1 HTTP call made.
3. **Tier fallback to JSON mode**: Tier-1 tool-call rejected by `FakeHttpClient`; Tier-2 JSON-mode response is valid → `TranslationUnit` returned, 2 HTTP calls made.
4. **JSON-mode empty-content → Tier-3 retry → success**: `FakeHttpClient` fails Tier-1 and returns EMPTY content on Tier-2 (the known DeepSeek JSON-mode quirk) → adapter falls through to Tier-3; valid JSON returned on first Tier-3 retry → `TranslationUnit` returned, no exception.
5. **All tiers exhausted → `MalformedOutput`**: `FakeHttpClient` fails Tier-1, returns empty on Tier-2, returns invalid JSON on both Tier-3 retries → `MalformedOutput` raised; total call count equals Tier-1 + Tier-2 + 2 Tier-3 retries (no extra calls).
6. **429 Retry-After honored**: `FakeHttpClient` returns HTTP 429 with `Retry-After: 2` on first call, valid on second → adapter waits ≥ 2 seconds (monkeypatch `time.sleep`) before retry, returns valid `TranslationUnit`.
7. **3 × 5xx → `ProviderError`**: `FakeHttpClient` returns HTTP 5xx on all 3 attempts → `ProviderError` raised after exactly 3 attempts.
8. **`price()` / `count_tokens()`**: `price("deepseek-v4-flash")` returns a tuple of two floats matching the DeepSeek Flash preset; unknown model returns the configured table default. `count_tokens(text, model)` returns a non-negative integer.
9. **Configurable `base_url` / `api_key` / price-table with DeepSeek preset**: constructing the adapter with the DeepSeek preset (`base_url="https://api.deepseek.com"`, `default_model="deepseek-v4-flash"`) and inspecting the outgoing HTTP request asserts the correct base URL and model string in the request body (model string passed unchanged).
10. **Import purity**: `openai` and `httpx` do NOT appear in any file under `borgesica/domain/` — already covered by the existing `test_domain_purity.py`; reference it, do not duplicate.

Integration test (CI-gated by `DEEPSEEK_API_KEY` env, marked `@pytest.mark.integration`):
11. Real call to `https://api.deepseek.com` with a short English text → valid `TranslationUnit`; `translation` non-empty; `pydantic.ValidationError` not raised.

Implementation targets:
- `borgesica/adapters/providers/openai_compatible_provider.py` — generic adapter; `openai` SDK (or `httpx` directly) as the HTTP layer, imported here and nowhere else.
- A `DeepSeekPreset` factory function or named constructor (e.g. `OpenAICompatibleProvider.deepseek(api_key)`) with `base_url="https://api.deepseek.com"`, `default_model="deepseek-v4-flash"`, and a price table covering `deepseek-v4-flash` and `deepseek-v4-pro`.
- `MODELS.md` — add DeepSeek models under the "Best Value" tier (see M4 MODELS.md update below).

**Implemented**: TDD RED→GREEN. Client choice: `openai` SDK (custom `base_url`), mirroring AnthropicProvider's vendor-SDK pattern. All 10 unit tests pass in `tests/unit/test_openai_compatible_provider.py` (14 test methods total including sub-cases). Integration test skipped without `DEEPSEEK_API_KEY`. Suite: 264 passed, 5 skipped (up from 250/4). ruff clean. Domain purity green — `openai` only in `adapters/providers/`. `openai-compat = ["openai>=1.0"]` added to `pyproject.toml` optional extras. Key design decisions: `_call_with_retry` helper retries 429 in-place (same tier) before falling through; empty Tier-2 content (DeepSeek quirk) detected as `not content.strip()` and triggers Tier-3; `_Propagate` sentinel relays server_err_count across tiers. TranslationProvider Protocol NOT modified.

---

### M4-4 [x] — OllamaProvider adapter

**Depends on**: M1-11  
**Spec**: quality-evaluation/Ollama-adapter  
**par**

NOTE: Ollama exposes an OpenAI-compatible `/v1/chat/completions` endpoint. The apply phase for this task MAY implement `OllamaProvider` as a thin config of `OpenAICompatibleProvider` (constructed with `base_url=f"http://{OLLAMA_HOST}/v1"`, `api_key="ollama"`, and a price table of zeroes) rather than a separate native adapter. That decision is left to the implementer; either approach is acceptable as long as the tests below pass and no network calls are made in unit tests.

Tests:
1. `OllamaProvider` satisfies `TranslationProvider` Protocol.
2. `OllamaProvider` not in `sys.modules` when not explicitly imported (import purity).

Integration test (marked `@pytest.mark.integration`; gated by `OLLAMA_HOST` env):
3. Real Ollama call → valid `TranslationUnit`, no `ValidationError`.

Implement `borgesica/adapters/providers/ollama_provider.py`.

**Implemented** (M4-4): `OllamaProvider` is a thin subclass (not wrapper) of `OpenAICompatibleProvider`. Design decision: subclass chosen over wrapper because OllamaProvider IS-A OpenAICompatibleProvider with different defaults (no duplicate constructor logic needed). Preset: `base_url="http://localhost:11434/v1"` (or `http://{OLLAMA_HOST}/v1` if env is set), `api_key="ollama"` (SDK requires non-empty; Ollama ignores it), `default_model="llama3"`, `price_table={}` with `price()` override returning `(0.0, 0.0)` for all models. No native `ollama` client lib — reuses the `openai` SDK from `openai-compat` extra (M4-3). Full Tier-1/2/3 fallback chain inherited. 7 unit tests RED→GREEN. Live integration test skipped without `OLLAMA_HOST`. Suite: **271 passed, 6 skipped**; ruff clean; domain purity green; `ollama_provider` NOT in default DI (not imported by api.py or __main__.py).

---

### M4-5 [x] — EpubReader: honor declared XHTML encoding (deferred from M2, S-M2-2)

**Depends on**: M2-1
**Spec**: book-translation/EPUB-reader
**par**

Deferred from M2 (verify finding S-M2-2). A non-UTF-8 EPUB chapter (e.g. `iso-8859-1`) currently produces replacement characters in `source_text` (`"Café"` → `"Caf�"`) because the XML parser defaults to UTF-8. Real correctness bug; deferred only because modern EPUBs are overwhelmingly UTF-8.

Tests first (`tests/integration/test_epub_reader.py`):
1. A fixture EPUB whose chapter is encoded `iso-8859-1` with accented text → extracted `source_text` contains the correct accented characters (no `�`).

Implement in `borgesica/adapters/readers/epub_reader.py`:
- Detect the chapter's encoding (XML declaration / EPUB content) and parse with the matching `etree.XMLParser(encoding=...)` (or decode bytes with the declared charset before parsing).

Deliverable: the non-UTF-8 fixture round-trips with correct characters; existing EPUB tests stay green.

**Implemented** (M4-5 / S-M2-2): Root cause identified: ebooklib's `EpubHtml.get_content()` calls `parse_html_string(self.content)` with `html.HTMLParser(encoding='utf-8')` unconditionally, then re-serialises as UTF-8 with corrupted bytes. lxml's XML parser then fails (XMLSyntaxError) and the HTML-parser fallback silently mis-decodes the chars.

**Fix**: In `_extract_chunks_from_item`, replaced `item.get_content()` with `item.content` (raw bytes as stored in the ZIP). `etree.fromstring(bytes)` honours the `<?xml ... encoding='iso-8859-1'?>` declaration directly. No change to Chunk shape, meta structure, or writer/e2e contract.

**TDD**: New test `test_non_utf8_chapter_encoding_produces_correct_accented_chars` in `tests/integration/test_epub_reader.py`. Fixture EPUB built as raw ZIP (ebooklib always writes UTF-8) with ISO-8859-1 chapter containing "Café mañana corazón". Asserts: no U+FFFD, correct é/ñ/ó present. RED: demonstrated by inspecting `get_content()` which returns bytes labeled utf-8 but containing Latin-1 byte values — lxml XML parser rejects them as XMLSyntaxError; HTML fallback happened to succeed on this lxml version. GREEN: fix bypasses `get_content()` entirely; lxml XML parser reads raw bytes with correct declaration.

**Suite**: 272 passed, 6 skipped (up from 271/6). All 4 EPUB/prose files (24 tests) green. ruff clean. Domain purity green.

---

### M4-6 [x] — Real token-usage cost accounting (debt #289 / S-2)

**Depends on**: M4-3, M4-4 (OpenAICompatibleProvider + OllamaProvider complete)
**Spec**: cost-control/cost-tracked-per-chunk; model-provider/translate-returns-TranslationResult
**par**

**Context**: Before M4-6, `job.cost_usd` was accumulated using `CostEstimator._project_chunk_cost()`
(flat word-count heuristic × 150 assumed output tokens). Real token counts from the provider were
discarded. Three bugs resulted:

- **BUG-0**: cost accumulation used estimates, not real usage.
- **BUG-1**: reflective mode charged only 1 pass of real tokens; critique+revise prompts (larger
  than draft) were undercharged.
- **BUG-2**: failed chunks accrued ZERO cost because `_actual_chunk_cost` was only called in the
  DONE branch, never for FAILED/PAUSED chunks.

**Protocol change** (M4-6 core):
- `TranslationProvider.translate()` return type changed from `TranslationUnit` to `TranslationResult`.
- New `Usage` model: `{input_tokens: int = 0, output_tokens: int = 0}`.
- New `TranslationResult` model: `{unit: TranslationUnit, usage: Usage}`.
- Both models added to `borgesica/domain/models.py`.
- Port updated in `borgesica/domain/ports.py`.

**Adapter changes**:
- `AnthropicProvider.translate()`: returns `TranslationResult`; extracts real usage from
  `response.usage.input_tokens / output_tokens`.
- `OpenAICompatibleProvider.translate()`: returns `TranslationResult`; extracts real usage from
  `response.usage.prompt_tokens / completion_tokens`.
- `OllamaProvider`: thin subclass — inherits fix from `OpenAICompatibleProvider`.
- `glossary.py` (LlmGlossaryExtractor): caller updated to `.unit.glossary_additions`.
- `quality.py` (QualityHarness): caller updated to `.unit.translation` and `.unit` for
  TranslationUnit-as-carrier pattern in judge + back-translate calls.

**Orchestrator changes** (`borgesica/domain/orchestrator.py`):
- Per-call real cost accrued into `running_cost` immediately after every `translate()` call
  (draft, critique, revise, tag-retry, fallback).
- `_usage_cost(usage, in_price, out_price)` static helper converts raw token counts to USD.
- Failed chunks: cost is accrued BEFORE the failure is recorded — tokens were consumed.
- `job.cost_usd` updated to `running_cost` on every persisted state transition.

**Test changes** (migration to new return type):
- `tests/fakes.py` (`FakeTranslationProvider.translate()`): returns `TranslationResult` with
  deterministic usage (`count_tokens(system+" "+user)` for input, `count_tokens(translation)` for
  output).
- `tests/unit/test_orchestrator.py`: all inline provider subclasses updated to return
  `TranslationResult`. Three new regression-guard tests added:
  - `test_fast_mode_cost_is_real_usage_not_estimate` (BUG-0)
  - `test_reflective_mode_cost_reflects_three_calls_per_chunk` (BUG-1)
  - `test_failed_chunk_cost_is_nonzero` (BUG-2)
- `tests/unit/test_quality.py`: tests use `FakeTranslationProvider` (now returns
  `TranslationResult`); no assertion changes needed (quality tests assert on `QualityScore`, not
  on the raw provider return).
- `tests/unit/test_anthropic_provider.py`: assert `isinstance(result, TranslationResult)`;
  access `.unit.translation` etc.
- `tests/unit/test_openai_compatible_provider.py`: same migration as Anthropic tests.
- `tests/integration/test_engine_e2e.py` (`LongFakeProvider`): returns `TranslationResult`.
- `tests/integration/test_epub_engine_e2e.py` (`EpubTagFakeProvider`): returns `TranslationResult`.
- `tests/integration/test_openai_compatible_provider_live.py`: migrated to `TranslationResult`.
- `tests/integration/test_ollama_provider_live.py`: migrated to `TranslationResult`.

**Deliverable**: Full suite green: 280 passed, 6 skipped (up from 272/6 before M4-6; the 8 new
tests: 3 orchestrator regression guards + 5 provider/e2e migration fixes). ruff exits 0. Domain
purity green. No remaining `.translation` access on a raw `translate()` result (grep-confirmed).

---

### M4-7 [x] — Harden fallback reinsert to word boundaries (debt #277)

**Depends on**: M2-0 (markup fallback path)
**Spec**: subtitle-translation/inline-tags-in-text (fallback placement)
**par**

**Context**: `markup.reinsert` (fallback-only since M2-0, used when the tags-in-text primary path
fails for weak/local models) placed tags by proportional CHARACTER position, which could wedge a
tag inside a translated word (e.g. `El v<i>eloz`).

**Fix** (`borgesica/domain/markup.py`):
- After computing the proportional target position, snap it to the nearest WORD boundary via
  `_snap_to_word_boundary` (`_is_word_boundary` helper). A tag never splits a word.
- Tie-break by tag kind: opening tags prefer the start of the next word; closing tags prefer the
  end of the preceding word.
- Tag count and relative order preserved (stable sort + right-to-left insertion unchanged).

**TDD** (`tests/unit/test_markup.py`): `test_reinsert_snaps_opening_tag_to_word_boundary` and
`test_reinsert_never_splits_a_word_multi_tag` — both confirmed RED (proportional split words like
`El zo<b>rro`) then GREEN after the snap. Existing reinsert round-trip/count tests unchanged.

**Note**: This is a positional heuristic (not token-alignment), but it no longer fractures a word.
Deeper token-alignment is out of scope — the primary tags-in-text path handles capable models.

---

## Cross-Cutting: Open Items Resolution

These items are assigned to specific tasks above but explicitly documented here for traceability:

| Open Item | Resolution | Task |
|-----------|------------|------|
| `prose_chunk_tokens` vs `chunk_size` for prose | **Distinct field** `prose_chunk_tokens: int = 800` added to `JobConfig` | M2-2 |
| EPUB writer atomic write on Windows | Write to `out_path + ".tmp"` in SAME directory, then `os.replace()` | M2-3 |
| Resume from `CANCELLED` state | **Accepted** — `resume_job` accepts `{CREATED, PAUSED, CANCELLED}`; DONE chunks skipped | M1-8 (test 8), M1-12 (test 10) |
| Prompt cache-write cost in estimate | **Not included** — cache-write cost is one-time amortized; estimate stays conservative | M1-6 |
| `quality_mode=reflective` in cost estimation | **3 passes counted** per PENDING chunk (translate + critique + revise) | M1-6 |

---

## Dependency Graph (summary)

```
M0-1 → M0-2 → M0-3
             ↓
           M1-1 → M1-2 ──────────────────────────────────────────────────→ [domain purity test]
           M1-1 → M1-3 (markup)
           M1-1 → M1-4 (SRT chunker, requires M1-3)
           M1-1 → M1-5 (ContextManager, requires M1-2)
                    ↓
           ┌────────┴─────────┬─────────────────────┐
          M1-6            M1-7               M1-9 M1-10 M1-11
       (CostEst)       (Glossary)         (SrtAdapters) (SQLite) (Anthropic)
           └────────┬─────────┘
                   M1-8 (Orchestrator, requires M1-5 M1-6 M1-7)
                    └────┬──────────────────────────────────────┘
                        M1-12 (TranslatorEngine, requires M1-8 M1-9 M1-10 M1-11)
                         ↓
                        M1-13 (CLI)
                         ↓
               ┌─────────┴──────────┐
              M2-1              M2-2 (prose chunker; adds prose_chunk_tokens to JobConfig)
              (EpubReader)          ↓
               │               M2-2R (prose chunker provenance rework)
               └───────────────────M2-3 (EpubWriter — depends on M2-1 + M2-2R)
                                    ↓
                                   M3-1 (PDF)
                                    ↓
                          M4-1 (Fixtures) → M4-2 (Judge)          [par]
                          M1-11 (Anthropic) → M4-3 (OpenAICompatibleProvider) → M4-4 (Ollama)
                                                                                  [par]
                          M4-5 (EPUB non-UTF-8 encoding, depends M2-1)           [par]
```

---

## Review Workload Forecast

**M1 is the first implementation slice** (the full SRT walking skeleton: M1-1 through M1-13).

| Metric | Estimate |
|--------|----------|
| Domain modules (models, errors, ports, markup, chunking, context, glossary, orchestrator, cost) | ~900–1,100 lines |
| Adapters (SrtReader, SrtWriter, AnthropicProvider, SqliteCheckpoint) | ~600–750 lines |
| Public API (api.py + CLI) | ~250–350 lines |
| Tests for M1 (unit + integration) | ~800–1,000 lines |
| Fixtures and fakes | ~100–150 lines |
| **M1 total estimated changed lines** | **~2,650–3,350 lines** |

**Exceeds 400-line review budget**: Yes, significantly (6–8× over).

**Chained/stacked PRs recommended**: Yes.

**Decision needed before apply**: Yes.

### Suggested PR slices for M1

| PR | Tasks | Approx lines | Description |
|----|-------|-------------|-------------|
| PR-1 | M0 + M1-1 + M1-2 | ~350 | Scaffolding, models, ports, domain purity test |
| PR-2 | M1-3 + M1-4 | ~300 | Markup + SRT chunker |
| PR-3 | M1-5 + M1-6 + M1-7 | ~500 | ContextManager, CostEstimator, GlossaryExtractor |
| PR-4 | M1-8 | ~600 | TranslationOrchestrator (densest logic) |
| PR-5 | M1-9 + M1-10 + M1-11 | ~700 | Adapters: SrtReader/Writer, SQLite, Anthropic |
| PR-6 | M1-12 + M1-13 | ~500 | TranslatorEngine public API + CLI + MODELS.md |

Each PR is independently reviewable and green-bar testable. M2–M4 follow as separate PRs after M1 is merged.
