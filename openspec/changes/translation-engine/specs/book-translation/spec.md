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

The EPUB reader SHALL traverse the EPUB's XHTML content documents in reading order (spine order as defined by the OPF). For each content document, it SHALL extract text nodes from the `<body>` element only, preserving paragraph and heading boundaries. Each `Chunk.meta` SHALL carry at minimum `{"epub_item_href": str, "node_path": str}` to enable faithful reinsertion. Structural markup (chapter headings, `<div>` wrappers) SHALL NOT be translated — only the text content of leaf nodes is extracted.

#### Scenario: spine order is respected

Given an EPUB with 3 chapters declared in spine order `[ch1.xhtml, ch2.xhtml, ch3.xhtml]`,

When `EpubReader.read(path, config)` is called,

Then the resulting `Chunk` list SHALL contain all chunks from `ch1.xhtml` before any chunk from `ch2.xhtml`, and all chunks from `ch2.xhtml` before any chunk from `ch3.xhtml`.

#### Scenario: images are not extracted as chunks

Given an EPUB where a content document contains `<img>` elements,

When `EpubReader.read(path, config)` is called,

Then no `Chunk` SHALL contain binary image data; image elements SHALL be left intact (tracked via `meta`) for the writer to reinsert.

---

### Requirement: prose text is chunked at paragraph boundaries; over-long paragraphs split at sentence boundaries

The prose chunker SHALL split text into chunks that do not exceed the configured token budget (derived from `JobConfig.chunk_size` reinterpreted as a prose token ceiling; default 800 tokens). Paragraph boundaries are the preferred split point.

When a single paragraph exceeds the token budget, it SHALL be split at sentence boundaries (`.`, `!`, `?` followed by whitespace or end-of-string). When a single sentence exceeds the token budget, it SHALL be hard-split at the budget boundary with a WARNING logged (containing the job ID, chunk index, and the sentence's character length). This rule is normative for both EPUB (M2) and PDF (M3) prose.

#### Scenario: paragraph that fits is kept intact

Given a paragraph of 400 tokens and a token budget of 800,

When the prose chunker processes it,

Then the paragraph SHALL be emitted as a single chunk, unsplit.

#### Scenario: over-long paragraph splits at sentence boundary

Given a paragraph containing 3 sentences totalling 1,200 tokens, each sentence individually ≤ 800 tokens, with a budget of 800 tokens,

When the prose chunker processes it,

Then the paragraph SHALL be split at sentence boundaries into 2 or more chunks, each ≤ 800 tokens, with no mid-sentence break.

#### Scenario: single over-long sentence is hard-split with logged warning

Given a single sentence of 1,500 tokens and a budget of 800 tokens,

When the prose chunker processes it,

Then the sentence SHALL be split at the 800-token boundary (or the nearest word boundary at or below 800 tokens), and a WARNING SHALL be emitted to the logging system containing: job ID, chunk index, and sentence character length. Translation proceeds — this is NOT a job-stopping error.

#### Scenario: chapter boundary is never merged across chunks

Given two paragraphs in different EPUB chapters,

When the prose chunker processes them,

Then the chunks from chapter A and the chunks from chapter B SHALL not be merged into a single chunk. Each chapter boundary SHALL produce a chunk boundary.

---

### Requirement: inline EPUB tags are preserved in place (tags-in-text), with strip/reinsert fallback

EPUB text nodes SHALL use the SAME tag-handling pipeline as SRT (see subtitle-translation): inline formatting tags (`<em>`, `<strong>`, `<span>`, `<a>`, and similar inline HTML elements) are kept IN the text sent to the provider, the model is instructed to carry them with the translated words, and `markup.validate_tags` checks the count. On mismatch the orchestrator retries (≤2), then falls back to deterministic `markup.strip` → translate-plain → `markup.reinsert`; only if the fallback also fails does the chunk become `ChunkStatus.FAILED` and the job `JobStatus.PAUSED`. Because EPUB prose is markup-dense, this shared behavior is the reason the M2 tag-rework (M2-0) is a prerequisite for the EPUB reader/writer.

#### Scenario: EPUB italic tag round-trips

Given an EPUB text node `"A <em>critical</em> point."`,

When the markup pipeline processes it through a translation cycle,

Then the output SHALL contain exactly 2 inline tags (`<em>` and `</em>`) wrapping the translated equivalent word(s), and the surrounding translated text SHALL be coherent Spanish.

---

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

The PDF reader adapter `PdfPlumberReader` SHALL extract text from digital (non-scanned) PDF files using `pdfplumber` with `layout=True`. It SHALL apply a post-extraction cleanup pipeline: strip repeated headers/footers (detected as lines appearing verbatim on ≥ 80% of pages), rejoin hyphenated line-breaks (`word-\n` → `word`), and detect chapter boundaries from heading patterns. The adapter SHALL NOT be used for scanned PDFs (OCR is out of scope for M3).

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
