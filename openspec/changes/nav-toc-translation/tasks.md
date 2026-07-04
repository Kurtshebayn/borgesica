# Tasks: Nav/TOC Translation (nav-toc-translation)

Change: `nav-toc-translation` · Phase: tasks · Status: draft · Artifact store: openspec
Depends on: `design.md` (Decisions D1-D7, module table), `specs/book-translation/spec.md`,
`specs/translation-quality/spec.md` (~19 Given/When/Then scenarios)

---

## Legend

- **[T]** = write the failing test first, then implement to green (strict TDD, RED before GREEN)
- **[I]** = implement/verify only (no new failing test — regression guard or pure verification)
- **seq** = must follow the previous task/unit in order
- **par** = can run in parallel with other `par` tasks in the same unit
- Spec refs use `capability/requirement-keyword` notation, scenario name in quotes
- Test runner: `.venv/Scripts/python -m pytest`. Confirm the current baseline (all green) before
  starting WU1, and after every unit.
- STRICT TDD MODE IS ACTIVE for every unit below: write the failing test, run it and confirm RED,
  implement the minimal code, run it and confirm GREEN. Do not write implementation before its
  failing test exists and has been observed to fail.

---

## WU-1 — Test fixture helper: populate `book.toc` (foundation, blocks everything)

Every existing EPUB fixture builder in the test suite never sets `book.toc`, so ebooklib emits
empty `<ol>`/`navMap` content — none of U1-U7's tests can assert on real nav labels without this.
This is pure test-infrastructure, lands first, touches no production code.

### WU1-1 [I] — Audit existing fixture builders, add `book.toc`-populating helper [x]

**Depends on**: nothing
**Spec**: enables book-translation/"nav <a> labels emitted as chunks..." and all nav scenarios
(no scenario of its own — infrastructure prerequisite named explicitly in the design's D6 test
strategy: "Existing fixtures never set `book.toc`... New tests MUST populate `book.toc`")
**seq**

1. Locate the current EPUB-fixture-building helper(s) in `tests/integration/test_epub_reader.py`
   and `tests/integration/test_epub_writer.py` (the function(s) that construct an `epub.EpubBook`,
   add chapters/items, and write it to a temp path).
2. Add a reusable helper (or extend the existing one with an optional parameter) that sets
   `book.toc = [epub.Link(href, title, uid), ...]` for the fixture's chapters, so `_get_nav`
   generates non-empty `<ol><li><a>` content and `_get_ncx` generates matching `navPoint` entries.
3. Do NOT change the default behavior of any EXISTING test that doesn't opt into the new
   parameter — this must be additive only (existing tests must stay green untouched).
4. Confirm manually (no assertion needed here, this is scaffolding) that a fixture built with the
   new helper produces a nav doc with real `<a href>` entries and a ncx with matching `navPoint`s
   — this is the precondition every later unit's RED test relies on.

Deliverable: `pytest tests/integration/test_epub_reader.py tests/integration/test_epub_writer.py`
→ unchanged pass count (no new tests yet, no regressions from the helper addition).

**Commit**: `test(fixtures): add book.toc-populating helper for nav/ncx integration fixtures`

---

## WU-2 — Reader: skip-gate fix + nav walk (D1, D2)

Corresponds to design U1 + U2. Two behaviors land together because U2's walk only matters once
U1's gate correctly routes control into it — same call site (`EpubReader.read`, ~line 308-314).

### WU2-1 [T] — Skip-gate fix: `isinstance(item, epub.EpubNav)` alone [x]

**Depends on**: WU1-1
**Spec**: book-translation/"EPUB reader extracts text nodes..." — scenario "nav document named
without 'nav' substring is still recognized and does not leak into body chunks"
**seq**

Test first (`tests/integration/test_epub_reader.py`):
1. Build a fixture whose `EpubNav` item's filename is `contents.xhtml` (no "nav" substring),
   `book.toc` populated (WU1-1 helper) → `EpubReader.read(path, config)` → assert NO chunk in the
   result has `source_text` containing raw nav markup (`<nav`, `epub:type`) and no chunk from the
   general body traversal originates from that item's href.
