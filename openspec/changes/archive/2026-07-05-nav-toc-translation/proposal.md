# Proposal: Nav/TOC Translation (the reading-app menu must speak the target language)

Change: `nav-toc-translation`
Phase: proposal · Status: draft · Artifact store: openspec

---

## 1. Problem

A book can be fully translated on every page and STILL feel half-finished, because the reading app's own navigation menu — the table of contents the reader taps to jump between chapters — stays in the source language.

Today `EpubReader.read` (`borgesica/adapters/readers/epub_reader.py:308-314`) deliberately skips the EPUB3 navigation document (`<nav epub:type="toc">`), so its `<a>` labels ("Chapter 1", "Foreword", ...) are never emitted as chunks, never translated, and pass through verbatim. The legacy `toc.ncx` (EPUB2 compat menu) is skipped for the same reason. Result: a Spanish translation of an English book shows an English chapter menu in Calibre, Apple Books, and most e-readers.

Why now: EPUB is the primary, shipped book format and the engine is otherwise complete. Nav labels are a tiny amount of text (~200-400 tokens per book) but they are the FIRST thing a reader sees and the surface they navigate the whole book from. A 100% target-language reading experience is not achievable while the menu is untranslated.

Success looks like: after translating an EPUB, both the EPUB3 nav document and the `toc.ncx` show chapter labels in the target language; hrefs and anchors are byte-untouched so the EPUB stays valid and every menu link still jumps to the right place; books that have no nav document are not regressed.

---

## 2. Goals / Non-goals

### Goals

- Translate the EPUB3 nav document's reading-visible labels (`<a>` link text and the nav's `<h1>/<h2>` section heading) into the target language.
- Keep `toc.ncx` labels consistent with the nav document at ZERO extra provider cost, by copying already-translated nav labels into the ncx by href match at write time.
- Preserve EPUB validity absolutely: `href`, `id`, anchors, and element attributes are never modified — only text content between tags.
- Keep nav labels reasonably consistent with translated body headings via glossary + rolling summary, by translating them late and in their own batch.
- Fix the latent nav-skip bug in the reader in passing (filename-substring gate).
- Zero false regressions on books WITHOUT a nav document.

### Non-goals

