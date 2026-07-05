# Design: Nav/TOC Translation

Change: `nav-toc-translation`
Phase: design · Status: draft · Artifact store: openspec
Depends on: `proposal.md` (Decisions 1-7), `spec.md` (deltas, same round)

---

## Context

The reader today skips the EPUB3 nav document entirely (`epub_reader.py:308-314`), so chapter-menu labels are never translated. This change routes nav-doc labels through the existing reader → chunker → orchestrator → writer pipeline (v1), then copies the resulting translated labels into `toc.ncx` at write time with no extra model call (v2).

The pipeline is already format-agnostic below the reader: `chunk_prose` passes every meta key through verbatim, and the writer resolves nodes positionally by local tag name. That means v1 needs code in exactly two places — the reader (emit nav chunks) and (for the reflective bypass) the orchestrator — and v2 needs one new writer branch. Everything else is inert-key plumbing.

Verified against source before writing this design:
- `ebooklib/epub.py:_get_nav` (1147-1286): nav shape is `<body><nav epub:type="toc"><h2>…</h2><ol><li><a href="…">Label</a></li>…</ol></nav></body>`; landmarks and page-list are sibling `<nav>`s under `<body>`, gated by `epub3_landmark`/`epub3_pages` options. Nav `<a href>` is `zip_path.relpath(file_name, nav_dir_name)` — relative to the nav document's own directory.
- `ebooklib/epub.py:_get_ncx` (1288-1391): ncx shape is `navMap > navPoint > (navLabel > text) + (content src="…")`. The `content src` is the item's `file_name` (path from the OPF/content root), **not** relativized to the nav dir. DAISY namespace `http://www.daisy.org/z3986/2005/ncx/`.
- `orchestrator.py`: reflective mode is gated ONLY by `config.quality_mode` inside `_translate_with_retry` (line 447) and `_project_chunk_cost` (line 387). There is NO per-chunk override today.
- `epub_writer.py`: `_do_write` builds `flat_patches` from `prose_nodes`, reading ONLY `epub_item_href`/`node_path` (lines 365-371). `_patch_entry` gates patching on `.xhtml/.html/.htm` (lines 430-435) → `.ncx` needs a new branch. No path-normalization helper exists in the writer today.
- `_set_element_content` never touches `element.attrib` (verified) → patching `<a href="…">Label</a>` preserves `href`.

---

## Decisions

Each decision is numbered; rationale and the rejected alternative are inline. These REFINE the proposal's Decisions 1-7 into implementable mechanics.

### D1 — Reader nav walk: locate by `epub:type`, emit `<a>`/`<span>`/heading node-chunks

Run a dedicated walk over the `EpubNav` spine item (do NOT `continue` past it). Mechanism:

1. Parse the nav item's raw `content` bytes with `etree.fromstring` (same encoding-safe path as `_extract_chunks_from_item`; HTML-parser fallback on `XMLSyntaxError`).
2. Locate the `<body>` the same way `_extract_chunks_from_item` does.
3. Select target `<nav>` elements by `epub:type` attribute using a namespace-agnostic match on the attribute VALUE — `epub:type in {"toc", "landmarks"}`, EXCLUDING `page-list` (Decision 6). Do NOT assume position; iterate all `<nav>` under `<body>` and filter. Rationale: landmarks/page-list siblings shift positional indices — selecting by value is stable regardless of sibling count (proposal Risk 1).
4. Within each selected `<nav>`, emit one Chunk per:
   - `<a>` descendant → `node_path` points DIRECTLY at the `<a>` (not its `<li>`), `nav_href` = the `<a>`'s `href` attribute value.
   - `<span>` descendant that is NOT inside an `<a>` (un-linked heading row `<li><span>…`) → `node_path` at the `<span>`, `nav_href = None`.
   - the nav's own `<h1>`/`<h2>` section heading (Decision 4) → `node_path` at the heading, `nav_href = None`.
