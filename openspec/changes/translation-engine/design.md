# Design: Translation Engine (English SRT + EPUB + PDF → Neutral Spanish)

Change: `translation-engine` · Phase: design · Status: draft · Artifact store: openspec

---

## Technical Approach

A clean/hexagonal engine. A pure **domain** (chunking, context management, orchestration, cost) defines Pydantic v2 contracts and `Protocol` ports. **Adapters** (readers/writers, providers, checkpoint store) implement those ports at the edges. A single **public API** (`TranslatorEngine`) wires them via constructor DI. The dependency rule is absolute: imports point INWARD — adapters import domain, domain imports nothing external (only stdlib + pydantic). This makes every domain decision unit-testable with a `FakeTranslationProvider`, no mocking framework, no network.

---

## 1. Architecture Overview

Dependency rule: `api → adapters → domain`. Domain never imports adapters or provider SDKs. Pydantic v2 is the one allowed third-party dependency in the domain (it is our contract language, not an adapter).

```
borgesica/
  domain/
    models.py            # Pydantic v2 entities + enums (the contract)
    ports.py             # Protocols: DocumentReader, DocumentWriter,
                         #            TranslationProvider, CheckpointStore
    chunking.py          # Chunker: SRT cue-batch + prose paragraph strategies
    context.py           # ContextManager: glossary+summary injection, budget
    glossary.py          # GlossaryExtractor Protocol + LLM/Spacy strategies
    orchestrator.py      # TranslationOrchestrator: sequential loop (+ optional reflection pass)
    cost.py              # CostEstimator: token math → CostEstimate
    markup.py            # strip/reinsert inline tags + validate count
    errors.py            # domain exceptions (BudgetExceeded, MalformedOutput…)
  adapters/
    readers/  srt_reader.py  epub_reader.py  pdf_plumber_reader.py
              pdf_pymupdf_reader.py   # AGPL, opt-in, not imported by default
    writers/  srt_writer.py  epub_writer.py  pdf_writer.py
    providers/ anthropic_provider.py  ollama_provider.py(M4)  litellm_provider.py(M4)
    checkpoints/ sqlite_checkpoint.py
    extraction/ spacy_extractor.py  llm_extractor.py
  api.py                 # TranslatorEngine: the public surface
tests/ unit/  integration/  golden/
```

The domain `orchestrator` knows ports, not implementations. The `api` layer is the only place concrete adapters are named.

---

## 2. Domain Model (Pydantic v2)

```python
class JobStatus(StrEnum):  CREATED  ESTIMATING  RUNNING  PAUSED  DONE  FAILED  CANCELLED
class ChunkStatus(StrEnum): PENDING  TRANSLATING  DONE  FAILED
class SourceType(StrEnum):  SRT  EPUB  PDF

class GlossaryEntry(BaseModel):
    term: str; translation: str; locked: bool = False; note: str | None = None

class Glossary(BaseModel):
    entries: list[GlossaryEntry] = []
    def render(self, budget_tokens: int = 300) -> str: ...   # compact table for prompt

class RollingSummary(BaseModel):
    text: str = ""; chunk_index: int = -1   # last chunk that produced it

class Chunk(BaseModel):
    index: int; source_text: str; status: ChunkStatus = ChunkStatus.PENDING
    translated_text: str | None = None
    meta: dict = {}     # adapter round-trip data: cue ids/timestamps, epub node path

# The structured LLM result — the per-chunk contract the provider MUST fulfill.
class TranslationUnit(BaseModel):
    translation: str
    summary_update: str                    # 3–5 sentences, REPLACES prior summary
    glossary_additions: list[GlossaryEntry] = []   # optional terms discovered mid-run

class CostEstimate(BaseModel):
    input_tokens: int; output_tokens: int; usd: float
    model: str; cached: bool = False; within_budget: bool = True

class JobConfig(BaseModel):
    source_type: SourceType; model: str
    target_lang: str = "es-neutral"
    budget_usd: float | None = None
    chunk_size: int = 25          # SRT cues / prose target tokens handled by chunker
    line_length: int = 42         # SRT reflow
    glossary_strategy: Literal["llm","spacy","hybrid","none"] = "llm"
    quality_mode: Literal["fast","reflective"] = "fast"  # reflective = translate→critique→revise (~2x cost/time)

class Job(BaseModel):
    id: str; config: JobConfig; source_path: str
    status: JobStatus = JobStatus.CREATED
    total_chunks: int = 0; completed_chunks: int = 0; cost_usd: float = 0.0
    created_at: datetime; updated_at: datetime
```

---

## 3. Ports (Protocols)

