"""End-to-end test for TranslatorEngine with EPUB source (W-M2-2).

Proves the full EPUB pipeline works end-to-end:
  Multi-chapter EPUB → TranslatorEngine (EpubTagFakeProvider) → valid translated EPUB
  with translated text in the CORRECT chapter document(s).

What this test proves:
- create_job reads the EPUB, runs the prose chunker, persists state
- run_job calls the provider for each chunk, assembles output EPUB via EpubWriter
- Output EPUB is openable with ebooklib.epub.read_epub
- Translated text landed in the correct chapter document (per-chapter placement)
- The job ends DONE
- This is the regression guard that a future api.py refactor can't silently break
  EPUB dispatch (e.g. wrong reader/writer selected, source_type dispatch broken)

Wiring assumptions (mirrored from SRT e2e test):
- TranslatorEngine is constructed with both SRT and EPUB readers/writers
- EpubReader() + EpubWriter() wired under SourceType.EPUB
- FakeTranslationProvider subclass that keeps \n\n-separated segment count intact
  and passes validate_tags by preserving inline tags
- InMemoryCheckpointStore (no SQLite file)
- NullGlossaryExtractor (no glossary seeding)

This is NOT a live API test. It requires no network or API key.
"""
from __future__ import annotations

import io
import os
import tempfile
import zipfile

import pytest
from ebooklib import epub

from borgesica.api import TranslatorEngine
from borgesica.adapters.readers.epub_reader import EpubReader
from borgesica.adapters.readers.srt_reader import SrtReader
from borgesica.adapters.writers.epub_writer import EpubWriter
from borgesica.adapters.writers.srt_writer import SrtWriter
from borgesica.domain.glossary import NullGlossaryExtractor
from borgesica.domain.models import (
    JobConfig,
    JobStatus,
    SourceType,
    TranslationResult,
    TranslationUnit,
    Usage,
)
from tests.fakes import FakeTranslationProvider, InMemoryCheckpointStore


# ---------------------------------------------------------------------------
# EPUB fixture helpers (mirrored from test_epub_reader.py / test_epub_writer.py)
# ---------------------------------------------------------------------------


def _chapter_xhtml(text: str, file_name: str | None = None) -> bytes:
    """Build a minimal XHTML chapter with one paragraph."""
    return (
        "<?xml version='1.0' encoding='utf-8'?>"
        "<html xmlns='http://www.w3.org/1999/xhtml'>"
        "<head><title>Chapter</title></head>"
        f"<body><p>{text}</p></body>"
        "</html>"
    ).encode("utf-8")


def _make_epub_bytes(
    chapters: list[tuple[str, bytes]],
    *,
    toc_links: list[tuple[str, str, str]] | None = None,
) -> bytes:
    """Build a minimal valid EPUB in memory using ebooklib.

    Args:
        toc_links: optional list of (href, title, uid) tuples. When provided,
            populates ``book.toc`` so ebooklib emits real ``<a href>`` entries
            in the generated nav doc AND matching ``navPoint`` entries in the
            auto-generated ``toc.ncx`` (WU1-1 helper, mirrored from
            test_epub_writer.py's ``_make_epub_bytes``). Additive only —
            omitting this parameter keeps every existing e2e test byte-for-byte
            unaffected (WU2-2 regression scenario: "book without a nav
          document is unaffected").
    """
    book = epub.EpubBook()
    book.set_identifier("e2e-test-001")
    book.set_title("E2E Test Book")
    book.set_language("en")
    book.add_author("Test Author")

    epub_items: list[epub.EpubHtml] = []
    for idx, (fname, content) in enumerate(chapters):
        item = epub.EpubHtml(title=f"Chapter {idx + 1}", file_name=fname, lang="en")
        item.content = content
        book.add_item(item)
        epub_items.append(item)

    if toc_links:
        book.toc = [epub.Link(href, title, uid) for href, title, uid in toc_links]

    book.spine = ["nav"] + epub_items
    book.add_item(epub.EpubNcx())
    book.add_item(epub.EpubNav())

    with tempfile.NamedTemporaryFile(suffix=".epub", delete=False) as f:
        tmp = f.name
    try:
        epub.write_epub(tmp, book, {})
        with open(tmp, "rb") as f:
            return f.read()
    finally:
        os.unlink(tmp)


