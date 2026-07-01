# Proposal: Translation Engine (English SRT + EPUB + PDF → Neutral Spanish)

Change: `translation-engine`
Phase: proposal · Status: draft · Artifact store: openspec

---

## 1. Vision / Problem

People who want to read English books or watch English video in Spanish are stuck choosing between two bad options: machine translation that is fast but flat, inconsistent, and tone-deaf to context; or human translation that is excellent but slow and expensive. LLMs can close that gap — but only if you solve the problem that breaks naive LLM translation of LONG texts.

That problem is **"lost in the middle"**: you cannot feed a 400-page book or a full season of subtitles to a model in one shot. You must chunk it. And the moment you chunk it, continuity collapses — a character's name gets translated three different ways, the register drifts from formal to casual between chapters, an invented term from chapter 2 is forgotten by chapter 30. The translation is locally fine and globally incoherent.

This engine solves that with three coordinated mechanisms:

- **Chunking that respects natural units** — SRT cues stay atomic (never split a subtitle); book text splits at paragraph/chapter boundaries. Each chunk is small enough to translate well.
- **A glossary** — fixed, consistent translations for proper nouns and recurring-ambiguous terms, injected into every chunk so the same name is always rendered the same way.
- **A rolling summary** — a compact running record of tone, register, and plot, injected into every chunk so the model keeps a consistent voice across the whole work.

The output target is deliberately **neutral Spanish** — understandable across regions, free of localisms that would alienate part of the audience.

Two strategic commitments shape everything:

- **Engine first, UI later.** We build a clean, well-tested core engine with a stable public API (`TranslatorEngine`). A UI/CLI plugs into that API later. This is non-negotiable: the engine must be usable and valuable on its own, and the UI must never leak into the domain.
- **Open-source, model-agnostic, contributor-friendly.** The tool is meant to be downloaded and run by anyone. It does not lock you into one vendor or one model: you choose your provider and model (frontier API, cheap API, or fully local/offline), guided by tiered recommendations. A quality-evaluation harness lets you MEASURE quality on your own content rather than trust marketing claims — that is what makes free model choice safe.

Success looks like: a user points the engine at an English `.srt`, `.epub`, or `.pdf`, picks a model, sees a cost estimate, runs the job, and gets coherent neutral-Spanish output — and if the machine crashes at page 300 of 400, resuming costs nothing already paid for. For a book, an EPUB in becomes a valid EPUB out, ready to Send to Kindle.

---

## 2. Scope

### In scope (this change — the ENGINE)

- **SRT translation** — parse subtitles, batch cues, translate to neutral Spanish, preserve timing/indices, preserve inline formatting tags, reflow to the line-length limit.
- **EPUB book translation** — parse the EPUB's structured XHTML, translate text nodes in place, and write back a valid EPUB that preserves chapters, inline formatting, and images (a clean round-trip). This is the PRIMARY book format: it is structured text, far simpler than PDF, and round-trips directly to e-readers (e.g. Send to Kindle). Targets standard, non-DRM EPUB.
- **PDF book translation** — extract text from digital PDFs, clean it (headers/footers, hyphenation), chunk by paragraph/chapter, translate. The harder book format; tackled after EPUB.
- **Glossary** — build candidate terms, let the user review/edit before translation, inject into every chunk for consistency.
- **Rolling summary** — maintain and inject tone/plot continuity across chunks.
- **Resumability** — SQLite checkpointing, idempotent per `(job_id, chunk_index)`, so long jobs survive crashes and resume without re-paying.
- **Model/provider agnostic** — pluggable `TranslationProvider` Protocol; user chooses the model, guided by tiered recommendations; adapters degrade gracefully on imperfect structured output from weaker/local models.
- **Cost estimation** — pre-flight cost estimate before a job runs, with a configurable budget cap.
- **Quality-evaluation harness** — golden samples + LLM-as-judge so users can measure translation quality on their own content.
- **Document I/O ports** — a `DocumentReader` port (parse input) and a `DocumentWriter` port (emit output). Output format matters: structured formats (SRT, EPUB) are written back faithfully in their original format; the writer is what makes the EPUB→EPUB round-trip clean.
- **Public API** — a clean `TranslatorEngine` surface (create / estimate / run / resume / status / glossary / cancel) that a future UI consumes.