2. Regression: existing nav-doc-named-`nav.xhtml` case still skipped by the body traversal
   (existing test, if present, must stay green — do not duplicate if already covered).

Implement in `borgesica/adapters/readers/epub_reader.py` (~line 308-314): replace the two-level
`if item_name and ("nav" in item_name.lower()): if isinstance(item, epub.EpubNav): continue` gate
with a single `if isinstance(item, epub.EpubNav):` check. At this stage, keep the body of the
branch as `continue` (the walk itself lands in WU2-2) — this task ONLY fixes the gate so the
`contents.xhtml`-named nav item is reliably identified and excluded from body-chunk emission
regardless of filename.

Deliverable: `pytest tests/integration/test_epub_reader.py -k nav` → new test passes, no regressions.

**Commit**: `fix(reader): recognize EpubNav via isinstance alone — filename substring gate leaked untranslated nav docs`

---

### WU2-2 [T] — Nav walk: emit `<a>`/`<span>`/heading chunks with provenance [x]

**Depends on**: WU2-1
**Spec**: book-translation/"EPUB reader emits nav-doc labels as translatable chunks with
provenance" — scenarios "nav <a> labels emitted as chunks with node_path and nav_href", "nav
heading is emitted and carries nav_href=None", "landmarks are emitted, page-list is excluded",
"nav-label chunks are isolated into their own chapter_index bucket"; also book-translation/"EPUB
reader extracts text nodes..." — scenario "book without a nav document is unaffected"
**seq**

Test first (`tests/integration/test_epub_reader.py`), using the WU1-1 `book.toc`-populated fixture:
1. Fixture with `epub.Link("ch1.xhtml", "Chapter One", "ch1")`, `epub.Link("ch2.xhtml", "Chapter
   Two", "ch2")` in `book.toc` → `read()` emits one `Chunk` per `<a>`, `source_text` = label text,
   `meta["node_path"]` points at the `<a>`, `meta["nav_href"]` = that `<a>`'s `href`.
2. Same fixture, nav doc has an `<h1>`/`<h2>` heading (e.g. "Contents") → a `Chunk` is emitted for
   it with `meta["nav_href"] is None`, `meta["node_path"]` at the heading element.
3. Fixture with a `<nav epub:type="landmarks">` (e.g. "Cover" link) AND a `<nav
   epub:type="page-list">` → landmarks `<a>` chunks emitted; NO chunk emitted for any page-list
   `<a>`.
4. Fixture with 3 body chapters (`chapter_index` 0,1,2) + populated nav → all nav-label chunks
   share `chapter_index == 3` (`max+1`), distinct from every body chapter's index.
5. Regression: fixture with NO `EpubNav` item at all → no nav-label chunks emitted, body-chapter
   extraction unaffected, no exception.
