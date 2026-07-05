# Delta for book-translation

Change: `nav-toc-translation` · Capability: `book-translation`
Phase: spec · Status: draft · Artifact store: openspec

---

## ADDED Requirements

### Requirement: EPUB reader emits nav-doc labels as translatable chunks with provenance

The EPUB reader SHALL locate the nav doc via `isinstance(item, epub.EpubNav)` alone (no filename check). For `toc`/`landmarks` sections (via `epub:type` XPath), it SHALL emit one `Chunk` per `<a>`, one per un-linked heading row (`<li><span>`, no `<a>`), and one for the nav's `<h1>/<h2>` heading. `page-list` SHALL be excluded (numeric, not prose).

| Meta key | Value |
|---|---|
| `epub_item_href` | nav doc href (existing key) |
| `node_path` | path to the `<a>`/`<span>`/heading itself, not its `<li>` |
| `chapter_index` | isolated bucket = `max(existing)+1` |
| `nav_href` | `<a href>` value at read time; `None` for headings |

#### Scenario: nav <a> labels emitted as chunks with node_path and nav_href

- GIVEN an EPUB fixture with `book.toc` populated (e.g. `epub.Link("ch1.xhtml", "Chapter One", "ch1")`, `epub.Link("ch2.xhtml", "Chapter Two", "ch2")`) so ebooklib generates a nav doc with non-empty `<ol><li><a>` entries
- WHEN `EpubReader.read(path, config)` is called
- THEN the resulting chunk list SHALL contain one `Chunk` per `<a>` in the nav's toc list, each `Chunk.source_text` equal to the link label text, each `meta["node_path"]` pointing at the `<a>` element, and each `meta["nav_href"]` equal to that `<a>`'s `href` attribute value

#### Scenario: nav heading is emitted and carries nav_href=None

- GIVEN the same populated-toc fixture, where the generated nav doc has an `<h1>` or `<h2>` heading (e.g. "Contents") inside the toc `<nav>`
- WHEN `EpubReader.read(path, config)` is called
- THEN a `Chunk` SHALL be emitted for that heading element with `meta["nav_href"] = None` and `meta["node_path"]` pointing at the heading element

#### Scenario: landmarks are emitted, page-list is excluded

- GIVEN an EPUB fixture whose nav doc contains a `<nav epub:type="landmarks">` section (e.g. a "Cover" link) AND a `<nav epub:type="page-list">` section (page-number links)
- WHEN `EpubReader.read(path, config)` is called
- THEN chunks SHALL be emitted for the landmarks section's `<a>` labels, and NO chunk SHALL be emitted for any `<a>` inside the page-list section

#### Scenario: nav-label chunks are isolated into their own chapter_index bucket

- GIVEN an EPUB fixture with 3 body chapters (`chapter_index` 0, 1, 2) and a populated nav doc
- WHEN `EpubReader.read(path, config)` is called
- THEN all nav-label chunks SHALL share a single `chapter_index` value of 3 (`max(0,1,2)+1`), distinct from every body-chapter `chapter_index`

---

### Requirement: EPUB writer copies translated nav labels into toc.ncx by href match, at zero provider cost