### Out of scope (for now)

- **Any UI** — no GUI, no web app. A thin testing CLI may exist to exercise the engine, but the real UI is a later, separate phase.
- **Languages other than English → neutral Spanish.** The architecture should not actively forbid other pairs, but no other pair is built, tested, or supported in this change.
- **Document formats other than SRT, EPUB, and PDF** (no DOCX, VTT, ASS) in this change.
- **DRM-protected e-books** (Amazon AZW3/KFX, Adobe-DRM EPUB). Out of scope, technically AND legally — we target standard, non-DRM EPUB only.
- **Scanned-PDF OCR** as a default path. The architecture leaves room for an OCR adapter later, but the default targets clean digital PDFs.

---

## 3. Consumers

The engine has exactly two consumers worth naming now. Full personas belong to the later UI phase — here we only need enough to keep the API honest.

- **The future UI / CLI (the API caller).** A program that creates jobs, shows a cost estimate, kicks off a run, displays live progress, and lets a person review the glossary. It needs a stable, side-effect-clear public API and a progress callback so it never has to poll or reach into the engine's internals.

- **The end beneficiary (Spanish-speaking reader/viewer).** The person who actually consumes the output — reads the translated book or watches the subtitled video. They never touch the engine directly; they care that the Spanish is coherent, consistent, and natural, and that subtitles stay on-screen the right length of time.

---

## 4. Use cases (high level)

1. **Translate a subtitle file** — user supplies an English `.srt`; engine returns a neutral-Spanish `.srt` with timing, indices, and inline tags intact, lines within length limits.
2. **Translate an EPUB book** — user supplies a non-DRM English `.epub`; engine returns a valid neutral-Spanish `.epub` with chapters, inline formatting, and images intact, ready for e-readers (Send to Kindle).
3. **Translate a PDF book** — user supplies an English `.pdf`; engine returns coherent neutral-Spanish prose with consistent terminology and register across the whole book.
4. **Resume an interrupted job** — a long job crashes or is cancelled; user resumes by `job_id` and the engine continues from the last completed chunk without re-translating or re-charging finished work.
5. **Choose and configure the model + provider** — user selects a model (frontier API / cheap API / local-offline), guided by tiered recommendations, and the engine runs against it through the provider adapter.
6. **Review and edit the glossary before translating** — engine proposes candidate terms; user inspects, corrects, and locks the glossary; translation then uses the approved version.
7. **Estimate cost up front** — before running, the user gets a cost/token estimate and can set a budget cap that hard-stops a job that would exceed it.

---

## 5. Risks & mitigations