5. `node_path` is computed with the EXISTING `_node_path(element, body)` — anchored at `<body>`, so multiple `<nav>` siblings produce distinct, stable paths (`/nav[0]/…`, `/nav[1]/…`). This is the SAME `<body>`-anchored scheme the writer's `_find_node` inverts, so read-time and write-time agree by construction.

**Rejected**: removing `"nav"` from `_SKIP_ELEMENTS` and adding `a`/`span` to `_TEXT_ELEMENTS` globally — would extract stray `<nav>`s in ordinary content docs and page-list numbers, and couples generic content extraction with nav semantics.

**Node-path anchoring note**: `_node_path` counts same-local-tag siblings under each parent. Because the walk emits `<a>`/`<span>`/heading paths rooted at `<body>` (traversing `nav[i]/ol[j]/li[k]/a[0]`), and the writer re-parses the ORIGINAL nav bytes and walks the identical algorithm, indices are invariant. No new path scheme is introduced.

### D2 — Skip-gate fix: `isinstance(item, epub.EpubNav)` alone

Replace the `if item_name and ("nav" in item_name.lower()): if isinstance(...)` two-level gate with a single `isinstance(item, epub.EpubNav)` check. When true, run the D1 nav walk instead of `continue`. Rationale: a nav doc named `contents.xhtml` (no "nav" substring) currently escapes the skip and leaks into the chunk stream (proposal Risk 5). ebooklib already classifies the item from OPF `properties="nav"`, so `isinstance` is authoritative.

### D3 — Single-pass signal: isolated `chapter_index` bucket + `meta["kind"] = "nav-label"` marker

Two distinct needs, resolved with ONE mechanism each — do not conflate them:

- **Batch isolation** (keep nav labels out of body prose calls): assign nav-label node-chunks `chapter_index = <max body chapter_index> + 1` (a dedicated final bucket). `chunk_prose`'s `groupby` then batches them separately, appended LAST so they see the fullest glossary/summary (Decision proposal §4.3). This uses the EXISTING mechanism — no chunker change.
- **Reflective bypass** (Decision 2 — nav labels never use reflective mode): the orchestrator needs a per-chunk signal, because reflective is gated by `config.quality_mode` globally today with no per-chunk override. **Decision: add `meta["kind"] = "nav-label"` to nav-label node-chunks at read time.** `chunk_prose` passes it through into each `prose_nodes` entry AND we propagate it to the batched output chunk's top-level meta (see D3a). The orchestrator reads `chunk.meta.get("kind") == "nav-label"` and forces single-pass for that chunk only.

Rationale for picking `meta["kind"]` OVER "infer from isolated bucket": the orchestrator has NO knowledge of which `chapter_index` is the nav bucket (chunk_prose strips `chapter_index` from output chunks — see chunking.py:199). An explicit marker is the only signal that survives to the orchestrator. The bucket handles batching; the marker handles branching. Each does exactly one job.

**Contract for `kind`**: `Literal["nav-label"] | absent`. Absent = ordinary prose (current behavior). The ONLY orchestrator branch is: if `kind == "nav-label"`, use single-pass regardless of `config.quality_mode`. No other chunk-kind branching is introduced (proposal Non-goal §2).

#### D3a — Propagating `kind` from node-chunk to batched chunk

`chunk_prose` builds output chunks whose top-level `meta` is `{"prose_nodes": [...]}` only (chunking.py:186). The per-node `kind` lands INSIDE each `prose_nodes` entry (passed through with all non-`chapter_index` keys). To let the orchestrator read `chunk.meta["kind"]` cheaply, chunk_prose must lift `kind` to the batch's top-level meta when present.