6. Also assert (support for WU3/WU4, same walk): every emitted nav-label chunk's `meta["kind"] ==
   "nav-label"` on the raw node-info dict produced by the walk (pre-chunking — this is the
   per-node marker the reader sets; WU-4 tests the chunker's LIFT of this to batch-level meta).

Implement in `borgesica/adapters/readers/epub_reader.py`:
- New `_extract_nav_chunks(item, start_index, nav_chapter_index)` function/method (D1): parse the
  nav item's raw `content` bytes with `etree.fromstring` (HTML-parser fallback on
  `XMLSyntaxError`, same encoding-safe path as `_extract_chunks_from_item`); locate `<body>` the
  same way; select `<nav>` elements by `epub:type` attribute VALUE (namespace-agnostic) —
  `{"toc", "landmarks"}` only, excluding `page-list`; within each selected `<nav>`, emit one chunk
  per `<a>` (`node_path` at the `<a>` itself, `nav_href` = its `href`), one per un-linked
  `<span>` heading row not inside an `<a>` (`nav_href = None`), and one for the nav's own
  `<h1>`/`<h2>` (`nav_href = None`). Use the existing `_node_path(element, body)` helper (anchored
  at `<body>`) — no new path scheme.
- In `EpubReader.read`, on the `isinstance(item, epub.EpubNav)` branch (from WU2-1): instead of
  `continue`, compute `nav_chapter_index = max(body chapter_index seen so far) + 1` and call
  `_extract_nav_chunks(...)`, appending its output AFTER all body chapters (nav chunks batched
  last, per design's ordering invariant #2).
- Set `meta["kind"] = "nav-label"` on every emitted nav-label node (D3, per-node marker — the
  chunker's lift to batch-level meta is WU-4's job, not this unit's).

Deliverable: `pytest tests/integration/test_epub_reader.py` → all pass (baseline + 6 new).

**Commit**: `feat(reader): emit nav-doc <a>/<span>/heading chunks via epub:type walk (toc + landmarks, page-list excluded)`

---

## WU-3 — Chunker: lift `kind` to batch-level meta (D3a)

### WU3-1 [T] — nav-label batch carries top-level `meta["kind"]` [x]

**Depends on**: WU2-2
**Spec**: translation-quality/"quality_mode controls how many model passes run per chunk" and
"reflection is orchestrator-level and provider-agnostic" (enabling prerequisite — the orchestrator
bypass in WU-4 reads this top-level key); design D3a
**seq**

Test first (`tests/unit/test_chunking.py` or the existing chunking test module — check which
exists first):
1. A batch of nodes that ALL carry `kind == "nav-label"` (per-node, as WU2-2 sets them) → the
   output `Chunk` from `chunk_prose`'s `_flush_batch` has top-level `chunk.meta["kind"] ==
   "nav-label"`.
2. A batch of ordinary body-prose nodes (no `kind` key) → output chunk's top-level `meta` has NO
   `"kind"` key (absent, not `None` — matches the `Literal["nav-label"] | absent` contract).
3. Defensive case: a batch with a MIX of `kind=="nav-label"` and no-`kind` nodes (should not occur
   given the isolated bucket, but the `all(...)` check must handle it) → output chunk's top-level
   meta has NO `"kind"` key (only set when ALL nodes agree).

Implement in `borgesica/domain/chunking.py`, `chunk_prose`'s `_flush_batch` (or equivalent batch
      -flush site): when building the output chunk's `meta = {"prose_nodes": [...]}`, add: if
`all(n.get("kind") == "nav-label" for n in batch_nodes)` (non-empty batch), set
`chunk.meta["kind"] = "nav-label"`. Additive only — no change to existing prose-chunk output shape.

Deliverable: `pytest tests/unit/test_chunking.py` → all pass (baseline + 3 new).

**Commit**: `feat(chunking): lift kind="nav-label" to batch-level meta when all nodes agree`

---

## WU-4 — Orchestrator: reflective bypass for nav-label chunks (D3)

### WU4-1 [T] — nav-label chunk forces single-pass regardless of `quality_mode` [x]

**Depends on**: WU3-1
**Spec**: translation-quality/"quality_mode controls how many model passes run per chunk" —
scenario "nav-label chunk bypasses reflective mode even when the job is reflective";
translation-quality/"reflection is orchestrator-level and provider-agnostic" — scenario
"reflective mode works with any provider via the same port" (mixed body + nav-label job)
**seq**

Test first (`tests/unit/test_orchestrator.py`):
1. Job with `quality_mode="reflective"`, one chunk carrying top-level `meta["kind"] ==
   "nav-label"` alongside ordinary body chunks, `FakeTranslationProvider` counting calls per
   chunk → `provider.translate` called exactly ONCE for the nav-label chunk (no critique/revise),
   while body chunks in the SAME job still receive the full 3-call reflective sequence.
2. Confirm `_project_chunk_cost` (budget projection) uses `passes=1` for nav-label chunks even
   under `quality_mode="reflective"` (assert the projected cost for a nav-label chunk matches a
   fast-mode single-pass projection, not a 3x reflective projection).
3. Regression: `quality_mode="fast"` job with a nav-label chunk → unchanged (still 1 pass — this
   path was already 1 pass, confirm the new branch doesn't accidentally special-case fast mode
   differently).

Implement in `borgesica/domain/orchestrator.py`:
- In the per-chunk loop / `_translate_with_retry`: read `chunk.meta.get("kind")`. If `== "nav-label"`,
  force single-pass (skip the critique/revise steps) regardless of `config.quality_mode`. This is
  the ONLY new branch — no other chunk-kind branching introduced.
- Mirror the same check in `_project_chunk_cost` so budget projection uses `passes=1` for
  nav-label chunks.

Deliverable: `pytest tests/unit/test_orchestrator.py` → all pass (baseline + 3 new).

**Commit**: `feat(orchestrator): nav-label chunks always single-pass — reflective mode bypassed by meta[kind]`

---

## WU-5 — Writer v1: nav `<a>`/`<span>`/heading patch (regression guard, D5 xhtml path)

### WU5-1 [T] — nav `<a>` patched, `href` preserved byte-for-byte; sibling nav node_path stability [x]

**Depends on**: WU2-2 (needs real nav-label chunks with `node_path`/`nav_href` to patch)
**Spec**: book-translation/"EPUB writer reinserts translated text using prose_nodes provenance" —
scenarios "nav <a> label is patched, href preserved byte-for-byte", "node_path stays correct when
landmarks and page-list nav siblings are present"; book-translation/"EPUB writer produces a valid,
openable EPUB" — scenario "nav ol/li nesting and epub:type attributes survive nav-label patching"
**seq** (independent of WU-3/WU-4's code paths — only needs WU2-2's chunk shape — but ordered
here since it exercises the SAME writer file that WU-6/WU-7 extend next)

Test first (`tests/integration/test_epub_writer.py`):
1. Translated nav-label chunk with source `<a href="ch1.xhtml">Chapter One</a>`,
   `translated_text = "Capítulo Uno"` → output nav doc's `<a>` has text `"Capítulo Uno"`, `href`
   attribute is the byte-identical string `"ch1.xhtml"`.
2. Nav doc with three sibling `<nav>` elements in source order `[landmarks, toc, page-list]`, a
   translated label for an `<a>` inside `toc` → the writer patches the correct `<a>` inside `toc`
   (not `landmarks`/`page-list`), because both read and write locate by `epub:type`, not sibling
   position.
3. Nav doc with nested `<ol><li><a>` structure and `epub:type="toc"` → after translation, output
   preserves identical `<ol>/<li>` nesting depth/count, and `epub:type` value unchanged.

This unit adds NO new writer code — `_do_write`/`_patch_entry`'s existing `.xhtml` branch already
patches by `epub_item_href` + `node_path` and never touches `element.attrib` (verified in design
Context). These are regression-guard tests confirming the existing mechanism correctly handles
nav-doc input now that WU2-2 produces real nav-label chunks feeding it. If any test fails
unexpectedly, treat it as a genuine bug surfaced by nav content (not a design assumption) and fix
the minimal writer code required — do not silently expand scope.

Deliverable: `pytest tests/integration/test_epub_writer.py -k nav` → all 3 pass.

**Commit**: `test(writer): regression-guard nav xhtml patch — href/attrs preserved, epub:type-based nav located correctly`

---

## WU-6 — Writer v2: ncx-copy lookup + patch branch, href match (D4)

Corresponds to design U6. This is the dense unit — new lookup construction plumbed through
`_do_write`, plus the new `.ncx` branch in `_patch_entry`.

### WU6-1 [T] — `nav_label_lookup` construction + `_normalize_ncx_href` helper

**Depends on**: WU5-1 (writer v1 nav patch confirmed correct — v2 builds its lookup from the same
per-node loop), WU1-1 (needs `book.toc`-populated ncx fixture)
**Spec**: book-translation/"EPUB writer copies translated nav labels into toc.ncx by href match,
at zero provider cost" — scenarios "ncx navLabel is replaced by href match, no provider call",
"fragment-bearing ncx href matches a fragment-less nav_href"
**seq**

Test first (`tests/integration/test_epub_writer.py`), reusing/extending the hand-built
`navPoint`/`navLabel` fixture template at `test_epub_reader.py:520-574` per design D6:
1. Job whose nav doc `<a href="ch1.xhtml">` was translated to `"Capítulo Uno"`, source
   `toc.ncx` has a `navPoint` with `<content src="ch1.xhtml"/>` and `<navLabel><text>Chapter
   One</text></navLabel>` → output `toc.ncx`'s corresponding `navPoint.navLabel.text` reads
   `"Capítulo Uno"`, `content src` attribute unchanged, and (using a call-counting
   `FakeTranslationProvider`) NO additional `translate` call was made for this label beyond the
   nav-doc translation itself.
2. Translated nav label with `nav_href = "ch1.xhtml"`, ncx `navPoint` with `<content
   src="ch1.xhtml#sec2"/>` → fragment `#sec2` normalized away before matching, that navPoint's
   label replaced with the translated label.