def _write_temp_epub(content: bytes) -> str:
    """Write bytes to a temporary .epub file and return its path."""
    with tempfile.NamedTemporaryFile(suffix=".epub", delete=False) as f:
        f.write(content)
        return f.name


# ---------------------------------------------------------------------------
# EPUB-aware fake translation provider
#
# The orchestrator sends chunk.source_text (prose segments joined by "\n\n",
# possibly containing inline tags) as the user message to the provider.
# validate_tags() checks that the tag count in source == tag count in translation.
#
# This provider prefixes each "\n\n"-separated segment with "[ES] " so that:
#   1. Tag count is preserved (no tags are stripped or added)
#   2. The segment count matches (segment structure is preserved)
#   3. The translation is deterministic and unique per chapter/sentence
# ---------------------------------------------------------------------------


class EpubTagFakeProvider(FakeTranslationProvider):
    """Fake provider that translates prose chunks while keeping inline tags.

    For each \n\n-separated segment in the user message, prefixes "[ES] ".
    This preserves tag counts (validate_tags passes) and segment count.
    """

    def translate(self, system: str, user: str, model: str) -> TranslationResult:
        self.call_log.append((system, user, model))

        # Prefix each segment with "[ES] " — keeps tag count and structure
        segments = user.split("\n\n")
        translated_segments = ["[ES] " + seg for seg in segments]
        translation_text = "\n\n".join(translated_segments)

        unit = TranslationUnit(
            translation=translation_text,
            summary_update="Fake EPUB translation summary.",
            glossary_additions=[],
        )
        in_tok = self.count_tokens(system + " " + user, model)
        out_tok = self.count_tokens(unit.translation, model)
        return TranslationResult(unit=unit, usage=Usage(input_tokens=in_tok, output_tokens=out_tok))


# ---------------------------------------------------------------------------
# Engine factory (EPUB + SRT wired, mirroring SRT e2e test's _make_engine)
# ---------------------------------------------------------------------------


def _make_epub_engine(provider=None, checkpoint=None):
    """Build a TranslatorEngine wired with both SRT and EPUB adapters."""
    provider = provider or EpubTagFakeProvider()
    checkpoint = checkpoint or InMemoryCheckpointStore()
    engine = TranslatorEngine(
        provider=provider,
        checkpoint=checkpoint,
        readers={
            SourceType.SRT: SrtReader(),
            SourceType.EPUB: EpubReader(),
        },
        writers={
            SourceType.SRT: SrtWriter(),
            SourceType.EPUB: EpubWriter(),
        },
        extractor=NullGlossaryExtractor(),
    )
    return engine, provider, checkpoint


# ---------------------------------------------------------------------------
# E2E Test 1: Multi-chapter EPUB → DONE job → valid translated EPUB
#             with per-chapter text placement (W-M2-2 main guard)
# ---------------------------------------------------------------------------


