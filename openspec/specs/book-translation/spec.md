# Spec: book-translation

Capability: `book-translation` · Status: canonical

---

### Requirement: EPUB reader rejects DRM-protected files

The EPUB reader SHALL detect DRM-protected EPUB files and raise a domain exception (`UnsupportedFormatError`) with a clear human-readable message before any partial output is written or any provider call is made.

#### Scenario: DRM EPUB triggers immediate error, no partial output

Given an `.epub` file that contains an `encryption.xml` element indicating Adobe DRM or any other DRM scheme,

When `EpubReader.read(path, config)` is called,

Then a `UnsupportedFormatError` SHALL be raised with a message identifying the file as DRM-protected, the job status SHALL remain `CREATED` (no chunks are persisted), and no output file SHALL be created.

#### Scenario: malformed or unreadable EPUB triggers clear error

Given a file with an `.epub` extension that is not a valid ZIP or not a valid EPUB container,

When `EpubReader.read(path, config)` is called,

Then a `UnsupportedFormatError` SHALL be raised with a message indicating the file is not a valid EPUB, and no partial output SHALL be written.

#### Scenario: valid non-DRM EPUB is accepted

Given a standard non-DRM `.epub` file,

When `EpubReader.read(path, config)` is called,

Then the call SHALL return an ordered list of `Chunk` objects with no exception raised.

---

### Requirement: EPUB reader extracts text nodes, preserving structure metadata

The EPUB reader SHALL traverse the EPUB's XHTML content documents in reading order (spine order as defined by the OPF). For each content document, it SHALL extract text nodes from the `<body>` element only, preserving paragraph and heading boundaries. Each `Chunk.meta` SHALL carry at minimum `{"epub_item_href": str, "node_path": str, "chapter_index": int}` to enable faithful reinsertion and chapter-boundary enforcement. `chapter_index` is 0-based per spine document (all nodes from the first spine document share `chapter_index=0`, all from the second share `chapter_index=1`, etc.). The reader returns one `Chunk` per text node. Structural markup (chapter headings, `<div>` wrappers) SHALL NOT be translated — only the text content of leaf nodes is extracted. The EPUB3 navigation document (`isinstance(item, epub.EpubNav)`) SHALL NOT be walked by this general body-text traversal; it is handled by a dedicated nav walk (see "EPUB reader emits nav-doc labels as translatable chunks with provenance") that runs instead of the `continue`-and-skip behavior. The nav-doc identification SHALL rely on `isinstance(item, epub.EpubNav)` alone — a prior filename-substring pre-check (`"nav" in item_name.lower()`) SHALL NOT gate this decision, since a nav document named without the substring `"nav"` (e.g. `contents.xhtml`) would otherwise leak into the general chunk stream as unmarked body prose.

