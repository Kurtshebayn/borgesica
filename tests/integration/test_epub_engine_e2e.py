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
    TranslationUnit,
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


def _make_epub_bytes(chapters: list[tuple[str, bytes]]) -> bytes:
    """Build a minimal valid EPUB in memory using ebooklib."""
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

    def translate(self, system: str, user: str, model: str) -> TranslationUnit:
        self.call_log.append((system, user, model))

        # Prefix each segment with "[ES] " — keeps tag count and structure
        segments = user.split("\n\n")
        translated_segments = ["[ES] " + seg for seg in segments]
        translation_text = "\n\n".join(translated_segments)

        return TranslationUnit(
            translation=translation_text,
            summary_update="Fake EPUB translation summary.",
            glossary_additions=[],
        )


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