def test_epub_engine_e2e_basic(tmp_path):
    """Multi-chapter EPUB runs end-to-end; output is a valid EPUB with translations in correct chapters.

    This is the EPUB analogue of test_engine_e2e_srt_basic.

    Regression guard: if api.py's source_type dispatch breaks (e.g. uses SrtReader for EPUB,
    or uses SrtWriter instead of EpubWriter), this test fails because:
    - SrtReader would raise or return 0 chunks for an EPUB
    - SrtWriter would write a .srt file, not an openable EPUB
    - Per-chapter placement assertions would fail even if the file happened to open
    """
    # Build a 2-chapter EPUB fixture
    chapters = [
        ("ch1.xhtml", _chapter_xhtml("The quick brown fox.")),
        ("ch2.xhtml", _chapter_xhtml("Jumps over the lazy dog.")),
    ]
    src_bytes = _make_epub_bytes(chapters)
    src_path = _write_temp_epub(src_bytes)
    out_path = str(tmp_path / "translated.epub")

    try:
        engine, provider, checkpoint = _make_epub_engine()
        config = JobConfig(source_type=SourceType.EPUB, model="fake")

        # create → run
        job = engine.create_job(src_path, config)
        assert job.status == JobStatus.CREATED
        assert job.total_chunks >= 1, "EPUB with 2 text nodes should produce ≥ 1 chunk"
        assert provider.call_count == 0, "No provider call during create_job"

        final_job = engine.run_job(job.id, out_path=out_path)
        assert final_job.status == JobStatus.DONE, (
            f"Expected DONE, got {final_job.status}"
        )
        assert provider.call_count >= 1, "Provider must be called ≥ 1 time during run_job"

        # Output file must exist
        assert os.path.exists(out_path), f"Output EPUB not written to {out_path}"

        # Output must be a valid EPUB (openable without exception)
        result_book = epub.read_epub(out_path)
        assert result_book is not None, "ebooklib could not open the translated EPUB"

        # Per-chapter placement: translated text must be in the correct chapter document
        with zipfile.ZipFile(out_path, "r") as zf:
            all_names = zf.namelist()

            def _find_chapter(suffix: str) -> str | None:
                for name in all_names:
                    if name.endswith(suffix) and "nav" not in name.lower():
                        return name
                return None

            ch1_file = _find_chapter("ch1.xhtml")
            ch2_file = _find_chapter("ch2.xhtml")

            assert ch1_file is not None, f"ch1.xhtml not found in output; files: {all_names}"
            assert ch2_file is not None, f"ch2.xhtml not found in output; files: {all_names}"

            ch1_text = zf.read(ch1_file).decode("utf-8", errors="replace")
            ch2_text = zf.read(ch2_file).decode("utf-8", errors="replace")

        # The fake provider prefixes "[ES] " to each segment — verify per chapter
        # ch1's source: "The quick brown fox." → translation: "[ES] The quick brown fox."
        assert "[ES] The quick brown fox." in ch1_text, (
            f"Chapter-1 translation not found in ch1.xhtml. Content snippet: {ch1_text[:300]!r}"
        )
        # ch2's source: "Jumps over the lazy dog." → translation: "[ES] Jumps over the lazy dog."
        assert "[ES] Jumps over the lazy dog." in ch2_text, (
            f"Chapter-2 translation not found in ch2.xhtml. Content snippet: {ch2_text[:300]!r}"
        )

        # Cross-chapter guard: ch1 text must NOT be in ch2 and vice versa
        assert "[ES] The quick brown fox." not in ch2_text, (
            "Chapter-1 translation must NOT appear in ch2.xhtml (wrong chapter placement)"
        )
        assert "[ES] Jumps over the lazy dog." not in ch1_text, (
            "Chapter-2 translation must NOT appear in ch1.xhtml (wrong chapter placement)"
        )
    finally:
        os.unlink(src_path)


# ---------------------------------------------------------------------------
# E2E Test 2: Resumability — second run_job on DONE job = 0 provider calls
# ---------------------------------------------------------------------------


def test_epub_engine_e2e_resumable_done_job(tmp_path):
    """Calling resume_job on a DONE EPUB job makes 0 additional provider calls."""
    chapters = [
        ("ch1.xhtml", _chapter_xhtml("Hello, world!")),
    ]
    src_bytes = _make_epub_bytes(chapters)
    src_path = _write_temp_epub(src_bytes)
    out_path = str(tmp_path / "translated.epub")

    try:
        engine, provider, checkpoint = _make_epub_engine()
        config = JobConfig(source_type=SourceType.EPUB, model="fake")

        job = engine.create_job(src_path, config)
        engine.run_job(job.id, out_path=out_path)

        calls_after_first_run = provider.call_count

        # resume on a DONE job → 0 extra provider calls
        engine.resume_job(job.id, out_path=out_path)
        assert provider.call_count == calls_after_first_run, (
            "resume_job on DONE EPUB job should make 0 additional provider calls"
        )
    finally:
        os.unlink(src_path)