(Previously: the nav document was detected via `if item_name and "nav" in item_name.lower(): if isinstance(item, epub.EpubNav): continue` — a nav doc without "nav" in its filename escaped the skip gate and its raw, untranslated markup leaked into the regular chunk stream. There was also no dedicated nav walk; the nav document's content was simply never processed.)

#### Scenario: spine order is respected

Given an EPUB with 3 chapters declared in spine order `[ch1.xhtml, ch2.xhtml, ch3.xhtml]`,

When `EpubReader.read(path, config)` is called,

Then the resulting `Chunk` list SHALL contain all chunks from `ch1.xhtml` before any chunk from `ch2.xhtml`, and all chunks from `ch2.xhtml` before any chunk from `ch3.xhtml`.

#### Scenario: images are not extracted as chunks

Given an EPUB where a content document contains `<img>` elements,

When `EpubReader.read(path, config)` is called,

Then no `Chunk` SHALL contain binary image data; image elements SHALL be left intact (tracked via `meta`) for the writer to reinsert.

#### Scenario: nav document named without "nav" substring is still recognized and does not leak into body chunks

Given an EPUB whose `EpubNav` item's filename is `contents.xhtml` (no "nav" substring) and `book.toc` is populated,

When `EpubReader.read(path, config)` is called,

Then NO chunk from the general body-text traversal SHALL contain that document's raw nav markup (e.g. `<nav`, `epub:type`) as `source_text`; the item's labels SHALL instead be emitted exclusively via the dedicated nav walk as nav-label chunks.

#### Scenario: book without a nav document is unaffected

Given an EPUB with no `EpubNav` item (e.g. an EPUB2-only book, ncx-only),

When `EpubReader.read(path, config)` is called,

Then no nav-label chunks SHALL be emitted, body-chapter chunk extraction SHALL proceed exactly as before this change, and no exception SHALL be raised.

---

### Requirement: prose text is chunked preserving per-node provenance for reinsertion

Signature: `chunk_prose(node_chunks: list[Chunk], config: JobConfig, provider: TranslationProvider) -> list[Chunk]`

Each input `Chunk` is one text node from EpubReader with `meta={"epub_item_href": str, "node_path": str, "chapter_index": int}` and `source_text` = the node's raw text (inline tags like `<em>` preserved).

The prose chunker SHALL:
- Skip empty/whitespace-only nodes (no empty chunks emitted).
- Group nodes by `meta["chapter_index"]`; NEVER batch across chapters.
- Within a chapter, greedily accumulate nodes while cumulative tokens (`provider.count_tokens`) stay within `config.prose_chunk_tokens` (default 800 tokens).
- When a single node exceeds the budget, split at sentence boundaries (`.`, `!`, `?` followed by whitespace or end-of-string).
- When a single sentence exceeds the budget, hard-split at the nearest word boundary ≤ budget and log exactly ONE WARNING per oversized sentence (containing chunk index and sentence character length — NOT one per fragment).

Each output `Chunk` SHALL carry:
- `source_text` = batched node texts joined with `"\n\n"`.
- `meta["prose_nodes"]` = ordered `list[{"epub_item_href": str, "node_path": str}]`, one entry per `"\n\n"`-separated segment of `source_text`, in the same order, so the writer can split on `"\n\n"` and map segment `i` → `prose_nodes[i].node_path`.
- `meta["hard_split"] = True` on hard-split chunks only.
- `index` = 0-based output index; `status` = PENDING.

This rule is normative for EPUB. PDF prose uses a compatible extension of this contract.

#### Scenario: nodes that fit within budget → single output chunk with full prose_nodes

Given two 200-token text-node Chunks in the same chapter (total 400 tokens, budget 800),

When `chunk_prose` processes them,

Then a single output Chunk SHALL be emitted with `source_text` equal to both node texts joined with `"\n\n"`, and `meta["prose_nodes"]` SHALL contain exactly 2 entries in input order, each mapping to the correct `node_path`.

#### Scenario: over-budget chapter splits into multiple chunks, prose_nodes correct per chunk

Given a chapter whose nodes total more than the token budget,

When `chunk_prose` processes them,

Then the output SHALL contain 2 or more chunks, each ≤ `prose_chunk_tokens`, and each chunk's `meta["prose_nodes"]` length SHALL equal `len(chunk.source_text.split("\n\n"))`.

#### Scenario: single over-budget node → hard-split, exactly one WARNING per sentence

Given a single text node containing one sentence of 1,500 tokens and a budget of 800 tokens,

When the prose chunker processes it,

Then the node SHALL be hard-split into fragments ≤ 800 tokens, exactly ONE WARNING SHALL be logged for that sentence (not one per fragment), the warning SHALL contain the sentence's character length, and each output chunk SHALL carry `meta["hard_split"]=True` and a `meta["prose_nodes"]` entry pointing to the original node.

#### Scenario: chapter boundary is never merged across chunks

Given two text-node Chunks from different `chapter_index` values, each well under the token budget individually,

When the prose chunker processes them,

Then the output SHALL contain exactly 2 chunks (one per chapter), and no single chunk SHALL contain nodes from more than one `chapter_index`. `meta["prose_nodes"]` in each output chunk SHALL only reference nodes from that chunk's chapter.

#### Scenario: provenance alignment — segment i maps to prose_nodes[i]

Given N text-node Chunks in the same chapter that fit within budget (producing one output chunk),

When the output chunk's `source_text` is split on `"\n\n"`,

Then the number of segments SHALL equal `len(meta["prose_nodes"])`, and segment `i` SHALL contain text originating from the node referenced by `prose_nodes[i]["node_path"]`.

#### Scenario: empty or whitespace-only nodes are skipped

Given input containing empty or whitespace-only text-node Chunks,

When `chunk_prose` processes them,

Then no output Chunk SHALL be emitted for those nodes.

---

### Requirement: inline EPUB tags are preserved in place (tags-in-text), with strip/reinsert fallback

EPUB text nodes SHALL use the same tag-handling pipeline as SRT (see subtitle-translation): inline formatting tags (`<em>`, `<strong>`, `<span>`, `<a>`, and similar inline HTML elements) are kept IN the text sent to the provider, the model is instructed to carry them with the translated words, and `markup.validate_tags` checks the count. On mismatch the orchestrator retries (≤2), then falls back to deterministic `markup.strip` → translate-plain → `markup.reinsert`. Only if the fallback also fails does the chunk become `ChunkStatus.FAILED`. Whether `ChunkStatus.FAILED` also sets `JobStatus.PAUSED` is governed by `JobConfig.continue_on_error` (see job-lifecycle's "run_job applies the continue_on_error gate on chunk failure"): the job pauses ONLY when `continue_on_error=False`; by default (`continue_on_error=True`) the run continues past the chunk, and the chunk's original text rides through to the writer as a pass-through (writers already fall back to `source_text` when `translated_text is None` — no writer change needed). Because EPUB prose is markup-dense, this shared behavior is the reason the tag-rework is a prerequisite for the EPUB reader/writer.

#### Scenario: EPUB italic tag round-trips

Given an EPUB text node `"A <em>critical</em> point."`,

When the markup pipeline processes it through a translation cycle,

Then the output SHALL contain exactly 2 inline tags (`<em>` and `</em>`) wrapping the translated equivalent word(s), and the surrounding translated text SHALL be coherent Spanish.

#### Scenario: fallback exhaustion with continue_on_error=True — chunk FAILED, job continues

Given an EPUB text node whose tags-in-text attempts AND strip/reinsert fallback both fail, and `JobConfig.continue_on_error=True` (default),

When the orchestrator finishes processing this chunk,

Then the chunk SHALL become `ChunkStatus.FAILED` with `translated_text=None`, the job SHALL NOT pause, and the run SHALL proceed to the next chunk.

#### Scenario: fallback exhaustion with continue_on_error=False — chunk FAILED, job PAUSED (unchanged prior contract)

Given an EPUB text node whose tags-in-text attempts AND strip/reinsert fallback both fail, and `JobConfig.continue_on_error=False` (`--strict`),

When the orchestrator finishes processing this chunk,

Then the chunk SHALL become `ChunkStatus.FAILED` with `translated_text=None`, and `job.status` SHALL be set to `JobStatus.PAUSED`, stopping the run — identical to the pre-existing contract.

#### Scenario: FAILED chunk's original text is written by the EPUB writer

Given a `FAILED` chunk with `translated_text=None` and `source_text` containing the original node text, produced under `continue_on_error=True`,

When `EpubWriter.write` reinserts this chunk,

Then the writer SHALL emit `source_text` in place of the missing translation (existing `None → source_text` fallback in `epub_writer.py`, no writer code change required).

---

### Requirement: EPUB reader emits nav-doc labels as translatable chunks with provenance

The EPUB reader SHALL locate the nav doc via `isinstance(item, epub.EpubNav)` alone (no filename check). For `toc`/`landmarks` sections (via `epub:type` XPath), it SHALL emit one `Chunk` per `<a>`, one per un-linked heading row (`<li><span>`, no `<a>`), and one for the nav's `<h1>/<h2>` heading. `page-list` SHALL be excluded (numeric, not prose).

| Meta key | Value |
|---|---|
| `epub_item_href` | nav doc href (existing key) |
| `node_path` | path to the `<a>`/`<span>`/heading itself, not its `<li>` |
| `chapter_index` | isolated bucket = `max(existing)+1` |
| `nav_href` | `<a href>` value at read time; `None` for headings |

#### Scenario: nav <a> labels emitted as chunks with node_path and nav_href

Given an EPUB fixture with `book.toc` populated (e.g. `epub.Link("ch1.xhtml", "Chapter One", "ch1")`, `epub.Link("ch2.xhtml", "Chapter Two", "ch2")`) so ebooklib generates a nav doc with non-empty `<ol><li><a>` entries,

When `EpubReader.read(path, config)` is called,

Then the resulting chunk list SHALL contain one `Chunk` per `<a>` in the nav's toc list, each `Chunk.source_text` equal to the link label text, each `meta["node_path"]` pointing at the `<a>` element, and each `meta["nav_href"]` equal to that `<a>`'s `href` attribute value.

#### Scenario: nav heading is emitted and carries nav_href=None

Given the same populated-toc fixture, where the generated nav doc has an `<h1>` or `<h2>` heading (e.g. "Contents") inside the toc `<nav>`,

When `EpubReader.read(path, config)` is called,

Then a `Chunk` SHALL be emitted for that heading element with `meta["nav_href"] = None` and `meta["node_path"]` pointing at the heading element.

#### Scenario: landmarks are emitted, page-list is excluded

Given an EPUB fixture whose nav doc contains a `<nav epub:type="landmarks">` section (e.g. a "Cover" link) AND a `<nav epub:type="page-list">` section (page-number links),

When `EpubReader.read(path, config)` is called,

Then chunks SHALL be emitted for the landmarks section's `<a>` labels, and NO chunk SHALL be emitted for any `<a>` inside the page-list section.

#### Scenario: nav-label chunks are isolated into their own chapter_index bucket

Given an EPUB fixture with 3 body chapters (`chapter_index` 0, 1, 2) and a populated nav doc,

When `EpubReader.read(path, config)` is called,

Then all nav-label chunks SHALL share a single `chapter_index` value of 3 (`max(0,1,2)+1`), distinct from every body-chapter `chapter_index`.

---

### Requirement: EPUB writer copies translated nav labels into toc.ncx by href match, at zero provider cost

After nav-doc `<a>`/`<span>`/heading patches are finalized within `_do_write`, the EPUB writer SHALL build a `nav_href → translated_label` lookup from nav-label `prose_nodes` entries (`meta["nav_href"]` + the node's `translated_text`). It SHALL then add a `.ncx` branch to `_patch_entry`: parse the `toc.ncx` XML, walk `navMap` recursively across all nested `navPoint` elements, and for each `navPoint` resolve its `<content src="...">` href, normalize away any fragment identifier and relative-path differences, and match against the lookup. On a match, the sibling `<navLabel><text>` content SHALL be replaced with the translated label; `src` and all other attributes SHALL remain untouched. A `navPoint` whose href finds no match in the lookup SHALL be left with its original (untranslated) label — no error, no crash. This step SHALL make NO provider/model call; it is a pure read-and-copy from data already produced by the v1 nav-doc translation. Books with no `toc.ncx` entry SHALL be unaffected (the step no-ops). Books with a `toc.ncx` but no `EpubNav` item SHALL leave the ncx completely untranslated (empty lookup, no crash) — this is an explicit non-goal, not a regression.

#### Scenario: ncx navLabel is replaced by href match, no provider call

Given a job whose nav doc `<a href="ch1.xhtml">` was translated to `"Capítulo Uno"`, and the source EPUB's `toc.ncx` has a `navPoint` with `<content src="ch1.xhtml"/>` and `<navLabel><text>Chapter One</text></navLabel>`,

When `EpubWriter.write` completes,

Then the output `toc.ncx`'s corresponding `navPoint`'s `<navLabel><text>` SHALL read `"Capítulo Uno"`, its `<content src="...">` attribute SHALL be unchanged, and no additional `TranslationProvider.translate` call SHALL have been made for this label.

#### Scenario: fragment-bearing ncx href matches a fragment-less nav_href

Given a translated nav label with `nav_href = "ch1.xhtml"`, and a `toc.ncx` `navPoint` whose `<content src="ch1.xhtml#sec2"/>`,

When the ncx-copy step runs,

Then the fragment `#sec2` SHALL be normalized away before matching, and that `navPoint`'s label SHALL be replaced with the translated label.

#### Scenario: unmatched navPoint is left untranslated, no crash

Given a `toc.ncx` with a `navPoint` whose `<content src="appendix.xhtml"/>` has no corresponding entry in the nav_href lookup,

When the ncx-copy step runs,

Then that `navPoint`'s `<navLabel><text>` SHALL remain its original source-language value, and no exception SHALL be raised.

#### Scenario: book with no toc.ncx is unaffected

Given an EPUB with no `.ncx` item (e.g. EPUB3-only, nav-doc-only book),

When `EpubWriter.write` completes,

Then the ncx-copy step SHALL no-op (no `.ncx` patch attempted), and nav-doc translation SHALL proceed exactly as in the nav-doc-only scenarios above.

#### Scenario: book with toc.ncx but no nav doc — ncx left untranslated, no crash (explicit non-goal)

Given an EPUB that has a `toc.ncx` item but no `EpubNav` item (legacy EPUB2-style book),

When `EpubReader.read` and `EpubWriter.write` run end to end,

Then no nav-label chunks SHALL be emitted (nothing to copy from), the `nav_href → translated_label` lookup SHALL be empty, the output `toc.ncx` SHALL retain its original source-language labels unchanged, and no exception SHALL be raised.

---

### Requirement: EPUB writer reinserts translated text using prose_nodes provenance

The EPUB writer SHALL reinsert translated text by splitting each output chunk's `translated_text` on `"\n\n"` and writing segment `i` into the XHTML node identified by `chunk.meta["prose_nodes"][i]["node_path"]` within the document identified by `chunk.meta["prose_nodes"][i]["epub_item_href"]`. Segments sharing a `node_path` SHALL be concatenated back into that single node. This mechanism applies unchanged to nav-label chunks: a nav `<a>`/`<span>`/heading node is patched via the same `epub_item_href` + `node_path` resolution, and `_set_element_content` replaces ONLY the element's text content — `href`, `id`, and all other attributes on the patched element and its ancestors SHALL remain byte-identical to the source. `node_path` resolution SHALL remain stable and correct when the nav document has sibling `<nav epub:type="landmarks">` and/or `<nav epub:type="page-list">` elements alongside the `toc` nav under `<body>` — read-time and write-time walks operate on the same original source structure, so positional sibling indices do not drift between read and write.

(Previously: this requirement described only body-chapter `prose_nodes` reinsertion; it did not address nav-doc patching or multi-nav-sibling `node_path` stability, since nav-doc chunks did not exist.)

#### Scenario: nav <a> label is patched, href preserved byte-for-byte

Given a translated nav-label chunk whose source was `<a href="ch1.xhtml">Chapter One</a>` with `translated_text = "Capítulo Uno"`,

When `EpubWriter.write` reinserts this chunk,

Then the output nav doc's corresponding `<a>` element SHALL have text content `"Capítulo Uno"` and its `href` attribute SHALL be the byte-identical string `"ch1.xhtml"`.

#### Scenario: node_path stays correct when landmarks and page-list nav siblings are present

Given a nav document with three sibling `<nav>` elements under `<body>` in source order `[landmarks, toc, page-list]`, and a translated label for an `<a>` inside the `toc` nav,

When `EpubWriter.write` reinserts this chunk,

Then the writer SHALL patch the correct `<a>` element inside the `toc` nav (not an element in the `landmarks` or `page-list` siblings), because the nav is located by `epub:type` at both read and write time, not by sibling position alone.

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

Given a source EPUB with 12 chapters,

When `EpubWriter.write(chunks, src_path, out_path)` completes,

Then `ebooklib.epub.read_epub(out_path)` SHALL succeed (no exception), and the number of EPUB items with media type `application/xhtml+xml` SHALL equal 12.

#### Scenario: images are byte-identical in output

Given a source EPUB containing 3 image files (PNG, JPEG),

When `EpubWriter.write(chunks, src_path, out_path)` completes,

Then each image in the output EPUB SHALL be byte-identical to its counterpart in the source EPUB.

#### Scenario: CSS stylesheets are preserved

Given a source EPUB containing a CSS stylesheet,

When `EpubWriter.write(chunks, src_path, out_path)` completes,

Then the output EPUB SHALL contain the same CSS file(s) with byte-identical content.

#### Scenario: no partial output on writer failure

Given that `EpubWriter.write` raises an exception partway through writing,

Then no incomplete output file SHALL be left at `out_path` (the writer SHALL write to a temp path and atomically rename on success).

#### Scenario: nav ol/li nesting and epub:type attributes survive nav-label patching

Given a source EPUB whose nav doc has nested `<ol><li><a>...</a></li></ol>` structure and `epub:type="toc"` on the toc `<nav>`,

When `EpubWriter.write` completes after translating nav labels,

Then the output nav doc SHALL preserve the identical `<ol>/<li>` nesting depth and count, and the `epub:type` attribute value on the toc `<nav>` SHALL be unchanged.

---

### Requirement: PDF reader extracts clean text from digital PDFs using pdfplumber by default

The PDF reader adapter `PdfPlumberReader` SHALL extract text from digital (non-scanned) PDF files using `pdfplumber`. It SHALL apply a post-extraction cleanup pipeline: strip repeated headers/footers (detected as lines appearing verbatim on ≥ 80% of pages), rejoin hyphenated line-breaks (`word-\n` → `word`), and detect chapter boundaries from heading patterns. The adapter SHALL NOT be used for scanned PDFs (OCR is out of scope).

**Implementation note**: The implementation uses `page.extract_text()` with no layout argument (pdfplumber default). `layout=True` inserts large amounts of positional whitespace padding that makes verbatim line-frequency header/footer detection unreliable. The default extraction produces clean newline-separated text and preserves reading order.

#### Scenario: header/footer stripping removes repeated boilerplate

Given a PDF where the string `"© 2020 Publisher Name"` appears on 40 out of 50 pages (80%),

When `PdfPlumberReader.read(path, config)` is called,

Then the extracted text SHALL not contain any occurrence of `"© 2020 Publisher Name"`.

#### Scenario: hyphenated line-break is rejoined

Given extracted text containing `"incompre-\nhensible"`,

When the cleanup pipeline runs,

Then the cleaned text SHALL contain `"incomprehensible"` with no hyphen or newline.

#### Scenario: PyMuPDF4LLM adapter is never imported by default

Given that no user-level code explicitly requests the PyMuPDF adapter,

When `api.py` wires up the `DocumentReader` for PDF source type,

Then `pdf_pymupdf_reader.py` SHALL NOT be imported (verified by inspecting `sys.modules` in a unit test with the default DI configuration).

---

### Requirement: EPUB reader honors declared XHTML encoding

The EPUB reader SHALL detect the encoding declared in each XHTML content document (via XML declaration, meta charset, or ebooklib item attributes) and decode content bytes using that encoding before parsing. Non-UTF-8 encodings (e.g. ISO-8859-1, Windows-1252) SHALL produce correct Unicode text; no U+FFFD replacement characters SHALL appear in extracted text nodes for well-formed non-UTF-8 XHTML chapters.

#### Scenario: non-UTF-8 chapter produces correct accented characters

Given an EPUB spine item whose XHTML declares `charset=iso-8859-1` and contains the byte sequence for `"é"` (0xE9) in ISO-8859-1,

When `EpubReader.read(path, config)` is called,

Then the resulting `Chunk.source_text` for that item SHALL contain `"é"` (U+00E9), and no U+FFFD replacement characters SHALL be present.

#### Scenario: UTF-8 chapters are unaffected

Given an EPUB where all spine items use standard UTF-8 encoding,

When `EpubReader.read(path, config)` is called,

Then all existing text extraction behavior is preserved (no regression).