**Decision**: minimal chunker change — when flushing a batch, if ALL nodes in the batch carry `kind == "nav-label"`, set `chunk.meta["kind"] = "nav-label"` on the output chunk. Because the isolated bucket guarantees nav-label nodes never mix with body nodes in one batch, "all or none" holds by construction; a defensive `all(...)` check keeps it correct even if that invariant ever breaks. This is the ONLY chunker change and it is additive (new key, no behavior change for existing prose).

**Rejected**: having the orchestrator inspect `prose_nodes[i]["kind"]` directly — leaks writer-locator structure into the orchestrator and requires it to reach into a list; a top-level marker is cleaner and matches how the orchestrator already reads `chunk.meta`.

**Rejected**: a `JobConfig` flag — Decision 1 is always-on; a flag contradicts it and adds config surface for a defect fix.

### D4 — v2 ncx-copy step: runs inside `_do_write` AFTER nav patch bytes finalized

The ncx-copy is a new step in `_do_write`, executed within the SAME ZIP-copy pass but ordered so the nav→label lookup is fully built BEFORE any `.ncx` entry is written.

**Ordering invariant (proposal Risk 3)**: `flat_patches` is built in full at the top of `_do_write` (Step 1) BEFORE the ZIP loop (Step 2). The `nav_href → translated_label` lookup is derived from the SAME `chunks`/`prose_nodes` data, so it too is built in Step 1, before the loop. Therefore the ncx-copy lookup is available regardless of whether the `.ncx` entry appears before or after `nav.xhtml` in the ZIP iteration order. **The invariant is: lookup construction precedes the ZIP write loop.** This is stronger and simpler than "ncx entry processed after nav entry" — it removes any dependency on ZIP entry ordering.

**Lookup construction** (new Step 1b, after `flat_patches`):
```
nav_label_lookup: dict[str, str] = {}   # normalized_href → translated_label
for chunk in chunks:
    for node_info in chunk.meta.get("prose_nodes", []):
        nav_href = node_info.get("nav_href")
        if not nav_href:
            continue                       # headings / spans → None; skip
        translated = <segment text for this node>   # same mapping _do_write uses
        nav_label_lookup[_normalize_ncx_href(nav_href)] = translated
```
The translated text per node is obtained the SAME way Step 1 maps segments → nodes (split translated_text on `\n\n`, positional, defensive-mismatch tolerant). To avoid duplicating that logic, capture `nav_href → translated` while building `flat_patches` in the existing per-node loop (the loop already has `node_info` and the mapped `seg` in scope at line 365-371). Keying by the node's `nav_href` when present is a two-line addition to that loop.

**`.ncx` patcher branch in `_patch_entry`** (new, parallel to the `.xhtml` branch):
1. Gate: `zip_entry_name.endswith(".ncx")` (media type `application/x-dtbncx+xml` also acceptable, but extension is what the writer already keys on).
2. Parse with the XML parser (`etree.XMLParser(recover=True)`) — NOT the HTML parser. ncx `<text>` is always plain text, never inline markup, so no fragment tolerance is needed.
3. Walk `navMap` recursively (nested `navPoint`s). For each `navPoint`:
   - read its child `<content src="…">` value;
   - `_normalize_ncx_href(src)` and look it up in `nav_label_lookup`;
   - on hit, set the sibling `<navLabel><text>` `.text` to the translated label — text content ONLY, no attribute touched;
   - on miss, leave that `navPoint` untranslated (defensive fallback, proposal Risk 4).
4. Serialize with `etree.tostring(..., encoding="utf-8", xml_declaration=True)` — preserve the XML declaration (ncx REQUIRES it).