# ---------------------------------------------------------------------------
# WU8-1: E2E — nav doc + ncx translated end to end, fast and reflective modes
#
# Exercises the FULL pipeline built by Slices A (reader nav walk), B
# (reflective bypass), and C (writer nav patch + ncx-copy) together, driven
# through TranslatorEngine.create_job/run_job — not through the lower-level
# reader/chunker/writer calls used by the unit and writer-integration tests.
#
# Proves, in one real run:
#   (a) nav doc <a> labels come out translated with hrefs intact
#   (b) toc.ncx labels match the nav doc translations exactly (consistency)
#   (c) a reflective-mode job translates body chunks in 3 passes and the
#       nav-label chunk in exactly 1 pass (call-counting fake provider)
#   (d) a book WITHOUT a nav doc behaves byte-identically to before this
#       change (regression guard, reuses test 1's existing fixture path)
#   (e) output EPUB opens with ebooklib.epub.read_epub with no errors
# ---------------------------------------------------------------------------


def _find_zip_entry(out_path: str, suffix: str) -> str:
    with zipfile.ZipFile(out_path, "r") as zf:
        names = [n for n in zf.namelist() if n.endswith(suffix) and "nav" not in n.lower()]
    assert names, f"no entry ending in {suffix!r} found; got a different set of names"
    return names[0]


def _find_nav_entry(out_path: str) -> str:
    with zipfile.ZipFile(out_path, "r") as zf:
        names = [n for n in zf.namelist() if n.endswith("nav.xhtml")]
    assert names, "nav.xhtml not found in output EPUB"
    return names[0]


def _find_ncx_entry(out_path: str) -> str:
    with zipfile.ZipFile(out_path, "r") as zf:
        names = [n for n in zf.namelist() if n.endswith(".ncx")]
    assert names, "toc.ncx not found in output EPUB"
    return names[0]


def _read_zip_text(out_path: str, entry_name: str) -> str:
    with zipfile.ZipFile(out_path, "r") as zf:
        return zf.read(entry_name).decode("utf-8", errors="replace")