```python
class DocumentReader(Protocol):
    def read(self, path: str, config: JobConfig) -> list[Chunk]: ...
    """Parse source into ordered Chunks; meta carries round-trip data. No I/O beyond the file."""

class DocumentWriter(Protocol):
    def write(self, chunks: list[Chunk], src_path: str, out_path: str) -> None: ...
    """Reassemble translated chunks into the SAME format faithfully (EPUB→EPUB round-trip)."""

class TranslationProvider(Protocol):
    def translate(self, system: str, user: str, model: str) -> TranslationUnit: ...
    """Return a VALID TranslationUnit. Adapter owns the structured-output mechanism
    and degrades gracefully (tool-call → constrained → prompt+parse+retry)."""
    def count_tokens(self, text: str, model: str) -> int: ...
    def price(self, model: str) -> tuple[float, float]: ...   # (in_usd_per_mtok, out_usd_per_mtok)

class CheckpointStore(Protocol):
    def save_job(self, job: Job) -> None: ...
    def load_job(self, job_id: str) -> Job | None: ...
    def save_chunk(self, job_id: str, chunk: Chunk) -> None: ...   # idempotent upsert
    def load_chunks(self, job_id: str) -> list[Chunk]: ...
    def save_glossary(self, job_id: str, g: Glossary) -> None: ...
    def load_glossary(self, job_id: str) -> Glossary: ...
    def save_summary(self, job_id: str, s: RollingSummary) -> None: ...
    def load_summary(self, job_id: str) -> RollingSummary: ...
```

Provider returns `TranslationUnit` (not a raw string): structured-output handling stays inside the adapter, domain stays library-free.

---

## 4. Public API — `TranslatorEngine`

```python
class TranslatorEngine:
    def __init__(self, *, provider: TranslationProvider, checkpoint: CheckpointStore,
                 readers: dict[SourceType, DocumentReader],
                 writers: dict[SourceType, DocumentWriter],
                 extractor: GlossaryExtractor | None = None): ...

    def create_job(self, source_path: str, config: JobConfig) -> Job: ...
    def estimate_cost(self, job_id: str) -> CostEstimate: ...
    def run_job(self, job_id: str, on_progress: ProgressCallback | None = None) -> Job: ...
    def resume_job(self, job_id: str, on_progress: ProgressCallback | None = None) -> Job: ...
    def status(self, job_id: str) -> Job: ...
    def get_glossary(self, job_id: str) -> Glossary: ...
    def update_glossary(self, job_id: str, entries: list[GlossaryEntry]) -> Glossary: ...
    def cancel_job(self, job_id: str) -> None: ...   # cooperative flag checked per chunk

ProgressCallback = Callable[[Progress], None]
class Progress(BaseModel):
    job_id: str; chunk_index: int; total_chunks: int
    cost_usd: float; status: JobStatus
```

`create_job` reads+chunks the source, seeds the glossary, persists everything (`status=CREATED`) so the user can review the glossary BEFORE spending. `run_job`/`resume_job` return the final `Job`. Progress is pushed via callback — the future UI never polls or reaches into internals.

---

## 5. Persistence / Checkpointing (SQLite, stdlib)

```sql
jobs(id PK, source_type, source_path, target_lang, model, status,
     budget_usd, total_chunks, completed_chunks, cost_usd, created_at, updated_at)
chunks(job_id, chunk_index, source_text, translated_text, status, meta_json,
       PRIMARY KEY(job_id, chunk_index))            -- idempotency key
glossary(job_id, term, translation, locked, note, PRIMARY KEY(job_id, term))
summaries(job_id, chunk_index, text, PRIMARY KEY(job_id, chunk_index))
```

`save_chunk` is an `INSERT … ON CONFLICT(job_id,chunk_index) DO UPDATE` inside a transaction → crash-safe, idempotent. **Resume** = `load_job` + `load_chunks`; skip every chunk with `status=DONE` (no API call, no charge); rebuild the rolling summary from the highest-index DONE summary row; continue from the first non-DONE chunk. Single-user, one job at a time — documented; no cross-process locking needed.

---

## 6. Translation Flow (one job, SEQUENTIAL by design)