**Href normalization `_normalize_ncx_href(href)`** (new helper): the ncx `content src` and the nav `<a href>` may have DIFFERENT bases (verified: nav href is relativized to the nav dir; ncx src is the item `file_name` from content root). Normalization rules, applied to BOTH sides before matching:
- strip any fragment: `href.split("#", 1)[0]` (proposal Risk 6 — `ch1.xhtml#sec2` matches `ch1.xhtml`);
- normalize path separators and collapse `.`/`..` via `posixpath.normpath` (EPUB paths are always `/`-separated);
- match on the resulting BASENAME (`posixpath.basename`) as the primary key, falling back to the full normalized path.
  Rationale: fixtures generated by THIS codebase place nav.xhtml and content docs in the same dir, so basename equality is exact and robust; matching on basename tolerates the nav-vs-content-root base difference for real books without resolving each side's true absolute path (which the writer does not track). Basename collisions across dirs are a documented, acceptable degradation (an unmatched-or-mismatched navPoint stays untranslated — never corrupts an href).

**Empty-lookup no-op**: if `nav_label_lookup` is empty (no nav chunks — Decision 3 no-nav-doc / ncx-only book), the `.ncx` branch does nothing and returns original bytes. Guarantees zero regression for books without a nav doc (proposal Risk 7).

**Behavior matrix**:

| Condition | Behavior |
|-----------|----------|
| ncx missing | No `.ncx` entry in ZIP → branch never runs. No-op. |
| nav doc missing (ncx-only book) | `nav_label_lookup` empty → `.ncx` branch no-ops → ncx untranslated (Decision 3). No crash. |
| navPoint href unmatched | That navPoint's `<text>` left as-is (source language). |
| navPoint href matched | `<text>` replaced with translated label; `content src` untouched. |
| ncx XML unparseable | Return original bytes (defensive, mirrors `_patch_xhtml_document`). |

**Rejected**: model-translate the ncx independently (Option 1 in exploration) — doubles cost and reintroduces nav-vs-ncx divergence.

### D5 — Data contracts

**Reader meta on nav-label node-chunks** (additions in **bold**):

| Key | Type | Set by | Consumed by | Purpose |
|-----|------|--------|-------------|---------|
| `epub_item_href` | `str` | reader (existing) | writer xhtml patch | locate the nav zip entry |
| `node_path` | `str` | reader (existing) | writer xhtml patch, `_find_node` | locate the element |
| `chapter_index` | `int` | reader (**isolated bucket** for nav) | chunk_prose groupby | isolate nav batch |
| **`nav_href`** | **`str \| None`** | **reader (NEW, nav labels only)** | **writer ncx-copy (v2)** | **href match into ncx** |
| **`kind`** | **`Literal["nav-label"]`** | **reader (NEW, nav labels only)** | **orchestrator (reflective bypass), chunk_prose (lift to batch)** | **mark nav-label chunks** |

`nav_href` and `kind` are INERT to the `.xhtml` patch path — `_do_write` reads only `epub_item_href`/`node_path` from `prose_nodes` (verified lines 365-371). Both pass through `chunk_prose` verbatim (all non-`chapter_index` keys are copied, chunking.py:198-200).

**`prose_nodes` entry shape** for a nav-label node after chunking:
`{"epub_item_href": str, "node_path": str, "nav_href": str | None, "kind": "nav-label"}`.

**Batched output chunk top-level meta** for a nav batch: `{"prose_nodes": [...], "kind": "nav-label"}` (D3a).

**JobConfig / model changes: NONE** — verified feasible. Always-on (D1) needs no flag; reflective bypass uses `meta["kind"]` not config; ncx-copy uses `nav_href`. No new pydantic fields. Confirms proposal §4.6 and Decision 1/5.

### D6 — Test strategy (strict TDD, RED first per work unit)

**Fixture strategy** — verified against installed ebooklib (`_get_nav`/`_get_ncx`):
- Existing fixtures never set `book.toc` → generated `<ol>`/`navMap` are EMPTY. New tests MUST populate `book.toc = [epub.Link(href, title, uid), ...]` (or `(Section, [children])` tuples) so ebooklib emits real `<a href>`/`navPoint` content.
- ebooklib generates nav `<a href>` as `zip_path.relpath(file_name, nav_dir)` and ncx `content src` as `file_name`. With the flat single-dir fixture layout used across existing tests, both reduce to the same basename — exactly what `_normalize_ncx_href` matches on.
- `test_epub_reader.py:520-574` has a hand-built `navPoint`/`navLabel` template — reuse it for a v2 ncx fixture that includes a fragment-bearing `content src` (`ch1.xhtml#sec2`) and an unmatched navPoint.