Implement in `borgesica/adapters/writers/epub_writer.py`:
- New Step 1b in `_do_write`, immediately after `flat_patches` is built (~line 373-379, before the
  ZIP-copy loop at ~385): build `nav_label_lookup: dict[str, str]` by extending the EXISTING
  per-node loop at lines 365-371 — when `node_info.get("nav_href")` is truthy, also set
  `nav_label_lookup[_normalize_ncx_href(nav_href)] = seg` (the same `seg` already computed for
  `flat_patches` in that loop). Two-line addition to the existing loop, not a separate pass.
- New helper `_normalize_ncx_href(href: str) -> str`: strip fragment (`href.split("#", 1)[0]`),
  normalize separators / collapse `.`/`..` via `posixpath.normpath`, then key on
  `posixpath.basename` as the primary match (falling back to the full normalized path).
- Pass `nav_label_lookup` down to `_patch_entry` as a new parameter, parallel to `flat_patches`
  (writer stays stateless — no instance state).

Deliverable: `pytest tests/integration/test_epub_writer.py -k ncx` → 2 pass.

**Commit**: `feat(writer): build nav_href->translated_label lookup before ZIP loop (ncx-copy foundation)`

---

### WU6-2 [T] — `.ncx` patch branch in `_patch_entry`

**Depends on**: WU6-1
**Spec**: book-translation/"EPUB writer copies translated nav labels into toc.ncx by href match,
at zero provider cost" — scenarios "ncx navLabel is replaced by href match, no provider call"
(full end-to-end via `_patch_entry`), "fragment-bearing ncx href matches a fragment-less
nav_href" (same, through the new branch)
**seq**