```
create_job:  read(path) → Chunk[]  →  glossary seed (extractor)  →  persist all (CREATED)
                                          │
            [user reviews/edits glossary via update_glossary]  ← OPTIONAL gate
                                          │
run_job (per chunk N, in order):
  load summary(N-1) ──┐
  load glossary  ─────┼─→ ContextManager builds system prompt:
                      │     [CACHEABLE static instructions + TRANSLATION PHILOSOPHY:
                      │      meaning+image over words, no literal calques] + [glossary.render ≤300t]
                      │     + [rolling summary ≤200t]
  markup.strip(chunk.source_text) → user prompt
  provider.translate(system,user,model) → TranslationUnit   (retry/degrade inside adapter)
  IF config.quality_mode == "reflective":                   ← OPTIONAL quality pass (~2x cost)
     provider.translate(CRITIQUE prompt, draft) → critique  (calques? unnatural? wrong image?)
     provider.translate(REVISE prompt, draft+critique) → TranslationUnit (revised: faithful+natural)
  validate: Pydantic parse + markup tag-count match  ── mismatch → retry (≤2), else FAILED
  markup.reinsert(tags) ; (SRT) reflow to line_length
  checkpoint.save_chunk(DONE)  →  summary = unit.summary_update ; save_summary(N)
  job.cost += chunk cost ; budget check (hard stop if exceeded)
  on_progress(Progress(...))
after last chunk:  writer.write(chunks, src, out)  →  status=DONE
```

Each step is a domain function; only `provider.translate`, `reader.read`, `writer.write`, and checkpoint calls cross a port.

---

## Architecture Decisions

### Decision 1 — Two-phase: up-front glossary seed, then SEQUENTIAL translation

| Option | Tradeoff | Verdict |
|---|---|---|
| Pure sequential | Max coherence, lowest throughput | Translation loop = THIS |
| Full parallel | Fast, but rolling summary of chunk N needs N-1 → coherence collapses | Rejected |
| Windowed parallel | Marginal speedup, breaks summary causality, complex resume | Rejected for v1 |
| Two-phase (parallel-capable seed → sequential translate) | Glossary seeding is order-independent and parallelizable; translation stays sequential | **Chosen** |

**Rationale**: The rolling summary makes chunk N causally depend on N-1 — that dependency IS the coherence mechanism, so the translation loop must be sequential. The glossary is built once up front and is order-independent, so seeding (and cost estimation, and pre-flight token counts) can fan out without touching coherence. **Core engine is SYNCHRONOUS.** Reasons: (a) sequential translation has no concurrency to exploit per job; (b) sync code is trivially unit-testable with a fake provider and zero event-loop machinery; (c) the real latency win is the **Batch API** (50% off, async-at-the-provider) which the adapter can expose later behind the same sync port. Throughput vs coherence tradeoff: we deliberately trade single-job speed for global consistency — the product's entire reason to exist.

### Decision 2 — Structured output is an ADAPTER concern; `instructor` is NOT a core dependency

| Option | Tradeoff | Verdict |
|---|---|---|
| `instructor` in domain | Couples domain to a structured-output lib + provider quirks | Rejected — violates dependency rule |
| Domain owns Pydantic contract; each adapter picks its mechanism | Domain stays pure; adapters degrade per model capability | **Chosen** |

**Rationale**: The domain owns `TranslationUnit` (the Pydantic contract). Each adapter fulfills it with the strongest mechanism its model supports and degrades gracefully: **Anthropic** → native tool-calling / structured output (may use `instructor` internally as an adapter-only dep); **Ollama (M4)** → constrained decoding / JSON mode; **floor for any weak model** → prompt-and-parse-with-retry. `instructor`, if used, is imported ONLY inside `adapters/providers/anthropic_provider.py`. Domain never sees it. This directly satisfies the locked "degrade gracefully on imperfect structured output" constraint.

### Decision 3 — Glossary extraction: pluggable, LLM default, SpaCy/hybrid opt-in

| Option | Tradeoff | Verdict |
|---|---|---|
| LLM-only | Zero install friction, high accuracy on invented terms, non-deterministic, small cost | **Default** |
| SpaCy NER + LLM validate (hybrid) | Deterministic seed, best accuracy; forces `en_core_web_sm` model download | Opt-in |
| SpaCy-only | Free/deterministic; misses fiction/invented terms | Available |

**Rationale**: For an "easy to install, run by anyone" open-source tool, forcing a SpaCy model download in the default path is real friction. Make `GlossaryExtractor` a Protocol with three swappable strategies selected by `JobConfig.glossary_strategy`; default `"llm"`. Non-determinism is fully mitigated by the locked design: the seeded glossary is **persisted immediately and user-editable before any translation spend** — the human locks the terms, not the model. Users who want determinism opt into `"hybrid"`/`"spacy"` and accept the download.

### Decision 4 — PDF: pdfplumber (MIT) default; PyMuPDF4LLM (AGPL) strictly opt-in