**Test placement**:
- Reader tests → `tests/integration/test_epub_reader.py`.
- Writer v1 (nav xhtml patch) + v2 (ncx copy) → `tests/integration/test_epub_writer.py`.
- Orchestrator reflective-bypass → `tests/unit/test_orchestrator.py`.
- Chunker `kind` lift → `tests/unit/test_chunking.py` (or existing chunking test module).
- End-to-end → extend `tests/integration/test_epub_engine_e2e.py`.

**RED-first ordering by work unit** (each unit: failing test first, then code to green):

| Unit | RED test asserts | Then implement |
|------|------------------|----------------|
| U1 skip-gate | nav named `contents.xhtml` recognized as EpubNav, does NOT leak into ordinary chunk stream | D2 gate fix |
| U2 nav walk | `read` emits `<a>` chunks w/ correct `node_path`, `nav_href`, isolated `chapter_index`, `kind="nav-label"`; `<h2>` heading emitted (`nav_href=None`); landmarks emitted; page-list NOT emitted | D1 walk |
| U3 chunker lift | nav-label batch chunk carries top-level `meta["kind"]="nav-label"`; body chunks do not | D3a |
| U4 orch bypass | nav-label chunk uses single-pass even when `config.quality_mode="reflective"` (assert provider call count = 1, not 3) | D3 orchestrator branch |
| U5 writer v1 | patching `<a href="ch1.xhtml">` replaces text, preserves `href` byte-for-byte; output EPUB valid | none (existing writer works) — regression guard only |
| U6 writer v2 match | ncx `<navLabel><text>` replaced by href match; fragment src `ch1.xhtml#sec2` matches nav `ch1.xhtml`; `content src` untouched | D4 ncx branch + `_normalize_ncx_href` |
| U7 writer v2 fallback | unmatched navPoint left untranslated; empty lookup no-ops; ncx-only book leaves ncx untranslated, no crash | D4 defensive paths |

### D7 — Alternatives rejected (one line each)

- Global `_SKIP_ELEMENTS`/`_TEXT_ELEMENTS` change instead of a scoped nav walk — extracts page-list/landmarks noise, couples generic and nav extraction (D1).
- Filename-substring skip gate retained — leaks nav docs lacking "nav" in the name (D2).
- Infer "is nav label" from the isolated `chapter_index` in the orchestrator — impossible: chunk_prose strips `chapter_index` from output chunks (D3).
- `JobConfig.translate_nav` flag — contradicts always-on Decision 1, adds config surface for a defect fix (D3).
- Model-translate the ncx independently — 2x cost and nav-vs-ncx divergence (D4).
- Order ncx-copy strictly after the nav ZIP entry — replaced by the stronger "build lookup before the ZIP loop" invariant (D4).
- True title-reuse (nav label = translated body `<h1>`) — deferred, needs id-aware provenance and inline-strip (proposal Decision 7).

---

## Module-by-module changes