Test first (extend the WU6-1 tests to exercise the FULL write path — `EpubWriter.write` end to
end, not just the lookup): confirm the same 2 assertions from WU6-1 hold when driven through
`EpubWriter.write()` on a real fixture (not by calling `_patch_entry` directly) — this is the
integration-level proof that the branch is wired correctly into `_do_write`'s ZIP loop.

Implement in `borgesica/adapters/writers/epub_writer.py`, `_patch_entry` (~line 396-434): add a
new `.ncx` branch parallel to the existing `.xhtml`/`.html`/`.htm` branch (gate:
`zip_entry_name.endswith(".ncx")`):
1. Parse with `etree.XMLParser(recover=True)` (XML parser, not the HTML fallback — ncx `<text>` is
   always plain text).
2. Walk `navMap` recursively (nested `navPoint`s).
3. For each `navPoint`: read child `<content src="...">`, normalize via `_normalize_ncx_href`,
   look up in `nav_label_lookup`; on hit, set sibling `<navLabel><text>`'s `.text` to the
   translated label (text content ONLY); on miss, leave untranslated.
4. Serialize with `etree.tostring(..., encoding="utf-8", xml_declaration=True)` (ncx REQUIRES the
   XML declaration).
5. New helper `_patch_ncx_document(raw_bytes, nav_label_lookup) -> bytes`, called from
   `_patch_entry`'s new branch.