| Option | Tradeoff | Verdict |
|---|---|---|
| pdfplumber default | MIT (zero license friction), good with `layout=True`, slower | **Default** |
| PyMuPDF4LLM | 8–12× faster, great layout, **AGPL-3.0 viral** | Opt-in, labeled |

**Rationale**: License hygiene for an open-source tool that proprietary users may integrate. `pdf_plumber_reader.py` is the only PDF reader imported by default. `pdf_pymupdf_reader.py` lives behind the same `DocumentReader` port, is **never imported by `api.py` by default**, and carries a clear AGPL header + docs warning. The hexagonal port makes the swap a one-line DI change. (PDF is M3 — designed now, built later.)

**Deviation (M3-FIX / W-M3-1)**: The table above says `layout=True` and the spec said the same. The implementation uses `page.extract_text()` with no arguments (pdfplumber default mode). `layout=True` produces whitespace-padded output where text positions are approximated spatially — this makes verbatim line-frequency boilerplate counting unreliable because padded lines do not match their unpadded counterparts across pages. Default extraction returns clean newline-separated text in reading order, which is exactly what the header/footer detection and hyphen-rejoin pipeline need. The deviation is technically correct and is the accepted implementation; the spec literal and this table should be read as "pdfplumber default extraction" going forward.

### Decision 5 — Translation quality: always-on philosophy prompt + optional reflection pass

| Option | Tradeoff | Verdict |
|---|---|---|
| Single literal translate | Cheapest/fastest; prone to confusing word-for-word calques | Floor (`quality_mode="fast"`) |
| Always reflect | Best quality; ~2x cost+time on every job | Rejected as default |
| Philosophy prompt always + optional reflection | Free quality lift for all; pay 2x only when opted in | **Chosen** |

**Rationale**: A real failure shared by Sonnet, Haiku, and Google Translate — EN "a slot for a locking wooden beam across the door" → "una ranura para una viga de madera con cerradura" (nonsense: the beam doesn't HAVE a lock, it IS the lock; correct: "tranca de madera que cierra la puerta"). Two architecture-neutral levers: (1) the static instruction block ALWAYS carries a translation philosophy — translate MEANING and the physical IMAGE, never literal calques, prioritize naturalness while staying faithful (free; lifts every job's floor); (2) `quality_mode="reflective"` adds an orchestrator-level **translate → critique → revise** loop (extra provider calls; the `TranslationProvider` port is unchanged), ~2x cost/time, off by default, worth it for literary work. The M4 eval harness lets a user measure whether reflective actually helps THEIR content before paying 2x.

---

## 7. Error Handling & Resilience

| Failure | Handling |
|---|---|
| Transient API / 5xx | Adapter retries with exponential backoff + jitter (≤3) |
| Rate limit (429) | Honor `Retry-After`; backoff; surface as recoverable, never crash the job |
| Malformed / non-JSON output | Adapter degrades (tool→constrained→prompt+parse); re-prompt ≤2; else raise `MalformedOutput` → chunk `FAILED`, job `PAUSED`, resumable |
| Inline-tag count mismatch | `markup` validation fails → retry the chunk ≤2; persist `FAILED` if still wrong |
| Budget cap exceeded | Hard stop BEFORE the offending call; job `PAUSED`; raise `BudgetExceeded`; completed chunks safe |
| Crash mid-job | Resume from last DONE chunk; idempotent `(job_id,chunk_index)`; no double charge |
| Cancel | Cooperative flag checked per chunk; finishes current chunk's checkpoint, stops, `CANCELLED` |

Partial failure never loses paid work — every DONE chunk is committed before the next call.

---

## 8. Testing Strategy (Strict TDD, pytest)

| Layer | What | Approach |
|---|---|---|
| Unit (domain) | chunker, context budgeting, markup strip/reinsert, cost math, orchestrator loop, resume logic, budget stop | `FakeTranslationProvider` (hand-written, returns canned `TranslationUnit`) + `InMemoryCheckpointStore`. NO mocking framework. NO network. Domain is 100% pure-unit. |
| Integration (adapters) | srt/epub readers+writers round-trip, sqlite checkpoint idempotency, anthropic adapter shape | Real fixtures: tiny `.srt`/`.epub` files, SQLite `:memory:`. EPUB round-trip asserts output opens + chapters/tags preserved. Provider integration gated behind an API-key env (skipped in CI by default). |
| Golden / Eval | translation quality | Tier1: curated (source, golden-es) pairs, LLM-as-judge (not exact match). Tier2: judge scores accuracy/fluency/neutral-register/glossary-consistency (advisory). Tier3 back-translation optional. |