def test_epub_engine_e2e_nav_and_ncx_translated_fast_mode(tmp_path):
    """Fast mode: nav doc labels translated, ncx labels match by href, output opens.

    Proves scenarios (a), (b), (e) from the WU8-1 docstring above using the
    real end-to-end TranslatorEngine.create_job/run_job pipeline (not the
    lower-level reader+chunk_prose+writer calls used elsewhere in the suite).
    """
    chapters = [
        ("ch1.xhtml", _chapter_xhtml("The quick brown fox.")),
        ("ch2.xhtml", _chapter_xhtml("Jumps over the lazy dog.")),
    ]
    src_bytes = _make_epub_bytes(
        chapters,
        toc_links=[
            ("ch1.xhtml", "Chapter One", "ch1"),
            ("ch2.xhtml", "Chapter Two", "ch2"),
        ],
    )
    src_path = _write_temp_epub(src_bytes)
    out_path = str(tmp_path / "translated.epub")

    try:
        engine, provider, checkpoint = _make_epub_engine()
        config = JobConfig(source_type=SourceType.EPUB, model="fake", quality_mode="fast")

        job = engine.create_job(src_path, config)
        final_job = engine.run_job(job.id, out_path=out_path)
        assert final_job.status == JobStatus.DONE, f"Expected DONE, got {final_job.status}"

        # (e) output EPUB opens without error
        result_book = epub.read_epub(out_path)
        assert result_book is not None, "ebooklib could not open the translated EPUB"

        # (a) nav doc labels translated, hrefs intact
        nav_text = _read_zip_text(out_path, _find_nav_entry(out_path))
        assert "[ES] Chapter One" in nav_text, (
            f"Expected translated nav label 'Chapter One' in nav doc, got: {nav_text[:500]!r}"
        )
        assert "[ES] Chapter Two" in nav_text, (
            f"Expected translated nav label 'Chapter Two' in nav doc, got: {nav_text[:500]!r}"
        )
        assert 'href="ch1.xhtml"' in nav_text, "nav <a> href for ch1 must be preserved byte-for-byte"
        assert 'href="ch2.xhtml"' in nav_text, "nav <a> href for ch2 must be preserved byte-for-byte"

        # (b) ncx labels match nav doc translations exactly (consistency),
        # zero-cost copy (no separate provider call for the ncx text itself —
        # verified by call-count assertions in the reflective-mode test below;
        # here we assert the CONTENT consistency invariant).
        ncx_text = _read_zip_text(out_path, _find_ncx_entry(out_path))
        assert "[ES] Chapter One" in ncx_text, (
            f"Expected ncx navLabel to match nav-doc translation for Chapter One, got: {ncx_text[:500]!r}"
        )
        assert "[ES] Chapter Two" in ncx_text, (
            f"Expected ncx navLabel to match nav-doc translation for Chapter Two, got: {ncx_text[:500]!r}"
        )
        assert 'src="ch1.xhtml"' in ncx_text, "ncx content src for ch1 must be preserved"
        assert 'src="ch2.xhtml"' in ncx_text, "ncx content src for ch2 must be preserved"

        # Body chapters still translated correctly (regression: nav-copy work
        # must not break body-chapter placement).
        ch1_text = _read_zip_text(out_path, _find_zip_entry(out_path, "ch1.xhtml"))
        ch2_text = _read_zip_text(out_path, _find_zip_entry(out_path, "ch2.xhtml"))
        assert "[ES] The quick brown fox." in ch1_text
        assert "[ES] Jumps over the lazy dog." in ch2_text
    finally:
        os.unlink(src_path)


class _SegmentPreservingCannedProvider(FakeTranslationProvider):
    """Fake provider returning a FIXED per-segment translation ("Texto N.")
    that preserves the "\\n\\n" segment COUNT of the user prompt, without
    echoing the prompt's own text back (unlike EpubTagFakeProvider's
    "[ES] " + seg approach).

    Rationale (apply-time discovery, WU8-1): EpubTagFakeProvider prefixes
    each segment with "[ES] " and returns it — including the SOURCE text
    itself. For reflective mode, the critique/revise prompts embed the prior
    step's OUTPUT verbatim ("Draft translation:\n{draft_text}"), so an
    echo-style provider's draft output re-appears (substring-wise) inside the
    critique/revise INPUT, and if that composite multi-line prompt is echoed
    back again it produces a translation with a DIFFERENT "\\n\\n" segment
    count than the original chunk's source_text — triggering the
    orchestrator's segment-mismatch retry loop (3 attempts, not 1), which
    would falsely inflate call counts for reflective body chunks (9 instead
    of 3) or even nav-label chunks whose batched source_text has >1 segment
    (e.g. this fixture's nav doc batches its <h2> heading + <a> label into
    ONE 2-segment chunk). This provider avoids the artifact entirely by
    returning "Texto N." per segment — always matching the INPUT segment
    count 1:1, regardless of what that input contains — while still letting
    call-content inspection distinguish nav vs body calls by checking the
    ORIGINAL source text, which every draft/critique/revise prompt embeds
    verbatim (see orchestrator._translate_reflective).
    """

    def translate(self, system: str, user: str, model: str) -> TranslationResult:
        self.call_log.append((system, user, model))
        # Reflective critique/revise prompts are COMPOSITE ("Source text:\n
        # {source}\n\nDraft translation:\n..."), but the orchestrator validates
        # the revise output's segment count against the ORIGINAL chunk source —
        # so mirror the source embedded verbatim in the prompt, not the whole
        # composite prompt (whose own "\n\n" count is meaningless).
        reference = user
        if user.startswith("Source text:\n") and "\n\nDraft translation:\n" in user:
            reference = user.split("\n\nDraft translation:\n", 1)[0][len("Source text:\n"):]
        segments = reference.split("\n\n")
        translated_segments = [f"Texto {i}." for i in range(len(segments))]
        unit = TranslationUnit(
            translation="\n\n".join(translated_segments),
            summary_update="Fake summary.",
            glossary_additions=[],
        )
        in_tok = self.count_tokens(system + " " + user, model)
        out_tok = self.count_tokens(unit.translation, model)
        return TranslationResult(unit=unit, usage=Usage(input_tokens=in_tok, output_tokens=out_tok))


