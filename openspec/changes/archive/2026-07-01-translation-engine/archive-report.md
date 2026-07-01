# Archive Report: translation-engine

**Archived**: 2026-07-01  
**Change**: `translation-engine`  
**Artifact store**: openspec  
**Final suite**: 282 passed, 6 skipped (6 env-gated; 0 unexpected failures)

---

## Milestone Summary

| Milestone | Description | Verdict |
|-----------|-------------|---------|
| M0 | Scaffolding (pyproject, skeleton, test doubles) | done |
| M1 | SRT walking skeleton — full engine core | PASS WITH WARNINGS (0 CRITICAL) |
| M2 | EPUB support (EpubReader, prose chunker, EpubWriter) | PASS WITH WARNINGS (0 CRITICAL) |
| M3 | PDF support (PdfPlumberReader, cleanup pipeline) | PASS WITH WARNINGS (0 CRITICAL) |
| M4 | Quality harness + provider breadth + encoding + cost + markup | PASS WITH WARNINGS (0 CRITICAL) |

All milestones M0–M4: **implemented and verified**. Zero CRITICAL findings across all verify reports.

---

## Resolved Debts

These items were deferred in earlier milestones and resolved before archive:

| Item | Resolution | Commit |
|------|------------|--------|
| S-M2-2: non-UTF-8 EPUB chapter encoding | RESOLVED in M4-5: EpubReader uses `item.content` raw bytes + `etree.fromstring` to honor declared encoding | 1474732 |
| #289: cost-accuracy (reflective×retry + failed-chunk + real usage) | RESOLVED in M4-6: `TranslationProvider.translate()` returns `TranslationResult{unit, usage}`; orchestrator accrues real per-call cost for ALL call types | (M4-6) |
| #277: markup.reinsert fallback placement splits words | RESOLVED in M4-7: `_snap_to_word_boundary` added to `markup.reinsert`; opening tags snap to next word start, closing tags to preceding word end | 0b9d912 |

---

## Carried-Open Post-Archive Tickets

These items are intentionally left open as post-archive follow-up work:

| ID | Description | Severity | Action |
|----|-------------|----------|--------|
| W-M2-3 | `chapter_index` increments for empty spine items, producing gaps in the sequence. Defensible behavior (tracks spine position not chunk count), non-issue in practice. | WARNING | Document non-issue; leave as-is |
| W-M3-3 | `pytest.ini` declares `integration` and `golden` markers but there is no `conftest.py` skip hook. All 53+ integration tests run in every invocation (not gated by `INTEGRATION=1`). | WARNING | Post-archive test-infra ticket |
| W-M3-4 | Hyphen-rejoin regex `(\w)-\n(\w)` cannot distinguish PDF artifact hyphens from legitimate compound hyphens (e.g. `well-known` → `wellknown`). Requires dictionary-backed disambiguation. | WARNING | Post-archive text-cleanup hardening |
| W-M4-1 | Fallback `translate()` raises `MalformedOutput`/`ProviderError`: the FALLBACK call's cost is not accrued (exception caught before cost line). Narrow path (requires 3 primary failures AND the fallback adapter call itself raises). Nil practical impact; primary path costs are always preserved. | WARNING | Post-archive minor fix in orchestrator |
| S-M4-1 | `advisory_gate` boundary test: no explicit test checking score=3 fails and score=4 passes. Values 3 and 5 are implicitly covered; boundary not explicitly documented. | SUGGESTION | Post-archive test-quality nit |
| S-M4-2 | `test_good_translation_passes_advisory_gate` does not hard-assert `result.passed == True` (intentional advisory-only design). | SUGGESTION | Post-archive test-quality nit |
| CLI | `borgesica/__main__.py` is SRT-only. EPUB/PDF work correctly via the Python API (`TranslatorEngine`) but the CLI's argument parser and DI wiring do not expose `--source-type epub` / `--source-type pdf`. | TODO | Post-archive CLI wiring ticket |

---

## Canonical Specs Written

The following canonical specs were created in `openspec/specs/` from the change's delta specs:

| Capability | Canonical Path |
|------------|----------------|
| subtitle-translation | openspec/specs/subtitle-translation/spec.md |
| book-translation | openspec/specs/book-translation/spec.md |
| context-continuity | openspec/specs/context-continuity/spec.md |
| job-lifecycle | openspec/specs/job-lifecycle/spec.md |
| model-provider | openspec/specs/model-provider/spec.md |
| quality-evaluation | openspec/specs/quality-evaluation/spec.md |
| cost-control | openspec/specs/cost-control/spec.md |
| translation-quality | openspec/specs/translation-quality/spec.md |

All 8 delta specs were first-ever specs for their domains (no prior `openspec/specs/` directory existed). Each delta was copied as the canonical spec with change-delta framing headers removed.

Notable merges:
- `book-translation/spec.md` incorporates M3 PDF reader deviation note (W-M3-1: `layout=True` spec literal corrected to default `extract_text()`) and M4-5 EPUB encoding requirement.
- `model-provider/spec.md` incorporates M4-6 `TranslationResult`/`Usage` protocol change and M4 OpenAICompatibleProvider + OllamaProvider requirements.
- `cost-control/spec.md` incorporates M4-6 real-token-usage cost tracking requirement (supersedes pre-M4-6 heuristic behavior).
- `subtitle-translation/spec.md` incorporates M4-7 word-boundary snapping requirement for fallback reinsert.

---

## Archive Structure

Change folder moved from:
- `openspec/changes/translation-engine/`

To:
- `openspec/changes/archive/2026-07-01-translation-engine/`

Contents preserved:
- `state.yaml` — updated: phase=archive, status=archived, archived_at=2026-07-01
- `specs/` — 8 delta specs (historical; canonical versions in openspec/specs/)
- `tasks.md` — summary reference (full content in git history)
- `archive-report.md` — this file

Note on large files: `proposal.md`, `design.md`, and `verify-report.md` (the full multi-milestone report) are preserved in git history at their original path `openspec/changes/translation-engine/`. The orchestrator should use `git mv` to stage the rename so git tracks the full history. The canonical content was not modified.

---

## SDD Cycle Closed

The `translation-engine` change is fully planned, implemented, verified, and archived.  
**Ready for the next change.**