Deliverable: `pytest tests/integration/test_epub_writer.py -k ncx` → all pass end-to-end.

**Commit**: `feat(writer): add .ncx patch branch — navMap walk, href-normalized match, XML-safe serialize`

---

## WU-7 — Writer v2: defensive fallback paths (D4 empty-lookup / unmatched / no-nav-doc)

### WU7-1 [T] — Unmatched navPoint, empty lookup, ncx-only book — no crash, no regression

**Depends on**: WU6-2
**Spec**: book-translation/"EPUB writer copies translated nav labels into toc.ncx..." — scenarios
"unmatched navPoint is left untranslated, no crash", "book with no toc.ncx is unaffected", "book
with toc.ncx but no nav doc — ncx left untranslated, no crash (explicit non-goal)"
**seq**

Test first (`tests/integration/test_epub_writer.py`):
1. `toc.ncx` with a `navPoint` whose `<content src="appendix.xhtml"/>` has NO entry in the
   lookup → that navPoint's `<navLabel><text>` remains its original source-language value, no
   exception raised.
2. EPUB with no `.ncx` item (EPUB3-only, nav-doc-only book) → ncx-copy step no-ops (no `.ncx`
   patch attempted), nav-doc translation proceeds exactly as in the nav-doc-only scenarios
   (WU5-1's assertions still hold on the same fixture).
3. EPUB with a `toc.ncx` item but NO `EpubNav` item (legacy EPUB2-style) → end-to-end
   `EpubReader.read` + `EpubWriter.write`: no nav-label chunks emitted (nothing to copy from),
   `nav_label_lookup` empty, output `toc.ncx` retains original source-language labels unchanged,
   no exception raised.

No new production code expected beyond what WU6-1/WU6-2 already implemented IF the "on miss, leave
untranslated" and "empty lookup no-op" branches were built defensively from the start (design
explicitly calls for this in the same implementation pass). Treat this unit as the RED-first proof
that those defensive paths actually hold — if any assertion fails, fix the minimal gap in
`_patch_ncx_document`/`_patch_entry`'s `.ncx` branch (e.g. missing guard for empty
`nav_label_lookup`), scoped strictly to this unit.

Deliverable: `pytest tests/integration/test_epub_writer.py` → all pass (full writer test file green).

**Commit**: `test(writer): confirm ncx-copy defensive paths — unmatched navPoint, empty lookup, ncx-only-book no-op`

---

## WU-8 — End-to-end integration + full regression

### WU8-1 [T] — E2E: nav doc + ncx translated together, fast and reflective modes

**Depends on**: WU2-2, WU3-1, WU4-1, WU5-1, WU6-2, WU7-1 (exercises the full pipeline built by all
prior units)
**Spec**: cross-cutting — exercises book-translation's nav-doc + ncx-copy requirements and
translation-quality's reflective-bypass requirement together, end to end
**seq**

Test first (extend `tests/integration/test_epub_engine_e2e.py`):
1. Full book with populated `book.toc`, `quality_mode="fast"` → nav doc labels translated, ncx
   labels match nav translations by href, output EPUB opens via `ebooklib.epub.read_epub` with no
   errors.
2. Same book, `quality_mode="reflective"` → body chapters get 3-pass reflective treatment, nav
   labels get exactly 1 pass (assert via call-counting fake provider), ncx still correctly
   copied from the single-pass nav translations.

No new production code — this unit wires together the pipeline; if a gap surfaces here that
wasn't caught by an earlier unit's tests, fix it in the unit that owns that code path (do not
patch symptoms in the e2e test file).

Deliverable: `pytest tests/integration/test_epub_engine_e2e.py -k nav` → both pass.

**Commit**: `test(e2e): nav doc + ncx translated end to end, fast and reflective modes`

---

### WU8-2 [I] — Full regression pass

**Depends on**: WU1-1 through WU8-1 (all prior units)
**seq** (final gate before handing off to `sdd-verify`)