After nav-doc `<a>`/`<span>`/heading patches are finalized within `_do_write`, the EPUB writer SHALL build a `nav_href → translated_label` lookup from nav-label `prose_nodes` entries (`meta["nav_href"]` + the node's `translated_text`). It SHALL then add a `.ncx` branch to `_patch_entry`: parse the `toc.ncx` XML, walk `navMap` recursively across all nested `navPoint` elements, and for each `navPoint` resolve its `<content src="...">` href, normalize away any fragment identifier and relative-path differences, and match against the lookup. On a match, the sibling `<navLabel><text>` content SHALL be replaced with the translated label; `src` and all other attributes SHALL remain untouched. A `navPoint` whose href finds no match in the lookup SHALL be left with its original (untranslated) label — no error, no crash. This step SHALL make NO provider/model call; it is a pure read-and-copy from data already produced by the v1 nav-doc translation. Books with no `toc.ncx` entry SHALL be unaffected (the step no-ops). Books with a `toc.ncx` but no `EpubNav` item SHALL leave the ncx completely untranslated (empty lookup, no crash) — this is an explicit non-goal, not a regression.

#### Scenario: ncx navLabel is replaced by href match, no provider call

- GIVEN a job whose nav doc `<a href="ch1.xhtml">` was translated to `"Capítulo Uno"`, and the source EPUB's `toc.ncx` has a `navPoint` with `<content src="ch1.xhtml"/>` and `<navLabel><text>Chapter One</text></navLabel>`
- WHEN `EpubWriter.write` completes
- THEN the output `toc.ncx`'s corresponding `navPoint`'s `<navLabel><text>` SHALL read `"Capítulo Uno"`, its `<content src="...">` attribute SHALL be unchanged, and no additional `TranslationProvider.translate` call SHALL have been made for this label

#### Scenario: fragment-bearing ncx href matches a fragment-less nav_href

- GIVEN a translated nav label with `nav_href = "ch1.xhtml"`, and a `toc.ncx` `navPoint` whose `<content src="ch1.xhtml#sec2"/>`
- WHEN the ncx-copy step runs
- THEN the fragment `#sec2` SHALL be normalized away before matching, and that `navPoint`'s label SHALL be replaced with the translated label

#### Scenario: unmatched navPoint is left untranslated, no crash

- GIVEN a `toc.ncx` with a `navPoint` whose `<content src="appendix.xhtml"/>` has no corresponding entry in the nav_href lookup
- WHEN the ncx-copy step runs
- THEN that `navPoint`'s `<navLabel><text>` SHALL remain its original source-language value, and no exception SHALL be raised

#### Scenario: book with no toc.ncx is unaffected

- GIVEN an EPUB with no `.ncx` item (e.g. EPUB3-only, nav-doc-only book)
- WHEN `EpubWriter.write` completes
- THEN the ncx-copy step SHALL no-op (no `.ncx` patch attempted), and nav-doc translation SHALL proceed exactly as in the nav-doc-only scenarios above

#### Scenario: book with toc.ncx but no nav doc — ncx left untranslated, no crash (explicit non-goal)

- GIVEN an EPUB that has a `toc.ncx` item but no `EpubNav` item (legacy EPUB2-style book)
- WHEN `EpubReader.read` and `EpubWriter.write` run end to end
- THEN no nav-label chunks SHALL be emitted (nothing to copy from), the `nav_href → translated_label` lookup SHALL be empty, the output `toc.ncx` SHALL retain its original source-language labels unchanged, and no exception SHALL be raised

---

## MODIFIED Requirements

### Requirement: EPUB reader extracts text nodes, preserving structure metadata

The EPUB reader SHALL traverse the EPUB's XHTML content documents in reading order (spine order as defined by the OPF). For each content document, it SHALL extract text nodes from the `<body>` element only, preserving paragraph and heading boundaries. Each `Chunk.meta` SHALL carry at minimum `{"epub_item_href": str, "node_path": str, "chapter_index": int}` to enable faithful reinsertion and chapter-boundary enforcement. `chapter_index` is 0-based per spine document (all nodes from the first spine document share `chapter_index=0`, all from the second share `chapter_index=1`, etc.). The reader returns one `Chunk` per text node. Structural markup (chapter headings, `<div>` wrappers) SHALL NOT be translated — only the text content of leaf nodes is extracted. The EPUB3 navigation document (`isinstance(item, epub.EpubNav)`) SHALL NOT be walked by this general body-text traversal; it is handled by a dedicated nav walk (see "EPUB reader emits nav-doc labels as translatable chunks with provenance") that runs instead of the `continue`-and-skip behavior. The nav-doc identification SHALL rely on `isinstance(item, epub.EpubNav)` alone — a prior filename-substring pre-check (`"nav" in item_name.lower()`) SHALL NOT gate this decision, since a nav document named without the substring `"nav"` (e.g. `contents.xhtml`) would otherwise leak into the general chunk stream as unmarked body prose.

(Previously: the nav document was detected via `if item_name and "nav" in item_name.lower(): if isinstance(item, epub.EpubNav): continue` — a nav doc without "nav" in its filename escaped the skip gate and its raw, untranslated markup leaked into the regular chunk stream. There was also no dedicated nav walk; the nav document's content was simply never processed.)

#### Scenario: spine order is respected

- GIVEN an EPUB with 3 chapters declared in spine order `[ch1.xhtml, ch2.xhtml, ch3.xhtml]`
- WHEN `EpubReader.read(path, config)` is called
- THEN the resulting `Chunk` list SHALL contain all chunks from `ch1.xhtml` before any chunk from `ch2.xhtml`, and all chunks from `ch2.xhtml` before any chunk from `ch3.xhtml`

#### Scenario: images are not extracted as chunks

- GIVEN an EPUB where a content document contains `<img>` elements
- WHEN `EpubReader.read(path, config)` is called
- THEN no `Chunk` SHALL contain binary image data; image elements SHALL be left intact (tracked via `meta`) for the writer to reinsert

#### Scenario: nav document named without "nav" substring is still recognized and does not leak into body chunks

- GIVEN an EPUB whose `EpubNav` item's filename is `contents.xhtml` (no "nav" substring) and `book.toc` is populated
- WHEN `EpubReader.read(path, config)` is called
- THEN NO chunk from the general body-text traversal SHALL contain that document's raw nav markup (e.g. `<nav`, `epub:type`) as `source_text`; the item's labels SHALL instead be emitted exclusively via the dedicated nav walk as nav-label chunks

#### Scenario: book without a nav document is unaffected

- GIVEN an EPUB with no `EpubNav` item (e.g. an EPUB2-only book, ncx-only)
- WHEN `EpubReader.read(path, config)` is called
- THEN no nav-label chunks SHALL be emitted, body-chapter chunk extraction SHALL proceed exactly as before this change, and no exception SHALL be raised

---

### Requirement: EPUB writer reinserts translated text using prose_nodes provenance

The EPUB writer SHALL reinsert translated text by splitting each output chunk's `translated_text` on `"\n\n"` and writing segment `i` into the XHTML node identified by `chunk.meta["prose_nodes"][i]["node_path"]` within the document identified by `chunk.meta["prose_nodes"][i]["epub_item_href"]`. Segments sharing a `node_path` SHALL be concatenated back into that single node. This mechanism applies unchanged to nav-label chunks: a nav `<a>`/`<span>`/heading node is patched via the same `epub_item_href` + `node_path` resolution, and `_set_element_content` replaces ONLY the element's text content — `href`, `id`, and all other attributes on the patched element and its ancestors SHALL remain byte-identical to the source. `node_path` resolution SHALL remain stable and correct when the nav document has sibling `<nav epub:type="landmarks">` and/or `<nav epub:type="page-list">` elements alongside the `toc` nav under `<body>` — read-time and write-time walks operate on the same original source structure, so positional sibling indices do not drift between read and write.

(Previously: this requirement described only body-chapter `prose_nodes` reinsertion; it did not address nav-doc patching or multi-nav-sibling `node_path` stability, since nav-doc chunks did not exist.)

#### Scenario: nav <a> label is patched, href preserved byte-for-byte

- GIVEN a translated nav-label chunk whose source was `<a href="ch1.xhtml">Chapter One</a>` with `translated_text = "Capítulo Uno"`
- WHEN `EpubWriter.write` reinserts this chunk
- THEN the output nav doc's corresponding `<a>` element SHALL have text content `"Capítulo Uno"` and its `href` attribute SHALL be the byte-identical string `"ch1.xhtml"`

#### Scenario: node_path stays correct when landmarks and page-list nav siblings are present

- GIVEN a nav document with three sibling `<nav>` elements under `<body>` in source order `[landmarks, toc, page-list]`, and a translated label for an `<a>` inside the `toc` nav
- WHEN `EpubWriter.write` reinserts this chunk
- THEN the writer SHALL patch the correct `<a>` element inside the `toc` nav (not an element in the `landmarks` or `page-list` siblings), because the nav is located by `epub:type` at both read and write time, not by sibling position alone

---

### Requirement: EPUB writer produces a valid, openable EPUB

The EPUB writer SHALL write the translated content back into a structurally valid EPUB using `ebooklib`. The output EPUB SHALL:
- Open without errors in `ebooklib.epub.read_epub`
- Preserve all original chapters in their original spine order
- Preserve all images (binary blobs unchanged)
- Preserve all original CSS stylesheets
- Preserve all internal EPUB structural metadata (OPF, NCX/NAV, container.xml)
- Replace only the translated text nodes; all surrounding structural HTML remains identical to the source, including nav `<ol>`/`<li>` nesting and `epub:type` attributes when nav-label patching is applied

(Previously: this requirement did not call out nav structure/`epub:type` preservation explicitly, since nav content was never patched.)

#### Scenario: output EPUB opens and chapter count matches

- GIVEN a source EPUB with 12 chapters
- WHEN `EpubWriter.write(chunks, src_path, out_path)` completes
- THEN `ebooklib.epub.read_epub(out_path)` SHALL succeed (no exception), and the number of EPUB items with media type `application/xhtml+xml` SHALL equal 12

#### Scenario: images are byte-identical in output

- GIVEN a source EPUB containing 3 image files (PNG, JPEG)
- WHEN `EpubWriter.write(chunks, src_path, out_path)` completes
- THEN each image in the output EPUB SHALL be byte-identical to its counterpart in the source EPUB

#### Scenario: CSS stylesheets are preserved

- GIVEN a source EPUB containing a CSS stylesheet
- WHEN `EpubWriter.write(chunks, src_path, out_path)` completes
- THEN the output EPUB SHALL contain the same CSS file(s) with byte-identical content

#### Scenario: no partial output on writer failure

- GIVEN that `EpubWriter.write` raises an exception partway through writing
- THEN no incomplete output file SHALL be left at `out_path` (the writer SHALL write to a temp path and atomically rename on success)

#### Scenario: nav ol/li nesting and epub:type attributes survive nav-label patching

- GIVEN a source EPUB whose nav doc has nested `<ol><li><a>...</a></li></ol>` structure and `epub:type="toc"` on the toc `<nav>`
- WHEN `EpubWriter.write` completes after translating nav labels
- THEN the output nav doc SHALL preserve the identical `<ol>/<li>` nesting depth and count, and the `epub:type` attribute value on the toc `<nav>` SHALL be unchanged