- True title-reuse (resolving each nav href to the target chapter's translated `<h1>` and reusing that exact text as the label). Deferred — see Decision 7. Glossary/summary context gives approximate consistency now.
- Independent model translation of `toc.ncx` when a nav document IS present (would double cost and reintroduce nav-vs-ncx divergence). The ncx is populated by copy, not by a second model call.
- Page-list (`epub3_pages`) nav translation — page numbers, not prose.
- Any chunk-kind branching in the orchestrator beyond what the meta contract below requires.
- Checkpoint schema changes.

---

## 3. Scope — one change, two work phases

This is ONE change delivered as two sequential work phases. v2 depends on v1's data contract.

| Phase | Deliverable | Provider cost |
|-------|-------------|---------------|
| **v1** | Translate EPUB3 nav-doc labels (`<a>` + nav `<h1>/<h2>`) via the existing reader → chunker → writer pipeline. | ~200-400 tokens/book |
| **v2** | Copy the v1-translated labels into `toc.ncx` by href match at write time. Requires the `nav_href` meta contract from v1. | $0 (no model call) |

v1 is independently shippable (nav.xhtml translated, ncx still source-language). v2 completes the picture. Both land under this one change.

---

## 4. Approach (per affected file)

### 4.1 Reader — `borgesica/adapters/readers/epub_reader.py`

- **Fix the skip gate.** Replace the `if item_name and "nav" in item_name.lower(): if isinstance(item, epub.EpubNav): continue` (lines 308-314) with a check on `isinstance(item, epub.EpubNav)` ALONE. Drop the filename substring pre-check — a nav document named `contents.xhtml` (no "nav" substring) currently escapes the skip and leaks into the chunk stream.
- **Dedicated nav walk (Approach A from exploration).** Instead of `continue`-ing past the `EpubNav` item, run a SEPARATE narrow walk scoped to that item. Locate the toc nav via `epub:type="toc"` (XPath `//nav[@*='toc']`, matching ebooklib's own `_parse_nav`) rather than assuming it is the first/only `<nav>`. Emit:
  - one Chunk per `<a>` descendant, `node_path` pointing directly at the `<a>` element (not its `<li>` parent — avoids ambiguity if `<li>` has nested markup);
  - one Chunk for the nav's `<h1>/<h2>` section heading (Decision 4);
  - for un-linked heading rows (`<li><span>` with no `<a>`), a Chunk on the `<span>`.
- **Meta on every nav-label node** (Decision 5 contract): `{"epub_item_href": <nav doc href>, "node_path": <path to a/span>, "chapter_index": <isolated bucket>, "nav_href": <the a's href attr value, or null for headings>}`. `nav_href` is captured at READ time from the `<a href>` attribute and is used ONLY by the v2 ncx-copy step — never sent to the model, never used for `.xhtml` patch routing.
- **Isolated chapter_index.** Assign nav-label nodes a dedicated bucket (e.g. `max(chapter_index)+1`) so `chunk_prose`'s `groupby` batches them together and they never concatenate with a real chapter's prose.

### 4.2 Chunking — `borgesica/domain/chunking.py`

No code change. `chunk_prose` is format-agnostic and passes all meta keys (including the new `nav_href`) through into `prose_nodes` verbatim. The isolated `chapter_index` naturally lands nav labels in their own batch via the existing `groupby`.

### 4.3 Orchestrator — `borgesica/domain/orchestrator.py`

No branching by chunk kind. Nav-label chunks (single short `<a>` text) pass the existing prose guard (they have alphabetic characters) and flow through translate-with-retry like any chunk. Batching them last (isolated `chapter_index` bucket, appended after body chapters) means they see the fullest glossary/summary state. Reflective quality mode does NOT apply — see Decision 2.

### 4.4 Writer, v1 XHTML patch — `borgesica/adapters/writers/epub_writer.py`

No change for v1. `_patch_entry` already matches the `.xhtml` suffix; `_find_node` resolves arbitrary tag names positionally; `_set_element_content` replaces only element content and NEVER touches `element.attrib`, so patching an `<a href="ch1.xhtml">Label</a>` node preserves `href`. The extra `nav_href` meta key is inert to this path (it reads only `epub_item_href`/`node_path`).

### 4.5 Writer, v2 ncx-copy branch — `borgesica/adapters/writers/epub_writer.py`

A new write-time step, running INSIDE `_do_write` AFTER nav.xhtml patch text is finalized:

1. Build a `nav_href → translated_label` lookup from the nav-label `prose_nodes` (using the `nav_href` meta key + the node's translated text).
2. Add a new `.ncx` patcher branch to `_patch_entry` (media type / `.ncx` extension). It parses the ncx XML, walks `navMap` recursively (nested `navPoint`s), and for each `<navPoint>` resolves its `<content src="...">` href, normalizes it the same way (`zip_path.normpath`/`join`), matches against the lookup, and replaces the sibling `<navLabel><text>` content with the translated label.
3. **Href/fragment matching:** normalize away fragment identifiers when matching (`ch1.xhtml#sec2` in ncx matches `ch1.xhtml` in nav). A `navPoint` whose href finds no nav label is left untranslated — a safe degraded fallback, same posture as `_find_node` returning None.

### 4.6 Meta contract summary

| Key | Set by | Consumed by | Purpose |
|-----|--------|-------------|---------|
| `epub_item_href` | reader (existing) | writer xhtml patch | locate the zip entry |
| `node_path` | reader (existing) | writer xhtml patch | locate the element |
| `chapter_index` | reader (isolated bucket for nav) | chunker groupby | isolate nav batch |
| `nav_href` | reader (NEW, nav labels only) | writer ncx-copy (v2 only) | href match into ncx |

---

## 5. Decisions (the open questions, resolved)

| # | Question | Decision | Rationale |
|---|----------|----------|-----------|
| 1 | Opt-in flag or always-on? | **Always-on** when an `EpubNav`/`toc.ncx` is present. No new JobConfig flag. | Cost is ~200-400 tokens/book (negligible) and an untranslated menu is a defect, not a feature. Opt-in would leave the default experience broken. |
| 2 | Do nav labels use reflective/quality mode when the job is configured that way? | **No — always single-pass** for nav-label chunks. | Labels are 1-3 word factual strings; critique/revise (3x cost) yields no quality gain. This needs a lightweight "is-nav-label" signal downstream (the isolated bucket / a meta marker) so the orchestrator can bypass reflective mode for these chunks only. |
| 3 | ncx-only book (no nav doc) fallback? | **Out of scope for this change (v3).** Detect absence of an `EpubNav` item and, for now, leave the ncx untranslated (documented, no crash). | v2's whole value is zero-cost copy FROM the nav doc; with no nav doc there is nothing to copy. Direct model translation of the ncx is a separate, cost-bearing path — defer to keep this change's contract clean. No regression: such books behave exactly as today. |
| 4 | Translate the nav's own `<h1>/<h2>` section heading ("Contents")? | **Yes.** Include it as a target in the nav walk. | It is reader-visible text inside the toc nav; leaving "Contents" in English breaks the 100%-target-language goal. Its meta carries `nav_href: null` (no ncx counterpart). |
| 5 | Exact `nav_href` meta contract shape? | **`nav_href: str \| None`** on nav-label `prose_nodes` entries only — the raw `<a href>` attribute value captured at read time; `None` for heading nodes. Used ONLY by the v2 ncx-copy step. | Orthogonal to the existing `epub_item_href`/`node_path` contract; extra key is inert to the xhtml patch path (verified — `_do_write` reads only known keys). Locking the name now lets spec/design freeze the contract before tasks. |
| 6 | Page-list and landmarks nav scope? | **Landmarks: yes** (reader-visible labels like "Cover", "Table of Contents"). **Page-list: no** (page numbers, not prose). | Landmarks are menu-visible navigational text; page-list entries are numeric locators with no translatable prose. Scope the walk to `epub:type` in {`toc`, `landmarks`}, exclude `page-list`. |
| 7 | True title-reuse (nav label = translated body `<h1>`)? | **Deferred (non-goal).** Rely on glossary + rolling summary for approximate consistency. | Title-reuse needs id-aware provenance (anchor → heading resolution), inline-tag stripping, and multi-chunk-heading handling — high effort/fragility for a marginal win over glossary/summary. Flag for a future enhancement. |

---

## 6. Risks & mitigations

| # | Risk | Mitigation |
|---|------|------------|
| 1 | `_node_path` sibling indices shift if landmarks/page-list `<nav>` siblings exist under `<body>`. | Locate the target nav by `epub:type` XPath, not by position. Read-time and write-time walk the SAME original source bytes, so indices stay consistent by construction. |
| 2 | Nav labels get concatenated with real chapter prose in one provider call, hurting quality/glossary framing. | Isolated `chapter_index` bucket forces `groupby` to batch nav labels separately. |
| 3 | v2 ordering: ncx-copy runs before nav patch text is finalized. | Explicit ordering invariant — the ncx-copy step runs AFTER nav.xhtml patch text is computed within the same `_do_write` call. Design phase must encode this. |
| 4 | ncx `navPoint` href has no matching nav label (richer ncx hierarchy). | Leave that navPoint's label untranslated (defensive fallback, no correctness regression — an English label in an otherwise-translated ncx). |
| 5 | Latent leak: a nav doc named without "nav" leaks into the chunk stream today. | Fixed here — skip gate keys on `isinstance(item, epub.EpubNav)` alone. |
| 6 | href/fragment mismatch (`ch1.xhtml#sec2` vs `ch1.xhtml`). | Normalize away fragments before matching. |
| 7 | Silent regression on books without a nav doc. | ncx-only books explicitly out of scope (Decision 3); reader emits no nav chunks, writer's ncx-copy no-ops with an empty lookup. |

---

## 7. Testing strategy sketch (strict TDD — RED first)

Existing EPUB fixtures never set `book.toc`, so ebooklib generates nav.xhtml/toc.ncx with EMPTY `<ol>`/`<navMap>`. New tests MUST populate `book.toc = [...]` (list of `epub.Link`/tuples) to exercise real label content. `test_epub_reader.py:520-574` has a hand-built ncx `navPoint`/`navLabel` template to reuse for v2 fixtures.

RED-first test targets:

- **Reader (v1):** given a fixture with `book.toc` populated, `read` emits nav-label chunks with correct `node_path`, isolated `chapter_index`, and `nav_href` meta; the nav `<h1>/<h2>` heading is emitted; landmarks emitted, page-list NOT emitted.
- **Reader skip-gate fix:** a nav document named `contents.xhtml` is still recognized as `EpubNav` and does not leak into the regular chunk stream.
- **Writer (v1):** patching an `<a href="ch1.xhtml">` node replaces text and preserves `href` byte-for-byte; output EPUB remains valid.
- **Writer (v2):** given translated nav labels, `toc.ncx` `<navLabel><text>` is replaced by href match; hrefs untouched; fragment-bearing ncx href (`ch1.xhtml#sec2`) matches nav's `ch1.xhtml`; an unmatched navPoint is left untranslated.
- **No-nav-doc book:** ncx-only fixture translates body, leaves ncx untranslated, no crash (Decision 3 regression guard).
- **Orchestrator:** nav-label chunk bypasses reflective mode even when the job is reflective (Decision 2).

---

## 8. Next phase

`sdd-spec` and `sdd-design` may run in parallel:

- `sdd-spec` — author deltas to the reader/book-translation spec: nav-label emission, the `nav_href` meta contract, always-on behavior, and the ncx-copy write step. New scenarios per Decision.
- `sdd-design` — encode the `_do_write` ordering invariant (nav patch before ncx-copy), the `.ncx` patcher branch structure, and the nav-walk `epub:type` scoping. Recommended because v2 introduces a real ordering dependency and a new writer branch.