**Fakes vs fixtures**: provider and clock get **fakes** (deterministic, in-domain-test). Readers/writers/checkpoint get **real fixtures** (they ARE the I/O under test). The `FakeTranslationProvider` is the keystone — it lets the entire orchestration (sequential loop, summary threading, retry, budget, resume) be tested with zero LLM calls.

---

## 9. Stack (pinned)

| Concern | Choice | Why |
|---|---|---|
| Language | Python **3.11+** | `StrEnum`, modern typing, mature ecosystem |
| Domain contract | **pydantic v2** | Structured-output contract + validation; only 3rd-party dep allowed in domain |
| SRT | **srt** (MIT) | ~30% faster than pysrt, active, preserves inline tags |
| EPUB r/w | **ebooklib** (M2) | Read+write EPUB, enables clean round-trip |
| PDF default | **pdfplumber** (MIT, M3) | License-clean, `layout=True` reading order |
| PDF opt-in | **pymupdf4llm** (AGPL, M3) | Fast/accurate; viral license → opt-in only |
| Provider (M1) | **anthropic** SDK | First concrete `TranslationProvider`; tool-calling structured output |
| Structured output | **instructor** — adapter-internal ONLY | Convenience inside Anthropic adapter; never in domain |
| Provider (M4) | **ollama**, optional **litellm** | Local/offline + multi-provider behind same Protocol |
| Checkpoint | **sqlite3** (stdlib) | Zero-dep, ACID, crash-safe, idempotent |
| Glossary | **LLM default**; **spacy** opt-in | Low install friction default; deterministic opt-in |
| Tests | **pytest** | Strict TDD; fakes for provider, fixtures for I/O |

---

## Open Questions

- [ ] Prompt-caching boundary: confirm the static-instruction block is large enough to make Anthropic prompt caching worth the cache-write cost at typical book sizes.
- [ ] Glossary mid-run growth: `TranslationUnit.glossary_additions` lets the model surface new terms during a run — decide in spec whether these auto-apply or require user re-confirmation (default: stage as unlocked, don't override locked entries).
- [ ] Very long single paragraph (prose) exceeding chunk budget: confirm sentence-level fallback split rule in the chunker (M2 detail).

---

## EPUB prose provenance & reinsertion (M2-2R)

Decision #291 — resolves composition gap discovered in post-M2 review (gap #290).

### Problem

M2-1 EpubReader produced one Chunk per text node (with `node_path` in meta) and M2-2 chunk_prose took `list[list[str]]` — discarding all provenance. A translated prose chunk could not be mapped back to its source XHTML node, blocking EpubWriter (M2-3).

### Decided Flow (mirrors SRT reader → chunker → writer)

```
EpubReader
  └─ per-node Chunk:
       source_text = raw node text (inline tags preserved)
       meta = {
           "epub_item_href": str,   # spine XHTML file
           "node_path": str,        # XPath-like positional path within <body>
           "chapter_index": int,    # 0-based spine position — enforces chapter boundaries
       }
         │
         ▼
chunk_prose(node_chunks: list[Chunk], config, provider) -> list[Chunk]
  - Groups nodes by chapter_index; NEVER batches across chapters.
  - Greedy accumulation within budget (prose_chunk_tokens, default 800).
  - Over-budget single node → sentence split; over-budget sentence → hard-split
    with exactly ONE WARNING per oversized sentence.
  - Skips empty/whitespace nodes.
  - Output Chunk:
       source_text = node texts joined with "\n\n"
       meta["prose_nodes"] = [{"epub_item_href": str, "node_path": str}, ...]
                               one entry per "\n\n"-separated segment (in order)
       meta["hard_split"] = True  (only on hard-split chunks)
         │
         ▼
EpubWriter (M2-3)
  - For each output chunk, split translated_text on "\n\n".
  - Map segment i → meta["prose_nodes"][i]["node_path"].
  - Write each segment back to its node in the source XHTML.
  - Segments sharing a node_path are concatenated into that node.
```

### Why prose_nodes mirrors cue_batches

`SrtChunker` stores `meta["cue_batches"]` — one entry per SRT cue in the batch — so `SrtWriter` can split the translated text on `"\n\n"` and restore each cue. `chunk_prose` stores `meta["prose_nodes"]` for the same reason: the writer splits on `"\n\n"` and restores each XHTML text node. Same pattern, same guarantee.

### Domain purity preserved

`chunk_prose` is pure domain: no ebooklib imports. `chapter_index` is an integer stored in `Chunk.meta` by the adapter (EpubReader); the chunker reads it to enforce chapter boundaries without knowing anything about spine structure.