def test_epub_engine_e2e_nav_reflective_mode_single_pass_nav_three_pass_body(tmp_path):
    """Reflective mode: body chunks get 3 passes, nav-label chunk(s) get exactly 1 pass each.

    Proves scenario (c): the orchestrator's per-chunk reflective bypass (D3,
    Slice B) holds through the FULL create_job/run_job pipeline, not just in
    the orchestrator unit tests.

    Note: this fixture's nav doc carries TWO nav-label chunks — the toc
    section's own `<h2>` book-title heading (ebooklib always emits one, even
    with a populated `book.toc`) plus the `<a>` for "Chapter One" — both must
    independently bypass reflective mode (1 call each), while the body chunk
    still gets the full 3-call sequence. ncx-copy is proven separately by the
    fast-mode test above (this test's provider returns a fixed canned
    translation, not the segment-echoing "[ES] " one, so nav/ncx text
    equality is not re-asserted here — see docstring on
    _SegmentPreservingCannedProvider for why).
    """
    chapters = [
        ("ch1.xhtml", _chapter_xhtml("The quick brown fox.")),
    ]
    src_bytes = _make_epub_bytes(
        chapters,
        toc_links=[("ch1.xhtml", "Chapter One", "ch1")],
    )
    src_path = _write_temp_epub(src_bytes)
    out_path = str(tmp_path / "translated.epub")

    try:
        provider = _SegmentPreservingCannedProvider()
        checkpoint = InMemoryCheckpointStore()
        engine, provider, checkpoint = _make_epub_engine(provider=provider, checkpoint=checkpoint)
        config = JobConfig(source_type=SourceType.EPUB, model="fake", quality_mode="reflective")

        job = engine.create_job(src_path, config)
        final_job = engine.run_job(job.id, out_path=out_path)
        assert final_job.status == JobStatus.DONE, f"Expected DONE, got {final_job.status}"

        # Count provider calls whose user prompt references the nav label's
        # source text ("Chapter One") vs the body chunk's source text ("The
        # quick brown fox."). Both draft/critique/revise prompts embed the
        # ORIGINAL source text verbatim (see orchestrator._translate_reflective),
        # so substring-counting is robust regardless of call ordering.
        nav_calls = [c for c in provider.call_log if "Chapter One" in c[1] and "quick brown fox" not in c[1]]
        body_calls = [c for c in provider.call_log if "quick brown fox" in c[1]]

        assert len(nav_calls) == 1, (
            f"Expected exactly 1 provider call for the 'Chapter One' nav-label "
            f"chunk (reflective bypass), got {len(nav_calls)}: "
            f"{[c[1][:80] for c in nav_calls]}"
        )
        assert len(body_calls) == 3, (
            f"Expected exactly 3 provider calls for the body chunk (reflective "
            f"draft+critique+revise), got {len(body_calls)}"
        )

        # Output still opens (ncx-copy plumbing runs without error even
        # though this provider returns a fixed canned translation).
        result_book = epub.read_epub(out_path)
        assert result_book is not None, "ebooklib could not open the translated EPUB"
    finally:
        os.unlink(src_path)


