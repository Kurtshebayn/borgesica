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

### M1-5 [T] — ContextManager (system prompt assembly)

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

### M1-6 [T] — CostEstimator (par after M1-5)

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

### M1-7 [T] — GlossaryExtractor (LLM default) (par after M1-5)

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

### M1-8 [T] — TranslationOrchestrator (core loop) (par after M1-5)

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

### M1-9 [T] — SrtReader + SrtWriter adapters (par after M1-5)

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

### M1-10 [T] — SQLiteCheckpointStore adapter (par after M1-5)

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

### M1-11 [T] — AnthropicProvider adapter (par after M1-5)

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

### M1-12 [T] — TranslatorEngine public API + DI wiring (seq — requires M1-8 through M1-11)

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

### M1-13 [I] — Thin CLI (borgesica command)

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

## M2 — EPUB Support

M2 tasks may begin as soon as M1-12 is green. M2-1 and M2-2 are parallel; M2-3 requires both.

---

### M2-1 [T] — EpubReader adapter

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

### M2-2 [T] — Prose chunker

**Depends on**: M1-4  
**Spec**: book-translation/prose-chunking  
**par**

Tests (`tests/unit/test_prose_chunker.py`):
1. Paragraph of 400 tokens, budget 800 → single chunk, unsplit.
2. Paragraph with 3 sentences (1,200 tokens total), budget 800 → split at sentence boundaries, each chunk ≤ 800 tokens.
3. Single sentence of 1,500 tokens → hard-split at 800-token boundary + WARNING logged (check `caplog`).
4. Paragraphs from different chapters never merged into one chunk.
5. `prose_chunk_tokens: int = 800` is a DISTINCT field on `JobConfig` (not reusing `chunk_size`). Add this field to `JobConfig` in `models.py` now; default `800`. `chunk_size` remains the SRT cue-batch control. Update `JobConfig` tests in M1-1 test file.

Implement `chunk_prose(paragraphs: list[str], config: JobConfig, provider: TranslationProvider) -> list[Chunk]` in `borgesica/domain/chunking.py`.

Deliverable: `pytest tests/unit/test_prose_chunker.py` → all pass.

---

### M2-3 [T] — EpubWriter adapter (seq — requires M2-1 + M2-2)

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

## M3 — PDF Support (coarser-grained; design is done)

M3 tasks depend on M2 being complete (prose chunker and EpubWriter patterns are established).

### M3-1 [T] — PdfPlumberReader adapter

**Depends on**: M2-3  
**Spec**: book-translation/PDF-reader  

Tests (`tests/integration/test_pdf_reader.py`):
1. Header/footer stripping: string appearing on ≥ 80% of pages removed from output.
2. Hyphenated line-break rejoined (`incompre-\nhensible` → `incomprehensible`).
3. Default DI config: `pdf_pymupdf_reader.py` NOT in `sys.modules` (import purity test).

Implement `borgesica/adapters/readers/pdf_plumber_reader.py` (pdfplumber, MIT).
Create `borgesica/adapters/readers/pdf_pymupdf_reader.py` as a commented stub with AGPL header — not imported by default anywhere.
Create `borgesica/adapters/writers/pdf_writer.py` stub (`write` raises `NotImplementedError("PDF output not supported in M3 — export as text or use source EPUB")`).

---

## M4 — Quality Harness + Provider Breadth (coarser-grained)

M4 depends on M1-12 (engine is complete).

### M4-1 [I] — Golden fixtures + QualityScore model

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

---

### M4-2 [T] — LLM-as-judge harness

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

---

### M4-3 [T] — OllamaProvider adapter

**Depends on**: M1-11  
**Spec**: quality-evaluation/Ollama-adapter  
**par**

Tests:
1. `OllamaProvider` satisfies `TranslationProvider` Protocol.
2. `OllamaProvider` not in `sys.modules` when not explicitly imported (import purity).

Integration test (marked `@pytest.mark.integration`; gated by `OLLAMA_HOST` env):
3. Real Ollama call → valid `TranslationUnit`, no `ValidationError`.

Implement `borgesica/adapters/providers/ollama_provider.py`.

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
               └───────────────────M2-3 (EpubWriter)
                                    ↓
                                   M3-1 (PDF)
                                    ↓
                          M4-1 (Fixtures) → M4-2 (Judge) || M4-3 (Ollama)
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
