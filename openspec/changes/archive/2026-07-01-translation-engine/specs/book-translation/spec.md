# Spec: book-translation

Change: `translation-engine` · Capability: `book-translation`
Phase: spec · Status: draft · Artifact store: openspec

---

## ADDED Requirements

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

The EPUB reader SHALL traverse the EPUB's XHTML content documents in reading order (spine order as defined by the OPF). For each content document, it SHALL extract text nodes from the `<body>` element only, preserving paragraph and heading boundaries. Each `Chunk.meta` SHALL carry at minimum `{"epub_item_href": str, "node_path": str, "chapter_index": int}` to enable faithful reinsertion and chapter-boundary enforcement. `chapter_index` is 0-based per spine document (all nodes from the first spine document share `chapter_index=0`, all from the second share `chapter_index=1`, etc.). The reader returns one `Chunk` per text node. Structural markup (chapter headings, `<div>` wrappers) SHALL NOT be translated — only the text content of leaf nodes is extracted.

#### Scenario: spine order is respected

Given an EPUB with 3 chapters declared in spine order `[ch1.xhtml, ch2.xhtml, ch3.xhtml]`,

When `EpubReader.read(path, config)` is called,

Then the resulting `Chunk` list SHALL contain all chunks from `ch1.xhtml` before any chunk from `ch2.xhtml`, and all chunks from `ch2.xhtml` before any chunk from `ch3.xhtml`.

#### Scenario: images are not extracted as chunks

Given an EPUB where a content document contains `<img>` elements,

When `EpubReader.read(path, config)` is called,

Then no `Chunk` SHALL contain binary image data; image elements SHALL be left intact (tracked via `meta`) for the writer to reinsert.

---

### Requirement: prose text is chunked preserving per-node provenance for reinsertion

**M2-2R (provenance rework)** — supersedes the original M2-2 contract.

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

This rule is normative for EPUB (M2). PDF (M3) prose may use a compatible extension of this contract.

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

EPUB text nodes SHALL use the SAME tag-handling pipeline as SRT (see subtitle-translation): inline formatting tags (`<em>`, `<strong>`, `<span>`, `<a>`, and similar inline HTML elements) are kept IN the text sent to the provider, the model is instructed to carry them with the translated words, and `markup.validate_tags` checks the count. On mismatch the orchestrator retries (≤2), then falls back to deterministic `markup.strip` → translate-plain → `markup.reinsert`; only if the fallback also fails does the chunk become `ChunkStatus.FAILED` and the job `JobStatus.PAUSED`. Because EPUB prose is markup-dense, this shared behavior is the reason the M2 tag-rework (M2-0) is a prerequisite for the EPUB reader/writer.

#### Scenario: EPUB italic tag round-trips

Given an EPUB text node `"A <em>critical</em> point."`,

When the markup pipeline processes it through a translation cycle,

Then the output SHALL contain exactly 2 inline tags (`<em>` and `</em>`) wrapping the translated equivalent word(s), and the surrounding translated text SHALL be coherent Spanish.

---

### Requirement: EPUB writer reinserts translated text using prose_nodes provenance

The EPUB writer SHALL reinsert translated text by splitting each output chunk's `translated_text` on `"\n\n"` and writing segment `i` into the XHTML node identified by `chunk.meta["prose_nodes"][i]["node_path"]` within the document identified by `chunk.meta["prose_nodes"][i]["epub_item_href"]`. Segments sharing a `node_path` SHALL be concatenated back into that single node.

### Requirement: EPUB writer produces a valid, openable EPUB

The EPUB writer SHALL write the translated content back into a structurally valid EPUB using `ebooklib`. The output EPUB SHALL:
- Open without errors in `ebooklib.epub.read_epub`
- Preserve all original chapters in their original spine order
- Preserve all images (binary blobs unchanged)
- Preserve all original CSS stylesheets
- Preserve all internal EPUB structural metadata (OPF, NCX/NAV, container.xml)
- Replace only the translated text nodes; all surrounding structural HTML remains identical to the source

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

---

### Requirement: PDF reader (M3) extracts clean text from digital PDFs using pdfplumber by default

The PDF reader adapter `PdfPlumberReader` SHALL extract text from digital (non-scanned) PDF files using `pdfplumber`. It SHALL apply a post-extraction cleanup pipeline: strip repeated headers/footers (detected as lines appearing verbatim on ≥ 80% of pages), rejoin hyphenated line-breaks (`word-\n` → `word`), and detect chapter boundaries from heading patterns. The adapter SHALL NOT be used for scanned PDFs (OCR is out of scope for M3).

**Deviation (M3-FIX / W-M3-1)**: The spec originally said `layout=True`. The implementation deliberately uses `page.extract_text()` with no layout argument (pdfplumber default). `layout=True` inserts large amounts of positional whitespace padding to approximate visual layout, which makes verbatim line-frequency header/footer detection unreliable (padded lines never match verbatim). The default extraction produces clean newline-separated text and preserves reading order. This is technically superior for the use-case and is the accepted implementation; the original spec literal was incorrect.

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