| File | Function/site | Change |
|------|---------------|--------|
| `borgesica/adapters/readers/epub_reader.py` | `EpubReader.read` (308-314) | Replace two-level skip gate with `isinstance(item, epub.EpubNav)`; on match call new `_extract_nav_chunks(item, start_index, nav_chapter_index)` instead of `continue`. |
| `borgesica/adapters/readers/epub_reader.py` | NEW `_extract_nav_chunks` | D1 walk: parse nav bytes, find `<body>`, select `<nav>` by `epub:type in {toc, landmarks}`, emit `<a>`/`<span>`/heading Chunks with meta `{epub_item_href, node_path, chapter_index=<nav bucket>, nav_href, kind="nav-label"}`. |
| `borgesica/adapters/readers/epub_reader.py` | `read` loop | Compute nav bucket = `max body chapter_index + 1`; process EpubNav item(s) so nav chunks are appended LAST (after body chapters). |
| `borgesica/domain/chunking.py` | `chunk_prose` `_flush_batch` | D3a: if all batch nodes carry `kind=="nav-label"`, set output `chunk.meta["kind"]="nav-label"`. Additive only. |
| `borgesica/domain/orchestrator.py` | `run` loop / `_translate_with_retry` | D3: when `chunk.meta.get("kind")=="nav-label"`, force single-pass (skip reflective) regardless of `config.quality_mode`. Also mirror in `_project_chunk_cost` so the budget projection uses `passes=1` for nav-label chunks. |
| `borgesica/adapters/writers/epub_writer.py` | `_do_write` Step 1 loop (365-371) | While mapping segments→nodes, also populate `nav_label_lookup[_normalize_ncx_href(nav_href)] = seg` when `node_info["nav_href"]` is set. |
| `borgesica/adapters/writers/epub_writer.py` | `_patch_entry` | Add `.ncx` branch (D4) after the `.xhtml` branch: XML-parse, walk navMap, patch matched `<navLabel><text>`, serialize with XML declaration. Pass `nav_label_lookup` down (via new param or instance state on the writer). |
| `borgesica/adapters/writers/epub_writer.py` | NEW `_normalize_ncx_href`, NEW `_patch_ncx_document` | Helper + XML patcher per D4. `_patch_ncx_document(raw_bytes, nav_label_lookup) -> bytes`. |

Threading `nav_label_lookup` into `_patch_entry`: pass it as an extra argument from `_do_write` (parallel to `flat_patches`). `_patch_entry` already takes `flat_patches`; add `nav_label_lookup` alongside. No instance state needed — keeps the writer stateless.

---

## Data contracts (summary)

- Reader adds `nav_href: str | None` and `kind: "nav-label"` to nav-label node meta ONLY. Both inert to the xhtml patch path.
- chunk_prose lifts `kind` to batch-level meta when the whole batch is nav-label.
- Orchestrator reads `chunk.meta["kind"]` for the single-pass bypass — the ONLY new branch.
- Writer builds `nav_label_lookup: dict[normalized_href, translated_label]` and consumes it in the `.ncx` branch only.
- No JobConfig / pydantic model changes (verified feasible).

---

## Ordering invariants

1. **Lookup-before-loop (v2 critical)**: `nav_label_lookup` is fully constructed in `_do_write` Step 1 (during `flat_patches` construction), BEFORE the ZIP-copy loop. Therefore ncx patching does not depend on `nav.xhtml` appearing before `toc.ncx` in ZIP iteration order. This supersedes the proposal's "ncx entry after nav entry" phrasing with a stronger guarantee.
2. **Nav chunks batched last**: reader assigns nav labels the highest `chapter_index`, so `chunk_prose` groupby emits them in the final batch(es) → they see the fullest glossary/summary.
3. **Read-time == write-time structure**: the writer parses the ORIGINAL nav bytes and walks the identical `_node_path`/`_find_node` algorithm, so positional node paths are invariant across the two phases.

---

## Test strategy

See D6. Strict TDD: each work unit (U1-U7) writes a failing test first, then the minimal code to green. Fixtures MUST populate `book.toc` to exercise real labels; the v2 ncx fixture MUST include a fragment-bearing `content src` and an unmatched navPoint to cover the normalization and fallback paths.

---

## Alternatives rejected

See D7 (consolidated one-liners).

---

## Next phase

`sdd-tasks` — break U1-U7 into ordered work units with strict-TDD RED-first steps and the delivery-size forecast. Design and spec are both ready.
