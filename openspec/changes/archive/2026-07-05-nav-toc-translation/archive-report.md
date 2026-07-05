# Archive Report: nav-toc-translation

**Archived**: 2026-07-05
**Change**: `nav-toc-translation`
**Artifact store**: openspec
**Final suite**: 380 passed, 6 skipped (0 unexpected failures)

---

## Milestone Summary

| Work Unit | Description | Verdict |
|-----------|-------------|---------|
| WU-1 | Fixture helper: `book.toc`-populating infrastructure for nav/ncx testing | PASS |
| WU-2 | Reader: skip-gate fix (isinstance-only) + nav walk (epub:type-based, <a>/<span>/heading emission) | PASS |
| WU-3 | Chunker: lift `kind="nav-label"` to batch-level meta when all nodes agree | PASS |
| WU-4 | Orchestrator: reflective-mode bypass for nav-label chunks (always single-pass) | PASS |
| WU-5 | Writer v1: nav xhtml patch regression guard (href/epub:type preservation verified) | PASS |
| WU-6 | Writer v2: ncx-copy lookup + patch branch (nav_href→label, fragment normalization) | PASS |
| WU-7 | Writer v2: defensive fallback paths (unmatched navPoint, empty lookup, no-nav-doc) | PASS |
| WU-8 | E2E integration + full regression (fast mode, reflective mode, multi-nav-sibling stability) | PASS |

All work units WU-1 through WU-8: **implemented and verified**. Verify verdict: **PASS WITH WARNINGS (0 CRITICAL, 2 WARNING, 2 SUGGESTION)**.

Commits reviewed: `7cbc7fd`, `c7edb38`, `e58162b`, `3a5bcf3`, `856592a`, `a16060e`, `0fc5841`, `e059b05`, `6c9227b` (diff `master..HEAD`).

---

## Verification Verdict — PASS WITH WARNINGS (2 WARNING, 2 SUGGESTION, 0 CRITICAL)