| # | Risk | Mitigation |
|---|------|------------|
| 1 | **PDF extraction is the beast.** Multi-column layouts, headers/footers, hyphenation, embedded images, and scanned pages defeat naive extraction and corrupt the input before translation even starts. | Hexagonal `DocumentReader` port with a sane MIT default (pdfplumber); a post-extraction cleanup pipeline (strip repeated headers/footers, rejoin hyphenated breaks, detect chapter boundaries); optional heavier adapters (PyMuPDF4LLM, Marker/OCR) behind the same port. Ship clear docs on supported PDF shapes. EPUB is offered as the easier, higher-fidelity alternative for books that have one. |
| 2 | **Spanish text expansion breaks the 42-char SRT line limit.** Translated Spanish runs ~15–20% longer than English and overflows the per-line subtitle constraint. | Post-translation reflow step with a tunable line-length limit; allow a 3-line cue as a graceful fallback when 2 lines cannot fit. |
| 3 | **Inline markup corruption (SRT tags / EPUB inline HTML).** Models silently drop or mangle `<i>`, `<b>`, `<u>` tags during translation. | Strip tags before the LLM call, translate plain text, reinsert tags by position; validate tag count on reassembly and retry on mismatch. |
| 4 | **Model quality variance below Sonnet** — cheaper/local models drift in literary nuance and register, more on books than on subtitles, and the drift compounds over length. | The glossary + rolling-summary architecture injects the context weaker models lack; the quality-eval harness lets users measure on their own content and decide; tiered recommendations steer high-stakes literary work toward stronger models. |
| 5 | **Structured-output reliability on weak/local models.** Models that follow strict-format instructions poorly return malformed/non-JSON responses, breaking the translation+summary contract. | Pydantic models as the domain contract; provider adapters fulfill it with the strongest mechanism the model supports (tool calling / native structured output → constrained decoding → prompt-and-parse-with-retry as the floor) and degrade gracefully instead of crashing. |
| 6 | **Cost runaway on long books.** A 400-page book across ~100 chunks can surprise the user with the bill. | Pre-flight `estimate_cost` before any spend; configurable budget cap that hard-stops; prompt caching of the static system prompt to cut input cost; batch-API path where latency allows. |
| 7 | **Glossary extraction non-determinism.** LLM-assisted term extraction is non-deterministic and may seed inconsistent glossaries run to run. | Persist the extracted glossary to the checkpoint immediately and make it user-editable BEFORE translation starts, so the human, not the model, locks the final terms. |
| 8 | **EPUB round-trip integrity.** Translating text nodes inside the EPUB's XHTML can corrupt structure, inline markup, or internal references — a broken EPUB won't open on an e-reader. | Use `ebooklib` to parse and write; translate text nodes ONLY, never structural markup; preserve inline tags with the same strip/reinsert+validate discipline as SRT; validate that the output EPUB opens after writing. |

---

## 6. Roadmap

Each milestone is independently usable and testable. Strict TDD applies throughout: domain logic is unit-tested with the LLM call faked out; adapters are integration-tested against fixtures.

### M1 — SRT engine core (the walking skeleton)
The smallest end-to-end vertical slice that proves the architecture.
- Domain: chunker (cue-batch), context manager (glossary + rolling summary), translation orchestrator, cost estimator.
- Ports + first adapters: `SrtReader`/`SrtWriter`, `AnthropicAdapter` (one concrete provider), `SQLiteCheckpoint`.
- Public API: `TranslatorEngine` (create / estimate / run / resume / status / glossary).
- A thin CLI to exercise the engine end to end (testing aid, not the product UI).
- **Done when:** an English `.srt` becomes a coherent, consistent, resumable neutral-Spanish `.srt`.

### M2 — EPUB book support (the easy book format first)
- `EpubReader` + `EpubWriter` adapters behind the `DocumentReader` / `DocumentWriter` ports (via `ebooklib`).
- Paragraph/chapter chunking for prose; glossary review flow surfaced for long-form content.
- Clean round-trip: the translated EPUB preserves chapters, inline formatting, and images.
- **Done when:** a non-DRM English `.epub` becomes a valid neutral-Spanish `.epub`, resumable mid-book, ready for Send to Kindle.

### M3 — PDF book support (the hard book format)
- `PdfPlumberReader` default adapter behind the `DocumentReader` port + the post-extraction cleanup pipeline.
- Paragraph/chapter chunking, reusing the prose flow proven in M2.
- **Done when:** a digital English `.pdf` book becomes coherent neutral-Spanish prose, resumable mid-book.

### M4 — Quality harness + provider breadth
- Quality-eval harness: golden fixtures (Tier 1), LLM-as-judge integration scoring (Tier 2), optional back-translation regression check (Tier 3).
- Additional provider adapters behind the existing Protocol: an optional multi-provider adapter (e.g. LiteLLM) and a **local/Ollama** adapter for private/offline/zero-cost translation, with graceful structured-output degradation.
- **Done when:** a user can run on a cheap or local model AND measure whether the quality is good enough on their own content.

### Later — UI shell (OUT of engine scope)
A separate phase builds the GUI/CLI product on top of the stable public API. Not part of this change; named here only so the engine API is designed to welcome it (progress callback, clear job lifecycle, no hidden state).