Run the complete suite from repo root: `.venv/Scripts/python -m pytest`. Expected: prior baseline
count PLUS all new tests from WU1 through WU8 (roughly +6 reader, +3 chunking, +3 orchestrator,
+3 writer-v1-regression, +4 writer-v2 (ncx match + fragment), +3 writer-v2-defensive, +2 e2e ≈
+24 net new passing tests). Zero regressions. Run `ruff check borgesica/` (or project's configured
linter) — must exit 0. Confirm no domain-purity regression if the project has an equivalent guard
test (orchestrator/chunking changes must remain stdlib + pydantic only, no adapter imports).

No commit — verification checkpoint only. If a regression surfaces, fix it inside the work unit
that caused it (do not create a new WU for cleanup of your own change).

---

## Dependency graph

```
WU1 (fixture helper)
  └─> WU2-1 (skip-gate fix)
        └─> WU2-2 (nav walk: <a>/<span>/heading + chapter_index bucket + kind marker)
              ├─> WU3-1 (chunker lifts kind to batch meta)
              │     └─> WU4-1 (orchestrator reflective bypass)
              └─> WU5-1 (writer v1 regression guard: nav xhtml patch)
                    └─> WU6-1 (nav_label_lookup + _normalize_ncx_href)
                          └─> WU6-2 (.ncx patch branch)
                                └─> WU7-1 (defensive fallbacks)
                                      └─> WU8-1 (e2e fast + reflective)
                                            └─> WU8-2 (full regression, no commit)
```

`WU4-1` and `WU5-1`/`WU6-*`/`WU7-1` are independent branches off `WU2-2` — the reflective-bypass
work (chunker+orchestrator) does not block or get blocked by the writer/ncx work, and vice versa.
They can be executed in either order, or in parallel by two separate reviewers, as long as both
complete before `WU8-1`.

---

## Work Unit → Commit Map

| Work Unit | Commit message | Files touched |
|-----------|-----------------|---------------|
| WU-1 | `test(fixtures): add book.toc-populating helper for nav/ncx integration fixtures` | `tests/integration/test_epub_reader.py`, `tests/integration/test_epub_writer.py` |
| WU2-1 | `fix(reader): recognize EpubNav via isinstance alone — filename substring gate leaked untranslated nav docs` | `borgesica/adapters/readers/epub_reader.py`, `tests/integration/test_epub_reader.py` |
| WU2-2 | `feat(reader): emit nav-doc <a>/<span>/heading chunks via epub:type walk (toc + landmarks, page-list excluded)` | `borgesica/adapters/readers/epub_reader.py`, `tests/integration/test_epub_reader.py` |
| WU3-1 | `feat(chunking): lift kind="nav-label" to batch-level meta when all nodes agree` | `borgesica/domain/chunking.py`, `tests/unit/test_chunking.py` |
| WU4-1 | `feat(orchestrator): nav-label chunks always single-pass — reflective mode bypassed by meta[kind]` | `borgesica/domain/orchestrator.py`, `tests/unit/test_orchestrator.py` |
| WU5-1 | `test(writer): regression-guard nav xhtml patch — href/attrs preserved, epub:type-based nav located correctly` | `tests/integration/test_epub_writer.py` |
| WU6-1 | `feat(writer): build nav_href->translated_label lookup before ZIP loop (ncx-copy foundation)` | `borgesica/adapters/writers/epub_writer.py`, `tests/integration/test_epub_writer.py` |
| WU6-2 | `feat(writer): add .ncx patch branch — navMap walk, href-normalized match, XML-safe serialize` | `borgesica/adapters/writers/epub_writer.py`, `tests/integration/test_epub_writer.py` |
| WU7-1 | `test(writer): confirm ncx-copy defensive paths — unmatched navPoint, empty lookup, ncx-only-book no-op` | `borgesica/adapters/writers/epub_writer.py` (if gaps found), `tests/integration/test_epub_writer.py` |
| WU8-1 | `test(e2e): nav doc + ncx translated end to end, fast and reflective modes` | `tests/integration/test_epub_engine_e2e.py` |
| WU8-2 | (no commit — verification only) | none |