Full verify report persisted at Engram topic `sdd/nav-toc-translation/verify-report` (observation #355). Summary:

- Test evidence (actually executed): `.venv/Scripts/python -m pytest` → **380 passed, 6 skipped**. `ruff check borgesica/` → all checks passed. Confirmed commit-by-commit diff against tasks.md's Work-Unit-to-Commit map.
- Spec-to-test compliance: all 21 spec scenarios (16 book-translation + 5 translation-quality) have a directly corresponding, passing test function.
- CRITICAL findings: **none**. Nav chunks flow through the pipeline; href normalization correct by construction; `kind` lifting works; reflective bypass confirmed; ncx-copy lookup and patching verified; FAILED-chunk fallback leaves both nav doc and ncx with source-language labels (safe, no crash); defensive paths confirmed (unmatched navPoint, empty lookup, no-nav-doc).

### WARNING findings

**WARNING-1 — `nav_label_lookup` basename collision across different subdirectories has no dedicated test.**
Two navPoints/hrefs sharing a basename in different dirs (e.g. `images/cover.xhtml` and `content/cover.xhtml`) would collide in the lookup (last-write-wins), a documented acceptable degradation per design D4. The collision scenario itself is not spec-required; a defensive-only safeguard. Worth a follow-up test if real-world EPUBs exhibit this.

**WARNING-2 — `_normalize_ncx_href` does not URL-decode or case-fold hrefs before matching.**
A real-world EPUB with percent-encoded hrefs (`%20` for spaces) or case-differing hrefs between nav.xhtml and toc.ncx (e.g. `Ch1.xhtml` vs `ch1.xhtml`) would silently leave that navPoint untranslated. Safe (no crash, never corrupts an href), but a missed translation. Not a spec violation (spec only requires fragment-stripping) but worth tracking as a follow-up enhancement.

### SUGGESTION findings

**SUGGESTION-1** — Test names vs assertions alignment: `test_epub_engine_e2e_no_nav_doc_byte_identical_to_before_change` implies byte-diff comparison, but assertions check chunk-count and translated-text substrings, not actual byte diffing. Naming is slightly aspirational; harmless but could mislead maintainers.

**SUGGESTION-2** — URL-encoding/case-folding gaps (WARNING-2) are cheap to address later if they become real bug reports — not urgent today.

---

## Spec Deltas Merged Into Canonical Specs

| Domain | Action | Details |
|--------|--------|---------|
| book-translation | ADDED "EPUB reader emits nav-doc labels as translatable chunks with provenance" | Requirement + 4 scenarios: nav <a> labels, nav heading (nav_href=None), landmarks vs. page-list, isolated chapter_index bucket |
| book-translation | ADDED "EPUB writer copies translated nav labels into toc.ncx by href match, at zero provider cost" | Requirement + 5 scenarios: ncx navLabel replaced by href match, fragment-bearing href normalization, unmatched navPoint fallback, no-ncx no-op, ncx-only-book (no nav) no-op |
| book-translation | MODIFIED "EPUB reader extracts text nodes, preserving structure metadata" | Added detail: EpubNav handled by dedicated nav walk, isinstance-only skip-gate (no filename substring pre-check), 2 new scenarios (nav doc named without "nav" substring, book without nav doc regression guard) |
| book-translation | MODIFIED "EPUB writer reinserts translated text using prose_nodes provenance" | Added nav-label patching detail (href/attrib preservation, multi-nav-sibling node_path stability), 2 new scenarios (nav <a> href preserved byte-for-byte, sibling nav locate by epub:type) |
| book-translation | MODIFIED "EPUB writer produces a valid, openable EPUB" | Added nav structure preservation (ol/li nesting, epub:type attributes), 1 new scenario (nav nesting/epub:type survive patching) |
| translation-quality | MODIFIED "quality_mode controls how many model passes run per chunk" | Added nav-label exception: nav-label chunks always single-pass regardless of quality_mode, 1 new scenario (nav-label chunk bypasses reflective mode in reflective job) |
| translation-quality | MODIFIED "reflection is orchestrator-level and provider-agnostic" | Added clarification: orchestrator reads chunk.meta["kind"] for per-chunk bypass, 1 new scenario (reflective mode works with mixed body + nav-label job) |

Canonical specs updated:
- `openspec/specs/book-translation/spec.md` (expanded from ~265 to ~450 lines)
- `openspec/specs/translation-quality/spec.md` (expanded from ~78 to ~135 lines)

Both merges preserved all pre-existing requirements not touched by this change (DRM rejection, PDF reader, encoding, calque golden sample, philosophy — book-translation; system prompt, calque failure case — translation-quality).

---

## Archive Contents

- `proposal.md` ✅ (full content, 146 lines)
- `design.md` ✅ (full content, 228 lines)
- `tasks.md` ✅ (full content, 503 lines — WU1-1 through WU8-2 all `[x]`, 11/11 units complete)
- `specs/book-translation/spec.md` ✅ (delta, ~185 lines)
- `specs/translation-quality/spec.md` ✅ (delta, ~65 lines)
- `state.yaml` ✅ (metadata snapshot)
- `archive-report.md` ✅ (this file)

All phase artifacts present and accounted for.

---

## Archive Structure

Change folder contents copied byte-identical from:
- `openspec/changes/nav-toc-translation/`

To:
- `openspec/changes/archive/2026-07-05-nav-toc-translation/`

**Process**: All files from the active change folder (proposal, design, tasks, specs/, state.yaml) have been copied into the archive folder, organized in the same structure. The original active folder will be removed after git finalization.

---

## Deferred Items (tracked for follow-up)

| ID | Description | Severity | Action |
|----|--------------|----------|--------|
| `backlog/nav-label-basename-collision-test` | `nav_label_lookup` basename collision across subdirectories (WARNING-1) has no dedicated test. If real-world EPUBs exhibit this, add a test exercising the last-write-wins degradation. | Enhancement | Post-archive backlog ticket |
| `backlog/nav-href-url-decode-case-fold` | `_normalize_ncx_href` does not URL-decode or case-fold hrefs (WARNING-2). A real-world EPUB with percent-encoded or case-differing hrefs would silently miss translations. Low risk (no crash), but worth a follow-up if real-world bug reports surface. | Enhancement | Post-archive backlog ticket |
| `backlog/test-naming-alignment` | `test_epub_engine_e2e_no_nav_doc_byte_identical_to_before_change` name implies byte-diff but uses chunk-count + substring assertions (SUGGESTION-1). Rename or expand assertions for clarity. | Documentation | Post-archive backlog ticket |

All items are recorded for cross-session tracking. See verify-report observation #355 for full WARNING/SUGGESTION detail.

---

## SDD Cycle Closed

The `nav-toc-translation` change is fully planned, implemented, verified, and archived.

**Ready for the next change.**

Commit digest (9 implementation commits):
- `7cbc7fd` — test(fixtures): add book.toc-populating helper for nav/ncx integration fixtures (WU1-1)
- `c7edb38` — fix(reader): recognize EpubNav via isinstance alone — filename substring gate leaked untranslated nav docs (WU2-1)
- `e58162b` — feat(reader): emit nav-doc <a>/<span>/heading chunks via epub:type walk (toc + landmarks, page-list excluded) (WU2-2)
- `3a5bcf3` — feat(chunking): lift kind="nav-label" to batch-level meta when all nodes agree (WU3-1)
- `856592a` — feat(orchestrator): nav-label chunks always single-pass — reflective mode bypassed by meta[kind] (WU4-1)
- `a16060e` — test(writer): regression-guard nav xhtml patch — href/attrs preserved, epub:type-based nav located correctly (WU5-1)
- `0fc5841` — feat(writer): build nav_href->translated_label lookup before ZIP loop + .ncx patch branch + defensive paths (WU6-1, WU6-2, WU7-1 combined)
- `e059b05` — tasks.md marker: WU7-1 complete (bookkeeping only)
- `6c9227b` — test(e2e): nav doc + ncx translated end to end, fast and reflective modes (WU8-1, WU8-2 combined)