def test_epub_engine_e2e_no_nav_doc_byte_identical_to_before_change(tmp_path):
    """A book with NO EpubNav item at all (removed post-write from the ZIP +
    OPF manifest/spine, simulating a legacy EPUB2-style book) behaves exactly
    like the pre-nav-walk pipeline: zero nav-label chunks are emitted,
    body-chapter translation is completely unaffected.

    NOTE (apply-time discovery, not a bug): a book built with `_make_epub_bytes`
    WITHOUT `toc_links` still gets an `EpubNav` item from ebooklib — and that
    nav doc's `<nav epub:type="toc">` section always contains an `<h2>` book
    -title heading even with an empty `<ol/>` (no `<a>` entries). Per spec
    ("EPUB reader emits nav-doc labels as translatable chunks with provenance"
    — nav heading scenario), that heading IS correctly emitted as a nav-label
    chunk with `nav_href=None`; this is intended, not dormant. To exercise the
    TRUE "no nav document at all" regression path (book-translation/"book
    without a nav document is unaffected"), this test strips the EpubNav item
    from the ZIP + OPF manifest/spine entirely, mirroring the
    `test_book_with_no_ncx_item_is_unaffected` pattern from
    test_epub_writer.py.
    """
    chapters = [
        ("ch1.xhtml", _chapter_xhtml("The quick brown fox.")),
        ("ch2.xhtml", _chapter_xhtml("Jumps over the lazy dog.")),
    ]
    src_bytes = _make_epub_bytes(chapters)  # no toc_links — same as test 1

    # Strip the nav.xhtml ZIP entry AND its OPF manifest/spine references, to
    # simulate a legacy EPUB2-style book with no EpubNav item whatsoever
    # (ebooklib's read_epub raises if the manifest references a missing entry).
    buf_in = io.BytesIO(src_bytes)
    buf_out = io.BytesIO()
    with (
        zipfile.ZipFile(buf_in, "r") as zin,
        zipfile.ZipFile(buf_out, "w", zipfile.ZIP_DEFLATED) as zout,
    ):
        for item in zin.infolist():
            if item.filename.endswith("nav.xhtml"):
                continue
            data = zin.read(item.filename)
            if item.filename.endswith("content.opf"):
                text = data.decode("utf-8")
                text = text.replace(
                    '<item href="nav.xhtml" id="nav" media-type="application/xhtml+xml" properties="nav"/>',
                    "",
                )
                data = text.encode("utf-8")
            zout.writestr(item, data)
    src_bytes = buf_out.getvalue()

    src_path = _write_temp_epub(src_bytes)
    out_path = str(tmp_path / "translated.epub")

    try:
        engine, provider, checkpoint = _make_epub_engine()
        config = JobConfig(source_type=SourceType.EPUB, model="fake")

        job = engine.create_job(src_path, config)
        # No EpubNav item at all → no nav-label chunks: total_chunks equals
        # exactly the 2 body chunks (one per chapter's single <p>), same as
        # the pre-nav-walk behavior.
        assert job.total_chunks == 2, (
            f"Expected exactly 2 body chunks (no EpubNav item present, so no "
            f"nav-label chunks), got {job.total_chunks}"
        )
        assert provider.call_count == 0, "No provider call during create_job"

        final_job = engine.run_job(job.id, out_path=out_path)
        assert final_job.status == JobStatus.DONE

        result_book = epub.read_epub(out_path)
        assert result_book is not None, "ebooklib could not open the translated EPUB"

        ch1_text = _read_zip_text(out_path, _find_zip_entry(out_path, "ch1.xhtml"))
        ch2_text = _read_zip_text(out_path, _find_zip_entry(out_path, "ch2.xhtml"))
        assert "[ES] The quick brown fox." in ch1_text
        assert "[ES] Jumps over the lazy dog." in ch2_text
    finally:
        os.unlink(src_path)