Each work unit is independently revertable and leaves the repo in a working, fully-green state.
`WU6-1`/`WU6-2` are split into two commits (lookup construction, then the patch branch consuming
it) because they are separately reviewable behaviors sharing one file/section.

---

## Review Workload Forecast

Estimated changed lines (implementation + tests, additions + modifications), based on the design's
module-by-module table and the archived change's calibration (`continue-on-error`: ~265 total
lines across a comparably-scoped change):

| Work unit | Impl LOC (est.) | Test LOC (est.) | Total |
|-----------|------------------|-------------------|-------|
| WU-1 (fixture helper) | ~0 | ~35 | ~35 |
| WU2-1 (skip-gate fix) | ~5 | ~30 | ~35 |
| WU2-2 (nav walk) | ~70 | ~130 | ~200 |
| WU3-1 (chunker lift) | ~10 | ~40 | ~50 |
| WU4-1 (orchestrator bypass) | ~15 | ~50 | ~65 |
| WU5-1 (writer v1 regression) | ~0 | ~55 | ~55 |
| WU6-1 (lookup + normalize helper) | ~25 | ~45 | ~70 |
| WU6-2 (.ncx patch branch) | ~45 | ~35 | ~80 |
| WU7-1 (defensive fallbacks) | ~10 | ~50 | ~60 |
| WU8-1 (e2e) | ~0 | ~55 | ~55 |
| WU8-2 (verification, no commit) | 0 | 0 | 0 |
| **Total** | **~180** | **~525** | **~705** |

- Estimated changed lines: **~705** across the whole change — roughly 1.75x the archived
  `continue-on-error` change (~265) and well above the 400-line single-PR budget.
- Per-commit sizes are individually reasonable (largest single unit ~200 lines, WU2-2), but the
  CUMULATIVE diff if landed as one PR would be ~705 lines — outside the ≤60-minute reviewable
  window the `chained-pr` skill targets.
- **400-line budget risk: High** — total estimate is ~1.75x the 400-line ceiling.
- **Chained PRs recommended: Yes.** Suggested slice boundary (each slice independently mergeable
  to `main`, work-unit commits preserved inside each slice):
  - **Slice A — Reader plumbing** (WU-1, WU2-1, WU2-2): ~270 lines. Nav labels become
    chunk-visible; no behavior change to translation quality or output files yet (nav labels flow
    through the pipeline as ordinary chunks, WITHOUT the reflective bypass or ncx-copy). Safe to
    land alone — regresses nothing, adds dormant capability.
  - **Slice B — Reflective bypass** (WU3-1, WU4-1): ~115 lines. Depends on Slice A's `kind`
    marker. Small, cleanly separable, low risk.
  - **Slice C — Writer v1 + v2 (ncx-copy)** (WU5-1, WU6-1, WU6-2, WU7-1): ~265 lines. Depends on
    Slice A's nav chunks existing; independent of Slice B. This is the user-visible payoff (nav
    menu actually appears translated in-reader).
  - **Slice D — E2E + full regression** (WU8-1, WU8-2): ~55 lines. Depends on B and C both being
    merged; final integration proof.
  - Slices A and then (B, C in either order or in parallel) form a **Stacked PRs to main**
    topology per the `chained-pr` skill (each slice lands independently, no long-lived feature
    branch needed) — B and C do not depend on each other, only on A.
- **Decision needed before apply: Yes** — this is the ask-on-risk gate. Before `sdd-apply` starts,
  the orchestrator must confirm with the user: proceed with 4 chained/stacked PRs (A → {B, C} → D)
  as scoped above, or record an explicit maintainer-approved `size:exception` to land this as one
  PR. Recommendation: chained PRs — the natural dependency seams (reader-plumbing vs.
  reflective-bypass vs. writer/ncx-copy) already match independently reviewable, independently
  revertible units, so slicing costs little and meaningfully reduces reviewer load per the
  `chained-pr` skill's ≤60-minute-per-PR guidance.
